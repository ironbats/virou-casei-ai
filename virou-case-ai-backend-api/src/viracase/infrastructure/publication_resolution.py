import re
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import select

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, aware, canonical_hash, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Audit, Job, Operation, PublicationResource, new_id
from viracase.infrastructure.publication_logging import safe_text
from viracase.infrastructure.publication_migrations import LEDGER, _remote_history, confirm_migration_receipt, migration_schema_fingerprint
from viracase.infrastructure.publication_outcome import migration_identity, publication_diagnostic
from viracase.infrastructure.publication_recovery import (
    LEASE_DURATION,
    PRE_UPLOAD_STAGES,
    record_recovery_progress,
    recovery_status,
    upload_may_have_started,
)
from viracase.infrastructure.repository import operation_dto

CHECK_COOLDOWN = timedelta(seconds=60)
PLAN_VALIDITY = timedelta(minutes=15)
CLASSIFICATIONS = {"unassessed", "confirmed", "no_effect", "schema_applied", "schema_unchanged", "unknown", "conflict", "resolved"}
SUMMARIES = {
    "unassessed": "A pendência ainda não possui uma avaliação assistida.",
    "confirmed": "Há evidência verificável para confirmar o recibo desta migration.",
    "no_effect": "A evidência registrada comprova ausência de efeitos pendentes desta migration.",
    "schema_applied": "O resultado estrutural completo corresponde ao plano preservado; a adoção exige revisão administrativa.",
    "schema_unchanged": "O schema corresponde ao estado anterior ao plano estrutural; o encerramento exige revisão administrativa.",
    "unknown": "Ainda não há evidência suficiente para resolver esta migration.",
    "conflict": "O histórico ou o resultado remoto diverge da migration preservada.",
    "resolved": "A pendência foi resolvida. Confira a disponibilidade de publicação da revisão atual.",
}


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        return aware(datetime.fromisoformat(value))
    except (ValueError, TypeError):
        return None


def _text(value, limit=500):
    return safe_text(value, limit=limit) if isinstance(value, str) else ""


def _object(value):
    return value if isinstance(value, dict) else {}


def _operation(repo, db, actor, oid, *, admin=False):
    account = repo.account(db, actor)
    if admin:
        Integrations(repo).require_admin(account)
    else:
        repo.access(db, account, "read")
    op = db.get(Operation, oid)
    if not op or op.type != "publication" or not admin and op.account_id != actor:
        raise RuleError("NOT_FOUND", "Publicação indisponível.", 404)
    repo.owned_project(db, op.account_id, op.project_id)
    return op


def _current_error(recovery):
    code = recovery.get("last_error_code")
    if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
        return {"code": code, "message": _text(recovery.get("last_error_message"))}
    return None


