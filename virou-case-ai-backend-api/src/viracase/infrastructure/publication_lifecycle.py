from sqlalchemy import select

from viracase.domain.rules import RuleError, aware, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import PublicationResource


def synchronize_publication_visibility(repo, gateway_factory=CloudflareGateway):
    registry = Integrations(repo)
    with repo.sessions() as db:
        projects = list(
            db.scalars(
                select(PublicationResource.project_id).where(
                    PublicationResource.state.in_(["published", "suspended"])
                )
            )
        )
    for pid in projects:
        with repo.sessions.begin() as db:
            resource = db.get(PublicationResource, pid)
            account = repo.account(db, resource.account_id, True)
            period = repo.period(db, account)
            enabled = bool(
                period
                and not account.suspended
                and account.lifecycle == "active"
                and aware(period.purge_at) > utcnow()
            )
            desired = "published" if enabled else "suspended"
            if resource.state == desired:
                continue
            cfg = registry.for_resource(db, resource)
            gateway = gateway_factory(cfg.settings, registry.vault.decrypt(cfg.id, cfg.credential))
            gateway.enable(resource.worker_name, enabled)
            observed = gateway.request(
                "GET", gateway.account_path + "/workers/scripts/" + resource.worker_name + "/subdomain"
            )
            if not observed or observed.get("enabled") != enabled:
                raise RuleError(
                    "PUBLICATION_ROUTING_UNKNOWN", "A suspensão ou retomada da rota ainda não foi confirmada."
                )
            resource.state = desired


def purge_publications(repo, actor, fence, gateway_factory=CloudflareGateway):
    registry = Integrations(repo)
    with repo.sessions() as db:
        rows = db.scalars(select(PublicationResource).where(PublicationResource.account_id == actor)).all()
        ids = [row.project_id for row in rows]
    for pid in ids:
        with repo.sessions.begin() as db:
            account = repo.account(db, actor, True)
            if account.fence != fence or account.lifecycle != "purging":
                raise RuleError("LIFECYCLE_FENCE_CONFLICT", "O ciclo mudou antes da suspensão.")
            row = db.get(PublicationResource, pid)
            config = registry.for_resource(db, row)
            policy, secret = dict(config.settings), registry.vault.decrypt(config.id, config.credential)
            name, database_id = row.worker_name, row.database_id
            creating = row.inventory.get("database_creation_started")
            row.state = "purging"
        gateway = gateway_factory(policy, secret)
        if not database_id and creating:
            remote_database = gateway.database(name)
            database_id = remote_database["uuid"] if remote_database else None
        if gateway.settings(name):
            gateway.enable(name, False)
        if database_id:
            gateway.delete(name, database_id)
        elif gateway.settings(name):
            gateway.request("DELETE", gateway.account_path + "/workers/scripts/" + name)
        if gateway.settings(name) or (
            database_id and gateway.request("GET", gateway.account_path + "/d1/database/" + database_id)
        ):
            raise RuleError("PURGE_INCOMPLETE", "Cloudflare ainda contém recursos deste projeto.")
        with repo.sessions.begin() as db:
            account = repo.account(db, actor, True)
            if account.fence != fence or account.lifecycle != "purging":
                raise RuleError("LIFECYCLE_FENCE_CONFLICT", "O ciclo mudou durante a suspensão.")
            row = db.get(PublicationResource, pid)
            row.state = "purged"
    return {"resources_confirmed_absent": len(ids), "provider_backup_retention": "requires_operator_evidence"}
