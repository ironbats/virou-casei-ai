from datetime import timedelta

from sqlalchemy import select

from viracase.application.conversation import (
    INPUT_OVERHEAD,
    MAX_GENERATION_CONTEXT_BYTES,
    apply_batch,
    context_input_bound,
    context_input_budget,
    generation_batch_budget,
    generation_call_limit,
    generation_recovery_policy,
    prompt_for,
)
from viracase.application.conversation_turn import InvalidTurn, parse_turn, turn_prompt
from viracase.application.progress import record_progress
from viracase.domain.migration_history import migration_manifest
from viracase.domain.rules import RuleError, cost, utcnow, validate_file_entries, validate_files
from viracase.infrastructure.agent_execution import execution_fields, execution_timeouts
from viracase.infrastructure.clarifications import checkpoint_clarification, continuation_files
from viracase.infrastructure.conversation_state import save_turn
from viracase.infrastructure.guided_build import attachment_context
from viracase.infrastructure.integration_traces import TraceStore, observe_integration, trace_context
from viracase.infrastructure.models import Attempt, Job, Operation
from viracase.infrastructure.provider_errors import report_rejection
from viracase.infrastructure.providers import ProviderRejected, ProviderUncertain
from viracase.infrastructure.publication_migration_context import (
    published_migration_context,
    validate_revision_history,
    validate_revision_migrations,
)
from viracase.infrastructure.repository import project_dto
from viracase.infrastructure.workflow_logging import emit as emit_workflow


