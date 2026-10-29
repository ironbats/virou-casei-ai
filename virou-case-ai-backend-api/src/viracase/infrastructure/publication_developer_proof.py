from hashlib import sha256

from viracase.domain.publication_no_effect import (
    EXECUTION_PROTOCOL,
    MISSING_EVIDENCE,
    POLICY_VERSION,
    constant_column_addition,
    decide_no_effect,
)
from viracase.domain.rules import RuleError, canonical_hash
from viracase.infrastructure.publication_d1_diagnostics import COMPILE_REASONS
from viracase.infrastructure.publication_migration_validation import validate_migration_plan
from viracase.infrastructure.publication_migrations import LEDGER
from viracase.infrastructure.publication_recovery_evidence import _structural_statement
from viracase.infrastructure.publication_sql_review import (
    TOKEN,
    _tokens,
    split_sql_statements,
    sql_review_findings,
)

MAX_SCHEMA_OBJECTS = 1024
MAX_COLUMNS = 2000
SCHEMA_FILTER = (
    "type IN ('table', 'index', 'view', 'trigger') "
    "AND name NOT GLOB 'sqlite_*' AND name NOT GLOB '_cf_*' AND name <> ?"
)
UPLOAD_STAGES = {"publication_uploading", "publication_routing", "publication_verifying", "publication_verified"}


class ObservationIncomplete(Exception):
    pass


def _object(value):
    return value if isinstance(value, dict) else {}


def _digest(value):
    return sha256(value.encode()).hexdigest()


def _rows(gateway, database_id, sql, params=None):
    response = gateway.query(database_id, sql, params)
    if (
        not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], dict)
        or response[0].get("success") is not True or not isinstance(response[0].get("results"), list)
        or response[0].get("truncated") or response[0].get("has_more")
        or _object(response[0].get("meta")).get("truncated")
        or _object(response[0].get("meta")).get("has_more")
    ):
        raise ObservationIncomplete()
    rows = response[0]["results"]
    if any(not isinstance(row, dict) for row in rows):
        raise ObservationIncomplete()
    return rows


def _observe_history(gateway, database_id):
    counts = _rows(gateway, database_id, f"SELECT COUNT(*) AS receipt_count FROM {LEDGER}")
    if (
        len(counts) != 1 or type(counts[0].get("receipt_count")) is not int
        or not 0 <= counts[0]["receipt_count"] <= 512
    ):
        raise ObservationIncomplete()
    rows = _rows(gateway, database_id, f"SELECT name, sha256 FROM {LEDGER} ORDER BY name LIMIT 513")
    if len(rows) != counts[0]["receipt_count"] or any(
        not isinstance(row.get("name"), str) or not isinstance(row.get("sha256"), str) for row in rows
    ):
        raise ObservationIncomplete()
    history = {row["name"]: row["sha256"] for row in rows}
    if len(history) != len(rows) or [row["name"] for row in rows] != sorted(history):
        raise ObservationIncomplete()
    return history


def _observe_schema(gateway, database_id):
    count = _rows(gateway, database_id, f"SELECT COUNT(*) AS object_count FROM sqlite_schema WHERE {SCHEMA_FILTER}", [LEDGER])
    if (
        len(count) != 1 or type(count[0].get("object_count")) is not int
        or not 0 <= count[0]["object_count"] <= MAX_SCHEMA_OBJECTS
    ):
        raise ObservationIncomplete()
    rows = _rows(
        gateway, database_id,
        f"SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE {SCHEMA_FILTER} ORDER BY type, name LIMIT 1025",
        [LEDGER],
    )
    if len(rows) != count[0]["object_count"] or any(
        row.get("type") not in ("table", "index", "view", "trigger")
        or not isinstance(row.get("name"), str) or not isinstance(row.get("tbl_name"), str)
        or row.get("sql") is not None and not isinstance(row["sql"], str)
        for row in rows
    ):
        raise ObservationIncomplete()
    identities = [(row["type"], row["name"]) for row in rows]
    if len(set(identities)) != len(rows) or identities != sorted(identities):
        raise ObservationIncomplete()
    values = [[row["type"], row["name"], row["tbl_name"], row.get("sql")] for row in rows]
    return {"sha256": canonical_hash(values), "object_count": len(rows)}, rows