def _snapshot(repo, db, op, *, actor=None):
    result = _object(op.result)
    recovery = _object(result.get("publication_recovery"))
    assessment = _object(result.get("publication_recovery_assessment"))
    request = _object(result.get("publication_recovery_request"))
    resource = db.get(PublicationResource, op.project_id)
    inventory = _object(resource.inventory) if resource and resource.account_id == op.account_id else {}
    pending = _object(inventory.get("migration_in_progress"))
    if (
        op.state != "reconciling" or pending.get("operation_id") not in (None, op.id)
        or pending.get("revision_id") not in (None, op.payload.get("revision_id"))
    ):
        pending = {}
        inventory = {**inventory, "migration_in_progress": None}
    assessed_identity = _object(assessment.get("identity"))
    identity = migration_identity(pending) or (
        migration_identity(assessed_identity.get("migration"))
        if _object(assessment.get("checks")).get("identity") == "matched"
        and assessed_identity.get("operation_id") == op.id and assessed_identity.get("revision_id") == op.payload.get("revision_id") else None
    )
    migration = dict(identity) if identity else None
    if migration:
        for field, allowed in (
            ("phase", {"prepared", "sql_sending", "sql_confirmed", "receipt_sending", "receipt_confirmed", "sql_rejected"}),
            ("effect_outcome", {"not_sent", "none", "unknown", "confirmed"}),
        ):
            if isinstance(pending.get(field), str) and pending[field] in allowed:
                migration[field] = pending[field]
        if type(pending.get("statement_count")) is int and 0 < pending["statement_count"] <= 2000:
            migration["statement_count"] = pending["statement_count"]
    now = utcnow()
    requested = _time(request.get("requested_at"))
    cooldown = requested + CHECK_COOLDOWN if requested else None
    job = db.scalar(select(Job).where(Job.operation_id == op.id))
    code, reason = None, None
    if op.state != "reconciling" or not job or job.state != "reconciling":
        code, reason = "RECOVERY_NOT_PENDING", "Esta operação não está aguardando recuperação."
    elif job.lease_until and aware(job.lease_until) > now:
        code, reason = "OPERATION_CONFLICT", "Uma conferência desta operação já está em andamento."
    elif recovery.get("state") in {"scheduled", "running", "retry_wait"}:
        code, reason = "RECOVERY_SCHEDULED", "A conferência desta operação já está agendada."
    elif cooldown and cooldown > now:
        code, reason = "RECOVERY_COOLDOWN", "Aguarde o intervalo indicado antes de solicitar outra conferência."
    elif repo.config.restore_hold or not repo.config.lifecycle_enabled:
        code, reason = "LIFECYCLE_LOCKED", "A conferência aguarda o término da manutenção."
    else:
        try:
            repo.access(db, repo.account(db, op.account_id), "publish")
        except RuleError as exc:
            code, reason = exc.code, _text(exc.message)
    classification = assessment.get("classification", "unassessed")
    if not isinstance(classification, str) or classification not in CLASSIFICATIONS:
        classification = "unknown"
    if recovery.get("state") == "resolved" and op.state in {"succeeded", "failed", "cancelled"}:
        classification = "resolved"
    missing = assessment.get("missing_evidence_messages") or recovery.get("missing_evidence") or []
    missing = [_text(item, 250) for item in missing[:20] if isinstance(item, str)] if isinstance(missing, list) else []
    checks = {
        key: _text(value, 180) for key, value in _object(assessment.get("checks")).items()
        if key in {"identity", "source", "history", "receipt", "execution", "schema"} and isinstance(value, str)
    }
    diagnostic = publication_diagnostic(result, inventory, operation_id=op.id, revision_id=op.payload.get("revision_id"))
    diagnostic.pop("migration", None)
    diagnostic.pop("detail_available", None)
    diagnostic.pop("version", None)
    current_error = _current_error(recovery)
    if current_error:
        diagnostic["current_error"] = current_error
    required = _text(assessment.get("required_action") or recovery.get("required_action"))
    if classification == "resolved":
        missing, required = [], "Confira a revisão atual e a disponibilidade da publicação antes de publicar novamente."
    elif not required:
        required = "Solicite uma conferência. Se faltar prova de execução, encaminhe o diagnóstico ao responsável pela operação."
    snapshot = {
        "format_version": 1, "operation_id": op.id, "project_id": op.project_id,
        "revision_id": op.payload.get("revision_id"), "operation_state": op.state,
        "recovery_state": recovery.get("state") if recovery.get("state") in {"scheduled", "running", "retry_wait", "attention", "resolved"} else None,
        "can_check": code is None, "blocked_code": code, "blocked_reason": reason,
        "requested_at": requested.isoformat() if requested else None,
        "last_checked_at": (_time(recovery.get("last_checked_at")) or _time(assessment.get("checked_at"))).isoformat()
        if _time(recovery.get("last_checked_at")) or _time(assessment.get("checked_at")) else None,
        "cooldown_until": cooldown.isoformat() if cooldown else None,
        "operator_required": classification != "resolved" and bool(assessment.get("requires_operator") or recovery.get("owner") == "operator"),
        "classification": classification, "summary": SUMMARIES[classification], "required_action": required,
        "missing_evidence": missing, "migration": migration, "checks": checks, "diagnostic": diagnostic,
    }
    from viracase.infrastructure.publication_developer_recovery import developer_recovery_projection

    return {**snapshot, **developer_recovery_projection(repo, db, op, actor, snapshot)}


def recovery_snapshot(repo, actor, oid, admin=False):
    with repo.sessions() as db:
        return _snapshot(repo, db, _operation(repo, db, actor, oid, admin=admin), actor=None if admin else actor)


