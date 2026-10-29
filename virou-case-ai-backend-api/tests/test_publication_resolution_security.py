from copy import deepcopy
from hashlib import sha256

import pytest
from sqlalchemy import select
from test_publication_pipeline import Builder, Remote, execute, start
from test_publication_pipeline import publication as publication_fixture

from viracase.domain.rules import RuleError
from viracase.infrastructure.models import Job, PublicationResource
from viracase.infrastructure.publication_migrations import LEDGER
from viracase.infrastructure.publication_reconcile import observe_publication, reconcile_publication
from viracase.infrastructure.publication_recovery import recover_publications


@pytest.fixture
def publication(system, monkeypatch):
    return publication_fixture.__wrapped__(system, monkeypatch)


class ProvisioningRemote(Remote):
    def __init__(self, mode="create_uncertain"):
        super().__init__(mode)
        self.ledger_created = False
        self.ledger_error_reason = "missing_table"
        self.sql_calls = []

    def query(self, database_id, sql, params=None):
        self.sql_calls.append(sql)
        if sql.startswith(f"CREATE TABLE IF NOT EXISTS {LEDGER}"):
            self.ledger_created = True
        if sql.startswith("SELECT name, sha256") and not self.ledger_created:
            self.calls.append("query")
            failure = RuleError("PUBLICATION_REJECTED", "Consulta remota rejeitada no laboratório.")
            failure.provider_diagnostic = {"reason": self.ledger_error_reason, "effect_outcome": "none"}
            raise failure
        return super().query(database_id, sql, params)


@pytest.mark.parametrize("recovery", ["automatic", "manual"])
def test_new_database_without_ledger_resumes_provisioning_before_first_migration(
    system, publication, recovery, monkeypatch
):
    monkeypatch.setattr("viracase.infrastructure.publication.WorkerBuildStatus", lambda repo: Builder())
    repo = system["repo"]
    _, _, actor, project = publication
    operation = start(system, publication)
    remote = ProvisioningRemote()
    execute(system, operation, remote)
    assert repo.operation(actor, operation["id"])["state"] == "reconciling"
    assert remote.db is not None and not remote.ledger_created
    with repo.sessions() as db:
        inventory = db.get(PublicationResource, project["id"]).inventory
        assert not inventory.get("migrations") and not inventory.get("migration_in_progress")
    wallet = repo.wallet(actor, True)
    remote.mode = "success"
    if recovery == "automatic":
        assert recover_publications(repo, lambda *_: remote, Builder(), operation["id"]) == operation["id"]
    else:
        reconcile_publication(
            repo,
            system["admin"],
            operation["id"],
            "reconcile-new-database",
            "Conferir banco criado antes do início das migrations.",
            lambda *_: remote,
        )
    current = repo.operation(actor, operation["id"])
    assert current["state"] == "queued", current
    assert current["result_ref"]["publication_recovery"]["resolution"] == "resume_safe"
    assert current["result_ref"]["external_effect_pending"] is False
    assert not remote.ledger_created and not remote.receipts
    assert all(sql.startswith("SELECT") for sql in remote.sql_calls)
    assert "upload" not in remote.calls
    execute(system, operation, remote)
    assert repo.operation(actor, operation["id"])["state"] == "succeeded"
    assert remote.ledger_created and remote.receipts
    assert remote.calls.count("create_database") == remote.calls.count("upload") == 1
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    for path, content in files.items():
        if path.startswith("migrations/"):
            assert remote.sql_calls.count(content) == 1
    assert repo.wallet(actor, True) == wallet


