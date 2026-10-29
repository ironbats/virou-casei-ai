import copy

from sqlalchemy import select

from viracase.domain.rules import RuleError, canonical_hash, validate_file_entries
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Operation
from viracase.infrastructure.repository_dto import operation_request_text


def clarification_chain(db, actor, project, identity, base):
    if project.current_revision_id != base:
        raise RuleError("CONTEXT_CHANGED", "O projeto mudou depois da pergunta. Envie um novo pedido.", 409)
    chain, seen = [], set()
    while identity:
        previous = db.get(Operation, identity)
        if (
            identity in seen
            or not previous
            or previous.account_id != actor
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
        chain.append(previous)
        seen.add(identity)
        identity = previous.payload.get("reply_to")
    return list(reversed(chain))


def continuation_context(db, actor, project, payload):
    chain = clarification_chain(db, actor, project, payload["reply_to"], payload["base_revision_id"])
    answered = db.scalar(
        select(Operation.id).where(
            Operation.account_id == actor,
            Operation.project_id == project.id,
            Operation.type == "generation",
            Operation.state.in_(["queued", "running", "reconciling", "succeeded"]),
            Operation.payload["reply_to"].as_string() == chain[-1].id,
        )
    )
    if answered:
        raise RuleError(
            "CLARIFICATION_ANSWERED",
            "Esta pergunta já recebeu uma resposta. Acompanhe a continuação no chat.",
            409,
        )
    pending = db.scalars(
        select(Operation).where(
            Operation.account_id == actor,
            Operation.project_id == project.id,
            Operation.type == "generation",
            Operation.state == "succeeded",
        )
    ).all()
    if any(
        item.result.get("candidate")
        and not item.result.get("applied_revision_id")
        and not item.result.get("discarded_at")
        for item in pending
    ):
        raise RuleError(
            "PROPOSAL_PENDING", "Revise e aplique ou descarte a proposta anterior antes de responder.", 409
        )
    attachments, identities = [], set()
    for source in [*(item.payload for item in chain), payload]:
        for snapshot in source.get("attachment_snapshots", []):
            digest = canonical_hash(snapshot)
            if digest not in identities:
                attachments.append(copy.deepcopy(snapshot))
                identities.add(digest)
    history = copy.deepcopy(chain[0].payload.get("history", []))
    history.extend(
        {
            "request": operation_request_text(item) or "",
            "response": item.result.get("message", ""),
            "outcome": "clarification",
            "applied": False,
        }
        for item in chain
    )
    checkpoint = chain[-1].payload.get("clarification_checkpoint")
    result = {
        "history": history,
        "attachment_snapshots": attachments,
        "clarification_checkpoint": copy.deepcopy(checkpoint),
    }
    parent = chain[-1]
    if parent.payload.get("analysis_id"):
        from viracase.infrastructure.code_analysis import correction_context, owned_analysis

        analysis = owned_analysis(db, actor, project.id, parent.payload["analysis_id"])
        result.update(
            analysis_id=analysis.id,
            analysis_context=copy.deepcopy(parent.payload.get("analysis_context") or correction_context(analysis)),
            auto_apply=False,
        )
    return result


def continuation_files(payload, original, secrets):
    checkpoint = payload.get("clarification_checkpoint")
    if checkpoint is None:
        return dict(original), []
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("version") != 1
        or checkpoint.get("base_revision_id") != payload.get("base_revision_id")
        or checkpoint.get("base_hash") != canonical_hash(original)
        or not isinstance(checkpoint.get("files"), dict)
        or not isinstance(checkpoint.get("messages"), list)
        or any(not isinstance(message, str) for message in checkpoint["messages"])
        or checkpoint.get("hash")
        != canonical_hash({key: value for key, value in checkpoint.items() if key != "hash"})
    ):
        raise RuleError(
            "GENERATION_CHECKPOINT_INVALID",
            "Não foi possível validar o contexto da construção. O código salvo foi preservado.",
            409,
        )
    validate_file_entries(checkpoint["files"], secrets)
    validate_file_entries({"response.txt": "\n\n".join(checkpoint["messages"])}, secrets)
    return dict(checkpoint["files"]), list(checkpoint["messages"])


def checkpoint_clarification(operation, files, original, messages, secrets):
    payload = {key: value for key, value in operation.payload.items() if key != "clarification_checkpoint"}
    if files != original:
        validate_file_entries(files, secrets)
        checkpoint = {
            "version": 1,
            "base_revision_id": operation.payload["base_revision_id"],
            "base_hash": canonical_hash(original),
            "files": dict(files),
            "messages": list(messages),
        }
        checkpoint["hash"] = canonical_hash(checkpoint)
        payload["clarification_checkpoint"] = checkpoint
    operation.payload = payload


class Clarifications:
    def __init__(self, repository):
        self.repo = repository

    def reply(self, actor, pid, oid, key, request, generation):
        intention = {**request, "reply_to": oid}
        previous = self.repo.recover_intention(actor, f"generation:{pid}", key, intention)
        if previous:
            return self.repo.operation(actor, previous["id"])
        construction = Integrations(self.repo).build_configuration()
        policy = construction["settings"]
        if len(request["message_text"]) > policy["max_message_chars"]:
            raise RuleError("DRAFT_LIMIT_EXCEEDED", "Resposta acima do limite configurado.", 400)
        payload = {
            "brief": request["message_text"],
            "base_revision_id": request["base_project_revision"],
            "policy_version": request["policy_version"],
            "reply_to": oid,
            "reply_request": intention,
            "review_required": True,
            "context_limit_bytes": policy["max_context_bytes"],
            "generation_context_limit_bytes": policy["max_generation_context_bytes"],
            "construction_configuration": construction,
        }
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "generate")
            project = self.repo.owned_project(db, actor, pid)
            payload.update(continuation_context(db, actor, project, payload))
            parent = db.get(Operation, oid)
            for field in ("correlation_id", "preparation_id", "review_snapshot_hash", "review_snapshot"):
                if field in parent.payload:
                    payload[field] = copy.deepcopy(parent.payload[field])
        return generation.start(actor, pid, key, payload, contract_version=4)
