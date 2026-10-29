import copy
import re
from decimal import Decimal
from pathlib import PurePosixPath

from sqlalchemy import select

from viracase.application.template_preparation import preparation_input_bytes
from viracase.application.template_preparation_proposals import build_proposals
from viracase.domain.rules import RuleError, canonical_hash, cost, money, utcnow
from viracase.domain.template_fields import (
    attachment_errors,
    empty,
    normalized_fields,
    render,
)
from viracase.infrastructure.agent_execution import freeze_execution
from viracase.infrastructure.feature_policies import FeaturePolicies
from viracase.infrastructure.guided_build import GuidedBuild
from viracase.infrastructure.integration_traces import TraceStore, trace_context
from viracase.infrastructure.models import Operation
from viracase.infrastructure.repository_dto import iso
from viracase.infrastructure.workflow_logging import current_context, emit

ACTIVE = {"queued", "running", "reconciling"}
MESSAGES = {
    "queued": "Preparação recebida.",
    "filling": "Preparando sugestões para os campos selecionados.",
    "ready": "Sugestões prontas para sua revisão.",
    "needs_input": "Responda às perguntas para completar a preparação.",
    "failed": "A preparação não foi concluída. Revise o motivo antes de tentar novamente.",
    "cancelled": "Preparação cancelada. Nenhum campo foi aplicado automaticamente.",
    "cancellation_requested": "Cancelamento solicitado. A chamada enviada ainda pode ter consumo.",
    "reconciling": "O resultado da chamada ainda é incerto. Aguarde a reconciliação antes de repetir.",
    "stale": "O contexto mudou. Prepare novamente os anexos selecionados.",
}
ERROR_MESSAGES = {
    "COMMERCIAL_CONFIGURATION_PENDING": "O preenchimento está indisponível até o operador habilitar uma integração e um modelo válidos.",
    "PREPARATION_DISABLED": "O preenchimento com IA está desabilitado pelo operador. Você pode continuar editando os campos.",
    "PROVIDER_CONFIGURATION_INVALID": "A integração de IA rejeitou a configuração de preenchimento. Peça ao operador para conferir o modelo e suas opções.",
    "PROVIDER_ACCESS_DENIED": "A integração de IA negou acesso. Peça ao operador para conferir a credencial e as permissões do modelo.",
    "PROVIDER_MODEL_UNAVAILABLE": "O modelo configurado não está disponível para esta integração. Peça ao operador para conferir o modelo.",
    "PROVIDER_CAPACITY_EXCEEDED": "A integração de IA atingiu seu limite de uso. Tente novamente mais tarde ou peça ao operador para conferir a capacidade.",
    "PROVIDER_REQUEST_TOO_LARGE": "O provedor recusou o tamanho deste pedido. Reduza os templates selecionados ou o contexto e tente novamente.",
    "PREPARATION_CONTEXT_LIMIT": "O pedido excede o limite de contexto configurado. Reduza os templates selecionados ou o texto antes de tentar novamente.",
    "PREPARATION_TRUNCATED": "A resposta atingiu o limite de saída configurado e ficou incompleta. Selecione menos templates ou peça ao operador para revisar o limite antes de repetir.",
    "PREPARATION_BLOCKED": "O provedor bloqueou a resposta. Revise o pedido antes de tentar preencher novamente.",
    "PREPARATION_INVALID_RESPONSE": "O provedor retornou uma resposta incompatível com os campos. Os valores já validados foram preservados; tente preencher os restantes novamente.",
    "PREPARATION_FIELD_TYPE": "O provedor retornou um valor incompatível com este campo. Tente preenchê-lo novamente ou informe o valor manualmente.",
    "PREPARATION_INVALID_SOURCE": "Não foi possível confirmar a fonte de um dos valores sugeridos. Os demais valores válidos foram preservados.",
    "PREPARATION_UNGROUNDED_VALUE": "Uma sugestão não estava sustentada pelo pedido ou pelo projeto e não foi aplicada. Os demais valores válidos foram preservados.",
    "PREPARATION_FIELD_PLACEHOLDER": "O provedor deixou um campo sem conteúdo concreto. Os demais valores válidos foram preservados.",
    "PROVIDER_RESULT_UNKNOWN": "O resultado da chamada ainda é incerto. Aguarde a conciliação antes de repetir o preenchimento.",
    "COST_DISCREPANCY": "O uso informado pelo provedor está em conciliação. Aguarde antes de repetir o preenchimento.",
    "PREPARATION_STALE": "O pedido ou a revisão do projeto mudou. Prepare a construção novamente com o contexto atual.",
    "PROCESSING_FAILED": "O preenchimento encontrou uma falha de processamento. Suas edições foram preservadas; consulte o suporte com o código do erro.",
}


