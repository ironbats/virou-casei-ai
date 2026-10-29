from datetime import timedelta

from sqlalchemy import func, or_, select

from viracase.application.progress import STAGES, record_progress
from viracase.domain.rules import RuleError, aware, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway, PublicationUncertain
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Audit, Job, Operation, PublicationResource, new_id
from viracase.infrastructure.publication_logging import emit, exception_fields, safe_text
from viracase.infrastructure.publication_migrations import preflight_migrations, verify_remote_migration_history
from viracase.infrastructure.publication_outcome import publication_diagnostic, recovery_guidance

MAX_ATTEMPTS = 6
LEASE_DURATION = timedelta(minutes=5)
PRE_UPLOAD_STAGES = {"publication_building", "publication_provisioning", "publication_migrating"}
DEPLOYMENT_STAGES = {
    "publication_uploading", "publication_routing", "publication_verifying", "publication_verified",
}
DETERMINISTIC_FAILURES = {
    "MIGRATION_CHECKSUM_CONFLICT", "MIGRATION_REVIEW_REQUIRED", "MIGRATION_HISTORY_IMMUTABLE",
    "MIGRATION_BASELINE_INCONSISTENT", "MIGRATION_VALIDATION_FAILED", "MIGRATION_ORDER_INVALID",
    "MIGRATION_SQL_REJECTED", "MIGRATION_BASELINE_UNVERIFIABLE", "ARTIFACT_IDENTITY_MISMATCH",
}
ATTENTION_FAILURES = {
    "MIGRATION_RESULT_UNKNOWN", "MIGRATION_RECEIPT_PENDING", "MIGRATION_RECEIPT_CONFLICT",
    "RESOURCE_CONFLICT", "FORBIDDEN", "CONFIGURATION_INVALID",
    *DETERMINISTIC_FAILURES,
    "PUBLICATION_ACCESS_DENIED", "PUBLICATION_REDIRECTED", "PUBLICATION_BINDINGS_MISMATCH",
}


def record_recovery_progress(operation, stage):
    result = operation.result if isinstance(operation.result, dict) else {}
    activity = result.get("activity", [])
    valid = isinstance(activity, list) and all(
        isinstance(item, dict) and isinstance(item.get("stage"), str) and item["stage"] in STAGES
        and ("at" not in item or isinstance(item["at"], str))
        for item in activity
    )
    discarded = max(0, len(activity) + (not activity or activity[-1]["stage"] != stage) - 24) if valid else 0
    if not valid or any(item["stage"] in DEPLOYMENT_STAGES for item in activity[:discarded]):
        operation.progress_stage = stage
        return
    record_progress(operation, stage)


def recovery_status(state, previous=None, **changes):
    previous = previous if isinstance(previous, dict) else {}
    messages = {
        "scheduled": "A conferência automática do resultado da publicação foi agendada.",
        "running": "Conferindo automaticamente os recursos e a revisão publicados na Cloudflare.",
        "retry_wait": "A conferência automática será repetida no horário indicado, sem reenviar a publicação.",
        "attention": "A recuperação automática foi interrompida por um impedimento identificado.",
        "resolved": "A conferência automática foi concluída.",
    }
    result = {
        "version": 2, "state": state, "message": messages[state], "attempts": 0,
        "last_attempt_at": None, "last_checked_at": None, "next_attempt_at": None,
        "last_error_code": None, "last_error_message": None,
        "reason": None, "required_action": None, "owner": None, "missing_evidence": [],
    }
    allowed = set(result) | {"resolution", "migration", "legacy_reassessment_pending", "requested_reassessment_pending"}
    result.update({key: value for key, value in previous.items() if key in allowed})
    result.update({key: value for key, value in changes.items() if key in allowed})
    result.update(version=2, state=state, message=messages[state])
    if state == "attention":
        result["message"] = result.get("last_error_message") or messages[state]
    if state == "resolved":
        result["message"] = {
            "pre_upload_failure": "A publicação foi interrompida antes do envio do Worker. Corrija a falha indicada para tentar novamente.",
            "published": "A revisão e o funcionamento da publicação foram confirmados automaticamente.",
            "resume_safe": "Os recursos foram conferidos. A publicação continuará a partir do estado confirmado.",
            "migration_recovered": "A pendência da migration foi resolvida. Confira a revisão atual e solicite uma nova publicação.",
        }.get(result.get("resolution"), messages[state])
        result.update(required_action=None, owner=None, missing_evidence=[])
    return result


