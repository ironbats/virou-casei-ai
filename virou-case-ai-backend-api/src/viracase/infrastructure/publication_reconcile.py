from sqlalchemy import select

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Audit, Job, Operation, PublicationResource
from viracase.infrastructure.publication_migrations import LEDGER
from viracase.infrastructure.repository import operation_dto


def reconcile_publication(repo, actor, oid, key, reason, gateway_factory=CloudflareGateway):
    registry = Integrations(repo)
    with repo.sessions() as db:
        registry.require_admin(repo.account(db, actor))
    recovered = repo.recover_intention(actor, "publication:reconcile:" + oid, key, {"reason": reason})
    if recovered is not None:
        return recovered
    with repo.sessions() as db:
        registry.require_admin(repo.account(db, actor))
        op = db.get(Operation, oid)
        if not op or op.type != "publication" or op.state != "reconciling":
            raise RuleError("NOT_FOUND", "Publicação em reconciliação indisponível.", 404)
        resource = db.get(PublicationResource, op.project_id)
        config = registry.for_resource(db, resource)
        gateway = gateway_factory(config.settings, registry.vault.decrypt(config.id, config.credential))
        name, revision = resource.worker_name, op.payload["revision_id"]
        inventory = dict(resource.inventory)
        target, owner = op.project_id, op.account_id
    database = gateway.database(name)
    if database and not inventory.get("database_creation_started") and not inventory.get("database_id"):
        raise RuleError("RESOURCE_CONFLICT", "Banco remoto sem criação registrada para este projeto.")
    pending = inventory.get("migration_in_progress")
    if pending:
        if not database:
            raise RuleError("MIGRATION_RESULT_UNKNOWN", "Banco ausente durante reconciliação da migration.")
        receipts = gateway.query(
            database["uuid"], f"SELECT name, sha256 FROM {LEDGER} WHERE name = ?", [pending["name"]]
        )
        matches = [
            r for result in receipts for r in result.get("results", []) if r["sha256"] == pending["sha256"]
        ]
        if not matches:
            raise RuleError(
                "MIGRATION_RESULT_UNKNOWN",
                "Não há recibo remoto da migration. O sistema não irá repeti-la: exige inspeção e reparo pelo responsável no D1.",
            )
        inventory.update(
            migrations={**inventory.get("migrations", {}), pending["name"]: pending["sha256"]},
            migration_in_progress=None,
        )
    remote = gateway.settings(name)
    verified, url = False, inventory.get("url")
    if remote:
        tags = remote.get("tags", [])
        bindings = remote.get("bindings", [])
        if (
            "viroucaseai" not in tags
            or not database
            or not any(
                b.get("type") == "d1" and b.get("name") == "DB" and b.get("id") == database["uuid"]
                for b in bindings
            )
        ):
            raise RuleError(
                "RESOURCE_CONFLICT", "Worker ou binding remoto não corresponde ao inventário autorizado."
            )
        if revision in tags:
            subdomain = gateway.request("GET", gateway.account_path + "/workers/subdomain")
            label = subdomain.get("subdomain", "") if subdomain else ""
            import re

            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", label):
                raise RuleError("CONFIGURATION_INVALID", "Subdomínio remoto inválido.")
            url = f"https://{name}.{label}.workers.dev"
            try:
                gateway.verify(url, revision)
                verified = True
            except RuleError:
                verified = False

    def command(db, operator):
        registry.require_admin(operator)
        account = repo.account(db, owner, True)
        if account.lifecycle != "active":
            raise RuleError("LIFECYCLE_LOCKED", "O projeto está em encerramento de acesso.")
        row = db.get(Operation, oid)
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        if row.state != "reconciling" or job.state != "reconciling":
            raise RuleError("OPERATION_CONFLICT", "A operação mudou durante a consulta remota.")
        resource = db.get(PublicationResource, target)
        resource.inventory = inventory
        if database:
            resource.database_id = database["uuid"]
            resource.inventory = {**resource.inventory, "database_id": database["uuid"]}
        if verified:
            resource.state = "published"
            resource.inventory = {
                **resource.inventory,
                "url": url,
                "published_revision_id": revision,
                "published_at": utcnow().isoformat(),
            }
            row.state, job.state = "succeeded", "done"
            row.result = {
                **row.result,
                "url": url,
                "revision_id": revision,
                "verified_at": utcnow().isoformat(),
                "external_effect_pending": False,
            }
            record_progress(row, "publication_verified")
        else:
            repo.access(db, account, "publish")
            from viracase.infrastructure.publication import PublicationService

            current = registry.for_resource(db, resource)
            project = repo.owned_project(db, owner, target)
            if (
                not current
                or current.settings["account_id"] != resource.remote_account_id
                or PublicationService(repo).configuration_reasons(current, project.stack)
            ):
                raise RuleError(
                    "CONFIGURATION_INVALID", "Valide e qualifique a configuração atual antes de retomar."
                )
            row.policy = {
                **row.policy,
                "execution_config_id": current.id,
                "execution_version": current.version,
            }
            row.payload = {**row.payload, "entitlement_version": account.entitlement_version}
            row.state, job.state, job.lease_token = "queued", "queued", None
            job.available_at = utcnow()
            record_progress(row, "queued")
        row.error_code = None
        db.add(Audit(actor_id=actor, action="publication:reconcile", resource_id=oid, reason=reason))
        return operation_dto(row, admin=True)

    return repo.mutate(actor, "publication:reconcile:" + oid, key, {"reason": reason}, command)
