from copy import deepcopy
from datetime import timedelta
from hashlib import sha256

import httpx
import pytest
from sqlalchemy import select
from test_publication_pipeline import Builder, POLICY, Remote, execute, start
from test_publication_pipeline import publication as publication_fixture

from viracase.domain.rules import RuleError, canonical_hash, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway
from viracase.infrastructure.models import IntegrationConfig, Job, Operation, PublicationResource
from viracase.infrastructure.publication_migrations import LEDGER
from viracase.infrastructure.publication_outcome import migration_needs_reconciliation, publication_diagnostic
from viracase.infrastructure.publication_reconcile import reconcile_publication
from viracase.infrastructure.publication_recovery import MAX_ATTEMPTS, recover_publications
from viracase.infrastructure.publication_sql_review import split_sql_statements
from viracase.infrastructure.worker import Worker


@pytest.fixture
def publication(system, monkeypatch):
    return publication_fixture.__wrapped__(system, monkeypatch)


class ObservedRemote(Remote):
    def __init__(self, sql=None, response=None):
        super().__init__()
        self.target_sql, self.response = sql, response
        self.sql_calls, self.application_sql = [], []
        self.on_sql_confirmed = None

    def query(self, database_id, sql, params=None):
        self.sql_calls.append(sql)
        if sql == self.target_sql:
            self.calls.append("query")
            gateway = CloudflareGateway(POLICY, "synthetic-cloudflare-token", httpx.MockTransport(self.response))
            gateway.before_request = self.before_request
            return gateway.query(database_id, sql, params)
        if getattr(self, "before_request", None):
            self.before_request("POST")
        result = super().query(database_id, sql, params)
        if LEDGER not in sql and "sqlite_schema" not in sql:
            self.application_sql.append(sql)
            if self.on_sql_confirmed:
                self.on_sql_confirmed()
            return [{"success": True, "results": []} for _ in split_sql_statements(sql)]
        return result


def current(system, publication, operation):
    return system["repo"].operation(publication[2], operation["id"])


def seed_interrupted(system, publication, operation, remote, phase, *, legacy=False, attention=False):
    repo = system["repo"]
    files = repo.source_files(publication[2], publication[3]["id"], publication[3]["current_revision_id"])
    name = "migrations/0001.sql"
    digest = sha256(files[name].encode()).hexdigest()
    effect = {"prepared": "not_sent", "sql_sending": "unknown", "sql_rejected": "none"}.get(phase, "confirmed")
    pending = {"name": name, "sha256": digest}
    if not legacy:
        pending.update(
            protocol_version=2, phase=phase, effect_outcome=effect,
            operation_id=operation["id"], revision_id=publication[3]["current_revision_id"],
            statement_count=len(split_sql_statements(files[name])),
            sql_result={"confirmed_statements": len(split_sql_statements(files[name]))},
            schema_after={"sha256": canonical_hash([]), "object_count": 0},
        )
    with repo.sessions.begin() as db:
        row = db.get(Operation, operation["id"])
        row.state, row.error_code = "reconciling", "PUBLICATION_REJECTED"
        row.result = {
            "external_effect_pending": True,
            "publication_attempt": {
                "version": 1, "stage": "publication_migrating", "upload_started": False,
                "revision_id": row.payload["revision_id"],
            },
            "publication_failure": {
                "code": "PUBLICATION_REJECTED", "stage": "publication_migrating",
                "message": "A Cloudflare recusou a operação.",
            },
            "publication_recovery": {"state": "attention" if attention else "scheduled", "attempts": 1},
        }
        job = db.scalar(select(Job).where(Job.operation_id == row.id))
        job.state, job.lease_token, job.lease_until = "reconciling", None, None
        job.available_at = utcnow() - timedelta(seconds=1)
        resource = db.get(PublicationResource, row.project_id)
        remote.db = {"name": resource.worker_name, "uuid": "synthetic-database"}
        resource.database_id = remote.db["uuid"]
        resource.inventory = {
            "database_id": remote.db["uuid"], "database_creation_started": True,
            "migration_in_progress": pending,
            "url": "https://previous.example.workers.dev", "published_revision_id": "previous-revision",
        }
    return pending


def recover(system, operation, remote):
    return recover_publications(system["repo"], lambda *_: remote, Builder(), operation["id"])


