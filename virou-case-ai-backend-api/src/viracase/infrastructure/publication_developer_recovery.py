import json
from time import monotonic

from sqlalchemy import select

from viracase.application.progress import STAGES, record_progress
from viracase.domain.publication_recovery_access import (
    RECOVER_OWN_PUBLICATION_NO_EFFECT,
    authorize_publication_recovery,
)
from viracase.domain.rules import RuleError, aware, canonical_hash, utcnow
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Audit, Idempotency, Job, Operation, PublicationResource, new_id
from viracase.infrastructure.publication_logging import emit, safe_text
from viracase.infrastructure.publication_recovery import DEPLOYMENT_STAGES, LEASE_DURATION, recovery_status

REQUEST_ACTION = "check_and_close_if_no_effect"
REQUEST_CONSENT = "verify_and_close_failed_attempt_when_proven_safe"
ACTIVE_REQUEST_STATES = {"queued", "running", "resolving"}
MAX_REQUEST_ATTEMPTS = 3
MAX_EVIDENCE_EVALUATIONS = 2
RETRYABLE_REASONS = {"REMOTE_UNAVAILABLE", "PLAN_EXPIRED", "EVIDENCE_CHANGED", "OPERATION_BUSY"}
MESSAGES = {
    "PROOF_NOT_YET_EVALUATED": "Confira as evidências para encerrar a tentativa quando a ausência de efeitos for comprovada.",
    "PROOF_INCOMPLETE": "Falta uma prova necessária para encerrar esta tentativa com segurança.",
    "REMOTE_UNAVAILABLE": "Não foi possível consultar a hospedagem. Aguarde o intervalo indicado e tente novamente.",
    "PLAN_EXPIRED": "A validade da análise terminou antes do encerramento. Prepare uma nova análise.",
    "EVIDENCE_CHANGED": "As evidências mudaram durante a conferência. A tentativa permanece pendente.",
    "OPERATION_BUSY": "Há uma conferência em andamento. Acompanhe o resultado desta solicitação.",
    "UNSUPPORTED_EFFECTS": "Os comandos desta tentativa não pertencem ao conjunto permitido para recuperação automática.",
    "DML_UNCERTAIN": "Há possíveis alterações de dados. A igualdade do schema não comprova ausência de efeitos.",
    "SCHEMA_APPLIED": "A estrutura indica efeitos aplicados. Esta recuperação não pode encerrar a tentativa como sem efeito.",
    "CONFIGURATION_CHANGED": "A conexão mudou ou não pôde ser confirmada. Revalide a configuração pelo fluxo existente.",
    "ACCESS_DENIED": "O acesso vigente não autoriza recuperar esta tentativa.",
    "RECOVERY_ALREADY_RESOLVED": "Pendência encerrada. Nenhuma migration foi reaplicada.",
    "RECOVERY_NOT_PENDING": "Esta tentativa não está aguardando recuperação.",
    "RECOVERY_COOLDOWN": "Aguarde o intervalo indicado antes de solicitar outra conferência.",
    "LIFECYCLE_LOCKED": "A recuperação aguarda o término da manutenção.",
}


def _object(value):
    return value if isinstance(value, dict) else {}


def _authorize(repo, db, actor, oid):
    from viracase.infrastructure.publication_resolution import _operation

    op = _operation(repo, db, actor, oid)
    account = repo.account(db, actor)
    project = repo.owned_project(db, actor, op.project_id)
    authorize_publication_recovery(actor, account.role, project.account_id, op.account_id)
    repo.access(db, account, RECOVER_OWN_PUBLICATION_NO_EFFECT)
    return op, account, project


def _publication_checkpoint(op, resource):
    result = _object(op.result)
    inventory = _object(resource.inventory) if resource else {}
    attempt = result.get("publication_attempt")
    activity = result.get("activity", [])
    failure = result.get("publication_failure")
    activity_valid = isinstance(activity, list) and all(
        isinstance(item, dict) and isinstance(item.get("stage"), str) and item["stage"] in STAGES
        and ("at" not in item or isinstance(item["at"], str))
        for item in activity
    )
    failure_valid = failure is None or isinstance(failure, dict) and (
        failure.get("stage") is None or isinstance(failure["stage"], str) and failure["stage"] in STAGES
    )
    upload_events = [item for item in activity if item["stage"] in DEPLOYMENT_STAGES] if activity_valid else None
    revision = op.payload.get("revision_id")
    safe = bool(
        activity_valid and failure_valid and isinstance(attempt, dict)
        and type(attempt.get("version")) is int and attempt["version"] == 1
        and isinstance(revision, str) and revision and attempt.get("revision_id") == revision
        and attempt.get("stage") == "publication_migrating" and attempt.get("upload_started") is False
        and attempt.get("deployment_id") in (None, "")
        and _object(failure).get("stage") not in DEPLOYMENT_STAGES
        and result.get("deployment_id") in (None, "") and result.get("verified_at") in (None, "")
        and (inventory.get("candidate_revision_id") is None or isinstance(inventory["candidate_revision_id"], str))
        and inventory.get("candidate_revision_id") != revision and not upload_events
    )
    evidence = {
        "publication_attempt": attempt,
        "failure_stage": _object(failure).get("stage") if failure_valid else failure,
        "upload_activity": upload_events if activity_valid else {"invalid_activity": activity},
        "deployment_id": result.get("deployment_id"), "verified_at": result.get("verified_at"),
        "candidate_revision_id": inventory.get("candidate_revision_id"),
    }
    return canonical_hash(evidence), safe


