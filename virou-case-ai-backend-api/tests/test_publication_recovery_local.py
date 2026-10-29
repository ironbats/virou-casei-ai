import importlib.util
from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_publication_resolution_http as recovery_support
from sqlalchemy import delete, func, select
from test_publication_pipeline import Builder, qualify
from test_publication_pipeline import publication as publication_fixture

from viracase.domain.rules import canonical_hash, utcnow
from viracase.infrastructure.models import (
    Account,
    Audit,
    IntegrationConfig,
    Job,
    Operation,
    PublicationResource,
    Session,
    new_id,
)
from viracase.infrastructure.publication import PublicationService
from viracase.infrastructure.publication_recovery import recover_publications
from viracase.infrastructure.publication_resolution import (
    prepare_recovery_plan,
    recovery_snapshot,
    request_recovery_check,
    resolve_recovery_plan,
)
from viracase.infrastructure.publication_worker import execute_publication
from viracase.infrastructure.repository import HASHER
from viracase.infrastructure.worker import Worker

MIGRATION = "migrations/0005_add_salt_to_users_and_update_admin.sql"
INCIDENT_SHA256 = "3045a3bbdd22a916f4c421faef0e14dc5d5b13a31429e0d343afaa8045f06b51"
SALT_SQL = """-- Migration para adicionar a coluna 'salt' à tabela 'users' e atualizar o usuário 'admin'
-- Requer que a migration 0002.sql original já tenha sido aplicada.

-- 1. Adicionar a coluna 'salt' à tabela 'users' com um valor temporário DEFAULT.
-- Isso é necessário porque SQLite não permite ADD COLUMN NOT NULL em tabela com dados sem um DEFAULT.
ALTER TABLE users ADD COLUMN salt TEXT NOT NULL DEFAULT 'temp_salt_placeholder';

-- O DEFAULT 'temp_salt_placeholder' permanecerá na definição da coluna, mas não afetará
-- novas inserções onde o salt é explicitamente fornecido. D1/SQLite não suporta DROP DEFAULT."""
BASELINE_USERS = (
    "CREATE TABLE users(id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, "
    "password_hash TEXT NOT NULL);"
)
GENERIC_SQL = "CREATE TABLE recovery_notes(id INTEGER PRIMARY KEY, note TEXT);"
REASON = "Conferir a baseline e encerrar somente a tentativa legada deste teste."


class SyntheticRemote(recovery_support.RecoveryRemote):
    def __init__(self, files, *, applied=False):
        super().__init__(files, applied=applied)
        self.worker_settings = None
        self.connection.execute(
            "INSERT INTO users(id, email, password_hash) VALUES (?, ?, ?)",
            [1, "synthetic-user@example.test", "synthetic-password-hash"],
        )
        self.connection.commit()

    def settings(self, name):
        self.calls.append(("settings", name))
        return self.worker_settings

    def upload(self, name, database_id, artifact):
        assert database_id == "test-database"
        self.calls.append(("upload", name, artifact["revision_id"]))
        self.worker_settings = {
            "tags": ["viroucaseai", artifact["revision_id"]],
            "bindings": [
                {"type": "d1", "name": "DB", "id": database_id},
                {"type": "assets", "name": "ASSETS"},
            ],
        }
        return "synthetic-local-recovery-deployment"

    def enable(self, name, enabled=True):
        self.calls.append(("enable", name, enabled))

    def verify(self, url, revision):
        assert revision in self.worker_settings["tags"]
        self.calls.append(("verify", url, revision))
        return {"passed": True, "source": "synthetic-test-double"}


class StubGateway:
    def __init__(self, remote):
        self.remote = remote
        self.before_request = None

    def _call(self, name, method, *args, **kwargs):
        if self.before_request:
            self.before_request(method)
        return getattr(self.remote, name)(*args, **kwargs)

    def database(self, name):
        return self._call("database", "GET", name)

    def settings(self, name):
        return self._call("settings", "GET", name)

    def query(self, database_id, sql, params=None):
        return self._call("query", "POST", database_id, sql, params)

    def upload(self, name, database_id, artifact):
        return self._call("upload", "PUT", name, database_id, artifact)

    def enable(self, name, enabled=True):
        return self._call("enable", "POST", name, enabled)

    def verify(self, url, revision):
        return self._call("verify", "GET", url, revision)


