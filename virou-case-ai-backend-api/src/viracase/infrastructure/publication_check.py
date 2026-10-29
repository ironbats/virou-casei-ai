from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, aware, canonical_hash, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway, PublicationUncertain
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Audit, IntegrationConfig, Job, Operation
from viracase.infrastructure.publication_build import IsolatedBuild
from viracase.infrastructure.publication_check_resources import retained_resource, verification_resource_name
from viracase.infrastructure.publication_logging import emit, exception_fields
from viracase.infrastructure.templates import starter
from viracase.infrastructure.publication_verification import (
    VERIFICATION_FORMAT_VERSION, VerificationFailure, confirm_bindings, failure_record,
)

CHECKS = {
    "no_network",
    "no_host_secrets",
    "no_cross_project_files",
    "read_only_sources",
    "resource_limits",
    "no_nested_userns",
    "remote_read_write",
    "binding_isolation",
    "resource_retention",
}
LEGACY_CHECKS = CHECKS - {"resource_retention"} | {"cleanup"}


def runtime_verified(row, stack, executor_digest):
    evidence = row.validation.get("runtime_verification", {})
    try:
        report = evidence["report"]
        return bool(
            executor_digest
            and evidence["digest"] == canonical_hash(report)
            and report.get("verification_format_version") == VERIFICATION_FORMAT_VERSION
            and report["configuration_id"] == row.id
            and report["configuration_digest"] == canonical_hash(row.settings)
            and report["executor_digest"] == executor_digest
            and aware(datetime.fromisoformat(report["tested_at"])) <= utcnow()
            < aware(datetime.fromisoformat(report["expires_at"]))
            and set(report["checks"]) in (CHECKS, LEGACY_CHECKS)
            and all(v is True for v in report["checks"].values())
            and isinstance(report["stacks"], list)
            and bool(report["stacks"])
            and set(report["stacks"]) <= set(row.settings["allowed_stacks"])
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
            emit("publication_check_blocked", "warning", configuration_id=cfg.id, connection_id=identity,
                 error_code="EXECUTOR_UNAVAILABLE", workers=[
                     {"id": w["id"], "code": w.get("code"), "diagnostic_id": w.get("diagnostic_id")}
                     for w in executor.workers
                 ])
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
                "OPERATION_CONFLICT", "Já existe uma verificação em execução ou aguardando retomada."
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
        emit("publication_check_queued", operation_id=op.id, configuration_id=cfg.id, connection_id=identity)
        return {"id": op.id, "state": "queued"}

    return repo.mutate(
        actor, f"publication-check:{identity}", key, {"version": version, "reason": reason}, command
    )


def execute_check(worker, oid, token, gateway_factory=CloudflareGateway, builder=None):
    from viracase.infrastructure.publication_maintenance import update_automatic_verification

    update_automatic_verification(worker.repo, oid, token, starting=True)
    try:
        result = _execute_check(worker, oid, token, gateway_factory, builder)
    except Exception as exc:
        update_automatic_verification(worker.repo, oid, token, error=exc)
        raise
    update_automatic_verification(worker.repo, oid, token)
    return result


def _execute_check(worker, oid, token, gateway_factory=CloudflareGateway, builder=None):
    from viracase.infrastructure.publication_maintenance import TRANSIENT_FAILURES

    repo, builder = worker.repo, builder or IsolatedBuild()
    registry = Integrations(repo)
    current_stage, current_stack = "verification_initializing", None

    def stage(name, **values):
        nonlocal current_stage
        if current_stage != name:
            emit("publication_check_stage", operation_id=oid, stage=name)
        current_stage = name
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                raise RuleError("LEASE_LOST", "Verificação assumida por outra execução.")
            op.state, op.result = "running", {**op.result, **values}
            job.lease_until = utcnow() + timedelta(minutes=15)
            record_progress(op, name)

    def persist(values):
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return False
            op.result = {**op.result, **values}
            return True

    with repo.sessions() as db:
        op = db.get(Operation, oid)
        cfg = db.get(IntegrationConfig, op.policy["config_id"])
        policy, secret = dict(cfg.settings), registry.vault.decrypt(cfg.id, cfg.credential)
        config_id, actor, connection_id = cfg.id, op.account_id, op.payload["connection_id"]
        automatic = op.payload.get("automatic") is True
        evidence = dict(op.result)
        current = registry.latest(db, "cloudflare", connection_id=connection_id)
        changed = not current or current.id != cfg.id or current.state not in {"active", "validated"}
    gateway = gateway_factory(policy, secret)

    def renew(method):
        with repo.sessions.begin() as db:
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                raise RuleError("LEASE_LOST", "Verificação assumida por outra execução.")
            current = registry.latest(db, "cloudflare", connection_id=connection_id)
            if not current or current.id != config_id or current.state not in {"active", "validated"}:
                raise RuleError("VERSION_CONFLICT", "A conexão mudou durante a verificação.")
            if repo.config.restore_hold or not repo.config.lifecycle_enabled:
                raise RuleError("LIFECYCLE_LOCKED", "A verificação aguarda o término da manutenção.")
            job.lease_until = utcnow() + timedelta(minutes=15)

    gateway.before_request = renew
    failure, checks, tested, reports = None, {}, [], []
    validation, known_databases, workers_count = None, set(), 0
    resources = [dict(item) for item in evidence.get("verification_resources", [])]
    if evidence.get("creation_started") and evidence.get("resource_name") and not resources:
        resources.append({
            "stack": evidence.get("verification_stack"), "worker_name": evidence["resource_name"],
            "database_id": evidence.get("database_id"), "state": "retained",
            "creation_started": True, "legacy": True,
        })
    retention = {"state": "retained", "intentional": False, "worker_removed": False, "database_removed": False}

    def save_resources(resource):
        nonlocal resources
        resources = [item for item in resources if item["worker_name"] != resource["worker_name"]]
        resources.append(dict(resource))
        stage(current_stage, verification_resources=resources, creation_started=True)

    def record_failure(exc):
        nonlocal failure
        detail = failure_record(exc, current_stage, current_stack, (secret,))
        if failure is None or (
            automatic and isinstance(failure, RuleError) and failure.code in TRANSIENT_FAILURES
            and (not isinstance(exc, RuleError) or exc.code not in TRANSIENT_FAILURES)
        ):
            failure = exc
            persist({"verification_failure": detail})
        emit("publication_check_failed", "error", operation_id=oid, stage=current_stage,
             verification=detail, **exception_fields(exc))
        return detail

    try:
        stage(
            "verification_isolation", verification_failure=None, remote_verification=None,
            stack_results=[], verification_stack=None, resource_policy="retain",
            verification_resources=resources,
            verification_cleanup=retention if resources else {"state": "not_required", "intentional": False},
        )
        if changed:
            raise RuleError("VERSION_CONFLICT", "A conexão mudou. Os recursos de diagnóstico foram preservados.")
        if repo.config.restore_hold or not repo.config.lifecycle_enabled:
            raise RuleError("LIFECYCLE_LOCKED", "A verificação aguarda o término da manutenção.")
        checks.update(builder.verify_isolation())
        expected_local = CHECKS - {"remote_read_write", "binding_isolation", "resource_retention"}
        if set(checks) != expected_local or not all(value is True for value in checks.values()):
            raise RuleError("EXECUTOR_UNAVAILABLE", "O executor não confirmou todos os controles de isolamento.")
        fingerprint = builder.fingerprint()
        if not fingerprint:
            raise RuleError("EXECUTOR_UNAVAILABLE", "Toolchain incompleta.")
        stage("verification_configuration")
        validation = gateway.validate()
        if not validation.get("ready"):
            raise RuleError(validation.get("code", "CONFIGURATION_INVALID"),
                            "A conexão Cloudflare não passou na verificação.")
        known_databases = set(validation.get("database_ids", []))
        workers_count = validation.get("workers_count", 100)
        for stack in policy["allowed_stacks"]:
            current_stack = stack
            revision = str(uuid5(NAMESPACE_URL, f"viroucaseai:qualification:{oid}:{stack}"))
            name = verification_resource_name(policy, connection_id, stack)
            resource = None
            try:
                stage("verification_cloudflare", verification_stack=stack, verification_revision=revision,
                      remote_verification=None, resource_name=name)
                prior = retained_resource(repo, policy, connection_id, stack, name, evidence)
                existing = gateway.database(name)
                settings = gateway.settings(name)
                if (existing or settings) and not prior:
                    raise RuleError("RESOURCE_CONFLICT", "Recurso existente sem propriedade comprovada. Não será alterado.")
                if existing and (not prior.get("database_id") or existing["uuid"] != prior["database_id"]):
                    raise RuleError("RESOURCE_CONFLICT", "A identidade do banco de diagnóstico não foi confirmada. Não será alterado.")
                previous_revision = None
                if settings:
                    from viracase.infrastructure.publication_verification import binding_observation

                    revisions = {prior.get("revision_id"), prior.get("previous_revision_id")} - {None}
                    previous_revision = next((item for item in revisions if item in settings.get("tags", [])), None)
                    if (
                        not existing or not previous_revision or "viroucaseai" not in settings.get("tags", [])
                        or not binding_observation(settings, existing["uuid"], previous_revision)["passed"]
                    ):
                        raise RuleError("RESOURCE_CONFLICT", "A propriedade do Worker de diagnóstico não foi confirmada.")
                same_revision = bool(prior and prior.get("revision_id") == revision)
                if same_revision and (prior.get("upload_started") or previous_revision == revision) and prior.get("executor_digest") != fingerprint:
                    raise RuleError("VERSION_CONFLICT", "O executor mudou. Inicie uma nova verificação mantendo os recursos existentes.")
                already_uploaded = same_revision and previous_revision == revision
                if prior and prior.get("revision_id") == revision and prior.get("upload_started") and not already_uploaded:
                    stage("verification_bindings")
                    raise PublicationUncertain()
                if (not existing and len(known_databases) >= 10) or (not settings and workers_count >= 100):
                    raise RuleError("QUOTA_EXCEEDED", "Libere capacidade de Worker e D1 para o diagnóstico deste perfil.")
                resource = {
                    "stack": stack, "worker_name": name, "database_id": existing["uuid"] if existing else None,
                    "url": f"https://{name}.{validation['subdomain']}.workers.dev",
                    "revision_id": revision, "previous_revision_id": previous_revision,
                    "state": "retained", "creation_started": True, "upload_started": already_uploaded,
                    "executor_digest": fingerprint, "worker_confirmed": bool(settings),
                }
                save_resources(resource)
                database_id = (existing or gateway.create_database(name))["uuid"]
                resource["database_id"] = database_id
                known_databases.add(database_id)
                save_resources(resource)
                stage("verification_d1", database_id=database_id)
                gateway.query(database_id, "CREATE TABLE IF NOT EXISTS __viracase_probe (id TEXT PRIMARY KEY, value TEXT)")
                gateway.query(database_id, "INSERT OR REPLACE INTO __viracase_probe (id,value) VALUES (?,?)", [oid, "verified"])
                read = gateway.query(database_id, "SELECT value FROM __viracase_probe WHERE id=?", [oid])
                if not isinstance(read, list) or not any(
                    isinstance(row, dict) and row.get("value") == "verified"
                    for item in read if isinstance(item, dict) and isinstance(item.get("results"), list)
                    for row in item["results"]
                ):
                    raise VerificationFailure("PUBLICATION_D1_PROBE_FAILED", {"read_back_matches": False})
                if already_uploaded:
                    deployment_id = prior.get("deployment_id")
                    resource["upload_recovered"] = True
                else:
                    stage("verification_building")
                    artifact = builder.build(starter(stack), stack, revision, repo.sources.secrets)
                    stage("verification_uploading")
                    resource["upload_started"] = True
                    save_resources(resource)
                    try:
                        deployment_id = gateway.upload(name, database_id, artifact)
                    except PublicationUncertain:
                        if not settings:
                            workers_count += 1
                        raise
                    if not settings:
                        workers_count += 1
                resource["deployment_id"] = deployment_id
                resource["worker_confirmed"] = True
                save_resources(resource)
                stage("verification_bindings", deployment_id=deployment_id)
                bindings = confirm_bindings(gateway, name, database_id, revision, policy["timeout_seconds"])
                stage("verification_routing")
                gateway.enable(name)
                stage("verification_remote")
                gateway.verification_observer = lambda report: stage("verification_remote", remote_verification=report)
                remote_report = gateway.verify(resource["url"], revision)
                tested.append(stack)
                reports.append({"stack": stack, "revision_id": revision, "passed": True,
                                "bindings": bindings, "remote": remote_report})
            except Exception as exc:
                if isinstance(exc, RuleError) and exc.code == "LEASE_LOST":
                    raise
                detail = record_failure(exc)
                reports.append({"stack": stack, "revision_id": revision, "passed": False, "failure": detail})
            stage(current_stage, stack_results=reports)
    except Exception as exc:
        if isinstance(exc, RuleError) and exc.code == "LEASE_LOST":
            return
        record_failure(exc)
    if not persist({
        "verification_resources": resources,
        "verification_cleanup": retention if resources else {"state": "not_required", "intentional": False},
    }):
        return
    if resources:
        emit("publication_check_resources_retained", operation_id=oid, resource_count=len(resources))
    if validation and validation.get("ready"):
        with repo.sessions.begin() as db:
            repo.account(db, actor, True)
            configuration_lock(db, "integration:cloudflare")
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return
            current = registry.latest(db, "cloudflare", connection_id=connection_id)
            if current and current.id == config_id:
                current.validation = {
                    **current.validation, **validation, "checked_at": utcnow().isoformat(),
                    "database_ids": sorted(known_databases),
                    "databases_count": len(known_databases), "workers_count": workers_count,
                }
                current.validated_at = utcnow()
    if not tested:
        if failure:
            raise failure
        raise RuleError("CAPABILITY_UNVALIDATED", "Nenhum perfil concluiu a verificação operacional.")
    checks.update(remote_read_write=True, binding_isolation=True, resource_retention=True)
    with repo.sessions.begin() as db:
        op = db.get(Operation, oid)
        repo.account(db, actor, True)
        configuration_lock(db, "integration:cloudflare")
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        current = registry.latest(db, "cloudflare", connection_id=connection_id)
        if (
            job.lease_token != token or not current or current.id != config_id
            or current.state not in {"active", "validated"}
        ):
            raise RuleError("VERSION_CONFLICT", "A conexão mudou durante a verificação.")
        report = {
            "verification_format_version": VERIFICATION_FORMAT_VERSION,
            "configuration_id": config_id, "configuration_digest": canonical_hash(policy),
            "executor_digest": fingerprint, "stacks": tested, "checks": checks,
            "tested_at": utcnow().isoformat(),
            "expires_at": (utcnow() + timedelta(hours=policy["validation_hours"])).isoformat(),
            "operation_id": oid, "resource_policy": "retain",
        }
        from viracase.infrastructure.publication_qualification import qualified

        preserve = bool(
            automatic and isinstance(failure, RuleError) and failure.code in TRANSIENT_FAILURES
            and all(qualified(current, stack, fingerprint) for stack in policy["allowed_stacks"])
        )
        if not preserve:
            current.validation = {
                **current.validation, "publication_check_blocked": False,
                "publication_check_blocked_stacks": [stack for stack in policy["allowed_stacks"] if stack not in tested],
                "database_ids": sorted(known_databases), "databases_count": len(known_databases),
                "workers_count": workers_count,
                "runtime_verification": {"report": report, "digest": canonical_hash(report)},
            }
        op.state, job.state = ("failed" if failure else "succeeded"), "done"
        op.error_code = failure.code if isinstance(failure, RuleError) else "PUBLICATION_VERIFICATION_FAILED" if failure else None
        record_progress(op, "verification_partial" if failure else "verification_complete")
        db.add(Audit(
            actor_id=actor, action="publication:partially_verified" if failure else "publication:verified",
            resource_id=config_id, reason=op.payload["reason"],
        ))


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
