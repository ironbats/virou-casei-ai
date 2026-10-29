from copy import deepcopy
from hashlib import sha256

import pytest
from test_publication_migration_integrity import Database

from viracase.infrastructure.publication_migrations import LEDGER
from viracase.infrastructure.publication_recovery_evidence import _simple_column_addition, assess_migration_recovery

FIRST = "migrations/0001.sql"
SECOND = "migrations/0005_add_salt_to_users_and_update_admin.sql"
BASE = "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL); INSERT INTO users VALUES (1, 'preserved');"
SQL = "ALTER TABLE users ADD COLUMN salt TEXT NOT NULL DEFAULT 'temp_salt_placeholder';"
OPERATION = "ce81e74c-8604-477b-95ff-ff2f703bf730"
REVISION = "68cab41e-054f-4e70-a74f-20961d197de3"


def digest(value):
    return sha256(value.encode()).hexdigest()


@pytest.fixture
def remote():
    gateway = Database()
    gateway.seed({FIRST: BASE})
    yield gateway
    gateway.connection.close()


def assess(remote, sql=SQL, *, state=None, files=None, **kwargs):
    inventory = state if state is not None else {
        "database_id": "test-database",
        "migrations": {FIRST: digest(BASE)},
        "migration_in_progress": {"name": SECOND, "sha256": digest(sql)},
    }
    return assess_migration_recovery(
        remote,
        "test-database",
        files if files is not None else {FIRST: BASE, SECOND: sql},
        inventory,
        operation_id=OPERATION,
        revision_id=REVISION,
        **kwargs,
    )


def test_single_sensitive_column_absent_from_complete_schema_can_be_attested_as_unchanged(remote):
    before = [tuple(row) for row in remote.connection.execute("SELECT * FROM users")]
    result = assess(remote)
    assert result["classification"] == "schema_unchanged"
    assert result["action"] == "confirm_schema_unchanged"
    assert result["origin"] == "schema_postconditions"
    assert result["requires_operator"] is True
    assert result["historical_provider_result_verified"] is False
    assert result["operator_evidence_verified"] is False
    assert result["checks"] == {
        "identity": "matched",
        "source": "matched",
        "history": "matched",
        "receipt": "absent",
        "execution": "unconfirmed",
        "schema": "matches_baseline",
    }
    assert result["statement_count"] == 1
    assert result["statement_manifest"][0]["kind"] == "security_sensitive"
    assert result["missing_evidence"] == ["operator_structural_attestation"]
    assert result["schema"]["baseline_sha256"] != result["schema"]["expected_sha256"]
    assert "pending_update" not in result
    assert all(sql.startswith("SELECT") for sql, _ in remote.calls)
    assert [tuple(row) for row in remote.connection.execute("SELECT * FROM users")] == before
    assert remote.connection.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0] == 1


@pytest.mark.parametrize("sql", [
    "ALTER TABLE users ADD salt TEXT;",
    'ALTER TABLE "users" ADD COLUMN "salt" TEXT DEFAULT NULL;',
    "ALTER TABLE users ADD role INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE users ADD salt REAL DEFAULT -1.25;",
    "ALTER TABLE users ADD salt NUMERIC DEFAULT .25;",
    "ALTER TABLE users ADD salt NUMERIC DEFAULT 1e-3;",
    "ALTER TABLE users ADD salt BLOB DEFAULT X'00FF';",
    "ALTER TABLE users ADD salt TEXT DEFAULT 'UPDATE users; DROP TABLE users;';",
])
def test_unchanged_sensitive_column_supports_only_closed_scalar_constant_grammar(remote, sql):
    result = assess(remote, sql)
    assert result["classification"] == "schema_unchanged"
    assert result["requires_operator"] is True
    assert result["checks"]["schema"] == "matches_baseline"
    assert all(query.startswith("SELECT") for query, _ in remote.calls)


@pytest.mark.parametrize("change", [
    SQL,
    "ALTER TABLE users ADD salt TEXT;",
    "CREATE INDEX users_extra ON users(name);",
    "CREATE TABLE other(id INTEGER);",
    "CREATE VIEW users_extra AS SELECT name FROM users;",
    "CREATE TRIGGER users_extra AFTER INSERT ON users BEGIN SELECT 1; END;",
])
def test_applied_sensitive_column_or_any_remote_schema_drift_remains_unresolved(remote, change):
    remote.connection.executescript(change)
    result = assess(remote)
    assert result["classification"] == "unknown"
    assert result["requires_operator"] is False
    assert result["checks"]["schema"] in {"matches_expected", "mismatch"}
    assert "pending_update" not in result
    assert all(query.startswith("SELECT") for query, _ in remote.calls)


@pytest.mark.parametrize("sql", [
    SQL + " UPDATE users SET name = 'changed' WHERE id = 1;",
    SQL + " ALTER TABLE users ADD permission TEXT;",
    "UPDATE users SET name = 'admin' WHERE id = 1;",
    "CREATE TABLE credentials(id INTEGER);",
    "ALTER TABLE users RENAME COLUMN name TO password;",
    "PRAGMA user_version = 1;",
    "ALTER TABLE users ADD salt TEXT DEFAULT ('constant');",
    "ALTER TABLE users ADD salt TEXT CHECK(length(salt) > 1);",
    "ALTER TABLE users ADD salt TEXT REFERENCES users(name);",
    "ALTER TABLE users ADD salt TEXT COLLATE NOCASE;",
    "ALTER TABLE users ADD salt TEXT GENERATED ALWAYS AS (lower(name)) VIRTUAL;",
    "ALTER TABLE users ADD salt VARCHAR(64);",
])
def test_unchanged_schema_does_not_authorize_mixed_sensitive_or_extended_sql(remote, sql):
    result = assess(remote, sql)
    assert result["classification"] == "unknown"
    assert "pending_update" not in result
    assert all(query.startswith("SELECT") for query, _ in remote.calls)


