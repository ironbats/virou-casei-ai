import json
import sqlite3
from copy import deepcopy
from hashlib import sha256

import pytest

from viracase.domain.rules import RuleError
from viracase.infrastructure.cloudflare_gateway import PublicationUncertain
from viracase.infrastructure.publication_migrations import (
    LEDGER,
    apply_migrations,
    confirmed_migrations,
    preflight_migrations,
)
from viracase.infrastructure.publication_sql_review import split_sql_statements

INITIAL = "CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT NOT NULL);\nINSERT INTO items(name) VALUES ('preserved');\n"
ADDITION = "ALTER TABLE items ADD COLUMN done INTEGER NOT NULL DEFAULT 0;\n"
FIRST = "migrations/0001.sql"
SECOND = "migrations/0002.sql"
REVISION = "a2b6e4f0-6677-4888-9999-123456789abc"


def digest(sql):
    return sha256(sql.encode()).hexdigest()


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.calls = []
        self.fail_receipt = False

    def query(self, database_id, sql, params=None):
        assert database_id == "test-database"
        self.calls.append((sql, params))
        if self.fail_receipt and sql.startswith(f"INSERT INTO {LEDGER}"):
            raise PublicationUncertain()
        if sql.startswith("SELECT") or params:
            cursor = self.connection.execute(sql, params or [])
            rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
        else:
            self.connection.executescript(sql)
            rows = []
        self.connection.commit()
        return [{"success": True, "results": rows} for _ in split_sql_statements(sql)]

    def seed(self, files):
        self.connection.execute(f"CREATE TABLE {LEDGER}(name TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
        for name, sql in sorted(files.items()):
            self.connection.executescript(sql)
            self.connection.execute(f"INSERT INTO {LEDGER}(name, sha256) VALUES (?, ?)", (name, digest(sql)))
        self.connection.commit()


@pytest.fixture
def remote():
    gateway = Database()
    yield gateway
    gateway.connection.close()


def record(inventory, checkpoints):
    def checkpoint(stage, changes=None, external=None):
        checkpoints.append((stage, deepcopy(changes), external))
        inventory.update(deepcopy(changes or {}))

    return checkpoint


@pytest.mark.parametrize(
    "files,reason", [({FIRST: INITIAL + "\n"}, "checksum_changed"), ({}, "missing_file")]
)
def test_local_preflight_rejects_changed_or_removed_applied_migration_before_remote_calls(
    remote, files, reason
):
    inventory = {"migrations": {FIRST: digest(INITIAL)}, "published_revision_id": REVISION}
    before, checkpoints = deepcopy(inventory), []
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", files, inventory, record(inventory, checkpoints))
    assert caught.value.code == "MIGRATION_CHECKSUM_CONFLICT"
    diagnostic = caught.value.details["migration"]
    assert diagnostic["published_revision_id"] == REVISION
    assert diagnostic["issues"][0]["path"] == FIRST
    assert diagnostic["issues"][0]["expected_sha256"] == digest(INITIAL)
    actual = digest(files[FIRST]) if FIRST in files else None
    assert diagnostic["issues"][0]["actual_sha256"] == actual
    assert diagnostic["issues"][0]["reason"] == reason
    assert diagnostic["issues"][0]["receipt_source"] == "inventory"
    assert inventory == before and remote.calls == checkpoints == []
    assert "CREATE TABLE" not in json.dumps(diagnostic)


def test_preflight_accepts_unchanged_history_and_new_increment_without_mutating_inventory():
    inventory = {"migrations": {FIRST: digest(INITIAL)}}
    before = deepcopy(inventory)
    assert preflight_migrations({FIRST: INITIAL, SECOND: ADDITION}, inventory) is None
    assert inventory == before


def test_confirmed_history_is_a_copy_and_does_not_promote_pending_receipts():
    inventory = {
        "migrations": {FIRST: digest(INITIAL)},
        "migration_in_progress": {"name": SECOND, "sha256": digest(ADDITION)},
    }
    confirmed = confirmed_migrations(inventory)
    assert confirmed == {FIRST: digest(INITIAL)}
    confirmed[SECOND] = digest(ADDITION)
    assert inventory["migrations"] == {FIRST: digest(INITIAL)}


@pytest.mark.parametrize(
    "history", [None, [], {"../../private.sql": digest(INITIAL)}, {FIRST: "invalid-checksum"}]
)
def test_context_rejects_unproven_or_malformed_inventory_without_exposing_values(history):
    with pytest.raises(RuleError) as caught:
        confirmed_migrations({"migrations": history})
    assert caught.value.code == "MIGRATION_RESULT_UNKNOWN"
    assert caught.value.details["migration"]["issues"][0]["reason"] == "invalid_receipt"
    assert "private" not in json.dumps(caught.value.details)


def test_all_remote_receipts_are_checked_before_any_new_migration_is_applied(remote):
    remote.seed({SECOND: INITIAL})
    files = {FIRST: "CREATE TABLE should_not_exist(id INTEGER);", SECOND: INITIAL + "\n"}
    inventory, checkpoints = {}, []
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", files, inventory, record(inventory, checkpoints))
    assert caught.value.code == "MIGRATION_CHECKSUM_CONFLICT"
    issue = caught.value.details["migration"]["issues"][0]
    assert issue["path"] == SECOND and issue["receipt_source"] == "remote"
    assert inventory["migrations"] == {SECOND: digest(INITIAL)}
    assert len(remote.calls) == 2
    assert remote.connection.execute("SELECT name FROM items").fetchone()[0] == "preserved"
    assert (
        remote.connection.execute("SELECT name FROM sqlite_master WHERE name = 'should_not_exist'").fetchone()
        is None
    )
    assert all(external is False for _, _, external in checkpoints)


def test_removed_remote_migration_is_not_silently_ignored(remote):
    remote.seed({FIRST: INITIAL})
    inventory = {}
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", {SECOND: ADDITION}, inventory, record(inventory, []))
    assert caught.value.code == "MIGRATION_CHECKSUM_CONFLICT"
    assert caught.value.details["migration"]["issues"][0]["reason"] == "missing_file"
    assert len(remote.calls) == 2


def test_remote_and_local_receipt_conflict_never_overwrites_either_history(remote):
    remote.seed({FIRST: INITIAL})
    changed = INITIAL + "\n"
    inventory = {"migrations": {FIRST: digest(changed)}}
    before = deepcopy(inventory)
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", {FIRST: changed}, inventory, record(inventory, []))
    assert caught.value.code == "MIGRATION_CHECKSUM_CONFLICT"
    issue = caught.value.details["migration"]["issues"][0]
    assert issue["reason"] == "receipt_conflict"
    assert issue["expected_sha256"] == digest(changed) and issue["actual_sha256"] == digest(INITIAL)
    assert inventory == before
    assert remote.connection.execute(f"SELECT sha256 FROM {LEDGER}").fetchone()[0] == digest(INITIAL)


def test_missing_remote_receipt_for_confirmed_local_history_blocks_replay(remote):
    remote.seed({})
    inventory = {"migrations": {FIRST: digest(INITIAL)}}
    before = deepcopy(inventory)
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, record(inventory, []))
    assert caught.value.code == "MIGRATION_RESULT_UNKNOWN"
    assert caught.value.details["migration"]["issues"][0]["reason"] == "receipt_missing"
    assert inventory == before and len(remote.calls) == 2
    assert remote.connection.execute("SELECT name FROM sqlite_master WHERE name = 'items'").fetchone() is None


