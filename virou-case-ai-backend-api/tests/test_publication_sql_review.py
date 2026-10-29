import json
from copy import deepcopy
from hashlib import sha256

import pytest
from test_publication_migration_integrity import FIRST, INITIAL, SECOND, Database, digest, record

from viracase.domain.rules import RuleError
from viracase.infrastructure.publication_migrations import LEDGER, apply_migrations
from viracase.infrastructure.publication_sql_review import ANALYSIS_METHOD, sql_review_findings
from viracase.infrastructure.publication_verification import failure_record, migration_diagnostic


@pytest.fixture
def remote():
    database = Database()
    yield database
    database.connection.close()


@pytest.mark.parametrize("action", ["CASCADE", "RESTRICT", "SET NULL", "SET DEFAULT", "NO ACTION"])
@pytest.mark.parametrize("event", ["DELETE", "UPDATE"])
def test_create_table_foreign_key_actions_preserve_published_data(remote, event, action):
    remote.seed({FIRST: INITIAL})
    sql = (
        "CREATE TABLE carts(id INTEGER PRIMARY KEY, item_id INTEGER, "
        f"FOREIGN KEY(item_id) REFERENCES items(id) ON {event} {action});\n"
        "INSERT INTO carts(id, item_id) VALUES (1, 1);"
    )
    inventory = {"migrations": {FIRST: digest(INITIAL)}}
    apply_migrations(remote, "test-database", {FIRST: INITIAL, SECOND: sql}, inventory, record(inventory, []))
    assert remote.connection.execute("SELECT name FROM items").fetchall()[0][0] == "preserved"
    assert remote.connection.execute("SELECT item_id FROM carts").fetchall()[0][0] == 1
    assert inventory["migrations"][SECOND] == digest(sql)
    apply_migrations(remote, "test-database", {FIRST: INITIAL, SECOND: sql}, inventory, record(inventory, []))
    assert sum(query == sql for query, _ in remote.calls) == 1


def test_inline_references_with_both_actions_and_comments_are_not_commands(remote):
    remote.seed({FIRST: INITIAL})
    sql = (
        "CREATE/* DROP TABLE items; */TABLE carts (id INTEGER PRIMARY KEY, "
        "item_id INTEGER REFERENCES items(id) ON /* delete action */ DELETE CASCADE ON UPDATE SET NULL);"
        "ALTER TABLE carts ADD COLUMN original_item INTEGER REFERENCES items(id) ON DELETE RESTRICT;"
    )
    inventory = {"migrations": {FIRST: digest(INITIAL)}}
    apply_migrations(remote, "test-database", {FIRST: INITIAL, SECOND: sql}, inventory, record(inventory, []))
    assert remote.connection.execute("SELECT name FROM items").fetchone()[0] == "preserved"
    assert remote.connection.execute("PRAGMA foreign_key_list(carts)").fetchall()


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE notices(message TEXT DEFAULT 'DROP DELETE UPDATE PRAGMA');",
        "CREATE TABLE notices(message TEXT DEFAULT 'It''s safe -- DELETE FROM items;');",
        'CREATE TABLE "DELETE" ("UPDATE" TEXT, [DROP] TEXT, `REPLACE` TEXT);',
        'CREATE TABLE "a""DELETE" ("b""UPDATE" TEXT);',
        "SELECT replace('UPDATE', 'UP', 'DOWN');",
        "SELECT '/* DROP */', '-- UPDATE', 'DELETE\nFROM items';",
        "-- DROP TABLE items;\n/* DELETE FROM items; */\nSELECT 1;",
        "SELECT 1; /* DELETE FROM items;",
        "\ufeffCREATE TABLE extra(id INTEGER);",
        "CREATE TRIGGER audit AFTER UPDATE ON items BEGIN INSERT INTO audit_log VALUES (NEW.id); END;",
        "CREATE TRIGGER audit UPDATE OF name ON items BEGIN INSERT INTO audit_log VALUES (NEW.id); END;",
        "CREATE TRIGGER audit DELETE ON items BEGIN INSERT INTO audit_log VALUES (OLD.id); END;",
    ],
)
def test_literals_identifiers_comments_and_trigger_events_are_not_executed_mutations(sql):
    assert sql_review_findings(sql, LEDGER) == []


