import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from threading import Barrier, Event

import pytest
from sqlalchemy import func, select
from test_publication_pipeline import POLICY, Builder, qualify
from test_publication_pipeline import publication as publication_fixture
from test_publication_resolution_http import authenticated_client
from test_publication_resolution_http import clients as clients_fixture
from test_publication_resolution_http import recovery_case as recovery_case_fixture

from viracase.domain.publication_recovery_access import authorize_publication_recovery
from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway, PublicationUncertain
from viracase.infrastructure.models import (
    Account,
    Attempt,
    Audit,
    Job,
    Operation,
    PublicationResource,
    Reservation,
)
from viracase.infrastructure.publication_migrations import LEDGER, apply_migrations
from viracase.infrastructure.publication_recovery import recover_publications
from viracase.infrastructure.publication_sql_review import split_sql_statements

MIGRATION = "migrations/0005_add_salt_to_users_and_update_admin.sql"
SQL = "ALTER TABLE users ADD COLUMN salt TEXT NOT NULL DEFAULT 'temp_salt_placeholder';"
CONSENT = "verify_and_close_failed_attempt_when_proven_safe"
ACTION = "check_and_close_if_no_effect"


@pytest.fixture
def clients(system):
    yield from clients_fixture.__wrapped__(system)


@pytest.fixture
def recovery_case(system, monkeypatch, request):
    yield from recovery_case_fixture.__wrapped__(system, monkeypatch, request)


def path(case):
    return f"/api/v1/operations/{case['operation']['id']}/publication-recovery"


def command_body(client, case):
    response = client.get(path(case))
    assert response.status_code == 200, response.text
    return {"expected_state_version": response.json()["state_version"], "action": ACTION, "consent": CONSENT}


def submit(client, headers, case, body, key="developer-recovery"):
    return client.post(path(case) + "/resolve", headers={**headers, "Idempotency-Key": key}, json=body)


def stored(case):
    with case["repo"].sessions() as db:
        operation = db.get(Operation, case["operation"]["id"])
        job = db.scalar(select(Job).where(Job.operation_id == operation.id))
        resource = db.get(PublicationResource, case["project"]["id"])
        return {
            "operation_state": operation.state,
            "error_code": operation.error_code,
            "result": deepcopy(operation.result),
            "job_state": job.state,
            "inventory": deepcopy(resource.inventory),
            "counts": {
                model.__name__: db.scalar(select(func.count()).select_from(model))
                for model in (Operation, Job, Attempt, Reservation)
            },
        }


