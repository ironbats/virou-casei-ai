import json
import math
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from sqlalchemy import and_, delete, func, or_, select

from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.feature_policies import FeaturePolicies
from viracase.infrastructure.models import (
    Account,
    Audit,
    Idempotency,
    IntegrationTrace,
    IntegrationTracePayload,
    Operation,
    Project,
    new_id,
)
from viracase.infrastructure.repository_dto import iso
from viracase.infrastructure.workflow_logging import emit, safe_fields

PROTECTED = "[CONTEÚDO PROTEGIDO]"
TRUNCATED = "[CONTEÚDO TRUNCADO]"
SECTIONS = {"request", "response", "normalized", "fields"}
PHASES = {"recommendation", "preparation", "review", "construction"}
OBSERVER = ContextVar("integration_trace_observer", default=None)
SENSITIVE = {
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "secret",
    "credential",
    "credentials",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "x-goog-api-key",
    "email",
    "phone",
    "address",
    "objective",
    "prompt",
    "input",
    "instructions",
    "text",
    "content",
    "message",
    "question",
    "answer",
    "value",
    "original_value",
    "suggested_value",
    "edited_value",
    "final_value",
    "evidence",
    "description",
    "reason",
    "summary",
    "name",
}
STRUCTURAL = {
    "method",
    "path",
    "headers",
    "body",
    "model",
    "max_output_tokens",
    "store",
    "service_tier",
    "format",
    "type",
    "strict",
    "schema",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "anyOf",
    "oneOf",
    "nullable",
    "maxLength",
    "minLength",
    "maxItems",
    "minItems",
    "contents",
    "role",
    "parts",
    "generationConfig",
    "responseMimeType",
    "maxOutputTokens",
    "candidateCount",
    "responseSchema",
    "propertyOrdering",
    "thinkingConfig",
    "thinkingLevel",
    "state",
    "questions",
    "criteria",
    "answers",
    "choices",
    "choice",
    "confidence",
    "id",
    "status",
    "usage",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "input_tokens_details",
    "output_tokens_details",
    "cached_tokens",
    "reasoning_tokens",
    "input",
    "cached",
    "output",
    "calls",
    "usageMetadata",
    "promptTokenCount",
    "cachedContentTokenCount",
    "candidatesTokenCount",
    "thoughtsTokenCount",
    "totalTokenCount",
    "candidates",
    "finishReason",
    "finish_reason",
    "responseId",
    "modelVersion",
    "incomplete_details",
    "error",
    "code",
    "output_error",
    "refusal",
    "index",
    "stop_reason",
    "promptFeedback",
    "blockReason",
    "attachment_id",
    "field_id",
    "field_ids",
    "source_type",
    "source_refs",
    "suggestion_version",
    "attachment_revision",
    "source_version_id",
    "snapshot_id",
    "draft_revision",
    "preparation_id",
    "suggested_fields",
    "errors",
    "completed",
    "total",
    "valid",
    "fallback",
    "selected",
    "compatible",
    "applied",
    "validation",
    "message_id",
    "proposal_id",
    "changed_files",
    "content-type",
}
ENUMS = {
    "POST",
    "GET",
    "application/json",
    "default",
    "json_object",
    "json_schema",
    "object",
    "array",
    "string",
    "number",
    "integer",
    "boolean",
    "null",
    "OBJECT",
    "ARRAY",
    "STRING",
    "NUMBER",
    "INTEGER",
    "BOOLEAN",
    "NULL",
    "user",
    "assistant",
    "model",
    "system",
    "message",
    "output_text",
    "completed",
    "incomplete",
    "failed",
    "cancelled",
    "ready",
    "needs_input",
    "suggested",
    "applied",
    "user_edited",
    "conflict",
    "snapshot",
    "valid",
    "invalid",
    "request",
    "project_context",
    "user_answer",
    "policy_suggestion",
    "user_edit",
    "empty",
    "STOP",
    "MAX_TOKENS",
    "SAFETY",
    "LOW",
    "max_output_tokens",
    "content_filter",
    "clarification",
    "proposal",
    "implement",
    "clarify",
    "true",
    "false",
}
NUMBER_KEYS = {
    "max_output_tokens",
    "maxOutputTokens",
    "candidateCount",
    "maxLength",
    "minLength",
    "maxItems",
    "minItems",
    "confidence",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "input",
    "cached",
    "output",
    "promptTokenCount",
    "cachedContentTokenCount",
    "candidatesTokenCount",
    "thoughtsTokenCount",
    "totalTokenCount",
    "index",
    "suggestion_version",
    "attachment_revision",
    "draft_revision",
    "completed",
    "total",
}
ERROR_STATES = {"failed", "uncertain", "invalid", "rejected"}


