import json

from sqlalchemy import select

from viracase.domain.rules import RuleError
from viracase.infrastructure.models import Project, Revision


def check(repo):
    checked = 0
    issues = []
    with repo.sessions() as session:
        rows = session.execute(
            select(Revision, Project.stack)
            .join(Project, Project.id == Revision.project_id)
            .where(Project.state.not_in(("deleted", "deleting")))
        )
        for revision, stack in rows:
            checked += 1
            try:
                repo.sources.read(revision, stack)
            except RuleError as exc:
                if exc.code not in {"SOURCE_UNAVAILABLE", "SOURCE_INTEGRITY_FAILED"}:
                    raise
                issues.append({"project_id": revision.project_id, "revision_id": revision.id, "code": exc.code})
    return {"revisions_checked": checked, "unavailable": len(issues), "issues": issues}


def main():
    from viracase.infrastructure.composition import compose

    _, engine, repo, _, _ = compose()
    try:
        result = check(repo)
    finally:
        engine.dispose()
    print("SOURCE_STORAGE_AUDIT=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result["unavailable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