def _developer_binding(op, resource, cfg):
    from viracase.infrastructure.publication_resolution import _binding

    checkpoint, safe = _publication_checkpoint(op, resource)
    result = _object(op.result)
    additional_effects = {key: result.get(key) for key in (
        "pending_execution_units", "migration_batch_pending", "resource_effect_pending", "upload_in_progress", "receipt_write_started",
    )}
    additional_effects["resolution_receipt_write_started"] = _object(
        _object(result.get("publication_resolution")).get("attempt")
    ).get("receipt_write_started")
    evidence = {
        "publication_checkpoint_sha256": checkpoint,
        "inventory_sha256": canonical_hash(resource.inventory if resource else None),
        "additional_effects_sha256": canonical_hash(additional_effects),
    }
    if not safe:
        return {
            "operation_id": op.id, **evidence, "publication_checkpoint_valid": False,
        }
    return {**_binding(op, resource, cfg), **evidence}


def _current_binding(repo, db, op, resource):
    if not resource or resource.account_id != op.account_id:
        return None
    cfg = Integrations(repo).for_resource(db, resource)
    return _developer_binding(op, resource, cfg) if cfg else None


def _state_version(repo, db, op, resource):
    return canonical_hash({
        "operation_id": op.id,
        "account_id": op.account_id,
        "project_id": op.project_id,
        "operation_state": op.state,
        "payload": op.payload,
        "recovery": _object(op.result).get("publication_recovery"),
        "assessment": _object(op.result).get("publication_recovery_assessment"),
        "request": _object(op.result).get("publication_developer_recovery"),
        "resolution": _object(op.result).get("publication_resolution"),
        "binding": _current_binding(repo, db, op, resource),
    })


def _public_request(request):
    if not request:
        return None
    return {
        "id": request.get("id"),
        "state": request.get("state"),
        "requested_at": request.get("requested_at"),
        "started_at": request.get("started_at"),
        "completed_at": request.get("completed_at"),
        "reason_code": request.get("reason_code"),
    }


def _request_block(repo, db, op, actor, resource, request):
    from viracase.infrastructure.publication_resolution import CHECK_COOLDOWN, _time

    now = utcnow()
    try:
        _authorize(repo, db, actor, op.id)
    except RuleError:
        return "ACCESS_DENIED", None
    if op.state != "reconciling":
        return "RECOVERY_ALREADY_RESOLVED" if request.get("state") == "resolved" else "RECOVERY_NOT_PENDING", None
    job = db.scalar(select(Job).where(Job.operation_id == op.id))
    if not job or job.state != "reconciling":
        return "RECOVERY_NOT_PENDING", None
    if repo.config.restore_hold or not repo.config.lifecycle_enabled:
        return "LIFECYCLE_LOCKED", None
    if not _publication_checkpoint(op, resource)[1]:
        return "PROOF_INCOMPLETE", None
    recovery = _object(op.result.get("publication_recovery"))
    attempt = _object(_object(op.result.get("publication_resolution")).get("attempt"))
    if (
        request.get("state") in ACTIVE_REQUEST_STATES
        or job.lease_until and aware(job.lease_until) > now
        or recovery.get("state") in {"scheduled", "running", "retry_wait"}
        or attempt.get("state") == "applying"
    ):
        return "OPERATION_BUSY", None
    previous_request = _object(op.result.get("publication_recovery_request"))
    requested = max(
        (value for value in (_time(request.get("requested_at")), _time(previous_request.get("requested_at"))) if value),
        default=None,
    )
    cooldown = requested + CHECK_COOLDOWN if requested else None
    if cooldown and cooldown > now:
        return "RECOVERY_COOLDOWN", cooldown.isoformat()
    reason = request.get("reason_code")
    if (
        request.get("state") == "attention"
        and reason not in RETRYABLE_REASONS
        and request.get("binding") == _current_binding(repo, db, op, resource)
    ):
        return reason if reason in MESSAGES else "PROOF_INCOMPLETE", None
    return None, cooldown.isoformat() if cooldown else None


