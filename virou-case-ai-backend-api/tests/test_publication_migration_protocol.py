import json
from copy import deepcopy

import httpx
import pytest
from test_publication_migration_integrity import ADDITION, FIRST, INITIAL, SECOND, Database, digest, record

from viracase.domain.rules import RuleError
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway, PublicationUncertain
from viracase.infrastructure.publication_d1_diagnostics import sanitize_provider_diagnostic
from viracase.infrastructure.publication_migration_validation import validate_migration_plan
from viracase.infrastructure.publication_migrations import (
    LEDGER,
    apply_migrations,
    confirm_migration_receipt,
)
from viracase.infrastructure.publication_sql_review import split_sql_statements

POLICY = {"account_id": "a" * 32, "timeout_seconds": 5}
RAY = "0123456789abcdef-GRU"


def gateway_response(payload, status=400):
    return CloudflareGateway(POLICY, "private-access-token", httpx.MockTransport(
        lambda request: httpx.Response(status, json=payload, headers={"cf-ray": RAY})
    ))


def rejection(message, result=None):
    return {"success": False, "errors": [{"code": 7500, "message": message}], "result": result}


def test_single_compile_rejection_retains_specific_safe_d1_diagnostic():
    gateway = gateway_response(rejection("D1_ERROR: duplicate column name: salt: SQLITE_ERROR"))
    with pytest.raises(RuleError) as caught:
        gateway.query("private-database", "ALTER TABLE users ADD COLUMN salt TEXT;")
    failure = caught.value
    assert failure.code == "MIGRATION_SQL_REJECTED"
    diagnostic = failure.provider_diagnostic
    assert diagnostic["status"] == 400 and diagnostic["cf_ray"] == RAY
    assert diagnostic["provider_error_codes"] == [7500]
    assert diagnostic["reason"] == "duplicate_column" and diagnostic["effect_outcome"] == "none"
    assert diagnostic["statement_index"] == 1 and diagnostic["statement_count"] == 1
    assert diagnostic["route"] == "/accounts/{account}/d1/database/{database}/query"
    assert "ALTER TABLE" not in json.dumps(diagnostic) and "private-" not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    "sql,payload",
    [
        ("CREATE TABLE users(id INTEGER); ALTER TABLE users ADD COLUMN id INTEGER;", rejection("duplicate column name: id")),
        ("INSERT INTO users VALUES (1);", rejection("UNIQUE constraint failed: users.id in INSERT INTO users VALUES ('syntax error')")),
        ("INSERT INTO users VALUES (1);", rejection("no such table: forged_by_raise_fail_trigger")),
        ("UPDATE users SET id = 2;", rejection("duplicate column name: forged_by_raise_fail_trigger")),
        ("CREATE TABLE users(id INTEGER);", rejection("table users already exists", [{"success": True, "results": []}])),
        ("CREATE TABLE users(id INTEGER);", rejection("SQL rejected: private email user@example.test token=private-access-token")),
        ("CREATE TABLE users(id INTEGER);", rejection("table users already exists", [{"success": False, "meta": {"changed_db": True}}])),
    ],
)
def test_multi_statement_partial_constraint_and_unrecognized_rejections_never_prove_no_effect(sql, payload):
    with pytest.raises(RuleError) as caught:
        gateway_response(payload).query("db", sql)
    assert caught.value.code == "PUBLICATION_REJECTED"
    assert caught.value.provider_diagnostic["effect_outcome"] == "unknown"
    serialized = json.dumps(caught.value.provider_diagnostic)
    assert "private-access-token" not in serialized and "user@example.test" not in serialized


def test_failed_result_item_reports_known_compile_rejection_without_generic_permissions_advice():
    gateway = gateway_response({"success": True, "result": [{"success": False, "error": "no such table: items"}]}, 200)
    with pytest.raises(RuleError) as caught:
        gateway.query("db", "ALTER TABLE items ADD COLUMN name TEXT;")
    assert caught.value.code == "MIGRATION_SQL_REJECTED"
    assert caught.value.provider_diagnostic["reason"] == "missing_table"
    assert "permiss" not in caught.value.message.casefold()