def request_recovery_check(repo, actor, oid, key):
    def command(db, account):
        op = _operation(repo, db, actor, oid)
        repo.access(db, account, "publish")
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        snapshot = _snapshot(repo, db, op, actor=actor)
        if not snapshot["can_check"]:
            raise RuleError(snapshot["blocked_code"], snapshot["blocked_reason"], 429 if snapshot["blocked_code"] == "RECOVERY_COOLDOWN" else 409)
        now = utcnow()
        op.result = {
            **op.result,
            "publication_recovery_request": {"requested_at": now.isoformat(), "requested_by": actor},
            "publication_recovery": recovery_status(
                "scheduled", previous=op.result.get("publication_recovery"),
                next_attempt_at=now.isoformat(), requested_reassessment_pending=True,
            ),
        }
        job.available_at = now
        record_recovery_progress(op, "publication_reconciling")
        db.add(Audit(actor_id=actor, action="publication:recovery:requested", resource_id=oid, reason="Conferência segura solicitada pelo proprietário, sem nova publicação ou análise IA."))
        return _snapshot(repo, db, op, actor=actor)

    return repo.mutate(actor, "publication:recovery:request:" + oid, key, {}, command)


def _configuration(repo, db, op, resource, registry, *, access_action="publish"):
    if not resource or resource.account_id != op.account_id:
        raise RuleError("RESOURCE_CONFLICT", "O inventário autorizado da publicação não está disponível.")
    account = repo.account(db, op.account_id)
    repo.access(db, account, access_action)
    if repo.config.restore_hold or not repo.config.lifecycle_enabled or account.lifecycle != "active":
        raise RuleError("LIFECYCLE_LOCKED", "A recuperação aguarda o término da manutenção.")
    project = repo.owned_project(db, op.account_id, op.project_id)
    current = registry.for_resource(db, resource)
    if (
        not current or current.state != "active" or not current.credential
        or not _object(current.validation).get("ready")
        or current.settings.get("account_id") != resource.remote_account_id
        or current.validation.get("publication_check_blocked")
        or project.stack in current.validation.get("publication_check_blocked_stacks", [])
    ):
        raise RuleError("CONFIGURATION_INVALID", "A conexão autorizada precisa estar válida para conferir a recuperação.")
    return current


def _binding(op, resource, cfg):
    inventory = _object(resource.inventory)
    return {
        "operation_id": op.id, "account_id": op.account_id, "project_id": op.project_id,
        "revision_id": op.payload.get("revision_id"), "source_hash": op.payload.get("source_hash"),
        "database_id": resource.database_id, "remote_account_id": resource.remote_account_id,
        "worker_name": resource.worker_name, "configuration_id": cfg.id, "configuration_version": cfg.version,
        "configuration_sha256": canonical_hash([cfg.settings, cfg.credential]),
        "pending_sha256": canonical_hash(inventory.get("migration_in_progress")),
        "history_sha256": canonical_hash(inventory.get("migrations", {})),
        "upload_started": upload_may_have_started(op.result, inventory, op.payload.get("revision_id")),
    }


@contextmanager
def _recovery_scope(repo, actor, oid, gateway_factory):
    registry = Integrations(repo)
    with repo.sessions.begin() as db:
        op = _operation(repo, db, actor, oid, admin=True)
        repo.account(db, op.account_id, True)
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        if op.state != "reconciling" or not job or job.state != "reconciling":
            raise RuleError("RECOVERY_NOT_PENDING", "A publicação não está aguardando recuperação.")
        request = _object(op.result.get("publication_developer_recovery"))
        if request.get("state") in {"queued", "running", "resolving"}:
            raise RuleError("OPERATION_CONFLICT", "Uma recuperação autorizada pelo proprietário já está em andamento.")
        if job.lease_until and aware(job.lease_until) > utcnow():
            raise RuleError("OPERATION_CONFLICT", "Uma execução já está conferindo esta publicação.")
        resource = db.get(PublicationResource, op.project_id)
        cfg = _configuration(repo, db, op, resource, registry)
        binding = _binding(op, resource, cfg)
        token = new_id()
        job.lease_token, job.lease_until = token, utcnow() + LEASE_DURATION
        context = {
            "token": token, "binding": binding, "inventory": dict(resource.inventory),
            "result": dict(op.result), "policy": dict(cfg.settings),
            "secret": registry.vault.decrypt(cfg.id, cfg.credential), "owner": op.account_id,
            "project_id": op.project_id, "revision_id": op.payload["revision_id"],
            "database_id": resource.database_id, "worker_name": resource.worker_name,
        }

    def guard(method="GET"):
        with repo.sessions.begin() as db:
            op = _operation(repo, db, actor, oid, admin=True)
            repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if op.state != "reconciling" or job.state != "reconciling" or job.lease_token != token:
                raise RuleError("LEASE_LOST", "Outra execução assumiu a recuperação desta publicação.")
            resource = db.get(PublicationResource, op.project_id)
            current = _configuration(repo, db, op, resource, registry)
            if _binding(op, resource, current) != binding:
                raise RuleError("RECOVERY_PLAN_STALE", "A operação, o inventário ou a conexão mudou. Prepare uma nova conferência.", 412)
            job.lease_until = utcnow() + LEASE_DURATION

    try:
        context["files"] = repo.source_files(context["owner"], context["project_id"], context["revision_id"])
        context["files_sha256"] = canonical_hash(context["files"])
        if binding["source_hash"] and binding["source_hash"] != context["files_sha256"]:
            raise RuleError("ARTIFACT_IDENTITY_MISMATCH", "As fontes não correspondem à revisão autorizada desta publicação.")
        gateway = gateway_factory(context["policy"], context["secret"])
        gateway.before_request = guard
        context.update(gateway=gateway, guard=guard)
        guard()
        remote = gateway.database(context["worker_name"])
        if not isinstance(remote, dict) or remote.get("uuid") != context["database_id"] or not context["database_id"]:
            raise RuleError("RESOURCE_CONFLICT", "O banco observado não corresponde ao inventário autorizado.")
        yield context
    finally:
        with repo.sessions.begin() as db:
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job and job.lease_token == token:
                job.lease_token, job.lease_until = None, None


