import json
import re
from datetime import datetime, timedelta
from time import perf_counter
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import event, select

from viracase.application.progress import STAGES
from viracase.domain.rules import aware, canonical_hash, utcnow
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import IntegrationConfig, Operation
from viracase.infrastructure.observability import WorkerBuildStatus
from viracase.infrastructure.publication import MESSAGES, PublicationService
from viracase.infrastructure.publication_check import runtime_verified
from viracase.infrastructure.publication_logging import emit, safe_text
from viracase.infrastructure.publication_maintenance import ACTIVE_CHECK_STATES, automatic_status, instant
from viracase.infrastructure.publication_qualification import qualified
from viracase.infrastructure.publication_verification import MESSAGES as FAILURE_MESSAGES
from viracase.infrastructure.publication_verification import verification_summary

STATES = {"unknown", "not_started", "queued", "running", "succeeded", "failed", "skipped", "cancelled"}
STEP_LABELS = {
    "verification_queued": "Aguardar executor",
    "verification_isolation": "Validar isolamento do executor",
    "verification_configuration": "Validar conta e credencial",
    "verification_cloudflare": "Conferir recursos de teste",
    "verification_d1": "Confirmar escrita e leitura no D1",
    "verification_building": "Preparar aplicação de teste",
    "verification_uploading": "Publicar aplicação de teste",
    "verification_bindings": "Confirmar bindings e revisão",
    "verification_routing": "Ativar endereço público",
    "verification_remote": "Validar HTTP e JSON",
    "verification_result": "Consolidar resultado",
}
PROFILE_STAGES = (
    "verification_cloudflare",
    "verification_d1",
    "verification_building",
    "verification_uploading",
    "verification_bindings",
    "verification_routing",
    "verification_remote",
)
FAILURE_SUMMARIES = {
    "PUBLICATION_API_NOT_READY": "API não respondeu como esperado",
    "PUBLICATION_FRONTEND_NOT_READY": "Frontend não respondeu como esperado",
    "PUBLICATION_REVISION_NOT_READY": "Revisão ou acesso ao banco não confirmados",
    "PUBLICATION_ACCESS_DENIED": "Endereço público recusou o acesso",
    "PUBLICATION_REDIRECTED": "Endereço público redirecionou a verificação",
    "PUBLICATION_BINDINGS_MISMATCH": "Bindings diferentes dos recursos autorizados",
    "PUBLICATION_BINDINGS_NOT_READY": "Bindings ainda não confirmados",
    "PUBLICATION_D1_PROBE_FAILED": "Leitura e escrita no banco não confirmadas",
    "EXECUTOR_UNAVAILABLE": "Executor de publicação indisponível",
    "VERSION_CONFLICT": "A configuração mudou durante a verificação",
    "VERIFICATION_RESUME_REQUIRED": "Verificação interrompida; recursos preservados",
}
PROBE_EXPECTATIONS = {
    "frontend": "HTTP 200 com conteúdo HTML",
    "revision": "HTTP 200, JSON, revisão esperada e acesso ao D1 confirmado",
    "api": "HTTP 200 com objeto JSON; indicadores de saúde não podem ser falsos",
}
SENSITIVE_KEYS = re.compile(
    r"authorization|cookie|password|secret|credential|token|private_key|(?:^|_)(?:headers|body|payload|traceback|stack_trace|raw_response)(?:$|_)",
    re.IGNORECASE,
)
IDENTIFIER_KEYS = {
    "fingerprint",
    "executor_fingerprint",
    "configuration_fingerprint",
    "executor_digest",
    "configuration_digest",
    "digest",
    "sha256",
    "evidence_sha256",
    "expected_sha256",
    "actual_sha256",
}


def timestamp(value):
    if isinstance(value, datetime):
        return aware(value).isoformat()
    parsed = instant(value)
    return parsed.isoformat() if parsed else None


def safe_url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "[URL OMITIDA]"
        return urlunsplit((parsed.scheme, parsed.hostname, parsed.path, "", ""))
    except (TypeError, ValueError):
        return "[URL OMITIDA]"


def sanitize(value, key="", depth=0):
    if depth > 12:
        return None
    if isinstance(value, dict):
        return {
            str(k): sanitize(v, str(k), depth + 1)
            for k, v in value.items()
            if isinstance(v, bool) or not SENSITIVE_KEYS.search(str(k))
        }
    if isinstance(value, list):
        return [sanitize(item, key, depth + 1) for item in value[:1000]]
    if isinstance(value, str):
        if key in IDENTIFIER_KEYS and re.fullmatch(r"[a-fA-F0-9]{64}", value):
            return value
        if key in {"url", "public_url", "origin"}:
            return safe_url(value)
        return safe_text(value, limit=2048)
    return value if isinstance(value, (bool, int, float)) or value is None else None