@pytest.mark.parametrize("pending_name", [FIRST, "migrations/removed.sql"])
def test_unconfirmed_pending_migration_blocks_all_other_sql_even_when_removed(remote, pending_name):
    inventory = {"migration_in_progress": {"name": pending_name, "sha256": digest(INITIAL)}}
    before = deepcopy(inventory)
    with pytest.raises(RuleError) as caught:
        apply_migrations(
            remote,
            "test-database",
            {SECOND: "CREATE TABLE another(id INTEGER);"},
            inventory,
            record(inventory, []),
        )
    assert caught.value.code == "MIGRATION_RESULT_UNKNOWN"
    assert caught.value.details["migration"]["issues"][0]["reason"] == "pending_receipt_missing"
    assert inventory == before and len(remote.calls) == 2
    assert (
        remote.connection.execute("SELECT name FROM sqlite_master WHERE name = 'another'").fetchone() is None
    )


def test_conflicting_pending_receipt_does_not_clear_pending_or_retry(remote):
    remote.seed({FIRST: INITIAL})
    inventory = {"migration_in_progress": {"name": FIRST, "sha256": digest(INITIAL + "\n")}}
    before = deepcopy(inventory)
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, record(inventory, []))
    assert caught.value.code == "MIGRATION_CHECKSUM_CONFLICT"
    assert caught.value.details["migration"]["issues"][0]["reason"] == "pending_receipt_conflict"
    assert inventory == before and len(remote.calls) == 2