def upload_may_have_started(result, inventory, revision):
    attempt = result.get("publication_attempt", {})
    if attempt.get("version") == 1 and attempt.get("revision_id") == revision:
        return bool(attempt.get("upload_started") or attempt.get("deployment_id"))
    failure = result.get("publication_failure") or {}
    return bool(
        inventory.get("candidate_revision_id") == revision
        or failure.get("stage") in DEPLOYMENT_STAGES
        or any(item.get("stage") in DEPLOYMENT_STAGES for item in result.get("activity", []))
    )


def pre_upload_migration_failure(result, error_code):
    failure = result.get("publication_failure") or {}
    return bool(
        error_code in DETERMINISTIC_FAILURES and failure.get("code") == error_code
        and failure.get("stage") in PRE_UPLOAD_STAGES
    )


def deterministic_pre_upload_failure(result, error_code, inventory, revision, operation_id=None):
    last = inventory.get("migration_last_attempt") or {}
    rejected_without_effect = bool(
        isinstance(last, dict) and last.get("protocol_version") == 2
        and last.get("phase") == "sql_rejected" and last.get("effect_outcome") in ("none", "not_sent")
        and last.get("revision_id") == revision
        and (operation_id is None or last.get("operation_id") == operation_id)
        and (result.get("publication_failure") or {}).get("stage") in PRE_UPLOAD_STAGES
    )
    return bool(
        (pre_upload_migration_failure(result, error_code) or rejected_without_effect)
        and not inventory.get("migration_in_progress")
        and not upload_may_have_started(result, inventory, revision)
    )


def pre_upload_observed(result, inventory, revision):
    attempt = result.get("publication_attempt") or {}
    stage = attempt.get("stage") if attempt.get("version") == 1 and attempt.get("revision_id") == revision else None
    stage = stage or (result.get("publication_failure") or {}).get("stage")
    return bool(stage in PRE_UPLOAD_STAGES and not upload_may_have_started(result, inventory, revision))


def assess_recovery_attempt(repo, oid, token, gateway, database_id, files, inventory):
    from viracase.infrastructure.publication_recovery_evidence import assess_migration_recovery

    with repo.sessions() as db:
        op = db.get(Operation, oid)
        revision = op.payload["revision_id"]
    assessment = assess_migration_recovery(gateway, database_id, files, inventory, operation_id=oid, revision_id=revision)
    with repo.sessions.begin() as db:
        op = db.get(Operation, oid)
        repo.account(db, op.account_id, True)
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        resource = db.get(PublicationResource, op.project_id)
        if op.state != "reconciling" or job.state != "reconciling" or job.lease_token != token:
            raise RuleError("LEASE_LOST", "A conferência foi assumida por outra execução.")
        if resource.inventory != inventory:
            raise RuleError("RESOURCE_CONFLICT", "O inventário mudou durante a conferência da migration.")
        op.result = {**op.result, "publication_recovery_assessment": {**assessment, "checked_at": utcnow().isoformat()}}
        if assessment.get("pending_update"):
            inventory = {**inventory, "migration_in_progress": assessment["pending_update"]}
            resource.inventory = inventory
    return inventory


def recover_publications(repo, gateway_factory=CloudflareGateway, executor=None, operation_id=None):
    if repo.config.restore_hold or not repo.config.lifecycle_enabled:
        return None
    now = utcnow()
    query = (
        select(Operation.id, Operation.account_id)
        .join(Job, Job.operation_id == Operation.id)
        .where(
            Operation.type == "publication", Operation.state == "reconciling",
            Job.state == "reconciling", Job.available_at <= now,
            or_(Job.lease_until.is_(None), Job.lease_until <= now),
            or_(
                func.coalesce(Operation.result["publication_recovery"]["state"].as_string(), "") != "attention",
                func.coalesce(Operation.result["publication_recovery"]["version"].as_integer(), 0) < 2,
            ),
        )
        .order_by(Job.available_at, Job.id)
        .limit(20)
    )
    if operation_id:
        query = query.where(Operation.id == operation_id)
    with repo.sessions() as db:
        candidates = db.execute(query).all()
    for oid, owner in candidates:
        with repo.sessions.begin() as db:
            repo.account(db, owner, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update(skip_locked=True))
            if not job:
                continue
            op = db.get(Operation, oid)
            previous = op.result.get("publication_recovery", {})
            if (
                op.state != "reconciling" or job.state != "reconciling"
                or previous.get("state") == "attention" and previous.get("version") == 2
                or aware(job.available_at) > utcnow()
                or job.lease_until and aware(job.lease_until) > utcnow()
            ):
                continue
            token = new_id()
            job.lease_token, job.lease_until = token, utcnow() + LEASE_DURATION
            op.result = {
                **op.result,
                "publication_recovery": recovery_status(
                    "running", previous=previous,
                    attempts=previous.get("attempts", 0) + 1,
                    last_attempt_at=utcnow().isoformat(), next_attempt_at=None,
                    legacy_reassessment_pending=bool(
                        previous.get("legacy_reassessment_pending")
                        or previous.get("state") == "attention" and previous.get("version") != 2
                    ),
                ),
            }
            developer_request = op.result.get("publication_developer_recovery") or {}
            if not isinstance(developer_request, dict) or developer_request.get("state") not in {"queued", "running", "resolving"}:
                record_recovery_progress(op, "publication_reconciling")
        with repo.sessions() as db:
            request = (db.get(Operation, oid).result or {}).get("publication_developer_recovery") or {}
        if request.get("state") in {"queued", "running", "resolving"}:
            from viracase.infrastructure.publication_developer_recovery import run_developer_recovery

            run_developer_recovery(repo, oid, token, gateway_factory)
        else:
            _recover_publication(repo, oid, token, gateway_factory, executor)
        return oid
    return None