@pytest.mark.parametrize("exception,effect", [(httpx.ReadTimeout, "unknown"), (httpx.ConnectTimeout, "not_sent"), (httpx.WriteError, "unknown")])
def test_d1_transport_phase_distinguishes_unsent_connect_failure_from_lost_response(exception, effect):
    def fail(request):
        raise exception("private body", request=request)

    gateway = CloudflareGateway(POLICY, "private-token", httpx.MockTransport(fail))
    with pytest.raises(PublicationUncertain) as caught:
        gateway.query("db", "CREATE TABLE items(id INTEGER);")
    assert caught.value.provider_diagnostic["effect_outcome"] == effect
    assert "private body" not in json.dumps(caught.value.provider_diagnostic)


@pytest.mark.parametrize("result", [None, [], {}, [None]])
def test_malformed_d1_results_never_confirm_execution(result):
    gateway = gateway_response({"success": True, "result": result}, 200)
    with pytest.raises(RuleError) as caught:
        gateway.query("db", "SELECT 1;")
    assert caught.value.code == "MIGRATION_FAILED"
    assert caught.value.provider_diagnostic["effect_outcome"] == "unknown"


def test_incomplete_success_array_is_an_uncertain_batch_result():
    gateway = gateway_response({"success": True, "result": [{"success": True, "results": []}]}, 200)
    with pytest.raises(PublicationUncertain) as caught:
        gateway.query("db", "CREATE TABLE one(id INTEGER); CREATE TABLE two(id INTEGER);")
    assert caught.value.provider_diagnostic["reason"] == "incomplete_results"
    assert caught.value.provider_diagnostic["confirmed_statements"] == 1


def test_provider_sanitizer_drops_arbitrary_messages_and_accepts_only_safe_structured_fields():
    value = {
        "method": "POST", "route": "/accounts/{account}/d1/database/{database}/query", "status": 400,
        "provider_error_codes": [7500, True, "private-token"], "cf_ray": RAY,
        "reason": "duplicate_column", "message": "private SQL and user@example.test",
        "sql": "INSERT INTO users VALUES ('private')", "body": {"password": "private"},
        "effect_outcome": "none", "statement_index": 1,
    }
    result = sanitize_provider_diagnostic(value)
    assert result["provider_error_codes"] == [7500] and result["statement_index"] == 1
    assert result["message"] != value["message"] and "private" not in json.dumps(result)
    assert sanitize_provider_diagnostic({"method": [], "transport_error": {}, "effect_outcome": []}) == {}


def test_sql_splitter_keeps_trigger_body_and_literal_semicolons_in_one_statement():
    sql = "CREATE TRIGGER audit AFTER INSERT ON items BEGIN INSERT INTO log VALUES ('a;b'); INSERT INTO log VALUES ('c'); END; SELECT ';';"
    assert len(split_sql_statements(sql)) == 2
    assert split_sql_statements("/* comment */ -- comment\n") == []
    assert split_sql_statements("; /* comment */ ; -- comment\n;") == []


@pytest.fixture
def remote():
    database = Database()
    yield database
    database.connection.close()


def test_bounded_new_column_backfill_is_validated_and_only_executed_once(remote):
    remote.seed({FIRST: INITIAL})
    sql = "ALTER TABLE items ADD COLUMN label TEXT; UPDATE items SET label = name WHERE label IS NULL;"
    files, inventory = {FIRST: INITIAL, SECOND: sql}, {"migrations": {FIRST: digest(INITIAL)}}
    evidence = validate_migration_plan(files, baseline_files={FIRST: INITIAL}, revision_id="revision-one")
    assert evidence["approved_backfills"][0]["scope"] == "new_column_null_rows"
    assert evidence["provider_equivalence"] is False and evidence["remote_execution"] is False
    assert evidence["preserved"] == [FIRST] and evidence["added"] == [SECOND]
    apply_migrations(remote, "test-database", files, inventory, record(inventory, []))
    assert remote.connection.execute("SELECT name, label FROM items").fetchone()[:] == ("preserved", "preserved")
    apply_migrations(remote, "test-database", files, inventory, record(inventory, []))
    assert sum(query == sql for query, _ in remote.calls) == 1


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE items SET name = 'changed' WHERE name IS NULL;",
        "ALTER TABLE items ADD COLUMN label TEXT; UPDATE items SET label = 'changed';",
        "ALTER TABLE items ADD COLUMN role TEXT; UPDATE items SET role = 'admin' WHERE role IS NULL;",
        "ALTER TABLE items ADD COLUMN label TEXT; UPDATE items SET label = (SELECT name FROM items) WHERE label IS NULL;",
        "ALTER TABLE items ADD COLUMN label TEXT; UPDATE items SET label = random() WHERE label IS NULL;",
        "ALTER TABLE items ADD COLUMN label TEXT; CREATE TRIGGER echo AFTER UPDATE ON items BEGIN INSERT INTO items(name) VALUES ('copy'); END; UPDATE items SET label = name WHERE label IS NULL;",
    ],
)
def test_backfill_permission_is_specific_to_safe_new_column_scope(sql):
    with pytest.raises(RuleError) as caught:
        validate_migration_plan({FIRST: INITIAL, SECOND: sql}, baseline_files={FIRST: INITIAL})
    assert caught.value.code == "MIGRATION_REVIEW_REQUIRED"