def developer_recovery_projection(repo, db, op, actor, snapshot):
    if actor is None:
        return {}
    resource = db.get(PublicationResource, op.project_id)
    request = _object(_object(op.result).get("publication_developer_recovery"))
    decision = _object(request.get("decision"))
    state = request.get("state")
    resolved_elsewhere = snapshot.get("classification") == "resolved" and state != "resolved"
    reason = request.get("reason_code") or "PROOF_NOT_YET_EVALUATED"
    eligibility = {
        "queued": "evaluating", "running": "evaluating", "resolving": "eligible",
        "resolved": "resolved", "attention": "ineligible",
    }.get(state, "requires_evaluation")
    blocked, cooldown = _request_block(repo, db, op, actor, resource, request) if actor else ("ACCESS_DENIED", None)
    can_request = blocked is None
    if blocked == "PROOF_INCOMPLETE" and state not in ACTIVE_REQUEST_STATES:
        eligibility, reason = "ineligible", "PROOF_INCOMPLETE"
        if not decision.get("missing_evidence_messages"):
            decision = {
                **decision, "missing_evidence": ["pre_upload_checkpoint"],
                "missing_evidence_messages": [
                    "O checkpoint ou histórico anterior ao upload está incompleto, malformado ou contém evidência de envio. Preserve o diagnóstico para conferência."
                ],
            }
    if resolved_elsewhere:
        eligibility, reason, blocked = "resolved", "RECOVERY_ALREADY_RESOLVED", "RECOVERY_ALREADY_RESOLVED"
        can_request = False
    if state in ACTIVE_REQUEST_STATES and not resolved_elsewhere:
        next_action = {"type": "poll", "label": "Acompanhar recuperação"}
    elif state == "resolved" or resolved_elsewhere:
        next_action = {"type": "review_readiness", "label": "Conferir revisão atual"}
    elif reason == "CONFIGURATION_CHANGED" and not can_request:
        next_action = {"type": "revalidate_configuration", "label": "Revalidar conexão"}
    elif can_request or blocked == "RECOVERY_COOLDOWN" and reason in RETRYABLE_REASONS:
        next_action = {"type": "check_and_resolve", "label": "Tentar novamente" if state else "Conferir e resolver"}
    else:
        next_action = {"type": "export_diagnostic", "label": "Exportar diagnóstico"}
    missing = decision.get("missing_evidence_messages") or []
    missing = [safe_text(item, limit=300) for item in missing[:30] if isinstance(item, str)]
    resolution = {
        "mode": "developer_no_effect", "action": "close_no_effect",
        "eligibility": eligibility, "reason_code": reason,
        "message": MESSAGES.get(reason, MESSAGES["PROOF_INCOMPLETE"]),
        "missing_evidence": missing,
        "missing_evidence_codes": decision.get("missing_evidence", [])[:30],
        "next_action": next_action,
        "policy_version": decision.get("policy_version"),
        "evidence_hash": decision.get("evidence_sha256"),
        "proofs": {name: value for name, value in _object(decision.get("proofs")).items() if type(value) is bool},
        "plan_id": _object(request.get("plan")).get("plan_id"),
        "completed_at": request.get("completed_at"),
        "blocked_code": blocked,
        "blocked_reason": MESSAGES.get(blocked) if blocked else None,
        "cooldown_until": cooldown,
    }
    if resolved_elsewhere:
        resolution["message"] = "A pendência foi encerrada por outra resolução. Confira o resultado e a revisão atual."
    projected = {
        "capabilities": {
            "can_check": snapshot.get("can_check", False),
            "can_check_and_resolve": can_request,
            "can_close_now": False,
        },
        "resolution": resolution,
        "state_version": _state_version(repo, db, op, resource),
        "checked_at": snapshot.get("last_checked_at"),
        "recovery_request_id": request.get("id"),
        "request": _public_request(request),
    }
    if actor and not resolved_elsewhere and (can_request or state in ACTIVE_REQUEST_STATES):
        projected["required_action"] = "Conferir e resolver neste projeto." if can_request else "Acompanhe a recuperação desta tentativa."
    if state == "attention" and not resolved_elsewhere:
        projected["required_action"] = resolution["message"]
    if state == "resolved" or resolved_elsewhere:
        from viracase.infrastructure.publication import PublicationService

        account = repo.account(db, op.account_id)
        project = repo.owned_project(db, op.account_id, op.project_id)
        try:
            projected["readiness"] = PublicationService(repo).inspect(db, account, project, project.current_revision_id)
        except RuleError as exc:
            projected["readiness"] = {
                "can_publish": False, "revision_id": project.current_revision_id,
                "reasons": [{"code": exc.code, "message": safe_text(exc.message, limit=300)}],
            }
        projected["summary"] = resolution["message"]
    return projected


