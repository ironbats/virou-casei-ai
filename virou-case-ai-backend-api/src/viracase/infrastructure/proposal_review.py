from hashlib import sha256

from sqlalchemy import select

from viracase.domain.rules import RuleError, canonical_hash
from viracase.infrastructure.models import Draft, Operation


class ProposalReviews:
    def __init__(self, repo):
        self.repo = repo

    def load(self, db, actor, pid, oid):
        project = self.repo.owned_project(db, actor, pid)
        operation = db.scalar(
            select(Operation).where(
                Operation.id == oid,
                Operation.account_id == actor,
                Operation.project_id == pid,
                Operation.type == "generation",
            )
        )
        if (
            not operation
            or operation.state != "succeeded"
            or not operation.result.get("candidate")
            or not operation.result.get("revision_id")
            or not operation.payload.get("base_revision_id")
        ):
            raise RuleError("NOT_FOUND", "Proposta indisponível.", 404)
        base = self.repo.owned_revision(db, actor, pid, operation.payload["base_revision_id"])
        candidate = self.repo.owned_revision(db, actor, pid, operation.result["revision_id"])
        before = self.read_revision(base, project.stack)
        after = self.read_revision(candidate, project.stack)
        return project, operation, base, candidate, before, after

    def read_revision(self, revision, stack):
        files = self.repo.sources.read(revision, stack)
        manifest = [
            {"id": sha256(path.encode()).hexdigest()[:24], "path": path, "size": len(content.encode())}
            for path, content in sorted(files.items())
        ]
        if revision.manifest != manifest:
            raise RuleError(
                "SOURCE_INTEGRITY_FAILED", "O manifesto da revisão não corresponde aos arquivos.", 503
            )
        return files

    def fingerprint(self, operation, base, candidate):
        return canonical_hash(
            {
                "proposal_id": operation.id,
                "base_revision_id": base.id,
                "base_source_hash": base.source_hash,
                "candidate_revision_id": candidate.id,
                "candidate_source_hash": candidate.source_hash,
                "candidate_manifest": candidate.manifest,
            }
        )

    def manifest(self, before, after):
        items = []
        for path in sorted(before.keys() | after.keys()):
            old, new = before.get(path), after.get(path)
            if old == new:
                continue
            items.append(
                {
                    "id": sha256(path.encode()).hexdigest()[:24],
                    "path": path,
                    "change": "created" if old is None else "deleted" if new is None else "modified",
                    "before_hash": sha256(old.encode()).hexdigest() if old is not None else None,
                    "after_hash": sha256(new.encode()).hexdigest() if new is not None else None,
                    "before_bytes": len(old.encode()) if old is not None else 0,
                    "after_bytes": len(new.encode()) if new is not None else 0,
                    "content_type": "text",
                }
            )
        return items

    def conflicts(self, db, actor, project, base, before):
        changed = set()
        revision_changed = project.current_revision_id != base.id
        if revision_changed:
            current = self.repo.owned_revision(db, actor, project.id, project.current_revision_id)
            latest = self.read_revision(current, project.stack)
            changed.update(
                path for path in before.keys() | latest.keys() if before.get(path) != latest.get(path)
            )
        draft = db.scalar(
            select(Draft).where(
                Draft.account_id == actor, Draft.kind == "project", Draft.target == project.id
            )
        )
        pending = draft.data.get("files") if draft else None
        dirty = pending is not None and pending != before
        if dirty:
            changed.update(
                path for path in before.keys() | pending.keys() if before.get(path) != pending.get(path)
            )
        return revision_changed or dirty, sorted(changed), draft

    def describe(self, db, actor, loaded):
        project, operation, base, candidate, before, after = loaded
        conflict, paths, _ = self.conflicts(db, actor, project, base, before)
        applied = operation.result.get("applied_revision_id")
        discarded = operation.result.get("discarded_at")
        state = (
            "applied" if applied else "discarded" if discarded else "conflict" if conflict else "generated"
        )
        structure_checked = operation.result.get("validation") == "structure_checked"
        return {
            "proposal_id": operation.id,
            "proposal_version": self.fingerprint(operation, base, candidate),
            "base_project_revision": base.id,
            "candidate_revision_id": candidate.id,
            "current_project_revision": project.current_revision_id,
            "expected_project_version": project.version,
            "state": state,
            "files": self.manifest(before, after),
            "verification_results": [
                {
                    "kind": "structure",
                    "state": "passed" if structure_checked else "not_run",
                    "message": "Estrutura conferida"
                    if structure_checked
                    else "Verificação estrutural não registrada",
                },
                {"kind": "tests", "state": "not_run", "message": "Testes do projeto gerado não executados"},
                {
                    "kind": "execution",
                    "state": "not_run",
                    "message": "Execução do projeto gerado não realizada",
                },
            ],
            "warnings": ["A revisão humana não substitui os testes e a execução do projeto."],
            "conflict_paths": paths if state == "conflict" else [],
            "applied_revision_id": applied,
            "discarded_at": discarded,
        }

    def get(self, actor, pid, oid):
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "read")
            return self.describe(db, actor, self.load(db, actor, pid, oid))

    def file(self, actor, pid, oid, file_id, proposal_version):
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "read")
            _project, operation, base, candidate, before, after = self.load(db, actor, pid, oid)
            actual_version = self.fingerprint(operation, base, candidate)
            if proposal_version != actual_version:
                raise RuleError("PROPOSAL_CONFLICT", "A versão da proposta mudou. Reabra a revisão.", 412)
            item = next((item for item in self.manifest(before, after) if item["id"] == file_id), None)
            if not item:
                raise RuleError("NOT_FOUND", "Arquivo indisponível nesta proposta.", 404)
            return {
                **item,
                "proposal_id": operation.id,
                "proposal_version": actual_version,
                "before": before.get(item["path"]),
                "after": after.get(item["path"]),
            }

    def validate_apply(self, db, actor, pid, oid, guards=None):
        loaded = self.load(db, actor, pid, oid)
        project, operation, base, candidate, before, _after = loaded
        guards = guards or {}
        if guards.get("expected_proposal_version") not in (
            None,
            self.fingerprint(operation, base, candidate),
        ):
            raise RuleError("PROPOSAL_CONFLICT", "A versão da proposta mudou. Reabra a revisão.", 412)
        if guards.get("expected_project_revision") not in (None, base.id):
            raise RuleError("VERSION_CONFLICT", "A base revisada não corresponde à proposta.", 412)
        if operation.result.get("discarded_at"):
            raise RuleError("NOT_FOUND", "Proposta descartada.", 404)
        if operation.result.get("applied_revision_id"):
            return project, operation, candidate, None
        conflict, paths, draft = self.conflicts(db, actor, project, base, before)
        if conflict:
            detail = ", ".join(paths[:5])
            raise RuleError(
                "VERSION_CONFLICT",
                "O projeto ou rascunho mudou. As duas versões foram preservadas."
                + (" Arquivos: " + detail + "." if detail else " Compare a revisão atual."),
                412,
            )
        return project, operation, candidate, draft