def test_real_d1_ddl_rejection_is_terminal_with_original_diagnostic_and_new_attempt_allowed(system, publication):
    repo, actor, project = system["repo"], publication[2], publication[3]
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    sql = "ALTER TABLE project_settings ADD COLUMN salt TEXT;"
    files["migrations/0002_add_salt.sql"] = sql
    saved = repo.save_revision(actor, project["id"], "add-migration", {
        "base_revision_id": project["current_revision_id"], "files": files,
    }, project["version"])
    publication = (*publication[:3], saved["project"])
    operation = start(system, publication)

    def reject(request):
        return httpx.Response(400, headers={"cf-ray": "1234567890abcdef-GRU"}, json={
            "success": False, "errors": [{"code": 7500, "message": "duplicate column name: salt: SQLITE_ERROR"}],
            "result": None,
        })

    remote = ObservedRemote(sql, reject)
    execute(system, operation, remote)
    result = current(system, publication, operation)
    assert result["state"] == "failed" and result["error_code"] == "MIGRATION_SQL_REJECTED"
    assert result["result_ref"]["external_effect_pending"] is False
    diagnosis = result["result_ref"]["publication_diagnostic"]
    assert diagnosis["phase"] == "sql_rejected" and diagnosis["effect_status"] == "none"
    assert diagnosis["migration"]["name"] == "migrations/0002_add_salt.sql"
    assert diagnosis["initial_error"]["provider"]["reason"] == "duplicate_column"
    assert diagnosis["initial_error"]["provider"]["cf_ray"] == "1234567890abcdef-GRU"
    with repo.sessions() as db:
        resource = db.get(PublicationResource, project["id"])
        assert resource.inventory["migration_in_progress"] is None
        assert resource.inventory["migration_last_attempt"]["operation_id"] == operation["id"]
    assert "upload" not in remote.calls
    assert publication[1].readiness(actor, project["id"])["can_publish"] is True
    again = publication[1].start(actor, project["id"], "corrected-attempt", {
        "revision_id": saved["project"]["current_revision_id"],
    })
    assert again["id"] != operation["id"] and again["state"] == "queued"


@pytest.mark.parametrize("previous_attempts", [1, MAX_ATTEMPTS, MAX_ATTEMPTS + 3])
def test_legacy_attention_is_observed_once_and_keeps_initial_and_current_errors_separate(system, publication, previous_attempts):
    operation, remote = start(system, publication), ObservedRemote()
    seed_interrupted(system, publication, operation, remote, "sql_sending", legacy=True, attention=True)
    with system["repo"].sessions.begin() as db:
        row = db.get(Operation, operation["id"])
        row.result = {
            **row.result,
            "publication_recovery": {**row.result["publication_recovery"], "attempts": previous_attempts},
        }
    assert recover(system, operation, remote) == operation["id"]
    result = current(system, publication, operation)
    recovery = result["result_ref"]["publication_recovery"]
    assert result["state"] == "reconciling" and result["error_code"] == "PUBLICATION_REJECTED"
    assert recovery["version"] == 2 and recovery["state"] == "attention"
    assert recovery["attempts"] == previous_attempts + 1 and not recovery["legacy_reassessment_pending"]
    assert recovery["last_error_code"] == "MIGRATION_RESULT_UNKNOWN"
    assert recovery["owner"] == "operator" and recovery["missing_evidence"] and recovery["required_action"]
    assert result["result_ref"]["publication_diagnostic"]["initial_error"]["code"] == "PUBLICATION_REJECTED"
    assert result["result_ref"]["publication_diagnostic"]["detail_available"] is False
    assert not remote.application_sql and "upload" not in remote.calls
    calls = len(remote.sql_calls)
    assert recover(system, operation, remote) is None and len(remote.sql_calls) == calls
    assert publication[1].readiness(publication[2], publication[3]["id"])["publication"]["revision_id"] == "previous-revision"


