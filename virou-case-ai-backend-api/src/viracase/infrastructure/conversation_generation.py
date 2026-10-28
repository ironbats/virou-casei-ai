import json
import logging
from datetime import timedelta

from sqlalchemy import select

from viracase.application.conversation import MAX_BATCHES, apply_batch, prompt_for
from viracase.application.conversation_turn import InvalidTurn, parse_turn, turn_prompt
from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, cost, utcnow, validate_file_entries, validate_files
from viracase.infrastructure.conversation_state import save_turn
from viracase.infrastructure.guided_build import attachment_context
from viracase.infrastructure.models import Attempt, Job, Operation
from viracase.infrastructure.provider_errors import report_rejection
from viracase.infrastructure.providers import ProviderRejected, ProviderUncertain
from viracase.infrastructure.repository import project_dto


def execute_conversation(worker, oid, token):
    repo = worker.repo
    total = {"input": 0, "cached": 0, "output": 0}
    calls, files, original, smaller = [], None, None, False
    feedback, repairs, messages = None, 0, []
    log = logging.getLogger("viracase.generation")

    def finish(db, op, job, state, error=None, stage=None):
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
            return False
        op.state, op.error_code, job.state = state, error, "done"
        record_progress(op, stage or ("proposal_ready" if state == "succeeded" else state))
        return True

    for batch in range(MAX_BATCHES):
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
                prompt = prompt_for(
                    project_dto(p),
                    attachment_context(op.payload),
                    files,
                    batch,
                    smaller,
                    output_tokens=op.policy["max_output_tokens"],
                )
            except RuleError as exc:
                finish(db, op, job, "failed", exc.code)
                return
            conversational = op.policy.get("generation_contract_version") == 4
            if conversational:
                try:
                    prompt = turn_prompt(
                        project_dto(p),
                        attachment_context(op.payload),
                        files,
                        batch,
                        smaller,
                        op.policy["max_output_tokens"],
                        op.payload.get("history", []),
                        feedback,
                    )
                except RuleError as exc:
                    finish(db, op, job, "failed", exc.code)
                    return
            policy, stack = dict(op.policy), p.stack
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
            job.lease_until = utcnow() + timedelta(seconds=repo.config.provider_timeout * 2 + 120)
        try:
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
                else:
                    report_rejection(oid, exc)
                    attempt.state = "confirmed"
                    attempt.actual_brl = cost(policy, total)
                    finish(db, op, job, "failed", exc.error_code)
            return
        total = {k: total[k] + result.usage[k] for k in total}
        calls.append(
            {"external_id": result.external_id, "usage": result.usage, "output_error": result.output_error}
        )
        # Complete chunks live only in this job. Never expose or save truncated output as a revision.
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
            if op.cancel_requested:
                finish(db, op, job, "cancelled")
                return
            try:
                repo.access(db, a, "write")
                if result.output_error == "GENERATION_TRUNCATED" and not smaller and batch < MAX_BATCHES - 1:
                    smaller = True
                    record_progress(op, "resizing_code")
                    continue
                if result.output_error:
                    raise RuleError(result.output_error, "Resposta incompleta.")
                record_progress(op, "validating_result")
                if conversational:
                    try:
                        turn = parse_turn(result.text, files)
                        if turn.action == "implement" and turn.complete and turn.files == original:
                            raise InvalidTurn("NO_CHANGES")
                    except InvalidTurn as exc:
                        log.warning(
                            json.dumps(
                                {
                                    "event": "generation_response_invalid",
                                    "operation_id": oid,
                                    "batch": batch + 1,
                                    "reason_code": exc.reason,
                                }
                            )
                        )
                        if repairs < 2 and batch < MAX_BATCHES - 1:
                            repairs += 1
                            feedback = exc.reason
                            record_progress(op, "repairing_response")
                            continue
                        code = (
                            "GENERATION_EDIT_MISMATCH" if exc.reason == "EDIT_TARGET_MISMATCH" else exc.code
                        )
                        raise RuleError(code, exc.message) from None
                    feedback = None
                    validate_file_entries({"response.txt": turn.message}, repo.sources.secrets)
                    if turn.action == "clarify":
                        op.result = {
                            **op.result,
                            "kind": "clarification",
                            "message": turn.message,
                            "changed_files": [],
                        }
                        finish(db, op, job, "succeeded", stage="awaiting_answer")
                        return
                    next_files, complete = turn.files, turn.complete
                    if turn.message and turn.message not in messages:
                        messages.append(turn.message)
                else:
                    next_files, complete = apply_batch(result.text, files)
                validate_file_entries(next_files, repo.sources.secrets)
                if next_files == files and not complete:
                    raise RuleError("GENERATION_NO_CHANGES", "O lote não trouxe alterações.")
                files = next_files
                if not complete:
                    if batch == MAX_BATCHES - 1:
                        raise RuleError(
                            "GENERATION_LIMIT", "A construção excedeu o limite desta solicitação."
                        )
                    if conversational:
                        op.result = {
                            **op.result,
                            "working_files": [
                                path for path in sorted(files) if files[path] != original.get(path)
                            ],
                        }
                    record_progress(op, "continuing_code")
                    continue
                changed = [
                    {"path": path, "change": "modified" if path in original else "created"}
                    for path, content in sorted(files.items())
                    if original.get(path) != content
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
                }
                finish(db, op, job, "succeeded")
            except RuleError as exc:
                finish(db, op, job, "failed", exc.code)
            return
