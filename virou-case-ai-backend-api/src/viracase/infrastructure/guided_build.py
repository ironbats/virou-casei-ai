import json
import re

from sqlalchemy import select

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, canonical_hash, utcnow
from viracase.infrastructure.drafts import Drafts, dto
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Operation, TemplateVersion
from viracase.infrastructure.template_catalog import TemplateCatalog


def attachment_context(payload):
    attachments = payload.get("attachment_snapshots", [])
    if not attachments:
        return payload["brief"]
    return json.dumps(
        {"message": payload["brief"], "untrusted_user_attachments": attachments}, ensure_ascii=False
    )


class GuidedBuild:
    def __init__(self, repo, learning):
        self.repo, self.learning = repo, learning
        self.drafts = Drafts(repo, learning)
        self.templates = TemplateCatalog(repo, learning)
        self.integrations = Integrations(repo)

    def initial(self, project, workspace=None):
        return {
            "stage": "review" if workspace and workspace.data.get("prompt") else "idea",
            "objective": project.objective,
            "message_text": workspace.data.get("prompt", "") if workspace else "",
            "attachments": [],
            "recommendation_id": None,
        }

    def get(self, actor, pid):
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "read")
            project = self.repo.owned_project(db, actor, pid)
            workspace = self.drafts.row(db, actor, "project", pid)
            return dto(self.drafts.row(db, actor, "build", pid), self.initial(project, workspace))

    def validate(self, db, actor, data, sending=False):
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
        attachments, seen = [], set()
        for index, item in enumerate(data["attachments"]):
            row = (
                self.templates.authorized(db, actor, item["source_version_id"])
                if item["enabled"]
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
            fields = {f["key"]: f for f in row.content["fields"]}
            if any(k not in fields or len(v) > fields[k]["max_length"] for k, v in item["fields"].items()):
                raise RuleError("INVALID_ATTACHMENT", "Confira os campos pessoais do template.", 400)
            if sending and item["enabled"]:
                if not text.strip() or re.search(r"\{\{[^}]+\}\}", text):
                    raise RuleError(
                        "ATTACHMENT_INCOMPLETE", f"Preencha os marcadores de {row.content['title']}.", 400
                    )
                if any(f["required"] and not item["fields"].get(k, "").strip() for k, f in fields.items()):
                    raise RuleError(
                        "ATTACHMENT_INCOMPLETE",
                        f"Preencha os campos obrigatórios de {row.content['title']}.",
                        400,
                    )
            attachments.append(
                {
                    **item,
                    "title": row.content["title"],
                    "category": row.content["category"],
                    "version": row.version,
                    "position": index,
                    "edited": text != row.content["content"],
                }
            )
        return {**data, "attachments": attachments}

    def save(self, actor, pid, key, version, data):
        def command(db, a):
            self.repo.owned_project(db, actor, pid)
            saved = self.validate(db, actor, data)
            return self.drafts.write(db, actor, "build", pid, version, saved)

        return self.repo.mutate(
            actor, f"build-draft:{pid}", key, {"version": version, **data}, command, "write"
        )

    def send(self, actor, pid, key, request, generation):
        previous = self.repo.recover_intention(actor, f"guided-send:{pid}", key, request)
        if previous:
            return self.repo.operation(actor, previous["id"])
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "generate")
            self.repo.owned_project(db, actor, pid)
            row = self.drafts.row(db, actor, "build", pid)
            if not row or row.version != request["expected_draft_revision"]:
                raise RuleError(
                    "DRAFT_CONFLICT", "Salve e confirme a versão atual do pedido antes de enviar.", 412
                )
            data = self.validate(db, actor, row.data, True)
        if not data["message_text"].strip():
            raise RuleError("VALIDATION_ERROR", "Escreva a mensagem antes de enviar.", 400)
        snapshots = [
            {
                "id": canonical_hash([pid, key, item["source_version_id"]])[:32],
                "source_template_id": item["template_id"],
                "source_version_id": item["source_version_id"],
                "title": item["title"],
                "category": item["category"],
                "version": item["version"],
                "content": item["edited_content"],
                "content_hash": canonical_hash(item["edited_content"]),
                "source_draft_revision": request["expected_draft_revision"],
                "position": item["position"],
            }
            for item in data["attachments"]
            if item["enabled"]
        ]
        payload = {
            "brief": data["message_text"].strip(),
            "base_revision_id": request["base_project_revision"],
            "policy_version": request["policy_version"],
            "review_required": True,
            "reply_to": request.get("reply_to"),
            "attachment_snapshots": snapshots,
            "guided_request": request,
            "guided_draft_hash": canonical_hash(row.data),
            "context_limit_bytes": self.integrations.build_policy()["max_context_bytes"],
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
                "message_text": "",
                "attachments": [{**item, "enabled": False} for item in row.data["attachments"]],
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
            data["stage"] = "review"
            saved = self.validate(db, actor, data, True)
            result = self.drafts.write(db, actor, "build", project.id, payload["target_version"], saved)
            return {**result, "project_id": project.id}

        return self.repo.mutate(actor, f"guided-transfer:{slug}", key, payload, command, "write")