def test_isolated_validation_catches_duplicate_column_before_migration_sql(remote):
    files = {FIRST: INITIAL, SECOND: "ALTER TABLE items ADD COLUMN name TEXT;"}
    inventory, checkpoints = {}, []
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", files, inventory, record(inventory, checkpoints))
    assert caught.value.code == "MIGRATION_VALIDATION_FAILED"
    assert caught.value.details["migration"]["issues"][0]["reason"] == "duplicate_column"
    assert inventory.get("migration_in_progress") is None
    assert all(query != INITIAL for query, _ in remote.calls)


def test_validation_evidence_reuse_requires_unchanged_migrations_policy_and_hash(monkeypatch):
    from viracase.infrastructure import publication_migration_validation as validation

    files = {FIRST: INITIAL, SECOND: ADDITION, "src/index.ts": "export {};"}
    previous = validate_migration_plan(files, baseline_files={FIRST: INITIAL}, revision_id="r1")
    calls = []
    original = validation._validate_cached

    def count(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(validation, "_validate_cached", count)
    reused = validate_migration_plan(files, previous_evidence=previous, revision_id="r2")
    assert reused["revision_id"] == "r2" and calls == []
    edited = validate_migration_plan({**files, "src/index.ts": "export const ready = true;"}, previous_evidence=previous)
    assert len(calls) == 0 and edited["artifact_sha256"] != previous["artifact_sha256"]
    validate_migration_plan({**files, SECOND: ADDITION + "\n"}, previous_evidence=previous)
    assert len(calls) == 1
    invalid = {**previous, "policy_version": "outdated"}
    validate_migration_plan(files, previous_evidence=invalid)
    assert len(calls) == 2


def test_validation_is_bounded_and_never_opens_external_database(tmp_path):
    destination = tmp_path / "must-not-exist.db"
    with pytest.raises(RuleError) as caught:
        validate_migration_plan({FIRST: f"ATTACH DATABASE '{destination}' AS external;"})
    assert caught.value.code == "MIGRATION_REVIEW_REQUIRED" and not destination.exists()
    with pytest.raises(RuleError) as caught:
        validate_migration_plan({FIRST: "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT sum(x) FROM n;"})
    assert caught.value.code == "MIGRATION_VALIDATION_FAILED"


def test_audited_no_effect_rejection_clears_marker_but_preserves_specific_attempt(remote):
    sql = "CREATE TABLE items(id INTEGER);"
    original = remote.query

    def reject(database_id, query, params=None):
        if query == sql:
            failure = RuleError("PUBLICATION_REJECTED", "Cloudflare rejected")
            failure.provider_diagnostic = {
                "method": "POST", "status": 400, "provider_error_codes": [7500],
                "reason": "object_exists", "effect_outcome": "none", "statement_count": 1,
            }
            raise failure
        return original(database_id, query, params)

    remote.query = reject
    inventory, checkpoints = {}, []
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", {FIRST: sql}, inventory, record(inventory, checkpoints))
    assert caught.value.code == "MIGRATION_SQL_REJECTED"
    assert inventory["migration_in_progress"] is None
    last = inventory["migration_last_attempt"]
    assert last["phase"] == "sql_rejected" and last["effect_outcome"] == "none"
    assert last["name"] == FIRST and last["sha256"] == digest(sql)
    assert last["provider"]["reason"] == "object_exists"
    assert checkpoints[-1][2] is False


def test_timeout_after_sql_dispatch_keeps_unknown_marker_and_never_replays(remote):
    original = remote.query

    def uncertain(database_id, sql, params=None):
        result = original(database_id, sql, params)
        if sql == INITIAL:
            raise PublicationUncertain({"effect_outcome": "unknown", "transport_error": "ReadTimeout"})
        return result

    remote.query = uncertain
    inventory = {}
    with pytest.raises(PublicationUncertain):
        apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, record(inventory, []))
    assert inventory["migration_in_progress"]["phase"] == "sql_sending"
    assert inventory["migration_in_progress"]["effect_outcome"] == "unknown"
    remote.query = original
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, record(inventory, []))
    assert caught.value.code == "MIGRATION_RESULT_UNKNOWN"
    assert sum(sql == INITIAL for sql, _ in remote.calls) == 1