def _observe_column(gateway, database_id, addition, schema):
    tables = [row for row in schema if row["type"] == "table" and row["name"].casefold() == addition.table]
    if len(tables) != 1 or not isinstance(tables[0].get("sql"), str):
        return False, False, None
    definition = _tokens(tables[0]["sql"])
    ordinary = (
        len(definition) > 3 and definition[0].kind == definition[1].kind == "word"
        and definition[0].value == "CREATE" and definition[1].value == "TABLE"
    )
    if not ordinary:
        return False, False, None
    rows = _rows(
        gateway, database_id,
        "SELECT cid, name, type, \"notnull\", dflt_value, pk, hidden FROM pragma_table_xinfo(?) ORDER BY cid LIMIT 2001",
        [tables[0]["name"]],
    )
    if (
        not 1 <= len(rows) <= MAX_COLUMNS
        or any(
            type(row.get("cid")) is not int or not isinstance(row.get("name"), str)
            or not isinstance(row.get("type"), str)
            or type(row.get("notnull")) is not int or row["notnull"] not in (0, 1)
            or "dflt_value" not in row or row["dflt_value"] is not None and not isinstance(row["dflt_value"], str)
            or type(row.get("pk")) is not int or row["pk"] < 0
            or type(row.get("hidden")) is not int or row["hidden"] not in (0, 1, 2, 3)
            for row in rows
        )
        or [row["cid"] for row in rows] != list(range(len(rows)))
        or len({row["name"].casefold() for row in rows}) != len(rows)
    ):
        raise ObservationIncomplete()
    count = _rows(gateway, database_id, "SELECT COUNT(*) AS column_count FROM pragma_table_xinfo(?)", [tables[0]["name"]])
    if len(count) != 1 or type(count[0].get("column_count")) is not int or count[0]["column_count"] != len(rows):
        raise ObservationIncomplete()
    return True, addition.column not in {row["name"].casefold() for row in rows}, canonical_hash(rows)


def _pre_upload(context):
    result, inventory, binding = context["result"], context["inventory"], context["binding"]
    attempt = _object(result.get("publication_attempt"))
    revision = context["revision_id"]
    activity = result.get("activity", [])
    if not isinstance(activity, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("stage"), str) for item in activity
    ):
        return False
    failure_stage = _object(result.get("publication_failure")).get("stage")
    if failure_stage is not None and not isinstance(failure_stage, str):
        return False
    return not (
        type(attempt.get("version")) is not int or attempt["version"] != 1
        or attempt.get("revision_id") != revision or attempt.get("stage") != "publication_migrating"
        or attempt.get("upload_started") is not False or attempt.get("deployment_id")
        or binding.get("upload_started") is not False
        or inventory.get("candidate_revision_id") == revision
        or failure_stage in UPLOAD_STAGES
        or any(item["stage"] in UPLOAD_STAGES for item in activity)
        or result.get("deployment_id") or result.get("verified_at")
    )


def _durable_no_effect(pending, statements):
    if (
        type(pending.get("protocol_version")) is not int or pending["protocol_version"] != 2
        or type(pending.get("statement_count")) is not int or pending["statement_count"] != len(statements)
        or len(statements) != 1
    ):
        return False
    provider = _object(pending.get("provider"))
    if provider.get("effect_outcome") == "confirmed" or provider.get("confirmed_statements") or pending.get("sql_result"):
        return False
    if pending.get("phase") == "prepared" and pending.get("effect_outcome") == "not_sent":
        return not provider and "schema_after" not in pending
    if pending.get("phase") != "sql_rejected":
        return False
    if provider.get("method") != "POST" or provider.get("route") != "/accounts/{account}/d1/database/{database}/query":
        return False
    if pending.get("effect_outcome") == "not_sent" and provider.get("effect_outcome") == "not_sent":
        return provider.get("status") is None and (
            provider.get("transport_error") in ("ConnectError", "ConnectTimeout", "PoolTimeout")
            or provider.get("reason") == "local_dispatch_blocked"
        )
    return (
        pending.get("effect_outcome") == provider.get("effect_outcome") == "none"
        and isinstance(provider.get("reason"), str) and provider["reason"] in COMPILE_REASONS
        and type(provider.get("statement_count")) is int and provider["statement_count"] == 1
        and type(provider.get("confirmed_statements")) is int and provider["confirmed_statements"] == 0
        and type(provider.get("status")) is int and provider["status"] in {200, 400}
    )


