from datetime import datetime, timedelta

from sqlalchemy import select

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, aware, canonical_hash, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Audit, IntegrationConfig, Job, Operation
from viracase.infrastructure.publication_build import IsolatedBuild
from viracase.infrastructure.templates import starter

CHECKS = {
    "no_network",
    "no_host_secrets",
    "no_cross_project_files",
    "read_only_sources",
    "resource_limits",
    "no_nested_userns",
    "remote_read_write",
    "binding_isolation",
    "cleanup",
}


def runtime_verified(row, stack, executor_digest):
    evidence = row.validation.get("runtime_verification", {})
    try:
        report = evidence["report"]
        return bool(
            executor_digest
            and evidence["digest"] == canonical_hash(report)
            and report["configuration_id"] == row.id
            and report["configuration_digest"] == canonical_hash(row.settings)
            and report["executor_digest"] == executor_digest
            and utcnow() < aware(datetime.fromisoformat(report["expires_at"]))
            and set(report["checks"]) == CHECKS
            and all(v is True for v in report["checks"].values())
            and (not stack or stack in report["stacks"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def request_check(repo, actor, identity, key, version, reason):
    registry = Integrations(repo)

    def command(db, operator):
        registry.require_admin(operator)
        configuration_lock(db, "integration:cloudflare")
        cfg = registry.latest(db, "cloudflare", connection_id=identity)
        if not cfg or cfg.version != version:
            raise RuleError("VERSION_CONFLICT", "Atualize a conexão antes de verificar.", 412)
        if (
            cfg.state not in {"active", "validated"}
            or not registry.fresh(cfg)
            or not cfg.validation.get("ready")
        ):
            raise RuleError("CONFIGURATION_INVALID", "Valide a conexão antes de testar a publicação.")
        from viracase.infrastructure.observability import WorkerBuildStatus

        executor = WorkerBuildStatus(repo)
        if not executor.available() or not executor.fingerprint():
            raise RuleError(
                "EXECUTOR_UNAVAILABLE", "Worker ausente ou sem isolamento. Confira o diagnóstico do executor."
            )
        active = db.scalar(
            select(Operation)
            .where(
                Operation.type == "publication_check",
                Operation.state.in_(["queued", "running", "reconciling"]),
            )
            .limit(1)
        )
        if active:
            raise RuleError(
                "OPERATION_CONFLICT", "Já existe uma verificação em execução ou aguardando limpeza."
            )
        if repo.config.restore_hold or not repo.config.lifecycle_enabled:
            raise RuleError(
                "LIFECYCLE_LOCKED", "Ative o lifecycle e conclua a recuperação antes de publicar."
            )
        cfg.validation = {**cfg.validation, "publication_check_blocked": True}
        op = Operation(
            account_id=actor,
            type="publication_check",
            progress_stage="verification_queued",
            policy={"config_id": cfg.id},
            payload={"reason": reason, "connection_id": identity},
        )
        db.add(op)
        db.flush()
        record_progress(op, "verification_queued")
        db.add(Job(operation_id=op.id))
        db.add(
            Audit(actor_id=actor, action="publication:verify_requested", resource_id=cfg.id, reason=reason)
        )
        return {"id": op.id, "state": "queued"}

    return repo.mutate(
        actor, f"publication-check:{identity}", key, {"version": version, "reason": reason}, command
    )


def execute_check(worker, oid, token, gateway_factory=CloudflareGateway, builder=None):
    repo, builder = worker.repo, builder or IsolatedBuild()
    registry = Integrations(repo)

    def stage(name, **values):
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                raise RuleError("LEASE_LOST", "Verificação assumida por outra execução.")
            op.state, op.result = "running", {**op.result, **values}
            job.lease_until = utcnow() + timedelta(minutes=15)
            record_progress(op, name)

    with repo.sessions() as db:
        op = db.get(Operation, oid)
        cfg = db.get(IntegrationConfig, op.policy["config_id"])
        policy, secret = dict(cfg.settings), registry.vault.decrypt(cfg.id, cfg.credential)
        config_id, actor = cfg.id, op.account_id
        evidence = dict(op.result)
        current = registry.latest(db, "cloudflare", connection_id=op.payload["connection_id"])
        changed = not current or current.id != cfg.id
        if (
            changed
            and evidence.get("creation_started")
            and current
            and current.settings["account_id"] == policy["account_id"]
        ):
            secret = registry.vault.decrypt(current.id, current.credential)
    gateway = gateway_factory(policy, secret)

    def renew(method):
        with repo.sessions.begin() as db:
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                raise RuleError("LEASE_LOST", "Verificação assumida por outra execução.")
            job.lease_until = utcnow() + timedelta(minutes=15)

    gateway.before_request = renew
    name = policy["worker_prefix"] + "-check-" + oid.replace("-", "")[:24]
    database_id = evidence.get("database_id")
    failure, checks, tested = None, {}, []
    try:
        stage("verification_isolation")
        if changed:
            raise RuleError(
                "VERSION_CONFLICT", "A conexão mudou. A limpeza será concluída antes de uma nova verificação."
            )
        checks.update(builder.verify_isolation())
        fingerprint = builder.fingerprint()
        if not fingerprint:
            raise RuleError("EXECUTOR_UNAVAILABLE", "Toolchain incompleta.")
        validation = gateway.validate()
        if not validation.get("ready"):
            raise RuleError("CONFIGURATION_INVALID", "A conexão Cloudflare não passou na verificação.")
        if len(validation.get("database_ids", [])) >= 10 or validation.get("workers_count", 100) >= 100:
            raise RuleError(
                "QUOTA_EXCEEDED", "Libere um Worker e um banco D1 temporários para a verificação."
            )
        stage("verification_cloudflare", resource_name=name)
        existing = gateway.database(name)
        if existing and not evidence.get("creation_started"):
            raise RuleError("RESOURCE_CONFLICT", "Nome temporário já ocupado. Não será alterado.")
        if gateway.settings(name) and not evidence.get("creation_started"):
            raise RuleError("RESOURCE_CONFLICT", "Worker temporário já ocupado. Não será alterado.")
        stage("verification_cloudflare", creation_started=True)
        database_id = (existing or gateway.create_database(name))["uuid"]
        stage("verification_cloudflare", database_id=database_id)
        gateway.query(
            database_id, "CREATE TABLE IF NOT EXISTS __viracase_probe (id TEXT PRIMARY KEY, value TEXT)"
        )
        gateway.query(
            database_id, "INSERT OR REPLACE INTO __viracase_probe (id,value) VALUES (?,?)", [oid, "verified"]
        )
        read = gateway.query(database_id, "SELECT value FROM __viracase_probe WHERE id=?", [oid])
        if not any(r.get("value") == "verified" for item in read for r in item.get("results", [])):
            raise RuleError("PUBLICATION_VERIFICATION_FAILED", "D1 não confirmou escrita e leitura.")
        for stack in policy["allowed_stacks"]:
            artifact = builder.build(starter(stack), stack, oid, repo.sources.secrets)
            gateway.upload(name, database_id, artifact)
            settings = gateway.settings(name)
            bindings = (settings or {}).get("bindings", [])
            if (
                not settings
                or {b.get("type") for b in bindings} != {"d1", "assets"}
                or any(
                    b.get("type") == "d1" and (b.get("name") != "DB" or b.get("id") != database_id)
                    for b in bindings
                )
            ):
                raise RuleError(
                    "PUBLICATION_VERIFICATION_FAILED", "Bindings do Worker não foram confirmados."
                )
            gateway.enable(name)
            gateway.verify(f"https://{name}.{validation['subdomain']}.workers.dev", oid)
            tested.append(stack)
        checks.update(remote_read_write=True, binding_isolation=True)
    except Exception as exc:
        failure = exc
    finally:
        with repo.sessions() as db:
            owned = db.get(Operation, oid).result.get("creation_started")
        if owned:
            try:
                stage("verification_cleanup")
                if not database_id:
                    found = gateway.database(name)
                    database_id = found["uuid"] if found else None
                if gateway.settings(name):
                    gateway.enable(name, False)
                if database_id:
                    gateway.delete(name, database_id)
                elif gateway.settings(name):
                    gateway.request("DELETE", gateway.account_path + "/workers/scripts/" + name)
                if gateway.settings(name) or (
                    database_id
                    and gateway.request("GET", gateway.account_path + "/d1/database/" + database_id)
                ):
                    raise RuleError("PURGE_INCOMPLETE", "Recursos de teste ainda presentes.")
                checks["cleanup"] = True
            except Exception:
                with repo.sessions.begin() as db:
                    op = db.get(Operation, oid)
                    repo.account(db, op.account_id, True)
                    job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
                    if job.lease_token != token:
                        return
                    op.state, job.state, op.error_code = (
                        "reconciling",
                        "reconciling",
                        "VERIFICATION_CLEANUP_PENDING",
                    )
                    record_progress(op, "verification_cleanup")
                return
    if failure:
        raise failure
    with repo.sessions.begin() as db:
        op = db.get(Operation, oid)
        repo.account(db, actor, True)
        configuration_lock(db, "integration:cloudflare")
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        current = registry.latest(db, "cloudflare", connection_id=op.payload["connection_id"])
        if (
            job.lease_token != token
            or not current
            or current.id != config_id
            or current.state not in {"active", "validated"}
        ):
            raise RuleError("VERSION_CONFLICT", "A conexão mudou durante a verificação.")
        report = {
            "configuration_id": config_id,
            "configuration_digest": canonical_hash(policy),
            "executor_digest": fingerprint,
            "stacks": tested,
            "checks": checks,
            "tested_at": utcnow().isoformat(),
            "expires_at": (utcnow() + timedelta(hours=policy["validation_hours"])).isoformat(),
            "operation_id": oid,
        }
        current.validation = {
            **current.validation,
            "publication_check_blocked": False,
            "runtime_verification": {"report": report, "digest": canonical_hash(report)},
        }
        op.state, job.state = "succeeded", "done"
        record_progress(op, "verification_complete")
        db.add(
            Audit(
                actor_id=actor,
                action="publication:verified",
                resource_id=config_id,
                reason=op.payload["reason"],
            )
        )


def resume_check(repo, actor, oid, key, reason):
    def command(db, operator):
        Integrations(repo).require_admin(operator)
        op = db.get(Operation, oid)
        if not op or op.type != "publication_check" or op.state != "reconciling":
            raise RuleError("OPERATION_CONFLICT", "Não há verificação pendente para retomar.")
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        op.state, op.error_code, job.state, job.available_at = "queued", None, "queued", utcnow()
        record_progress(op, "verification_queued")
        db.add(Audit(actor_id=actor, action="publication:verify_resumed", resource_id=oid, reason=reason))
        return {"id": oid, "state": "queued"}

    return repo.mutate(actor, f"resume-check:{oid}", key, {"reason": reason}, command)