@pytest.mark.parametrize("phase", ["prepared", "sql_sending", "sql_confirmed", "receipt_sending", "receipt_confirmed"])
def test_crashes_at_each_checkpoint_preserve_distinct_safe_recovery(remote, phase):
    inventory, checkpoints = {}, []
    checkpoint = record(inventory, checkpoints)

    def crash(stage, changes=None, external=None):
        checkpoint(stage, changes, external)
        if (changes or {}).get("migration_in_progress", {}).get("phase") == phase:
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError, match="crash"):
        apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, crash)
    assert inventory["migration_in_progress"]["phase"] == phase
    executions = sum(sql == INITIAL for sql, _ in remote.calls)
    if phase in {"prepared", "receipt_sending", "receipt_confirmed"}:
        apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, checkpoint)
        assert inventory["migration_in_progress"] is None
        assert sum(sql == INITIAL for sql, _ in remote.calls) == 1
    else:
        with pytest.raises(RuleError) as caught:
            apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, checkpoint)
        assert caught.value.code in {"MIGRATION_RESULT_UNKNOWN", "MIGRATION_RECEIPT_PENDING"}
        assert sum(sql == INITIAL for sql, _ in remote.calls) == executions


def test_confirmed_sql_receipt_recovery_rejects_schema_drift(remote):
    inventory = {}
    remote.fail_receipt = True
    with pytest.raises(RuleError):
        apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, record(inventory, []))
    pending = deepcopy(inventory["migration_in_progress"])
    remote.fail_receipt = False
    remote.connection.execute("ALTER TABLE items ADD COLUMN unexpected TEXT")
    remote.connection.commit()
    with pytest.raises(RuleError) as caught:
        confirm_migration_receipt(remote, "test-database", pending)
    assert caught.value.code == "MIGRATION_RECEIPT_PENDING"
    assert caught.value.details["migration"]["issues"][0]["reason"] == "schema_changed_after_execution"
    assert remote.connection.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0] == 0


def test_existing_valid_receipt_recovers_legacy_marker_without_reexecuting_any_sql(remote):
    remote.seed({FIRST: INITIAL})
    result = confirm_migration_receipt(remote, "test-database", {"name": FIRST, "sha256": digest(INITIAL)})
    assert result["resolution"] == "existing_receipt" and result["receipt_confirmed"] is True
    assert len(remote.calls) == 1 and remote.calls[0][0].startswith("SELECT")


def test_schema_alone_never_creates_a_receipt_for_legacy_unknown_execution(remote):
    remote.seed({})
    remote.connection.executescript(INITIAL)
    with pytest.raises(RuleError) as caught:
        confirm_migration_receipt(remote, "test-database", {"name": FIRST, "sha256": digest(INITIAL)})
    assert caught.value.code == "MIGRATION_RECEIPT_PENDING"
    assert remote.connection.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0] == 0


def test_built_migration_must_match_the_immutable_source_before_any_remote_call(remote):
    with pytest.raises(RuleError) as caught:
        apply_migrations(
            remote, "test-database", {FIRST: INITIAL + "\n"}, {}, record({}, []),
            source_files={FIRST: INITIAL, "src/index.ts": "export {};"}, revision_id="r1",
        )
    assert caught.value.code == "ARTIFACT_IDENTITY_MISMATCH" and remote.calls == []


