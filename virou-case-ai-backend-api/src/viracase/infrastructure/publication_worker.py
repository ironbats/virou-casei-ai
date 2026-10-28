from sqlalchemy import select

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway, PublicationUncertain
from viracase.infrastructure.models import IntegrationConfig, Job, Operation, PublicationResource
from viracase.infrastructure.publication import PublicationService
from viracase.infrastructure.publication_migrations import apply_migrations


def execute_publication(worker, oid, token, gateway_factory=CloudflareGateway, builder=None):
    from viracase.infrastructure.publication_build import IsolatedBuild

    repo, service = worker.repo, PublicationService(worker.repo, builder or IsolatedBuild())
    current_stage = "publication_building"

    def checkpoint(stage, changes=None, external=False):
        nonlocal current_stage
        current_stage = stage
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            account = repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                raise RuleError("LEASE_LOST", "Operação assumida por outra execução.")
            repo.access(db, account, "publish")
            if op.cancel_requested and not op.result.get("external_effect_pending"):
                op.state, job.state = "cancelled", "done"
                record_progress(op, "cancelled_before_dispatch")
                return None
            project = repo.owned_project(db, account.id, op.project_id)
            cfg = db.get(IntegrationConfig, op.policy.get("execution_config_id", op.policy["config_id"]))
            connection = service.registry.connection_for(db, cfg)
            current = service.registry.latest(db, "cloudflare", True, connection.id) if connection else None
            if not current or current.id != cfg.id or service.configuration_reasons(cfg, project.stack):
                raise RuleError(
                    "CONFIGURATION_INVALID", "Configuração mudou ou perdeu validade durante a publicação."
                )
            if account.entitlement_version != op.payload["entitlement_version"]:
                raise RuleError("LIFECYCLE_LOCKED", "A vigência mudou durante a publicação.")
            resource = db.get(PublicationResource, project.id)
            if changes:
                resource.inventory = {**resource.inventory, **changes}
                if changes.get("database_id"):
                    resource.database_id = changes["database_id"]
            resource.config_id = cfg.id
            op.state = "running"
            op.result = {
                **op.result,
                "external_effect_pending": external or op.result.get("external_effect_pending", False),
            }
            record_progress(op, stage)
            from datetime import timedelta

            job.lease_until = utcnow() + timedelta(seconds=cfg.settings["timeout_seconds"] * 4 + 120)
            return {
                "policy": cfg.settings,
                "token": service.registry.vault.decrypt(cfg.id, cfg.credential),
                "name": resource.worker_name,
                "database_id": resource.database_id,
                "inventory": dict(resource.inventory),
                "stack": project.stack,
                "revision": op.payload["revision_id"],
                "actor": account.id,
                "pid": project.id,
                "subdomain": cfg.validation["subdomain"],
            }

    try:
        state = checkpoint("publication_building")
        if not state:
            return
        files = repo.source_files(state["actor"], state["pid"], state["revision"])
        artifact = service.builder.build(files, state["stack"], state["revision"], repo.sources.secrets)
        from viracase.domain.rules import canonical_hash

        checkpoint(
            "publication_building", {"artifact_sha256": artifact.get("digest") or canonical_hash(artifact)}
        )
        gateway = gateway_factory(state["policy"], state["token"])
        gateway.before_request = lambda method: checkpoint(current_stage, external=method != "GET")
        if not checkpoint("publication_provisioning", external=True):
            return
        database_id = state["database_id"]
        if not database_id:
            existing = gateway.database(state["name"])
            if existing and not state["inventory"].get("database_creation_started"):
                raise RuleError(
                    "RESOURCE_CONFLICT", "Já existe um banco remoto sem vínculo comprovado ao projeto."
                )
            if not existing:
                checkpoint("publication_provisioning", {"database_creation_started": True}, external=True)
                existing = gateway.create_database(state["name"])
            database_id = existing["uuid"]
            checkpoint("publication_migrating", {"database_id": database_id})
        apply_migrations(gateway, database_id, artifact["migrations"], state["inventory"], checkpoint)
        existing_worker = gateway.settings(state["name"])
        if existing_worker and "viroucaseai" not in existing_worker.get("tags", []):
            raise RuleError("RESOURCE_CONFLICT", "Nome de Worker ocupado por recurso fora deste projeto.")
        checkpoint("publication_uploading", {"candidate_revision_id": state["revision"]}, external=True)
        deployment = gateway.upload(state["name"], database_id, artifact)
        checkpoint("publication_routing", {"deployment_id": deployment}, external=True)
        gateway.enable(state["name"])
        url = f"https://{state['name']}.{state['subdomain']}.workers.dev"
        checkpoint("publication_verifying", external=True)
        gateway.verify(url, state["revision"])
        checkpoint(
            "publication_verified",
            {"url": url, "published_revision_id": state["revision"], "published_at": utcnow().isoformat()},
        )
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return
            resource = db.get(PublicationResource, op.project_id)
            resource.state = "published"
            op.state, job.state = "succeeded", "done"
            op.result = {
                **op.result,
                "url": url,
                "revision_id": state["revision"],
                "deployment_id": deployment,
                "verified_at": utcnow().isoformat(),
                "external_effect_pending": False,
            }
            record_progress(op, "publication_verified")
    except Exception as exc:
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return
            uncertain = isinstance(exc, PublicationUncertain) or bool(
                op.result.get("external_effect_pending")
            )
            op.state, job.state = ("reconciling", "reconciling") if uncertain else ("failed", "done")
            op.error_code = exc.code if isinstance(exc, RuleError) else "PUBLICATION_FAILED"
            record_progress(op, "publication_reconciling" if uncertain else "publication_failed")
