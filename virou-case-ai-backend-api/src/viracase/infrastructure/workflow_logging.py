import json
import logging
import math
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import PurePath
from threading import Lock

from sqlalchemy.exc import SQLAlchemyError

_context = ContextVar("workflow_diagnostic_context", default=None)
_lock = Lock()
_log = logging.getLogger("viracase.workflow")
_names = {
    "request_id",
    "http_request_id",
    "workflow_request_id",
    "trace_id",
    "correlation_id",
    "operation_id",
    "operation_type",
    "preparation_id",
    "schedule_id",
    "parent_preparation_id",
    "account_id",
    "project_id",
    "project_revision",
    "context_revision",
    "connection_id",
    "attachment_id",
    "source_version_id",
    "field_id",
    "provider",
    "model",
    "policy_version",
    "phase",
    "stage",
    "direction",
    "state",
    "status",
    "billing_status",
    "capture_state",
    "capture_reason",
    "reason_code",
    "retry_reason",
    "unsupported_field",
    "provider_request_id",
    "method",
    "audience",
    "source_type",
    "transaction_state",
    "metadata_state",
    "finish_reason",
    "response_status",
    "thinking_mode",
    "thinking_level",
    "reasoning_effort",
    "effective_thinking_mode",
    "effective_thinking_level",
    "effective_reasoning_effort",
}
_numbers = {
    "http_status",
    "duration_ms",
    "attempt",
    "draft_revision",
    "attachment_revision",
    "schema_version",
    "selected_count",
    "attachment_count",
    "field_count",
    "context_bytes",
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "max_output_tokens",
    "reserved_input_bound",
    "applied_count",
    "suggested_count",
    "skipped_count",
    "pending_count",
    "invalid_count",
    "question_count",
    "dispatch_count",
    "retry_count",
    "status_code",
    "sequence",
    "refs_count",
    "evidence_length",
    "max_length",
    "batch",
    "max_batches",
    "provider_calls",
    "accepted_batches",
    "repair_attempts",
    "truncation_retries",
    "files_count",
    "changed_count",
    "remaining_input_budget",
    "total_input_budget",
    "request_context_bytes",
    "request_context_limit_bytes",
    "generation_context_limit_bytes",
    "generation_context_version",
    "construction_policy_version",
    "max_truncation_retries",
    "max_response_repairs",
    "batch_character_budget",
    "max_batch_characters",
    "minimum_batch_characters",
    "batch_shrink_factor",
    "request_timeout_seconds",
    "connect_timeout_seconds",
    "thinking_budget",
    "effective_thinking_budget",
    "temperature",
    "top_p",
    "reasoning_tokens",
    "visible_output_tokens",
    "output_characters",
    "provider_dispatches",
}
_booleans = {"usage_known", "retryable", "automatic_apply", "cancel_requested", "payload_captured"}
_money = {"actual_brl", "reserved_brl"}
_enums = {
    "expected_type": {"text", "boolean", "enum", "string_list"},
    "received_type": {"string", "boolean", "array", "object", "number", "null", "missing", "unknown"},
    "value_encoding": {"json_string", "native", "source_selection"},
    "preparation_state": {"queued", "running", "needs_input", "ready", "failed", "cancelled", "reconciling"},
}


def _atom(value, secrets=(), limit=120):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1," + str(limit) + "}", value):
        return None
    if any(secret and secret in value for secret in secrets if isinstance(secret, str)):
        return None
    return value


def _code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,95}", value) else None


def safe_fields(values, secrets=()):
    result = {}
    for name in _names:
        value = _atom(values.get(name), secrets)
        if value is not None:
            result[name] = value
    for name, choices in _enums.items():
        value = _atom(values.get(name), secrets)
        if value in choices:
            result[name] = value
    code = _code(values.get("error_code"))
    if code and not any(secret and secret in code for secret in secrets if isinstance(secret, str)):
        result["error_code"] = code
    for name in _numbers:
        value = values.get(name)
        minimum = -1 if name in {"thinking_budget", "effective_thinking_budget"} else 0
        if type(value) in {int, float} and minimum <= value <= 10**15 and math.isfinite(value):
            result[name] = value
    for name in _booleans:
        if type(values.get(name)) is bool:
            result[name] = values[name]
    for name in _money:
        value = values.get(name)
        if isinstance(value, Decimal) and value.is_finite() and 0 <= value <= 10**12:
            result[name] = format(value, "f")
    route = values.get("route")
    if isinstance(route, str) and len(route) <= 240 and re.fullmatch(r"/[A-Za-z0-9_/{}.:\-]*", route):
        result["route"] = route
    versions = values.get("template_versions")
    if isinstance(versions, (list, tuple)):
        result["template_versions"] = []
        for item in versions[:64]:
            if not isinstance(item, dict):
                continue
            clean = {
                name: value
                for name in ("attachment_id", "source_version_id")
                if (value := _atom(item.get(name), secrets)) is not None
            }
            revision = item.get("attachment_revision")
            if type(revision) is int and 0 <= revision <= 10**15:
                clean["attachment_revision"] = revision
            if clean:
                result["template_versions"].append(clean)
    return result