def _assess(context, oid):
    from viracase.infrastructure.publication_recovery_evidence import assess_migration_recovery

    return assess_migration_recovery(
        context["gateway"], context["database_id"], context["files"], context["inventory"],
        operation_id=oid, revision_id=context["revision_id"],
    )


def _plan_action(assessment):
    return {
        "confirmed": "record_receipt", "no_effect": "close_no_effect",
        "schema_applied": "adopt_schema", "schema_unchanged": "close_no_effect",
    }.get(assessment.get("classification"))


def _pre_upload_proven(context):
    result, revision = context["result"], context["revision_id"]
    attempt = _object(result.get("publication_attempt"))
    stage = attempt.get("stage") if attempt.get("version") == 1 and attempt.get("revision_id") == revision else None
    stage = stage or _object(result.get("publication_failure")).get("stage")
    return isinstance(stage, str) and stage in PRE_UPLOAD_STAGES and not context["binding"]["upload_started"]


def _can_supersede(attempt, context, assessment):
    binding = _object(attempt.get("binding"))
    fixed = {
        "operation_id", "account_id", "project_id", "revision_id", "source_hash", "database_id",
        "remote_account_id", "worker_name", "pending_sha256", "history_sha256", "upload_started",
    }
    return bool(
        binding and all(binding.get(key) == context["binding"].get(key) for key in fixed)
        and attempt.get("identity") == assessment.get("identity")
        and (
            not attempt.get("receipt_write_started")
            or assessment.get("classification") in {"confirmed", "schema_applied"}
            and attempt.get("action") in {"record_receipt", "adopt_schema"}
        )
    )