def _audit(db, op, request, action, **changes):
    decision = _object(request.get("decision"))
    plan = _object(request.get("plan"))
    fields = {
        "correlation_id": op.id, "tenant_id": op.account_id, "project_id": op.project_id,
        "operation_id": op.id, "revision_id": op.payload.get("revision_id"),
        "actor_id": request["actor_id"], "request_id": request["id"],
        "policy_version": decision.get("policy_version"), "plan_id": plan.get("plan_id"),
        "plan_hash": plan.get("plan_sha256"), "evidence_hash": decision.get("evidence_sha256"),
        "action": "close_no_effect", "reason_code": request.get("reason_code"),
        "proofs": {name: value for name, value in _object(decision.get("proofs")).items() if type(value) is bool},
        "missing_evidence": decision.get("missing_evidence", []),
        **changes,
    }
    db.add(Audit(
        actor_id=request["actor_id"], action="publication:developer_recovery:" + action,
        resource_id=op.id, reason=json.dumps(fields, sort_keys=True, ensure_ascii=False),
    ))
    if action not in {"committed", "readiness_updated"}:
        emit("publication_developer_recovery_" + action, **fields)
    return fields


def _persist_response(db, request, response):
    intention = db.get(Idempotency, request.get("intention_id"))
    if intention and intention.actor_id == request["actor_id"] and intention.payload_hash == request["payload_hash"]:
        intention.result = response


def request_developer_recovery(repo, actor, oid, key, payload):
    from viracase.infrastructure.publication_resolution import _configuration, _snapshot

    if (
        set(payload) != {"expected_state_version", "action", "consent"}
        or payload.get("action") != REQUEST_ACTION or payload.get("consent") != REQUEST_CONSENT
        or not isinstance(payload.get("expected_state_version"), str)
        or len(payload["expected_state_version"]) != 64
    ):
        raise RuleError("RECOVERY_ACTION_INVALID", "Informe a intenção explícita de conferir e encerrar somente uma tentativa comprovadamente sem efeito.", 400)
    command = "publication:developer_recovery:" + oid
    with repo.sessions() as db:
        _authorize(repo, db, actor, oid)
    previous = repo.recover_intention(actor, command, key, payload)
    if previous is not None:
        with repo.sessions() as db:
            op, _, _ = _authorize(repo, db, actor, oid)
            current = _object(op.result.get("publication_developer_recovery"))
            if current.get("id") == previous.get("recovery_request_id"):
                return _snapshot(repo, db, op, actor=actor)
            return previous
    repo.rate_limit("publication:developer_recovery:" + actor, maximum=10, seconds=300)

    def accept(db, account):
        op, _, _ = _authorize(repo, db, actor, oid)
        configuration_lock(db, "integration:cloudflare")
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        resource = db.scalar(select(PublicationResource).where(PublicationResource.project_id == op.project_id).with_for_update())
        request = _object(op.result.get("publication_developer_recovery"))
        if request.get("state") == "resolved":
            return _snapshot(repo, db, op, actor=actor)
        if payload["expected_state_version"] != _state_version(repo, db, op, resource):
            raise RuleError("EVIDENCE_CHANGED", "O estado mudou desde a última leitura. Atualize o diagnóstico e solicite nova análise.", 412)
        blocked, cooldown = _request_block(repo, db, op, actor, resource, request)
        if blocked:
            raise RuleError(blocked, MESSAGES.get(blocked, MESSAGES["PROOF_INCOMPLETE"]), 429 if blocked == "RECOVERY_COOLDOWN" else 409, {"cooldown_until": cooldown})
        cfg = _configuration(repo, db, op, resource, Integrations(repo), access_action=RECOVER_OWN_PUBLICATION_NO_EFFECT)
        now = utcnow()
        accepted = {
            "format_version": 1, "id": new_id(), "state": "queued", "actor_id": actor,
            "account_id": op.account_id, "project_id": op.project_id, "operation_id": op.id,
            "revision_id": op.payload.get("revision_id"), "requested_at": now.isoformat(),
            "action": REQUEST_ACTION, "consent": REQUEST_CONSENT,
            "intention_id": canonical_hash([actor, command, key]), "payload_hash": canonical_hash(payload),
            "expected_state_version": payload["expected_state_version"],
            "binding": _developer_binding(op, resource, cfg), "attempts": 0,
            "reason_code": "PROOF_NOT_YET_EVALUATED",
        }
        history = list(op.result.get("publication_developer_recovery_history") or [])
        if request:
            history.append(request)
        op.result = {
            **op.result, "publication_developer_recovery": accepted,
            "publication_developer_recovery_history": history,
            "publication_recovery": recovery_status(
                "scheduled", previous=op.result.get("publication_recovery"), next_attempt_at=now.isoformat(),
                requested_reassessment_pending=False,
            ),
        }
        job.available_at = now
        record_progress(op, "publication_reconciling")
        _audit(db, op, accepted, "requested", consent=REQUEST_CONSENT, state_before="attention", state_after="queued")
        return _snapshot(repo, db, op, actor=actor)

    accepted = repo.mutate(actor, command, key, payload, accept)
    with repo.sessions() as db:
        op, _, _ = _authorize(repo, db, actor, oid)
        current = _object(op.result.get("publication_developer_recovery"))
        return _snapshot(repo, db, op, actor=actor) if current.get("id") == accepted.get("recovery_request_id") else accepted


