import json
from copy import deepcopy
from hashlib import sha256

import pytest
from test_publication_migration_integrity import Database

from viracase.domain.rules import RuleError, canonical_hash
from viracase.infrastructure.publication_migrations import LEDGER, confirm_migration_receipt
from viracase.infrastructure.publication_recovery_evidence import assess_migration_recovery
from viracase.infrastructure.publication_sql_review import split_sql_statements

OPERATION = "60c65530-48c1-48bb-a35b-b5809fb18373"
REVISION = "d8a31ef9-5e79-4f3e-860c-d91d23eba907"
DATABASE = "test-database"
FIRST = "migrations/0001.sql"
SECOND = "migrations/0002.sql"
BASE = "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL); CREATE INDEX users_name ON users(name);"
ADDITION = "ALTER TABLE users ADD COLUMN nickname TEXT; CREATE INDEX users_nickname ON users(nickname);"


def digest(sql):
    return sha256(sql.encode()).hexdigest()


@pytest.fixture
def remote():
    result = Database()
    result.seed({FIRST: BASE})
    yield result
    result.connection.close()


def inventory(sql=ADDITION, durable=False):
    pending = {"name": SECOND, "sha256": digest(sql)}
    if durable:
        pending.update(
            protocol_version=2, phase="sql_confirmed", effect_outcome="confirmed",
            statement_count=len(split_sql_statements(sql)),
            sql_result={"confirmed_statements": len(split_sql_statements(sql)), "changes": 0},
            operation_id=OPERATION, revision_id=REVISION,
        )
    return {"database_id": DATABASE, "migrations": {FIRST: digest(BASE)}, "migration_in_progress": pending}


def assess(remote, state=None, files=None, **kwargs):
    return assess_migration_recovery(
        remote, DATABASE, files if files is not None else {FIRST: BASE, SECOND: ADDITION},
        state if state is not None else inventory(), operation_id=OPERATION, revision_id=REVISION, **kwargs,
    )


def test_durable_complete_execution_recovers_missing_schema_observation_without_replaying_sql(remote):
    remote.connection.executescript(ADDITION)
    state = inventory(durable=True)
    original = deepcopy(state)
    result = assess(remote, state)
    assert result["classification"] == "confirmed"
    assert result["origin"] == "durable_execution_reconstructed_schema"
    assert result["requires_operator"] is False
    assert result["pending_update"]["schema_after"] == {
        "sha256": result["schema"]["expected_sha256"], "object_count": result["schema"]["observed_object_count"],
    }
    assert result["pending_update"]["recovery_evidence"]["evidence_sha256"] == result["evidence_sha256"]
    assert state == original
    assert all(sql.startswith("SELECT") for sql, _ in remote.calls)
    assert remote.connection.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0] == 1
    receipt = confirm_migration_receipt(remote, DATABASE, result["pending_update"])
    assert receipt["receipt_confirmed"] is True
    assert sum(sql == ADDITION for sql, _ in remote.calls) == 0


@pytest.mark.parametrize("durable", [True, False])
@pytest.mark.parametrize("change", [
    "DROP INDEX users_nickname;",
    "CREATE TABLE unrelated(id INTEGER);",
    "CREATE TRIGGER users_extra AFTER INSERT ON users BEGIN SELECT 1; END;",
])
def test_column_existence_never_overrides_full_schema_mismatch(remote, durable, change):
    remote.connection.executescript(ADDITION + change)
    result = assess(remote, inventory(durable=durable))
    assert result["classification"] == "unknown"
    assert result["checks"]["schema"] == "mismatch"
    assert "matching_remote_schema" in result["missing_evidence"]
    assert "pending_update" not in result


@pytest.mark.parametrize("applied,classification,action", [
    (True, "schema_applied", "adopt_schema"),
    (False, "schema_unchanged", "confirm_schema_unchanged"),
])
def test_legacy_structural_state_requires_explicit_operator_action(remote, applied, classification, action):
    if applied:
        remote.connection.executescript(ADDITION)
    result = assess(remote)
    assert result["classification"] == classification and result["action"] == action
    assert result["requires_operator"] is True and result["origin"] == "schema_postconditions"
    assert result["historical_provider_result_verified"] is False
    assert "operator_structural_attestation" in result["missing_evidence"]
    assert "pending_update" not in result
    assert all(sql.startswith("SELECT") for sql, _ in remote.calls)
    assert remote.connection.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0] == 1