def prepare_recovery_plan(repo, actor, oid, key, reason, gateway_factory=CloudflareGateway):
    with repo.sessions() as db:
        _operation(repo, db, actor, oid, admin=True)
    command, payload = "publication:recovery:plan:" + oid, {"reason": reason}
    previous = repo.recover_intention(actor, command, key, payload)
    if previous is not None:
        return previous
    with _recovery_scope(repo, actor, oid, gateway_factory) as context:
        assessment = _assess(context, oid)
        action = _plan_action(assessment) if _pre_upload_proven(context) else None
        now = utcnow()
        plan = {
            "format_version": 1, "plan_id": new_id(), "operation_id": oid,
            "revision_id": context["revision_id"], "prepared_by": actor,
            "created_at": now.isoformat(), "expires_at": (now + PLAN_VALIDITY).isoformat(),
            "binding": context["binding"], "files_sha256": context["files_sha256"],
            "assessment": assessment, "action": action, "can_resolve": action is not None,
            "reason": _text(reason, 300),
            "resulting_behavior": "Encerrar esta tentativa preservando a causa original e permitir nova publicação quando o readiness autorizar.",
        }
        if not _pre_upload_proven(context):
            plan["blocked_reason"] = "O envio do Worker não foi descartado. Use a reconciliação da publicação para conferir também a revisão remota."
        plan["plan_sha256"] = canonical_hash(plan)

        def store_plan(db, operator):
            Integrations(repo).require_admin(operator)
            op = db.get(Operation, oid)
            repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != context["token"] or op.state != "reconciling" or job.state != "reconciling":
                raise RuleError("LEASE_LOST", "A recuperação foi assumida por outra execução.")
            resource = db.get(PublicationResource, op.project_id)
            cfg = _configuration(repo, db, op, resource, Integrations(repo))
            if _binding(op, resource, cfg) != context["binding"]:
                raise RuleError("RECOVERY_PLAN_STALE", "A evidência mudou durante a preparação.", 412)
            previous_resolution = _object(op.result.get("publication_resolution"))
            prior_attempt = _object(previous_resolution.get("attempt"))
            previous_attempts = list(previous_resolution.get("previous_attempts") or [])
            if prior_attempt.get("state") == "applying":
                if not _can_supersede(prior_attempt, context, assessment):
                    raise RuleError("RECOVERY_IN_PROGRESS", "A intenção existente ainda exige conferência do mesmo destino e resultado. Retome-a antes de preparar outra.")
                previous_attempts.append({
                    **prior_attempt, "state": "superseded", "superseded_at": now.isoformat(),
                    "superseded_by_plan_id": plan["plan_id"], "superseded_by_actor": actor,
                })
                db.add(Audit(actor_id=actor, action="publication:recovery:superseded", resource_id=oid, reason=_text(reason, 300)))
            elif prior_attempt:
                previous_attempts.append(prior_attempt)
            op.result = {
                **op.result, "publication_recovery_assessment": {**assessment, "checked_at": now.isoformat()},
                "publication_resolution": {"format_version": 1, "plan": plan, "previous_attempts": previous_attempts},
            }
            db.add(Audit(actor_id=actor, action="publication:recovery:plan", resource_id=oid, reason=_text(reason, 300)))
            return plan

        context["guard"]()
        return repo.mutate(actor, command, key, payload, store_plan)