def _locked_context(repo, db, oid, token, context=None):
    from viracase.infrastructure.publication_resolution import _configuration

    op = db.get(Operation, oid)
    if not op:
        raise RuleError("LEASE_LOST", "A tentativa não está mais disponível.")
    request = _object(op.result.get("publication_developer_recovery"))
    actor = request.get("actor_id")
    if not actor:
        raise RuleError("ACCESS_DENIED", MESSAGES["ACCESS_DENIED"], 403)
    repo.account(db, actor, True)
    op = db.scalar(select(Operation).where(Operation.id == oid).with_for_update().execution_options(populate_existing=True))
    request = _object(op.result.get("publication_developer_recovery"))
    if request.get("actor_id") != actor:
        raise RuleError("ACCESS_DENIED", "A identidade autorizada mudou durante a recuperação.", 403)
    _authorize(repo, db, actor, oid)
    configuration_lock(db, "integration:cloudflare")
    job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
    if (
        op.state != "reconciling" or not job or job.state != "reconciling"
        or job.lease_token != token or not job.lease_until or aware(job.lease_until) <= utcnow()
        or request.get("state") not in ACTIVE_REQUEST_STATES
    ):
        raise RuleError("LEASE_LOST", "Outra execução assumiu a recuperação.")
    if (
        request.get("action") != REQUEST_ACTION or request.get("consent") != REQUEST_CONSENT
        or request.get("account_id") != op.account_id or request.get("project_id") != op.project_id
        or request.get("operation_id") != op.id or request.get("revision_id") != op.payload.get("revision_id")
    ):
        raise RuleError("ACCESS_DENIED", "O escopo da autorização mudou. É necessária uma nova ação explícita.", 403)
    resource = db.scalar(select(PublicationResource).where(PublicationResource.project_id == op.project_id).with_for_update())
    if not _publication_checkpoint(op, resource)[1]:
        raise RuleError("PROOF_INCOMPLETE", "O checkpoint ou histórico anterior ao upload não permite comprovar uma tentativa sem envio.")
    cfg = _configuration(repo, db, op, resource, Integrations(repo), access_action=RECOVER_OWN_PUBLICATION_NO_EFFECT)
    binding = _developer_binding(op, resource, cfg)
    accepted = request.get("binding")
    if accepted != binding or context and context["binding"] != binding:
        code = "CONFIGURATION_CHANGED" if any(
            _object(accepted).get(key) != binding.get(key)
            for key in ("configuration_id", "configuration_version", "configuration_sha256", "remote_account_id", "database_id")
        ) else "EVIDENCE_CHANGED"
        raise RuleError(code, MESSAGES[code], 412)
    if context and request.get("id") != context["request_id"]:
        raise RuleError("ACCESS_DENIED", "A solicitação autorizada mudou durante a recuperação.", 403)
    competing = db.scalar(select(Operation.id).where(
        Operation.project_id == op.project_id, Operation.type == "publication", Operation.id != oid,
        Operation.state.in_(["queued", "running", "reconciling"]),
    ).limit(1))
    if competing:
        raise RuleError("OPERATION_BUSY", MESSAGES["OPERATION_BUSY"])
    job.lease_until = utcnow() + LEASE_DURATION
    return op, job, resource, cfg, request, binding


def _remote_identity(context):
    gateway, policy = context["gateway"], context["policy"]
    context["remote_identity_verified"] = False
    account_path = "/accounts/" + policy["account_id"]
    token_path = account_path + "/tokens/verify" if policy["token_type"] == "account" else "/user/tokens/verify"
    token = gateway.request("GET", token_path)
    account = gateway.request("GET", account_path)
    database = gateway.request("GET", account_path + "/d1/database/" + context["database_id"])
    named = gateway.database(context["worker_name"])
    if (
        not isinstance(token, dict) or token.get("status") != "active"
        or not isinstance(account, dict) or account.get("id") != policy["account_id"]
        or not isinstance(database, dict) or database.get("uuid") != context["database_id"]
        or database.get("name") != context["worker_name"]
        or not isinstance(named, dict) or named.get("uuid") != context["database_id"]
    ):
        raise RuleError("CONFIGURATION_CHANGED", MESSAGES["CONFIGURATION_CHANGED"])
    context["remote_identity_verified"] = True
    context["remote_account_id"] = account["id"]


