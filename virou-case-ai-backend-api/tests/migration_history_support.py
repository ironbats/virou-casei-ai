from hashlib import sha256

from viracase.infrastructure.models import Revision, new_id
from viracase.infrastructure.repository_dto import project_dto


def install_legacy_revision(repo, actor, project, files, *, origin="edit"):
    with repo.sessions.begin() as db:
        repo.account(db, actor, True)
        current = repo.owned_project(db, actor, project["id"])
        identity = new_id()
        digest = repo.sources.write(actor, current.id, identity, files, current.stack)
        revision = Revision(
            id=identity,
            account_id=actor,
            project_id=current.id,
            source_hash=digest,
            manifest=[
                {"id": sha256(path.encode()).hexdigest()[:24], "path": path, "size": len(content.encode())}
                for path, content in sorted(files.items())
            ],
            origin=origin,
            candidate=False,
        )
        db.add(revision)
        current.current_revision_id, current.version = identity, current.version + 1
        db.flush()
        return project_dto(current)