def phase(stage):
    if stage in {
        "verification_isolation",
        "verification_configuration",
        "verification_cloudflare",
        "verification_d1",
    }:
        return "prepare"
    if stage in {
        "verification_building",
        "verification_uploading",
        "verification_bindings",
        "verification_routing",
    }:
        return "publish"
    return "validate" if stage == "verification_remote" else "consolidate"


def probes(report, observed_at):
    if not isinstance(report, dict):
        return []
    attempts = report.get("attempts")
    last = attempts[-1] if isinstance(attempts, list) and attempts and isinstance(attempts[-1], dict) else {}
    checks = last.get("checks", [])
    if not isinstance(checks, list):
        return []
    return [
        {
            "key": safe_text(check.get("name", "unknown"), limit=80),
            "state": "succeeded"
            if check.get("passed") is True
            else "failed"
            if check.get("passed") is False
            else "unknown",
            "observed_http_status": check.get("status") if type(check.get("status")) is int else None,
            "expected_summary": PROBE_EXPECTATIONS.get(check.get("name")),
            "observed_summary": safe_text(check.get("code"), limit=120) or None,
            "error_code": safe_text(check.get("code"), limit=120)
            if check.get("passed") is not True
            else None,
            "observed_at": timestamp(check.get("observed_at")),
            "duration_ms": check.get("duration_ms") if type(check.get("duration_ms")) is int else None,
        }
        for check in checks
        if isinstance(check, dict)
    ]


