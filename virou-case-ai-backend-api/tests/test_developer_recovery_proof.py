from copy import deepcopy
from hashlib import sha256

import pytest
from test_publication_migration_integrity import Database

from viracase.domain.publication_no_effect import constant_column_addition
from viracase.domain.rules import RuleError, canonical_hash
from viracase.infrastructure.cloudflare_gateway import PublicationUncertain
from viracase.infrastructure.publication_developer_proof import evaluate_developer_no_effect
from viracase.infrastructure.publication_migrations import LEDGER, apply_migrations
from viracase.infrastructure.publication_recovery_evidence import assess_migration_recovery
from viracase.infrastructure.publication_sql_review import _tokens, split_sql_statements

FIRST = "migrations/0001.sql"
LAST = "migrations/0005_add_salt_to_users_and_update_admin.sql"
BASE = "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL); INSERT INTO users VALUES (1, 'preserved');"
SQL = "ALTER TABLE users ADD COLUMN salt TEXT NOT NULL DEFAULT 'temp_salt_placeholder';"
OPERATION = "ce81e74c-8604-477b-95ff-ff2f703bf730"
REVISION = "68cab41e-054f-4e70-a74f-20961d197de3"


def digest(value):
    return sha256(value.encode()).hexdigest()


class UncertainDatabase(Database):
    def query(self, database_id, sql, params=None):
        if sql == SQL:
            self.calls.append((sql, params))
            failure = PublicationUncertain()
            failure.provider_diagnostic = {
                "method": "POST", "route": "/accounts/{account}/d1/database/{database}/query",
                "status": 400, "provider_error_codes": [7500], "effect_outcome": "unknown",
            }
            raise failure
        return super().query(database_id, sql, params)


@pytest.fixture
def evidence_case():
    gateway = UncertainDatabase()
    gateway.seed({FIRST: BASE})
    files = {FIRST: BASE, LAST: SQL, "src/index.ts": "export default {}"}
    inventory = {"database_id": "test-database", "migrations": {FIRST: digest(BASE)}}
    result = {
        "publication_attempt": {"version": 1, "revision_id": REVISION, "stage": "publication_migrating", "upload_started": False},
        "external_effect_pending": True,
    }
    checkpoints = []

    def checkpoint(stage, changes=None, external=None):
        changes = deepcopy(changes or {})
        for key in ("migration_in_progress", "migration_last_attempt", "migration_plan"):
            if isinstance(changes.get(key), dict):
                changes[key].update(operation_id=OPERATION, revision_id=REVISION)
        inventory.update(changes)
        checkpoints.append(deepcopy(inventory))
        return {"inventory": deepcopy(inventory)}

    with pytest.raises(PublicationUncertain):
        apply_migrations(
            gateway, "test-database", {FIRST: BASE, LAST: SQL}, inventory, checkpoint,
            revision_id=REVISION, source_files=files,
        )
    source_hash = canonical_hash(files)
    context = {
        "gateway": gateway, "files": files, "files_sha256": source_hash,
        "revision_id": REVISION, "database_id": "test-database", "inventory": inventory,
        "result": result, "remote_identity_verified": True, "remote_account_id": "cloudflare-account",
        "policy": {"account_id": "cloudflare-account"},
        "binding": {
            "operation_id": OPERATION, "revision_id": REVISION, "source_hash": source_hash,
            "database_id": "test-database", "remote_account_id": "cloudflare-account", "upload_started": False,
        },
    }
    gateway.calls.clear()
    yield context, checkpoints
    gateway.connection.close()


def evaluate(context):
    assessment = assess_migration_recovery(
        context["gateway"], context["database_id"], context["files"], context["inventory"],
        operation_id=OPERATION, revision_id=REVISION,
    )
    return evaluate_developer_no_effect(context, assessment)


