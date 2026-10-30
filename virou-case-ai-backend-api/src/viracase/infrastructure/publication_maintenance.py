from datetime import datetime, timedelta

from sqlalchemy import select

from viracase.application.progress import record_progress
from viracase.application.verification_progress import finish_verification_attempt, queue_verification_attempt
from viracase.domain.rules import RuleError, aware, utcnow
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Audit, Job, Operation
from viracase.infrastructure.observability import WorkerBuildStatus
from viracase.infrastructure.publication_logging import emit
from viracase.infrastructure.publication_qualification import qualified

ACTIVE_CHECK_STATES = {"queued", "running", "reconciling"}
TRANSIENT_FAILURES = {"DEPENDENCY_UNAVAILABLE", "QUOTA_EXCEEDED", "PUBLICATION_RESULT_UNKNOWN"}
AUTOMATIC_REASON = "Verificação automática para manter a publicação disponível."


def instant(value):
    try:
        return aware(datetime.fromisoformat(value)) if isinstance(value, str) else None
    except (TypeError, ValueError):
        return None


def eligible(row):
    return bool(row and row.state in {"active", "validated"} and row.credential and row.validated_at)


def renewal_due_at(row, fingerprint):
    from viracase.infrastructure.publication_check import runtime_verified

    now = utcnow()
    if not row.validation.get("ready") or not all(
        qualified(row, stack, fingerprint) for stack in row.settings["allowed_stacks"]
    ):
        return now
    lifetime = timedelta(hours=row.settings["validation_hours"])
    deadlines = [aware(row.validated_at) + lifetime - min(timedelta(hours=1), lifetime / 5)]
    key = "runtime_verification" if runtime_verified(row, None, fingerprint) else "qualification"
    report = row.validation.get(key, {}).get("report", {})
    tested, expires = instant(report.get("tested_at")), instant(report.get("expires_at"))
    if not tested or not expires:
        return now
    deadlines.append(expires - min(timedelta(hours=1), (expires - tested) / 5))
    return min(deadlines)


def next_attempt(row, fingerprint):
    maintenance = row.validation.get("automatic_verification", {})
    retry = instant(maintenance.get("next_attempt_at"))
    if (
        maintenance.get("consecutive_failures", 0)
        and maintenance.get("executor_digest") == fingerprint
        and retry
    ):
        return retry
    return renewal_due_at(row, fingerprint)


def automatic_status(repo, row, executor=None, *, public=False):
    executor = executor or WorkerBuildStatus(repo)
    maintenance = row.validation.get("automatic_verification", {}) if row else {}
    enabled = eligible(row)
    state = maintenance.get("state", "scheduled")
    scheduled = instant(maintenance.get("next_attempt_at"))
    if not enabled:
        state = "disabled" if row and row.state == "disabled" else "waiting_configuration"
        scheduled = None
    elif repo.config.restore_hold or not repo.config.lifecycle_enabled:
        state = "waiting_lifecycle"
    elif not executor.available() or not executor.fingerprint():
        state = "waiting_executor"
    elif state not in {"queued", "running", "retry_wait", "attention"}:
        state = "scheduled"
        scheduled = next_attempt(row, executor.fingerprint())
    messages = {
        "disabled": "A verificação automática está pausada porque a conexão foi desativada.",
        "waiting_configuration": "Valide e ative a conexão para iniciar a verificação automática.",
        "waiting_executor": "A verificação automática aguarda o executor de publicação ficar disponível.",
        "waiting_lifecycle": "A verificação automática aguarda o término da manutenção da plataforma.",
        "scheduled": "A conexão é verificada automaticamente antes do vencimento da validação.",
        "queued": "Verificação automática na fila. O estado da publicação será atualizado ao concluir.",
        "running": "Verificação automática em andamento. O estado da publicação será atualizado ao concluir.",
        "retry_wait": "A verificação automática será repetida após o intervalo de recuperação.",
        "attention": "A verificação interrompida precisa de revisão no backoffice.",
    }
    result = {
        "enabled": enabled,
        "state": state,
        "message": messages[state],
        "next_attempt_at": scheduled.isoformat() if scheduled else None,
        "last_attempt_at": maintenance.get("last_attempt_at"),
        "last_succeeded_at": maintenance.get("last_succeeded_at"),
        "last_error_code": maintenance.get("last_error_code"),
        "consecutive_failures": maintenance.get("consecutive_failures", 0),
    }
    if not public:
        result["operation_id"] = maintenance.get("operation_id")
    return result


