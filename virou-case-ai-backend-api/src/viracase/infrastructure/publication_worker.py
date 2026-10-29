from sqlalchemy import select

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway, PublicationUncertain
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.models import IntegrationConfig, Job, Operation, PublicationResource
from viracase.infrastructure.publication import PublicationService
from viracase.infrastructure.publication_d1_diagnostics import sanitize_provider_diagnostic
from viracase.infrastructure.publication_logging import emit, exception_fields
from viracase.infrastructure.publication_migrations import apply_migrations, preflight_migrations
from viracase.infrastructure.publication_outcome import migration_identity, migration_needs_reconciliation, publication_diagnostic
from viracase.infrastructure.publication_recovery import recovery_status
from viracase.infrastructure.publication_verification import failure_record


def execute_publication(worker, oid, token, gateway_factory=CloudflareGateway, builder=None):
    from viracase.infrastructure.publication_build import IsolatedBuild

    repo, service = worker.repo, PublicationService(worker.repo, builder or IsolatedBuild())
    current_stage = "publication_building"
    authorized, executor_digest = False, None

    def checkpoint(stage, changes=None, external=None, upload_started=False):
        nonlocal current_stage, authorized, executor_digest
        if stage != current_stage:
            emit("publication_stage", operation_id=oid, stage=stage)
        current_stage = stage
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            account = repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token or job.state != "running":
                raise RuleError("LEASE_LOST", "Operação assumida por outra execução.")
            repo.access(db, account, "publish")
            resource = db.get(PublicationResource, op.project_id)
            if (
                op.cancel_requested and not op.result.get("external_effect_pending")
                and not migration_needs_reconciliation((resource.inventory or {}).get("migration_in_progress"))
            ):
                op.state, job.state = "cancelled", "done"
                record_progress(op, "cancelled_before_dispatch")
                return None
            project = repo.owned_project(db, account.id, op.project_id)
            cfg = db.get(IntegrationConfig, op.policy.get("execution_config_id", op.policy["config_id"]))
            connection = service.registry.connection_for(db, cfg)
            current = service.registry.latest(db, "cloudflare", True, connection.id) if connection else None
            invalid = (
                not current or current.id != cfg.id or cfg.state != "active"
                or not cfg.validation.get("ready") or cfg.validation.get("publication_check_blocked")
                or project.stack in cfg.validation.get("publication_check_blocked_stacks", [])
                or repo.config.restore_hold or not repo.config.lifecycle_enabled
            )
            if not authorized:
                invalid = invalid or bool(service.configuration_reasons(cfg, project.stack))
            else:
                invalid = invalid or service.builder.fingerprint() != executor_digest or not service.builder.available()
            if invalid:
                raise RuleError(
                    "CONFIGURATION_INVALID", "Configuração mudou ou perdeu validade durante a publicação."
                )
            if account.entitlement_version != op.payload["entitlement_version"]:
                raise RuleError("LIFECYCLE_LOCKED", "A vigência mudou durante a publicação.")
            if changes:
                changes = dict(changes)
                for name in ("migration_in_progress", "migration_last_attempt", "migration_plan"):
                    if isinstance(changes.get(name), dict):
                        changes[name] = {
                            **changes[name], "operation_id": oid, "revision_id": op.payload["revision_id"],
                        }
                resource.inventory = {**resource.inventory, **changes}
                if changes.get("database_id"):
                    resource.database_id = changes["database_id"]
            resource.config_id = cfg.id
            op.state = "running"
            attempt = dict(op.result.get("publication_attempt") or {
                "version": 1, "revision_id": op.payload["revision_id"], "upload_started": False,
            })
            attempt["stage"] = stage
            if upload_started or changes and changes.get("deployment_id"):
                attempt["upload_started"] = True
            if changes and changes.get("deployment_id"):
                attempt["deployment_id"] = changes["deployment_id"]
            op.result = {
                **op.result,
                "external_effect_pending": op.result.get("external_effect_pending", False)
                if external is None else external,
                "publication_attempt": attempt,
            }
            op.result = {
                **op.result,
                "publication_diagnostic": publication_diagnostic(
                    op.result, resource.inventory, stage=stage, operation_id=oid, revision_id=op.payload["revision_id"],
                ),
            }
            authorized, executor_digest = True, service.builder.fingerprint()
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
                "source_hash": op.payload.get("source_hash"),
                "migration_validation": op.result.get("migration_validation"),
                "actor": account.id,
                "pid": project.id,
                "subdomain": cfg.validation["subdomain"],
            }

    try:
        state = checkpoint("publication_building")
        if not state:
            return
        files = repo.source_files(state["actor"], state["pid"], state["revision"])
        from viracase.domain.rules import canonical_hash
        from viracase.infrastructure.publication_migration_context import migration_validation_for_revision
        from viracase.infrastructure.publication_migration_validation import validate_migration_plan

        if state["source_hash"] and canonical_hash(files) != state["source_hash"]:
            raise RuleError("ARTIFACT_IDENTITY_MISMATCH", "Os arquivos não correspondem à revisão autorizada para publicação.")
        preflight_migrations(files, state["inventory"])
        with repo.sessions() as db:
            evidence = migration_validation_for_revision(db, state["actor"], state["revision"])
        evidence = validate_migration_plan(
            files, revision_id=state["revision"], previous_evidence=evidence or state["migration_validation"],
        )
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token or job.state != "running":
                raise RuleError("LEASE_LOST", "Operação assumida por outra execução.")
            op.result = {**op.result, "migration_validation": evidence}
        artifact = service.builder.build(
            files, state["stack"], state["revision"], (*repo.sources.secrets, state["token"])
        )
        checkpoint(
            "publication_building", {"artifact_sha256": artifact.get("digest") or canonical_hash(artifact)}
        )
        gateway = gateway_factory(state["policy"], state["token"])

        def before_request(method):
            if not checkpoint(
                current_stage,
                external=True if method != "GET" else None,
                upload_started=current_stage == "publication_uploading" and method == "PUT",
            ):
                raise RuleError("PUBLICATION_CANCELLED", "Publicação cancelada antes do envio.")

        gateway.before_request = before_request
        if not checkpoint("publication_provisioning", external=False):
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
            checkpoint("publication_migrating", {"database_id": database_id}, external=False)
        apply_migrations(
            gateway, database_id, artifact["migrations"], state["inventory"], checkpoint,
            revision_id=state["revision"], source_files=files, validation_evidence=evidence,
        )
        checkpoint("publication_migrating", external=False)
        existing_worker = gateway.settings(state["name"])
        if existing_worker and "viroucaseai" not in existing_worker.get("tags", []):
            raise RuleError("RESOURCE_CONFLICT", "Nome de Worker ocupado por recurso fora deste projeto.")
        checkpoint("publication_uploading", {"candidate_revision_id": state["revision"]}, external=False)
        deployment = gateway.upload(state["name"], database_id, artifact)
        checkpoint("publication_routing", {"deployment_id": deployment}, external=True)
        gateway.enable(state["name"])
        url = f"https://{state['name']}.{state['subdomain']}.workers.dev"
        checkpoint("publication_verifying", external=True)
        verification = gateway.verify(url, state["revision"])
        if isinstance(verification, dict) and verification.get("passed") is not True:
            raise RuleError("PUBLICATION_VERIFICATION_FAILED", "O funcionamento da publicação não foi confirmado.")
        checkpoint("publication_verified")
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            account = repo.account(db, op.account_id, True)
            configuration_lock(db, "integration:cloudflare")
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token or job.state != "running":
                return
            repo.access(db, account, "publish")
            cfg = db.get(IntegrationConfig, op.policy.get("execution_config_id", op.policy["config_id"]))
            connection = service.registry.connection_for(db, cfg)
            current = service.registry.latest(db, "cloudflare", True, connection.id) if connection else None
            if (
                not current or current.id != cfg.id or cfg.state != "active"
                or not cfg.validation.get("ready") or cfg.validation.get("publication_check_blocked")
                or state["stack"] in cfg.validation.get("publication_check_blocked_stacks", [])
                or repo.config.restore_hold or not repo.config.lifecycle_enabled
                or service.builder.fingerprint() != executor_digest or not service.builder.available()
            ):
                raise RuleError("CONFIGURATION_INVALID", "A configuração mudou antes da confirmação da publicação.")
            if account.entitlement_version != op.payload["entitlement_version"]:
                raise RuleError("LIFECYCLE_LOCKED", "A vigência mudou antes da confirmação da publicação.")
            resource = db.get(PublicationResource, op.project_id)
            resource.state = "published"
            resource.inventory = {
                **resource.inventory, "url": url, "published_revision_id": state["revision"],
                "published_at": utcnow().isoformat(),
            }
            op.state, job.state = "succeeded", "done"
            op.result = {
                **op.result,
                "url": url,
                "revision_id": state["revision"],
                "deployment_id": deployment,
                "verified_at": utcnow().isoformat(),
                "external_effect_pending": False,
                "remote_verification": verification,
                "publication_diagnostic": {
                    **publication_diagnostic(
                        op.result, resource.inventory, stage="publication_verified",
                        operation_id=oid, revision_id=op.payload["revision_id"],
                    ),
                    "effect_status": "confirmed",
                },
            }
            if op.result.get("publication_recovery"):
                op.result = {
                    **op.result,
                    "publication_recovery": recovery_status(
                        "resolved", previous=op.result["publication_recovery"], resolution="published",
                        last_checked_at=utcnow().isoformat(), next_attempt_at=None, last_error_code=None,
                    ),
                }
            job.lease_token, job.lease_until = None, None
            record_progress(op, "publication_verified")
    except Exception as exc:
        current = state if "state" in locals() and state else {}
        secrets = (*repo.sources.secrets, current.get("token", ""))
        failure = failure_record(exc, current_stage, stack=current.get("stack"), secrets=secrets)
        if isinstance(failure.get("build"), dict):
            failure["build"] = {
                **failure["build"],
                "operation_id": oid,
                "correlation_id": oid,
                "revision_id": current.get("revision"),
            }
        fields = {"build": failure["build"]} if "build" in failure else {}
        emit(
            "publication_failed",
            "error",
            operation_id=oid,
            stage=current_stage,
            **exception_fields(exc),
            **fields,
        )
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return
            if op.state == "cancelled" and job.state == "done":
                return
            resource = db.get(PublicationResource, op.project_id)
            attempt = dict(op.result.get("publication_attempt", {}))
            provider = sanitize_provider_diagnostic(getattr(exc, "provider_diagnostic", None))
            effect_proven_absent = provider.get("effect_outcome") == "not_sent"
            resolution = getattr(exc, "migration_resolution", None)
            pending = (resource.inventory or {}).get("migration_in_progress") if resource else None
            if (
                isinstance(resolution, dict) and isinstance(pending, dict)
                and job.state == "running" and op.state == "running"
                and (not isinstance(exc, RuleError) or exc.code not in {"LEASE_LOST", "OPERATION_CONFLICT", "VERSION_CONFLICT"})
                and resolution.get("protocol_version") == pending.get("protocol_version") == 2
                and resolution.get("phase") == "sql_rejected" and pending.get("phase") == "sql_sending"
                and resolution.get("effect_outcome") in ("none", "not_sent")
                and provider.get("effect_outcome") == resolution.get("effect_outcome")
                and provider.get("method") == "POST"
                and provider.get("route", "").endswith("/d1/database/{database}/query")
                and migration_identity(resolution) is not None
                and migration_identity(resolution) == migration_identity(pending)
                and resolution.get("operation_id") == pending.get("operation_id") == oid
                and resolution.get("revision_id") == pending.get("revision_id") == op.payload["revision_id"]
            ):
                resource.inventory = {
                    **resource.inventory, "migration_in_progress": None,
                    "migration_last_attempt": {**resolution, "provider": provider},
                }
                op.result = {**op.result, "external_effect_pending": False}
                effect_proven_absent = True
            if (
                isinstance(exc, PublicationUncertain) and current_stage == "publication_uploading"
                and not provider
            ):
                attempt["upload_started"] = True
            if (
                not attempt.get("deployment_id") and provider.get("method") == "PUT"
                and (
                    provider.get("effect_outcome") == "not_sent"
                    or provider.get("status") in {400, 401, 403, 404, 409, 413, 422, 429}
                    and not isinstance(exc, PublicationUncertain)
                )
            ):
                attempt["upload_started"] = False
            uncertain = bool(
                isinstance(exc, PublicationUncertain) and not effect_proven_absent
                or isinstance(exc, RuleError) and exc.code == "MIGRATION_RESULT_UNKNOWN"
                or resource and migration_needs_reconciliation(resource.inventory.get("migration_in_progress"))
                or attempt.get("upload_started")
                or not isinstance(exc, RuleError) and op.result.get("external_effect_pending") and not effect_proven_absent
            )
            op.state, job.state = ("reconciling", "reconciling") if uncertain else ("failed", "done")
            op.error_code = exc.code if isinstance(exc, RuleError) else "PUBLICATION_FAILED"
            op.result = {
                **op.result, "publication_failure": failure, "publication_attempt": attempt,
                "external_effect_pending": uncertain,
            }
            op.result = {
                **op.result,
                "publication_diagnostic": publication_diagnostic(
                    op.result, resource.inventory if resource else None, stage=current_stage, failure=failure,
                    operation_id=oid, revision_id=op.payload["revision_id"],
                ),
            }
            if uncertain:
                op.result = {
                    **op.result,
                    "publication_recovery": recovery_status(
                        "scheduled", previous=op.result.get("publication_recovery"),
                        next_attempt_at=utcnow().isoformat(), last_error_code=op.error_code,
                    ),
                }
                job.available_at = utcnow()
            elif op.result.get("publication_recovery"):
                op.result = {
                    **op.result,
                    "publication_recovery": recovery_status(
                        "resolved", previous=op.result["publication_recovery"], resolution="pre_upload_failure",
                        last_checked_at=utcnow().isoformat(), next_attempt_at=None, last_error_code=op.error_code,
                    ),
                }
            job.lease_token, job.lease_until = None, None
            record_progress(op, "publication_reconciling" if uncertain else "publication_failed")