class DeveloperRemote:
    def __init__(self, files, worker_name):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.calls = []
        self.worker_name = worker_name
        self.before_request = None
        self.fail_migration = True
        self.connection.execute(f"CREATE TABLE {LEDGER}(name TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
        for name, sql in sorted(files.items()):
            if not name.startswith("migrations/") or not name.endswith(".sql") or name == MIGRATION:
                continue
            self.connection.executescript(sql)
            self.connection.execute(
                f"INSERT INTO {LEDGER}(name, sha256) VALUES (?, ?)",
                [name, sha256(sql.encode()).hexdigest()],
            )
        self.connection.commit()

    def database(self, name):
        self.calls.append(("database", name))
        return {"uuid": "test-database", "name": self.worker_name}

    def settings(self, name):
        self.calls.append(("settings", name))

    def request(self, method, url):
        self.calls.append(("request", method, url))
        assert method == "GET"
        if url.endswith("/tokens/verify"):
            return {"status": "active"}
        if url == "/accounts/" + POLICY["account_id"]:
            return {"id": POLICY["account_id"]}
        if url.endswith("/d1/database/test-database"):
            return {"uuid": "test-database", "name": self.worker_name}
        raise AssertionError(url)

    def query(self, database_id, sql, params=None):
        assert database_id == "test-database"
        self.calls.append(("query", sql, params))
        if sql == SQL and self.fail_migration:
            raise PublicationUncertain()
        if sql.startswith("SELECT") or params:
            cursor = self.connection.execute(sql, params or [])
            rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
        else:
            self.connection.executescript(sql)
            rows = []
        self.connection.commit()
        return [{"success": True, "results": rows} for _ in split_sql_statements(sql)]


@pytest.fixture
def developer_case(system, monkeypatch):
    registry, service, actor, project = publication_fixture.__wrapped__(system, monkeypatch)
    repo = system["repo"]
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    files["migrations/0002_users.sql"] = "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
    files[MIGRATION] = SQL
    project = repo.save_revision(
        actor,
        project["id"],
        "developer-recovery-schema",
        {"base_revision_id": project["current_revision_id"], "files": files},
        project["version"],
    )["project"]
    qualify(system, registry)
    operation = service.start(
        actor, project["id"], "uncertain-developer-publication", {"revision_id": project["current_revision_id"]}
    )
    with repo.sessions() as db:
        worker_name = db.get(PublicationResource, project["id"]).worker_name
    remote = DeveloperRemote(files, worker_name)
    remote.connection.execute("INSERT INTO users(id, name) VALUES (1, 'preserved')")
    remote.connection.commit()
    inventory = {
        "database_id": "test-database",
        "database_creation_started": True,
        "migrations": {
            name: sha256(sql.encode()).hexdigest()
            for name, sql in files.items()
            if name.startswith("migrations/") and name != MIGRATION
        },
    }

    def checkpoint(stage, changes=None, external=None):
        updates = deepcopy(changes or {})
        pending = updates.get("migration_in_progress")
        if isinstance(pending, dict):
            pending.update(operation_id=operation["id"], revision_id=project["current_revision_id"])
        inventory.update(updates)
        return {"inventory": deepcopy(inventory)}

    with pytest.raises(PublicationUncertain):
        apply_migrations(
            remote,
            "test-database",
            {name: sql for name, sql in files.items() if name.startswith("migrations/")},
            inventory,
            checkpoint,
            revision_id=project["current_revision_id"],
            source_files=files,
        )
    with repo.sessions.begin() as db:
        row = db.get(Operation, operation["id"])
        row.state, row.error_code, row.progress_stage = "reconciling", "PUBLICATION_REJECTED", "publication_reconciling"
        row.result = {
            "external_effect_pending": True,
            "publication_attempt": {
                "version": 1,
                "revision_id": project["current_revision_id"],
                "stage": "publication_migrating",
                "upload_started": False,
            },
            "publication_failure": {
                "code": "PUBLICATION_REJECTED",
                "message": "A requisição D1 não foi confirmada.",
                "stage": "publication_migrating",
                "at": utcnow().isoformat(),
                "provider": {"http_status": 400, "code": 7500},
            },
            "publication_recovery": {
                "version": 2,
                "state": "attention",
                "attempts": 1,
                "last_error_code": "MIGRATION_RESULT_UNKNOWN",
                "last_error_message": "O recibo não foi localizado.",
                "last_checked_at": utcnow().isoformat(),
            },
        }
        job = db.scalar(select(Job).where(Job.operation_id == row.id))
        job.state, job.lease_token, job.lease_until = "reconciling", None, None
        resource = db.get(PublicationResource, project["id"])
        resource.database_id, resource.inventory = "test-database", inventory

    def proxy(name, method):
        def call(gateway, *args, **kwargs):
            if gateway.before_request:
                gateway.before_request(method)
            return getattr(remote, name)(*args, **kwargs)
        return call

    for name, method in (("database", "GET"), ("settings", "GET"), ("query", "POST"), ("request", "GET")):
        monkeypatch.setattr(CloudflareGateway, name, proxy(name, method))
    monkeypatch.setattr("viracase.infrastructure.publication.WorkerBuildStatus", lambda repo: Builder())
    remote.calls.clear()
    yield {"repo": repo, "service": service, "actor": actor, "project": project, "operation": operation, "remote": remote}
    remote.connection.close()


def test_public_snapshot_is_read_only_and_does_not_authorize_closure(recovery_case, clients):
    client, _ = clients["owner"]
    before = stored(recovery_case)
    first = client.get(path(recovery_case))
    second = client.get(path(recovery_case))
    assert first.status_code == second.status_code == 200
    assert first.json()["state_version"] == second.json()["state_version"]
    assert first.json()["capabilities"]["can_check_and_resolve"] is True
    assert first.json()["capabilities"]["can_close_now"] is False
    assert stored(recovery_case) == before
    assert recovery_case["remote"].calls == []


@pytest.mark.parametrize("subject,status", [("other", 404), ("anonymous", 401), ("admin", 404)])
def test_public_resolve_rejects_foreign_or_nonpublic_identity(recovery_case, clients, subject, status):
    owner, owner_headers = clients["owner"]
    body = command_body(owner, recovery_case)
    client, headers = clients[subject]
    if subject == "anonymous":
        headers = {"Origin": owner_headers["Origin"]}
    before = stored(recovery_case)
    response = submit(client, headers, recovery_case, body)
    assert response.status_code == status, response.text
    assert recovery_case["operation"]["id"] not in response.text
    assert MIGRATION not in response.text
    assert stored(recovery_case) == before
    assert recovery_case["remote"].calls == []


@pytest.mark.parametrize("csrf", [None, "invalid-csrf-token"])
def test_public_resolve_requires_matching_csrf(recovery_case, clients, csrf):
    client, headers = clients["owner"]
    body = command_body(client, recovery_case)
    altered = {key: value for key, value in headers.items() if key != "X-CSRF-Token"}
    if csrf is not None:
        altered["X-CSRF-Token"] = csrf
    response = submit(client, altered, recovery_case, body)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_DENIED"
    assert stored(recovery_case)["operation_state"] == "reconciling"


@pytest.mark.parametrize("alteration", [
    {"action": "adopt_schema"},
    {"action": "record_receipt"},
    {"action": "close_no_effect"},
    {"consent": ""},
    {"consent": "operator_confirmed"},
    {"expected_state_version": "invalid"},
    {"attestation": {"reference": "forged", "outcome": "no_effect"}},
    {"baseline": {"safe": True}},
])
def test_public_resolve_rejects_expanded_or_forged_contract(recovery_case, clients, alteration):
    client, headers = clients["owner"]
    body = {**command_body(client, recovery_case), **alteration}
    before = stored(recovery_case)
    response = submit(client, headers, recovery_case, body)
    assert response.status_code == 400, response.text
    assert stored(recovery_case) == before
    assert recovery_case["remote"].calls == []


def test_public_resolve_requires_intention_key(recovery_case, clients):
    client, headers = clients["owner"]
    response = client.post(path(recovery_case) + "/resolve", headers=headers, json=command_body(client, recovery_case))
    assert response.status_code == 400, response.text
    assert recovery_case["remote"].calls == []


def test_public_resolve_rejects_role_without_recovery_capability_even_if_publish_is_allowed(recovery_case, clients):
    client, headers = clients["owner"]
    body = command_body(client, recovery_case)
    with recovery_case["repo"].sessions.begin() as db:
        account = db.get(Account, recovery_case["actor"])
        account.role = "admin"
        assert "publish" in recovery_case["repo"].access(db, account)["allowed_actions"]
    response = submit(client, headers, recovery_case, body)
    assert response.status_code == 403, response.text
    snapshot = client.get(path(recovery_case))
    assert snapshot.status_code == 200
    assert snapshot.json()["capabilities"]["can_check_and_resolve"] is False
    assert recovery_case["remote"].calls == []


@pytest.mark.parametrize("role", ["admin", "viewer", "collaborator", "manager", "", None])
def test_role_mapping_denies_unknown_and_administrative_roles(role):
    with pytest.raises(RuleError) as caught:
        authorize_publication_recovery("owner", role, "owner", "owner")
    assert caught.value.code == "ACCESS_DENIED"


def test_public_api_does_not_expose_administrative_resolution_routes(recovery_case, clients):
    client, headers = clients["owner"]
    for suffix in ("recovery-plan", "resolve-recovery"):
        response = client.post(
            f"/api/v1/publications/{recovery_case['operation']['id']}/{suffix}",
            headers={**headers, "Idempotency-Key": suffix},
            json={"action": "adopt_schema"},
        )
        assert response.status_code == 404


def test_public_command_is_durable_idempotent_and_readable_after_reload(recovery_case, clients, system):
    client, headers = clients["owner"]
    body = command_body(client, recovery_case)
    before = stored(recovery_case)
    first = submit(client, headers, recovery_case, body)
    assert first.status_code == 202, first.text
    duplicate = submit(client, headers, recovery_case, body)
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["recovery_request_id"] == first.json()["recovery_request_id"]
    assert stored(recovery_case)["counts"] == before["counts"]
    assert recovery_case["remote"].calls == []
    changed = submit(client, headers, recovery_case, {**body, "expected_state_version": "0" * 64})
    assert changed.status_code == 409, changed.text
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with ExitStack() as stack:
        reloaded, _ = authenticated_client(system, stack, "student0")
        snapshot = reloaded.get(path(recovery_case))
        assert snapshot.status_code == 200
        assert snapshot.json()["recovery_request_id"] == first.json()["recovery_request_id"]
        assert snapshot.json()["capabilities"]["can_check_and_resolve"] is False
    assert stored(recovery_case)["operation_state"] == "reconciling"


def test_cached_intention_does_not_bypass_revoked_access(recovery_case, clients):
    client, headers = clients["owner"]
    body = command_body(client, recovery_case)
    accepted = submit(client, headers, recovery_case, body)
    assert accepted.status_code == 202, accepted.text
    with recovery_case["repo"].sessions.begin() as db:
        db.get(Account, recovery_case["actor"]).suspended = True
    response = submit(client, headers, recovery_case, body)
    assert response.status_code == 403, response.text
    assert accepted.json()["recovery_request_id"] not in response.text


def test_legacy_check_and_polling_do_not_supply_resolution_consent(developer_case, clients):
    client, headers = clients["owner"]
    before = stored(developer_case)
    response = client.post(path(developer_case) + "/check", headers={**headers, "Idempotency-Key": "legacy-check"}, json={})
    assert response.status_code == 202, response.text
    assert recover_publications(developer_case["repo"], operation_id=developer_case["operation"]["id"])
    for _ in range(3):
        snapshot = client.get(path(developer_case))
        assert snapshot.status_code == 200
        assert snapshot.json()["recovery_state"] == "attention"
    after = stored(developer_case)
    assert after["operation_state"] == after["job_state"] == "reconciling"
    assert after["inventory"]["migration_in_progress"] == before["inventory"]["migration_in_progress"]
    assert all(call[1].startswith("SELECT") for call in developer_case["remote"].calls if call[0] == "query")


def test_public_owner_resolves_proven_single_column_without_sql_replay_or_ai(developer_case, clients):
    case = developer_case
    client, headers = clients["owner"]
    before, wallet = stored(case), case["repo"].wallet(case["actor"], True)
    body = command_body(client, case)
    accepted = submit(client, headers, case, body)
    assert accepted.status_code == 202, accepted.text
    assert recover_publications(case["repo"], operation_id=case["operation"]["id"]) == case["operation"]["id"]
    response = client.get(path(case))
    assert response.status_code == 200, response.text
    snapshot = response.json()
    after = stored(case)
    assert snapshot["recovery_state"] == "resolved", snapshot
    assert after["operation_state"] == "failed"
    assert after["job_state"] == "done"
    assert after["result"]["external_effect_pending"] is False
    assert after["result"]["publication_failure"] == before["result"]["publication_failure"]
    assert after["inventory"]["migrations"] == before["inventory"]["migrations"]
    assert after["inventory"].get("migration_in_progress") is None
    assert after["inventory"]["migration_last_attempt"]["name"] == MIGRATION
    assert after["counts"] == before["counts"]
    assert case["repo"].wallet(case["actor"], True) == wallet
    assert case["remote"].connection.execute("SELECT name FROM users WHERE id = 1").fetchone()[0] == "preserved"
    assert "salt" not in [row[1] for row in case["remote"].connection.execute("PRAGMA table_info(users)")]
    assert case["remote"].connection.execute(f"SELECT COUNT(*) FROM {LEDGER} WHERE name = ?", [MIGRATION]).fetchone()[0] == 0
    assert all(call[1].startswith("SELECT") for call in case["remote"].calls if call[0] == "query")
    for secret in ("synthetic-cloudflare-token", "temp_salt_placeholder", SQL):
        assert secret not in response.text
    assert not snapshot.get("published")
    assert snapshot["readiness"]["can_publish"] is True
    generic = client.get(f"/api/v1/operations/{case['operation']['id']}")
    assert generic.status_code == 200
    for private in ("publication_developer_recovery", "intention_id", "configuration_sha256", CONSENT):
        assert private not in generic.text
    with case["repo"].sessions() as db:
        audits = db.scalars(select(Audit).where(Audit.resource_id == case["operation"]["id"])).all()
        commits = [entry for entry in audits if entry.action == "publication:developer_recovery:committed"]
        assert len(commits) == 1
        details = json.loads(commits[0].reason)
        assert details["actor_id"] == case["actor"]
        assert details["action"] == "close_no_effect"
        assert details["plan_hash"] and details["evidence_hash"] and details["policy_version"]
        assert details["state_after"] == "failed"
    duplicate = submit(client, headers, case, body)
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["recovery_request_id"] == accepted.json()["recovery_request_id"]
    assert duplicate.json()["recovery_state"] == "resolved"
    assert recover_publications(case["repo"], operation_id=case["operation"]["id"]) is None


def test_missing_execution_unit_is_a_concrete_pending_result_not_false_success(developer_case, clients):
    case = developer_case
    with case["repo"].sessions.begin() as db:
        resource = db.get(PublicationResource, case["project"]["id"])
        inventory = deepcopy(resource.inventory)
        inventory["migration_in_progress"].pop("execution_unit", None)
        resource.inventory = inventory
    client, headers = clients["owner"]
    before = stored(case)
    accepted = submit(client, headers, case, command_body(client, case))
    assert accepted.status_code == 202, accepted.text
    assert recover_publications(case["repo"], operation_id=case["operation"]["id"])
    response = client.get(path(case))
    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["recovery_state"] == "attention"
    assert snapshot["resolution"]["reason_code"] == "PROOF_INCOMPLETE", snapshot
    assert snapshot["resolution"]["missing_evidence"]
    after = stored(case)
    assert after["operation_state"] == after["job_state"] == "reconciling"
    assert after["inventory"] == before["inventory"]
    assert all(call[1].startswith("SELECT") for call in case["remote"].calls if call[0] == "query")


def test_recovery_preserves_previous_link_and_requires_separate_current_revision_publication(developer_case, clients):
    case = developer_case
    client, headers = clients["owner"]
    repo, project = case["repo"], case["project"]
    old_revision = project["current_revision_id"]
    files = repo.source_files(case["actor"], project["id"], old_revision)
    files["public/index.html"] += "\n<div>Revisão atual independente da tentativa antiga</div>"
    current = repo.save_revision(
        case["actor"], project["id"], "current-revision-after-pending",
        {"base_revision_id": old_revision, "files": files}, project["version"],
    )["project"]
    previous_link = {
        "url": "https://previous-publication.synthetic-test.workers.dev/",
        "published_revision_id": old_revision,
        "published_at": (utcnow() - timedelta(days=1)).isoformat(),
    }
    with repo.sessions.begin() as db:
        resource = db.get(PublicationResource, project["id"])
        resource.inventory = {**resource.inventory, **previous_link}
    readiness_path = f"/api/v1/projects/{project['id']}/publication-readiness"
    blocked = client.get(readiness_path).json()
    assert blocked["can_publish"] is False
    assert blocked["revision_id"] == current["current_revision_id"]
    assert "OPERATION_CONFLICT" in [item["code"] for item in blocked["reasons"]]
    accepted = submit(client, headers, case, command_body(client, case))
    assert accepted.status_code == 202, accepted.text
    assert recover_publications(repo, operation_id=case["operation"]["id"])
    snapshot = client.get(path(case)).json()
    assert snapshot["recovery_state"] == "resolved"
    assert snapshot["revision_id"] == old_revision
    assert snapshot["readiness"]["revision_id"] == current["current_revision_id"]
    assert snapshot["readiness"]["publication"]["url"] == previous_link["url"]
    assert snapshot["readiness"]["publication"]["revision_id"] == old_revision
    with repo.sessions() as db:
        operations = db.scalars(select(Operation).where(Operation.project_id == project["id"], Operation.type == "publication")).all()
        assert len(operations) == 1 and operations[0].state == "failed"
    sent = client.post(
        f"/api/v1/projects/{project['id']}/publications",
        headers={**headers, "Idempotency-Key": "explicit-publish-current-revision"},
        json={"revision_id": current["current_revision_id"]},
    )
    assert sent.status_code == 202, sent.text
    assert sent.json()["id"] != case["operation"]["id"]
    with repo.sessions() as db:
        operation = db.get(Operation, sent.json()["id"])
        assert operation.state == "queued"
        assert operation.payload["revision_id"] == current["current_revision_id"]
        assert db.get(Operation, case["operation"]["id"]).state == "failed"
        inventory = db.get(PublicationResource, project["id"]).inventory
        assert all(inventory[key] == value for key, value in previous_link.items())
    assert all(call[1].startswith("SELECT") for call in case["remote"].calls if call[0] == "query")


def test_safe_recovery_does_not_hide_independent_readiness_block(developer_case, clients, monkeypatch):
    case = developer_case
    client, headers = clients["owner"]
    accepted = submit(client, headers, case, command_body(client, case))
    assert accepted.status_code == 202
    monkeypatch.setattr(Builder, "available", lambda self: False)
    assert recover_publications(case["repo"], operation_id=case["operation"]["id"])
    snapshot = client.get(path(case)).json()
    assert snapshot["recovery_state"] == "resolved"
    assert snapshot["readiness"]["can_publish"] is False
    assert "EXECUTOR_UNAVAILABLE" in [item["code"] for item in snapshot["readiness"]["reasons"]]
    assert stored(case)["operation_state"] == "failed"


def test_transient_provider_failure_stays_pending_and_enforces_cooldown(developer_case, clients, monkeypatch):
    case = developer_case
    client, headers = clients["owner"]
    before = stored(case)
    original_query = case["remote"].query

    def unavailable(database_id, sql, params=None):
        raise PublicationUncertain()

    monkeypatch.setattr(case["remote"], "query", unavailable)
    accepted = submit(client, headers, case, command_body(client, case))
    assert accepted.status_code == 202, accepted.text
    assert recover_publications(case["repo"], operation_id=case["operation"]["id"])
    snapshot = client.get(path(case)).json()
    assert snapshot["resolution"]["reason_code"] == "REMOTE_UNAVAILABLE"
    assert snapshot["resolution"]["cooldown_until"]
    assert snapshot["capabilities"]["can_check_and_resolve"] is False
    duplicate = submit(client, headers, case, command_body(client, case), "retry-too-soon")
    assert duplicate.status_code == 429, duplicate.text
    assert stored(case)["inventory"] == before["inventory"]
    with case["repo"].sessions.begin() as db:
        op = db.get(Operation, case["operation"]["id"])
        request = {**op.result["publication_developer_recovery"], "requested_at": (utcnow() - timedelta(minutes=2)).isoformat()}
        op.result = {**op.result, "publication_developer_recovery": request}
    monkeypatch.setattr(case["remote"], "query", original_query)
    retry = submit(client, headers, case, command_body(client, case), "retry-after-cooldown")
    assert retry.status_code == 202, retry.text
    assert retry.json()["recovery_request_id"] != accepted.json()["recovery_request_id"]
    assert recover_publications(case["repo"], operation_id=case["operation"]["id"])
    assert client.get(path(case)).json()["recovery_state"] == "resolved"


@pytest.mark.parametrize("same_key", [True, False])
def test_postgres_concurrent_public_commands_create_one_authorized_request(developer_case, clients, same_key):
    case = developer_case
    if case["repo"].sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Exige PostgreSQL real para validar exclusão mútua de comandos concorrentes.")
    client, headers = clients["owner"]
    body = command_body(client, case)
    barrier = Barrier(2)

    def send(index):
        barrier.wait(timeout=10)
        return submit(client, headers, case, body, "shared-key" if same_key else f"parallel-{index}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send, index) for index in range(2)]
        responses = [future.result(timeout=30) for future in futures]
    accepted = [response for response in responses if response.status_code == 202]
    assert len(accepted) == (2 if same_key else 1), [response.text for response in responses]
    assert len({response.json()["recovery_request_id"] for response in accepted}) == 1
    assert all(response.status_code in {202, 409, 412} for response in responses)
    assert case["remote"].calls == []
    with case["repo"].sessions() as db:
        requests = db.scalar(select(func.count()).select_from(Audit).where(
            Audit.resource_id == case["operation"]["id"],
            Audit.action == "publication:developer_recovery:requested",
        ))
        assert requests == 1


def test_postgres_competing_worker_cannot_observe_or_finalize_held_scope(developer_case, clients, monkeypatch):
    case = developer_case
    if case["repo"].sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Exige PostgreSQL real para validar o lease durante observação remota.")
    client, headers = clients["owner"]
    accepted = submit(client, headers, case, command_body(client, case))
    assert accepted.status_code == 202
    observation, release = Event(), Event()
    original_query = case["remote"].query

    def pause_observation(database_id, sql, params=None):
        if not observation.is_set():
            observation.set()
            assert release.wait(timeout=15)
        return original_query(database_id, sql, params)

    monkeypatch.setattr(case["remote"], "query", pause_observation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(recover_publications, case["repo"], operation_id=case["operation"]["id"])
        try:
            assert observation.wait(timeout=10)
            calls = len(case["remote"].calls)
            competitor = pool.submit(recover_publications, case["repo"], operation_id=case["operation"]["id"])
            assert competitor.result(timeout=10) is None
            assert len(case["remote"].calls) == calls
            assert stored(case)["operation_state"] == "reconciling"
        finally:
            release.set()
        assert first.result(timeout=30) == case["operation"]["id"]
    assert client.get(path(case)).json()["recovery_state"] == "resolved"
    with case["repo"].sessions() as db:
        commits = db.scalar(select(func.count()).select_from(Audit).where(
            Audit.resource_id == case["operation"]["id"],
            Audit.action == "publication:developer_recovery:committed",
        ))
        assert commits == 1
    assert all(call[1].startswith("SELECT") for call in case["remote"].calls if call[0] == "query")