def schedule_automatic_verification(repo, executor=None):
    if repo.config.restore_hold or not repo.config.lifecycle_enabled:
        return None
    executor = executor or WorkerBuildStatus(repo)
    if not executor.available() or not executor.fingerprint():
        return None
    fingerprint, registry = executor.fingerprint(), Integrations(repo)
    active_query = (
        select(Operation)
        .where(Operation.type == "publication_check", Operation.state.in_(ACTIVE_CHECK_STATES))
        .order_by(Operation.created_at, Operation.id)
        .limit(1)
    )
    with repo.sessions() as db:
        active = db.scalar(active_query)
        if active:
            if not active.payload.get("automatic") or active.state not in {"queued", "reconciling"}:
                return None
            actor, identity, config_id, selected_operation = (
                active.account_id,
                active.payload["connection_id"],
                active.policy["config_id"],
                active.id,
            )
        else:
            candidates = [
                (next_attempt(row, fingerprint), identity, row)
                for identity, row in registry.current_rows(db, "cloudflare").items()
                if eligible(row)
            ]
            if not candidates:
                return None
            due, identity, candidate = min(candidates, key=lambda item: (item[0], item[1]))
            if due > utcnow():
                return None
            actor, config_id, selected_operation = candidate.created_by, candidate.id, None
    with repo.sessions.begin() as db:
        repo.account(db, actor, True)
        configuration_lock(db, "integration:cloudflare")
        now = utcnow()
        active = db.scalar(active_query)
        if active:
            if active.id != selected_operation or active.account_id != actor:
                return None
            row = registry.latest(db, "cloudflare", connection_id=identity)
            if active.payload.get("automatic") and active.state in {"queued", "reconciling"}:
                job = db.scalar(select(Job).where(Job.operation_id == active.id).with_for_update())
                if not eligible(row) or row.id != active.policy.get("config_id"):
                    active.state, job.state, job.lease_token = "cancelled", "done", None
                    record_progress(active, "cancelled")
                elif active.state == "reconciling":
                    maintenance = row.validation.get("automatic_verification", {})
                    failures = maintenance.get("consecutive_failures", 0) + 1
                    retry_at = now + timedelta(seconds=min(3600, 60 * 2 ** min(failures - 1, 6)))
                    queue_verification_attempt(active)
                    active.state, active.error_code = "queued", None
                    job.state, job.lease_token, job.available_at = "queued", None, retry_at
                    record_progress(active, "verification_queued")
                    row.validation = {
                        **row.validation,
                        "automatic_verification": {
                            **maintenance,
                            "state": "queued",
                            "operation_id": active.id,
                            "consecutive_failures": failures,
                            "next_attempt_at": retry_at.isoformat(),
                            "last_error_code": "VERIFICATION_RESUME_REQUIRED",
                            "executor_digest": fingerprint,
                        },
                    }
                    emit(
                        "publication_automatic_check_resumed",
                        operation_id=active.id,
                        configuration_id=row.id,
                        next_attempt_at=retry_at.isoformat(),
                    )
            return None
        if selected_operation or repo.config.restore_hold or not repo.config.lifecycle_enabled:
            return None
        row = registry.latest(db, "cloudflare", connection_id=identity)
        if (
            not eligible(row)
            or row.id != config_id
            or row.created_by != actor
            or next_attempt(row, fingerprint) > utcnow()
        ):
            return None
        operation = Operation(
            account_id=actor,
            type="publication_check",
            progress_stage="verification_queued",
            policy={"config_id": row.id},
            payload={"reason": AUTOMATIC_REASON, "connection_id": identity, "automatic": True},
        )
        db.add(operation)
        db.flush()
        record_progress(operation, "verification_queued")
        db.add(Job(operation_id=operation.id))
        previous = row.validation.get("automatic_verification", {})
        row.validation = {
            **row.validation,
            "automatic_verification": {
                **previous,
                "state": "queued",
                "operation_id": operation.id,
                "next_attempt_at": None,
                "executor_digest": fingerprint,
            },
        }
        db.add(
            Audit(
                actor_id=actor,
                action="publication:auto_verify_requested",
                resource_id=row.id,
                reason=AUTOMATIC_REASON,
            )
        )
        emit(
            "publication_automatic_check_queued",
            operation_id=operation.id,
            configuration_id=row.id,
            connection_id=identity,
        )
        return operation.id


def update_automatic_verification(repo, oid, token, *, starting=False, error=None):
    registry = Integrations(repo)
    with repo.sessions.begin() as db:
        operation = db.get(Operation, oid)
        if not operation or (
            not operation.payload.get("automatic") and (starting or error or operation.state != "succeeded")
        ):
            return
        repo.account(db, operation.account_id, True)
        configuration_lock(db, "integration:cloudflare")
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        row = registry.latest(db, "cloudflare", connection_id=operation.payload["connection_id"])
        if job.lease_token != token or not eligible(row) or row.id != operation.policy.get("config_id"):
            return
        maintenance = row.validation.get("automatic_verification", {})
        now = utcnow()
        if starting:
            row.validation = {
                **row.validation,
                "automatic_verification": {
                    **maintenance,
                    "state": "running",
                    "operation_id": oid,
                    "last_attempt_at": now.isoformat(),
                    "next_attempt_at": None,
                },
            }
            return
        if not error and operation.state not in {"succeeded", "failed"}:
            return
        code = (
            error.code
            if isinstance(error, RuleError)
            else "PROCESSING_FAILED"
            if error
            else operation.error_code
        )
        if code == "LEASE_LOST":
            return
        if code:
            failures = maintenance.get("consecutive_failures", 0) + 1
            retry_at = now + timedelta(seconds=min(3600, 60 * 2 ** min(failures - 1, 6)))
            changes = {}
            if code == "CONFIGURATION_INVALID":
                changes.update(publication_check_blocked=True, ready=False)
            elif code not in TRANSIENT_FAILURES and operation.state != "failed":
                changes["publication_check_blocked"] = True
            row.validation = {
                **row.validation,
                **changes,
                "automatic_verification": {
                    **maintenance,
                    "state": "retry_wait",
                    "operation_id": oid,
                    "consecutive_failures": failures,
                    "next_attempt_at": retry_at.isoformat(),
                    "last_error_code": code,
                },
            }
            if error:
                finish_verification_attempt(operation)
                operation.state, operation.error_code, job.state = "failed", code, "done"
                record_progress(operation, "failed")
            emit(
                "publication_automatic_check_retry",
                "warning",
                operation_id=oid,
                configuration_id=row.id,
                error_code=code,
                next_attempt_at=retry_at.isoformat(),
            )
        else:
            row.validation = {
                **row.validation,
                "automatic_verification": {
                    **maintenance,
                    "state": "scheduled",
                    "operation_id": oid,
                    "last_succeeded_at": now.isoformat(),
                    "last_attempt_at": maintenance.get("last_attempt_at") or now.isoformat(),
                    "consecutive_failures": 0,
                    "last_error_code": None,
                    "next_attempt_at": None,
                },
            }