def _decision_snapshot(decision):
    return {
        "eligible": decision.get("eligible") is True,
        "reason_code": decision.get("reason_code", "PROOF_INCOMPLETE"),
        "missing_evidence": list(decision.get("missing_evidence") or []),
        "missing_evidence_messages": list(decision.get("missing_evidence_messages") or []),
        "policy_version": decision.get("policy_version"),
        "evidence_sha256": decision.get("evidence_sha256"),
        "proofs": dict(decision.get("proofs") or {}),
        "origin": "automated_no_effect_proof",
    }


def _store_plan(repo, oid, token, context, assessment, decision, evaluation):
    from viracase.infrastructure.publication_resolution import PLAN_VALIDITY, _time

    now = utcnow()
    plan = {
        "format_version": 1, "plan_id": new_id(), "operation_id": oid,
        "revision_id": context["revision_id"], "request_id": context["request_id"],
        "prepared_by": context["actor_id"], "origin": "automated_no_effect_proof",
        "action": "close_no_effect", "created_at": now.isoformat(),
        "expires_at": (now + PLAN_VALIDITY).isoformat(),
        "binding": context["binding"], "files_sha256": context["files_sha256"],
        "policy_version": decision["policy_version"], "evidence_hash": decision["evidence_sha256"],
        "evidence_sha256": decision["evidence_sha256"], "proofs": decision["proofs"],
        "can_resolve": True, "consent": REQUEST_CONSENT,
    }
    plan["plan_sha256"] = canonical_hash(plan)
    with repo.sessions.begin() as db:
        op, _, _, _, request, _ = _locked_context(repo, db, oid, token, context)
        previous_plans = list(request.get("previous_plans") or [])
        if request.get("plan"):
            previous_expiry = _time(request["plan"].get("expires_at"))
            previous_plans.append({
                **request["plan"], "invalidated_at": now.isoformat(),
                "reason_code": "PLAN_EXPIRED" if not previous_expiry or previous_expiry <= now else "EVIDENCE_CHANGED",
            })
        updated = {
            **request, "state": "resolving", "decision": _decision_snapshot(decision),
            "plan": plan, "previous_plans": previous_plans, "evaluation": evaluation,
        }
        op.result = {
            **op.result, "publication_developer_recovery": updated,
            "publication_recovery_assessment": {**assessment, "checked_at": now.isoformat()},
        }
        _audit(db, op, updated, "proof_collected", state_before=request["state"], state_after="resolving")
        _audit(db, op, updated, "plan_eligible", state_before=request["state"], state_after="resolving")
    return plan


