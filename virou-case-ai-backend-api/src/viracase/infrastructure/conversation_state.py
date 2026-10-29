from sqlalchemy import select

from viracase.application.conversation import (
    context_input_bound,
    generation_batch_budget,
    generation_call_limit,
)
from viracase.application.conversation_turn import turn_prompt
from viracase.domain.rules import RuleError
from viracase.infrastructure.clarifications import continuation_context, continuation_files
from viracase.infrastructure.drafts import Drafts
from viracase.infrastructure.guided_build import attachment_context
from viracase.infrastructure.models import Operation
from viracase.infrastructure.publication_migration_context import published_migration_context
from viracase.infrastructure.repository_dto import operation_request_text
from viracase.infrastructure.workflow_logging import emit as emit_workflow


def prepare_turn(repo, db, operation, project):
    from viracase.infrastructure.repository import project_dto

    base = operation.payload["base_revision_id"]
    files = repo.sources.read(repo.owned_revision(db, operation.account_id, project.id, base), project.stack)
    baseline_files = dict(files)
    drafts = Drafts(repo, None)
    draft = drafts.row(db, operation.account_id, "project", project.id)
    if (
        draft
        and draft.data.get("files") is not None
        and (draft.data.get("base_revision_id") != base or draft.data["files"] != files)
    ):
        raise RuleError("DRAFT_NOT_SAVED", "Salve as alterações do rascunho antes de enviar o pedido.", 409)
    reply = operation.payload.get("reply_to")
    if reply:
        context = continuation_context(db, operation.account_id, project, operation.payload)
        operation.payload = {**operation.payload, **context, "project_version": project.version}
        files, _ = continuation_files(operation.payload, files, repo.sources.secrets)
        context = operation.payload["history"]
    else:
        context = recent_history(db, operation.account_id, project.id)
        operation.payload = {**operation.payload, "history": context, "project_version": project.version}
    try:
        migrations = published_migration_context(repo, db, project, files, base_revision_id=base, baseline_files=baseline_files)
        operation.payload = {**operation.payload, "publication_migration_context": migrations}
        prompt = turn_prompt(
            project_dto(project),
            attachment_context(operation.payload),
            files,
            history=context,
            output_tokens=operation.policy["max_output_tokens"],
            input_bound=context_input_bound(operation.policy, operation.payload),
            max_batches=generation_call_limit(operation.policy),
            character_budget=generation_batch_budget(
                operation.policy, input_bound=context_input_bound(operation.policy, operation.payload)
            ),
            request_limit_bytes=operation.payload.get("context_limit_bytes", 60000)
            if operation.policy.get("generation_context_version") == 2
            else None,
            allow_deletions=bool(operation.payload.get("analysis_id")),
            published_migrations=migrations,
        )
        if (
            operation.policy.get("generation_context_version") != 2
            and len(prompt.encode()) > operation.payload.get("context_limit_bytes", 60000)
        ):
            raise RuleError(
                "CONTEXT_LIMIT_EXCEEDED",
                "Mensagem, anexos, histórico e arquivos excedem o limite configurado. Reduza o conteúdo antes de enviar.",
                400,
                {"context_bytes": len(prompt.encode())},
            )
    except RuleError as exc:
        emit_workflow(
            "generation_context_rejected",
            "error",
            operation=operation,
            error=exc,
            secrets=repo.sources.secrets,
            phase="admission",
            state="rejected",
            transaction_state="not_reserved",
            **exc.details,
        )
        raise


def recent_history(db, actor, pid):
    history = db.scalars(
        select(Operation)
        .where(
            Operation.account_id == actor,
            Operation.project_id == pid,
            Operation.type == "generation",
            Operation.state.in_(["succeeded", "failed", "cancelled"]),
        )
        .order_by(Operation.created_at.desc(), Operation.id.desc())
        .limit(4)
    ).all()
    return [
        {
            "request": (operation_request_text(item) or "")[:3000],
            "response": item.result.get("message", "")[:2000],
            "outcome": item.result.get("kind") or item.state,
            "applied": bool(
                item.result.get("applied_revision_id")
                or (item.result.get("revision_id") and not item.result.get("candidate"))
            ),
        }
        for item in reversed(history)
    ]


def save_turn(repo, db, operation, project, files, original, changed, message):
    drafts = Drafts(repo, None)
    draft = drafts.row(db, operation.account_id, "project", project.id)
    current = draft.data if draft else None
    conflict = (
        project.current_revision_id != operation.payload["base_revision_id"]
        or project.version != operation.payload.get("project_version")
        or (
            current is not None
            and current.get("files") is not None
            and (
                current.get("base_revision_id") != operation.payload["base_revision_id"]
                or current["files"] != original
            )
        )
    )
    revision = repo.add_revision(
        db,
        project,
        files,
        "generation",
        operation.payload["base_revision_id"],
        proposal=bool(conflict or operation.payload.get("review_required")),
    )
    operation.result = {
        **operation.result,
        "kind": "implementation",
        "message": message,
        "revision_id": revision.id,
        "candidate": revision.candidate,
        "changed_files": changed,
        "validation": "structure_checked",
        **revision.migration_evidence,
    }
    if operation.payload.get("review_required") and not conflict:
        return "proposal_ready"
    if conflict:
        operation.result = {**operation.result, "apply_conflict": True}
        return "proposal_ready"
    operation.result = {**operation.result, "applied_revision_id": revision.id}
    if draft:
        drafts.write(
            db,
            operation.account_id,
            "project",
            project.id,
            draft.version,
            {**current, "base_revision_id": revision.id, "files": None},
        )
    return "changes_applied"
