from datetime import timedelta

from sqlalchemy import delete, select

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import (
    Audit,
    Draft,
    Job,
    Operation,
    Project,
    PublicationResource,
    RecommendationRun,
    Revision,
)


def request_deletion(repo, actor, pid, key, version, confirmation):
    def command(db, account):
        project = db.scalar(select(Project).where(Project.id == pid, Project.account_id == actor))
        if not project or project.state == "deleted":
            raise RuleError("NOT_FOUND", "Projeto indisponível.", 404)
        if project.version != version:
            raise RuleError("VERSION_CONFLICT", "O projeto mudou. Atualize antes de excluir.", 412)
        if confirmation != project.name:
            raise RuleError("VALIDATION_ERROR", "Digite o nome exato do projeto para confirmar.", 400)
        if db.scalar(
            select(Operation.id)
            .where(Operation.project_id == pid, Operation.state.in_(["queued", "running", "reconciling"]))
            .limit(1)
        ) or db.scalar(
            select(RecommendationRun.id)
            .where(
                RecommendationRun.project_id == pid,
                RecommendationRun.state == "pending",
                RecommendationRun.created_at > utcnow() - timedelta(minutes=5),
            )
            .limit(1)
        ):
            raise RuleError(
                "OPERATION_CONFLICT",
                "Aguarde as operações e recomendações do projeto terminarem antes de excluir.",
            )
        project.state, project.version = "deleting", project.version + 1
        op = Operation(
            account_id=actor, project_id=pid, type="project_deletion", progress_stage="deletion_queued"
        )
        db.add(op)
        db.flush()
        record_progress(op, "deletion_queued")
        db.add(Job(operation_id=op.id))
        db.add(Audit(actor_id=actor, action="project:delete_requested", resource_id=pid))
        return {"id": op.id, "project_id": pid, "state": "queued"}

    return repo.mutate(
        actor,
        f"delete-project:{pid}",
        key,
        {"version": version, "confirmation": confirmation},
        command,
        "write",
    )


def execute_deletion(worker, oid, token, gateway_factory=CloudflareGateway):
    repo = worker.repo
    registry = Integrations(repo)
    with repo.sessions.begin() as db:
        op = db.get(Operation, oid)
        repo.account(db, op.account_id, True)
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        if job.lease_token != token:
            return
        project = db.get(Project, op.project_id)
        if not project or project.state != "deleting":
            raise RuleError("OPERATION_CONFLICT", "A exclusão não está mais pendente.")
        op.state = "running"
        record_progress(op, "deletion_remote")
        actor, pid = op.account_id, op.project_id
        resource = db.get(PublicationResource, pid)
        remote = None
        if resource and resource.state != "purged":
            cfg = registry.for_resource(db, resource)
            remote = (
                dict(cfg.settings),
                registry.vault.decrypt(cfg.id, cfg.credential),
                resource.worker_name,
                resource.database_id,
                bool(resource.inventory.get("database_creation_started")),
            )
            resource.state = "purging"
    if remote:
        policy, secret, name, database_id, creating = remote
        gateway = gateway_factory(policy, secret)

        def renew(method):
            with repo.sessions.begin() as db:
                repo.account(db, actor, True)
                job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
                project = db.get(Project, pid)
                if job.lease_token != token or not project or project.state != "deleting":
                    raise RuleError("LEASE_LOST", "A exclusão foi assumida por outra execução.")
                job.lease_until = utcnow() + timedelta(seconds=policy["timeout_seconds"] * 4 + 120)

        gateway.before_request = renew
        if not database_id and creating:
            found = gateway.database(name)
            database_id = found["uuid"] if found else None
        if gateway.settings(name):
            gateway.enable(name, False)
        if database_id:
            gateway.delete(name, database_id)
        elif gateway.settings(name):
            gateway.request("DELETE", gateway.account_path + "/workers/scripts/" + name)
        if gateway.settings(name) or (
            database_id and gateway.request("GET", gateway.account_path + "/d1/database/" + database_id)
        ):
            raise RuleError(
                "PURGE_INCOMPLETE", "A Cloudflare ainda contém recursos. Tente retomar a exclusão."
            )
    with repo.sessions.begin() as db:
        op = db.get(Operation, oid)
        repo.account(db, actor, True)
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        if job.lease_token != token:
            return
        record_progress(op, "deletion_sources")
        repo.sources.remove_project(actor, pid)
        db.execute(delete(Draft).where(Draft.account_id == actor, Draft.target == pid))
        db.execute(
            delete(Draft).where(
                Draft.account_id == actor, Draft.kind == "migration_plan",
                Draft.target.in_(select(Revision.id).where(Revision.project_id == pid, Revision.account_id == actor)),
            )
        )
        db.execute(delete(RecommendationRun).where(RecommendationRun.project_id == pid))
        from viracase.infrastructure.integration_traces import TraceStore

        TraceStore(repo).purge_project(db, project_id=pid)
        db.execute(delete(Revision).where(Revision.project_id == pid))
        for previous in db.scalars(select(Operation).where(Operation.project_id == pid, Operation.id != oid)):
            previous.payload = {}
            previous.result = {"activity": previous.result.get("activity", [])}
        resource = db.get(PublicationResource, pid)
        if resource:
            resource.state, resource.inventory = "purged", {}
        project = db.get(Project, pid)
        project.state, project.current_revision_id, project.objective = "deleted", None, ""
        project.version += 1
        op.state, job.state = "succeeded", "done"
        record_progress(op, "deletion_complete")
        db.add(Audit(actor_id=actor, action="project:deleted", resource_id=pid))