@pytest.mark.parametrize("change", ["hash", "operation", "revision", "database", "history", "receipt", "saved_schema"])
def test_sensitive_unchanged_column_keeps_identity_receipt_history_and_saved_schema_guards(remote, change):
    state = {
        "database_id": "test-database",
        "migrations": {FIRST: digest(BASE)},
        "migration_in_progress": {"name": SECOND, "sha256": digest(SQL)},
    }
    pending = state["migration_in_progress"]
    if change == "hash":
        pending["sha256"] = "0" * 64
    elif change == "operation":
        pending["operation_id"] = "different-operation"
    elif change == "revision":
        pending["revision_id"] = "different-revision"
    elif change == "database":
        state["database_id"] = "different-database"
    elif change == "history":
        remote.connection.execute(f"DELETE FROM {LEDGER} WHERE name = ?", [FIRST])
    elif change == "receipt":
        pending["phase"] = "receipt_confirmed"
    else:
        pending["schema_after"] = {"sha256": "0" * 64, "object_count": 1}
    before = deepcopy(state)
    result = assess(remote, state=state)
    assert result["classification"] in {"unknown", "conflict"}
    assert state == before
    assert "pending_update" not in result
    assert all(query.startswith("SELECT") for query, _ in remote.calls)


def test_operator_claim_cannot_convert_unchanged_sensitive_column_into_applied(remote):
    original = assess(remote)
    claimed = assess(remote, operator_evidence={
        "outcome": "applied",
        "success": True,
        "statement_indices": [1],
        "evidence_sha256": "f" * 64,
    })
    assert claimed == original
    assert claimed["classification"] == "schema_unchanged"
    assert claimed["operator_evidence_verified"] is False


def test_unrecognized_or_empty_sensitive_sql_does_not_authorize_unchanged_resolution(remote):
    for sql in ("", "ALTER TABLE users ADD salt TEXT DEFAULT random();", "ALTER TABLE users ADD salt TEXT NOT NULL;"):
        result = assess(remote, sql)
        assert result["classification"] == "unknown"
        assert "pending_update" not in result


@pytest.mark.parametrize("sql", [
    "ALTER TABLE users ADD salt;",
    "ALTER TABLE users ADD salt TEXT DEFAULT;",
    "ALTER TABLE users ADD salt TEXT NOT DEFAULT 'value';",
    "ALTER TABLE users ADD salt TEXT DEFAULT 'value' NOT NULL;",
    "ALTER TABLE users ADD salt TEXT DEFAULT CURRENT_TIMESTAMP;",
    "ALTER TABLE users ADD salt TEXT DEFAULT ('constant');",
    "ALTER TABLE users ADD salt INTEGER DEFAULT (1 + 1);",
    "ALTER TABLE users ADD salt TEXT DEFAULT 'a' || 'b';",
    "ALTER TABLE users ADD salt TEXT DEFAULT randomblob(4);",
    "ALTER TABLE users ADD salt TEXT DEFAULT load_extension('unused');",
    "ALTER TABLE users ADD salt TEXT DEFAULT readfile('unused');",
    "ALTER TABLE users ADD salt TEXT DEFAULT writefile('unused', 'value');",
    "ALTER TABLE users ADD salt TEXT DEFAULT ?;",
    "ALTER TABLE users ADD salt TEXT DEFAULT :value;",
    'ALTER TABLE users ADD salt TEXT DEFAULT "value";',
    "ALTER TABLE users ADD salt REAL DEFAULT 1 2;",
    "ALTER TABLE users ADD salt REAL DEFAULT 1/*gap*/e3;",
    "ALTER TABLE users ADD salt BLOB DEFAULT X'0';",
    "ALTER TABLE users ADD salt BLOB DEFAULT X'zz';",
    "ALTER TABLE users ADD salt BLOB DEFAULT X 'FF';",
    "ALTER TABLE users ADD salt TEXT UNIQUE;",
    "ALTER TABLE users ADD salt INTEGER PRIMARY KEY;",
    "ALTER TABLE users ADD salt TEXT CONSTRAINT present CHECK(salt IS NOT NULL);",
    "ALTER TABLE users ADD salt TEXT REFERENCES users(name);",
    "ALTER TABLE users ADD salt TEXT COLLATE NOCASE;",
    "ALTER TABLE users ADD salt TEXT GENERATED ALWAYS AS (lower(name)) VIRTUAL;",
    "ALTER TABLE users ADD salt TEXT DEFAULT 'unterminated;",
    "ALTER TABLE users ADD salt TEXT\x00;",
    "ALTER TABLE __viroucaseai_migrations ADD salt TEXT;",
    "ALTER TABLE users ADD salt TEXT; UPDATE users SET name = 'changed';",
    "ALTER TABLE users ADD salt TEXT; ALTER TABLE users ADD permission TEXT;",
    "CREATE TABLE credentials(id INTEGER);",
    "ALTER TABLE users RENAME COLUMN name TO salt;",
])
def test_closed_column_grammar_rejects_expressions_constraints_ledger_and_statement_smuggling(sql):
    assert _simple_column_addition(sql) is False