def sanitized(value, categories, identifiers=()):
    redacted, truncated = [], []
    identifiers = set(identifiers)
    nodes = 0

    def clean(item, path, key=None, depth=0, schema=False, usage=False):
        nonlocal nodes
        nodes += 1
        if depth > 16 or nodes > 2500:
            truncated.append(path)
            return TRUNCATED
        if (
            key
            and not (schema and isinstance(item, dict))
            and (
                key.lower() in SENSITIVE
                or any(
                    part in key.lower()
                    for part in ("password", "secret", "authorization", "cookie", "credential")
                )
            )
        ):
            if item is None:
                return None
            if (
                key == "input"
                and isinstance(item, (int, float))
                and usage
                and "usage" in categories
                and math.isfinite(item)
            ):
                return item
            if key in {"text", "content"} and isinstance(item, (dict, list)):
                pass
            else:
                redacted.append(path)
                return PROTECTED
        if isinstance(item, dict):
            result = {}
            for index, (name, child) in enumerate(item.items()):
                allowed = name in STRUCTURAL or name.lower() in SENSITIVE or name in identifiers
                safe_name = name if allowed else "protected_key_" + str(index + 1)
                childpath = path + "." + safe_name
                if not allowed:
                    redacted.append(childpath)
                    result[safe_name] = PROTECTED
                else:
                    result[safe_name] = clean(
                        child,
                        childpath,
                        name,
                        depth + 1,
                        schema or name in {"schema", "responseSchema"},
                        usage or name in {"usage", "usageMetadata"},
                    )
            return result
        if isinstance(item, list):
            if len(item) > 100:
                truncated.append(path)
            return [
                clean(
                    child,
                    path + "[" + str(index) + "]",
                    key if key in {"required", "enum", "propertyOrdering"} else None,
                    depth + 1,
                    schema,
                    usage,
                )
                for index, child in enumerate(item[:100])
            ]
        if item is None or type(item) is bool:
            return item
        if type(item) in {int, float}:
            if (
                key in NUMBER_KEYS
                and math.isfinite(item)
                and (
                    usage
                    and "usage" in categories
                    or schema
                    and "schemas" in categories
                    or not usage
                    and not schema
                    and "technical_metadata" in categories
                )
            ):
                return item
        if isinstance(item, str):
            if "field_identifiers" in categories and item in identifiers:
                return item
            if "technical_metadata" in categories and item in ENUMS:
                return item
            if "schemas" in categories and schema and item in ENUMS:
                return item
        redacted.append(path)
        return PROTECTED

    payload = clean(value, "$", depth=0)
    return {
        "payload": payload,
        "redacted_paths": sorted(set(redacted))[:200],
        "truncated_paths": sorted(set(truncated))[:200],
    }


def bounded_payload(value, limit):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) <= limit:
        return value, len(encoded)
    output = {
        "payload": {"capture": TRUNCATED},
        "redacted_paths": value["redacted_paths"][:8],
        "truncated_paths": ["$"],
    }
    while len(json.dumps(output, ensure_ascii=False).encode()) > limit and output["redacted_paths"]:
        output["redacted_paths"].pop()
    return output, len(json.dumps(output, ensure_ascii=False).encode())


