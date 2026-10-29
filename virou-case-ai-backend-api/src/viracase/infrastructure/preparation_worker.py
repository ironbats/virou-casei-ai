import copy
from datetime import timedelta
from time import monotonic

from sqlalchemy import select

from viracase.application.progress import record_progress
from viracase.application.template_preparation import (
    merge_preparation,
    parse_preparation_result,
    preparation_input_bytes,
    preparation_prompt,
)
from viracase.application.template_preparation_proposals import explicit_empty_target, proposal_call_schema
from viracase.application.template_preparation_selection import selection_call_schema
from viracase.domain.rules import RuleError, cost, utcnow, validate_file_entries
from viracase.domain.template_fields import empty
from viracase.infrastructure.agent_execution import execution_fields, execution_timeouts
from viracase.infrastructure.build_workflow import apply_automatic
from viracase.infrastructure.integration_traces import (
    TraceStore,
    observe_integration,
    trace_context,
)
from viracase.infrastructure.models import Attempt, Job, Operation
from viracase.infrastructure.providers import ProviderRejected, ProviderUncertain
from viracase.infrastructure.template_preparations import TemplatePreparations, preparation_error_message
from viracase.infrastructure.workflow_logging import emit


def execute_preparation(worker, oid, token):
    repo = worker.repo
    total, calls = {"input": 0, "cached": 0, "output": 0}, []
    feedback = None
    accepted = []

    def finish(db, op, job, state, error=None, items=None):
        schedule = (
            db.get(Operation, op.payload["autofill_schedule_id"])
            if op.payload.get("autofill_schedule_id")
            else None
        )
        if schedule and schedule.cancel_requested:
            state, items, op.cancel_requested = "cancelled", None, True
        if items is None and accepted and state != "cancelled":
            items = copy.deepcopy(accepted)
        try:
            repo.settle_operation(db, op, cost(op.policy, total))
        except RuleError as exc:
            op.state, op.billing_status, op.error_code, job.state = (
                "reconciling",
                "reconciliation_pending",
                "COST_DISCREPANCY",
                "reconciling",
            )
            record_progress(op, "provider_result_unknown")
            emit(
                "preparation_settlement_pending",
                "warning",
                operation=op,
                error=exc,
                phase="settlement",
                state=op.state,
                error_code=op.error_code,
                usage_known=True,
            )
            apply_automatic(repo, db, op)
            return
        op.state, op.error_code, job.state = state, error, "done"
        value = {
            "state": "needs_input"
            if state == "succeeded" and items and any(item["questions"] or item["errors"] for item in items)
            else "ready"
            if state == "succeeded"
            else state,
            "items": items or op.payload["public_items"],
            "completed_at": utcnow().isoformat(),
        }
        if error:
            for item in value["items"]:
                item["state"] = "failed"
                item["errors"] = [
                    *item.get("errors", []),
                    {
                        "attachment_id": item["attachment_id"],
                        "code": error,
                        "message": preparation_error_message(error),
                        "action": "Revise o pedido e consulte o estado antes de repetir",
                    },
                ]
        op.result = {
            **op.result,
            "preparation": value,
            "preparation_version": op.result.get("preparation_version", 1) + 1,
        }
        record_progress(op, "preparation_" + value["state"])
        emit(
            "preparation_completed",
            "warning" if error else "info",
            operation=op,
            phase="settlement",
            state=op.state,
            preparation_state=value["state"],
            error_code=error,
            usage_known=True,
            input_tokens=total["input"],
            cached_tokens=total["cached"],
            output_tokens=total["output"],
            suggested_count=sum(len(item.get("suggested_fields", [])) for item in value["items"]),
            pending_count=sum(
                len(item.get("errors", [])) + len(item.get("questions", [])) for item in value["items"]
            ),
            actual_brl=cost(op.policy, total),
            billing_status=op.billing_status,
        )
        normalized = {
            "state": value["state"],
            "items": value["items"],
            "usage": total,
            "error": {"code": error} if error else None,
        }
        sections = {"normalized": normalized}
        if items:
            sections["fields"] = {
                "items": [
                    {
                        "attachment_id": item["attachment_id"],
                        "field_id": field["field_id"],
                        "original_value": field["original_value"],
                        "suggested_value": field["value"],
                        "source_type": field["source_type"],
                        "state": "suggested",
                        "attachment_revision": item["attachment_revision"],
                    }
                    for item in items
                    for field in item["suggested_fields"]
                ]
            }
        TraceStore(repo).put(
            trace_context(repo, op, "preparation", len(calls) or 1),
            "normalized",
            value["state"],
            sections,
            {"error_code": error},
            db=db,
        )
        apply_automatic(repo, db, op)

    with repo.sessions() as db:
        max_repairs = db.get(Operation, oid).payload["preparation_settings"]["max_repairs"]
    for dispatch in range(1 + max_repairs):
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            account = repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return
            db.refresh(op)
            schedule = (
                db.get(Operation, op.payload["autofill_schedule_id"])
                if op.payload.get("autofill_schedule_id")
                else None
            )
            if op.cancel_requested or schedule and schedule.cancel_requested:
                finish(db, op, job, "cancelled")
                return
            try:
                repo.access(db, account, "generate")
                repo.owned_project(db, account.id, op.project_id)
                if repo.config.restore_hold:
                    raise RuleError("RECOVERY_HOLD", "A preparação está pausada para conciliação.")
                preparer = TemplatePreparations(repo)
                row, draft = preparer.current_draft(db, account.id, op.project_id)
                if preparer.stale(db, op, draft):
                    raise RuleError("PREPARATION_STALE", "O contexto mudou antes do envio.")
                selected = {item["template_id"]: item for item in draft["attachments"] if item["enabled"]}
                relevant = [
                    copy.deepcopy(item)
                    for item in op.payload["items"]
                    if item["attachment_id"] in selected
                    and selected[item["attachment_id"]]["attachment_instance_id"]
                    == item["attachment_instance_id"]
                ]
                if not relevant:
                    finish(db, op, job, "cancelled")
                    return
                relevant_ids = {item["attachment_id"] for item in relevant}
                accepted = [item for item in accepted if item["attachment_id"] in relevant_ids]
                if op.result.get("preparation_partial"):
                    op.result = {**op.result, "preparation_partial": copy.deepcopy(accepted)}
                accepted_fields = {
                    item["attachment_id"]: {field["field_id"] for field in item["suggested_fields"]}
                    for item in accepted
                }
                needs_fill = any(
                    empty(item["original_fields"].get(field["key"]))
                    and field["key"] not in accepted_fields.get(item["attachment_id"], set())
                    and empty(selected[item["attachment_id"]]["fields"].get(field["key"]))
                    and (
                        not selected[item["attachment_id"]]
                        .get("field_values", {})
                        .get(field["key"], {})
                        .get("edited_by_user")
                        or explicit_empty_target(
                            item,
                            field["key"],
                            selected[item["attachment_id"]]["fields"].get(field["key"]),
                            selected[item["attachment_id"]].get("field_values", {}).get(field["key"], {}),
                        )
                    )
                    and selected[item["attachment_id"]]
                    .get("field_values", {})
                    .get(field["key"], {})
                    .get("field_revision", 1)
                    == item.get("original_field_revisions", {}).get(field["key"], 1)
                    for item in relevant
                    for field in item["definitions"]
                )
                if not needs_fill:
                    finish(db, op, job, "succeeded" if accepted else "cancelled")
                    return
                for item in relevant:
                    metadata = selected[item["attachment_id"]].get("field_values", {})
                    item["protected_field_ids"] = [
                        field["key"]
                        for field in item["definitions"]
                        if (
                            metadata.get(field["key"], {}).get("edited_by_user")
                            and not explicit_empty_target(
                                item,
                                field["key"],
                                selected[item["attachment_id"]]["fields"].get(field["key"]),
                                metadata.get(field["key"], {}),
                            )
                        )
                        or metadata.get(field["key"], {}).get("field_revision", 1)
                        != item.get("original_field_revisions", {}).get(field["key"], 1)
                    ]
                    item["context_fields"] = preparer.clean_definition(
                        {
                            key: selected[item["attachment_id"]]["fields"].get(key)
                            for key in (
                                item.get("context_fields", item["original_fields"])
                                if op.payload.get("template_preparation_contract") == 4
                                else [field["key"] for field in item["definitions"]]
                            )
                        }
                    )
                    prior = next(
                        (value for value in accepted if value["attachment_id"] == item["attachment_id"]), None
                    )
                    if prior:
                        for field in prior["suggested_fields"]:
                            if (
                                empty(item["context_fields"].get(field["field_id"]))
                                and field["field_id"] not in item["protected_field_ids"]
                            ):
                                item["context_fields"][field["field_id"]] = field["value"]
                if len(relevant) != len(op.payload["items"]):
                    ids = {item["attachment_id"] for item in relevant}
                    op.payload = {
                        **op.payload,
                        "items": relevant,
                        "public_items": [
                            item for item in op.payload["public_items"] if item["attachment_id"] in ids
                        ],
                    }
                else:
                    op.payload = {**op.payload, "items": relevant}
                for item in op.payload["items"]:
                    preparer.guided.templates.authorized(db, account.id, item["source_version_id"])
                prompt = preparation_prompt(op.payload, feedback)
                input_bytes = preparation_input_bytes(op.payload, feedback)
                input_bound = op.policy["reserved_input_bound"]
                overhead = 4096 if op.payload.get("template_preparation_contract", 1) not in {3, 4} else 0
                if input_bytes + overhead > input_bound:
                    raise RuleError("PREPARATION_CONTEXT_LIMIT", "O contexto excede a reserva aceita.")
            except RuleError as exc:
                emit(
                    "preparation_preflight_failed",
                    "warning",
                    operation=op,
                    error=exc,
                    phase="preflight",
                    state="failed",
                    attempt=dispatch + 1,
                )
                finish(db, op, job, "failed", exc.code)
                return
            policy = dict(op.policy)
            if op.payload.get("template_preparation_contract", 1) == 3:
                policy["preparation_selection_schema"] = selection_call_schema(op.payload)
            elif op.payload.get("template_preparation_contract", 1) == 4:
                policy["preparation_proposal_schema"] = proposal_call_schema(op.payload)
            attempt = db.scalar(select(Attempt).where(Attempt.operation_id == oid))
            if not attempt:
                attempt = Attempt(
                    operation_id=oid, provider=policy["provider"], model=policy["model"], state="sent"
                )
                db.add(attempt)
            attempt.state = "sent"
            op.state = "running"
            op.result = {**op.result, "preparation_version": op.result.get("preparation_version", 1) + 1}
            record_progress(op, "preparation_repairing" if feedback else "preparation_filling")
            request_timeout, _ = execution_timeouts(policy, repo.config.provider_timeout)
            job.lease_until = utcnow() + timedelta(seconds=request_timeout * 2 + 120)
            context = trace_context(repo, op, "preparation", dispatch + 1)
            emit(
                "preparation_dispatch",
                operation=op,
                phase="provider",
                state="running",
                attempt=dispatch + 1,
                context_bytes=input_bytes,
                max_output_tokens=policy["max_output_tokens"],
                **execution_fields(policy, repo.config.provider_timeout),
                attachment_count=len(op.payload["items"]),
                field_count=sum(len(item["definitions"]) for item in op.payload["items"]),
            )
        started = monotonic()
        try:
            with observe_integration(repo, context):
                result = worker.gateway.generate(policy, prompt)
        except (ProviderRejected, ProviderUncertain) as exc:
            with repo.sessions.begin() as db:
                op = db.get(Operation, oid)
                repo.account(db, op.account_id, True)
                job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
                if job.lease_token != token:
                    return
                attempt = db.scalar(select(Attempt).where(Attempt.operation_id == oid))
                emit(
                    "preparation_provider_failed",
                    "warning",
                    operation=op,
                    error=exc,
                    phase="provider",
                    attempt=dispatch + 1,
                    duration_ms=round((monotonic() - started) * 1000),
                    usage_known=not isinstance(exc, ProviderUncertain),
                    error_code="PROVIDER_RESULT_UNKNOWN"
                    if isinstance(exc, ProviderUncertain)
                    else exc.error_code,
                    http_status=getattr(exc, "diagnostic", {}).get("http_status"),
                    reason_code=getattr(exc, "diagnostic", {}).get("reason_code"),
                )
                if isinstance(exc, ProviderUncertain):
                    attempt.state = "uncertain"
                    op.state, op.billing_status, job.state, op.error_code = (
                        "reconciling",
                        "reconciliation_pending",
                        "reconciling",
                        "PROVIDER_RESULT_UNKNOWN",
                    )
                    record_progress(op, "provider_result_unknown")
                    if accepted:
                        op.result = {
                            **op.result,
                            "preparation": {"state": "reconciling", "items": copy.deepcopy(accepted)},
                        }
                    apply_automatic(repo, db, op)
                    TraceStore(repo).put(
                        context,
                        "normalized",
                        "uncertain",
                        {"normalized": {"error": {"code": "PROVIDER_RESULT_UNKNOWN"}, "usage": total}},
                        {"error_code": "PROVIDER_RESULT_UNKNOWN"},
                        db=db,
                    )
                else:
                    attempt.state, attempt.actual_brl = "confirmed", cost(policy, total)
                    finish(db, op, job, "failed", exc.error_code)
            return
        total = {name: total[name] + result.usage[name] for name in total}
        calls.append(
            {"external_id": result.external_id, "usage": result.usage, "output_error": result.output_error,
             "diagnostics": result.diagnostics}
        )
        emit(
            "preparation_provider_received",
            operation=op,
            phase="provider",
            attempt=dispatch + 1,
            duration_ms=round((monotonic() - started) * 1000),
            usage_known=True,
            error_code=result.output_error,
            input_tokens=result.usage["input"],
            cached_tokens=result.usage["cached"],
            output_tokens=result.usage["output"],
            **result.diagnostics,
        )
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            account = repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return
            db.refresh(op)
            attempt = db.scalar(select(Attempt).where(Attempt.operation_id == oid))
            attempt.state, attempt.actual_brl, attempt.usage, attempt.external_id = (
                "confirmed",
                cost(policy, total),
                {**total, "calls": calls},
                result.external_id,
            )
            if (
                account.lifecycle in {"purging", "reconciling", "purged"}
                or "read" not in repo.access(db, account)["allowed_actions"]
                or not op.project_id
                or not op.payload
            ):
                repo.settle_operation(db, op, cost(policy, total))
                op.state, job.state, op.payload, op.result = "failed", "done", {}, {}
                op.error_code = "RETENTION_EXPIRED"
                record_progress(op, "result_discarded_after_retention")
                return
            if op.cancel_requested:
                finish(db, op, job, "cancelled")
                return
            try:
                repo.access(db, account, "write")
                repo.owned_project(db, account.id, op.project_id)
                if result.output_error:
                    raise RuleError(
                        "PREPARATION_TRUNCATED"
                        if result.output_error == "GENERATION_TRUNCATED"
                        else "PREPARATION_BLOCKED",
                        "O provedor não retornou uma resposta completa.",
                    )
                validate_file_entries({"response.txt": result.text}, repo.sources.secrets)
                items, explicit_questions = parse_preparation_result(
                    result.text, op.payload, oid, partial=True
                )
                items = merge_preparation(accepted, items, explicit_questions)
            except RuleError as exc:
                emit(
                    "preparation_validation_failed",
                    "warning",
                    operation=op,
                    error=exc,
                    phase="parse",
                    attempt=dispatch + 1,
                )
                if dispatch < op.payload["preparation_settings"]["max_repairs"] and exc.code.startswith(
                    "PREPARATION_"
                ):
                    feedback = exc.code
                    TraceStore(repo).put(
                        context,
                        "normalized",
                        "invalid",
                        {"normalized": {"error": {"code": exc.code}, "usage": result.usage}},
                        {"error_code": exc.code},
                        db=db,
                    )
                    continue
                finish(db, op, job, "failed", exc.code)
                return
            errors = [error for item in items for error in item["errors"]]
            for error in errors:
                emit(
                    "preparation_field_rejected",
                    "warning",
                    operation=op,
                    phase="parse",
                    attempt=dispatch + 1,
                    attachment_id=error["attachment_id"],
                    field_id=error.get("field_id"),
                    error_code=error["code"],
                    **{
                        key: error[key]
                        for key in (
                            "reason_code",
                            "expected_type",
                            "received_type",
                            "value_encoding",
                            "refs_count",
                            "evidence_length",
                            "max_length",
                        )
                        if key in error
                    },
                )
            if errors and dispatch < op.payload["preparation_settings"]["max_repairs"]:
                accepted = copy.deepcopy(items)
                op.result = {**op.result, "preparation_partial": accepted}
                feedback = [
                    {
                        key: error[key]
                        for key in (
                            "attachment_id",
                            "field_id",
                            "code",
                            "reason_code",
                            "expected_type",
                            "received_type",
                            "value_encoding",
                            "max_length",
                            "message",
                        )
                        if key in error
                    }
                    for error in errors
                ]
                emit(
                    "preparation_repair_queued",
                    "warning",
                    operation=op,
                    phase="parse",
                    attempt=dispatch + 1,
                    pending_count=len(errors),
                    suggested_count=sum(len(item["suggested_fields"]) for item in accepted),
                )
                continue
            finish(db, op, job, "succeeded", items=items)
            return