def _exception(exc, secrets=()):
    frames = []
    current = exc.__traceback__
    while current:
        code = current.tb_frame.f_code
        filename = _atom(PurePath(code.co_filename).name, secrets, limit=100) or "external"
        function = _atom(code.co_name, secrets, limit=100) or "anonymous"
        frames.append({"file": filename, "line": current.tb_lineno, "function": function})
        current = current.tb_next
    return {
        "exception_type": _atom(type(exc).__name__, secrets, limit=100) or "Exception",
        "frames": frames[-8:],
    }


def exception_fields(exc, secrets=()):
    if not isinstance(exc, BaseException):
        return {}
    result = _exception(exc, secrets)
    code = _code(getattr(exc, "code", None)) or _code(getattr(exc, "error_code", None))
    result["error_code"] = safe_fields({"error_code": code}, secrets).get("error_code", "UNEXPECTED_ERROR")
    diagnostic = getattr(exc, "diagnostic", None)
    if isinstance(diagnostic, dict):
        result.update(
            safe_fields(
                {
                    name: diagnostic.get(name)
                    for name in (
                        "http_status",
                        "reason_code",
                        "unsupported_field",
                        "expected_type",
                        "received_type",
                        "value_encoding",
                        "refs_count",
                        "evidence_length",
                        "max_length",
                    )
                },
                secrets,
            )
        )
    causes, seen = [], {id(exc)}
    current = exc.__cause__ or exc.__context__
    while isinstance(current, BaseException) and id(current) not in seen and len(causes) < 3:
        seen.add(id(current))
        causes.append(_exception(current, secrets))
        current = current.__cause__ or current.__context__
    if causes:
        result["causes"] = causes
    return result


def _operation_fields(operation):
    if operation is None:
        return {}
    payload = getattr(operation, "payload", None)
    policy = getattr(operation, "policy", None)
    payload = payload if isinstance(payload, dict) else {}
    policy = policy if isinstance(policy, dict) else {}
    versions = []
    items = payload.get("items", [])
    for item in (items if isinstance(items, (list, tuple)) else [])[:64]:
        if isinstance(item, dict):
            versions.append(
                {
                    key: item.get(key)
                    for key in (
                        "attachment_id",
                        "source_version_id",
                        "attachment_revision",
                    )
                }
            )
    snapshots = payload.get("attachment_snapshots", [])
    for item in (snapshots if isinstance(snapshots, (list, tuple)) else [])[:64]:
        if isinstance(item, dict):
            versions.append(
                {
                    "attachment_id": item.get("source_template_id"),
                    "source_version_id": item.get("source_version_id"),
                    "attachment_revision": item.get("attachment_revision"),
                }
            )
    return safe_fields(
        {
            "operation_id": getattr(operation, "id", None),
            "operation_type": getattr(operation, "type", None),
            "account_id": getattr(operation, "account_id", None),
            "project_id": getattr(operation, "project_id", None),
            "state": getattr(operation, "state", None),
            "billing_status": getattr(operation, "billing_status", None),
            "request_id": payload.get("http_request_id"),
            "workflow_request_id": payload.get("request_id"),
            "trace_id": payload.get("correlation_id") or getattr(operation, "id", None),
            "schedule_id": payload.get("autofill_schedule_id"),
            "preparation_id": payload.get("preparation_id"),
            "parent_preparation_id": payload.get("parent_preparation_id"),
            "draft_revision": payload.get("draft_revision"),
            "project_revision": payload.get("base_revision_id"),
            "provider": policy.get("provider"),
            "model": policy.get("model"),
            "connection_id": policy.get("connection_id"),
            "policy_version": policy.get("version"),
            "template_versions": versions,
            "automatic_apply": payload.get("automatic_apply"),
        }
    )


def operation_fields(operation):
    try:
        return _operation_fields(operation)
    except (AttributeError, TypeError, ValueError, RuntimeError, SQLAlchemyError):
        return {"metadata_state": "unavailable"}


def configure_workflow_logging():
    with _lock:
        if not _log.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            _log.addHandler(handler)
        _log.setLevel(logging.INFO)
        _log.propagate = False


def current_context():
    return dict(_context.get() or {})


@contextmanager
def workflow_context(**fields):
    token = _context.set({**current_context(), **safe_fields(fields)})
    try:
        yield
    finally:
        _context.reset(token)


def emit(event, level="info", *, operation=None, error=None, secrets=(), **fields):
    configure_workflow_logging()
    level = level if level in {"debug", "info", "warning", "error", "critical"} else "info"
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "service": "workflow",
        "level": level,
        "event": _atom(event, limit=96) or "workflow_diagnostic",
        **safe_fields(current_context(), secrets),
        **safe_fields(operation_fields(operation), secrets),
        **exception_fields(error, secrets),
        **safe_fields(fields, secrets),
    }
    record["correlation_id"] = (
        record.get("trace_id") or record.get("operation_id") or record.get("request_id")
    )
    try:
        _log.log(getattr(logging, level.upper()), json.dumps(record, ensure_ascii=False))
    except (OSError, ValueError, TypeError):
        return
