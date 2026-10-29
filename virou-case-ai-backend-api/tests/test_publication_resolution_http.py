import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_publication_pipeline import qualify
from test_publication_pipeline import publication as publication_fixture

from viracase.domain.rules import utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway
from viracase.infrastructure.models import (
    Account,
    Attempt,
    IntegrationConfig,
    Job,
    Operation,
    PublicationResource,
    Reservation,
)
from viracase.infrastructure.publication_migrations import LEDGER
from viracase.infrastructure.publication_sql_review import split_sql_statements
from viracase.interfaces.api import create_app


MIGRATION = "migrations/0002.sql"
SQL = "CREATE TABLE recovery_notes(id INTEGER PRIMARY KEY, note TEXT);"


class RecoveryRemote:
    def __init__(self, files, applied=True):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.calls = []
        self.connection.execute(f"CREATE TABLE {LEDGER}(name TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
        for path, sql in sorted(files.items()):
            if not path.startswith("migrations/") or not path.endswith(".sql"):
                continue
            if path == MIGRATION and not applied:
                continue
            self.connection.executescript(sql)
            if path != MIGRATION:
                self.connection.execute(
                    f"INSERT INTO {LEDGER}(name, sha256) VALUES (?, ?)",
                    [path, sha256(sql.encode()).hexdigest()],
                )
        self.connection.commit()

    def database(self, name):
        self.calls.append(("database", name))
        return {"uuid": "test-database", "name": name}

    def settings(self, name):
        self.calls.append(("settings", name))
        return None

    def query(self, database_id, sql, params=None):
        assert database_id == "test-database"
        self.calls.append(("query", sql, params))
        cursor = self.connection.execute(sql, params or [])
        rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
        self.connection.commit()
        return [{"success": True, "results": rows} for _ in split_sql_statements(sql)]

    def receipt_count(self):
        return self.connection.execute(
            f"SELECT COUNT(*) FROM {LEDGER} WHERE name = ?", [MIGRATION]
        ).fetchone()[0]


@pytest.fixture
def recovery_case(system, monkeypatch, request):
    registry, service, actor, project = publication_fixture.__wrapped__(system, monkeypatch)
    repo = system["repo"]
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    files[MIGRATION] = SQL
    saved = repo.save_revision(
        actor,
        project["id"],
        "recovery-schema",
        {"base_revision_id": project["current_revision_id"], "files": files},
        project["version"],
    )
    project = saved["project"]
    qualify(system, registry)
    operation = service.start(
        actor, project["id"], "legacy-publication", {"revision_id": project["current_revision_id"]}
    )
    with repo.sessions.begin() as db:
        row = db.get(Operation, operation["id"])
        row.state, row.error_code, row.progress_stage = (
            "reconciling",
            "PUBLICATION_REJECTED",
            "publication_reconciling",
        )
        row.result = {
            "external_effect_pending": True,
            "publication_attempt": {
                "version": 1,
                "revision_id": project["current_revision_id"],
                "upload_started": False,
                "stage": "publication_migrating",
            },
            "publication_failure": {
                "code": "PUBLICATION_REJECTED",
                "message": "A requisição D1 não foi confirmada.",
                "stage": "publication_migrating",
                "at": utcnow().isoformat(),
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
        resource.database_id = "test-database"
        resource.inventory = {
            "database_id": "test-database",
            "database_creation_started": True,
            "migrations": {
                path: sha256(content.encode()).hexdigest()
                for path, content in files.items()
                if path.startswith("migrations/") and path != MIGRATION
            },
            "migration_in_progress": {"name": MIGRATION, "sha256": sha256(SQL.encode()).hexdigest()},
        }
    remote = RecoveryRemote(files, applied=getattr(request, "param", True))

    def proxy(name, method):
        def call(gateway, *args, **kwargs):
            if gateway.before_request:
                gateway.before_request(method)
            return getattr(remote, name)(*args, **kwargs)

        return call

    for name, method in (("database", "GET"), ("settings", "GET"), ("query", "POST")):
        monkeypatch.setattr(CloudflareGateway, name, proxy(name, method))
    yield {
        "repo": repo,
        "service": service,
        "actor": actor,
        "project": project,
        "operation": operation,
        "remote": remote,
    }
    remote.connection.close()


def authenticated_client(system, stack, name, *, admin=False):
    dependencies = (system["config"], system["engine"], system["repo"], system["catalog"], None)
    client = stack.enter_context(TestClient(create_app(admin, dependencies)))
    prefix = "/admin-api/v1" if admin else "/api/v1"
    origin = system["config"].admin_origin if admin else system["config"].public_origin
    login = client.post(
        prefix + "/auth/login",
        headers={"Origin": origin},
        json={"login": name, "password": "test-operator-password" if admin else "test-student-password"},
    )
    assert login.status_code == 200, login.text
    return client, {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}


@pytest.fixture
def clients(system):
    with ExitStack() as stack:
        owner = authenticated_client(system, stack, "student0")
        other = authenticated_client(system, stack, "student1")
        admin = authenticated_client(system, stack, "operator", admin=True)
        dependencies = (system["config"], system["engine"], system["repo"], system["catalog"], None)
        anonymous = stack.enter_context(TestClient(create_app(False, dependencies)))
        anonymous_admin = stack.enter_context(TestClient(create_app(True, dependencies)))
        yield {
            "owner": owner,
            "other": other,
            "admin": admin,
            "anonymous": (anonymous, {}),
            "anonymous_admin": (anonymous_admin, {}),
        }


def owner_path(case):
    return f"/api/v1/operations/{case['operation']['id']}/publication-recovery"


def admin_path(case, action="recovery"):
    return f"/admin-api/v1/publications/{case['operation']['id']}/{action}"


def write(client, headers, path, payload, key):
    return client.post(path, headers={**headers, "Idempotency-Key": key}, json=payload)


def counts(case):
    with case["repo"].sessions() as db:
        return {
            model.__name__: db.scalar(select(func.count()).select_from(model))
            for model in (Operation, Job, Attempt, Reservation)
        }


def stored_resolution(case):
    with case["repo"].sessions() as db:
        return deepcopy(db.get(Operation, case["operation"]["id"]).result["publication_resolution"])


def test_owner_diagnostic_is_read_only_sanitized_and_account_scoped(recovery_case, clients):
    case = recovery_case
    repo, oid = case["repo"], case["operation"]["id"]
    with repo.sessions.begin() as db:
        row = db.get(Operation, oid)
        row.result = {**row.result, "untrusted_dump": {"token": "private-diagnostic-token", "sql": SQL}}
        resource = db.get(PublicationResource, case["project"]["id"])
        resource.inventory = {
            **resource.inventory,
            "migration_in_progress": {
                **resource.inventory["migration_in_progress"],
                "sql": SQL,
                "secret": "private-marker-token",
            },
        }
    before, wallet = counts(case), repo.wallet(case["actor"], True)
    owner, _ = clients["owner"]
    response = owner.get(owner_path(case))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["format_version"] == 1 and data["operation_id"] == oid
    assert data["migration"]["name"] == MIGRATION
    assert data["migration"]["sha256"] == sha256(SQL.encode()).hexdigest()
    for secret in ("private-diagnostic-token", "private-marker-token", "synthetic-cloudflare-token", SQL):
        assert secret not in response.text
    assert clients["other"][0].get(owner_path(case)).status_code == 404
    assert clients["anonymous"][0].get(owner_path(case)).status_code == 401
    assert counts(case) == before and repo.wallet(case["actor"], True) == wallet
    assert case["remote"].calls == []


def test_owner_diagnostic_does_not_reintroduce_foreign_migration_from_rejected_assessment(
    recovery_case, clients
):
    from viracase.infrastructure.publication_recovery_evidence import assess_migration_recovery

    case = recovery_case
    repo, oid = case["repo"], case["operation"]["id"]
    revision = case["project"]["current_revision_id"]
    with repo.sessions.begin() as db:
        other = Operation(
            account_id=case["actor"],
            project_id=case["project"]["id"],
            type="publication",
            state="failed",
            policy={},
            payload={"revision_id": revision},
            result={},
        )
        db.add(other)
        db.flush()
        foreign_operation_id = other.id
        resource = db.get(PublicationResource, case["project"]["id"])
        resource.inventory = {
            **resource.inventory,
            "migration_in_progress": {
                **resource.inventory["migration_in_progress"],
                "operation_id": foreign_operation_id,
                "revision_id": revision,
            },
        }
        assessment = assess_migration_recovery(
            None,
            resource.database_id,
            {},
            resource.inventory,
            operation_id=oid,
            revision_id=revision,
        )
        assert assessment["classification"] == "conflict"
        assert assessment["missing_evidence"] == ["operation_identity"]
        assert assessment["identity"]["operation_id"] == oid
        assert assessment["identity"]["migration"]["name"] == MIGRATION
        assert "identity" not in assessment["checks"]
        operation = db.get(Operation, oid)
        operation.result = {**operation.result, "publication_recovery_assessment": assessment}
        saved_result = deepcopy(operation.result)
        saved_inventory = deepcopy(resource.inventory)
    before, wallet = counts(case), repo.wallet(case["actor"], True)
    owner, _ = clients["owner"]
    response = owner.get(owner_path(case))
    assert response.status_code == 200, response.text
    assert response.json()["classification"] == "conflict"
    assert response.json()["migration"] is None
    assert MIGRATION not in response.text and foreign_operation_id not in response.text
    assert case["remote"].calls == []
    assert counts(case) == before and repo.wallet(case["actor"], True) == wallet
    with repo.sessions() as db:
        assert db.get(Operation, oid).result == saved_result
        assert db.get(PublicationResource, case["project"]["id"]).inventory == saved_inventory


@pytest.mark.parametrize("violation", ["csrf", "origin", "extra", "other_owner"])
def test_owner_check_rejects_unauthorized_or_extended_commands_without_side_effects(
    recovery_case, clients, violation
):
    case = recovery_case
    client, headers = clients["other" if violation == "other_owner" else "owner"]
    headers = dict(headers)
    payload = {}
    if violation == "csrf":
        headers.pop("X-CSRF-Token")
    elif violation == "origin":
        headers["Origin"] = "https://untrusted.example"
    elif violation == "extra":
        payload = {"force": True, "sql": SQL, "confirmed_no_effect": True}
    before = counts(case)
    result = write(client, headers, owner_path(case) + "/check", payload, "invalid-owner-check")
    assert result.status_code == {"csrf": 403, "origin": 403, "extra": 400, "other_owner": 404}[violation]
    assert (
        case["repo"].operation(case["actor"], case["operation"]["id"])["result_ref"]["publication_recovery"][
            "state"
        ]
        == "attention"
    )
    assert counts(case) == before and case["remote"].calls == []


def test_owner_check_schedules_the_same_operation_once_without_ai_or_wallet_effect(recovery_case, clients):
    case = recovery_case
    client, headers = clients["owner"]
    before, wallet = counts(case), case["repo"].wallet(case["actor"], True)
    path = owner_path(case) + "/check"
    first = write(client, headers, path, {}, "owner-check")
    assert first.status_code in {200, 202}, first.text
    repeated = write(client, headers, path, {}, "owner-check")
    assert repeated.status_code == first.status_code and repeated.json() == first.json()
    assert first.json()["operation_id"] == case["operation"]["id"]
    assert first.json()["requested_at"] and first.json()["cooldown_until"]
    assert not first.json()["can_check"]
    too_soon = write(client, headers, path, {}, "owner-check-too-soon")
    assert too_soon.status_code in {409, 429}, too_soon.text
    assert counts(case) == before and case["repo"].wallet(case["actor"], True) == wallet
    assert case["remote"].calls == []
    with case["repo"].sessions() as db:
        job = db.scalar(select(Job).where(Job.operation_id == case["operation"]["id"]))
        operation = db.get(Operation, case["operation"]["id"])
        assert job.state == operation.state == "reconciling"
        assert job.lease_token is None
        assert operation.result["publication_recovery"]["state"] == "scheduled"


@pytest.mark.parametrize("blocked", ["lease", "suspended", "maintenance"])
def test_owner_check_revalidates_current_access_and_lease(recovery_case, clients, system, blocked):
    case = recovery_case
    with case["repo"].sessions.begin() as db:
        if blocked == "lease":
            job = db.scalar(select(Job).where(Job.operation_id == case["operation"]["id"]))
            job.lease_token, job.lease_until = "active-conference", utcnow() + timedelta(minutes=5)
        elif blocked == "suspended":
            db.get(Account, case["actor"]).suspended = True
    if blocked == "maintenance":
        system["config"].restore_hold = True
    client, headers = clients["owner"]
    response = write(client, headers, owner_path(case) + "/check", {}, "blocked-owner-check")
    assert response.status_code in {403, 409}, response.text
    assert case["remote"].calls == []
    assert (
        case["repo"].operation(case["actor"], case["operation"]["id"])["result_ref"]["publication_recovery"][
            "state"
        ]
        == "attention"
    )


def prepare_plan(
    case, clients, key="prepare-recovery", reason="Conferir evidência estrutural desta migration legada."
):
    client, headers = clients["admin"]
    response = write(client, headers, admin_path(case, "recovery-plan"), {"reason": reason}, key)
    assert response.status_code in {200, 201}, response.text
    return response.json()


def resolution_payload(plan):
    return {
        "plan_id": plan["plan_id"],
        "plan_sha256": plan["plan_sha256"],
        "action": plan["action"],
        "reason": "Registrar a resolução auditada sem repetir a migration.",
        "attestation": {
            "reference": "synthetic-operator-evidence-reference",
            "evidence_sha256": "e" * 64,
            "statement_indices": [1],
            "observed_outcome": "schema_applied",
        },
    }


def test_admin_recovery_routes_require_admin_audience_and_csrf(recovery_case, clients, system):
    case = recovery_case
    anonymous, _ = clients["anonymous_admin"]
    assert anonymous.get(admin_path(case)).status_code == 401
    anonymous.cookies.set("vira_admin", clients["owner"][0].cookies.get("vira_student"))
    assert anonymous.get(admin_path(case)).status_code == 401
    rejected = write(
        anonymous,
        {"Origin": system["config"].admin_origin},
        admin_path(case, "recovery-plan"),
        {"reason": "Conferir tentativa sem autorização administrativa."},
        "wrong-audience",
    )
    assert rejected.status_code == 401
    client, headers = clients["admin"]
    no_csrf = write(
        client,
        {"Origin": headers["Origin"]},
        admin_path(case, "recovery-plan"),
        {"reason": "Conferir tentativa sem token da sessão administrativa."},
        "missing-admin-csrf",
    )
    assert no_csrf.status_code == 403
    assert case["remote"].calls == []


def test_admin_prepare_and_commit_are_strict_audited_idempotent_and_do_not_publish_old_revision(
    recovery_case, clients
):
    case = recovery_case
    before, wallet = counts(case), case["repo"].wallet(case["actor"], True)
    plan = prepare_plan(case, clients)
    assert plan["can_resolve"] is True and plan["action"] == "adopt_schema", plan
    assert prepare_plan(case, clients) == plan
    assert case["remote"].receipt_count() == 0
    client, headers = clients["admin"]
    payload = resolution_payload(plan)
    malformed = write(
        client, headers, admin_path(case, "resolve-recovery"), {**payload, "sql": SQL}, "invalid-raw-sql"
    )
    assert malformed.status_code == 400 and case["remote"].receipt_count() == 0
    response = write(client, headers, admin_path(case, "resolve-recovery"), payload, "resolve-recovery")
    assert response.status_code == 200, response.text
    repeated = write(client, headers, admin_path(case, "resolve-recovery"), payload, "resolve-recovery")
    assert repeated.status_code == 200 and repeated.json() == response.json()
    current = case["repo"].operation(case["actor"], case["operation"]["id"])
    assert current["state"] == "failed" and current["error_code"] == "PUBLICATION_REJECTED"
    assert current["result_ref"]["publication_recovery"]["resolution"] == "migration_recovered"
    assert case["remote"].receipt_count() == 1
    inserts = [
        call
        for call in case["remote"].calls
        if call[0] == "query" and call[1].startswith(f"INSERT INTO {LEDGER}")
    ]
    assert len(inserts) == 1
    assert not any(call[0] == "query" and call[1] == SQL for call in case["remote"].calls)
    assert counts(case) == before and case["repo"].wallet(case["actor"], True) == wallet
    assert case["service"].readiness(case["actor"], case["project"]["id"])["can_publish"]


@pytest.mark.parametrize("recovery_case", [False], indirect=True)
def test_admin_attested_unchanged_schema_closes_attempt_without_receipt_or_sql(recovery_case, clients):
    case = recovery_case
    plan = prepare_plan(case, clients)
    assert plan["can_resolve"] is True and plan["action"] == "close_no_effect", plan
    payload = resolution_payload(plan)
    payload["attestation"]["observed_outcome"] = "schema_unchanged"
    client, headers = clients["admin"]
    response = write(client, headers, admin_path(case, "resolve-recovery"), payload, "close-unchanged-schema")
    assert response.status_code == 200, response.text
    current = case["repo"].operation(case["actor"], case["operation"]["id"])
    assert current["state"] == "failed"
    assert current["result_ref"]["publication_recovery"]["resolution"] == "migration_recovered"
    assert case["remote"].receipt_count() == 0
    assert not any(call[0] == "query" and call[1] == SQL for call in case["remote"].calls)
    with case["repo"].sessions() as db:
        resource = db.get(PublicationResource, case["project"]["id"])
        assert not resource.inventory.get("migration_in_progress")
        assert MIGRATION not in resource.inventory["migrations"]


@pytest.mark.parametrize(
    "change",
    [
        "plan_hash",
        "action",
        "missing_attestation",
        "statement_indices",
        "remote_schema",
        "inventory",
        "expiry",
    ],
)
def test_admin_commit_rejects_stale_or_incompatible_evidence_without_receipt(
    recovery_case, clients, monkeypatch, change
):
    from viracase.infrastructure import publication_resolution

    case = recovery_case
    plan = prepare_plan(case, clients)
    payload = resolution_payload(plan)
    if change == "plan_hash":
        payload["plan_sha256"] = "0" * 64
    elif change == "action":
        payload["action"] = "close_no_effect"
    elif change == "missing_attestation":
        payload.pop("attestation")
    elif change == "statement_indices":
        payload["attestation"]["statement_indices"] = [2]
    elif change == "remote_schema":
        case["remote"].connection.execute("CREATE TABLE unrelated_external_change(id INTEGER)")
        case["remote"].connection.commit()
    elif change == "inventory":
        with case["repo"].sessions.begin() as db:
            resource = db.get(PublicationResource, case["project"]["id"])
            inventory = deepcopy(resource.inventory)
            inventory["migration_in_progress"]["sha256"] = "0" * 64
            resource.inventory = inventory
    else:
        later = utcnow() + timedelta(minutes=16)
        monkeypatch.setattr(publication_resolution, "utcnow", lambda: later)
    client, headers = clients["admin"]
    result = write(client, headers, admin_path(case, "resolve-recovery"), payload, "reject-recovery")
    assert result.status_code in {400, 403, 409, 410, 412}, result.text
    assert case["remote"].receipt_count() == 0
    assert case["repo"].operation(case["actor"], case["operation"]["id"])["state"] == "reconciling"


def test_admin_plan_intention_cannot_be_reused_for_different_reason(recovery_case, clients):
    case = recovery_case
    plan = prepare_plan(case, clients)
    client, headers = clients["admin"]
    response = write(
        client,
        headers,
        admin_path(case, "recovery-plan"),
        {"reason": "Outra intenção administrativa diferente da original."},
        "prepare-recovery",
    )
    assert response.status_code == 409, response.text
    assert case["remote"].receipt_count() == 0 and plan["can_resolve"] is True


def test_recovery_plan_cannot_be_committed_by_another_administrator(recovery_case, clients, system):
    case = recovery_case
    plan = prepare_plan(case, clients)
    with case["repo"].sessions.begin() as db:
        original = db.get(Account, system["admin"])
        db.add(
            Account(
                login="second-operator",
                name="Segundo operador de teste",
                role="admin",
                password_hash=original.password_hash,
            )
        )
    with ExitStack() as stack:
        client, headers = authenticated_client(system, stack, "second-operator", admin=True)
        result = write(
            client,
            headers,
            admin_path(case, "resolve-recovery"),
            resolution_payload(plan),
            "other-operator-plan",
        )
    assert result.status_code in {403, 404, 409}, result.text
    assert case["remote"].receipt_count() == 0
    assert case["repo"].operation(case["actor"], case["operation"]["id"])["state"] == "reconciling"


def test_concurrent_commits_record_one_receipt_and_never_repeat_application_sql(recovery_case, clients):
    case = recovery_case
    if case["repo"].sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Requer PostgreSQL real para exclusão mútua entre commits.")
    plan = prepare_plan(case, clients)
    payload = resolution_payload(plan)
    client, headers = clients["admin"]
    barrier = Barrier(2)

    def commit(index):
        barrier.wait(timeout=10)
        return write(
            client, headers, admin_path(case, "resolve-recovery"), payload, f"concurrent-resolution-{index}"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(commit, index) for index in range(2)]
        responses = [future.result(timeout=30) for future in futures]
    assert any(response.status_code == 200 for response in responses), [
        response.text for response in responses
    ]
    assert all(response.status_code in {200, 409, 412} for response in responses), [
        response.text for response in responses
    ]
    assert case["remote"].receipt_count() == 1
    assert (
        len(
            [
                call
                for call in case["remote"].calls
                if call[0] == "query" and call[1].startswith(f"INSERT INTO {LEDGER}")
            ]
        )
        == 1
    )
    assert not any(call[0] == "query" and call[1] == SQL for call in case["remote"].calls)


@pytest.mark.parametrize("resume", ["manual", "worker"])
def test_receipt_written_before_timeout_resumes_only_the_same_resolution_intention(
    recovery_case, clients, monkeypatch, resume
):
    from viracase.infrastructure.cloudflare_gateway import PublicationUncertain

    case = recovery_case
    plan = prepare_plan(case, clients)
    payload = resolution_payload(plan)
    remote = case["remote"]
    original_query = remote.query
    interrupted = False

    def interrupted_receipt(database_id, sql, params=None):
        nonlocal interrupted
        result = original_query(database_id, sql, params)
        if sql.startswith(f"INSERT INTO {LEDGER}") and not interrupted:
            interrupted = True
            raise PublicationUncertain()
        return result

    monkeypatch.setattr(remote, "query", interrupted_receipt)
    client, headers = clients["admin"]
    path = admin_path(case, "resolve-recovery")
    failed = write(client, headers, path, payload, "interrupted-resolution")
    assert failed.status_code in {409, 503}, failed.text
    assert remote.receipt_count() == 1
    current = case["repo"].operation(case["actor"], case["operation"]["id"])
    assert current["state"] == "reconciling"
    assert stored_resolution(case)["attempt"]["state"] == "applying"

    changed_key = write(client, headers, path, payload, "another-resolution-intention")
    changed_payload = write(
        client,
        headers,
        path,
        {**payload, "reason": "Uma intenção diferente da que iniciou a resolução."},
        "interrupted-resolution",
    )
    assert changed_key.status_code == changed_payload.status_code == 409
    assert case["repo"].operation(case["actor"], case["operation"]["id"])["state"] == "reconciling"

    if resume == "worker":
        from test_publication_pipeline import Builder
        from viracase.infrastructure.publication_recovery import recover_publications

        owner, owner_headers = clients["owner"]
        scheduled = write(owner, owner_headers, owner_path(case) + "/check", {}, "confirm-written-receipt")
        assert scheduled.status_code in {200, 202}, scheduled.text
        assert (
            recover_publications(case["repo"], executor=Builder(), operation_id=case["operation"]["id"])
            == case["operation"]["id"]
        )
    completed = write(client, headers, path, payload, "interrupted-resolution")
    assert completed.status_code == 200, completed.text
    current = case["repo"].operation(case["actor"], case["operation"]["id"])
    assert current["state"] == "failed" and current["error_code"] == "PUBLICATION_REJECTED"
    attempt = stored_resolution(case)["attempt"]
    assert attempt["state"] == "completed"
    assert attempt["origin"] == "operator_attested_schema_postconditions"
    assert remote.receipt_count() == 1
    assert (
        len(
            [
                call
                for call in remote.calls
                if call[0] == "query" and call[1].startswith(f"INSERT INTO {LEDGER}")
            ]
        )
        == 1
    )
    assert not any(call[0] == "query" and call[1] == SQL for call in remote.calls)


def test_owner_request_after_automatic_limit_runs_one_real_assessment_without_replaying_sql(
    recovery_case, clients
):
    from test_publication_pipeline import Builder
    from viracase.infrastructure.publication_recovery import MAX_ATTEMPTS, recover_publications

    case = recovery_case
    repo, oid = case["repo"], case["operation"]["id"]
    attempts = MAX_ATTEMPTS + 2
    with repo.sessions.begin() as db:
        op = db.get(Operation, oid)
        op.result = {
            **op.result,
            "publication_recovery": {**op.result["publication_recovery"], "attempts": attempts},
        }
    client, headers = clients["owner"]
    response = write(client, headers, owner_path(case) + "/check", {}, "owner-after-automatic-limit")
    assert response.status_code in {200, 202}, response.text
    before, wallet = counts(case), repo.wallet(case["actor"], True)
    assert recover_publications(repo, executor=Builder(), operation_id=oid) == oid
    snapshot = client.get(owner_path(case)).json()
    assert snapshot["classification"] == "schema_applied", snapshot
    assert snapshot["operator_required"] is True
    current = repo.operation(case["actor"], oid)
    recovery = current["result_ref"]["publication_recovery"]
    assert current["state"] == "reconciling" and recovery["state"] == "attention"
    assert recovery["attempts"] == attempts + 1
    assert not recovery.get("requested_reassessment_pending")
    observed = list(case["remote"].calls)
    assert any(call[0] == "query" for call in observed)
    assert not any(
        call[0] == "query" and not call[1].lstrip().upper().startswith("SELECT") for call in observed
    )
    assert recover_publications(repo, executor=Builder(), operation_id=oid) is None
    assert case["remote"].calls == observed and case["remote"].receipt_count() == 0
    assert counts(case) == before and repo.wallet(case["actor"], True) == wallet


def test_schema_change_before_receipt_write_aborts_only_resolution_and_allows_new_plan(
    recovery_case, clients, monkeypatch
):
    from viracase.infrastructure import publication_resolution

    case = recovery_case
    plan = prepare_plan(case, clients)
    remote = case["remote"]
    original_fingerprint = publication_resolution.migration_schema_fingerprint

    def changed_before_write(gateway, database_id):
        remote.connection.execute("CREATE TABLE concurrent_external_change(id INTEGER)")
        remote.connection.commit()
        return original_fingerprint(gateway, database_id)

    monkeypatch.setattr(publication_resolution, "migration_schema_fingerprint", changed_before_write)
    client, headers = clients["admin"]
    response = write(
        client,
        headers,
        admin_path(case, "resolve-recovery"),
        resolution_payload(plan),
        "schema-changed-before-write",
    )
    assert response.status_code == 412, response.text
    assert response.json()["error"]["code"] == "RECOVERY_PLAN_STALE"
    current = case["repo"].operation(case["actor"], case["operation"]["id"])
    attempt = stored_resolution(case)["attempt"]
    assert current["state"] == "reconciling" and attempt["state"] == "aborted"
    assert attempt["receipt_write_started"] is False
    assert remote.receipt_count() == 0
    assert not any(
        call[0] == "query" and not call[1].lstrip().upper().startswith("SELECT") for call in remote.calls
    )
    with case["repo"].sessions() as db:
        resource = db.get(PublicationResource, case["project"]["id"])
        assert resource.inventory["migration_in_progress"] == {
            "name": MIGRATION,
            "sha256": sha256(SQL.encode()).hexdigest(),
        }
        assert MIGRATION not in resource.inventory["migrations"]
        job = db.scalar(select(Job).where(Job.operation_id == case["operation"]["id"]))
        assert job.lease_token is None and job.lease_until is None
    next_plan = prepare_plan(case, clients, key="reassess-after-aborted-resolution")
    assert next_plan["plan_id"] != plan["plan_id"]
    assert next_plan["can_resolve"] is False
    assert next_plan["assessment"]["classification"] == "unknown"
    assert "matching_remote_schema" in next_plan["assessment"]["missing_evidence"]


def interrupt_before_remote_receipt(case, clients, monkeypatch):
    from viracase.infrastructure.cloudflare_gateway import PublicationUncertain

    plan = prepare_plan(case, clients)
    remote = case["remote"]
    original_query = remote.query
    interrupted = False

    def interrupt_once(database_id, sql, params=None):
        nonlocal interrupted
        if sql.startswith(f"INSERT INTO {LEDGER}") and not interrupted:
            interrupted = True
            raise PublicationUncertain()
        return original_query(database_id, sql, params)

    monkeypatch.setattr(remote, "query", interrupt_once)
    client, headers = clients["admin"]
    response = write(
        client,
        headers,
        admin_path(case, "resolve-recovery"),
        resolution_payload(plan),
        "interrupted-before-remote-receipt",
    )
    assert response.status_code in {409, 503}, response.text
    attempt = stored_resolution(case)["attempt"]
    assert attempt["state"] == "applying" and attempt["receipt_write_started"] is True
    assert remote.receipt_count() == 0
    return plan


def test_credential_version_rotation_supersedes_interrupted_intention_with_new_attestation(
    recovery_case, clients, monkeypatch
):
    case = recovery_case
    before, wallet = counts(case), case["repo"].wallet(case["actor"], True)
    plan = interrupt_before_remote_receipt(case, clients, monkeypatch)
    with case["repo"].sessions.begin() as db:
        resource = db.get(PublicationResource, case["project"]["id"])
        db.get(IntegrationConfig, resource.config_id).version += 1
    client, headers = clients["admin"]
    old = write(
        client,
        headers,
        admin_path(case, "resolve-recovery"),
        resolution_payload(plan),
        "interrupted-before-remote-receipt",
    )
    assert old.status_code == 412, old.text
    renewed = prepare_plan(case, clients, key="prepare-after-credential-rotation")
    assert renewed["plan_id"] != plan["plan_id"]
    assert renewed["binding"]["configuration_version"] == plan["binding"]["configuration_version"] + 1
    assert renewed["assessment"]["identity"] == plan["assessment"]["identity"]
    assert renewed["action"] == "adopt_schema" and renewed["can_resolve"]
    payload = resolution_payload(renewed)
    missing = write(
        client,
        headers,
        admin_path(case, "resolve-recovery"),
        {key: value for key, value in payload.items() if key != "attestation"},
        "renewed-plan-without-attestation",
    )
    assert missing.status_code == 400 and case["remote"].receipt_count() == 0
    response = write(
        client, headers, admin_path(case, "resolve-recovery"), payload, "resolve-after-credential-rotation"
    )
    assert response.status_code == 200, response.text
    current = case["repo"].operation(case["actor"], case["operation"]["id"])
    resolution = stored_resolution(case)
    assert current["state"] == "failed" and current["error_code"] == "PUBLICATION_REJECTED"
    assert resolution["attempt"]["state"] == "completed"
    previous = resolution["previous_attempts"][-1]
    assert previous["state"] == "superseded" and previous["plan_id"] == plan["plan_id"]
    assert previous["intention_id"] != resolution["attempt"]["intention_id"]
    assert previous["attestation"] == resolution_payload(plan)["attestation"]
    assert case["remote"].receipt_count() == 1
    writes = [
        call
        for call in case["remote"].calls
        if call[0] == "query" and not call[1].lstrip().upper().startswith("SELECT")
    ]
    assert len(writes) == 1 and writes[0][1].startswith(f"INSERT INTO {LEDGER}")
    assert counts(case) == before and case["repo"].wallet(case["actor"], True) == wallet


@pytest.mark.parametrize("change", ["remote_account", "database", "schema_unchanged"])
def test_interrupted_receipt_write_cannot_supersede_with_other_target_or_absent_schema(
    recovery_case, clients, monkeypatch, change
):
    case = recovery_case
    plan = interrupt_before_remote_receipt(case, clients, monkeypatch)
    with case["repo"].sessions.begin() as db:
        resource = db.get(PublicationResource, case["project"]["id"])
        cfg = db.get(IntegrationConfig, resource.config_id)
        cfg.version += 1
        if change == "remote_account":
            resource.remote_account_id = "b" * 32
            cfg.settings = {**cfg.settings, "account_id": resource.remote_account_id}
        elif change == "database":
            resource.database_id = "different-database"
            resource.inventory = {**resource.inventory, "database_id": resource.database_id}
    remote = case["remote"]
    if change == "database":
        original_query = remote.query

        def other_database_query(database_id, sql, params=None):
            assert database_id == "different-database"
            return original_query("test-database", sql, params)

        monkeypatch.setattr(remote, "database", lambda name: {"uuid": "different-database", "name": name})
        monkeypatch.setattr(remote, "query", other_database_query)
    elif change == "schema_unchanged":
        remote.connection.execute("DROP TABLE recovery_notes")
        remote.connection.commit()
    client, headers = clients["admin"]
    response = write(
        client,
        headers,
        admin_path(case, "recovery-plan"),
        {"reason": "Conferir mudança posterior à gravação incerta do recibo."},
        "unsafe-supersession",
    )
    assert response.status_code in {409, 412}, response.text
    current = case["repo"].operation(case["actor"], case["operation"]["id"])
    resolution = stored_resolution(case)
    assert current["state"] == "reconciling" and resolution["attempt"]["state"] == "applying"
    assert resolution["plan"]["plan_id"] == plan["plan_id"]
    assert not resolution.get("previous_attempts")
    assert remote.receipt_count() == 0
    assert not any(
        call[0] == "query" and not call[1].lstrip().upper().startswith("SELECT") for call in remote.calls
    )