def _recover_publication(repo, oid, token, gateway_factory, executor):
    from viracase.infrastructure.publication_reconcile import observe_publication

    registry, observed = Integrations(repo), None

    def guard(method="GET"):
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            account = repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token or job.state != "reconciling" or op.state != "reconciling":
                raise RuleError("LEASE_LOST", "A conferência foi assumida por outra execução.")
            if repo.config.restore_hold or not repo.config.lifecycle_enabled or account.lifecycle != "active":
                raise RuleError("LIFECYCLE_LOCKED", "A publicação aguarda o término da manutenção.")
            repo.access(db, account, "publish")
            resource = db.get(PublicationResource, op.project_id)
            current = registry.for_resource(db, resource)
            project = repo.owned_project(db, account.id, resource.project_id)
            if (
                not current or current.id != config_id or current.state != "active"
                or not current.credential or not current.validation.get("ready")
                or current.settings["account_id"] != resource.remote_account_id
                or current.validation.get("publication_check_blocked")
                or project.stack in current.validation.get("publication_check_blocked_stacks", [])
            ):
                raise RuleError("CONFIGURATION_INVALID", "A conexão precisa ser confirmada antes da conferência.")
            job.lease_until = utcnow() + LEASE_DURATION

    try:
        with repo.sessions() as db:
            op = db.get(Operation, oid)
            resource = db.get(PublicationResource, op.project_id)
            if not resource:
                raise RuleError("RESOURCE_CONFLICT", "O inventário da publicação não está disponível.")
            revision, result, inventory = op.payload["revision_id"], dict(op.result), dict(resource.inventory)
            owner, project_id = op.account_id, op.project_id
            error_code = op.error_code
            if deterministic_pre_upload_failure(result, op.error_code, inventory, revision, oid):
                _complete_recovery(repo, oid, token, "pre_upload_failure", executor=executor)
                return
            if (
                result["publication_recovery"]["attempts"] > MAX_ATTEMPTS
                and not result["publication_recovery"].get("legacy_reassessment_pending")
                and not result["publication_recovery"].get("requested_reassessment_pending")
            ):
                raise RuleError("PUBLICATION_RECOVERY_EXHAUSTED", "O limite de conferências automáticas foi atingido.")
            config = registry.for_resource(db, resource)
            if not config:
                raise RuleError("CONFIGURATION_INVALID", "A conexão da publicação não está disponível.")
            config_id, name, database_id = config.id, resource.worker_name, resource.database_id
            policy, secret = dict(config.settings), registry.vault.decrypt(config.id, config.credential)
        guard()
        gateway = gateway_factory(policy, secret)
        gateway.before_request = guard
        files = repo.source_files(owner, project_id, revision)
        requested = bool(result["publication_recovery"].get("requested_reassessment_pending"))
        pending = inventory.get("migration_in_progress") or {}
        reconstruct = bool(
            pending.get("protocol_version") == 2 and pending.get("phase") in {"sql_confirmed", "receipt_sending"}
            and pending.get("effect_outcome") == "confirmed" and not pending.get("schema_after")
        )
        if requested or reconstruct:
            remote = gateway.database(name)
            if not isinstance(remote, dict) or remote.get("uuid") != database_id or not database_id:
                raise RuleError("RESOURCE_CONFLICT", "O banco observado não corresponde ao inventário autorizado.")
            inventory = assess_recovery_attempt(repo, oid, token, gateway, database_id, files, inventory)
        if error_code == "MIGRATION_RESULT_UNKNOWN" and not inventory.get("migration_in_progress"):
            verify_remote_migration_history(gateway, database_id, files, inventory, require_complete=True)
        observed = observe_publication(gateway, name, revision, inventory, database_id, files)
        observed["configuration_id"] = config_id
        from viracase.infrastructure.publication_resolution import assisted_receipt_observed

        assisted = assisted_receipt_observed(result, oid, revision, database_id, inventory, observed["inventory"])
        observed["assisted_attempt"] = assisted
        if deterministic_pre_upload_failure(result, error_code, observed["inventory"], revision, oid):
            _complete_recovery(repo, oid, token, "pre_upload_failure", observed, executor=executor)
            return
        if assisted or requested and pre_upload_observed(result, inventory, revision):
            if not inventory.get("migration_in_progress"):
                verify_remote_migration_history(gateway, database_id, files, observed["inventory"], require_complete=True)
            _complete_recovery(repo, oid, token, "migration_recovered", observed, executor=executor)
            return
        if pre_upload_migration_failure(result, error_code):
            raise RuleError(error_code, "O histórico de migrations precisa ser conferido antes de confirmar a publicação.")
        if observed["matches_revision"]:
            guard()
            preflight_migrations(repo.source_files(owner, project_id, revision), observed["inventory"])
            guard("POST")
            gateway.enable(name)
            guard()
            verification = gateway.verify(observed["url"], revision)
            if isinstance(verification, dict) and verification.get("passed") is not True:
                raise RuleError("PUBLICATION_VERIFICATION_FAILED", "O funcionamento da publicação não foi confirmado.")
            _complete_recovery(repo, oid, token, "published", observed, verification, executor)
        elif upload_may_have_started(result, inventory, revision) or (
            inventory.get("database_creation_started") and not observed["database_id"]
        ):
            raise PublicationUncertain()
        else:
            _complete_recovery(repo, oid, token, "resume_safe", observed, executor=executor)
    except Exception as exc:
        if isinstance(exc, RuleError) and exc.code == "LEASE_LOST":
            return
        _retry_recovery(
            repo, oid, token, exc, observed,
            secrets=(*repo.sources.secrets, secret if "secret" in locals() else ""),
        )