def _complete(repo, oid, token, context, plan, assessment, decision, elapsed):
    from viracase.infrastructure.publication_resolution import _snapshot, _time, apply_resolution_result

    with repo.sessions.begin() as db:
        op, job, resource, _, request, _ = _locked_context(repo, db, oid, token, context)
        saved_plan = _object(request.get("plan"))
        now = utcnow()
        if (
            saved_plan != plan
            or plan.get("plan_sha256") != canonical_hash({key: value for key, value in plan.items() if key != "plan_sha256"})
            or plan.get("request_id") != request["id"]
            or plan.get("binding") != context["binding"]
            or plan.get("action") != "close_no_effect"
            or plan.get("consent") != request["consent"]
            or plan.get("policy_version") != decision.get("policy_version")
            or plan.get("evidence_hash") != decision.get("evidence_sha256")
            or decision.get("eligible") is not True
        ):
            raise RuleError("EVIDENCE_CHANGED", MESSAGES["EVIDENCE_CHANGED"], 412)
        expires = _time(plan.get("expires_at"))
        if not expires or expires <= now:
            raise RuleError("PLAN_EXPIRED", MESSAGES["PLAN_EXPIRED"], 410)
        original = repo.source_files(context["owner"], context["project_id"], context["revision_id"])
        if canonical_hash(original) != context["files_sha256"]:
            raise RuleError("EVIDENCE_CHANGED", "A revisão original mudou durante a conferência.", 412)
        _audit(db, op, request, "revalidated", state_before="resolving", state_after="resolving")
        attempt = {
            "state": "applying", "actor_id": context["actor_id"],
            "intention_id": request["intention_id"], "payload_hash": request["payload_hash"],
            "request_id": request["id"], "plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"],
            "action": "close_no_effect", "origin": "automated_no_effect_proof",
            "identity": assessment["identity"], "attestation": None,
            "evidence_sha256": decision["evidence_sha256"], "policy_version": decision["policy_version"],
            "started_at": request.get("started_at"), "consent": request["consent"],
            "receipt_requested": False, "receipt_write_started": False,
            "binding": context["binding"],
        }
        previous_resolution = _object(op.result.get("publication_resolution"))
        previous_plans = list(previous_resolution.get("previous_plans") or [])
        if previous_resolution.get("plan"):
            previous_plans.append(previous_resolution["plan"])
        op.result = {
            **op.result,
            "publication_resolution": {**previous_resolution, "plan": plan, "previous_plans": previous_plans},
            "publication_recovery_assessment": {**assessment, "checked_at": now.isoformat()},
        }
        apply_resolution_result(op, job, resource, attempt, now=now)
        completed = {
            **request, "state": "resolved", "reason_code": "RECOVERY_ALREADY_RESOLVED",
            "completed_at": now.isoformat(), "decision": _decision_snapshot(decision),
        }
        op.result = {**op.result, "publication_developer_recovery": completed}
        committed_event = _audit(db, op, completed, "committed", state_before="reconciling", state_after="failed", duration_ms=elapsed)
        db.flush()
        response = _snapshot(repo, db, op, actor=context["actor_id"])
        _persist_response(db, completed, response)
        readiness_event = _audit(db, op, completed, "readiness_updated", can_publish=response.get("readiness", {}).get("can_publish", False))
    emit("publication_developer_recovery_committed", **committed_event)
    emit("publication_developer_recovery_readiness_updated", **readiness_event)
    return response


def _fail(repo, oid, token, code, *, decision=None, assessment=None, elapsed=None):
    from viracase.infrastructure.publication_resolution import _snapshot

    if code not in MESSAGES:
        code = "PROOF_INCOMPLETE"
    with repo.sessions.begin() as db:
        op = db.get(Operation, oid)
        if not op:
            return
        repo.account(db, op.account_id, True)
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        request = _object(op.result.get("publication_developer_recovery"))
        if not job or job.lease_token != token or job.state != "reconciling" or op.state != "reconciling":
            return
        now = utcnow()
        updated = {**request, "state": "attention", "reason_code": code, "completed_at": now.isoformat()}
        if request.get("plan"):
            updated["previous_plans"] = [
                *(request.get("previous_plans") or []),
                {**request["plan"], "invalidated_at": now.isoformat(), "reason_code": code},
            ]
            updated["plan"] = None
        if decision:
            updated["decision"] = {**_decision_snapshot(decision), "eligible": False, "reason_code": code}
        else:
            if request.get("decision"):
                updated["previous_decisions"] = [*(request.get("previous_decisions") or []), request["decision"]]
            updated["decision"] = {
                "eligible": False, "reason_code": code,
                "policy_version": _object(request.get("decision")).get("policy_version"),
                "evidence_sha256": None, "proofs": {},
                "missing_evidence": ["remote_observation"] if code == "REMOTE_UNAVAILABLE" else [code.lower()],
                "missing_evidence_messages": [MESSAGES[code]],
            }
        op.result = {
            **op.result, "publication_developer_recovery": updated,
            "publication_recovery": recovery_status(
                "attention", previous=op.result.get("publication_recovery"),
                last_checked_at=now.isoformat(), next_attempt_at=None,
                last_error_code=code, last_error_message=MESSAGES[code],
                required_action=MESSAGES[code], missing_evidence=updated["decision"].get("missing_evidence_messages", []),
                requested_reassessment_pending=False, legacy_reassessment_pending=False,
            ),
        }
        if assessment:
            op.result = {**op.result, "publication_recovery_assessment": {**assessment, "checked_at": now.isoformat()}}
        job.lease_token, job.lease_until = None, None
        resource = db.get(PublicationResource, op.project_id)
        if _publication_checkpoint(op, resource)[1]:
            record_progress(op, "publication_reconciling")
        else:
            op.progress_stage = "publication_reconciling"
        _audit(db, op, updated, "denied", state_before=request.get("state"), state_after="attention", duration_ms=elapsed)
        _persist_response(db, updated, _snapshot(repo, db, op, actor=updated["actor_id"]))


