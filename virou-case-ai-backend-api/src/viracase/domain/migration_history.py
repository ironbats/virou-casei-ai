import posixpath
import unicodedata
from hashlib import sha256

from viracase.domain.rules import RuleError

POLICY_VERSION = "migration-history-v1"


def migration_path(path):
    return isinstance(path, str) and path.startswith("migrations/") and path.endswith(".sql")


def path_identity(path):
    return unicodedata.normalize("NFC", posixpath.normpath(path.replace("\\", "/"))).casefold()


def migration_order(path):
    return path


def migration_manifest(files):
    return [
        {"path": path, "sha256": sha256(content.encode()).hexdigest()}
        for path, content in sorted(files.items())
        if migration_path(path)
    ]


def _failure(code, message, base_revision_id, operation, issues):
    raise RuleError(
        code,
        message,
        409,
        {
            "migration_history": {
                "format_version": 1,
                "policy_version": POLICY_VERSION,
                "base_revision_id": base_revision_id,
                "operation": operation,
                "issues": issues[:80],
                "issue_count": len(issues),
            }
        },
    )


def validate_migration_history(baseline_files, files, *, base_revision_id=None, operation="save"):
    baseline = {item["path"]: item["sha256"] for item in migration_manifest(baseline_files)}
    protected = {path_identity(path): path for path in baseline}
    issues = []
    for path, expected in baseline.items():
        actual = sha256(files[path].encode()).hexdigest() if isinstance(files.get(path), str) else None
        if actual != expected:
            issues.append(
                {
                    "path": path,
                    "operation": "delete" if path not in files else "modify",
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                }
            )
    seen = {}
    for path in files:
        if not isinstance(path, str):
            continue
        identity = path_identity(path)
        if identity in protected and path != protected[identity]:
            issues.append({"path": protected[identity], "operation": "rename_or_alias"})
        if not identity.startswith("migrations/") or not identity.endswith(".sql"):
            continue
        if identity in seen and path != seen[identity]:
            issues.append({"path": seen[identity], "operation": "duplicate_path"})
        seen[identity] = path
    if issues:
        _failure(
            "MIGRATION_HISTORY_IMMUTABLE",
            "Uma migration já consolidada não pode ser alterada, removida ou renomeada. Crie uma nova migration incremental.",
            base_revision_id,
            operation,
            issues,
        )
    current = {item["path"]: item["sha256"] for item in migration_manifest(files)}
    added = current.keys() - baseline.keys()
    if baseline:
        latest = max(baseline, key=migration_order)
        out_of_order = [
            path
            for path in sorted(added, key=migration_order)
            if migration_order(path) <= migration_order(latest)
        ]
        if out_of_order:
            _failure(
                "MIGRATION_ORDER_INVALID",
                "Novas migrations devem vir depois do histórico consolidado. Use um novo nome incremental.",
                base_revision_id,
                operation,
                [
                    {"path": path, "operation": "insert_before_history", "after_path": latest}
                    for path in out_of_order
                ],
            )
    return {
        "format_version": 1,
        "policy_version": POLICY_VERSION,
        "base_revision_id": base_revision_id,
        "preserved": [
            {"path": path, "sha256": baseline[path]} for path in sorted(baseline, key=migration_order)
        ],
        "added": [{"path": path, "sha256": current[path]} for path in sorted(added, key=migration_order)],
    }
