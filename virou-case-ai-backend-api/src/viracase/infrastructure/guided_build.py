import copy
import json

from sqlalchemy import select

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, canonical_hash, utcnow
from viracase.domain.template_fields import (
    attachment_errors,
    attachment_signature,
    empty,
    field_error,
    normalized_fields,
    provenance,
)
from viracase.infrastructure.drafts import Drafts, dto
from viracase.infrastructure.build_workflow import (
    decorate,
    idea_fingerprint,
    initialize,
    invalidate_review,
    record_transition,
    schedule_fill,
    selection_fingerprint,
    transition,
)
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Operation, TemplateVersion, new_id
from viracase.infrastructure.template_catalog import TemplateCatalog


def attachment_context(payload):
    attachments = payload.get("attachment_snapshots", [])
    analysis = payload.get("analysis_context") if payload.get("analysis_id") else None
    if not attachments and analysis is None:
        return payload["brief"]
    context = {"message": payload["brief"]}
    if attachments:
        context["untrusted_user_attachments"] = attachments
    if analysis is not None:
        context["untrusted_code_analysis"] = analysis
    return json.dumps(context, ensure_ascii=False)


class GuidedBuild:
    def __init__(self, repo, learning):
        self.repo, self.learning = repo, learning
        self.drafts = Drafts(repo, learning)
        self.templates = TemplateCatalog(repo, learning)
        self.integrations = Integrations(repo)

    def initial(self, project, workspace=None):
        return initialize(
            {
                "stage": "idea",
                "objective": project.objective,
                "message_text": workspace.data.get("prompt", "") if workspace else "",
                "attachments": [],
                "recommendation_id": None,
            }
        )

    @staticmethod
    def public_data(data):
        return {
            name: value for name, value in data.items() if name not in {"correlation_id", "preparation_id"}
        }

    def get(self, actor, pid):
        from viracase.infrastructure.recommendations import Recommendations

        Recommendations(self).recover_pending(actor, pid)
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "read")
            project = self.repo.owned_project(db, actor, pid)
            workspace = self.drafts.row(db, actor, "project", pid)
            row = self.drafts.row(db, actor, "build", pid)
            value = dto(row, self.initial(project, workspace))
            if row:
                value["data"] = self.public_data(
                    self.validate(db, actor, row.data, previous=row.data, read_only=True)
                )
            value["data"] = decorate(self.repo, db, actor, project, value["data"], value["version"])
            return value

    def validate(self, db, actor, data, sending=False, previous=None, revision=1, read_only=False):
        policy = self.integrations.build_policy()
        if (
            len(data["objective"]) > policy["max_objective_chars"]
            or len(data["message_text"]) > policy["max_message_chars"]
        ):
            raise RuleError("DRAFT_LIMIT_EXCEEDED", "Ideia ou mensagem acima do limite configurado.", 400)
        if sum(1 for item in data["attachments"] if item["enabled"]) > policy["max_attachments"]:
            raise RuleError(
                "ATTACHMENT_LIMIT_EXCEEDED", f"Reduza para até {policy['max_attachments']} anexos.", 400
            )
        attachments, seen, all_errors = [], set(), []
        previous_items = {item["template_id"]: item for item in (previous or data).get("attachments", [])}
        for index, item in enumerate(data["attachments"]):
            row = (
                self.templates.authorized(db, actor, item["source_version_id"])
                if item["enabled"] and not read_only
                else db.get(TemplateVersion, item["source_version_id"])
            )
            if not row or row.owner_id not in {None, actor}:
                raise RuleError("TEMPLATE_UNAVAILABLE", "Um anexo não está autorizado para esta conta.", 409)
            if row.slug != item["template_id"] or row.slug in seen:
                raise RuleError("INVALID_ATTACHMENT", "Template inválido ou duplicado.", 400)
            seen.add(row.slug)
            text = item["edited_content"]
            if len(text) > policy["max_attachment_chars"]:
                raise RuleError(
                    "ATTACHMENT_LIMIT_EXCEEDED",
                    f"O anexo {row.content['title']} excede {policy['max_attachment_chars']} caracteres.",
                    400,
                )
            definitions = normalized_fields(row.content, row.slug)
            fields = {f["key"]: f for f in definitions}
            if any(k not in fields for k in item["fields"]):
                raise RuleError("INVALID_ATTACHMENT", "Confira os campos pessoais do template.", 400)
            invalid = attachment_errors(item, definitions, False)
            if invalid:
                raise field_error(invalid, "INVALID_ATTACHMENT")
            prior = previous_items.get(item["template_id"])
            if prior and prior["source_version_id"] != item["source_version_id"]:
                prior = None
            errors = attachment_errors(item, definitions)
            if sending and item["enabled"]:
                all_errors.extend(errors)
                if not text.strip():
                    all_errors.append(
                        {
                            "attachment_id": item["template_id"],
                            "code": "ATTACHMENT_EMPTY",
                            "message": "O conteúdo revisado está vazio.",
                            "action": "Revise o anexo",
                        }
                    )
            attachments.append(
                {
                    **{
                        key: item[key]
                        for key in ("template_id", "source_version_id", "edited_content", "fields", "enabled")
                    },
                    "title": row.content["title"],
                    "category": row.content["category"],
                    "version": row.version,
                    "position": index,
                    "edited": text != row.content["content"],
                    "attachment_revision": prior.get("attachment_revision", 1)
                    if prior and attachment_signature(prior) == attachment_signature(item)
                    else revision,
                    "attachment_instance_id": prior.get(
                        "attachment_instance_id",
                        canonical_hash([actor, item["template_id"], item["source_version_id"]])[:32],
                    )
                    if prior
                    else new_id(),
                    "schema_version": 1,
                    "field_values": {
                        field["key"]: provenance(
                            field,
                            item["fields"].get(field["key"]),
                            (prior or {}).get("field_values", {}).get(field["key"]),
                        )
                        for field in definitions
                    },
                    "validation_errors": errors,
                    **{
                        name: prior[name]
                        for name in (
                            "fill_state",
                            "fill_message",
                            "fill_error_code",
                            "fill_questions",
                            "fill_context",
                            "fill_request_id",
                            "preparation_id",
                            "filled_at",
                            "field_generations",
                        )
                        if prior and name in prior
                    },
                }
            )
        if all_errors:
            raise field_error(all_errors)
        return {**data, "attachments": attachments}

    def save(self, actor, pid, key, version, data):
        def command(db, a):
            project = self.repo.owned_project(db, actor, pid)
            previous = self.drafts.row(db, actor, "build", pid)
            if (previous.version if previous else 0) != version:
                raise RuleError(
                    "DRAFT_CONFLICT", "O pedido mudou em outra sessão. Compare antes de salvar.", 412
                )
            before_data = copy.deepcopy(previous.data) if previous else self.initial(project)
            incoming = {name: data[name] for name in ("stage", "objective", "message_text", "attachments")}
            incoming["recommendation_id"] = before_data.get("recommendation_id")
            saved = self.validate(db, actor, incoming, previous=before_data, revision=version + 1)
            saved = transition(self.repo, db, actor, project, before_data, saved, data["stage"], version + 1)
            if previous:
                for name in ("correlation_id", "preparation_id"):
                    if name in previous.data:
                        saved[name] = previous.data[name]
            before = {item["template_id"]: item for item in previous.data["attachments"]} if previous else {}
            result = self.drafts.write(db, actor, "build", pid, version, saved)
            if selection_fingerprint(before_data) != selection_fingerprint(saved):
                record_transition(self.repo, db, actor, pid, saved, result["version"], "selection_changed")
            elif any(before_data.get(name) != saved.get(name) for name in ("objective", "message_text")):
                record_transition(self.repo, db, actor, pid, saved, result["version"], "idea_changed")
            result["data"] = self.public_data(
                decorate(self.repo, db, actor, project, saved, result["version"])
            )
            if previous and saved.get("preparation_id"):
                from viracase.infrastructure.integration_traces import TraceStore, trace_context

                preparation = db.get(Operation, saved["preparation_id"])
                changes = [
                    {
                        "attachment_id": item["template_id"],
                        "field_id": field_id,
                        "original_value": before[item["template_id"]]["fields"].get(field_id),
                        "edited_value": value,
                        "final_value": value,
                        "source_type": "user_edit",
                        "state": "user_edited",
                        "attachment_revision": item["attachment_revision"],
                    }
                    for item in saved["attachments"]
                    if item["template_id"] in before
                    for field_id, value in item["fields"].items()
                    if before[item["template_id"]]["fields"].get(field_id) != value
                ]
                if preparation and changes:
                    TraceStore(self.repo).put(
                        trace_context(self.repo, preparation, "review"),
                        "fields",
                        "user_edited",
                        {"fields": {"items": changes}},
                        db=db,
                    )
            return result

        return self.repo.mutate(
            actor, f"build-draft:{pid}", key, {"version": version, **data}, command, "write"
        )

    def review(self, actor, pid, key, request):
        def command(db, account):
            project = self.repo.owned_project(db, actor, pid)
            row = self.drafts.row(db, actor, "build", pid)
            if not row or row.version != request["expected_draft_revision"]:
                raise RuleError("DRAFT_CONFLICT", "O pedido mudou. Salve e revise a versão atual.", 412)
            data = self.validate(db, actor, initialize(row.data), previous=row.data)
            state = decorate(self.repo, db, actor, project, data, row.version)
            if state["blocking_reasons"]:
                reason = state["blocking_reasons"][0]
                raise RuleError(
                    reason["code"], reason["message"], 409, {"blocking_reasons": state["blocking_reasons"]}
                )
            selected = [item for item in data["attachments"] if item["enabled"]]
            if (
                not selected
                and not request.get("confirm_empty")
                and not data.get("empty_selection_confirmed")
            ):
                raise RuleError(
                    "EMPTY_SELECTION_UNCONFIRMED",
                    "Confirme na etapa Templates que nenhum template é aplicável a este pedido.",
                    409,
                )
            self.validate(db, actor, data, True, previous=data)
            attachments = [
                {
                    "id": canonical_hash(
                        [
                            data["request_id"],
                            item["source_version_id"],
                            item["attachment_revision"],
                            item["edited_content"],
                        ]
                    )[:32],
                    "source_template_id": item["template_id"],
                    "source_version_id": item["source_version_id"],
                    "title": item["title"],
                    "category": item["category"],
                    "version": item["version"],
                    "content": item["edited_content"],
                    "content_hash": canonical_hash(item["edited_content"]),
                    "source_draft_revision": row.version + 1,
                    "position": item["position"],
                    "schema_version": item["schema_version"],
                    "attachment_revision": item["attachment_revision"],
                    "fields": item["fields"],
                    "field_values": item["field_values"],
                }
                for item in selected
            ]
            snapshot = {
                "request_id": data["request_id"],
                "draft_revision": row.version + 1,
                "context_revision": data["context_revision"],
                "context_hash": data["context_hash"],
                "objective": data["objective"],
                "message_text": data["message_text"].strip(),
                "attachments": attachments,
                "empty_selection_confirmed": not selected,
                "selection_hash": selection_fingerprint(data),
                "prepared_fingerprint": data["prepared_fingerprint"],
                "reviewed_at": utcnow().isoformat(),
            }
            snapshot["hash"] = canonical_hash(snapshot)
            data.update(
                stage="review",
                templates_completed=True,
                templates_completed_at=utcnow().isoformat(),
                empty_selection_confirmed=not selected,
                review_snapshot=snapshot,
            )
            result = self.drafts.write(db, actor, "build", pid, row.version, data)
            record_transition(self.repo, db, actor, pid, data, result["version"], "review_ready")
            result["data"] = self.public_data(
                decorate(self.repo, db, actor, project, data, result["version"])
            )
            return result

        return self.repo.mutate(actor, "build-review:" + pid, key, request, command, "write")

    def retry_fill(self, actor, pid, key, request):
        def command(db, account):
            project = self.repo.owned_project(db, actor, pid)
            row = self.drafts.row(db, actor, "build", pid)
            if not row or row.version != request["expected_draft_revision"]:
                raise RuleError("DRAFT_CONFLICT", "O pedido mudou. Atualize antes de tentar novamente.", 412)
            data = initialize(row.data)
            if data.get("prepared_fingerprint") != idea_fingerprint(
                data
            ) or project.current_revision_id != data.get("context_revision"):
                raise RuleError(
                    "CONTEXT_STALE", "Volte à Ideia e prepare a construção com o contexto atual.", 409
                )
            items = [
                item
                for item in data["attachments"]
                if item["enabled"] and item["template_id"] in request["attachment_ids"]
            ]
            if len(items) != len(set(request["attachment_ids"])):
                raise RuleError("INVALID_ATTACHMENT", "Selecione anexos pertencentes a este pedido.", 400)
            target = request.get("target_field_id")
            if target:
                if len(request["attachment_ids"]) != 1:
                    raise RuleError("PREPARATION_UNKNOWN_FIELD", "Escolha um campo de um único anexo.", 400)
                item = items[0]
                template = self.templates.authorized(db, actor, item["source_version_id"])
                field = next(
                    (
                        field
                        for field in normalized_fields(template.content, template.slug)
                        if field["key"] == target
                    ),
                    None,
                )
                if not field or field["sensitivity"] == "secret":
                    raise RuleError("PREPARATION_UNKNOWN_FIELD", "O campo não pode ser gerado com IA.", 400)
                if not empty(item["fields"].get(target)):
                    raise RuleError(
                        "FIELD_NOT_EMPTY", "Este campo já foi preenchido. Sua edição foi preservada.", 409
                    )
                if db.scalar(
                    select(Operation.id).where(
                        Operation.project_id == pid,
                        Operation.type.in_(["template_preparation", "template_autofill"]),
                        Operation.state.in_(["queued", "running", "reconciling"]),
                    )
                ):
                    raise RuleError(
                        "FILL_PENDING", "Aguarde a conclusão ou conciliação da preparação existente.", 409
                    )
            if any(
                item.get("fill_state") in {"queued", "filling", "reconciling"}
                if target
                else item.get("fill_state") not in {"failed", "needs_input"}
                for item in items
            ):
                raise RuleError(
                    "FILL_PENDING", "Aguarde a conclusão ou conciliação da preparação existente.", 409
                )
            for item in items:
                item.pop("preparation_id", None)
            schedule_fill(self.repo, db, actor, pid, data, items, target_field_id=target)
            invalidate_review(data)
            data["stage"] = "templates"
            result = self.drafts.write(db, actor, "build", pid, row.version, data)
            result["data"] = self.public_data(
                decorate(self.repo, db, actor, project, data, result["version"])
            )
            return result

        return self.repo.mutate(
            actor,
            "build-fill:" + pid,
            key,
            request,
            command,
            "generate" if request.get("target_field_id") else "write",
        )

    def send(self, actor, pid, key, request, generation):
        previous = self.repo.recover_intention(actor, f"guided-send:{pid}", key, request)
        if previous:
            return self.repo.operation(actor, previous["id"])
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "generate")
            project = self.repo.owned_project(db, actor, pid)
            row = self.drafts.row(db, actor, "build", pid)
            if not row or row.version != request["expected_draft_revision"]:
                raise RuleError(
                    "DRAFT_CONFLICT", "Salve e confirme a versão atual do pedido antes de enviar.", 412
                )
            data = self.validate(db, actor, row.data, True)
            state = decorate(self.repo, db, actor, project, data, row.version)
            snapshot = data.get("review_snapshot")
            if (
                not state["can_build"]
                or not snapshot
                or request.get("review_snapshot_hash") != snapshot["hash"]
            ):
                reasons = state["blocking_reasons"] or [
                    {
                        "code": "REVIEW_STALE",
                        "message": "Conclua Templates e revise o pedido atual antes de enviar.",
                    }
                ]
                raise RuleError(reasons[0]["code"], reasons[0]["message"], 409, {"blocking_reasons": reasons})
            if request.get("base_project_revision") != snapshot["context_revision"]:
                raise RuleError("CONTEXT_STALE", "O contexto enviado difere da revisão aprovada.", 409)
        if not data["message_text"].strip():
            raise RuleError("VALIDATION_ERROR", "Escreva a mensagem antes de enviar.", 400)
        snapshots = copy.deepcopy(snapshot["attachments"])
        construction = self.integrations.build_configuration()
        build_policy = construction["settings"]
        payload = {
            "brief": snapshot["message_text"],
            "base_revision_id": request["base_project_revision"],
            "policy_version": request["policy_version"],
            "review_required": True,
            "reply_to": request.get("reply_to"),
            "attachment_snapshots": snapshots,
            "guided_request": request,
            "guided_draft_hash": canonical_hash(row.data),
            "context_limit_bytes": build_policy["max_context_bytes"],
            "generation_context_limit_bytes": build_policy["max_generation_context_bytes"],
            "max_generation_calls": build_policy["max_generation_calls"],
            "construction_configuration": construction,
            "correlation_id": data.get("correlation_id") or data.get("recommendation_id"),
            "preparation_id": data.get("preparation_id"),
            "review_snapshot_hash": snapshot["hash"],
            "review_snapshot": copy.deepcopy(snapshot),
        }
        return generation.start(actor, pid, key, payload, contract_version=4)

    def freeze(self, db, actor, pid, payload):
        request = payload.get("guided_request")
        if not request:
            return
        row = self.drafts.row(db, actor, "build", pid)
        if (
            not row
            or row.version != request["expected_draft_revision"]
            or canonical_hash(row.data) != payload["guided_draft_hash"]
        ):
            raise RuleError("DRAFT_CONFLICT", "O pedido mudou antes do envio. Revise a versão atual.", 412)
        self.validate(db, actor, row.data, True)
        project = self.repo.owned_project(db, actor, pid)
        state = decorate(self.repo, db, actor, project, row.data, row.version)
        if not state["can_build"] or row.data["review_snapshot"]["hash"] != request.get(
            "review_snapshot_hash"
        ):
            raise RuleError("REVIEW_STALE", "O pedido mudou. Revise a versão atual antes de enviar.", 409)
        if payload.get("attachment_snapshots") != row.data["review_snapshot"]["attachments"]:
            raise RuleError("REVIEW_STALE", "O conteúdo enviado difere dos anexos revisados.", 409)
        pending = db.scalars(
            select(Operation).where(
                Operation.account_id == actor,
                Operation.project_id == pid,
                Operation.type == "generation",
                Operation.state == "succeeded",
            )
        ).all()
        if any(
            op.result.get("candidate")
            and not op.result.get("applied_revision_id")
            and not op.result.get("discarded_at")
            for op in pending
        ):
            raise RuleError(
                "PROPOSAL_PENDING", "Revise e aplique ou descarte a proposta anterior antes de enviar."
            )
        self.drafts.write(
            db,
            actor,
            "build",
            pid,
            row.version,
            {
                **row.data,
                "stage": "conversation",
                "request_id": new_id(),
                "message_text": "",
                "attachments": [{**item, "enabled": False} for item in row.data["attachments"]],
                "recommendation_id": None,
                "recommendation_status": None,
                "prepared_fingerprint": None,
                "templates_completed": False,
                "templates_completed_at": None,
                "empty_selection_confirmed": False,
                "review_snapshot": None,
            },
        )

    def discard(self, actor, pid, oid, key):
        def command(db, a):
            self.repo.owned_project(db, actor, pid)
            op = db.scalar(
                select(Operation).where(
                    Operation.id == oid, Operation.account_id == actor, Operation.project_id == pid
                )
            )
            if (
                not op
                or op.state != "succeeded"
                or not op.result.get("candidate")
                or op.result.get("applied_revision_id")
            ):
                raise RuleError("NOT_FOUND", "Proposta indisponível.", 404)
            op.result = {**op.result, "discarded_at": utcnow().isoformat()}
            record_progress(op, "proposal_discarded")
            return {"id": op.id, "discarded": True}

        return self.repo.mutate(actor, f"discard:{oid}", key, {}, command, "write")

    def transfer(self, actor, slug, key, payload):
        def command(db, a):
            self.templates.ensure_seed(db)
            source = self.drafts.row(db, actor, "learning", slug)
            if not source or source.version != payload["source_version"]:
                raise RuleError("DRAFT_CONFLICT", "Salve a edição atual do guia antes de transferir.", 412)
            project = self.repo.owned_project(db, actor, payload["project_id"])
            target = self.drafts.row(db, actor, "build", project.id)
            if (target.version if target else 0) != payload["target_version"]:
                raise RuleError("DRAFT_CONFLICT", "O pedido mudou. Confira o rascunho no projeto.", 412)
            source_identity = source.data["guide"].get("template_source_version_id")
            template = (
                self.templates.authorized(db, actor, source_identity)
                if source_identity
                else db.scalar(
                    select(TemplateVersion)
                    .where(TemplateVersion.slug == slug)
                    .order_by(TemplateVersion.version)
                    .limit(1)
                )
            )
            if not template:
                raise RuleError("TEMPLATE_UNAVAILABLE", "Template indisponível.", 404)
            data = dict(
                target.data
                if target
                else self.initial(project, self.drafts.row(db, actor, "project", project.id))
            )
            item = {
                "template_id": slug,
                "source_version_id": template.id,
                "edited_content": source.data["prompt"],
                "fields": source.data["fields"],
                "enabled": True,
            }
            data["attachments"] = [x for x in data["attachments"] if x["template_id"] != slug] + [item]
            data = initialize(data)
            invalidate_review(data)
            data["stage"] = (
                "templates" if data.get("prepared_fingerprint") == idea_fingerprint(data) else "idea"
            )
            saved = self.validate(db, actor, data)
            if data["stage"] == "templates":
                schedule_fill(self.repo, db, actor, project.id, saved, [saved["attachments"][-1]])
            result = self.drafts.write(db, actor, "build", project.id, payload["target_version"], saved)
            return {**result, "project_id": project.id}

        return self.repo.mutate(actor, f"guided-transfer:{slug}", key, payload, command, "write")