def run_projection(operation, configuration, current_configuration, now):
    if operation is None:
        return None
    result = operation.result or {}
    failure = result.get("verification_failure")
    failure = failure if isinstance(failure, dict) else {}
    state = operation.state if operation.state in STATES else "unknown"
    profile_ids = configuration.settings.get("allowed_stacks", []) if configuration else []
    attempt = result.get("verification_attempt", 1)
    run_id = operation.id if attempt == 1 else f"{operation.id}:{attempt}"
    instrumented = "verification_steps" in result
    raw_events = result.get("verification_events") if instrumented else result.get("activity")
    events = []
    for index, item in enumerate(raw_events if isinstance(raw_events, list) else []):
        if not isinstance(item, dict):
            continue
        stage = item.get("stage")
        if not isinstance(stage, str):
            continue
        events.append(
            {
                "id": item.get("id") or f"{run_id}:legacy:{index + 1}",
                "order": index + 1,
                "stage": safe_text(stage, limit=80),
                "label": STEP_LABELS.get(stage, STAGES.get(stage, "Evento da verificação")),
                "state": item.get("state") if item.get("state") in STATES else "unknown",
                "profile_id": item.get("profile_id") if instrumented else None,
                "at": timestamp(item.get("at")),
                "error_code": item.get("error_code"),
            }
        )
    started = timestamp(result.get("verification_started_at"))
    if not started:
        started = next((item["at"] for item in events if item["stage"] == "verification_isolation"), None)
    finished = timestamp(result.get("verification_finished_at"))
    if not finished and operation.state in {"succeeded", "failed", "cancelled"}:
        finished = next(
            (
                timestamp(item.get("at"))
                for item in reversed(result.get("activity", []))
                if isinstance(item, dict)
                and item.get("stage")
                in {"verification_complete", "verification_partial", "failed", "cancelled"}
            ),
            None,
        )
    last_event = next((item["at"] for item in reversed(events) if item["at"]), None)
    raw_steps = result.get("verification_steps", [])
    observed = {item.get("id"): item for item in raw_steps if isinstance(item, dict)}
    plan = [(None, "verification_isolation"), (None, "verification_configuration")]
    for profile in profile_ids:
        plan.extend((profile, stage) for stage in PROFILE_STAGES)
    plan.append((None, "verification_result"))
    for item in observed.values():
        pair = (item.get("profile_id"), item.get("stage"))
        if pair not in plan and isinstance(pair[1], str):
            plan.append(pair)
    stack_results = result.get("stack_results", [])
    stack_results = stack_results if isinstance(stack_results, list) else []
    steps = []
    for index, (profile, stage) in enumerate(plan):
        identity = f"{profile or 'connection'}:{stage}"
        item = observed.get(identity, {})
        step_state = (
            item.get("state") if item.get("state") in STATES else "not_started" if instrumented else "unknown"
        )
        if not instrumented and operation.state == "queued":
            step_state = "not_started"
        if (
            not instrumented
            and stage == operation.progress_stage
            and profile == result.get("verification_stack")
        ):
            step_state = "running" if operation.state == "running" else "failed" if failure else "unknown"
        if operation.state != "running" and step_state == "running":
            step_state = "unknown"
        report = next(
            (
                entry.get("remote") or (entry.get("failure") or {}).get("details")
                for entry in stack_results
                if isinstance(entry, dict) and entry.get("stack") == profile
            ),
            None,
        )
        if profile == result.get("verification_stack"):
            report = result.get("remote_verification") or failure.get("details") or report
        at = timestamp(item.get("finished_at")) or last_event
        steps.append(
            {
                "id": identity,
                "order": index,
                "label": STEP_LABELS.get(stage, "Etapa da verificação"),
                "stage": stage,
                "phase_id": phase(stage),
                "state": step_state,
                "profile_id": profile,
                "started_at": timestamp(item.get("started_at")),
                "finished_at": timestamp(item.get("finished_at")),
                "message": (
                    FAILURE_SUMMARIES.get(item.get("error_code"), "A etapa falhou; consulte o diagnóstico.")
                    if step_state == "failed"
                    else "Recurso já confirmado; etapa não executada novamente."
                    if step_state == "skipped"
                    else None
                ),
                "probes": probes(report, at) if stage == "verification_remote" else [],
            }
        )
    code = failure.get("code") or operation.error_code
    active_profiles = sorted(
        {step["profile_id"] for step in steps if step["state"] == "running" and step["profile_id"]}
    )
    duration = None
    if started and (finished or operation.state in ACTIVE_CHECK_STATES):
        end = instant(finished) or now
        duration = max(0, int((end - instant(started)).total_seconds() * 1000))
    configuration_current = bool(
        configuration and current_configuration and configuration.id == current_configuration.id
    )
    return sanitize(
        {
            "id": run_id,
            "operation_id": operation.id,
            "configuration_id": configuration.id if configuration else operation.policy.get("config_id"),
            "configuration_version": configuration.version if configuration else None,
            "configuration_fingerprint": canonical_hash(configuration.settings) if configuration else None,
            "executor_fingerprint": result.get("verification_executor_digest"),
            "configuration_current": configuration_current,
            "state": state,
            "raw_state": getattr(operation, "interrupted_state", None) or operation.state,
            "interrupted": bool(getattr(operation, "interrupted_state", None)),
            "profile_ids": profile_ids,
            "active_profile_ids": active_profiles,
            "current_step_ids": [step["id"] for step in steps if step["state"] == "running"],
            "started_at": started,
            "finished_at": finished,
            "last_event_at": last_event,
            "duration_ms": duration,
            "duration_kind": "finished" if finished else "elapsed" if duration is not None else "unknown",
            "steps": steps,
            "error_code": code,
            "operation_error_code": operation.error_code,
            "error_summary": FAILURE_SUMMARIES.get(
                code, FAILURE_MESSAGES.get(code, "A verificação falhou; consulte o diagnóstico.")
            )
            if code
            else None,
            "failed_stage": failure.get("stage"),
            "failed_profile": failure.get("stack"),
            "correlation_id": operation.id,
            "events": events,
            "failure": failure or None,
            "stack_results": [
                {
                    **entry,
                    "state": "succeeded"
                    if entry.get("passed") is True
                    else "failed"
                    if entry.get("passed") is False
                    else "unknown",
                    "configuration_current": configuration_current,
                    "observed_at": timestamp((entry.get("failure") or {}).get("at")) or finished,
                }
                for entry in stack_results
                if isinstance(entry, dict)
            ],
            "remote_verification": result.get("remote_verification"),
            "resources": result.get("verification_resources", []),
            "cleanup": result.get("verification_cleanup"),
        }
    )


def gate(row, service, profile_ids, now, profile=None, reasons=None):
    codes = (
        service.configuration_reasons(row, profile)
        if reasons is None
        else [reason["code"] for reason in reasons]
    )
    reasons = (
        reasons
        if reasons is not None
        else [{"code": code, "message": service.configuration_message(row, code)} for code in codes]
    )
    return {
        "allowed": not codes,
        "state": "allowed" if not codes else "unavailable" if "EXECUTOR_UNAVAILABLE" in codes else "blocked",
        "reason_code": codes[0] if codes else None,
        "reason_text": reasons[0]["message"] if reasons else None,
        "reasons": reasons,
        "evaluated_at": now.isoformat(),
        "applicable_profiles": [profile] if profile else profile_ids,
    }


