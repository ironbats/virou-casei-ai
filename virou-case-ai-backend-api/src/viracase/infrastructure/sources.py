import json
import os
import shutil
import tempfile
from pathlib import Path
from uuid import UUID
from viracase.domain.rules import canonical_hash, RuleError, validate_files


class SourceStore:
    def __init__(self, root, secrets=()):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.secrets = secrets

    def path(self, account, project, revision):
        for value in (account, project, revision):
            if str(UUID(value)) != value:
                raise RuleError("INVALID_PATH", "Identificador inválido.", 400)
        path = self.root / account / project / (revision + ".json")
        for parent in [path, *path.parents]:
            if parent == self.root.parent:
                break
            if parent.is_symlink():
                raise RuleError("INVALID_PATH", "Storage inválido.", 400)
        return path

    def write(self, account, project, revision, files, stack):
        digest = validate_files(files, stack, self.secrets)
        path = self.path(account, project, revision)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw = json.dumps(files, ensure_ascii=False, sort_keys=True).encode()
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileExistsError:
            if canonical_hash(json.loads(path.read_text())) != digest:
                raise RuleError(
                    "REVISION_IMMUTABLE", "Uma revisão existente não pode ser alterada."
                ) from None
        finally:
            os.unlink(temporary)
        return digest

    def read(self, revision, stack):
        path = self.path(revision.account_id, revision.project_id, revision.id)
        try:
            files = json.loads(path.read_text())
        except (OSError, ValueError):
            raise RuleError(
                "SOURCE_UNAVAILABLE", "A fonte está indisponível; contate o operador.", 503
            ) from None
        if validate_files(files, stack, self.secrets) != revision.source_hash:
            raise RuleError("SOURCE_INTEGRITY_FAILED", "A integridade da revisão não foi confirmada.", 503)
        return files

    def remove_account(self, account):
        UUID(account)
        path = self.root / account
        if path.is_symlink():
            raise RuleError("INVALID_PATH", "Storage inválido.")
        if path.exists():
            shutil.rmtree(path)
        return {"source_absent": not path.exists()}