class TraceStore:
    def __init__(self, repo):
        self.repo = repo
        self.policies = FeaturePolicies(repo)

    def log_stage(self, context, direction, status, capture_state, details=None):
        metadata = dict(context)
        metadata.update({
            key: value for key, value in (details or {}).items()
            if key in {
                "error_code", "http_status", "duration_ms", "retry_reason", "reason_code",
                "capture_reason", "provider_request_id",
            }
        })
        if metadata.get("capture_reason") == "RETENTION_PURGED":
            metadata.pop("account_id", None)
            metadata.pop("project_id", None)
        metadata.update(
            phase="provider" if direction == "exchange" else "parse" if status == "invalid" else context["stage"],
            direction=direction,
            status=status,
            capture_state=capture_state,
            transaction_state="pending_commit",
        )
        emit(
            "integration_stage",
            "warning" if status in {"invalid", "rejected", "uncertain", "failed"} else "info",
            secrets=self.repo.sources.secrets,
            **safe_fields(metadata, self.repo.sources.secrets),
        )

    def prune_db(self, db):
        settings = self.policies.read(db, "integration_capture")["settings"]
        now, cutoff = utcnow(), utcnow() - timedelta(days=settings["retention_days"])
        ids = select(IntegrationTrace.id).where(IntegrationTrace.created_at < cutoff)
        db.execute(
            delete(IntegrationTracePayload).where(
                or_(IntegrationTracePayload.expires_at <= now, IntegrationTracePayload.trace_id.in_(ids))
            )
        )
        rows = db.scalars(
            select(IntegrationTrace).where(
                or_(IntegrationTrace.expires_at <= now, IntegrationTrace.created_at < cutoff),
                IntegrationTrace.capture_state.in_(["captured", "protected", "truncated"]),
            )
        ).all()
        for row in rows:
            if row.capture_state in {"captured", "protected", "truncated"}:
                row.capture_state = "expired"
        total = db.scalar(select(func.coalesce(func.sum(IntegrationTracePayload.size_bytes), 0)))
        if total > settings["max_total_bytes"]:
            for row in db.scalars(
                select(IntegrationTracePayload).order_by(
                    IntegrationTracePayload.expires_at,
                    IntegrationTracePayload.trace_id,
                    IntegrationTracePayload.section,
                )
            ):
                if total <= settings["max_total_bytes"]:
                    break
                total -= row.size_bytes
                parent = db.get(IntegrationTrace, row.trace_id)
                parent.capture_state = "unavailable"
                db.delete(row)

    def prune(self):
        with self.repo.sessions.begin() as db:
            configuration_lock(db, "trace-payload-quota")
            self.prune_db(db)

    def put(self, context, direction, status, sections=None, details=None, identity=None, db=None):
        if db is None:
            with self.repo.sessions.begin() as session:
                return self.put(context, direction, status, sections, details, identity, session)
        configuration_lock(db, "trace-payload-quota")
        self.prune_db(db)
        policy = self.policies.read(db, "integration_capture")
        settings = policy["settings"]
        row = db.get(IntegrationTrace, identity) if identity else None
        if not row:
            parent = db.scalar(
                select(IntegrationTrace)
                .where(IntegrationTrace.trace_id == context["trace_id"])
                .order_by(IntegrationTrace.sequence.desc())
                .limit(1)
            )
            row = IntegrationTrace(
                id=identity or new_id(),
                trace_id=context["trace_id"],
                parent_span_id=context.get("parent_span_id") or (parent.id if parent else None),
                sequence=(parent.sequence if parent else 0) + 1,
                operation_id=context.get("operation_id"),
                account_id=context.get("account_id"),
                project_id=context.get("project_id"),
                service="jev" if context["stage"] == "recommendation" else "agents",
                stage=context["stage"],
                direction=direction,
                status=status,
                capture_state="disabled",
                details={},
                expires_at=utcnow() + timedelta(days=settings["retention_days"]),
            )
            db.add(row)
            db.flush()
        row.status = status
        account = db.get(Account, context.get("account_id")) if context.get("account_id") else None
        project = db.get(Project, context.get("project_id")) if context.get("project_id") else None
        if (
            row.details.get("capture_reason") == "RETENTION_PURGED"
            or not account
            or not project
            or project.account_id != account.id
            or project.state in {"deleting", "deleted"}
            or "read" not in self.repo.access(db, account)["allowed_actions"]
        ):
            db.execute(delete(IntegrationTracePayload).where(IntegrationTracePayload.trace_id == row.id))
            row.capture_state, row.project_id, row.account_id, row.details = (
                "unavailable",
                None,
                None,
                {"capture_reason": "RETENTION_PURGED"},
            )
            self.log_stage(context, direction, status, row.capture_state, row.details)
            return row.id
        dispatches = (
            db.scalar(
                select(func.count())
                .select_from(IntegrationTrace)
                .where(
                    IntegrationTrace.operation_id == row.operation_id,
                    IntegrationTrace.direction == "exchange",
                )
            )
            if row.operation_id
            else 0
        )
        safe_details = {
            key: context[key]
            for key in (
                "provider",
                "model",
                "connection_id",
                "attempt",
                "draft_revision",
                "preparation_id",
                "snapshot_id",
                "project_revision",
                "schema_version",
                "template_versions",
                "request_id",
                "workflow_request_id",
            )
            if context.get(key) is not None
        }
        safe_details.pop("attempt", None)
        if dispatches:
            safe_details["attempt"] = (
                row.details.get("attempt", dispatches) if direction == "exchange" else dispatches
            )
        previous_section_states = dict(row.details.get("section_states", {}))
        row.details = {**row.details, **safe_details, **(details or {})}
        if not settings["capture_enabled"]:
            row.capture_state = "disabled"
            self.log_stage(context, direction, status, row.capture_state, row.details)
            return row.id
        identifiers = list(context.get("identifiers", [])) + [
            value for key, value in safe_details.items() if isinstance(value, str)
        ]
        if details and details.get("error_code"):
            identifiers.append(details["error_code"])
        section_states = previous_section_states
        section_states.update((details or {}).get("section_states", {}))
        for section, raw in (sections or {}).items():
            if section not in SECTIONS:
                continue
            value, size = bounded_payload(
                sanitized(raw, settings["allowed_categories"], identifiers), settings["max_payload_bytes"]
            )
            existing = db.get(IntegrationTracePayload, (row.id, section))
            if existing:
                db.delete(existing)
                db.flush()
            operation_ids = (
                select(IntegrationTrace.id).where(IntegrationTrace.operation_id == row.operation_id)
                if row.operation_id
                else select(IntegrationTrace.id).where(IntegrationTrace.trace_id == row.trace_id)
            )
            used = db.scalar(
                select(func.coalesce(func.sum(IntegrationTracePayload.size_bytes), 0)).where(
                    IntegrationTracePayload.trace_id.in_(operation_ids)
                )
            )
            total = db.scalar(select(func.coalesce(func.sum(IntegrationTracePayload.size_bytes), 0)))
            if used + size > settings["max_operation_bytes"] or total + size > settings["max_total_bytes"]:
                section_states[section] = "unavailable"
                row.details = {**row.details, "capture_reason": "CAPTURE_QUOTA_EXCEEDED"}
                continue
            db.add(
                IntegrationTracePayload(
                    trace_id=row.id,
                    section=section,
                    content=value,
                    size_bytes=size,
                    policy_version=policy["version"],
                    expires_at=row.expires_at,
                )
            )
            section_states[section] = (
                "truncated"
                if value["truncated_paths"]
                else "protected"
                if value["redacted_paths"]
                else "captured"
            )
            db.flush()
        contents = [
            value.content
            for value in db.scalars(
                select(IntegrationTracePayload).where(IntegrationTracePayload.trace_id == row.id)
            )
        ]
        redacted = any(value["redacted_paths"] for value in contents)
        truncated = any(value["truncated_paths"] for value in contents)
        row.capture_state = (
            "truncated"
            if truncated
            else "protected"
            if redacted
            else "captured"
            if contents
            else "unavailable"
            if "unavailable" in section_states.values()
            else "not_captured"
        )
        row.details = {
            **row.details,
            "section_states": section_states,
            "redacted": redacted,
            "truncated": truncated,
        }
        self.log_stage(context, direction, status, row.capture_state, row.details)
        return row.id

    def purge_project(self, db, project_id=None, account_id=None):
        configuration_lock(db, "trace-payload-quota")
        query = (
            select(IntegrationTrace).where(IntegrationTrace.project_id == project_id)
            if project_id
            else select(IntegrationTrace).where(IntegrationTrace.account_id == account_id)
        )
        rows = db.scalars(query).all()
        if rows:
            db.execute(
                delete(IntegrationTracePayload).where(
                    IntegrationTracePayload.trace_id.in_([row.id for row in rows])
                )
            )
        for row in rows:
            row.capture_state, row.project_id, row.account_id, row.details = (
                "unavailable",
                None,
                None,
                {"capture_reason": "RETENTION_PURGED"},
            )
        operations = db.scalars(
            select(Operation).where(Operation.project_id == project_id)
            if project_id
            else select(Operation).where(Operation.account_id == account_id)
        ).all()
        commands = {"preparation-apply:" + operation.id for operation in operations} | {
            "preparation-answers:" + operation.id for operation in operations
        }
        if commands:
            for intent in db.scalars(select(Idempotency).where(Idempotency.command.in_(commands))):
                intent.result = {"expired": True}

    def metadata(self, db, row, sections=None):
        sections = (
            list(
                db.scalars(
                    select(IntegrationTracePayload.section).where(
                        IntegrationTracePayload.trace_id == row.id,
                        IntegrationTracePayload.expires_at > utcnow(),
                    )
                )
            )
            if sections is None
            else sections
        )
        details = row.details
        return {
            "id": row.id,
            "trace_id": row.trace_id,
            "span_id": row.id,
            "parent_span_id": row.parent_span_id,
            "sequence": row.sequence,
            "operation_id": row.operation_id,
            "project_id": row.project_id,
            "account_id": row.account_id,
            "service": row.service,
            "level": "error"
            if row.status in ERROR_STATES
            else "warning"
            if row.status in {"cancelled", "fallback"}
            else "info",
            "stage": row.stage,
            "direction": row.direction,
            "at": iso(row.created_at),
            "status": row.status,
            "capture_state": row.capture_state,
            "available_sections": sorted(sections),
            "redacted": bool(details.get("redacted")) and bool(sections),
            "truncated": bool(details.get("truncated")) and bool(sections),
            "expires_at": iso(row.expires_at),
            "section_states": details.get("section_states", {}),
            **{
                key: details[key]
                for key in (
                    "provider",
                    "model",
                    "connection_id",
                    "attempt",
                    "duration_ms",
                    "http_status",
                    "provider_request_id",
                    "draft_revision",
                    "preparation_id",
                    "snapshot_id",
                    "error_code",
                    "endpoint",
                    "project_revision",
                    "schema_version",
                    "template_versions",
                    "retry_reason",
                    "capture_reason",
                )
                if details.get(key) is not None
            },
        }

    def metadata_list(self, db, rows):
        sections = {}
        if rows:
            for identity, section in db.execute(
                select(IntegrationTracePayload.trace_id, IntegrationTracePayload.section).where(
                    IntegrationTracePayload.trace_id.in_([row.id for row in rows]),
                    IntegrationTracePayload.expires_at > utcnow(),
                )
            ):
                sections.setdefault(identity, []).append(section)
        return [self.metadata(db, row, sections.get(row.id, [])) for row in rows]

    def listing(
        self,
        actor,
        service=None,
        level=None,
        correlation=None,
        project=None,
        before=None,
        limit=100,
        phase=None,
        state=None,
        since=None,
        until=None,
    ):
        self.prune()
        with self.repo.sessions() as db:
            self.policies.admin(db, actor)
            query = select(IntegrationTrace)
            for value, column in (
                (service, IntegrationTrace.service),
                (correlation, IntegrationTrace.trace_id),
                (project, IntegrationTrace.project_id),
                (phase, IntegrationTrace.stage),
                (state, IntegrationTrace.status),
            ):
                if value:
                    query = query.where(column == value)
            if level:
                errors, warnings = ERROR_STATES, {"cancelled", "fallback"}
                query = (
                    query.where(IntegrationTrace.status.in_(errors if level == "error" else warnings))
                    if level != "info"
                    else query.where(IntegrationTrace.status.not_in(errors | warnings))
                )
            if since:
                query = query.where(IntegrationTrace.created_at >= since)
            if until:
                query = query.where(IntegrationTrace.created_at <= until)
            if before:
                boundary = db.get(IntegrationTrace, before)
                if not boundary:
                    raise RuleError("INVALID_CURSOR", "Página indisponível.", 400)
                query = query.where(
                    or_(
                        IntegrationTrace.created_at < boundary.created_at,
                        and_(
                            IntegrationTrace.created_at == boundary.created_at,
                            IntegrationTrace.id < boundary.id,
                        ),
                    )
                )
            rows = db.scalars(
                query.order_by(IntegrationTrace.created_at.desc(), IntegrationTrace.id.desc()).limit(
                    limit + 1
                )
            ).all()
            return {
                "items": self.metadata_list(db, rows[:limit]),
                "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
                "server_time": iso(utcnow()),
                "retention_days": self.policies.read(db, "integration_capture")["settings"]["retention_days"],
                "can_inspect_payloads": self.policies.can_inspect(db, actor),
            }

    def payload(self, actor, identity, section, copying=False):
        if section not in SECTIONS:
            raise RuleError("VALIDATION_ERROR", "Seção inválida.", 400)
        denied, missing, result = False, False, None
        with self.repo.sessions.begin() as db:
            self.policies.admin(db, actor)
            allowed = self.policies.can_inspect(db, actor)
            db.add(
                Audit(
                    actor_id=actor,
                    action="trace:" + ("copy" if copying else "read") + ("" if allowed else ":denied"),
                    resource_id=identity[:60],
                    reason="section:" + section,
                )
            )
            if not allowed:
                denied = True
            else:
                configuration_lock(db, "trace-payload-quota")
                self.prune_db(db)
                row = db.get(IntegrationTrace, identity)
                if not row:
                    missing = True
                payload = db.get(IntegrationTracePayload, (identity, section)) if row else None
                result = (
                    {
                        "trace_id": identity,
                        "section": section,
                        "payload": payload.content["payload"] if payload else None,
                        "capture_state": (
                            "truncated"
                            if payload.content["truncated_paths"]
                            else "protected"
                            if payload.content["redacted_paths"]
                            else "captured"
                        )
                        if payload
                        else row.capture_state
                        if row.capture_state in {"expired", "disabled", "unavailable"}
                        else row.details.get("section_states", {}).get(section, "not_captured"),
                        "redacted_paths": payload.content["redacted_paths"] if payload else [],
                        "truncated_paths": payload.content["truncated_paths"] if payload else [],
                        "policy_version": payload.policy_version if payload else None,
                        "expires_at": iso(payload.expires_at if payload else row.expires_at),
                        **(
                            {"capture_reason": row.details["capture_reason"]}
                            if not payload and row.details.get("capture_reason")
                            else {}
                        ),
                    }
                    if row
                    else None
                )
        if denied:
            raise RuleError(
                "PAYLOAD_ACCESS_DENIED",
                "Sua conta não possui permissão específica para inspecionar cargas protegidas.",
                403,
            )
        if missing:
            raise RuleError("NOT_FOUND", "Trace indisponível.", 404)
        return result

    def timeline(self, actor, identity):
        with self.repo.sessions() as db:
            self.policies.admin(db, actor)
            row = db.get(IntegrationTrace, identity)
            if not row:
                raise RuleError("NOT_FOUND", "Trace indisponível.", 404)
            rows = db.scalars(
                select(IntegrationTrace)
                .where(IntegrationTrace.trace_id == row.trace_id)
                .order_by(IntegrationTrace.sequence)
                .limit(200)
            ).all()
            return {
                "items": self.metadata_list(db, rows),
                "can_inspect_payloads": self.policies.can_inspect(db, actor),
            }