def evidence(row, profile, fingerprint, now):
    if not row:
        return {"evidence_run_id": None, "evidence_valid_until": None, "evidence_state": "unknown"}
    runtime = row.validation.get("runtime_verification", {}).get("report", {})
    formal = row.validation.get("qualification", {}).get("report", {})
    runtime_valid = runtime_verified(row, profile, fingerprint)
    formal_row = SimpleNamespace(
        id=row.id,
        settings=row.settings,
        validation={"qualification": row.validation.get("qualification", {})},
    )
    formal_valid = qualified(formal_row, profile, fingerprint)
    report = runtime if runtime_valid or not formal_valid and runtime else formal
    valid = qualified(row, profile, fingerprint)
    expiry = instant(report.get("expires_at"))
    operation_id, attempt = report.get("operation_id"), report.get("verification_attempt")
    expected_run_id = (
        (operation_id if attempt == 1 else f"{operation_id}:{attempt}")
        if isinstance(operation_id, str) and type(attempt) is int and attempt > 0
        else None
    )
    correlated_run_id = (
        expected_run_id if expected_run_id and report.get("run_id") == expected_run_id else None
    )
    return {
        "evidence_run_id": correlated_run_id if valid else None,
        "evidence_operation_id": operation_id,
        "evidence_correlation": "attempt" if correlated_run_id else "unknown",
        "evidence_valid_until": expiry.isoformat() if expiry else None,
        "evidence_state": "valid"
        if valid
        else "expired"
        if expiry and expiry <= now
        else "invalid"
        if report
        else "not_started",
        "evidence_source": "operational"
        if report is runtime and report
        else "qualification"
        if report
        else None,
    }


def profile_verification(run, identity):
    if not run or not run["configuration_current"]:
        return "not_started"
    results = [entry for entry in run["stack_results"] if entry.get("stack") == identity]
    if results:
        return results[-1]["state"]
    if identity in run["active_profile_ids"]:
        return "running"
    if run["state"] == "queued":
        return "queued"
    if run["state"] == "cancelled":
        return "cancelled"
    if run["state"] == "running":
        return "not_started"
    return "failed" if run["failed_profile"] == identity else "unknown"