@pytest.mark.parametrize("state", ["fresh", "confirmed", "pending", "complete", "denied"])
def test_absent_ledger_exception_is_limited_to_initial_unconfirmed_history(state):
    from viracase.infrastructure.publication_migrations import verify_remote_migration_history

    path = "migrations/0001.sql"
    sql = "CREATE TABLE items(id INTEGER PRIMARY KEY);"
    digest = sha256(sql.encode()).hexdigest()
    files, inventory = {path: sql}, {"migrations": {}}
    remote = ProvisioningRemote()
    if state == "confirmed":
        inventory["migrations"][path] = digest
    elif state == "pending":
        inventory["migration_in_progress"] = {"name": path, "sha256": digest}
    elif state == "denied":
        remote.ledger_error_reason = "permission_denied"
    before = deepcopy(inventory)
    if state == "fresh":
        assert verify_remote_migration_history(remote, "synthetic-database", files, inventory) == {}
    else:
        with pytest.raises(RuleError) as caught:
            verify_remote_migration_history(
                remote, "synthetic-database", files, inventory, require_complete=state == "complete"
            )
        assert caught.value.code == (
            "PUBLICATION_REJECTED" if state == "denied" else "MIGRATION_RESULT_UNKNOWN"
        )
    assert inventory == before and remote.calls == ["query"]
    assert remote.sql_calls == [f"SELECT name, sha256 FROM {LEDGER}"]


def test_healthy_worker_cannot_confirm_missing_ledger_without_local_receipts():
    remote = ProvisioningRemote()
    remote.db = {"uuid": "synthetic-database", "name": "owned-worker"}
    remote.remote = {
        "tags": ["viroucaseai", "authorized-revision"],
        "bindings": [
            {"type": "d1", "name": "DB", "id": "synthetic-database"},
            {"type": "assets", "name": "ASSETS"},
        ],
    }
    with pytest.raises(RuleError) as caught:
        observe_publication(
            remote,
            "owned-worker",
            "authorized-revision",
            {"database_creation_started": True, "migrations": {}},
            "synthetic-database",
            {"migrations/0001.sql": "CREATE TABLE items(id INTEGER PRIMARY KEY);"},
        )
    assert caught.value.code == "MIGRATION_RESULT_UNKNOWN"
    assert not remote.receipts and not remote.ledger_created
    assert "upload" not in remote.calls and "verify" not in remote.calls
    assert all(sql.startswith("SELECT") for sql in remote.sql_calls)


def test_manual_reconciliation_cannot_hide_missing_remote_receipts_behind_healthy_worker(system, publication):
    repo = system["repo"]
    _, service, actor, project = publication
    first = start(system, publication)
    remote = Remote()
    execute(system, first, remote)
    published = repo.operation(actor, first["id"])
    assert published["state"] == "succeeded"
    assert remote.receipts
    remote.receipts.clear()

    second = service.start(
        actor,
        project["id"],
        "republish-with-missing-receipt",
        {"revision_id": project["current_revision_id"]},
    )
    execute(system, second, remote)
    uncertain = repo.operation(actor, second["id"])
    assert uncertain["state"] == "reconciling"
    assert uncertain["error_code"] == "MIGRATION_RESULT_UNKNOWN"
    with repo.sessions() as db:
        resource = db.get(PublicationResource, project["id"])
        assert not resource.inventory.get("migration_in_progress")
        assert resource.inventory["migrations"]
    before = list(remote.calls)
    wallet = repo.wallet(actor, True)

    with pytest.raises(RuleError) as caught:
        reconcile_publication(
            repo,
            system["admin"],
            second["id"],
            "confirm-missing-ledger",
            "Conferir recibos sem repetir migrations da revisão publicada.",
            lambda *_: remote,
        )
    assert caught.value.code == "MIGRATION_RESULT_UNKNOWN"
    result = repo.operation(actor, second["id"])
    assert result["state"] == "reconciling"
    assert result["result_ref"]["publication_recovery"]["state"] == "attention"
    assert "url" not in result["result_ref"]
    assert remote.receipts == {}
    assert remote.calls.count("upload") == before.count("upload")
    assert remote.calls.count("verify") == before.count("verify")
    assert repo.wallet(actor, True) == wallet
    with repo.sessions() as db:
        job = db.scalar(select(Job).where(Job.operation_id == second["id"]))
        assert job.state == "reconciling" and job.lease_token is None
        resource = db.get(PublicationResource, project["id"])
        assert resource.inventory["published_revision_id"] == project["current_revision_id"]
        assert resource.inventory["url"] == published["result_ref"]["url"]