def test_complete_real_executor_evidence_allows_only_no_effect_and_preserves_data(evidence_case):
    context, checkpoints = evidence_case
    before = deepcopy(context["inventory"])
    decision = evaluate(context)
    assert decision["eligible"] is True, decision
    assert decision["reason_code"] == "NO_EFFECT_PROVEN"
    assert decision["origin"] == "automated_no_effect_proof"
    assert all(decision["proofs"].get(key) is True for key in (
        "execution_unit", "intact_baseline", "complete_remote_schema", "absent_target_column", "pre_upload_checkpoint",
    ))
    assert context["inventory"] == before
    assert context["gateway"].connection.execute("SELECT name FROM users").fetchone()[0] == "preserved"
    assert context["gateway"].connection.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0] == 1
    assert all(sql.startswith("SELECT") for sql, _ in context["gateway"].calls)
    prepared = next(item["migration_in_progress"] for item in checkpoints if (item.get("migration_in_progress") or {}).get("phase") == "prepared")
    assert prepared["execution_unit"] == context["inventory"]["migration_in_progress"]["execution_unit"]
    assert prepared["execution_unit"]["sql_sha256"] == digest(SQL)
    assert evaluate(context)["evidence_sha256"] == decision["evidence_sha256"]


@pytest.mark.parametrize("metadata", [
    "prepared_confirmed_provider", "unsent_http_response", "opaque_activity", "null_activity",
    "opaque_activity_item", "opaque_stage", "opaque_unit", "opaque_previous_attempt",
    "opaque_phase", "opaque_provider", "unsent_wrong_route", "unsent_wrong_method", "opaque_failure",
])
def test_malformed_or_contradictory_historical_records_do_not_prove_no_effect(evidence_case, metadata):
    context, _ = evidence_case
    pending = context["inventory"]["migration_in_progress"]
    if metadata == "prepared_confirmed_provider":
        pending.pop("execution_unit")
        pending.update(phase="prepared", effect_outcome="not_sent", provider={"effect_outcome": "confirmed", "status": 200})
    elif metadata == "unsent_http_response":
        pending.pop("execution_unit")
        pending.update(phase="sql_rejected", effect_outcome="not_sent", provider={
            "effect_outcome": "not_sent", "status": 200, "transport_error": "ConnectTimeout",
        })
    elif metadata == "opaque_activity":
        context["result"]["activity"] = "publication_uploading"
    elif metadata == "null_activity":
        context["result"]["activity"] = None
    elif metadata == "opaque_activity_item":
        context["result"]["activity"] = ["publication_uploading"]
    elif metadata == "opaque_stage":
        context["result"]["activity"] = [{"stage": ["publication_uploading"]}]
    elif metadata == "opaque_unit":
        pending.update(phase="prepared", effect_outcome="not_sent", provider={}, execution_unit=["unknown"])
    elif metadata == "opaque_previous_attempt":
        context["inventory"]["migration_last_attempt"] = "unknown"
    elif metadata == "opaque_phase":
        pending["phase"] = []
    elif metadata in ("unsent_wrong_route", "unsent_wrong_method"):
        pending.pop("execution_unit")
        pending.update(phase="sql_rejected", effect_outcome="not_sent", provider={
            "effect_outcome": "not_sent", "transport_error": "ConnectTimeout",
            "method": "GET" if metadata == "unsent_wrong_method" else "POST",
            "route": "/accounts/{account}/d1/database/{database}/query" if metadata == "unsent_wrong_method" else "/other",
        })
    elif metadata == "opaque_failure":
        context["result"]["publication_failure"] = ["publication_uploading"]
    else:
        pending.update(phase="prepared", effect_outcome="not_sent", provider=["unknown"])
    decision = evaluate(context)
    assert decision["eligible"] is False
    assert decision["missing_evidence"]


@pytest.mark.parametrize("location", ["schema", "ledger"])
@pytest.mark.parametrize("flag", ["truncated", "has_more", "meta_has_more"])
def test_provider_partial_flags_cover_schema_and_ledger(evidence_case, location, flag):
    context, _ = evidence_case
    assessment = assess_migration_recovery(context["gateway"], context["database_id"], context["files"], context["inventory"], operation_id=OPERATION, revision_id=REVISION)
    original = context["gateway"].query

    def query(database_id, sql, params=None):
        response = original(database_id, sql, params)
        match = "ORDER BY type, name LIMIT" in sql if location == "schema" else sql.startswith("SELECT name, sha256")
        if match:
            if flag == "meta_has_more":
                response[0]["meta"] = {"has_more": True}
            else:
                response[0][flag] = True
        return response

    context["gateway"].query = query
    decision = evaluate_developer_no_effect(context, assessment)
    assert decision["eligible"] is False
    assert ("complete_remote_schema" if location == "schema" else "complete_history") in decision["missing_evidence"]


