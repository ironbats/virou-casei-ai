from sqlalchemy import select

from viracase.application.conversation_turn import turn_prompt
from viracase.domain.rules import RuleError
from viracase.infrastructure.drafts import Drafts
from viracase.infrastructure.guided_build import attachment_context
from viracase.infrastructure.models import Operation


def prepare_turn(repo, db, operation, project):
    from viracase.infrastructure.repository import project_dto

    base = operation.payload["base_revision_id"]
    files = repo.sources.read(repo.owned_revision(db, operation.account_id, project.id, base), project.stack)
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
        previous = db.get(Operation, reply)
        if (
            not previous
            or previous.account_id != operation.account_id
            or previous.project_id != project.id
            or previous.type != "generation"
            or previous.state != "succeeded"
            or previous.result.get("kind") != "clarification"
        ):
            raise RuleError("NOT_FOUND", "Pergunta indisponível nesta conversa.", 404)
        if previous.payload.get("base_revision_id") != base:
            raise RuleError(
                "CONTEXT_CHANGED", "O projeto mudou depois da pergunta. Envie um novo pedido.", 409
            )
    history = db.scalars(
        select(Operation)
        .where(
            Operation.account_id == operation.account_id,
            Operation.project_id == project.id,
            Operation.type == "generation",
            Operation.state.in_(["succeeded", "failed", "cancelled"]),
        )
        .order_by(Operation.created_at.desc(), Operation.id.desc())
        .limit(4)
    ).all()
    if reply and all(item.id != reply for item in history):
        history = [*history[:3], previous]
    context = [
        {
            "request": (
                item.payload.get("brief", "") if item.id == reply else item.payload.get("brief", "")[:3000]
            ),
            "response": (
                item.result.get("message", "") if item.id == reply else item.result.get("message", "")[:2000]
            ),
            "outcome": item.result.get("kind") or item.state,
            "applied": bool(
                item.result.get("applied_revision_id")
                or (item.result.get("revision_id") and not item.result.get("candidate"))
            ),
        }
        for item in reversed(history)
    ]
    operation.payload = {**operation.payload, "history": context, "project_version": project.version}
    prompt = turn_prompt(
        project_dto(project),
        attachment_context(operation.payload),
        files,
        history=context,
        output_tokens=operation.policy["max_output_tokens"],
    )
    if len(prompt.encode()) > operation.payload.get("context_limit_bytes", 60000):
        raise RuleError(
            "CONTEXT_LIMIT_EXCEEDED",
            "Mensagem, anexos, histórico e arquivos excedem o limite configurado. Reduza o conteúdo antes de enviar.",
            400,
        )


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
