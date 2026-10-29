from hashlib import sha256

from sqlalchemy import select

from viracase.domain.migration_history import migration_manifest, validate_migration_history
from viracase.domain.rules import RuleError
from viracase.infrastructure.models import Draft, PublicationResource, Revision
from viracase.infrastructure.publication_migrations import confirmed_migrations, preflight_migrations


def migration_inventory(db, actor, pid):
    resource = db.scalar(
        select(PublicationResource).where(
            PublicationResource.account_id == actor,
            PublicationResource.project_id == pid,
        )
    )
    return dict(resource.inventory or {}) if resource else {}


def validate_published_migrations(db, actor, pid, files):
    preflight_migrations(files, migration_inventory(db, actor, pid))


def migration_evidence_for_revision(db, actor, revision_id):
    row = db.scalar(
        select(Draft).where(Draft.account_id == actor, Draft.kind == "migration_plan", Draft.target == revision_id)
    )
    return dict(row.data) if row and isinstance(row.data, dict) else {}


def migration_validation_for_revision(db, actor, revision_id):
    value = migration_evidence_for_revision(db, actor, revision_id).get("migration_validation")
    return dict(value) if isinstance(value, dict) else None


def revision_baseline(repo, db, project, base_revision_id):
    if base_revision_id is None:
        return {}
    revision = repo.owned_revision(db, project.account_id, project.id, base_revision_id)
    if revision.candidate:
        raise RuleError("VERSION_CONFLICT", "Uma proposta não aplicada não pode substituir a revisão-base consolidada.", 412)
    return repo.sources.read(revision, project.stack)


def validate_revision_history(repo, db, project, files, *, base_revision_id, baseline_files=None, operation="save"):
    baseline = revision_baseline(repo, db, project, base_revision_id) if baseline_files is None else baseline_files
    inventory = migration_inventory(db, project.account_id, project.id)
    try:
        preflight_migrations(baseline, inventory)
    except RuleError as exc:
        if exc.code != "MIGRATION_CHECKSUM_CONFLICT":
            raise
        raise RuleError(
            "MIGRATION_BASELINE_INCONSISTENT",
            "A revisão-base já diverge do histórico confirmado do banco. Compare as fontes e os recibos antes de consolidar outra revisão.",
            409,
            {**exc.details, "base_revision_id": base_revision_id, "migration_operation": operation},
        ) from None
    history = validate_migration_history(baseline, files, base_revision_id=base_revision_id, operation=operation)
    preflight_migrations(files, inventory)
    return history


def validate_revision_migrations(
    repo, db, project, files, *, base_revision_id, revision_id=None,
    baseline_files=None, operation="save", previous_evidence=None,
):
    from viracase.infrastructure.publication_migration_validation import validate_migration_plan

    baseline = revision_baseline(repo, db, project, base_revision_id) if baseline_files is None else baseline_files
    history = validate_revision_history(
        repo, db, project, files, base_revision_id=base_revision_id,
        baseline_files=baseline, operation=operation,
    )
    if previous_evidence is None and base_revision_id is not None:
        previous_evidence = migration_validation_for_revision(db, project.account_id, base_revision_id)
    evidence = validate_migration_plan(
        files, baseline_files=baseline, revision_id=revision_id, previous_evidence=previous_evidence,
    )
    return {"migration_history": history, "migration_validation": evidence}


def has_restorable_missing_migration(evidence, files):
    migrations = evidence.get("published_database_migrations") or {}
    if not isinstance(migrations, dict) or not isinstance(migrations.get("files"), list):
        return False
    return any(
        item.get("status") == "historical_source_verified"
        and isinstance(item.get("path"), str)
        and item.get("path") not in files
        and isinstance(item.get("content"), str)
        and sha256(item["content"].encode()).hexdigest() == item.get("sha256")
        for item in migrations["files"]
        if isinstance(item, dict)
    )


def published_migration_context(repo, db, project, files, *, base_revision_id=None, baseline_files=None):
    inventory = migration_inventory(db, project.account_id, project.id)
    receipts = confirmed_migrations(inventory)
    baseline_id = base_revision_id if base_revision_id is not None else project.current_revision_id
    baseline = revision_baseline(repo, db, project, baseline_id) if baseline_files is None else baseline_files
    protected = migration_manifest(baseline)
    conflicts = [
        {"path": path, "expected_sha256": digest, "actual_sha256": sha256(baseline[path].encode()).hexdigest() if path in baseline else None}
        for path, digest in sorted(receipts.items())
        if path not in baseline or sha256(baseline[path].encode()).hexdigest() != digest
    ]
    entries = {}
    missing = set()
    for path, digest in sorted(receipts.items()):
        matches = path in files and sha256(files[path].encode()).hexdigest() == digest
        entries[path] = {
            "path": path,
            "sha256": digest,
            "status": "current_matches" if matches else "source_unavailable",
        }
        if not matches:
            missing.add(path)
    if missing:
        scope = (
            Revision.account_id == project.account_id,
            Revision.project_id == project.id,
        )
        preferred = [
            value
            for key in ("published_revision_id", "candidate_revision_id")
            if isinstance(value := inventory.get(key), str)
        ]
        revisions = list(db.scalars(select(Revision).where(*scope, Revision.id.in_(preferred))))
        revisions.extend(
            db.scalars(
                select(Revision)
                .where(*scope)
                .order_by(Revision.created_at.desc(), Revision.id.desc())
                .limit(100)
            )
        )
        visited = set()
        for revision in revisions:
            if not missing:
                break
            if revision.id in visited:
                continue
            visited.add(revision.id)
            try:
                historical = repo.sources.read(revision, project.stack)
            except RuleError:
                continue
            for path in sorted(missing):
                content = historical.get(path)
                if isinstance(content, str) and sha256(content.encode()).hexdigest() == receipts[path]:
                    entries[path].update(
                        status="historical_source_verified",
                        content=content,
                        source_revision_id=revision.id,
                    )
                    missing.remove(path)
    return {
        "format_version": 2,
        "base_revision_id": baseline_id,
        "protected_files": protected,
        "files": list(entries.values()),
        "baseline_conflicts": conflicts,
    }