def test_additional_receipt_after_initial_read_invalidates_current_proof(evidence_case):
    context, _ = evidence_case
    original = context["gateway"].query

    def query(database_id, sql, params=None):
        response = original(database_id, sql, params)
        if "AS column_count" in sql:
            context["gateway"].connection.execute(f"INSERT INTO {LEDGER}(name, sha256) VALUES (?, ?)", ["migrations/9999.sql", "f" * 64])
        return response

    context["gateway"].query = query
    decision = evaluate(context)
    assert decision["eligible"] is False and "complete_history" in decision["missing_evidence"]


@pytest.mark.parametrize("field", ["cid", "name", "type", "notnull", "dflt_value", "pk", "hidden"])
def test_column_inventory_must_include_complete_column_metadata(evidence_case, field):
    context, _ = evidence_case
    original = context["gateway"].query

    def query(database_id, sql, params=None):
        response = original(database_id, sql, params)
        if sql.startswith("SELECT cid, name"):
            response[0]["results"][0].pop(field)
        return response

    context["gateway"].query = query
    decision = evaluate(context)
    assert decision["eligible"] is False and "complete_remote_schema" in decision["missing_evidence"]


@pytest.mark.parametrize("field", [
    "version", "protocol", "database_id", "sql_sha256", "statement_count", "statement_sha256",
    "parameters_sha256", "confirmed_prefix_sha256", "baseline_schema_sha256", "transport", "atomicity",
])
def test_missing_exact_dispatch_field_denies_structural_recovery(evidence_case, field):
    context, _ = evidence_case
    context["inventory"]["migration_in_progress"]["execution_unit"].pop(field)
    decision = evaluate(context)
    assert decision["eligible"] is False
    assert "execution_unit" in decision["missing_evidence"]


def test_observed_incident_sql_and_generic_400_are_not_enough_for_legacy_attempt(evidence_case):
    context, _ = evidence_case
    context["inventory"]["migration_in_progress"].pop("execution_unit")
    decision = evaluate(context)
    assert decision["eligible"] is False and decision["reason_code"] == "PROOF_INCOMPLETE"
    assert "execution_unit" in decision["missing_evidence"]


@pytest.mark.parametrize("alteration,missing", [
    ("pending_operation", "operation_binding"), ("pending_revision", "operation_binding"),
    ("remote_identity", "remote_identity"), ("remote_account", "remote_identity"),
    ("source_hash", "immutable_source"), ("missing_stage", "pre_upload_checkpoint"),
    ("contradictory_upload_event", "pre_upload_checkpoint"), ("candidate_revision", "pre_upload_checkpoint"),
    ("missing_upload_flag", "pre_upload_checkpoint"), ("other_unit", "no_additional_effects"),
    ("other_pending_attempt", "no_additional_effects"), ("success_response", "absent_receipt"),
    ("phase", "execution_unit"), ("boolean_count", "execution_unit"),
])
def test_binding_upload_and_other_effect_guards(evidence_case, alteration, missing):
    context, _ = evidence_case
    pending = context["inventory"]["migration_in_progress"]
    if alteration == "pending_operation":
        pending["operation_id"] = "another-operation"
    elif alteration == "pending_revision":
        pending["revision_id"] = "another-revision"
    elif alteration == "remote_identity":
        context["remote_identity_verified"] = False
    elif alteration == "remote_account":
        context["remote_account_id"] = "another-account"
    elif alteration == "source_hash":
        context["binding"]["source_hash"] = "0" * 64
    elif alteration == "missing_stage":
        context["result"]["publication_attempt"].pop("stage")
    elif alteration == "contradictory_upload_event":
        context["result"]["activity"] = [{"stage": "publication_uploading"}]
    elif alteration == "candidate_revision":
        context["inventory"]["candidate_revision_id"] = REVISION
    elif alteration == "missing_upload_flag":
        context["result"]["publication_attempt"].pop("upload_started")
    elif alteration == "other_unit":
        context["inventory"]["pending_execution_units"] = ["other"]
    elif alteration == "other_pending_attempt":
        context["inventory"]["migration_last_attempt"] = {"operation_id": OPERATION, "name": FIRST, "phase": "sql_sending"}
    elif alteration == "success_response":
        pending["provider"]["confirmed_statements"] = 1
    elif alteration == "phase":
        pending["phase"] = "unknown"
    else:
        pending["statement_count"] = True
    decision = evaluate(context)
    assert decision["eligible"] is False
    assert missing in decision["missing_evidence"]


