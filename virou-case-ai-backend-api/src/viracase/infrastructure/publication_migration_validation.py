import copy
import re
import sqlite3
import time
from functools import lru_cache
from hashlib import sha256

from viracase.domain.rules import RuleError, canonical_hash
from viracase.infrastructure.publication_d1_diagnostics import sql_error_summary
from viracase.infrastructure.publication_sql_review import (
    ANALYSIS_METHOD,
    null_backfill_target,
    split_sql_statements,
    sql_review_findings,
)

POLICY_VERSION = "migration_plan_v1"
LEDGER = "__viroucaseai_migrations"
MESSAGES = {
    "MIGRATION_VALIDATION_FAILED": "A migration nova não passou na validação isolada do schema.",
    "MIGRATION_BASELINE_UNVERIFIABLE": "Não foi possível reconstruir o schema a partir do histórico preservado.",
    "MIGRATION_REVIEW_REQUIRED": "O plano contém SQL que não atende à política de preservação dos dados.",
}


def _migrations(files):
    return {
        path: content for path, content in sorted((files or {}).items())
        if isinstance(path, str) and path.startswith("migrations/") and path.endswith(".sql")
        and isinstance(content, str)
    }


def _fail(code, path, sql, reason, *, statement_index=None, findings=None, source="candidate"):
    issue = {
        "code": code, "reason": reason, "receipt_source": source,
        "path": path, "actual_sha256": sha256(sql.encode()).hexdigest(),
        "message": MESSAGES[code],
        "hint": "Preserve as migrations existentes e corrija somente o arquivo incremental ainda em preparação.",
    }
    if findings:
        issue.update(findings[0])
    if statement_index:
        issue["statement_index"] = statement_index
    raise RuleError(code, MESSAGES[code], details={"migration": {
        "format_version": 1, "category": "migration", "code": code,
        "analysis_method": ANALYSIS_METHOD, "validation_method": "sqlite_isolated_schema",
        "policy_version": POLICY_VERSION, "issues": [issue], "issue_count": 1,
    }})


def _columns(connection):
    tables = connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT GLOB 'sqlite_*' ORDER BY name").fetchall()
    if len(tables) > 1024:
        raise sqlite3.OperationalError("validation object limit exceeded")
    return {
        row[0].casefold(): {column[0].casefold() for column in connection.execute("SELECT name FROM pragma_table_info(?)", [row[0]])}
        for row in tables
    }


def _has_trigger(connection, table):
    return bool(connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'trigger' AND lower(tbl_name) = ? LIMIT 1", [table]
    ).fetchone())


def _authorizer(action, first, second, database, source):
    if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and str(second).casefold() in {"load_extension", "writefile", "readfile"}:
        return sqlite3.SQLITE_DENY
    if database not in {None, "main", "temp"}:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


@lru_cache(maxsize=32)
def _validate_cached(items, baseline_items):
    files, baseline = dict(items), dict(baseline_items)
    connection = sqlite3.connect(":memory:")
    started = time.monotonic()
    calls = 0

    def interrupt():
        nonlocal calls
        calls += 1
        return int(calls > 2000 or time.monotonic() - started > 2)

    approved, total, original_columns, new_started = [], 0, {}, False
    try:
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2_000_000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 2_000_000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA max_page_count = 4096")
        connection.execute("PRAGMA temp.max_page_count = 4096")
        connection.set_authorizer(_authorizer)
        connection.set_progress_handler(interrupt, 1000)
        for path, sql in files.items():
            historical = path in baseline and baseline[path] == sql
            code = "MIGRATION_BASELINE_UNVERIFIABLE" if historical else "MIGRATION_VALIDATION_FAILED"
            statements = split_sql_statements(sql)
            if not statements:
                _fail(code, path, sql, "empty_sql", source="baseline" if historical else "candidate")
            if not historical and not new_started:
                original_columns = _columns(connection)
                new_started = True
            offset = 0
            for number, statement in enumerate(statements, 1):
                start = sql.find(statement, offset)
                offset = start + len(statement)
                findings = sql_review_findings(statement, LEDGER)
                findings = [{
                    **finding,
                    "line": sql.count("\n", 0, start) + finding["line"],
                    "column": finding["column"] + (start - sql.rfind("\n", 0, start) - 1 if finding["line"] == 1 else 0),
                    "statement_index": number,
                } for finding in findings]
                if historical:
                    isolated_findings = [finding for finding in findings if finding["rule"] in {"ledger_access", "external_database", "connection_setting", "unrecognized_sql"}]
                    if isolated_findings:
                        _fail(code, path, sql, "baseline_not_replayable", findings=isolated_findings, source="baseline")
                elif findings:
                    backfill = null_backfill_target(statement)
                    columns = _columns(connection)
                    allowed = (
                        len(findings) == 1 and findings[0]["rule"] == "update_rows" and backfill
                        and backfill["column"] in columns.get(backfill["table"], set())
                        and backfill["column"] not in original_columns.get(backfill["table"], set())
                        and not _has_trigger(connection, backfill["table"])
                        and not re.search(r"(?:password|secret|token|salt|permission|role|admin|auth|credential)", backfill["column"])
                    )
                    if not allowed:
                        _fail("MIGRATION_REVIEW_REQUIRED", path, sql, "unsafe_sql", statement_index=number, findings=findings)
                    approved.append({
                        "path": path, "statement_index": number,
                        "statement_sha256": sha256(statement.encode()).hexdigest(),
                        "scope": "new_column_null_rows", "predicate": "target_column_is_null",
                    })
                try:
                    connection.execute(statement)
                except sqlite3.Error as exc:
                    summary = sql_error_summary([str(exc)])
                    _fail(code, path, sql, summary["reason"], statement_index=number, source="baseline" if historical else "candidate")
                total += 1
                if total > 2000:
                    _fail(code, path, sql, "validation_limit", statement_index=number)
            if historical:
                original_columns = _columns(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            path, sql = next(reversed(files.items()))
            _fail("MIGRATION_VALIDATION_FAILED", path, sql, "foreign_key_violation")
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name"
        ).fetchall()
        return {
            "format_version": 1, "policy_version": POLICY_VERSION, "passed": True,
            "method": "sqlite_isolated_schema", "sqlite_version": sqlite3.sqlite_version,
            "provider_equivalence": False, "remote_execution": False,
            "manifest": {path: sha256(sql.encode()).hexdigest() for path, sql in files.items()},
            "baseline_manifest": {path: sha256(sql.encode()).hexdigest() for path, sql in baseline.items()},
            "preserved": sorted(path for path, sql in baseline.items() if files.get(path) == sql),
            "added": sorted(set(files) - set(baseline)),
            "statement_count": total, "schema_sha256": canonical_hash(schema),
            "approved_backfills": approved,
            "limits": {"max_statements": 2000, "max_execution_seconds": 2, "max_database_pages": 4096},
            "limitations": ["provider_specific_sql", "production_data_constraints", "remote_schema_drift"],
        }
    except sqlite3.Error:
        path, sql = next(reversed(files.items()), ("migrations/unknown.sql", ""))
        _fail("MIGRATION_VALIDATION_FAILED", path, sql, "validation_limit_or_schema_error")
    finally:
        connection.close()