def test_new_migration_cannot_be_inserted_before_applied_remote_history(remote):
    remote.seed({SECOND: INITIAL})
    inventory = {"migrations": {SECOND: digest(INITIAL)}}
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", {FIRST: ADDITION, SECOND: INITIAL}, inventory, record(inventory, []))
    assert caught.value.code == "MIGRATION_ORDER_INVALID"
    assert not any(sql == ADDITION for sql, _ in remote.calls)


def test_execution_reuses_revision_evidence_and_records_exact_revision(remote):
    files = {FIRST: INITIAL, "src/index.ts": "export {};"}
    evidence = validate_migration_plan(files, revision_id="current-revision")
    inventory = {"candidate_revision_id": "previous-revision"}
    apply_migrations(
        remote, "test-database", {FIRST: INITIAL}, inventory, record(inventory, []),
        revision_id="current-revision", source_files=files, validation_evidence=evidence,
    )
    assert inventory["migration_validation"] == evidence
    assert inventory["migration_last_attempt"]["revision_id"] == "current-revision"
    assert inventory["migration_plan"]["revision_id"] == "current-revision"


def test_deleted_previously_confirmed_receipt_is_never_recreated_from_schema(remote):
    inventory = {}
    apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, record(inventory, []))
    pending = deepcopy(inventory["migration_last_attempt"])
    remote.connection.execute(f"DELETE FROM {LEDGER}")
    remote.connection.commit()
    for recover in (
        lambda: confirm_migration_receipt(remote, "test-database", pending),
        lambda: apply_migrations(remote, "test-database", {FIRST: INITIAL}, {"migration_in_progress": pending}, record({}, [])),
    ):
        with pytest.raises(RuleError) as caught:
            recover()
        assert caught.value.code == "MIGRATION_RESULT_UNKNOWN"
    assert remote.connection.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0] == 0


@pytest.mark.parametrize("phase", [[], {}, None])
def test_malformed_pending_phase_fails_closed_without_type_error_or_receipt_write(remote, phase):
    remote.seed({})
    pending = {"name": FIRST, "sha256": digest(INITIAL), "protocol_version": 2, "phase": phase}
    with pytest.raises(RuleError) as caught:
        confirm_migration_receipt(remote, "test-database", pending)
    assert caught.value.code == "MIGRATION_RECEIPT_PENDING"
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", {FIRST: INITIAL}, {"migration_in_progress": pending}, record({}, []))
    assert caught.value.code == "MIGRATION_RESULT_UNKNOWN"
    assert remote.connection.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0] == 0


def test_partial_durable_sql_result_never_authorizes_receipt_creation(remote):
    inventory = {}
    remote.fail_receipt = True
    with pytest.raises(RuleError):
        apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, record(inventory, []))
    remote.fail_receipt = False
    pending = deepcopy(inventory["migration_in_progress"])
    assert pending["statement_count"] == pending["sql_result"]["confirmed_statements"] == 2
    pending["sql_result"]["confirmed_statements"] = 1
    with pytest.raises(RuleError) as caught:
        confirm_migration_receipt(remote, "test-database", pending)
    assert caught.value.code == "MIGRATION_RECEIPT_PENDING"
    assert caught.value.details["migration"]["issues"][0]["reason"] == "execution_evidence_missing"
    assert remote.connection.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0] == 0


def test_early_policy_diagnostic_uses_original_file_location_after_multiple_statements():
    sql = "-- heading\nCREATE TABLE added(id INTEGER);\n\n  UPDATE items SET name = 'private';"
    with pytest.raises(RuleError) as caught:
        validate_migration_plan({FIRST: INITIAL, SECOND: sql}, baseline_files={FIRST: INITIAL})
    issue = caught.value.details["migration"]["issues"][0]
    assert (issue["line"], issue["column"], issue["statement_index"]) == (4, 3, 2)
    assert "private" not in json.dumps(caught.value.details)


def test_empty_semicolons_are_rejected_locally_without_remote_uncertainty():
    with pytest.raises(RuleError) as caught:
        validate_migration_plan({FIRST: "; /* empty */ ;"})
    assert caught.value.code == "MIGRATION_VALIDATION_FAILED"
    assert caught.value.details["migration"]["issues"][0]["reason"] == "empty_sql"