def _validate_attestation(plan, payload):
    assessment = plan["assessment"]
    classification = assessment.get("classification")
    attestation = payload.get("attestation")
    if classification not in {"schema_applied", "schema_unchanged"}:
        if attestation is not None:
            raise RuleError("RECOVERY_ATTESTATION_INVALID", "Este plano usa evidência técnica e não admite substituição por uma atestação.", 400)
        return None
    count = assessment.get("statement_count")
    if (
        not isinstance(attestation, dict) or type(count) is not int or count < 1
        or attestation.get("observed_outcome") != classification
        or attestation.get("statement_indices") != list(range(1, count + 1))
        or any(type(index) is not int for index in attestation.get("statement_indices", []))
        or not isinstance(attestation.get("reference"), str) or not 3 <= len(attestation["reference"].strip()) <= 300
        or not isinstance(attestation.get("evidence_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", attestation["evidence_sha256"])
    ):
        raise RuleError("RECOVERY_ATTESTATION_REQUIRED", "Ateste o resultado estrutural completo e todas as instruções deste plano, com referência e hash da evidência revisada.", 400)
    return {**attestation, "reference": _text(attestation["reference"], 300)}


def _validate_plan(context, actor, oid, payload, intention_id, payload_hash):
    resolution = _object(context["result"].get("publication_resolution"))
    plan, attempt = _object(resolution.get("plan")), _object(resolution.get("attempt"))
    if (
        not plan or plan.get("plan_id") != payload.get("plan_id")
        or plan.get("plan_sha256") != payload.get("plan_sha256")
        or plan.get("plan_sha256") != canonical_hash({key: value for key, value in plan.items() if key != "plan_sha256"})
        or plan.get("operation_id") != oid or plan.get("revision_id") != context["revision_id"]
    ):
        raise RuleError("RECOVERY_PLAN_STALE", "O plano não corresponde à conferência preservada. Prepare uma nova conferência.", 412)
    if plan.get("prepared_by") != actor:
        raise RuleError("FORBIDDEN", "A resolução deve ser feita pelo administrador que preparou e revisou este plano.", 403)
    resumed = attempt.get("state") == "applying"
    if resumed and (attempt.get("intention_id") != intention_id or attempt.get("payload_hash") != payload_hash):
        raise RuleError("RECOVERY_IN_PROGRESS", "Retome a mesma chave e o mesmo conteúdo da resolução já iniciada.")
    expires = _time(plan.get("expires_at"))
    if not expires or expires <= utcnow() and not resumed:
        raise RuleError("RECOVERY_PLAN_EXPIRED", "A observação deste plano expirou. Prepare uma nova conferência.", 410)
    expected_binding = attempt.get("binding") if resumed else plan.get("binding")
    if (
        expected_binding != context["binding"] or plan.get("files_sha256") != context["files_sha256"]
        or not _pre_upload_proven(context)
    ):
        raise RuleError("RECOVERY_PLAN_STALE", "A revisão, o inventário ou a conexão mudou desde a conferência.", 412)
    if not plan.get("can_resolve") or plan.get("action") != payload.get("action") or plan.get("action") != _plan_action(plan.get("assessment", {})):
        raise RuleError("RECOVERY_ACTION_INVALID", "Esta ação não foi autorizada pela evidência do plano.", 400)
    attestation = _validate_attestation(plan, payload)
    return plan, attempt, attestation


def _locked_resolution(repo, db, actor, oid, context):
    op = _operation(repo, db, actor, oid, admin=True)
    repo.account(db, op.account_id, True)
    configuration_lock(db, "integration:cloudflare")
    job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
    if not job or op.state != "reconciling" or job.state != "reconciling" or job.lease_token != context["token"]:
        raise RuleError("LEASE_LOST", "A recuperação foi assumida por outra execução.")
    resource = db.get(PublicationResource, op.project_id)
    cfg = _configuration(repo, db, op, resource, Integrations(repo))
    if _binding(op, resource, cfg) != context["binding"]:
        raise RuleError("RECOVERY_PLAN_STALE", "A evidência mudou durante a resolução.", 412)
    return op, job, resource, cfg


def _start_resolution(repo, actor, oid, context, plan, assessment, attestation, intention_id, payload_hash, reason):
    now = utcnow()
    with repo.sessions.begin() as db:
        op, job, resource, cfg = _locked_resolution(repo, db, actor, oid, context)
        resolution = _object(op.result.get("publication_resolution"))
        if resolution.get("plan", {}).get("plan_sha256") != plan["plan_sha256"]:
            raise RuleError("RECOVERY_PLAN_STALE", "Outro plano foi preparado durante a resolução.", 412)
        attempt = _object(resolution.get("attempt"))
        if attempt.get("state") == "applying":
            if attempt.get("intention_id") != intention_id or attempt.get("payload_hash") != payload_hash:
                raise RuleError("RECOVERY_IN_PROGRESS", "Outra intenção de resolução já foi iniciada.")
            return dict(attempt)
        pending_update = assessment.get("pending_update")
        if pending_update:
            resource.inventory = {**resource.inventory, "migration_in_progress": pending_update}
        attempt = {
            "state": "applying", "actor_id": actor, "intention_id": intention_id, "payload_hash": payload_hash,
            "plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"], "action": plan["action"],
            "origin": "operator_attested_schema_postconditions" if attestation else assessment["origin"],
            "identity": assessment["identity"], "attestation": attestation,
            "evidence_sha256": assessment["evidence_sha256"], "started_at": now.isoformat(),
            "reason": _text(reason, 300), "receipt_requested": plan["action"] in {"record_receipt", "adopt_schema"},
            "receipt_write_started": False,
            "binding": _binding(op, resource, cfg),
        }
        op.result = {
            **op.result, "publication_resolution": {**resolution, "attempt": attempt},
            "publication_recovery_assessment": {**assessment, "checked_at": now.isoformat()},
        }
        job.lease_until = now + LEASE_DURATION
        db.add(Audit(actor_id=actor, action="publication:recovery:started", resource_id=oid, reason=_text(reason, 300)))
        context["inventory"] = dict(resource.inventory)
        context["binding"].clear()
        context["binding"].update(attempt["binding"])
        return attempt