@pytest.mark.parametrize("mode", ["false_success", "truncated", "short_schema", "short_columns", "inconsistent_count"])
def test_partial_remote_observation_never_proves_absence(evidence_case, mode):
    context, _ = evidence_case
    assessment = assess_migration_recovery(context["gateway"], context["database_id"], context["files"], context["inventory"], operation_id=OPERATION, revision_id=REVISION)
    original = context["gateway"].query

    def query(database_id, sql, params=None):
        response = original(database_id, sql, params)
        if mode == "false_success":
            response[0]["success"] = False
        elif mode == "truncated":
            response[0]["truncated"] = True
        elif mode == "short_schema" and "ORDER BY type, name LIMIT" in sql:
            response[0]["results"] = []
        elif mode == "short_columns" and "SELECT cid" in sql:
            response[0]["results"] = response[0]["results"][:1]
        elif mode == "inconsistent_count" and "AS object_count" in sql:
            response[0]["results"][0]["object_count"] += 1
        return response

    context["gateway"].query = query
    decision = evaluate_developer_no_effect(context, assessment)
    assert decision["eligible"] is False
    assert "complete_remote_schema" in decision["missing_evidence"]


def test_remote_timeout_is_propagated_instead_of_interpreted_as_empty_database(evidence_case):
    context, _ = evidence_case
    assessment = assess_migration_recovery(context["gateway"], context["database_id"], context["files"], context["inventory"], operation_id=OPERATION, revision_id=REVISION)

    def unavailable(*args):
        raise RuleError("DEPENDENCY_UNAVAILABLE", "Consulta indisponível.", 503)

    context["gateway"].query = unavailable
    with pytest.raises(RuleError) as caught:
        evaluate_developer_no_effect(context, assessment)
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE"


def test_schema_drift_during_column_read_invalidates_proof(evidence_case):
    context, _ = evidence_case
    original = context["gateway"].query

    def query(database_id, sql, params=None):
        response = original(database_id, sql, params)
        if "AS column_count" in sql:
            context["gateway"].connection.execute("CREATE TABLE raced(id INTEGER)")
        return response

    context["gateway"].query = query
    decision = evaluate(context)
    assert decision["eligible"] is False and "complete_remote_schema" in decision["missing_evidence"]


@pytest.mark.parametrize("phase", ["prepared", "sql_rejected"])
def test_supported_durable_protocol_has_a_separate_verifiable_legacy_route(evidence_case, phase):
    context, _ = evidence_case
    pending = context["inventory"]["migration_in_progress"]
    pending.pop("execution_unit")
    pending.update(phase=phase, effect_outcome="not_sent")
    pending["provider"] = {} if phase == "prepared" else {
        "effect_outcome": "not_sent", "transport_error": "ConnectTimeout",
        "method": "POST", "route": "/accounts/{account}/d1/database/{database}/query",
    }
    decision = evaluate(context)
    assert decision["eligible"] is True and decision["origin"] == "durable_no_effect"