@pytest.mark.parametrize(
    "sql,operation,rule",
    [
        ("DROP TABLE items;", "DROP", "drop_object"),
        ("ALTER TABLE items DROP COLUMN name;", "DROP", "drop_object"),
        ("DELETE FROM items WHERE id = 1;", "DELETE", "delete_rows"),
        ("TRUNCATE TABLE items;", "TRUNCATE", "truncate_rows"),
        ("UPDATE items SET name = 'changed' WHERE id = 1;", "UPDATE", "update_rows"),
        ("REPLACE INTO items(id, name) VALUES (1, 'changed');", "REPLACE", "replace_rows"),
        ("INSERT OR REPLACE INTO items(id, name) VALUES (1, 'changed');", "REPLACE", "replace_rows"),
        (
            "INSERT INTO items(id) VALUES(1) ON CONFLICT(id) DO UPDATE SET name = 'changed';",
            "UPDATE",
            "update_rows",
        ),
        (
            "WITH old AS (SELECT id FROM items) DELETE FROM items WHERE id IN (SELECT id FROM old);",
            "DELETE",
            "delete_rows",
        ),
        ("WITH old AS (SELECT id FROM items) UPDATE items SET name = 'changed';", "UPDATE", "update_rows"),
        ("CREATE TRIGGER clear AFTER INSERT ON items BEGIN DELETE FROM items; END;", "DELETE", "delete_rows"),
        (
            "CREATE TRIGGER clear AFTER UPDATE ON items BEGIN UPDATE items SET name = 'changed'; END;",
            "UPDATE",
            "update_rows",
        ),
        ("CREATE TABLE additions(id INTEGER); DELETE FROM items;", "DELETE", "delete_rows"),
        (
            "CREATE TABLE additions(id INTEGER REFERENCES items(id) ON DELETE CASCADE); DELETE FROM items;",
            "DELETE",
            "delete_rows",
        ),
        ("SELECT '-- not a comment'; DELETE FROM items;", "DELETE", "delete_rows"),
        ("SELECT '/*'; DROP TABLE items; -- */", "DROP", "drop_object"),
        ("DELETE/* safe-looking comment */FROM items;", "DELETE", "delete_rows"),
        ("ATTACH DATABASE 'private.db' AS external;", "ATTACH", "external_database"),
        ("DETACH DATABASE external;", "DETACH", "external_database"),
        ("PRAGMA foreign_keys = OFF;", "PRAGMA", "connection_setting"),
    ],
)
def test_real_mutations_are_blocked_before_any_pending_sql(remote, sql, operation, rule):
    remote.seed({FIRST: INITIAL})
    before = {"migrations": {FIRST: digest(INITIAL)}}
    inventory, checkpoints = deepcopy(before), []
    with pytest.raises(RuleError) as caught:
        apply_migrations(
            remote, "test-database", {FIRST: INITIAL, SECOND: sql}, inventory, record(inventory, checkpoints)
        )
    assert caught.value.code == "MIGRATION_REVIEW_REQUIRED"
    diagnostic = caught.value.details["migration"]
    assert diagnostic["analysis_method"] == ANALYSIS_METHOD
    issue = diagnostic["issues"][0]
    assert issue["path"] == SECOND and issue["operation"] == operation and issue["rule"] == rule
    assert issue["actual_sha256"] == digest(sql)
    assert issue["line"] >= 1 and issue["column"] >= 1 and issue["statement_index"] >= 1
    assert inventory == before and checkpoints == [] and len(remote.calls) == 2
    assert remote.connection.execute("SELECT name FROM items").fetchone()[0] == "preserved"
    assert not remote.connection.execute("SELECT name FROM sqlite_master WHERE name = 'additions'").fetchall()


