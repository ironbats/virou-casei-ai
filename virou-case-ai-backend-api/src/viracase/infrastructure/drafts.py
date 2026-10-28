"""Drafts are account-owned working copies, never canonical revisions or AI jobs."""

import re

from sqlalchemy import select, update

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, utcnow, validate_file_entries
from viracase.infrastructure.models import Draft, Operation


def dto(row, data=None):
    return {
        "version": row.version if row else 0,
        "updated_at": row.updated_at.isoformat() if row else None,
        "data": row.data if row else data,
    }


class Drafts:
    def __init__(self, repo, learning):
        self.repo, self.learning = repo, learning

    def row(self, db, actor, kind, target):
        return db.scalar(
            select(Draft).where(Draft.account_id == actor, Draft.kind == kind, Draft.target == target)
        )

    def authorize(self, db, actor, kind, target, writing=False):
        a = self.repo.account(db, actor)
        self.repo.access(db, a, "learn" if kind == "learning" else "write" if writing else "read")
        if kind == "project":
            return self.repo.owned_project(db, actor, target)
        self.learning_guide(db, actor, target)

    def learning_guide(self, db, actor, target):
        from viracase.infrastructure.template_catalog import TemplateCatalog

        return TemplateCatalog(self.repo, self.learning).guide(db, actor, target)

    def initial(self, kind, target, project=None):
        if kind == "learning":
            return {"guide": self.learning.get(target), "fields": {}, "prompt": "", "manual": False}
        return {
            "base_revision_id": project.current_revision_id,
            "files": None,
            "prompt": "",
            "provenance": [],
        }

    def get(self, actor, kind, target):
        with self.repo.sessions.begin() as db:
            project = self.authorize(db, actor, kind, target)
            initial = (
                {"guide": self.learning_guide(db, actor, target), "fields": {}, "prompt": "", "manual": False}
                if kind == "learning"
                else self.initial(kind, target, project)
            )
            return dto(self.row(db, actor, kind, target), initial)

    def write(self, db, actor, kind, target, version, data):
        row = self.row(db, actor, kind, target)
        if (row.version if row else 0) != version:
            raise RuleError(
                "DRAFT_CONFLICT", "O rascunho mudou em outra sessão. Compare antes de reaplicar.", 412
            )
        now = utcnow()
        if row:
            changed = db.execute(
                update(Draft)
                .where(Draft.id == row.id, Draft.version == version)
                .values(data=data, version=version + 1, updated_at=now)
            )
            if changed.rowcount != 1:
                raise RuleError("DRAFT_CONFLICT", "O rascunho mudou em outra sessão.", 412)
        else:
            db.add(Draft(account_id=actor, kind=kind, target=target, data=data, version=1, updated_at=now))
        return {"version": version + 1, "updated_at": now.isoformat()}

    def save(self, actor, kind, target, key, data, version):
        def command(db, a):
            self.authorize(db, actor, kind, target, True)
            row = self.row(db, actor, kind, target)
            if kind == "project":
                if data["base_revision_id"]:
                    self.repo.owned_revision(db, actor, target, data["base_revision_id"])
                if data["files"] is not None:
                    validate_file_entries(data["files"], self.repo.sources.secrets)
                saved = {**data, "provenance": row.data.get("provenance", []) if row else []}
            else:
                guide = row.data["guide"] if row else self.learning_guide(db, actor, target)
                valid = {f["key"]: f for f in guide["fields"]}
                if any(k not in valid or len(v) > valid[k]["max_length"] for k, v in data["fields"].items()):
                    raise RuleError("VALIDATION_ERROR", "Campo fora dos limites do guia.", 400)
                saved = {**data, "guide": guide}
            return self.write(db, actor, kind, target, version, saved)

        return self.repo.mutate(
            actor,
            f"draft:{kind}:{target}",
            key,
            {**data, "version": version},
            command,
            "learn" if kind == "learning" else "write",
        )

    def transfer(self, actor, slug, key, payload):
        def command(db, a):
            self.authorize(db, actor, "learning", slug)
            p = self.authorize(db, actor, "project", payload["project_id"], True)
            source = self.row(db, actor, "learning", slug)
            if not source or source.version != payload["source_version"]:
                raise RuleError("DRAFT_CONFLICT", "Salve a versão atual do guia antes de transferir.", 412)
            content = source.data
            for field in content["guide"]["fields"]:
                if field["required"] and not content["fields"].get(field["key"], "").strip():
                    raise RuleError("VALIDATION_ERROR", "Preencha os campos obrigatórios do guia.", 400)
            if not content["prompt"].strip() or re.search(r"\{\{[^}]+\}\}", content["prompt"]):
                raise RuleError(
                    "VALIDATION_ERROR", "Revise o prompt e preencha os marcadores antes de transferir.", 400
                )
            row = self.row(db, actor, "project", p.id)
            data = dict(row.data if row else self.initial("project", p.id, p))
            prompt = content["prompt"]
            if payload["mode"] == "append" and data["prompt"]:
                prompt = data["prompt"] + "\n\n" + prompt
            if len(prompt) > 12000:
                raise RuleError(
                    "VALIDATION_ERROR",
                    "A combinação excede 12.000 caracteres. Edite antes de transferir.",
                    400,
                )
            data.update(
                prompt=prompt,
                provenance=[
                    *data.get("provenance", [])[-9:],
                    {
                        "slug": slug,
                        "version": content["guide"]["template_version"],
                        "fields": content["fields"],
                        "source_version": source.version,
                    },
                ],
            )
            result = self.write(db, actor, "project", p.id, payload["target_version"], data)
            return {**result, "project_id": p.id}

        return self.repo.mutate(actor, f"transfer:{slug}", key, payload, command, "write")

    def apply(self, actor, pid, oid, key, version):
        def command(db, a):
            p = self.repo.owned_project(db, actor, pid)
            op = db.scalar(
                select(Operation).where(
                    Operation.id == oid,
                    Operation.account_id == actor,
                    Operation.project_id == pid,
                    Operation.type == "generation",
                )
            )
            if (
                not op
                or op.state != "succeeded"
                or not op.result.get("candidate")
                or op.result.get("discarded_at")
            ):
                raise RuleError("NOT_FOUND", "Proposta indisponível.", 404)
            if p.version != version or p.current_revision_id != op.payload.get("base_revision_id"):
                raise RuleError(
                    "VERSION_CONFLICT", "A base mudou. Compare a proposta com a revisão atual.", 412
                )
            if op.result.get("applied_revision_id"):
                return {"revision_id": op.result["applied_revision_id"]}
            r = self.repo.owned_revision(db, actor, pid, op.result["revision_id"])
            # Accept this immutable revision; no duplicate revision or additional quota consumed.
            p.current_revision_id, p.version, r.candidate = r.id, p.version + 1, False
            op.result = {**op.result, "applied_revision_id": r.id}
            record_progress(op, "changes_applied")
            return {"revision_id": r.id}

        return self.repo.mutate(actor, f"apply:{oid}", key, {"version": version}, command, "write")