def _adopt_receipt(context, assessment):
    pending = context["inventory"]["migration_in_progress"]
    identity = assessment["identity"]["migration"]
    name, digest = identity["name"], identity["sha256"]
    gateway, database = context["gateway"], context["database_id"]
    existing = _remote_history(gateway.query(database, f"SELECT name, sha256 FROM {LEDGER} WHERE name = ?", [name]), context["inventory"])
    if existing:
        if existing != {name: digest}:
            raise RuleError("MIGRATION_CHECKSUM_CONFLICT", "O recibo remoto diverge do plano revisado.")
        return confirm_migration_receipt(gateway, database, pending)
    schema = assessment["schema"]
    observed = migration_schema_fingerprint(gateway, database)
    if observed != {"sha256": schema["observed_sha256"], "object_count": schema["observed_object_count"]}:
        raise RuleError("RECOVERY_PLAN_STALE", "O schema mudou após a revisão administrativa.", 412)
    context["before_receipt_write"]()
    gateway.query(database, f"INSERT INTO {LEDGER} (name, sha256) VALUES (?, ?)", [name, digest])
    return confirm_migration_receipt(gateway, database, pending)


def apply_resolution_result(op, job, resource, attempt, *, now=None):
    now = now or utcnow()
    identity = attempt["identity"]["migration"]
    inventory = dict(resource.inventory)
    pending = inventory.get("migration_in_progress")
    if pending and migration_identity(pending) != identity:
        raise RuleError("RECOVERY_PLAN_STALE", "O marcador pendente não corresponde à resolução.", 412)
    if attempt["action"] in {"record_receipt", "adopt_schema"}:
        inventory["migrations"] = {**inventory.get("migrations", {}), identity["name"]: identity["sha256"]}
    inventory["migration_last_attempt"] = {
        **_object(pending or inventory.get("migration_last_attempt")),
        "name": identity["name"], "sha256": identity["sha256"],
        "operation_id": op.id, "revision_id": op.payload["revision_id"],
        "resolution": "assisted_recovery", "resolution_origin": attempt["origin"],
        "resolution_plan_id": attempt["plan_id"], "reconciled_at": now.isoformat(),
    }
    inventory["migration_in_progress"] = None
    resource.inventory = inventory
    op.state, job.state = "failed", "done"
    job.lease_token, job.lease_until = None, None
    op.result = {
        **op.result, "external_effect_pending": False,
        "publication_resolution": {
            **_object(op.result.get("publication_resolution")),
            "attempt": {**attempt, "state": "completed", "completed_at": now.isoformat()},
        },
        "publication_recovery": recovery_status(
            "resolved", previous=op.result.get("publication_recovery"), resolution="migration_recovered",
            last_checked_at=now.isoformat(), next_attempt_at=None, last_error_code=None, last_error_message=None,
            requested_reassessment_pending=False, legacy_reassessment_pending=False,
        ),
    }
    record_progress(op, "publication_failed")
    op.result = {
        **op.result, "publication_diagnostic": publication_diagnostic(
            op.result, inventory, stage="publication_failed", operation_id=op.id, revision_id=op.payload["revision_id"],
        ),
    }


def assisted_receipt_observed(result, oid, revision, database_id, before, observed):
    from viracase.infrastructure.publication_recovery import pre_upload_observed

    resolution = _object(result.get("publication_resolution"))
    candidates = [_object(resolution.get("attempt")), *(resolution.get("previous_attempts") or [])]
    for candidate in candidates:
        attempt = _object(candidate)
        identity = _object(attempt.get("identity"))
        migration = migration_identity(identity.get("migration"))
        if (
            attempt.get("state") in {"applying", "superseded"} and attempt.get("receipt_requested")
            and attempt.get("action") in {"record_receipt", "adopt_schema"}
            and identity.get("operation_id") == oid and identity.get("revision_id") == revision
            and identity.get("database_id") == database_id and migration
            and migration_identity(before.get("migration_in_progress")) == migration
            and not observed.get("migration_in_progress")
            and _object(observed.get("migrations")).get(migration["name"]) == migration["sha256"]
            and pre_upload_observed(result, before, revision)
        ):
            return attempt
    return None