def management_snapshot(repo, db, registry, row, connection, executor, service, legacy, now):
    query = select(Operation).where(
        Operation.type == "publication_check", Operation.payload["connection_id"].as_string() == connection.id
    )
    active = db.scalar(
        query.where(Operation.state.in_(ACTIVE_CHECK_STATES))
        .order_by(Operation.created_at.desc(), Operation.id.desc())
        .limit(1)
    )
    previous = db.scalar(
        query.where(Operation.state.in_({"succeeded", "failed", "cancelled"}))
        .order_by(Operation.updated_at.desc(), Operation.id.desc())
        .limit(1)
    )
    if active:
        history = (active.result or {}).get("verification_attempt_history", [])
        if history:
            item = history[-1]
            at = instant(item.get("updated_at"))
            if not previous or at and at > aware(previous.updated_at):
                previous = SimpleNamespace(
                    id=active.id,
                    policy=active.policy,
                    result=item.get("result", {}),
                    state=item.get("state", "unknown"),
                    interrupted_state=item.get("interrupted_state"),
                    progress_stage=item.get("progress_stage"),
                    error_code=item.get("error_code"),
                    created_at=active.created_at,
                    updated_at=at,
                )
    configurations = {}
    for operation in (active, previous):
        identity = operation.policy.get("config_id") if operation else None
        if identity and identity not in configurations:
            configurations[identity] = db.get(IntegrationConfig, identity)
    current_run = run_projection(
        active, configurations.get(active.policy.get("config_id")) if active else None, row, now
    )
    previous_result = run_projection(
        previous, configurations.get(previous.policy.get("config_id")) if previous else None, row, now
    )
    profile_ids = list(row.settings.get("allowed_stacks", [])) if row else []
    publication = gate(row, service, profile_ids, now, reasons=legacy["reasons"])
    readiness = {item["stack"]: item["reasons"] for item in legacy["stack_readiness"]}
    profiles = [
        {
            "id": identity,
            "label": identity,
            "publication": gate(row, service, profile_ids, now, identity, readiness[identity]),
            "verification_state": profile_verification(current_run or previous_result, identity),
            **evidence(row, identity, executor.fingerprint(), now),
        }
        for identity in profile_ids
    ]
    if publication["allowed"] and any(not profile["publication"]["allowed"] for profile in profiles):
        publication["state"] = "partial"
        publication["reason_text"] = "Publicação disponível somente para os perfis aprovados."
    validation = row.validation if row else {}
    credential_expiry = (
        aware(row.validated_at) + timedelta(hours=row.settings["validation_hours"])
        if row and row.validated_at
        else None
    )
    credential_state = (
        "unknown"
        if not row or not row.validated_at
        else "expired"
        if credential_expiry <= now
        else "valid"
        if validation.get("ready") is True
        else "invalid"
    )
    formal = validation.get("qualification", {}).get("report", {})
    formal_copy = (
        SimpleNamespace(
            id=row.id,
            settings=row.settings,
            validation={"qualification": validation.get("qualification", {})},
        )
        if row
        else None
    )
    formal_valid = bool(formal_copy and qualified(formal_copy, None, executor.fingerprint()))
    formal_expiry = instant(formal.get("expires_at"))
    maintenance = validation.get("automatic_verification", {})
    scheduled_at = instant(maintenance.get("next_attempt_at"))
    retry_scheduled = bool(
        not active
        and scheduled_at
        and maintenance.get("state") in {"retry_wait", "scheduled"}
        and legacy["automatic_verification"]["state"] in {"retry_wait", "scheduled"}
    )
    count = maintenance.get("consecutive_failures")
    retry = {
        "scheduled": retry_scheduled,
        "next_attempt_at": scheduled_at.isoformat() if retry_scheduled else None,
        "failure_count": count if type(count) is int and count >= 0 else None,
        "count_scope": "consecutive_failures",
        "count_label": "Falhas consecutivas da verificação automática desta configuração",
        "reason_code": maintenance.get("last_error_code"),
        "reason_text": legacy["automatic_verification"].get("message"),
        "state": "running"
        if active and active.state == "running"
        else "queued"
        if active and active.state == "queued"
        else legacy["automatic_verification"]["state"],
        "overdue": bool(retry_scheduled and scheduled_at < now),
    }
    resources = (current_run or previous_result or {}).get("resources") or []
    source = active or previous
    retained = (source.result or {}).get("resource_policy") == "retain" if source else None
    any_active = db.scalar(
        select(Operation.id)
        .where(Operation.type == "publication_check", Operation.state.in_(ACTIVE_CHECK_STATES))
        .limit(1)
    )
    can_verify = legacy["can_verify"] and not any_active
    verification_reason = (
        "Já existe uma verificação em execução ou aguardando retomada."
        if any_active
        else legacy["verification_reasons"][0]["message"]
        if legacy["verification_reasons"]
        else None
    )
    can_resume = bool(active and active.state == "reconciling")
    polling = bool(active or retry_scheduled)
    account = row.settings.get("account_id", "") if row else ""
    snapshot = {
        "schema_version": 1,
        "revision_order": "request_generation",
        "observed_at": now.isoformat(),
        "server_time": now.isoformat(),
        "connection": {
            "id": connection.id,
            "name": safe_text(connection.name, limit=80),
            "active": bool(row and row.state == "active"),
            "state": row.state if row else "missing",
            "is_default": connection.default_for == "cloudflare",
            "environment": row.settings.get("environment") if row else None,
            "account_masked": account[:4] + "…" + account[-4:] if len(account) > 8 else None,
            "configuration_id": row.id if row else None,
            "configuration_version": row.version if row else None,
            "configuration_fingerprint": canonical_hash(row.settings) if row else None,
            "executor_fingerprint": executor.fingerprint(),
        },
        "credential": {
            "state": credential_state,
            "type": "credential_and_account",
            "checked_at": timestamp(row.validated_at) if row else None,
            "valid_until": timestamp(credential_expiry),
            "reason_code": validation.get("code") if credential_state == "invalid" else None,
        },
        "qualification": {
            "state": "valid"
            if formal_valid
            else "expired"
            if formal_expiry and formal_expiry <= now
            else "invalid"
            if formal
            else "not_started",
            "checked_at": timestamp(formal.get("tested_at")),
            "valid_until": timestamp(formal_expiry),
            "reason_code": "QUALIFICATION_INVALID" if formal and not formal_valid else None,
        },
        "publication": publication,
        "current_run": current_run,
        "previous_result": previous_result,
        "profiles": profiles,
        "retry": retry,
        "retention": {
            "enabled": retained,
            "resource_count": len(resources) if source else None,
            "summary": "Retenção habilitada. Os recursos de teste são preservados, inclusive em caso de falha."
            if retained
            else "Consulte a política de retenção nos detalhes da verificação.",
        },
        "actions": [
            {
                "key": "refresh",
                "label": "Atualizar estado",
                "enabled": True,
                "disabled_reason": None,
                "kind": "read",
            },
            {
                "key": "verify",
                "label": "Verificar publicação agora",
                "enabled": can_verify,
                "disabled_reason": verification_reason,
                "kind": "command",
            },
            {
                "key": "resume",
                "label": "Retomar verificação pendente",
                "enabled": can_resume,
                "disabled_reason": None if can_resume else "Não há verificação pendente para retomar.",
                "kind": "command",
            },
        ],
        "polling": {"active": polling, "interval_ms": 3000 if active else 15000 if retry_scheduled else None},
        "poll_after_ms": 3000 if active else 15000 if retry_scheduled else None,
    }
    revision_source = {
        key: value for key, value in snapshot.items() if key not in {"observed_at", "server_time"}
    }
    snapshot["revision"] = canonical_hash(revision_source)
    return snapshot