def run_developer_recovery(repo, oid, token, gateway_factory):
    from viracase.infrastructure.publication_developer_proof import evaluate_developer_no_effect
    from viracase.infrastructure.publication_resolution import _assess, _time

    started = monotonic()
    context, assessment, decision = None, None, None
    try:
        with repo.sessions.begin() as db:
            op, _, resource, cfg, request, binding = _locked_context(repo, db, oid, token)
            if request.get("attempts", 0) >= MAX_REQUEST_ATTEMPTS:
                raise RuleError("REMOTE_UNAVAILABLE", "O limite de retomadas desta solicitação foi atingido.")
            now = utcnow()
            updated = {
                **request, "state": "running", "started_at": request.get("started_at") or now.isoformat(),
                "attempts": request.get("attempts", 0) + 1,
            }
            op.result = {**op.result, "publication_developer_recovery": updated}
            registry = Integrations(repo)
            context = {
                "token": token, "binding": binding, "inventory": dict(resource.inventory),
                "result": dict(op.result), "policy": dict(cfg.settings),
                "secret": registry.vault.decrypt(cfg.id, cfg.credential), "owner": op.account_id,
                "actor_id": request["actor_id"], "project_id": op.project_id,
                "revision_id": op.payload["revision_id"], "database_id": resource.database_id,
                "worker_name": resource.worker_name, "request_id": request["id"],
            }
            _audit(db, op, updated, "started", state_before=request["state"], state_after="running")

        def guard(method="GET"):
            with repo.sessions.begin() as db:
                _locked_context(repo, db, oid, token, context)

        context["guard"] = guard
        context["files"] = repo.source_files(context["owner"], context["project_id"], context["revision_id"])
        context["files_sha256"] = canonical_hash(context["files"])
        if not context["binding"].get("source_hash") or context["binding"]["source_hash"] != context["files_sha256"]:
            raise RuleError("EVIDENCE_CHANGED", "A fonte original não corresponde ao hash preservado.", 412)
        gateway = gateway_factory(context["policy"], context["secret"])
        gateway.before_request = guard
        context["gateway"] = gateway
        if not context["database_id"]:
            raise RuleError("PROOF_INCOMPLETE", "A identidade do banco não foi preservada.")
        for evaluation in range(1, MAX_EVIDENCE_EVALUATIONS + 1):
            guard()
            _remote_identity(context)
            assessment = _assess(context, oid)
            decision = evaluate_developer_no_effect(context, assessment)
            if not decision.get("eligible"):
                _fail(repo, oid, token, decision.get("reason_code", "PROOF_INCOMPLETE"), decision=decision, assessment=assessment, elapsed=round((monotonic() - started) * 1000))
                return
            plan = _store_plan(repo, oid, token, context, assessment, decision, evaluation)
            guard()
            _remote_identity(context)
            refreshed_assessment = _assess(context, oid)
            refreshed = evaluate_developer_no_effect(context, refreshed_assessment)
            if not refreshed.get("eligible"):
                _fail(repo, oid, token, refreshed.get("reason_code", "PROOF_INCOMPLETE"), decision=refreshed, assessment=refreshed_assessment, elapsed=round((monotonic() - started) * 1000))
                return
            expires = _time(plan.get("expires_at"))
            if not expires or expires <= utcnow():
                if evaluation < MAX_EVIDENCE_EVALUATIONS:
                    continue
                raise RuleError("PLAN_EXPIRED", MESSAGES["PLAN_EXPIRED"], 410)
            if refreshed.get("evidence_sha256") != plan["evidence_hash"] or refreshed.get("policy_version") != plan["policy_version"]:
                if evaluation < MAX_EVIDENCE_EVALUATIONS:
                    continue
                raise RuleError("EVIDENCE_CHANGED", MESSAGES["EVIDENCE_CHANGED"], 412)
            guard()
            _complete(repo, oid, token, context, plan, refreshed_assessment, refreshed, round((monotonic() - started) * 1000))
            return
    except Exception as exc:
        code = getattr(exc, "code", "REMOTE_UNAVAILABLE")
        if code == "LEASE_LOST":
            return
        code = {
            "CONFIGURATION_INVALID": "CONFIGURATION_CHANGED", "RESOURCE_CONFLICT": "CONFIGURATION_CHANGED",
            "DEPENDENCY_UNAVAILABLE": "REMOTE_UNAVAILABLE", "PUBLICATION_RESULT_UNKNOWN": "REMOTE_UNAVAILABLE",
            "PUBLICATION_REJECTED": "REMOTE_UNAVAILABLE", "QUOTA_EXCEEDED": "REMOTE_UNAVAILABLE",
            "ACCESS_EXPIRED": "ACCESS_DENIED", "RETENTION_EXPIRED": "ACCESS_DENIED", "FORBIDDEN": "ACCESS_DENIED",
            "NOT_FOUND": "ACCESS_DENIED", "RECOVERY_PLAN_STALE": "EVIDENCE_CHANGED",
        }.get(code, code)
        _fail(repo, oid, token, code, assessment=assessment, elapsed=round((monotonic() - started) * 1000))