@pytest.mark.parametrize("failure_mode", ["local_guard", "connect_timeout"])
def test_rejection_before_sql_is_terminal_even_when_cleanup_checkpoint_is_blocked(system, publication, failure_mode):
    repo, actor, project = system["repo"], publication[2], publication[3]
    operation = start(system, publication)
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    sql = files["migrations/0001.sql"]
    transport_calls = []

    def invalidate():
        with repo.sessions.begin() as db:
            row = db.get(Operation, operation["id"])
            cfg = db.get(IntegrationConfig, row.policy.get("execution_config_id", row.policy["config_id"]))
            cfg.validation = {**cfg.validation, "ready": False}

    def transport(request):
        transport_calls.append(request)
        invalidate()
        raise httpx.ConnectTimeout("synthetic connect failure before dispatch")

    class InvalidatedRemote(ObservedRemote):
        def query(self, database_id, statement, params=None):
            if statement == sql and failure_mode == "local_guard":
                invalidate()
            return super().query(database_id, statement, params)

    remote = InvalidatedRemote(sql, transport)
    execute(system, operation, remote)
    result = current(system, publication, operation)
    assert result["state"] == "failed"
    assert result["error_code"] == ("CONFIGURATION_INVALID" if failure_mode == "local_guard" else "MIGRATION_SQL_REJECTED")
    assert result["result_ref"]["external_effect_pending"] is False
    diagnostic = result["result_ref"]["publication_diagnostic"]
    assert diagnostic["effect_status"] == "not_sent" and diagnostic["phase"] == "sql_rejected"
    provider = diagnostic["initial_error"]["provider"]
    if failure_mode == "local_guard":
        assert provider["reason"] == "local_dispatch_blocked" and not transport_calls
    else:
        assert provider["transport_error"] == "ConnectTimeout" and len(transport_calls) == 1
    assert not remote.receipts and not remote.application_sql and "upload" not in remote.calls
    with repo.sessions() as db:
        resource = db.get(PublicationResource, project["id"])
        assert resource.inventory["migration_in_progress"] is None
        assert resource.inventory["migration_last_attempt"]["operation_id"] == operation["id"]
        assert resource.inventory["migration_last_attempt"]["effect_outcome"] == "not_sent"


def test_upload_connection_failure_before_dispatch_is_terminal_and_preserves_confirmed_migrations(system, publication):
    operation = start(system, publication)

    def transport(request):
        raise httpx.ConnectTimeout("synthetic connection failure before upload")

    class UnsentUploadRemote(ObservedRemote):
        def upload(self, name, database_id, artifact):
            gateway = CloudflareGateway(POLICY, "synthetic-cloudflare-token", httpx.MockTransport(transport))
            gateway.before_request = self.before_request
            gateway.request("PUT", gateway.account_path + "/workers/scripts/" + name, content=b"synthetic artifact")

    remote = UnsentUploadRemote()
    execute(system, operation, remote)
    result = current(system, publication, operation)
    assert result["state"] == "failed" and result["result_ref"]["external_effect_pending"] is False
    assert result["result_ref"]["publication_attempt"]["upload_started"] is False
    assert "migration" not in result["result_ref"]["publication_diagnostic"]
    with system["repo"].sessions() as db:
        resource = db.get(PublicationResource, publication[3]["id"])
        assert resource.inventory["migration_in_progress"] is None
        assert resource.inventory["migrations"] == remote.receipts
    assert len(remote.application_sql) == len(remote.receipts) == 1
    assert publication[1].readiness(publication[2], publication[3]["id"])["can_publish"] is True


@pytest.mark.parametrize("phase", ["sql_confirmed", "receipt_sending"])
def test_confirmed_sql_repairs_only_receipt_and_resumes_same_operation(system, publication, phase):
    operation, remote = start(system, publication), ObservedRemote()
    pending = seed_interrupted(system, publication, operation, remote, phase)
    recover(system, operation, remote)
    result = current(system, publication, operation)
    assert result["state"] == "queued"
    assert remote.receipts[pending["name"]] == pending["sha256"]
    assert not remote.application_sql
    execute(system, operation, remote)
    result = current(system, publication, operation)
    assert result["state"] == "succeeded" and result["id"] == operation["id"]
    assert not remote.application_sql and remote.calls.count("upload") == 1


def test_crash_before_send_resumes_without_inventing_a_receipt(system, publication):
    operation, remote = start(system, publication), ObservedRemote()
    seed_interrupted(system, publication, operation, remote, "prepared")
    recover(system, operation, remote)
    assert current(system, publication, operation)["state"] == "queued"
    assert not remote.receipts and not remote.application_sql
    execute(system, operation, remote)
    assert current(system, publication, operation)["state"] == "succeeded"
    assert len(remote.application_sql) == 1


def test_crash_after_receipt_confirmation_never_replays_sql(system, publication):
    operation, remote = start(system, publication), ObservedRemote()
    pending = seed_interrupted(system, publication, operation, remote, "receipt_confirmed")
    remote.receipts[pending["name"]] = pending["sha256"]
    recover(system, operation, remote)
    execute(system, operation, remote)
    assert current(system, publication, operation)["state"] == "succeeded"
    assert not remote.application_sql


@pytest.mark.parametrize("phase", ["sql_sending", "receipt_confirmed"])
def test_missing_receipt_never_proves_no_effect_or_success(system, publication, phase):
    operation, remote = start(system, publication), ObservedRemote()
    seed_interrupted(system, publication, operation, remote, phase)
    recover(system, operation, remote)
    result = current(system, publication, operation)
    assert result["state"] == "reconciling"
    assert result["result_ref"]["publication_recovery"]["state"] == "attention"
    assert not remote.receipts and not remote.application_sql and "upload" not in remote.calls


