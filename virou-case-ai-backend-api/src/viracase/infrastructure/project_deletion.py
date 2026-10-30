from datetime import timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, aware, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import (
    Attempt,
    Audit,
    Draft,
    Idempotency,
    Job,
    Operation,
    Project,
    PublicationResource,
    RecommendationRun,
    Revision,
)


ACTIVE_STATES = ("queued", "running", "reconciling")


def quiesce_operations(repo, db, pid):
    blockers = []
    operations = db.scalars(
        select(Operation)
        .where(
            Operation.project_id == pid,
            Operation.type != "project_deletion",
            Operation.state.in_(ACTIVE_STATES),
        )
        .order_by(Operation.created_at, Operation.id)
        .with_for_update()
    ).all()
    for operation in operations:
        operation.cancel_requested = True
        job = db.scalar(select(Job).where(Job.operation_id == operation.id).with_for_update())
        attempt = db.scalar(select(Attempt.id).where(Attempt.operation_id == operation.id))
        safe_queued = operation.state == "queued" and not attempt and (not job or job.state == "queued")
        resource = db.get(PublicationResource, pid) if operation.type == "publication" else None
        abandoned_publication = (
            operation.type == "publication"
            and operation.state == "reconciling"
            and resource is not None
            and not attempt
            and job is not None
            and job.state == "reconciling"
            and (job.lease_until is None or aware(job.lease_until) <= utcnow())
        )
        if safe_queued or abandoned_publication:
            repo.settle_operation(db, operation, Decimal(0))
            operation.state = "cancelled"
            operation.progress_stage = "cancelled_before_deletion"
            record_progress(operation, operation.progress_stage)
            if job:
                job.state = "done"
                job.lease_token = None
                job.lease_until = None
        else:
            blockers.append({"id": operation.id, "type": operation.type, "state": operation.state})
    return blockers


def request_deletion(repo, actor, pid, key, version, confirmation):
    def command(db, account):
        project = db.scalar(
            select(Project).where(Project.id == pid, Project.account_id == actor).with_for_update()
        )
        if not project or project.state == "deleted":
            raise RuleError("NOT_FOUND", "Projeto indisponível.", 404)
        if project.version != version:
            raise RuleError("VERSION_CONFLICT", "O projeto mudou. Atualize antes de excluir.", 412)
        if confirmation != project.name:
            raise RuleError("VALIDATION_ERROR", "Digite o nome exato do projeto para confirmar.", 400)
        if project.state == "deleting":
            current = db.scalar(
                select(Operation)
                .where(
                    Operation.project_id == pid,
                    Operation.type == "project_deletion",
                    Operation.state.in_(ACTIVE_STATES),
                )
                .order_by(Operation.created_at.desc())
                .limit(1)
            )
            if current:
                return {"id": current.id, "project_id": pid, "state": current.state}
        project.state, project.version = "deleting", project.version + 1
        blockers = quiesce_operations(repo, db, pid)
        op = Operation(
            account_id=actor,
            project_id=pid,
            type="project_deletion",
            progress_stage="deletion_queued",
            result={"awaiting_operations": blockers} if blockers else {},
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
        blockers = quiesce_operations(repo, db, op.project_id)
        if blockers:
            op.result = {**op.result, "awaiting_operations": blockers}
            op.state = "queued"
            record_progress(op, "deletion_waiting_operations")
            job.state = "queued"
            job.available_at = utcnow() + timedelta(seconds=15)
            job.lease_token = None
            job.lease_until = None
            return
        op.result = {key: value for key, value in op.result.items() if key != "awaiting_operations"}
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
        db.execute(delete(Draft).where(Draft.account_id == actor, Draft.target == pid))
        db.execute(
            delete(Draft).where(
                Draft.account_id == actor,
                Draft.kind == "migration_plan",
                Draft.target.in_(
                    select(Revision.id).where(Revision.project_id == pid, Revision.account_id == actor)
                ),
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
        for intent in db.scalars(select(Idempotency).where(Idempotency.actor_id == actor)):
            cached = intent.result if isinstance(intent.result, dict) else {}
            cached_project = cached.get("project") if isinstance(cached.get("project"), dict) else {}
            if (
                intent.command.endswith(":" + pid)
                or cached.get("id") == pid
                or cached.get("project_id") == pid
                or cached_project.get("id") == pid
            ):
                intent.result = {"expired": True}
    repo.sources.remove_project(actor, pid)
    with repo.sessions.begin() as db:
        op = db.get(Operation, oid)
        repo.account(db, actor, True)
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        if job.lease_token != token:
            return
        project = db.get(Project, pid)
        project.state, project.current_revision_id, project.objective, project.name = "deleted", None, "", ""
        project.version += 1
        op.state, job.state = "succeeded", "done"
        record_progress(op, "deletion_complete")
        db.add(Audit(actor_id=actor, action="project:deleted", resource_id=pid))