def test_source_history_validation_never_authorizes_unpublished_security_backfill(remote):
    sql = "CREATE TABLE users(id INTEGER, role TEXT); INSERT INTO users VALUES (1, NULL); UPDATE users SET role = 'admin' WHERE role IS NULL;"
    files = {FIRST: sql, "src/index.ts": "export {};"}
    evidence = validate_migration_plan(files, baseline_files=files, revision_id="legacy")
    assert evidence["passed"] is True and evidence["approved_backfills"] == []
    inventory = {}
    with pytest.raises(RuleError) as caught:
        apply_migrations(
            remote, "test-database", {FIRST: sql}, inventory, record(inventory, []),
            revision_id="legacy", source_files=files, validation_evidence=evidence,
        )
    assert caught.value.code == "MIGRATION_REVIEW_REQUIRED"
    assert not any(query == sql for query, _ in remote.calls)
    assert remote.connection.execute("SELECT name FROM sqlite_schema WHERE name='users'").fetchone() is None
    assert inventory.get("migration_in_progress") is None


def test_code_only_revision_rebind_preserves_specific_previous_backfill_approval(remote):
    remote.seed({FIRST: INITIAL})
    sql = "ALTER TABLE items ADD COLUMN label TEXT; UPDATE items SET label = name WHERE label IS NULL;"
    files = {FIRST: INITIAL, SECOND: sql, "src/index.ts": "export {};"}
    first = validate_migration_plan(files, baseline_files={FIRST: INITIAL}, revision_id="r1")
    edited = {**files, "src/index.ts": "export const edited = true;"}
    second = validate_migration_plan(edited, baseline_files=files, previous_evidence=first, revision_id="r2")
    assert second["approved_backfills"] == first["approved_backfills"]
    assert second["artifact_sha256"] != first["artifact_sha256"]
    assert second["baseline_manifest"] == second["manifest"] and second["added"] == []
    inventory = {"migrations": {FIRST: digest(INITIAL)}}
    apply_migrations(
        remote, "test-database", {FIRST: INITIAL, SECOND: sql}, inventory, record(inventory, []),
        revision_id="r2", source_files=edited, validation_evidence=second,
    )
    assert remote.connection.execute("SELECT label FROM items").fetchone()[0] == "preserved"
    assert sum(query == sql for query, _ in remote.calls) == 1


def test_appended_migration_preserves_approval_only_for_unchanged_preceding_plan(remote):
    remote.seed({FIRST: INITIAL})
    sql = "ALTER TABLE items ADD COLUMN label TEXT; UPDATE items SET label = name WHERE label IS NULL;"
    files = {FIRST: INITIAL, SECOND: sql}
    first = validate_migration_plan(files, baseline_files={FIRST: INITIAL}, revision_id="r1")
    extended = {**files, "migrations/0003.sql": "ALTER TABLE items ADD COLUMN description TEXT;"}
    second = validate_migration_plan(extended, baseline_files=files, previous_evidence=first, revision_id="r2")
    assert second["approved_backfills"] == first["approved_backfills"]
    inventory = {"migrations": {FIRST: digest(INITIAL)}}
    apply_migrations(remote, "test-database", extended, inventory, record(inventory, []), validation_evidence=second)
    assert remote.connection.execute("SELECT label, description FROM items").fetchone()[:] == ("preserved", None)


def test_changed_sql_never_inherits_a_previous_statement_approval():
    sql = "ALTER TABLE items ADD COLUMN label TEXT; UPDATE items SET label = name WHERE label IS NULL;"
    files = {FIRST: INITIAL, SECOND: sql}
    first = validate_migration_plan(files, baseline_files={FIRST: INITIAL})
    changed = {FIRST: INITIAL, SECOND: sql.replace("label", "role")}
    second = validate_migration_plan(changed, baseline_files=changed, previous_evidence=first)
    assert second["approved_backfills"] == []


def test_local_before_request_rejection_proves_request_was_not_sent():
    calls = []
    gateway = CloudflareGateway(POLICY, "private-token", httpx.MockTransport(lambda request: calls.append(request)))

    def guard(method):
        assert method == "POST"
        raise RuleError("CONFIGURATION_INVALID", "A configuração mudou antes do envio.")

    gateway.before_request = guard
    with pytest.raises(RuleError) as caught:
        gateway.query("db", "CREATE TABLE items(id INTEGER);")
    assert caught.value.code == "CONFIGURATION_INVALID" and calls == []
    assert caught.value.provider_diagnostic["effect_outcome"] == "not_sent"
    assert caught.value.provider_diagnostic["reason"] == "local_dispatch_blocked"