def _complete_recovery(repo, oid, token, resolution, observed=None, verification=None, executor=None):
    from viracase.infrastructure.publication import PublicationService

    registry, now = Integrations(repo), utcnow()
    with repo.sessions.begin() as db:
        op = db.get(Operation, oid)
        account = repo.account(db, op.account_id, True)
        configuration_lock(db, "integration:cloudflare")
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        if job.lease_token != token or op.state != "reconciling" or job.state != "reconciling":
            return
        resource = db.get(PublicationResource, op.project_id)
        if resolution != "pre_upload_failure":
            if repo.config.restore_hold or not repo.config.lifecycle_enabled or account.lifecycle != "active":
                raise RuleError("LIFECYCLE_LOCKED", "A publicação aguarda o término da manutenção.")
            repo.access(db, account, "publish")
            current = registry.for_resource(db, resource)
            project = repo.owned_project(db, account.id, resource.project_id)
            if (
                not current or current.id != observed.get("configuration_id") or current.state != "active"
                or not current.validation.get("ready")
                or current.settings["account_id"] != resource.remote_account_id
                or current.validation.get("publication_check_blocked")
                or project.stack in current.validation.get("publication_check_blocked_stacks", [])
            ):
                raise RuleError("CONFIGURATION_INVALID", "A conexão mudou durante a conferência da publicação.")
        if observed:
            resource.inventory = observed["inventory"]
            if observed["database_id"]:
                resource.database_id = observed["database_id"]
        assisted = (observed or {}).get("assisted_attempt") or {}
        if resolution == "migration_recovered" and assisted.get("state") in {"applying", "superseded"}:
            from viracase.infrastructure.publication_resolution import apply_resolution_result

            apply_resolution_result(op, job, resource, assisted, now=now)
            db.add(Audit(actor_id=assisted["actor_id"], action="publication:recovery:resolved", resource_id=oid, reason="Recibo da resolução iniciada confirmado na retomada, sem repetir SQL ou publicar revisão antiga."))
            emit("publication_recovery_resolved", operation_id=oid, state=op.state, resolution=resolution)
            return
        if resolution == "published":
            revision = op.payload["revision_id"]
            resource.state = "published"
            resource.inventory = {
                **resource.inventory, "url": observed["url"], "published_revision_id": revision,
                "published_at": now.isoformat(),
            }
            op.state, job.state, op.error_code = "succeeded", "done", None
            op.result = {
                **op.result, "url": observed["url"], "revision_id": revision,
                "verified_at": now.isoformat(), "remote_verification": verification,
            }
            stage = "publication_verified"
        elif resolution == "resume_safe":
            current = registry.for_resource(db, resource)
            if PublicationService(repo, executor).configuration_reasons(current, project.stack):
                raise RuleError("CONFIGURATION_INVALID", "A retomada aguarda a verificação automática da conexão.")
            op.policy = {**op.policy, "execution_config_id": current.id, "execution_version": current.version}
            op.payload = {**op.payload, "entitlement_version": account.entitlement_version}
            op.state, job.state, op.error_code = "queued", "queued", None
            job.available_at = now
            op.result = {
                **op.result,
                "publication_attempt": {
                    "version": 1, "revision_id": op.payload["revision_id"], "upload_started": False,
                },
            }
            stage = "queued"
        else:
            op.state, job.state, stage = "failed", "done", "publication_failed"
        op.result = {
            **op.result, "external_effect_pending": False,
            "publication_recovery": recovery_status(
                "resolved", previous=op.result.get("publication_recovery"), resolution=resolution,
                last_checked_at=now.isoformat(), next_attempt_at=None, last_error_code=op.error_code,
                legacy_reassessment_pending=False,
                requested_reassessment_pending=False,
            ),
        }
        op.result = {
            **op.result,
            "publication_diagnostic": publication_diagnostic(
                op.result, resource.inventory, stage=stage, operation_id=oid, revision_id=op.payload["revision_id"],
            ),
        }
        if resolution == "published":
            op.result = {
                **op.result,
                "publication_diagnostic": {
                    **op.result["publication_diagnostic"], "effect_status": "confirmed",
                },
            }
        job.lease_token, job.lease_until = None, None
        record_recovery_progress(op, stage)
        emit("publication_recovery_resolved", operation_id=oid, state=op.state, resolution=resolution)