def publication_diagnostics(repo, actor, identity):
    started, query_count = perf_counter(), [0]
    registry = Integrations(repo)
    with repo.sessions() as db:
        options = (
            {"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
            if db.get_bind().dialect.name == "postgresql"
            else {}
        )
        connection = db.connection(execution_options=options)
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN")

        def counted(*args):
            query_count[0] += 1

        event.listen(connection, "before_cursor_execute", counted)
        try:
            registry.require_admin(repo.account(db, actor))
            executor = WorkerBuildStatus(repo, db)
            service = PublicationService(repo, executor)
            row = registry.latest(db, "cloudflare", connection_id=identity)
            integration_connection = registry.connection(db, "cloudflare", identity)
            codes = service.configuration_reasons(row)
            verification_codes = []
            if (
                not row
                or row.state not in {"validated", "active"}
                or not registry.fresh(row)
                or not row.validation.get("ready")
            ):
                verification_codes.append("CONFIGURATION_INVALID")
            if not executor.available() or not executor.fingerprint():
                verification_codes.append("EXECUTOR_UNAVAILABLE")
            if repo.config.restore_hold or not repo.config.lifecycle_enabled:
                verification_codes.append("LIFECYCLE_LOCKED")
            latest = db.scalar(
                select(Operation)
                .where(
                    Operation.type == "publication_check",
                    Operation.payload["connection_id"].as_string() == identity,
                )
                .order_by(Operation.created_at.desc())
                .limit(1)
            )
            stack_readiness = []
            for stack in row.settings["allowed_stacks"] if row else []:
                stack_codes = service.configuration_reasons(row, stack)
                stack_readiness.append(
                    {
                        "stack": stack,
                        "can_publish": not stack_codes,
                        "reasons": [
                            {"code": code, "message": service.configuration_message(row, code)}
                            for code in stack_codes
                        ],
                    }
                )
            response = {
                "can_publish": not codes,
                "stack_readiness": stack_readiness,
                "can_verify": not verification_codes,
                "verification_reasons": [
                    {"code": code, "message": MESSAGES[code]} for code in verification_codes
                ],
                "workers": sanitize(executor.workers),
                "reasons": [
                    {"code": code, "message": service.configuration_message(row, code)} for code in codes
                ],
                "verification": sanitize(verification_summary(latest)),
                "automatic_verification": automatic_status(repo, row, executor),
            }
            response["snapshot"] = management_snapshot(
                repo, db, registry, row, integration_connection, executor, service, response, utcnow()
            )
        finally:
            event.remove(connection, "before_cursor_execute", counted)
    emit(
        "publication_management_snapshot_read",
        connection_id=identity,
        configuration_version=response["snapshot"]["connection"]["configuration_version"],
        run_id=(response["snapshot"]["current_run"] or {}).get("id"),
        outcome="observed",
        duration_ms=round((perf_counter() - started) * 1000),
        database_queries=query_count[0],
        payload_bytes=len(json.dumps(response, ensure_ascii=False).encode()),
        remote_calls=0,
    )
    return response