class TraceObserver:
    def __init__(self, repo, context):
        self.store, self.context, self.identity = TraceStore(repo), context, None
        self.started = None
        self.secrets = []
        self.retry_reason = None

    def request(self, request):
        self.started = time.monotonic()
        self.secrets = [
            value.removeprefix("Bearer ")
            for name in ("authorization", "x-goog-api-key")
            if (value := request.headers.get(name))
        ]
        details = {"endpoint": request.url.scheme + "://" + request.url.host + request.url.path}
        if self.retry_reason:
            details["retry_reason"] = self.retry_reason
        sections = {}
        try:
            body = json.loads(request.content)
            sections["request"] = {
                "method": request.method,
                "headers": {"content-type": request.headers.get("content-type")},
                "body": body,
            }
        except (ValueError, UnicodeError):
            details.update(capture_reason="NON_JSON_REQUEST", section_states={"request": "unavailable"})
        self.identity = self.store.put(
            self.context,
            "exchange",
            "pending",
            sections,
            details,
        )

    def response(self, response, raw):
        sections, details = {}, {}
        try:
            body = json.loads(raw)
            sections["response"] = body
        except (ValueError, UnicodeError):
            details.update(capture_reason="NON_JSON_RESPONSE", section_states={"response": "unavailable"})
        external = response.headers.get("x-request-id") or response.headers.get("x-goog-request-id")
        external = (
            external
            if external
            and re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", external)
            and not any(
                secret and (secret == external or len(secret) > 12 and secret in external)
                for secret in (*self.secrets, *self.store.repo.sources.secrets)
            )
            else None
        )
        self.store.put(
            self.context,
            "exchange",
            "received" if response.status_code == 200 else "rejected",
            sections,
            {
                **details,
                "http_status": response.status_code,
                "provider_request_id": external,
                "duration_ms": max(0, int((time.monotonic() - self.started) * 1000)),
            },
            self.identity,
        )

    def failure(self, code="PROVIDER_RESULT_UNKNOWN"):
        if self.identity:
            self.store.put(
                self.context,
                "exchange",
                "uncertain",
                details={
                    "error_code": code,
                    "duration_ms": max(0, int((time.monotonic() - self.started) * 1000)),
                },
                identity=self.identity,
            )

    def retry(self, reason):
        self.retry_reason = reason