def test_crash_with_confirmed_sql_and_false_external_flag_still_enters_reconciliation(system, publication):
    operation, remote = start(system, publication), ObservedRemote()
    seed_interrupted(system, publication, operation, remote, "sql_confirmed")
    with system["repo"].sessions.begin() as db:
        row = db.get(Operation, operation["id"])
        row.state, row.result = "running", {**row.result, "external_effect_pending": False}
        job = db.scalar(select(Job).where(Job.operation_id == row.id))
        job.state, job.lease_token, job.lease_until = "running", "expired", utcnow() - timedelta(seconds=1)
    Worker(system["repo"], None, system["catalog"]).recover()
    assert current(system, publication, operation)["state"] == "reconciling"


def test_cancel_after_confirmed_sql_waits_for_receipt_before_stopping(system, publication):
    operation, remote = start(system, publication), ObservedRemote()

    def cancel():
        system["repo"].cancel(publication[2], operation["id"], "cancel-after-sql")

    remote.on_sql_confirmed = cancel
    execute(system, operation, remote)
    result = current(system, publication, operation)
    assert result["state"] == "cancelled"
    assert len(remote.application_sql) == 1 and len(remote.receipts) == 1
    assert "upload" not in remote.calls
    with system["repo"].sessions() as db:
        resource = db.get(PublicationResource, publication[3]["id"])
        assert resource.inventory["migration_in_progress"] is None
        assert resource.inventory["migrations"] == remote.receipts


def test_manual_reconciliation_records_current_impediment_without_replacing_original_error(system, publication):
    operation, remote = start(system, publication), ObservedRemote()
    seed_interrupted(system, publication, operation, remote, "sql_sending", legacy=True, attention=True)
    with pytest.raises(RuleError) as caught:
        reconcile_publication(system["repo"], system["admin"], operation["id"], "inspect", "Conferir recibo pendente", lambda *_: remote)
    assert caught.value.code == "MIGRATION_RESULT_UNKNOWN"
    result = current(system, publication, operation)
    assert result["error_code"] == "PUBLICATION_REJECTED"
    recovery = result["result_ref"]["publication_recovery"]
    assert recovery["last_error_code"] == "MIGRATION_RESULT_UNKNOWN" and recovery["last_error_message"]
    assert recovery["state"] == "attention" and recovery["next_attempt_at"] is None


def test_previous_attempt_migration_is_not_reported_as_current_build_failure():
    inventory = {"migration_last_attempt": {
        "protocol_version": 2, "name": "migrations/0001.sql", "sha256": "a" * 64,
        "phase": "receipt_confirmed", "effect_outcome": "confirmed",
        "revision_id": "same-revision", "operation_id": "previous-attempt",
    }}
    original = deepcopy(inventory)
    result = publication_diagnostic({}, inventory, stage="publication_building", operation_id="current-attempt", revision_id="same-revision")
    assert "migration" not in result and result["detail_available"] is False
    assert result["effect_status"] == "none" and inventory == original


def test_terminal_recovery_retains_its_migration_diagnostic_and_never_carries_it_into_upload():
    inventory = {"migration_last_attempt": {
        "protocol_version": 2, "name": "migrations/0002.sql", "sha256": "b" * 64,
        "phase": "sql_rejected", "effect_outcome": "none",
        "revision_id": "revision", "operation_id": "operation",
    }}
    result = {"publication_failure": {
        "code": "MIGRATION_SQL_REJECTED", "stage": "publication_migrating", "message": "SQL recusado.",
    }}
    terminal = publication_diagnostic(result, inventory, stage="publication_failed", operation_id="operation", revision_id="revision")
    assert terminal["migration"]["name"] == "migrations/0002.sql"
    assert terminal["phase"] == "sql_rejected" and terminal["effect_status"] == "none"
    uploading = publication_diagnostic(result, inventory, stage="publication_uploading", operation_id="operation", revision_id="revision")
    assert "migration" not in uploading and uploading["phase"] == "publication_uploading"


@pytest.mark.parametrize("value", [[], {}, 123, None])
def test_malformed_phase_is_conservatively_uncertain(value):
    pending = {"protocol_version": 2, "name": "migrations/0001.sql", "sha256": "a" * 64, "phase": value, "effect_outcome": value}
    assert migration_needs_reconciliation(pending) is True
    result = publication_diagnostic({"external_effect_pending": True}, {"migration_in_progress": pending})
    assert result["effect_status"] == "unknown"