def execute_conversation(worker, oid, token):
    repo = worker.repo
    total = {"input": 0, "cached": 0, "output": 0}
    calls, files, original = [], None, None
    truncation_retries, batch_character_budget = 0, 0
    feedback, repairs, messages = None, 0, []
    full_files = False
    context_bytes, input_bound = 0, 0
    provider_calls, accepted_batches = 0, 0
    confirmed_deletions = set()
    with repo.sessions() as db:
        frozen_policy = db.get(Operation, oid).policy
        max_batches = generation_call_limit(frozen_policy)
        recovery = generation_recovery_policy(frozen_policy)

    def observe(event, op, level="info", error=None, phase="construction", **fields):
        details = getattr(error, "details", None) or {}
        emit_workflow(
            event,
            level,
            operation=op,
            error=error,
            secrets=repo.sources.secrets,
            **{
                "transaction_state": "pending_commit",
                "phase": phase,
                "batch": batch + 1,
                "max_batches": max_batches,
                "provider_calls": provider_calls,
                "accepted_batches": accepted_batches,
                "repair_attempts": repairs,
                "truncation_retries": truncation_retries,
                "batch_character_budget": batch_character_budget,
                "construction_policy_version": op.policy.get("construction_policy_version", 0),
                **recovery,
                **execution_fields(op.policy, repo.config.provider_timeout),
                "context_bytes": context_bytes,
                "reserved_input_bound": input_bound,
                "remaining_input_budget": max(0, context_input_budget(op.policy) - total["input"]),
                "input_tokens": total["input"],
                "cached_tokens": total["cached"],
                "output_tokens": total["output"],
                "max_output_tokens": op.policy["max_output_tokens"],
                "files_count": len(files or {}),
                "generation_context_version": op.policy.get("generation_context_version", 1),
                "request_context_limit_bytes": op.payload.get("context_limit_bytes", 60000),
                "generation_context_limit_bytes": op.payload.get(
                    "generation_context_limit_bytes", MAX_GENERATION_CONTEXT_BYTES
                ),
                "total_input_budget": context_input_budget(op.policy),
                **details,
                **fields,
            },
        )

    def finish(db, op, job, state, error=None, stage=None, failure=None, phase="construction"):
        try:
            repo.settle_operation(db, op, cost(op.policy, total))
        except RuleError:
            op.state, op.billing_status, op.error_code, job.state = (
                "reconciling",
                "reconciliation_pending",
                "COST_DISCREPANCY",
                "reconciling",
            )
            record_progress(op, "reconciling")
            observe("generation_finished", op, "error", phase="settlement", error_code=op.error_code)
            return False
        op.state, op.error_code, job.state = state, error, "done"
        if isinstance(failure, RuleError) and failure.code.startswith("MIGRATION_"):
            op.result = {**op.result, "migration_failure": {"code": failure.code, "message": failure.message, **failure.details}}
        record_progress(op, stage or ("proposal_ready" if state == "succeeded" else state))
        if state != "succeeded":
            observe(
                "generation_finished",
                op,
                "error" if state == "failed" else "info",
                error=failure,
                phase=phase,
                error_code=error,
            )
        return True

    for batch in range(max_batches):
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            a = repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return
            db.refresh(op)
            if op.cancel_requested:
                finish(db, op, job, "cancelled")
                return
            try:
                repo.access(db, a, "generate")
                if repo.config.restore_hold or not any(
                    p["model"] == op.policy["model"] and p["version"] == op.policy["version"]
                    for p in worker.catalog.eligible()
                ):
                    raise RuleError("COMMERCIAL_CONFIGURATION_PENDING", "Integração indisponível.")
                p = repo.owned_project(db, a.id, op.project_id)
                if files is None:
                    files = repo.sources.read(
                        repo.owned_revision(db, a.id, p.id, op.payload["base_revision_id"]), p.stack
                    )
                    original = dict(files)
                    if op.policy.get("generation_contract_version") == 4 and op.payload.get("reply_to"):
                        files, messages = continuation_files(op.payload, original, repo.sources.secrets)
                        confirmed_deletions.update(original.keys() - files.keys())
                conversational = op.policy.get("generation_contract_version") == 4
                allow_deletions = conversational and bool(op.payload.get("analysis_id"))
                input_bound = context_input_bound(op.policy, op.payload, total["input"])
                batch_character_budget = generation_batch_budget(op.policy, truncation_retries, input_bound)
                request_limit = (
                    op.payload.get("context_limit_bytes", 60000)
                    if op.policy.get("generation_context_version") == 2 and batch == 0
                    else None
                )
                if conversational:
                    migrations = published_migration_context(
                        repo, db, p, files, base_revision_id=op.payload["base_revision_id"], baseline_files=original,
                    )
                    op.payload = {**op.payload, "publication_migration_context": migrations}
                    prompt = turn_prompt(
                        project_dto(p),
                        attachment_context(op.payload),
                        files,
                        batch,
                        bool(truncation_retries),
                        op.policy["max_output_tokens"],
                        op.payload.get("history", []),
                        feedback,
                        input_bound=input_bound,
                        request_limit_bytes=request_limit,
                        max_batches=max_batches,
                        character_budget=batch_character_budget,
                        allow_deletions=allow_deletions,
                        full_files=full_files,
                        published_migrations=migrations,
                        confirmed_deletions=confirmed_deletions,
                    )
                else:
                    prompt = prompt_for(
                        project_dto(p),
                        attachment_context(op.payload),
                        files,
                        batch,
                        bool(truncation_retries),
                        output_tokens=op.policy["max_output_tokens"],
                        input_bound=input_bound,
                        request_limit_bytes=request_limit,
                        max_batches=max_batches,
                        character_budget=batch_character_budget,
                        protected_migrations=migration_manifest(original),
                        feedback=feedback,
                    )
                context_bytes = len(prompt.encode())
            except RuleError as exc:
                if exc.code == "CONTEXT_LIMIT_EXCEEDED":
                    full_limit = op.payload.get("generation_context_limit_bytes", MAX_GENERATION_CONTEXT_BYTES)
                    exc.details = {
                        "reason_code": "LEGACY_CONTEXT_LIMIT"
                        if op.policy.get("generation_context_version") != 2
                        else "GENERATION_INPUT_BUDGET"
                        if input_bound < full_limit + INPUT_OVERHEAD
                        else "GENERATION_CONTEXT_LIMIT",
                        "remaining_input_budget": max(0, context_input_budget(op.policy) - total["input"]),
                        "input_tokens": total["input"],
                        "batch": batch + 1,
                        "max_batches": max_batches,
                        **exc.details,
                    }
                finish(db, op, job, "failed", exc.code, failure=exc, phase="context")
                return
            observe("generation_context_ready", op, phase="context")
            policy, stack = dict(op.policy), p.stack
            if conversational:
                policy["generation_allow_deletions"] = allow_deletions
                policy["generation_full_files"] = full_files
            attempt = db.scalar(select(Attempt).where(Attempt.operation_id == oid))
            if not attempt:
                attempt = Attempt(
                    operation_id=oid, provider=policy["provider"], model=policy["model"], state="sent"
                )
                db.add(attempt)
            # Persist the dispatch BEFORE the network boundary. A crash/timeout is never auto-replayed.
            attempt.state = "sent"
            op.state = "running"
            record_progress(
                op, "repairing_response" if feedback else "continuing_code" if batch else "provider_sent"
            )
            request_timeout, _ = execution_timeouts(policy, repo.config.provider_timeout)
            job.lease_until = utcnow() + timedelta(seconds=request_timeout * 2 + 120)
            provider_calls += 1
            observe(
                "generation_batch_dispatched",
                op,
                phase="provider",
                max_output_tokens=policy["max_output_tokens"],
            )
        try:
            with observe_integration(repo, trace_context(repo, op, "construction", batch + 1)):
                result = worker.gateway.generate(policy, prompt)
        except (ProviderRejected, ProviderUncertain) as exc:
            with repo.sessions.begin() as db:
                op = db.get(Operation, oid)
                repo.account(db, op.account_id, True)
                job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
                if job.lease_token != token:
                    return
                attempt = db.scalar(select(Attempt).where(Attempt.operation_id == oid))
                if isinstance(exc, ProviderUncertain):
                    attempt.state = "uncertain"
                    op.state, op.billing_status, job.state = (
                        "reconciling",
                        "reconciliation_pending",
                        "reconciling",
                    )
                    op.error_code = "PROVIDER_RESULT_UNKNOWN"
                    record_progress(op, "reconciling")
                    observe(
                        "generation_finished", op, "error", error=exc,
                        phase="provider", error_code=op.error_code,
                    )
                else:
                    report_rejection(oid, exc)
                    attempt.state = "confirmed"
                    attempt.actual_brl = cost(policy, total)
                    finish(db, op, job, "failed", exc.error_code, failure=exc, phase="provider")
            return
        total = {k: total[k] + result.usage[k] for k in total}
        calls.append(
            {"external_id": result.external_id, "usage": result.usage, "output_error": result.output_error,
             "diagnostics": result.diagnostics}
        )
        # Never expose or save truncated output as a revision.
        with repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            a = repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return
            db.refresh(op)
            attempt = db.scalar(select(Attempt).where(Attempt.operation_id == oid))
            attempt.usage, attempt.state, attempt.actual_brl = (
                {**total, "calls": calls},
                "confirmed",
                cost(policy, total),
            )
            attempt.external_id = result.external_id
            TraceStore(repo).put(trace_context(repo, op, "construction", batch + 1), "normalized", "received", {"normalized": {"usage": result.usage, "output_error": result.output_error}}, db=db)
            observe("generation_batch_received", op, phase="validation", error_code=result.output_error, **result.diagnostics)
            if op.cancel_requested:
                finish(db, op, job, "cancelled")
                return
            try:
                repo.access(db, a, "write")
                if (
                    result.output_error == "GENERATION_TRUNCATED"
                    and truncation_retries < recovery["max_truncation_retries"]
                    and batch < max_batches - 1
                ):
                    truncation_retries += 1
                    record_progress(op, "resizing_code")
                    observe(
                        "generation_batch_retry", op, "warning", phase="validation",
                        reason_code="GENERATION_TRUNCATED",
                        **result.diagnostics,
                    )
                    continue
                if result.output_error:
                    raise RuleError(
                        result.output_error, "Resposta incompleta.",
                        details={
                            **result.diagnostics,
                            "reason_code": "GENERATION_CALL_LIMIT" if batch == max_batches - 1 else
                            "TRUNCATION_RECOVERY_LIMIT" if result.output_error == "GENERATION_TRUNCATED" else
                            result.output_error,
                        },
                    )
                record_progress(op, "validating_result")
                if conversational:
                    try:
                        turn = parse_turn(
                            result.text, files, allow_deletions=allow_deletions, full_files=full_files,
                            confirmed_deletions=confirmed_deletions,
                        )
                        if turn.action == "implement" and turn.complete and turn.files == original:
                            raise InvalidTurn("NO_CHANGES")
                    except InvalidTurn as exc:
                        observe(
                            "generation_response_invalid", op, "warning", error=exc,
                            phase="validation", reason_code=exc.reason,
                        )
                        if repairs < recovery["max_response_repairs"] and batch < max_batches - 1:
                            repairs += 1
                            feedback = exc.reason + (f" no arquivo {exc.path}" if exc.path else "")
                            if exc.reason in {"EDIT_TARGET_MISMATCH", "FULL_FILES_REQUIRED"}:
                                full_files = True
                            record_progress(op, "repairing_response")
                            continue
                        code = (
                            "GENERATION_EDIT_MISMATCH"
                            if exc.reason in {"EDIT_TARGET_MISMATCH", "FULL_FILES_REQUIRED"}
                            else "GENERATION_DELETE_MISMATCH"
                            if exc.reason == "DELETE_TARGET_MISMATCH"
                            else exc.code
                        )
                        raise RuleError(code, exc.message) from None
                    feedback = None
                    validate_file_entries({"response.txt": turn.message}, repo.sources.secrets)
                    if turn.action == "clarify":
                        checkpoint_clarification(op, files, original, messages, repo.sources.secrets)
                        op.result = {
                            **op.result,
                            "kind": "clarification",
                            "message": turn.message,
                            "changed_files": [],
                        }
                        if finish(db, op, job, "succeeded", stage="awaiting_answer"):
                            observe("generation_finished", op, phase="clarification", changed_count=0)
                        return
                    next_files, complete = turn.files, turn.complete
                else:
                    next_files, complete = apply_batch(result.text, files)
                validate_file_entries(next_files, repo.sources.secrets)
                p = repo.owned_project(db, a.id, op.project_id)
                try:
                    validate_revision_history(
                        repo, db, p, next_files, base_revision_id=op.payload["base_revision_id"],
                        baseline_files=original, operation="generation_batch",
                    )
                    if complete:
                        validate_revision_migrations(
                            repo, db, p, next_files, base_revision_id=op.payload["base_revision_id"],
                            baseline_files=original, operation="generation_result",
                        )
                except RuleError as exc:
                    if not exc.code.startswith("MIGRATION_"):
                        raise
                    observe("generation_response_invalid", op, "warning", error=exc, phase="validation", reason_code=exc.code)
                    if (
                        exc.code != "MIGRATION_BASELINE_INCONSISTENT"
                        and repairs < recovery["max_response_repairs"]
                        and batch < max_batches - 1
                    ):
                        repairs += 1
                        issues = (exc.details.get("migration_history") or exc.details.get("migration") or {}).get("issues", [])
                        issue = issues[0] if issues else {}
                        feedback = exc.code + (f" no arquivo {issue['path']}" if issue.get("path") else "")
                        if isinstance(issue.get("statement_index"), int):
                            feedback += f" na instrução {issue['statement_index']}"
                        if isinstance(issue.get("reason"), str):
                            feedback += f" ({issue['reason']})"
                        feedback += ": " + exc.message
                        record_progress(op, "repairing_response")
                        continue
                    raise
                feedback = None
                if conversational and turn.message and turn.message not in messages:
                    messages.append(turn.message)
                if next_files == files and not complete:
                    raise RuleError("GENERATION_NO_CHANGES", "O lote não trouxe alterações.")
                confirmed_deletions.update(files.keys() - next_files.keys())
                confirmed_deletions.difference_update(next_files)
                files = next_files
                accepted_batches += 1
                changed_paths = [
                    path for path in sorted(original.keys() | files.keys())
                    if original.get(path) != files.get(path)
                ]
                changed_count = len(changed_paths)
                observe(
                    "generation_batch_validated", op, phase="validation", changed_count=changed_count,
                    status="complete" if complete else "incomplete",
                )
                if not complete:
                    if batch == max_batches - 1:
                        raise RuleError(
                            "GENERATION_LIMIT",
                            "A construção excedeu o limite desta solicitação.",
                            details={"reason_code": "GENERATION_CALL_LIMIT"},
                        )
                    if conversational:
                        op.result = {
                            **op.result,
                            "working_files": changed_paths,
                        }
                    record_progress(op, "continuing_code")
                    continue
                changed = [
                    {
                        "path": path,
                        "change": "deleted" if path not in files else "modified" if path in original else "created",
                    }
                    for path in changed_paths
                ]
                if not changed:
                    raise RuleError("GENERATION_NO_CHANGES", "Nenhuma alteração produzida.")
                validate_files(files, stack, repo.sources.secrets)
                p = repo.owned_project(db, a.id, op.project_id)
                if conversational:
                    if not finish(db, op, job, "succeeded", stage="saving_revision"):
                        return
                    message = (
                        "\n\n".join(messages) or "Implementei as alterações nos arquivos indicados abaixo."
                    )
                    stage = save_turn(repo, db, op, p, files, original, changed, message[:16000])
                    record_progress(op, stage)
                    observe("generation_finished", op, phase="revision", changed_count=len(changed))
                    return
                r = repo.add_revision(
                    db, p, files, "generation", op.payload["base_revision_id"], proposal=True
                )
                op.result = {
                    **op.result,
                    "revision_id": r.id,
                    "candidate": True,
                    "changed_files": changed,
                    "validation": "structure_checked",
                    **r.migration_evidence,
                }
                if finish(db, op, job, "succeeded"):
                    observe("generation_finished", op, phase="revision", changed_count=len(changed))
            except RuleError as exc:
                finish(db, op, job, "failed", exc.code, failure=exc, phase="validation")
            return