def test_confirmed_pending_receipt_allows_increment_and_republication_without_duplicate_sql(remote):
    remote.seed({FIRST: INITIAL})
    inventory = {"migration_in_progress": {"name": FIRST, "sha256": digest(INITIAL)}}
    checkpoints = []
    files = {FIRST: INITIAL, SECOND: ADDITION}
    apply_migrations(remote, "test-database", files, inventory, record(inventory, checkpoints))
    assert inventory["migration_in_progress"] is None
    assert inventory["migrations"] == {FIRST: digest(INITIAL), SECOND: digest(ADDITION)}
    assert checkpoints[0][1]["migration_in_progress"] is None and checkpoints[0][2] is False
    assert checkpoints[-1][2] is False
    assert any(
        changes.get("migration_in_progress") and external is True for _, changes, external in checkpoints
    )
    assert remote.connection.execute("SELECT name, done FROM items").fetchall()[0][:] == ("preserved", 0)
    assert sum(sql == INITIAL for sql, _ in remote.calls) == 0
    assert sum(sql == ADDITION for sql, _ in remote.calls) == 1
    apply_migrations(remote, "test-database", files, inventory, record(inventory, checkpoints))
    assert sum(sql == ADDITION for sql, _ in remote.calls) == 1
    assert remote.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_sql_success_without_receipt_is_never_automatically_replayed(remote):
    inventory, checkpoints = {}, []
    remote.fail_receipt = True
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, record(inventory, checkpoints))
    assert caught.value.code == "MIGRATION_RECEIPT_PENDING"
    pending = inventory["migration_in_progress"]
    assert pending["name"] == FIRST and pending["sha256"] == digest(INITIAL)
    assert pending["phase"] == "receipt_sending" and pending["effect_outcome"] == "confirmed"
    assert pending["sql_result"]["confirmed_statements"] > 0 and pending["schema_after"]["sha256"]
    assert remote.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    remote.fail_receipt = False
    apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, record(inventory, checkpoints))
    assert inventory["migration_in_progress"] is None and inventory["migrations"][FIRST] == digest(INITIAL)
    assert sum(sql == INITIAL for sql, _ in remote.calls) == 1
    assert remote.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_remote_receipt_recovers_crash_before_local_confirmation_without_replaying_sql(remote):
    inventory, checkpoints = {}, []
    checkpoint = record(inventory, checkpoints)

    def crash(stage, changes=None, external=None):
        if changes and changes.get("migration_in_progress", "absent") is None:
            raise RuntimeError("local-checkpoint-interrupted")
        checkpoint(stage, changes, external)

    with pytest.raises(RuntimeError, match="local-checkpoint-interrupted"):
        apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, crash)
    assert inventory["migration_in_progress"]["name"] == FIRST
    apply_migrations(remote, "test-database", {FIRST: INITIAL}, inventory, checkpoint)
    assert inventory["migration_in_progress"] is None
    assert inventory["migrations"] == {FIRST: digest(INITIAL)}
    assert sum(sql == INITIAL for sql, _ in remote.calls) == 1
    assert remote.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_potentially_destructive_later_file_blocks_all_new_migrations(remote):
    inventory = {}
    files = {FIRST: INITIAL, SECOND: "DELETE FROM items;"}
    with pytest.raises(RuleError) as caught:
        apply_migrations(remote, "test-database", files, inventory, record(inventory, []))
    assert caught.value.code == "MIGRATION_REVIEW_REQUIRED"
    assert caught.value.details["migration"]["issues"][0]["path"] == SECOND
    assert len(remote.calls) == 2
    assert remote.connection.execute("SELECT name FROM sqlite_master WHERE name = 'items'").fetchone() is None


@pytest.mark.parametrize(
    "rows",
    [
        None,
        [],
        [{"success": True}],
        [{"success": True, "results": [{"name": "../../private.sql", "sha256": "private-receipt"}]}],
        [
            {
                "success": True,
                "results": [
                    {"name": FIRST, "sha256": digest(INITIAL)},
                    {"name": FIRST, "sha256": digest(INITIAL)},
                ],
            }
        ],
    ],
)
def test_invalid_remote_history_is_bounded_and_never_exposes_untrusted_receipt(rows):
    class InvalidDatabase:
        def query(self, *_):
            return rows

    with pytest.raises(RuleError) as caught:
        apply_migrations(
            InvalidDatabase(), "test-database", {FIRST: INITIAL}, {}, lambda *_args, **_kwargs: None
        )
    assert caught.value.code == "MIGRATION_RESULT_UNKNOWN"
    serialized = json.dumps(caught.value.details)
    assert "private" not in serialized and "../" not in serialized


def test_diagnostics_limit_issues_and_do_not_echo_arbitrary_revision_or_sql():
    files = {f"migrations/{number:04}.sql": INITIAL for number in range(20)}
    inventory = {
        "migrations": {name: digest(sql + "\n") for name, sql in files.items()},
        "published_revision_id": "private-revision-value",
    }
    with pytest.raises(RuleError) as caught:
        preflight_migrations(files, inventory)
    diagnostic = caught.value.details["migration"]
    assert diagnostic["issue_count"] == 20 and len(diagnostic["issues"]) == 12
    assert "published_revision_id" not in diagnostic
    assert "preserved" not in json.dumps(diagnostic)