@pytest.mark.parametrize("quote", ["", "'", '"', "`", "["])
def test_reserved_ledger_references_remain_protected(quote):
    end = "]" if quote == "[" else quote
    sql = f"SELECT * FROM {quote}{LEDGER}{end};"
    finding = sql_review_findings(sql, LEDGER)[0]
    assert finding["rule"] == "ledger_access" and finding["operation"] == "LEDGER"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'unfinished",
        'SELECT "unfinished',
        "SELECT `unfinished",
        "SELECT [unfinished",
        "SELECT 1;\x00 DELETE FROM items;",
    ],
)
def test_incomplete_lexical_input_is_not_approved(sql):
    assert sql_review_findings(sql, LEDGER)[0]["rule"] == "unrecognized_sql"


def test_diagnostic_reports_precise_location_without_sql_values(remote):
    sql = "-- heading\nSELECT 'private-value';\n  UPDATE items SET name = 'secret-password';"
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", {FIRST: sql}, {}, record({}, []))
    result = failure_record(caught.value, "publication_migrating")
    issue = result["migration"]["issues"][0]
    assert result["migration"]["analysis_method"] == ANALYSIS_METHOD
    assert (issue["line"], issue["column"], issue["statement_index"]) == (3, 3, 2)
    assert issue["statement_type"] == "UPDATE" and issue["operation"] == "UPDATE"
    assert issue["rule"] == "update_rows" and issue["explanation"] and issue["hint"]
    assert issue["actual_sha256"] == sha256(sql.encode()).hexdigest()
    serialized = json.dumps(result)
    assert "private-value" not in serialized and "secret-password" not in serialized
    assert "SET name" not in serialized


def test_every_pending_file_is_reviewed_before_application_and_multiple_issues_are_bounded(remote):
    files = {f"migrations/{index:04}.sql": "DELETE FROM items;" for index in range(20)}
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", files, {}, record({}, []))
    diagnostic = caught.value.details["migration"]
    assert diagnostic["issue_count"] == 20 and len(diagnostic["issues"]) == 12
    assert len(remote.calls) == 2


def diagnostic_issue(**overrides):
    return {
        "code": "MIGRATION_REVIEW_REQUIRED",
        "reason": "unsafe_sql",
        "receipt_source": "candidate",
        "path": FIRST,
        "line": 3,
        "column": 3,
        "statement_index": 2,
        "operation": "DELETE",
        "statement_type": "DELETE",
        "rule": "delete_rows",
        "explanation": "private explanation",
        "hint": "private hint",
        **overrides,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"line": True},
        {"line": 0},
        {"column": -1},
        {"statement_index": 100_000_000},
        {"operation": "DELETE; private-value"},
        {"operation": []},
        {"rule": "ledger_access"},
        {"rule": {}},
        {"statement_type": "private-value"},
    ],
)
def test_untrusted_sql_evidence_is_discarded_without_losing_original_failure(overrides):
    result = migration_diagnostic(
        {
            "code": "MIGRATION_REVIEW_REQUIRED",
            "analysis_method": "private-method",
            "issues": [diagnostic_issue(**overrides)],
        }
    )
    issue = result["issues"][0]
    assert issue["code"] == "MIGRATION_REVIEW_REQUIRED" and issue["path"] == FIRST
    assert "operation" not in issue and "line" not in issue and "analysis_method" not in result
    assert "private" not in json.dumps(result)


def test_sql_evidence_explanations_and_hints_always_come_from_trusted_rules():
    result = migration_diagnostic({"code": "MIGRATION_REVIEW_REQUIRED", "issues": [diagnostic_issue()]})
    issue = result["issues"][0]
    assert issue["operation"] == "DELETE" and issue["line"] == 3
    assert "private" not in json.dumps(result)