@pytest.fixture
def local_case(system, monkeypatch, request):
    options = getattr(request, "param", {})
    sql = options.get("sql", SALT_SQL)
    monkeypatch.setattr(recovery_support, "MIGRATION", MIGRATION)
    monkeypatch.setattr("viracase.infrastructure.publication.WorkerBuildStatus", lambda repo: Builder())
    registry, service, actor, project = publication_fixture.__wrapped__(system, monkeypatch)
    repo = system["repo"]
    previous_revision = project["current_revision_id"]
    files = repo.source_files(actor, project["id"], previous_revision)
    files["migrations/0002.sql"] = BASELINE_USERS
    files[MIGRATION] = sql
    project = repo.save_revision(
        actor,
        project["id"],
        "local-recovery-incident-schema",
        {"base_revision_id": previous_revision, "files": files},
        project["version"],
    )["project"]
    qualify(system, registry)
    operation = service.start(
        actor,
        project["id"],
        "local-recovery-legacy-publication",
        {"revision_id": project["current_revision_id"]},
    )
    previous_publication = {
        "url": "https://previous-verified.example.test",
        "published_revision_id": previous_revision,
        "published_at": utcnow().isoformat(),
    }
    failure = {
        "code": "PUBLICATION_REJECTED",
        "message": "A requisição D1 legada não foi confirmada no cenário sintético.",
        "stage": "publication_migrating",
        "at": utcnow().isoformat(),
        "provider": {"method": "POST", "status": 400, "provider_error_codes": [7500]},
    }
    with repo.sessions.begin() as db:
        row = db.get(Operation, operation["id"])
        row.state, row.error_code, row.progress_stage = (
            "reconciling", "PUBLICATION_REJECTED", "publication_reconciling"
        )
        row.result = {
            "external_effect_pending": True,
            "publication_attempt": {
                "version": 1,
                "revision_id": project["current_revision_id"],
                "upload_started": False,
                "stage": "publication_migrating",
            },
            "publication_failure": failure,
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
            **previous_publication,
            "database_id": "test-database",
            "database_creation_started": True,
            "migrations": {
                path: sha256(content.encode()).hexdigest()
                for path, content in files.items()
                if path.startswith("migrations/") and path != MIGRATION
            },
            "migration_in_progress": {"name": MIGRATION, "sha256": sha256(sql.encode()).hexdigest()},
        }
    remote = SyntheticRemote(files, applied=options.get("applied", False))
    try:
        yield {
            "repo": repo,
            "service": service,
            "actor": actor,
            "project": project,
            "operation": operation,
            "remote": remote,
            "gateway_factory": lambda *_: StubGateway(remote),
            "previous_publication": previous_publication,
            "original_failure": failure,
        }
    finally:
        remote.connection.close()