def _retry_recovery(repo, oid, token, exc, observed=None, secrets=(), manual=False):
    code = exc.code if isinstance(exc, RuleError) else "PUBLICATION_RECOVERY_FAILED"
    with repo.sessions.begin() as db:
        op = db.get(Operation, oid)
        repo.account(db, op.account_id, True)
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        if job.lease_token != token or op.state != "reconciling" or job.state != "reconciling":
            return
        resource = db.get(PublicationResource, op.project_id)
        if observed:
            resource.inventory = observed["inventory"]
            if observed["database_id"]:
                resource.database_id = observed["database_id"]
        previous = op.result.get("publication_recovery", {})
        attempts, now = previous.get("attempts", 1), utcnow()
        attention = manual or code in ATTENTION_FAILURES or attempts >= MAX_ATTEMPTS
        next_attempt = None if attention else now + timedelta(seconds=min(300, 30 * 2 ** (attempts - 1)))
        pending = (resource.inventory or {}).get("migration_in_progress") if resource else None
        message = safe_text(exc.message, secrets=secrets, limit=500) if isinstance(exc, RuleError) else (
            "Não foi possível concluir a conferência. Consulte a operação pelo identificador informado."
        )
        guidance = recovery_guidance(code, pending, exhausted=attempts >= MAX_ATTEMPTS)
        assessment = op.result.get("publication_recovery_assessment") or {}
        if assessment.get("classification") in {"schema_applied", "schema_unchanged", "unknown", "conflict"}:
            guidance = {
                **guidance, "required_action": assessment.get("required_action"), "owner": "operator",
                "missing_evidence": assessment.get("missing_evidence_messages", []),
            }
        op.result = {
            **op.result,
            "publication_recovery": recovery_status(
                "attention" if attention else "retry_wait", previous=previous,
                last_checked_at=now.isoformat(), last_error_code=code,
                last_error_message=message,
                next_attempt_at=next_attempt.isoformat() if next_attempt else None,
                legacy_reassessment_pending=False,
                requested_reassessment_pending=False,
                **guidance,
            ),
        }
        op.result = {
            **op.result,
            "publication_diagnostic": publication_diagnostic(
                op.result, resource.inventory if resource else None,
                operation_id=oid, revision_id=op.payload["revision_id"],
            ),
        }
        if next_attempt:
            job.available_at = next_attempt
        job.lease_token, job.lease_until = None, None
        record_recovery_progress(op, "publication_reconciling")
        emit(
            "publication_recovery_attention" if attention else "publication_recovery_retry", "warning",
            operation_id=oid, attempts=attempts,
            next_attempt_at=next_attempt.isoformat() if next_attempt else None,
            **exception_fields(exc),
        )