def test_local_guard_evidence_survives_a_second_configuration_refusal_in_checkpoint(remote):
    original = remote.query
    transport_calls = []
    gateway = CloudflareGateway(POLICY, "private-token", httpx.MockTransport(lambda request: transport_calls.append(request)))

    def guard(method):
        raise RuleError("CONFIGURATION_INVALID", "A configuração mudou antes do envio.")

    gateway.before_request = guard

    def query(database_id, sql, params=None):
        if sql == INITIAL:
            return gateway.query(database_id, sql, params)
        return original(database_id, sql, params)

    remote.query = query
    inventory, checkpoints = {}, []
    remember = record(inventory, checkpoints)

    def checkpoint(stage, changes=None, external=None):
        if changes and "migration_in_progress" in changes and changes["migration_in_progress"] is None:
            raise RuleError("CONFIGURATION_INVALID", "A configuração continua indisponível.")
        remember(stage, changes, external)
        if inventory.get("migration_in_progress"):
            inventory["migration_in_progress"].update(operation_id="operation-one", revision_id="revision-one")
        return {"inventory": deepcopy(inventory)}

    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, checkpoint, revision_id="revision-one")
    assert caught.value.code == "CONFIGURATION_INVALID" and transport_calls == []
    assert caught.value.message == "A configuração mudou antes do envio."
    resolution = caught.value.migration_resolution
    assert resolution["phase"] == "sql_rejected" and resolution["effect_outcome"] == "not_sent"
    assert resolution["operation_id"] == "operation-one" and resolution["revision_id"] == "revision-one"
    assert resolution["name"] == FIRST and resolution["sha256"] == digest(INITIAL)
    assert inventory["migration_in_progress"]["phase"] == "sql_sending"
    assert remote.connection.execute("SELECT name FROM sqlite_schema WHERE name='items'").fetchone() is None


def test_lost_lease_before_send_never_attempts_to_clear_the_new_workers_marker(remote):
    original = remote.query
    gateway = CloudflareGateway(POLICY, "private-token", httpx.MockTransport(lambda request: pytest.fail("No dispatch expected")))

    def guard(method):
        raise RuleError("LEASE_LOST", "A operação foi assumida por outro worker.")

    gateway.before_request = guard
    remote.query = lambda database_id, sql, params=None: gateway.query(database_id, sql, params) if sql == INITIAL else original(database_id, sql, params)
    inventory, checkpoints = {}, []
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, record(inventory, checkpoints))
    assert caught.value.code == "LEASE_LOST"
    assert inventory["migration_in_progress"]["phase"] == "sql_sending"
    assert not any(changes.get("migration_in_progress", "missing") is None for _, changes, _ in checkpoints)


def test_unsent_connect_timeout_remains_specific_rejection_when_cleanup_is_refused(remote):
    original = remote.query

    def connect_timeout(request):
        raise httpx.ConnectTimeout("connection unavailable", request=request)

    gateway = CloudflareGateway(POLICY, "private-token", httpx.MockTransport(connect_timeout))
    remote.query = lambda database_id, sql, params=None: gateway.query(database_id, sql, params) if sql == INITIAL else original(database_id, sql, params)
    inventory, checkpoints = {}, []
    remember = record(inventory, checkpoints)

    def checkpoint(stage, changes=None, external=None):
        if changes and "migration_in_progress" in changes and changes["migration_in_progress"] is None:
            raise RuleError("CONFIGURATION_INVALID", "A configuração mudou depois da falha de conexão.")
        remember(stage, changes, external)

    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, checkpoint)
    assert caught.value.code == "MIGRATION_SQL_REJECTED"
    assert not isinstance(caught.value, PublicationUncertain)
    assert caught.value.provider_diagnostic["transport_error"] == "ConnectTimeout"
    assert caught.value.provider_diagnostic["effect_outcome"] == "not_sent"
    assert caught.value.migration_resolution["effect_outcome"] == "not_sent"
    assert caught.value.migration_resolution["code"] == "PUBLICATION_RESULT_UNKNOWN"
    assert inventory["migration_in_progress"]["phase"] == "sql_sending"
    assert remote.connection.execute("SELECT name FROM sqlite_schema WHERE name='items'").fetchone() is None