@pytest.fixture
def cli():
    path = Path(__file__).parents[1] / "script" / "recover-publication-local.py"
    spec = importlib.util.spec_from_file_location("publication_recovery_local_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def session_hashes(repo):
    with repo.sessions() as db:
        return set(db.scalars(select(Session.token_hash)).all())


def stored_resolution(case):
    with case["repo"].sessions() as db:
        return deepcopy(db.get(Operation, case["operation"]["id"]).result.get("publication_resolution", {}))


def assert_original_state(case, *, resolved=False, previous_publication=True):
    with case["repo"].sessions() as db:
        operation = db.get(Operation, case["operation"]["id"])
        job = db.scalar(select(Job).where(Job.operation_id == operation.id))
        resource = db.get(PublicationResource, case["project"]["id"])
        assert operation.error_code == "PUBLICATION_REJECTED"
        assert operation.result["publication_failure"] == case["original_failure"]
        assert operation.state == ("failed" if resolved else "reconciling")
        assert job.state == ("done" if resolved else "reconciling")
        assert job.lease_token is None and job.lease_until is None
        if previous_publication:
            assert {key: resource.inventory[key] for key in case["previous_publication"]} == case["previous_publication"]
        if previous_publication or not resolved:
            assert MIGRATION not in resource.inventory["migrations"]
        assert bool(resource.inventory.get("migration_in_progress")) is not resolved


@pytest.fixture
def run_cli(cli, local_case, system, monkeypatch, capsys):
    case, repo = local_case, local_case["repo"]

    def run(*, login="operator", password="test-operator-password", confirmation=None, on_confirm=None, inspect=False):
        oid = case["operation"]["id"]
        before_sessions = session_hashes(repo)
        before_counts = recovery_support.counts(case)
        wallet = repo.wallet(case["actor"], True)
        first_call = len(case["remote"].calls)
        prompts = []
        disposed = []
        dispose = system["engine"].dispose

        def close_engine(*args, **kwargs):
            disposed.append(True)
            return dispose(*args, **kwargs)

        def answer(prompt):
            prompts.append(prompt)
            assert "ENCERRAR " + oid in prompt
            if on_confirm:
                on_confirm()
            return "ENCERRAR " + oid if confirmation is None else confirmation

        args = [oid, "--login", login, "--reason", REASON]
        if inspect:
            args.append("--inspect")
        capsys.readouterr()
        with monkeypatch.context() as patch:
            patch.setattr("builtins.input", answer)
            patch.setattr("getpass.getpass", lambda prompt: password)
            patch.setattr(system["engine"], "dispose", close_engine)
            code = cli.main(
                args,
                components_factory=lambda: (system["config"], system["engine"], repo, system["catalog"], None),
                gateway_factory=case["gateway_factory"],
                publication_factory=lambda current_repo: PublicationService(current_repo, Builder()),
            )
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert disposed == [True]
        assert session_hashes(repo) == before_sessions
        assert recovery_support.counts(case) == before_counts
        assert repo.wallet(case["actor"], True) == wallet
        calls = case["remote"].calls[first_call:]
        assert all(call[0] in {"database", "settings", "query"} for call in calls)
        assert all(call[0] != "query" or call[1].startswith("SELECT") for call in calls)
        for secret in (password, "synthetic-cloudflare-token", "synthetic-password-hash", SALT_SQL):
            assert secret not in output
        return SimpleNamespace(code=code, output=output, prompts=prompts, calls=calls)

    return run


def test_exact_incident_can_close_then_publish_current_revision_through_worker(
    local_case, run_cli, system
):
    case, repo = local_case, local_case["repo"]
    actor, project, oid = case["actor"], case["project"], case["operation"]["id"]
    assert sha256(SALT_SQL.encode()).hexdigest() == INCIDENT_SHA256
    assert case["remote"].receipt_count() == 0
    original_revision = project["current_revision_id"]
    files = repo.source_files(actor, project["id"], original_revision)
    files["README.md"] += "\nRevisão atual posterior à tentativa em recuperação.\n"
    current = repo.save_revision(
        actor,
        project["id"],
        "current-revision-after-incident",
        {"base_revision_id": original_revision, "files": files},
        project["version"],
    )["project"]
    assert current["current_revision_id"] != original_revision
    wallet, counts = repo.wallet(actor, True), recovery_support.counts(case)
    request_recovery_check(repo, actor, oid, "owner-check-before-local-recovery")
    assert recover_publications(repo, case["gateway_factory"], Builder(), oid) == oid
    snapshot = recovery_snapshot(repo, actor, oid)
    assert snapshot["classification"] == "schema_unchanged"
    assert snapshot["checks"]["schema"] == "matches_baseline"
    assert snapshot["checks"]["receipt"] == "absent"
    assert snapshot["operator_required"] is True
    assert snapshot["migration"] == {"name": MIGRATION, "sha256": INCIDENT_SHA256}
    assert case["service"].readiness(actor, project["id"])["can_publish"] is False

    result = run_cli()
    assert result.code == 0, result.output
    assert len(result.prompts) == 1
    assert "schema_unchanged" in result.output and "close_no_effect" in result.output
    assert "resolved" in result.output and current["current_revision_id"] in result.output
    assert_original_state(case, resolved=True)
    assert case["remote"].receipt_count() == 0
    resolution = stored_resolution(case)
    plan, attempt = resolution["plan"], resolution["attempt"]
    assert plan["revision_id"] == original_revision
    assert plan["can_resolve"] is True
    assert attempt["state"] == "completed" and attempt["action"] == "close_no_effect"
    assert attempt["actor_id"] == system["admin"]
    assert attempt["origin"] == "operator_attested_schema_postconditions"
    assert attempt["attestation"]["observed_outcome"] == "schema_unchanged"
    assert attempt["attestation"]["statement_indices"] == [1]
    assert attempt["attestation"]["evidence_sha256"] == canonical_hash(plan)
    assert attempt["receipt_requested"] is False and attempt["receipt_write_started"] is False
    with repo.sessions() as db:
        old = db.get(Operation, oid)
        assert old.result["external_effect_pending"] is False
        assert old.result["publication_recovery"]["state"] == "resolved"
    assert case["service"].readiness(actor, project["id"])["can_publish"] is True
    assert recovery_snapshot(repo, actor, oid)["classification"] == "resolved"
    assert recovery_support.counts(case) == counts

    with ExitStack() as stack:
        owner, headers = recovery_support.authenticated_client(system, stack, "student0")
        response = recovery_support.write(
            owner,
            headers,
            f"/api/v1/projects/{project['id']}/publications",
            {"revision_id": current["current_revision_id"]},
            "owner-authorized-current-revision-after-recovery",
        )
        assert response.status_code == 202, response.text
        new_operation = response.json()
        assert new_operation["state"] == "queued" and new_operation["id"] != oid
    assert recovery_support.counts(case) == {**counts, "Operation": counts["Operation"] + 1, "Job": counts["Job"] + 1}
    with repo.sessions.begin() as db:
        row = db.get(Operation, new_operation["id"])
        assert row.payload["revision_id"] == current["current_revision_id"]
        job = db.scalar(select(Job).where(Job.operation_id == row.id))
        job.state, job.lease_token = "running", "local-publication-after-recovery"
        job.lease_until = utcnow() + timedelta(minutes=5)
    execute_publication(
        Worker(repo, None, system["catalog"]),
        new_operation["id"],
        "local-publication-after-recovery",
        case["gateway_factory"],
        Builder(),
    )
    published = repo.operation(actor, new_operation["id"])
    assert published["state"] == "succeeded", published
    assert published["result_ref"]["revision_id"] == current["current_revision_id"]
    assert case["remote"].receipt_count() == 1
    assert sum(call[0] == "query" and call[1] == SALT_SQL for call in case["remote"].calls) == 1
    assert sum(call[0] == "upload" for call in case["remote"].calls) == 1
    row = case["remote"].connection.execute("SELECT id, email, password_hash, salt FROM users").fetchone()
    assert tuple(row) == (1, "synthetic-user@example.test", "synthetic-password-hash", "temp_salt_placeholder")
    assert repo.wallet(actor, True) == wallet
    assert_original_state(case, resolved=True, previous_publication=False)


@pytest.mark.parametrize(
    ("login", "password"),
    [("operator", "wrong-test-password"), ("student0", "test-student-password")],
)
def test_local_recovery_requires_actual_admin_password(local_case, run_cli, login, password):
    result = run_cli(login=login, password=password)
    assert result.code == 3, result.output
    assert result.prompts == [] and result.calls == []
    assert stored_resolution(local_case) == {}
    assert_original_state(local_case)


@pytest.mark.parametrize("answer", ["", "NAO", "correct-with-space"])
def test_local_recovery_requires_exact_confirmation(local_case, run_cli, answer):
    if answer == "correct-with-space":
        answer = "ENCERRAR " + local_case["operation"]["id"] + " "
    result = run_cli(confirmation=answer)
    assert result.code == 2, result.output
    assert len(result.prompts) == 1
    assert stored_resolution(local_case)["plan"]["can_resolve"] is True
    assert "attempt" not in stored_resolution(local_case)
    assert local_case["remote"].receipt_count() == 0
    assert_original_state(local_case)


def test_local_inspection_persists_reviewable_plan_without_attesting(local_case, run_cli):
    result = run_cli(inspect=True)
    assert result.code == 2 and result.prompts == [], result.output
    plan = stored_resolution(local_case)["plan"]
    assert plan["assessment"]["classification"] == "schema_unchanged"
    assert plan["plan_id"] in result.output and plan["plan_sha256"] in result.output
    assert "attempt" not in stored_resolution(local_case)
    assert_original_state(local_case)


@pytest.mark.parametrize(
    "local_case", [{"sql": GENERIC_SQL + "\nCREATE TABLE recovery_labels(id INTEGER PRIMARY KEY, label TEXT);"}],
    indirect=True,
)
def test_local_recovery_attests_every_instruction_in_the_preserved_manifest(local_case, run_cli):
    result = run_cli()
    assert result.code == 0, result.output
    resolution = stored_resolution(local_case)
    plan, attempt = resolution["plan"], resolution["attempt"]
    assert plan["assessment"]["statement_count"] == 2
    assert [statement["index"] for statement in plan["assessment"]["statement_manifest"]] == [1, 2]
    assert attempt["attestation"]["statement_indices"] == [1, 2]
    assert attempt["attestation"]["evidence_sha256"] == canonical_hash(plan)
    assert local_case["remote"].receipt_count() == 0
    assert_original_state(local_case, resolved=True)


@pytest.mark.parametrize("revocation", ["session", "role"])
def test_local_recovery_reauthenticates_after_confirmation(local_case, run_cli, system, revocation):
    def revoke():
        with local_case["repo"].sessions.begin() as db:
            if revocation == "session":
                db.execute(delete(Session).where(Session.account_id == system["admin"]))
            else:
                db.get(Account, system["admin"]).role = "student"

    result = run_cli(on_confirm=revoke)
    assert result.code == 3, result.output
    assert len(result.prompts) == 1
    assert "attempt" not in stored_resolution(local_case)
    assert local_case["remote"].receipt_count() == 0
    assert_original_state(local_case)


@pytest.mark.parametrize(
    "local_case,classification",
    [({"applied": True}, "unknown"), ({"sql": GENERIC_SQL, "applied": True}, "schema_applied")],
    indirect=["local_case"],
)
def test_local_recovery_refuses_unknown_or_applied_schema(local_case, run_cli, classification):
    result = run_cli()
    assert result.code == 2 and result.prompts == [], result.output
    plan = stored_resolution(local_case)["plan"]
    assert plan["assessment"]["classification"] == classification
    assert "attempt" not in stored_resolution(local_case)
    assert local_case["remote"].receipt_count() == 0
    assert_original_state(local_case)


def test_local_recovery_needs_proof_that_worker_upload_never_started(local_case, run_cli):
    with local_case["repo"].sessions.begin() as db:
        operation = db.get(Operation, local_case["operation"]["id"])
        result = deepcopy(operation.result)
        result.pop("publication_attempt")
        result["publication_failure"].pop("stage")
        operation.result = result
        local_case["original_failure"] = deepcopy(result["publication_failure"])
    result = run_cli()
    assert result.code == 2 and result.prompts == [], result.output
    plan = stored_resolution(local_case)["plan"]
    assert plan["assessment"]["classification"] == "schema_unchanged"
    assert plan["can_resolve"] is False and plan["action"] is None
    assert "attempt" not in stored_resolution(local_case)
    assert_original_state(local_case)


def test_local_recovery_accepts_preserved_legacy_preupload_stage(local_case, run_cli):
    with local_case["repo"].sessions.begin() as db:
        operation = db.get(Operation, local_case["operation"]["id"])
        result = deepcopy(operation.result)
        result.pop("publication_attempt")
        assert result["publication_failure"]["stage"] == "publication_migrating"
        operation.result = result
    result = run_cli()
    assert result.code == 0, result.output
    plan = stored_resolution(local_case)["plan"]
    assert plan["binding"]["upload_started"] is False
    assert plan["can_resolve"] is True
    assert_original_state(local_case, resolved=True)


def test_local_recovery_displays_long_authorized_worker_identity(local_case, run_cli):
    name = "viroucaseai-" + local_case["project"]["id"].replace("-", "")
    assert len(name) > 40
    with local_case["repo"].sessions.begin() as db:
        db.get(PublicationResource, local_case["project"]["id"]).worker_name = name
    result = run_cli()
    assert result.code == 0, result.output
    assert name in result.output
    plan = stored_resolution(local_case)["plan"]
    assert plan["binding"]["worker_name"] == name
    assert_original_state(local_case, resolved=True)


@pytest.mark.parametrize("unsafe", ["unknown_field", "sql_reason"])
def test_local_recovery_refuses_attestation_if_plan_cannot_be_displayed_exactly(
    local_case, run_cli, cli, monkeypatch, unsafe
):
    prepare = cli.prepare_recovery_plan

    def altered(*args, **kwargs):
        plan = deepcopy(prepare(*args, **kwargs))
        if unsafe == "unknown_field":
            plan["untrusted_extension"] = "private-extension-must-not-appear"
        else:
            plan["reason"] = "SELECT secret FROM users"
        plan["plan_sha256"] = canonical_hash({key: value for key, value in plan.items() if key != "plan_sha256"})
        return plan

    monkeypatch.setattr(cli, "prepare_recovery_plan", altered)
    result = run_cli()
    assert result.code == 3 and result.prompts == [], result.output
    assert "PLAN_UNSAFE" in result.output
    assert "private-extension-must-not-appear" not in result.output
    assert "SELECT secret FROM users" not in result.output
    assert "attempt" not in stored_resolution(local_case)
    assert_original_state(local_case)


def test_local_recovery_rechecks_remote_schema_after_confirmation(local_case, run_cli):
    def drift():
        local_case["remote"].connection.execute("CREATE TABLE concurrent_schema_change(id INTEGER)")
        local_case["remote"].connection.commit()

    result = run_cli(on_confirm=drift)
    assert result.code == 3 and "RECOVERY_PLAN_STALE" in result.output, result.output
    assert "attempt" not in stored_resolution(local_case)
    assert local_case["remote"].receipt_count() == 0
    assert_original_state(local_case)


def test_local_recovery_rejects_plan_replaced_by_another_admin(local_case, run_cli):
    repo = local_case["repo"]
    other_admin = new_id()
    with repo.sessions.begin() as db:
        db.add(Account(id=other_admin, login="other-operator", name="Outro operador", role="admin", password_hash=HASHER.hash("other-test-password")))

    def replace_plan():
        prepare_recovery_plan(
            repo, other_admin, local_case["operation"]["id"], "concurrent-admin-plan", REASON,
            local_case["gateway_factory"],
        )

    result = run_cli(on_confirm=replace_plan)
    assert result.code == 3 and "RECOVERY_PLAN_STALE" in result.output, result.output
    assert stored_resolution(local_case)["plan"]["prepared_by"] == other_admin
    assert "attempt" not in stored_resolution(local_case)
    assert_original_state(local_case)


def test_local_recovery_does_not_take_another_executors_live_lease(local_case, run_cli):
    with local_case["repo"].sessions.begin() as db:
        job = db.scalar(select(Job).where(Job.operation_id == local_case["operation"]["id"]))
        job.lease_token = "another-live-executor"
        job.lease_until = utcnow() + timedelta(minutes=5)
    result = run_cli()
    assert result.code == 3 and result.prompts == [] and result.calls == [], result.output
    assert stored_resolution(local_case) == {}
    with local_case["repo"].sessions() as db:
        job = db.scalar(select(Job).where(Job.operation_id == local_case["operation"]["id"]))
        assert job.state == "reconciling" and job.lease_token == "another-live-executor"


@pytest.mark.parametrize("blocker", ["another_publication", "qualification"])
def test_local_recovery_reports_remaining_readiness_blockers(local_case, run_cli, blocker):
    repo = local_case["repo"]
    other_id = new_id()
    with repo.sessions.begin() as db:
        if blocker == "another_publication":
            db.add(Operation(id=other_id, account_id=local_case["actor"], project_id=local_case["project"]["id"], type="publication", state="queued"))
            db.flush()
            db.add(Job(operation_id=other_id))
        else:
            resource = db.get(PublicationResource, local_case["project"]["id"])
            config = db.get(IntegrationConfig, resource.config_id)
            validation = deepcopy(config.validation)
            validation["qualification"]["report"]["checks"][0]["passed"] = False
            config.validation = validation
    result = run_cli()
    assert result.code == 4 and "resolved" in result.output, result.output
    expected = "OPERATION_CONFLICT" if blocker == "another_publication" else "CAPABILITY_UNVALIDATED"
    assert expected in result.output
    assert_original_state(local_case, resolved=True)
    readiness = local_case["service"].readiness(local_case["actor"], local_case["project"]["id"])
    assert readiness["can_publish"] is False
    assert expected in [reason["code"] for reason in readiness["reasons"]]
    if blocker == "another_publication":
        with repo.sessions() as db:
            assert db.get(Operation, other_id).state == "queued"
            assert db.scalar(select(Job).where(Job.operation_id == other_id)).state == "queued"


def test_repeated_local_command_only_reads_current_status(local_case, run_cli):
    first = run_cli()
    assert first.code == 0, first.output
    before = stored_resolution(local_case)
    with local_case["repo"].sessions() as db:
        audits = db.scalar(select(func.count()).select_from(Audit))
    second = run_cli()
    assert second.code == 0 and second.prompts == [] and second.calls == [], second.output
    assert stored_resolution(local_case) == before
    with local_case["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(Audit)) == audits
    assert_original_state(local_case, resolved=True)


def interrupt_completion(monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("synthetic terminal interruption before local completion")

    monkeypatch.setattr("viracase.infrastructure.publication_resolution.apply_resolution_result", interrupted)


def test_local_recovery_does_not_supersede_intention_started_between_preflight_and_claim(
    local_case, run_cli, cli, monkeypatch, system
):
    repo, oid = local_case["repo"], local_case["operation"]["id"]
    plan = prepare_recovery_plan(
        repo, system["admin"], oid, "concurrent-plan-before-cli", REASON,
        local_case["gateway_factory"],
    )
    payload = recovery_support.resolution_payload(plan)
    payload["attestation"]["observed_outcome"] = "schema_unchanged"
    prepare = cli.prepare_recovery_plan
    applying = []

    def concurrent_prepare(*args, **kwargs):
        with monkeypatch.context() as patch:
            interrupt_completion(patch)
            with pytest.raises(KeyboardInterrupt):
                resolve_recovery_plan(
                    repo, system["admin"], oid, "different-concurrent-intention", payload,
                    local_case["gateway_factory"],
                )
        applying.append(stored_resolution(local_case))
        return prepare(*args, **kwargs)

    monkeypatch.setattr(cli, "prepare_recovery_plan", concurrent_prepare)
    result = run_cli()
    assert result.code == 2 and result.prompts == [], result.output
    assert "RECOVERY_IN_PROGRESS" in result.output
    assert applying[0]["attempt"]["state"] == "applying"
    assert stored_resolution(local_case) == applying[0]
    assert_original_state(local_case)


def test_local_command_resumes_its_persisted_intention_after_interruption(
    local_case, run_cli, monkeypatch
):
    with monkeypatch.context() as patch:
        interrupt_completion(patch)
        first = run_cli()
    assert first.code == 2, first.output
    before = stored_resolution(local_case)
    assert before["attempt"]["state"] == "applying"
    assert_original_state(local_case)
    second = run_cli()
    assert second.code == 0, second.output
    after = stored_resolution(local_case)
    assert after["plan"] == before["plan"]
    assert after.get("previous_attempts", []) == before.get("previous_attempts", [])
    for field in ("actor_id", "intention_id", "payload_hash", "attestation", "started_at"):
        assert after["attempt"][field] == before["attempt"][field]
    assert after["attempt"]["state"] == "completed"
    assert local_case["remote"].receipt_count() == 0
    assert_original_state(local_case, resolved=True)


@pytest.mark.parametrize("incompatibility", ["different_actor", "different_intention"])
def test_local_command_preserves_another_applying_intention(
    local_case, run_cli, monkeypatch, incompatibility
):
    with monkeypatch.context() as patch:
        interrupt_completion(patch)
        first = run_cli()
    assert first.code == 2, first.output
    login, password = "operator", "test-operator-password"
    with local_case["repo"].sessions.begin() as db:
        if incompatibility == "different_actor":
            login, password = "other-operator", "other-test-password"
            db.add(Account(login=login, name="Outro operador", role="admin", password_hash=HASHER.hash(password)))
        else:
            operation = db.get(Operation, local_case["operation"]["id"])
            result = deepcopy(operation.result)
            result["publication_resolution"]["attempt"]["intention_id"] = "d" * 64
            operation.result = result
    before = stored_resolution(local_case)
    second = run_cli(login=login, password=password)
    assert second.code in {2, 3} and second.prompts == [] and second.calls == [], second.output
    assert stored_resolution(local_case) == before
    assert_original_state(local_case)