def resolve_recovery_plan(repo, actor, oid, key, payload, gateway_factory=CloudflareGateway):
    command = "publication:recovery:resolve:" + oid
    with repo.sessions() as db:
        op = _operation(repo, db, actor, oid, admin=True)
        completed = dict(_object(_object(op.result.get("publication_resolution")).get("attempt")))
    previous = repo.recover_intention(actor, command, key, payload)
    if previous is not None:
        return previous
    intention_id, payload_hash = canonical_hash([actor, command, key]), canonical_hash(payload)
    if completed.get("state") == "completed" and completed.get("intention_id") == intention_id:
        if completed.get("payload_hash") != payload_hash:
            raise RuleError("IDEMPOTENCY_CONFLICT", "A chave já identifica outro conteúdo.")

        def recovered_result(db, operator):
            op = _operation(repo, db, actor, oid, admin=True)
            return operation_dto(op, admin=True)

        return repo.mutate(actor, command, key, payload, recovered_result)
    with _recovery_scope(repo, actor, oid, gateway_factory) as context:
        plan, started, attestation = _validate_plan(context, actor, oid, payload, intention_id, payload_hash)
        assessment = _assess(context, oid)
        same_evidence = assessment.get("evidence_sha256") == plan["assessment"].get("evidence_sha256")
        resumed_receipt = bool(
            started.get("state") == "applying" and started.get("receipt_requested")
            and assessment.get("classification") == "confirmed" and assessment.get("origin") == "remote_receipt"
            and assessment.get("identity") == started.get("identity")
        )
        resumed_durable = bool(
            started.get("state") == "applying" and started.get("action") == "record_receipt"
            and assessment.get("classification") == "confirmed"
            and assessment.get("identity") == started.get("identity")
            and assessment.get("origin") == "durable_execution_reconstructed_schema"
        )
        if not same_evidence and not resumed_receipt and not resumed_durable:
            raise RuleError("RECOVERY_PLAN_STALE", "A observação remota mudou. A resolução não foi aplicada; prepare nova conferência.", 412)
        attempt = _start_resolution(repo, actor, oid, context, plan, assessment, attestation, intention_id, payload_hash, payload["reason"])

        def before_receipt_write():
            with repo.sessions.begin() as db:
                op, _, _, _ = _locked_resolution(repo, db, actor, oid, context)
                resolution = op.result["publication_resolution"]
                current = resolution["attempt"]
                if current.get("intention_id") != intention_id or current.get("state") != "applying":
                    raise RuleError("RECOVERY_IN_PROGRESS", "A intenção de resolução mudou.")
                updated = {**current, "receipt_write_started": True, "receipt_write_started_at": utcnow().isoformat()}
                op.result = {**op.result, "publication_resolution": {**resolution, "attempt": updated}}
            attempt.update(receipt_write_started=True, receipt_write_started_at=updated["receipt_write_started_at"])

        context["before_receipt_write"] = before_receipt_write
        try:
            if plan["action"] == "adopt_schema":
                if resumed_receipt:
                    confirm_migration_receipt(context["gateway"], context["database_id"], context["inventory"]["migration_in_progress"])
                else:
                    _adopt_receipt(context, assessment)
            elif plan["action"] == "record_receipt":
                confirm_migration_receipt(context["gateway"], context["database_id"], context["inventory"]["migration_in_progress"], before_write=before_receipt_write)
            context["guard"]()

            def complete(db, operator):
                op, job, resource, _ = _locked_resolution(repo, db, actor, oid, context)
                current = _object(_object(op.result.get("publication_resolution")).get("attempt"))
                if current.get("intention_id") != intention_id or current.get("payload_hash") != payload_hash:
                    raise RuleError("RECOVERY_IN_PROGRESS", "A intenção de resolução mudou.")
                apply_resolution_result(op, job, resource, attempt)
                db.add(Audit(actor_id=actor, action="publication:recovery:resolved", resource_id=oid, reason=_text(payload["reason"], 300)))
                return operation_dto(op, admin=True)

            return repo.mutate(actor, command, key, payload, complete)
        except Exception as exc:
            from viracase.infrastructure.publication_recovery import _retry_recovery

            if not isinstance(exc, RuleError) or exc.code != "LEASE_LOST":
                if not attempt.get("receipt_write_started"):
                    with repo.sessions.begin() as db:
                        op = db.get(Operation, oid)
                        repo.account(db, op.account_id, True)
                        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
                        current_resolution = _object(op.result.get("publication_resolution"))
                        current_attempt = _object(current_resolution.get("attempt"))
                        if job.lease_token == context["token"] and current_attempt.get("intention_id") == intention_id and not current_attempt.get("receipt_write_started"):
                            op.result = {
                                **op.result, "publication_resolution": {
                                    **current_resolution,
                                    "attempt": {**current_attempt, "state": "aborted", "aborted_at": utcnow().isoformat()},
                                },
                            }
                _retry_recovery(repo, oid, context["token"], exc, secrets=(*repo.sources.secrets, context["secret"]), manual=True)
            raise