def evaluate_developer_no_effect(context, assessment):
    binding, inventory, files = context["binding"], context["inventory"], context["files"]
    pending = _object(inventory.get("migration_in_progress"))
    identity = _object(assessment.get("identity"))
    checks = _object(assessment.get("checks"))
    migration = _object(identity.get("migration"))
    name = pending.get("name")
    sql = files.get(name) if isinstance(name, str) else None
    sql = sql if isinstance(sql, str) else ""
    statements = split_sql_statements(sql)
    tokens = _tokens(sql)
    complete_tokens = not any(match.lastgroup == "block_comment" and not match.group().endswith("*/") for match in TOKEN.finditer(sql))
    addition = constant_column_addition(tokens) if len(statements) == 1 and complete_tokens and not sql_review_findings(sql, LEDGER) else None
    contains_dml = any(token.kind == "word" and token.value in {"UPDATE", "INSERT", "DELETE", "REPLACE", "MERGE", "UPSERT"} for token in tokens)
    manifest = {path: _digest(content) for path, content in sorted(files.items()) if path.startswith("migrations/") and path.endswith(".sql")}
    prefix = {path: files[path] for path in manifest if isinstance(name, str) and path < name}
    prefix_manifest = {path: manifest[path] for path in prefix}
    local_history = _object(inventory.get("migrations"))
    source_hash = canonical_hash(files)
    durable = _durable_no_effect(pending, statements)
    unit = _object(pending.get("execution_unit"))
    previous = _object(inventory.get("migration_last_attempt"))
    metadata_valid = (
        all(key not in pending or isinstance(pending[key], dict) for key in ("execution_unit", "provider", "sql_result", "schema_after"))
        and ("migration_last_attempt" not in inventory or isinstance(inventory["migration_last_attempt"], dict))
        and all(key not in context["result"] or isinstance(context["result"][key], dict) for key in ("publication_attempt", "publication_failure"))
        and isinstance(pending.get("phase"), str) and isinstance(pending.get("effect_outcome"), str)
        and isinstance(assessment.get("classification"), str)
    )
    previous_uncertain = (
        previous.get("operation_id") == binding.get("operation_id") and previous.get("name") != name
        and previous.get("phase") != "receipt_confirmed"
        and not (previous.get("phase") in ("prepared", "sql_rejected") and previous.get("effect_outcome") in ("none", "not_sent"))
    )
    proofs = {
        "remote_identity": context.get("remote_identity_verified") is True
        and context.get("remote_account_id") == binding.get("remote_account_id") == _object(context.get("policy")).get("account_id")
        and context["database_id"] == binding.get("database_id") == inventory.get("database_id"),
        "operation_binding": all(pending.get(key) == identity.get(key) == binding.get(key) and bool(binding.get(key)) for key in ("operation_id", "revision_id"))
        and identity.get("database_id") == context["database_id"],
        "immutable_source": bool(sql) and binding.get("source_hash") == source_hash == context.get("files_sha256")
        and pending.get("sha256") == _digest(sql) == migration.get("sha256") and checks.get("source") == "matched",
        "complete_history": checks.get("history") == "matched" and local_history == prefix_manifest
        and assessment.get("remote_history_sha256") == canonical_hash(prefix_manifest),
        "absent_receipt": checks.get("receipt") == "absent" and name not in local_history
        and pending.get("phase") not in ("sql_confirmed", "receipt_sending", "receipt_confirmed")
        and pending.get("effect_outcome") != "confirmed" and not pending.get("sql_result")
        and not _object(pending.get("provider")).get("confirmed_statements")
        and _object(pending.get("provider")).get("effect_outcome") != "confirmed",
        "pre_upload_checkpoint": _pre_upload(context),
        "no_additional_effects": not any(inventory.get(key) or context["result"].get(key) for key in (
            "pending_execution_units", "migration_batch_pending", "resource_effect_pending", "upload_in_progress", "receipt_write_started",
        )) and not previous_uncertain
        and not _object(_object(context["result"].get("publication_resolution")).get("attempt")).get("receipt_write_started"),
        "intact_baseline": False,
        "execution_unit": False,
        "complete_remote_schema": False,
        "unchanged_schema": False,
        "ordinary_target_table": False,
        "absent_target_column": False,
        "durable_execution_result": durable,
        "recorded_metadata": metadata_valid,
    }
    observed, columns_hash, baseline_hash = None, None, None
    try:
        history_assessed = proofs["complete_history"]
        proofs["complete_history"] = False
        history = _observe_history(context["gateway"], context["database_id"])
        proofs["complete_history"] = history_assessed and history == prefix_manifest
        baseline = validate_migration_plan(prefix, baseline_files=prefix, revision_id=context["revision_id"])
        baseline_hash = baseline["schema_sha256"]
        expected_unit = {
            "version": 1, "protocol": EXECUTION_PROTOCOL, "database_id": context["database_id"],
            "sql_sha256": _digest(sql), "statement_count": len(statements),
            "statement_sha256": [_digest(statement) for statement in statements],
            "parameters_sha256": canonical_hash([]), "confirmed_prefix_sha256": canonical_hash(prefix_manifest),
            "baseline_schema_sha256": baseline_hash, "transport": "cloudflare_d1_rest",
            "atomicity": "sqlite_single_statement" if len(statements) == 1 else "unproven_batch",
        }
        proofs["execution_unit"] = (
            type(unit.get("version")) is int and type(unit.get("statement_count")) is int
            and type(pending.get("statement_count")) is int and pending["statement_count"] == len(statements)
            and (durable or pending.get("phase") == "sql_sending" and pending.get("effect_outcome") == "unknown")
            and all(unit.get(key) == value for key, value in expected_unit.items())
        ) or ("execution_unit" not in pending and durable and proofs["operation_binding"] and proofs["immutable_source"])
        proofs["intact_baseline"] = baseline.get("passed") is True and proofs["complete_history"] and (
            unit.get("baseline_schema_sha256") == baseline_hash or not unit and durable
        )
        observed, schema = _observe_schema(context["gateway"], context["database_id"])
        assessed_schema = _object(assessment.get("schema"))
        proofs["complete_remote_schema"] = not assessed_schema or observed["sha256"] == assessed_schema.get("observed_sha256")
        proofs["unchanged_schema"] = observed["sha256"] == baseline_hash
        if addition:
            ordinary, absent, columns_hash = _observe_column(context["gateway"], context["database_id"], addition, schema)
            proofs["ordinary_target_table"], proofs["absent_target_column"] = ordinary, absent
            confirmed_schema, _ = _observe_schema(context["gateway"], context["database_id"])
            proofs["complete_remote_schema"] = proofs["complete_remote_schema"] and confirmed_schema == observed
        history_verified = proofs["complete_history"]
        proofs["complete_history"] = False
        confirmed_history = _observe_history(context["gateway"], context["database_id"])
        proofs["complete_history"] = history_verified and confirmed_history == history
    except ObservationIncomplete:
        proofs["complete_remote_schema"] = False
    except RuleError as exc:
        if exc.code not in {"MIGRATION_BASELINE_UNVERIFIABLE", "MIGRATION_VALIDATION_FAILED", "MIGRATION_REVIEW_REQUIRED"}:
            raise
    durable_supported = bool(
        durable and len(statements) == 1 and complete_tokens
        and (addition or _structural_statement(statements[0]) in {"create_table", "create_index", "create_view"})
    )
    decision = decide_no_effect(
        proofs, classification=assessment.get("classification") if isinstance(assessment.get("classification"), str) else "unknown", statement_count=len(statements),
        contains_dml=contains_dml, structural=addition is not None, durable=durable_supported,
    )
    evidence = {
        "policy_version": POLICY_VERSION, "proofs": proofs,
        "binding_sha256": canonical_hash(binding), "assessment_sha256": assessment.get("evidence_sha256"),
        "execution_unit_sha256": canonical_hash(unit), "source_sha256": source_hash,
        "baseline_sha256": baseline_hash, "observed_schema": observed, "columns_sha256": columns_hash,
        "statement_manifest_sha256": canonical_hash([_digest(statement) for statement in statements]),
    }
    return {
        **decision, "policy_version": POLICY_VERSION, "proofs": proofs,
        "missing_evidence_messages": [MISSING_EVIDENCE[name] for name in decision["missing_evidence"]],
        "evidence_sha256": canonical_hash(evidence), "evidence": evidence,
    }