@pytest.mark.parametrize("statement", [
    "INSERT INTO users(id, name) VALUES (1, 'private-value');",
    "UPDATE users SET name = 'private-value' WHERE id = 1;",
    "ALTER TABLE users ADD COLUMN salt TEXT;",
    "CREATE TABLE credential_store(id INTEGER);",
    "CREATE TABLE copy AS SELECT * FROM users;",
    "CREATE TRIGGER users_insert AFTER INSERT ON users BEGIN SELECT 1; END;",
    "ALTER TABLE users RENAME COLUMN name TO display_name;",
    "DROP INDEX users_name;",
    "CREATE VIRTUAL TABLE search USING fts5(name);",
    "CREATE TEMP TABLE transient(id INTEGER);",
    "PRAGMA user_version = 1;",
    "CREATE VIEW unsafe_file AS SELECT readfile('/private-path');",
])
def test_unconfirmed_data_security_and_unsupported_statements_cannot_be_attested_as_structural(remote, statement):
    if not statement.startswith("PRAGMA"):
        remote.connection.executescript(statement)
    result = assess(
        remote, inventory(statement), {FIRST: BASE, SECOND: statement},
        operator_evidence={
            "outcome": "applied", "cf_ray": "0123456789abcdef-DFW", "success": True,
            "request_sha256": digest(statement), "reference": "operator-reviewed", "evidence_sha256": "f" * 64,
            "statement_indices": [1],
        },
    )
    assert result["classification"] == "unknown" and result["requires_operator"] is False
    assert result["operator_evidence_verified"] is False
    assert "pending_update" not in result


def test_durable_dml_execution_uses_execution_evidence_in_addition_to_full_schema(remote):
    sql = "INSERT INTO users(id, name) VALUES (1, 'preserved');"
    remote.connection.executescript(sql)
    result = assess(remote, inventory(sql, durable=True), {FIRST: BASE, SECOND: sql})
    assert result["classification"] == "confirmed"
    assert result["checks"]["execution"] == "durable_confirmed"
    assert result["pending_update"]["sql_result"]["confirmed_statements"] == 1
    assert remote.connection.execute("SELECT name FROM users WHERE id = 1").fetchone()[0] == "preserved"


@pytest.mark.parametrize("changed", [
    {"sql_result": {"confirmed_statements": 1}},
    {"statement_count": 1},
    {"sql_result": {"confirmed_statements": True}},
    {"phase": "sql_sending", "effect_outcome": "unknown"},
    {"operation_id": None},
])
def test_incomplete_durable_evidence_never_produces_automatic_pending_update(remote, changed):
    remote.connection.executescript(ADDITION)
    state = inventory(durable=True)
    state["migration_in_progress"].update(changed)
    result = assess(remote, state)
    assert result["classification"] == "schema_applied"
    assert result["requires_operator"] is True
    assert "pending_update" not in result


@pytest.mark.parametrize("field", ["operation_id", "revision_id"])
def test_durable_evidence_of_another_operation_or_revision_is_rejected_before_remote_reads(remote, field):
    state = inventory(durable=True)
    state["migration_in_progress"][field] = "another-attempt"
    result = assess(remote, state)
    assert result["classification"] == "conflict" and "operation_identity" in result["missing_evidence"]
    assert remote.calls == []


@pytest.mark.parametrize("change,missing", [
    ("missing", "matching_remote_history"),
    ("different", "matching_remote_history"),
    ("future", "matching_remote_history"),
])
def test_missing_conflicting_or_future_history_blocks_recovery(remote, change, missing):
    if change == "missing":
        remote.connection.execute(f"DELETE FROM {LEDGER}")
    elif change == "different":
        remote.connection.execute(f"UPDATE {LEDGER} SET sha256 = ?", ["f" * 64])
    else:
        remote.connection.execute(f"INSERT INTO {LEDGER} VALUES (?, ?)", ["migrations/0003.sql", "f" * 64])
    remote.connection.executescript(ADDITION)
    result = assess(remote, inventory(durable=True))
    assert result["classification"] == "conflict" and missing in result["missing_evidence"]
    assert "pending_update" not in result