def validate_migration_plan(files, *, baseline_files=None, revision_id=None, previous_evidence=None):
    migrations, baseline = _migrations(files), _migrations(baseline_files)
    if len(migrations) > 512 or sum(len(sql.encode()) for sql in migrations.values()) > 4_000_000:
        path, sql = next(iter(migrations.items()))
        _fail("MIGRATION_VALIDATION_FAILED", path, sql, "validation_limit")
    evidence = _reusable_evidence(files, migrations, baseline_files, previous_evidence)
    if evidence is None:
        evidence = copy.deepcopy(_validate_cached(tuple(migrations.items()), tuple(baseline.items())))
        previous = _retained_evidence(files, migrations, previous_evidence)
        if previous:
            retained = previous["approved_backfills"]
            present = {(item["path"], item["statement_index"], item["statement_sha256"]) for item in evidence["approved_backfills"]}
            evidence["approved_backfills"] = [
                *[item for item in retained if (item["path"], item["statement_index"], item["statement_sha256"]) not in present],
                *evidence["approved_backfills"],
            ]
    if baseline_files is not None:
        evidence["baseline_manifest"] = {path: sha256(sql.encode()).hexdigest() for path, sql in baseline.items()}
        evidence["preserved"] = sorted(path for path, sql in baseline.items() if migrations.get(path) == sql)
        evidence["added"] = sorted(set(migrations) - set(baseline))
    evidence.pop("evidence_sha256", None)
    evidence["artifact_sha256"] = canonical_hash(files)
    if isinstance(revision_id, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", revision_id):
        evidence["revision_id"] = revision_id
    evidence["evidence_sha256"] = canonical_hash(evidence)
    return evidence


def _reusable_evidence(files, migrations, baseline_files, value):
    if not isinstance(value, dict):
        return None
    payload = {key: item for key, item in value.items() if key != "evidence_sha256"}
    manifest = {path: sha256(sql.encode()).hexdigest() for path, sql in migrations.items()}
    baseline_manifest = value.get("baseline_manifest")
    if (
        value.get("format_version") != 1 or value.get("policy_version") != POLICY_VERSION
        or value.get("passed") is not True or value.get("method") != "sqlite_isolated_schema"
        or value.get("provider_equivalence") is not False or value.get("remote_execution") is not False
        or value.get("manifest") != manifest
        or not isinstance(value.get("artifact_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["artifact_sha256"])
        or value.get("evidence_sha256") != canonical_hash(payload)
        or not isinstance(baseline_manifest, dict)
        or any(manifest.get(path) != digest for path, digest in baseline_manifest.items())
        or value.get("preserved") != sorted(baseline_manifest)
        or value.get("added") != sorted(set(manifest) - set(baseline_manifest))
        or not isinstance(value.get("approved_backfills"), list)
    ):
        return None
    for item in value["approved_backfills"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or item["path"] not in migrations or type(item.get("statement_index")) is not int:
            return None
        statements = split_sql_statements(migrations[item["path"]])
        index = item["statement_index"] - 1
        if (
            not 0 <= index < len(statements) or item.get("statement_sha256") != sha256(statements[index].encode()).hexdigest()
            or not null_backfill_target(statements[index]) or item.get("scope") != "new_column_null_rows"
            or item.get("predicate") != "target_column_is_null"
        ):
            return None
    return copy.deepcopy(value)


def _retained_evidence(files, migrations, value):
    previous = value.get("manifest") if isinstance(value, dict) else None
    if not isinstance(previous, dict) or not previous:
        return None
    if any(path not in migrations or sha256(migrations[path].encode()).hexdigest() != digest for path, digest in previous.items()):
        return None
    added = set(migrations) - set(previous)
    if any(path <= max(previous) for path in added):
        return None
    return _reusable_evidence(files, {path: migrations[path] for path in previous}, None, value)