def preparation_error_message(code):
    return ERROR_MESSAGES.get(
        code, "Não foi possível concluir o preenchimento. Suas edições foram preservadas."
    )


class TemplatePreparations:
    def __init__(self, repo, learning=None, catalog=None):
        self.repo, self.catalog = repo, catalog
        self.guided = GuidedBuild(repo, learning)
        self.policies = FeaturePolicies(repo, catalog)

    def capability(self):
        settings = self.policies.current("template_preparation")["settings"]
        enabled = (
            settings["enabled"]
            and not self.repo.config.restore_hold
            and any(
                not settings["model_id"]
                or settings["model_id"] == item.get("model_id", item["provider"] + ":" + item["model"])
                for item in self.catalog.eligible()
            )
        )
        return {
            "enabled": bool(enabled),
            "reason_code": None
            if enabled
            else "PREPARATION_DISABLED"
            if not settings["enabled"]
            else "COMMERCIAL_CONFIGURATION_PENDING",
        }

    def owned(self, db, actor, pid, identity):
        self.repo.owned_project(db, actor, pid)
        row = db.scalar(
            select(Operation).where(
                Operation.id == identity,
                Operation.account_id == actor,
                Operation.project_id == pid,
                Operation.type == "template_preparation",
            )
        )
        if not row:
            raise RuleError("NOT_FOUND", "Preparação indisponível.", 404)
        return row

    def current_draft(self, db, actor, pid):
        row = self.guided.drafts.row(db, actor, "build", pid)
        if not row:
            raise RuleError("DRAFT_CONFLICT", "Salve o pedido antes de preparar os templates.", 412)
        return row, self.guided.validate(db, actor, row.data, previous=row.data)

    def stale(self, db, operation, draft):
        project = self.repo.owned_project(db, operation.account_id, operation.project_id)
        return project.current_revision_id != operation.payload.get("base_revision_id") or canonical_hash(
            {key: draft.get(key, "") for key in ("objective", "message_text")}
        ) != operation.payload.get("request_context_hash")

    def dto(self, db, operation):
        data = operation.result.get("preparation", {})
        state = {
            "queued": "queued",
            "running": "filling",
            "failed": "failed",
            "cancelled": "cancelled",
            "reconciling": "reconciling",
        }.get(operation.state, data.get("state", "failed"))
        row = self.guided.drafts.row(db, operation.account_id, "build", operation.project_id)
        current = row.data if row else {}
        changed = self.stale(db, operation, current)
        items = copy.deepcopy(data.get("items", operation.payload.get("public_items", [])))
        current_items = {item["template_id"]: item for item in current.get("attachments", [])}
        snapshots = {item["attachment_id"]: item for item in operation.payload.get("items", [])}
        for item in items:
            existing, snapshot = current_items.get(item["attachment_id"]), snapshots[item["attachment_id"]]
            compatible = bool(
                existing
                and existing.get("enabled")
                and existing["source_version_id"] == snapshot["source_version_id"]
                and existing.get(
                    "attachment_instance_id",
                    canonical_hash(
                        [operation.account_id, existing["template_id"], existing["source_version_id"]]
                    )[:32],
                )
                == snapshot["attachment_instance_id"]
            )
            for field in item["suggested_fields"]:
                field["compatible"] = (
                    not changed
                    and compatible
                    and existing.get("fields", {}).get(field["field_id"]) == field["original_value"]
                    and existing.get("field_values", {}).get(field["field_id"], {}).get("field_revision", 1)
                    == snapshot.get("original_field_revisions", {}).get(field["field_id"], 1)
                    and not existing.get("field_values", {}).get(field["field_id"], {}).get("edited_by_user")
                )
                if not field["compatible"]:
                    field["reason"] = "O campo ou o anexo mudou desde a preparação."
            if not compatible and operation.state == "succeeded":
                item["state"] = "stale"
            elif operation.state in ACTIVE or operation.state in {"failed", "cancelled"}:
                item["state"] = state
        if changed and operation.state == "succeeded":
            state = "stale"
        if operation.cancel_requested and operation.state in ACTIVE:
            state = "cancellation_requested" if operation.state != "reconciling" else "reconciling"
        return {
            "id": operation.id,
            "project_id": operation.project_id,
            "state": state,
            "state_version": operation.result.get("preparation_version", 1),
            "draft_revision": operation.payload["draft_revision"],
            "project_revision": operation.payload["base_revision_id"],
            "attachment_revisions": {
                item["attachment_id"]: item["attachment_revision"] for item in operation.payload["items"]
            },
            "policy_version": operation.payload["preparation_policy_version"],
            "context_summary": operation.payload.get("context_summary"),
            "items": items,
            "progress": {
                "completed": len(items) if operation.state in {"succeeded", "failed", "cancelled"} else 0,
                "total": len(items),
            },
            "correlation_id": operation.payload["correlation_id"],
            "created_at": iso(operation.created_at),
            "updated_at": iso(operation.updated_at),
            "completed_at": data.get("completed_at"),
            "error_code": operation.error_code,
            "message": preparation_error_message(operation.error_code)
            if operation.error_code and state in {"failed", "reconciling"}
            else MESSAGES[state],
            "can_apply": state in {"ready", "needs_input"}
            and any(
                field["compatible"] and field["state"] == "suggested"
                for item in items
                for field in item["suggested_fields"]
            ),
        }

    def get(self, actor, pid, identity):
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "read")
            return self.dto(db, self.owned(db, actor, pid, identity))

    def listing(self, actor, pid):
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "read")
            self.repo.owned_project(db, actor, pid)
            rows = db.scalars(
                select(Operation)
                .where(
                    Operation.account_id == actor,
                    Operation.project_id == pid,
                    Operation.type == "template_preparation",
                )
                .order_by(Operation.created_at.desc(), Operation.id.desc())
                .limit(20)
            ).all()
            return {"items": [self.dto(db, row) for row in rows], "next_cursor": None}

    def clean_context(self, value):
        for secret in self.repo.sources.secrets:
            if secret and len(secret) > 12:
                value = value.replace(secret, "[SEGREDO REMOVIDO]")
        value = re.sub(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}|\bAIza[\w-]{30,}|\bghp_[\w]{30,}|(?i:bearer)\s+[A-Za-z0-9._~-]{15,}",
            "[SEGREDO REMOVIDO]",
            value,
        )
        value = re.sub(
            r'(?im)((?:["\']?)(?:password|senha|secret|client_secret|private_key|api[_-]?key|access[_-]?token|database_url)(?:["\']?)\s*[=:]\s*)[^\r\n,;]+',
            r"\1[SEGREDO REMOVIDO]",
            value,
        )
        return re.sub(
            r'(?i)(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s"\']+', "[CONEXÃO PROTEGIDA]", value
        )

    def clean_definition(self, value):
        if isinstance(value, str):
            return self.clean_context(value)
        if isinstance(value, list):
            return [self.clean_definition(item) for item in value]
        if isinstance(value, dict):
            return {key: self.clean_definition(item) for key, item in value.items()}
        return value

    def start(self, actor, pid, key, request, parent=None, answers=None, automatic=False, schedule_id=None):
        intention = (
            {**request, "parent_preparation_id": parent, "answers": answers or []} if parent else request
        )
        previous = self.repo.recover_intention(actor, "template_preparation:" + pid, key, intention)
        if previous:
            return self.get(actor, pid, previous["id"])
        self.repo.rate_limit("prepare:" + actor, 30, 3600)
        policy = self.policies.current("template_preparation")
        if not policy["settings"]["enabled"]:
            raise RuleError(
                "PREPARATION_DISABLED",
                "A preparação com IA está desabilitada. O preenchimento manual continua disponível.",
            )
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "generate")
            project = self.repo.owned_project(db, actor, pid)
            row, draft = self.current_draft(db, actor, pid)
            if (
                row.version != request["expected_draft_revision"]
                or project.current_revision_id != request["base_project_revision"]
            ):
                raise RuleError(
                    "DRAFT_CONFLICT", "O pedido ou a revisão mudou. Atualize antes de preparar.", 412
                )
            selected = request["attachment_ids"]
            if not selected or len(selected) != len(set(selected)):
                raise RuleError("NO_ATTACHMENTS_SELECTED", "Selecione os templates que deseja preparar.", 400)
            target = request.get("target_field_id")
            if target and len(selected) != 1:
                raise RuleError("PREPARATION_UNKNOWN_FIELD", "Escolha um campo de um único anexo.", 400)
            attachments = {item["template_id"]: item for item in draft["attachments"] if item["enabled"]}
            if set(selected) - attachments.keys() or set(request["expected_attachment_revisions"]) != set(
                selected
            ):
                raise RuleError("ATTACHMENT_CONFLICT", "Um anexo selecionado mudou ou foi removido.", 412)
            items = []
            for identity in selected:
                item = attachments[identity]
                if item["attachment_revision"] != request["expected_attachment_revisions"][identity]:
                    raise RuleError("ATTACHMENT_CONFLICT", "O anexo mudou. Revise a versão atual.", 412)
                template = self.guided.templates.authorized(db, actor, item["source_version_id"])
                definitions = normalized_fields(template.content, template.slug)
                definitions = [field for field in definitions if field["sensitivity"] != "secret"]
                context_fields = {
                    field["key"]: self.clean_definition(item["fields"].get(field["key"]))
                    for field in definitions
                }
                target_snapshot = None
                if target:
                    definitions = [field for field in definitions if field["key"] == target]
                    if not definitions:
                        raise RuleError(
                            "PREPARATION_UNKNOWN_FIELD", "O campo não pode ser gerado com IA.", 400
                        )
                    metadata = item.get("field_values", {}).get(target, {})
                    if not empty(item["fields"].get(target)) or metadata.get(
                        "field_revision", 1
                    ) != request.get("target_field_revision"):
                        raise RuleError("FIELD_CHANGED", "O campo mudou. Sua edição foi preservada.", 412)
                    target_snapshot = {
                        "field_id": target,
                        "field_revision": metadata.get("field_revision", 1),
                        "value": item["fields"].get(target),
                    }
                context_definitions = self.clean_definition(definitions)
                if any(
                    original.get("constraints", {}).get("enum") != safe.get("constraints", {}).get("enum")
                    for original, safe in zip(definitions, context_definitions, strict=True)
                ):
                    raise RuleError(
                        "PREPARATION_SENSITIVE_TEMPLATE",
                        "Um campo contém uma opção sensível. Preencha esse template manualmente ou revise sua definição.",
                        400,
                    )
                original_fields = {field["key"]: item["fields"].get(field["key"]) for field in definitions}
                items.append(
                    {
                        "attachment_id": identity,
                        "source_version_id": template.id,
                        "attachment_revision": item["attachment_revision"],
                        "attachment_instance_id": item["attachment_instance_id"],
                        "title": template.content["title"],
                        "definitions": definitions,
                        "context_definitions": context_definitions,
                        "context_title": self.clean_context(template.content["title"]),
                        "original_fields": original_fields,
                        "original_field_revisions": {
                            field["key"]: item.get("field_values", {})
                            .get(field["key"], {})
                            .get("field_revision", 1)
                            for field in definitions
                        },
                        "context_fields": context_fields,
                        **({"explicit_empty_target": target_snapshot} if target_snapshot else {}),
                    }
                )
            if not any(
                empty(item["original_fields"].get(field["key"]))
                for item in items
                for field in item["definitions"]
            ):
                raise RuleError(
                    "PREPARATION_NOT_NEEDED",
                    "Os campos selecionados já estão preenchidos. Revise-os antes de enviar.",
                    400,
                )
            context = [
                {
                    "id": "request:objective",
                    "type": "request",
                    "content": self.clean_context(draft["objective"]),
                },
                {
                    "id": "request:message",
                    "type": "request",
                    "content": self.clean_context(draft["message_text"]),
                },
            ]
            source = (
                self.repo.owned_revision(db, actor, pid, project.current_revision_id)
                if project.current_revision_id
                else None
            )
            files = self.repo.sources.read(source, project.stack) if source else {}
            history = db.scalars(
                select(Operation)
                .where(
                    Operation.account_id == actor,
                    Operation.project_id == pid,
                    Operation.type == "generation",
                    Operation.state == "succeeded",
                )
                .order_by(Operation.created_at.desc(), Operation.id.desc())
                .limit(20)
            ).all()
            conversation = []
            for turn in history:
                applied = turn.result.get("applied_revision_id") == project.current_revision_id
                if not applied and turn.payload.get("base_revision_id") != project.current_revision_id:
                    continue
                if turn.result.get("message"):
                    conversation.append(
                        {
                            "id": "conversation:" + turn.id + ":assistant",
                            "type": "policy_suggestion",
                            "content": self.clean_context(turn.result["message"]),
                        }
                    )
                if turn.payload.get("brief"):
                    conversation.append(
                        {
                            "id": "conversation:" + turn.id + ":user",
                            "type": "request",
                            "content": self.clean_context(turn.payload["brief"]),
                        }
                    )
            correlation = (
                draft.get("correlation_id")
                or draft.get("recommendation_id")
                or canonical_hash([actor, pid, key])[:32]
            )
            if parent:
                prior = self.owned(db, actor, pid, parent)
                correlation = prior.payload["correlation_id"]
                context.extend(item for item in prior.payload["context"] if item["type"] == "user_answer")
            for answer in answers or []:
                context.append(
                    {
                        "id": "answer:" + answer["question_id"],
                        "type": "user_answer",
                        "content": self.clean_context(answer["value"]),
                    }
                )
            payload = {
                "preparation_request": intention,
                "template_preparation_contract": 4,
                "draft_revision": row.version,
                "base_revision_id": project.current_revision_id,
                "request_context_hash": canonical_hash(
                    {key: draft.get(key, "") for key in ("objective", "message_text")}
                ),
                "draft_hash": canonical_hash(row.data),
                "items": items,
                "context": context,
                "correlation_id": correlation,
                "parent_preparation_id": parent,
                "preparation_policy_version": policy["version"],
                "preparation_settings": policy["settings"],
                "output_budget_tokens": policy["settings"]["max_output_tokens"],
                "request_id": draft.get("request_id"),
                "http_request_id": current_context().get("request_id"),
                "automatic_apply": automatic,
                "autofill_schedule_id": schedule_id,
                "public_items": [
                    {
                        "attachment_id": item["attachment_id"],
                        "source_version_id": item["source_version_id"],
                        "attachment_revision": item["attachment_revision"],
                        "title": item["title"],
                        "state": "queued",
                        "suggested_fields": [],
                        "questions": [],
                        "errors": [],
                    }
                    for item in items
                ],
            }
            if schedule_id:
                schedule = db.get(Operation, schedule_id)
                if schedule and schedule.account_id == actor and schedule.project_id == pid:
                    payload["http_request_id"] = schedule.payload.get("http_request_id")
        included_messages, omitted_messages = 0, max(0, len(conversation) - 3)
        omitted_units = 0
        for entry in reversed(conversation[:3]):
            payload["context"].append(entry)
            if preparation_input_bytes(payload) + 512 > policy["settings"]["max_context_bytes"]:
                payload["context"].pop()
                omitted_units += 1
                omitted_messages += 1
            else:
                included_messages += 1
        included_files, omitted_files = 0, 0
        for path, content in sorted(
            files.items(),
            key=lambda item: (not item[0].endswith((".py", ".ts", ".tsx", ".js", ".md")), item[0]),
        ):
            if PurePosixPath(path).suffix.lower() not in {
                ".py",
                ".ts",
                ".tsx",
                ".js",
                ".jsx",
                ".json",
                ".jsonc",
                ".md",
                ".html",
                ".css",
                ".sql",
            } or any(
                part.startswith(".")
                or any(name in part.lower() for name in ("credential", "secret", "token"))
                for part in PurePosixPath(path).parts
            ):
                omitted_files += 1
                continue
            entry = {
                "id": "file:" + canonical_hash(path)[:24],
                "type": "project_context",
                "path": path,
                "content": self.clean_context(content),
            }
            payload["context"].append(entry)
            if preparation_input_bytes(payload) + 512 > policy["settings"]["max_context_bytes"]:
                payload["context"].pop()
                omitted_units += 1
                omitted_files += 1
            else:
                included_files += 1
        payload["context_summary"] = {
            "included_files": included_files,
            "omitted_files": omitted_files,
            "included_conversation_messages": included_messages,
            "omitted_conversation_messages": omitted_messages,
            "reason": "CONTEXT_PARTIAL" if omitted_files or omitted_messages else None,
        }
        payload["context_summary"]["included_units"] = len(payload["context"])
        payload["context_summary"]["omitted_units"] = omitted_units
        payload["proposal_schema"] = build_proposals(payload)
        input_bytes = preparation_input_bytes(payload)
        if input_bytes > policy["settings"]["max_context_bytes"]:
            raise RuleError(
                "PREPARATION_CONTEXT_LIMIT", "Reduza o pedido ou prepare menos templates por vez.", 400
            )
        choices, execution_error = [], None
        for candidate in self.catalog.eligible():
            if candidate["provider"] not in {"openai", "gemini"}:
                continue
            if policy["settings"]["model_id"] and policy["settings"]["model_id"] != candidate.get(
                "model_id", candidate["provider"] + ":" + candidate["model"]
            ):
                continue
            candidate = {
                **candidate,
                "max_output_tokens": min(
                    candidate["max_output_tokens"], policy["settings"]["max_output_tokens"]
                ),
                "template_preparation_contract": 4,
                "preparation_proposal_schema": payload["proposal_schema"],
                "preparation_source_refs": [
                    item["id"]
                    for item in payload["context"]
                    if item["type"] in {"request", "project_context", "user_answer"}
                ],
                "reserved_input_bound": policy["settings"]["max_context_bytes"] + 4096,
            }
            try:
                candidate = freeze_execution(candidate, self.repo.config.provider_timeout)
            except RuleError as exc:
                execution_error = exc
                continue
            ceiling = cost(
                candidate,
                {
                    "input": candidate["reserved_input_bound"] * (1 + policy["settings"]["max_repairs"]),
                    "cached": 0,
                    "output": candidate["max_output_tokens"] * (1 + policy["settings"]["max_repairs"]),
                },
            )
            choices.append((money(ceiling * Decimal("1.10")), candidate))
        if not choices:
            if execution_error:
                raise execution_error
            raise RuleError(
                "COMMERCIAL_CONFIGURATION_PENDING",
                "O operador precisa validar e habilitar um modelo já configurado para preparação.",
            )
        ceiling, chosen = min(choices, key=lambda pair: pair[0])
        payload["output_budget_tokens"] = chosen["max_output_tokens"]
        operation = self.repo.create_operation(
            actor, pid, "template_preparation", key, payload, chosen, ceiling
        )
        with self.repo.sessions() as db:
            created = db.get(Operation, operation["id"])
            emit(
                "preparation_queued",
                operation=created,
                phase="configuration",
                state=created.state,
                attachment_count=len(items),
                field_count=sum(len(item["definitions"]) for item in items),
                context_bytes=input_bytes,
                max_output_tokens=chosen["max_output_tokens"],
                reserved_brl=ceiling,
            )
        return self.get(actor, pid, operation["id"])

    def freeze(self, db, actor, pid, payload):
        row, _ = self.current_draft(db, actor, pid)
        if row.version != payload["draft_revision"] or canonical_hash(row.data) != payload["draft_hash"]:
            raise RuleError(
                "DRAFT_CONFLICT", "O pedido mudou antes da reserva. Revise antes de preparar.", 412
            )
        if self.policies.read(db, "template_preparation")["version"] != payload["preparation_policy_version"]:
            raise RuleError(
                "POLICY_CHANGED", "A política mudou antes da preparação. Atualize e tente novamente.", 412
            )

    def answer(self, actor, pid, identity, key, request):
        previous = self.repo.recover_intention(actor, "preparation-answers:" + identity, key, request)
        if previous:
            return self.get(actor, pid, previous["id"])
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "generate")
            operation = self.owned(db, actor, pid, identity)
            value = self.dto(db, operation)
            if (
                value["state"] not in {"ready", "needs_input"}
                or value["state_version"] != request["expected_state_version"]
            ):
                raise RuleError(
                    "PREPARATION_CONFLICT", "A preparação mudou. Atualize antes de responder.", 412
                )
            row, draft = self.current_draft(db, actor, pid)
            if row.version != request["expected_draft_revision"]:
                raise RuleError("DRAFT_CONFLICT", "O pedido mudou. Salve antes de responder.", 412)
            questions = {
                question["id"]: question for item in value["items"] for question in item["questions"]
            }
            answers = request["answers"]
            if len({answer["question_id"] for answer in answers}) != len(answers) or any(
                answer["question_id"] not in questions or not answer["value"].strip() for answer in answers
            ):
                raise RuleError("PREPARATION_INVALID_ANSWER", "Responda às perguntas desta preparação.", 400)
            selected = [
                item["template_id"]
                for item in draft["attachments"]
                if item["enabled"]
                and item["template_id"] in operation.payload["preparation_request"]["attachment_ids"]
            ]
            start_request = {
                "expected_draft_revision": row.version,
                "base_project_revision": operation.payload["base_revision_id"],
                "attachment_ids": selected,
                "expected_attachment_revisions": {
                    item["template_id"]: item["attachment_revision"]
                    for item in draft["attachments"]
                    if item["template_id"] in selected
                },
            }
            automatic = bool(operation.payload.get("automatic_apply"))
        result = self.start(
            actor,
            pid,
            "answer:" + canonical_hash([identity, key])[:64],
            start_request,
            identity,
            answers,
            automatic=automatic,
        )
        self.repo.mutate(
            actor,
            "preparation-answers:" + identity,
            key,
            request,
            lambda db, account: {"id": result["id"]},
            "generate",
        )
        return result

    def apply(self, actor, pid, identity, key, request):
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "write")
            self.owned(db, actor, pid, identity)

        def command(db, account):
            operation = self.owned(db, actor, pid, identity)
            value = self.dto(db, operation)
            if (
                value["state"] not in {"ready", "needs_input"}
                or value["state_version"] != request["expected_state_version"]
            ):
                raise RuleError(
                    "PREPARATION_CONFLICT", "A preparação mudou. Revise as sugestões novamente.", 412
                )
            row, draft = self.current_draft(db, actor, pid)
            if row.version != request["expected_draft_revision"]:
                raise RuleError("DRAFT_CONFLICT", "O pedido mudou. Compare antes de aplicar sugestões.", 412)
            selections = [(field["attachment_id"], field["field_id"]) for field in request["accepted_fields"]]
            if len(set(selections)) != len(selections):
                raise RuleError("VALIDATION_ERROR", "Seleção duplicada.", 400)
            saved = copy.deepcopy(draft)
            attachments = {item["template_id"]: item for item in saved["attachments"]}
            suggestions = {
                (item["attachment_id"], field["field_id"]): field
                for item in value["items"]
                for field in item["suggested_fields"]
            }
            conflicts, applied, trace_fields = [], [], []
            if any(selection not in suggestions for selection in selections):
                raise RuleError("PREPARATION_UNKNOWN_FIELD", "Selecione campos desta preparação.", 400)
            original_revisions = {
                item["template_id"]: item["attachment_revision"] for item in draft["attachments"]
            }
            for attachment_id, field_id in selections:
                attachment, suggestion = (
                    attachments.get(attachment_id),
                    suggestions[(attachment_id, field_id)],
                )
                error = None
                if not attachment or not suggestion["compatible"] or suggestion["state"] != "suggested":
                    error = "FIELD_CHANGED"
                elif (
                    request["expected_attachment_revisions"].get(attachment_id)
                    != original_revisions[attachment_id]
                ):
                    error = "ATTACHMENT_CONFLICT"
                if error:
                    conflicts.append(
                        {
                            "attachment_id": attachment_id,
                            "field_id": field_id,
                            "code": error,
                            "message": "A edição atual foi preservada. Revise este campo manualmente.",
                        }
                    )
                    continue
                template = self.guided.templates.authorized(db, actor, attachment["source_version_id"])
                definitions = normalized_fields(template.content, template.slug)
                before = dict(attachment["fields"])
                after = {**before, field_id: suggestion["value"]}
                automatic = attachment["edited_content"] == render(
                    template.content["content"], definitions, before
                )
                if not automatic and "{{" + field_id + "}}" not in attachment["edited_content"]:
                    conflicts.append(
                        {
                            "attachment_id": attachment_id,
                            "field_id": field_id,
                            "code": "CONTENT_CHANGED",
                            "message": "O conteúdo do anexo foi editado. Aplique este campo manualmente para preservar seu texto.",
                        }
                    )
                    continue
                attachment["fields"] = after
                attachment["edited_content"] = render(
                    template.content["content"] if automatic else attachment["edited_content"],
                    definitions,
                    after,
                )
                attachment["attachment_revision"] = row.version + 1
                field_revision = (
                    attachment.get("field_values", {}).get(field_id, {}).get("field_revision", 1) + 1
                )
                attachment["field_values"][field_id] = {
                    "field_id": field_id,
                    "value": suggestion["value"],
                    "state": "applied",
                    "source_type": suggestion["source_type"],
                    "source_refs": suggestion["source_refs"],
                    "suggestion_version": suggestion["suggestion_version"],
                    "edited_by_user": False,
                    "updated_at": utcnow().isoformat(),
                    "preparation_id": identity,
                    "field_revision": field_revision,
                }
                attachment["validation_errors"] = attachment_errors(attachment, definitions)
                applied.append({"attachment_id": attachment_id, "field_id": field_id})
                trace_fields.append(
                    {
                        "attachment_id": attachment_id,
                        "field_id": field_id,
                        "original_value": suggestion["original_value"],
                        "suggested_value": suggestion["value"],
                        "final_value": suggestion["value"],
                        "source_type": suggestion["source_type"],
                        "state": "applied",
                        "attachment_revision": attachment["attachment_revision"],
                    }
                )
            if applied:
                from viracase.infrastructure.build_workflow import invalidate_review

                invalidate_review(saved)
                saved["stage"] = "templates" if saved.get("prepared_fingerprint") else "idea"
                saved["correlation_id"], saved["preparation_id"] = (
                    operation.payload["correlation_id"],
                    identity,
                )
                result = self.guided.drafts.write(db, actor, "build", pid, row.version, saved)
                result["data"] = saved
                preparation = copy.deepcopy(operation.result["preparation"])
                for item in preparation["items"]:
                    for field in item["suggested_fields"]:
                        if (item["attachment_id"], field["field_id"]) in {
                            (item["attachment_id"], item["field_id"]) for item in applied
                        }:
                            field["state"] = "applied"
                operation.result = {
                    **operation.result,
                    "preparation": preparation,
                    "preparation_version": operation.result.get("preparation_version", 1) + 1,
                }
                TraceStore(self.repo).put(
                    trace_context(self.repo, operation, "review"),
                    "fields",
                    "applied",
                    {"fields": {"items": trace_fields}},
                    db=db,
                )
                db.flush()
                db.expire(row)
            else:
                result = {"version": row.version, "data": draft, "updated_at": iso(row.updated_at)}
            result["data"] = self.guided.public_data(result["data"])
            return {
                "preparation": self.dto(db, operation),
                "draft": result,
                "applied_fields": applied,
                "conflicts": conflicts,
            }

        return self.repo.mutate(actor, "preparation-apply:" + identity, key, request, command, "write")

    def cancel(self, actor, pid, identity, key):
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "read")
            self.owned(db, actor, pid, identity)
        self.repo.cancel(actor, identity, key)
        return self.get(actor, pid, identity)