def trace_context(repo, operation, stage, attempt=1):
    payload = operation.payload
    identifiers = []
    for item in payload.get("items", []):
        identifiers.extend([item["attachment_id"], item["source_version_id"]])
        identifiers.extend(field["key"] for field in item.get("definitions", []))
    for item in payload.get("attachment_snapshots", []):
        identifiers.extend([item["source_template_id"], item["source_version_id"]])
        identifiers.extend(item.get("fields", {}))
    versions = [
        {
            "attachment_id": item["attachment_id"],
            "source_version_id": item["source_version_id"],
            "attachment_revision": item["attachment_revision"],
        }
        for item in payload.get("items", [])
    ]
    versions.extend(
        {
            "attachment_id": item["source_template_id"],
            "source_version_id": item["source_version_id"],
            "attachment_revision": item.get("attachment_revision"),
        }
        for item in payload.get("attachment_snapshots", [])
    )
    return {
        "trace_id": payload.get("correlation_id") or operation.id,
        "request_id": payload.get("http_request_id"),
        "workflow_request_id": payload.get("request_id"),
        "operation_id": operation.id,
        "account_id": operation.account_id,
        "project_id": operation.project_id,
        "stage": stage,
        "provider": operation.policy.get("provider"),
        "model": operation.policy.get("model"),
        "connection_id": operation.policy.get("connection_id"),
        "attempt": attempt,
        "draft_revision": payload.get("draft_revision")
        or payload.get("guided_request", {}).get("expected_draft_revision"),
        "preparation_id": operation.id if stage == "preparation" else payload.get("preparation_id"),
        "identifiers": identifiers,
        "schema_version": 1 if versions else None,
        "template_versions": versions,
        "project_revision": payload.get("base_revision_id"),
    }


@contextmanager
def observe_integration(repo, context):
    observer = TraceObserver(repo, context)
    token = OBSERVER.set(observer)
    try:
        yield observer
    except Exception as exc:
        emit(
            "integration_failed",
            "error",
            error=exc,
            secrets=repo.sources.secrets,
            **safe_fields({**context, "phase": "provider"}, repo.sources.secrets),
        )
        raise
    finally:
        OBSERVER.reset(token)


def observed_request(request):
    observer = OBSERVER.get()
    if observer:
        observer.request(request)


def observed_response(response, raw):
    observer = OBSERVER.get()
    if observer:
        observer.response(response, raw)


def observed_failure():
    observer = OBSERVER.get()
    if observer:
        observer.failure()


def observed_retry(reason):
    observer = OBSERVER.get()
    if observer:
        observer.retry(reason)