def test_remote_confirmed_prefix_can_repair_a_missing_local_acknowledgement(remote):
    state = inventory(durable=True)
    state["migrations"] = {}
    remote.connection.executescript(ADDITION)
    result = assess(remote, state)
    assert result["classification"] == "confirmed"


def test_all_preceding_source_migrations_need_receipts_even_when_absent_from_local_inventory(remote):
    files = {FIRST: BASE, "migrations/0001_extra.sql": "CREATE TABLE absent(id INTEGER);", SECOND: ADDITION}
    result = assess(remote, files=files)
    assert result["classification"] == "conflict" and "complete_history_prefix" in result["missing_evidence"]


def test_existing_matching_receipt_recovers_legacy_without_schema_reconstruction(remote):
    remote.connection.executescript(ADDITION)
    remote.connection.execute(f"INSERT INTO {LEDGER} VALUES (?, ?)", [SECOND, digest(ADDITION)])
    result = assess(remote)
    assert result["classification"] == "confirmed" and result["origin"] == "remote_receipt"
    assert result["statement_count"] == 2 and len(remote.calls) == 1
    assert "pending_update" not in result


def test_previously_confirmed_receipt_cannot_be_recreated_through_structural_adoption(remote):
    remote.connection.executescript(ADDITION)
    state = inventory(durable=True)
    state["migration_in_progress"]["phase"] = "receipt_confirmed"
    result = assess(remote, state)
    assert result["classification"] == "unknown"
    assert "previously_confirmed_receipt" in result["missing_evidence"]


def test_saved_post_execution_schema_is_never_replaced_when_current_schema_differs(remote):
    remote.connection.executescript(ADDITION)
    state = inventory(durable=True)
    state["migration_in_progress"]["schema_after"] = {"sha256": "a" * 64, "object_count": 3}
    result = assess(remote, state)
    assert result["classification"] == "unknown" and "matching_saved_schema" in result["missing_evidence"]


@pytest.mark.parametrize("phase,effect", [("prepared", "not_sent"), ("sql_rejected", "none"), ("sql_rejected", "not_sent")])
def test_server_recorded_no_effect_has_a_separate_action_without_receipt_creation(remote, phase, effect):
    state = inventory(durable=True)
    state["migration_in_progress"].update(phase=phase, effect_outcome=effect)
    state["migration_in_progress"].pop("sql_result")
    result = assess(remote, state)
    assert result["classification"] == "no_effect" and result["action"] == "record_no_effect"
    assert result["origin"] == "durable_no_effect" and len(remote.calls) == 1


def test_operator_supplied_reference_hash_and_ray_do_not_change_the_technical_assessment(remote):
    remote.connection.executescript(ADDITION)
    before = assess(remote)
    after = assess(remote, operator_evidence={"success": True, "cf_ray": "a" * 16, "evidence_sha256": "b" * 64})
    assert before == after
    assert before["evidence_sha256"] == canonical_hash({key: value for key, value in before.items() if key != "evidence_sha256"})


def test_report_contains_hashes_and_safe_identity_without_sql_provider_body_or_arbitrary_pending_fields(remote):
    sql = "INSERT INTO users(id, name) VALUES (1, 'sensitive-customer-name');"
    remote.connection.executescript(sql)
    state = inventory(sql, durable=True)
    state["migration_in_progress"].update(raw_body="private-body", provider={"body": "secret-token"})
    result = assess(remote, state, {FIRST: BASE, SECOND: sql})
    encoded = json.dumps(result)
    assert "sensitive-customer-name" not in encoded and "private-body" not in encoded and "secret-token" not in encoded
    assert "CREATE TABLE" not in encoded and "INSERT INTO" not in encoded


@pytest.mark.parametrize("pending", [None, [], {}, {"name": SECOND, "sha256": "wrong"}])
def test_missing_or_malformed_pending_identity_fails_without_remote_access(remote, pending):
    state = inventory()
    state["migration_in_progress"] = pending
    result = assess(remote, state)
    assert result["classification"] == "unknown" and remote.calls == []