@pytest.mark.parametrize("sql", [
    SQL,
    "-- atualizar administrador\nALTER TABLE users ADD COLUMN salt TEXT DEFAULT 'UPDATE users;';",
    "ALTER TABLE users ADD role INTEGER NOT NULL DEFAULT 0;",
    'ALTER TABLE "users" ADD COLUMN "salt" TEXT DEFAULT NULL;',
    "ALTER TABLE users ADD salt BLOB DEFAULT X'00FF';",
    "ALTER TABLE users ADD salt REAL DEFAULT -1.25;",
    "ALTER TABLE users ADD salt NUMERIC DEFAULT 1e-3;",
])
def test_closed_grammar_accepts_only_scalar_column_additions(sql):
    assert len(split_sql_statements(sql)) == 1
    target = constant_column_addition(_tokens(sql))
    assert target is not None and target.table == "users"


@pytest.mark.parametrize("sql", [
    SQL + " UPDATE users SET name = 'changed';", SQL + " ALTER TABLE users ADD another TEXT;",
    "UPDATE users SET name = 'changed';", "INSERT INTO users VALUES(1, 'name');", "DELETE FROM users;",
    "ALTER TABLE users ADD salt TEXT NOT NULL;", "ALTER TABLE users ADD salt TEXT NOT NULL DEFAULT NULL;",
    "ALTER TABLE users ADD salt TEXT CHECK(length(salt)>1);", "ALTER TABLE users ADD salt TEXT REFERENCES users(name);",
    "ALTER TABLE users ADD salt TEXT COLLATE NOCASE;", "ALTER TABLE users ADD salt VARCHAR(20);",
    "ALTER TABLE users ADD salt TEXT DEFAULT random();", "ALTER TABLE users ADD salt TEXT DEFAULT ('a');",
    "ALTER TABLE users ADD salt TEXT DEFAULT CURRENT_TIMESTAMP;", "ALTER TABLE users ADD salt INTEGER PRIMARY KEY;",
    "ALTER TABLE users ADD salt TEXT GENERATED ALWAYS AS(name) VIRTUAL;",
    "ALTER TABLE main.users ADD salt TEXT;", "ALTER TABLE users ADD salt TEXT DEFAULT 'a' || 'b';",
    "ALTER TABLE __viroucaseai_migrations ADD salt TEXT;", "ALTER TABLE users ADD salt TEXT DEFAULT ?;",
])
def test_grammar_rejects_dml_batches_functions_constraints_and_opaque_units(sql):
    assert constant_column_addition(_tokens(sql)) is None


def test_confirmed_receipt_prevents_closing_as_no_effect(evidence_case):
    context, _ = evidence_case
    context["gateway"].connection.execute(f"INSERT INTO {LEDGER}(name, sha256) VALUES (?, ?)", [LAST, digest(SQL)])
    decision = evaluate(context)
    assert decision["eligible"] is False and decision["reason_code"] == "SCHEMA_APPLIED"


@pytest.mark.parametrize("sql,reason", [
    ("UPDATE users SET name = 'changed' WHERE id = 1;", "DML_UNCERTAIN"),
    ("INSERT INTO users VALUES (2, 'changed');", "DML_UNCERTAIN"),
    ("DELETE FROM users WHERE id = 1;", "DML_UNCERTAIN"),
    (SQL + " UPDATE users SET name = 'changed';", "DML_UNCERTAIN"),
    (SQL + " ALTER TABLE users ADD label TEXT;", "UNSUPPORTED_EFFECTS"),
])
def test_unchanged_schema_cannot_close_dml_or_multiple_statements(evidence_case, sql, reason):
    context, _ = evidence_case
    context["files"][LAST] = sql
    source_hash = canonical_hash(context["files"])
    context["files_sha256"] = source_hash
    context["binding"]["source_hash"] = source_hash
    pending = context["inventory"]["migration_in_progress"]
    statements = split_sql_statements(sql)
    pending["sha256"], pending["statement_count"] = digest(sql), len(statements)
    pending["execution_unit"].update(
        sql_sha256=digest(sql), statement_count=len(statements),
        statement_sha256=[digest(statement) for statement in statements],
        atomicity="sqlite_single_statement" if len(statements) == 1 else "unproven_batch",
    )
    decision = evaluate(context)
    assert decision["eligible"] is False and decision["reason_code"] == reason
    assert all(query.startswith("SELECT") for query, _ in context["gateway"].calls)
    assert context["gateway"].connection.execute("SELECT name FROM users").fetchone()[0] == "preserved"