def test_gateway_authorization_and_network_errors_remain_visible_to_the_orchestrator(remote):
    def unavailable(*args, **kwargs):
        raise RuleError("CONFIGURATION_INVALID", "A conexão precisa de validação.")

    remote.query = unavailable
    with pytest.raises(RuleError) as caught:
        assess(remote)
    assert caught.value.code == "CONFIGURATION_INVALID"


def test_ledger_query_rejection_is_not_mistaken_for_no_effect_of_the_project_migration(remote):
    def absent(*args, **kwargs):
        failure = RuleError("MIGRATION_SQL_REJECTED", "Tabela ausente.")
        failure.provider_diagnostic = {"reason": "missing_table", "effect_outcome": "none"}
        raise failure

    remote.query = absent
    result = assess(remote, inventory(durable=True))
    assert result["classification"] == "unknown" and "remote_ledger" in result["missing_evidence"]


@pytest.mark.parametrize("statement", [
    "CREATE TABLE linked(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE);",
    "CREATE UNIQUE INDEX users_unique ON users(name);",
    "CREATE VIEW user_names AS SELECT name FROM users;",
    'CREATE VIEW "user names"(display_name) AS SELECT name FROM users;',
    'ALTER TABLE "users" ADD COLUMN "display name" TEXT DEFAULT \'UPDATE DROP DELETE\';',
    "-- INSERT fake\nCREATE TABLE additions(id INTEGER CHECK(id > 0));",
])
def test_supported_additive_statements_have_specific_postconditions_without_project_sql_execution(remote, statement):
    remote.connection.executescript(statement)
    result = assess(remote, inventory(statement), {FIRST: BASE, SECOND: statement})
    assert result["classification"] == "schema_applied"
    assert result["statement_count"] == 1 and result["statement_manifest"][0]["sha256"] == digest(statement)
    assert all(sql.startswith("SELECT") for sql, _ in remote.calls)


def test_historical_triggers_are_part_of_the_complete_schema_comparison(remote):
    trigger = "CREATE TRIGGER users_observer AFTER INSERT ON users BEGIN SELECT 1; END;"
    base = BASE + trigger
    remote.connection.executescript(trigger + ADDITION)
    remote.connection.execute(f"UPDATE {LEDGER} SET sha256 = ? WHERE name = ?", [digest(base), FIRST])
    state = inventory()
    state["migrations"] = {FIRST: digest(base)}
    accepted = assess(remote, state, {FIRST: base, SECOND: ADDITION})
    assert accepted["classification"] == "schema_applied"
    remote.connection.execute("DROP TRIGGER users_observer")
    rejected = assess(remote, state, {FIRST: base, SECOND: ADDITION})
    assert rejected["classification"] == "unknown"
    assert rejected["evidence_sha256"] != accepted["evidence_sha256"]


def test_report_changes_when_source_or_remote_state_changes_and_is_stable_otherwise(remote):
    initial = assess(remote)
    same = assess(remote)
    assert same["evidence_sha256"] == initial["evidence_sha256"]
    source_changed = assess(remote, files={FIRST: BASE, SECOND: ADDITION, "public/app.js": "new code"})
    assert source_changed["evidence_sha256"] != initial["evidence_sha256"]
    remote.connection.executescript(ADDITION)
    state_changed = assess(remote)
    assert state_changed["evidence_sha256"] != initial["evidence_sha256"]


@pytest.mark.parametrize("field,value", [("phase", []), ("effect_outcome", {}), ("protocol_version", 2.0)])
def test_malformed_protocol_values_never_authorize_durable_confirmation(remote, field, value):
    remote.connection.executescript(ADDITION)
    state = inventory(durable=True)
    state["migration_in_progress"][field] = value
    result = assess(remote, state)
    assert result["classification"] == "schema_applied" and "pending_update" not in result


@pytest.mark.parametrize("rows", [
    [],
    [{"success": False, "results": []}],
    [{"success": True, "results": [{"name": FIRST, "sha256": digest(BASE)}] * 2}],
    [{"success": True, "results": [{"name": "migrations/../private.sql", "sha256": "f" * 64}]}],
])
def test_malformed_or_duplicate_remote_receipts_fail_closed(remote, rows):
    remote.query = lambda *args, **kwargs: rows
    result = assess(remote)
    assert result["classification"] == "conflict" and "valid_remote_history" in result["missing_evidence"]
