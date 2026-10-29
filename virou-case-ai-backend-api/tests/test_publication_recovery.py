from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import select
from sqlalchemy.orm import object_session
from test_publication_pipeline import Builder, Remote, execute, start
from test_publication_pipeline import publication as publication_fixture
from test_publication_runtime import claim

from viracase.domain.rules import RuleError, aware, canonical_hash, utcnow
from viracase.infrastructure.cloudflare_gateway import PublicationUncertain
from viracase.infrastructure.models import Job, Operation, PublicationResource
from viracase.infrastructure.publication_reconcile import reconcile_publication
from viracase.infrastructure.publication_recovery import MAX_ATTEMPTS, recover_publications
from viracase.infrastructure.publication_worker import execute_publication
from viracase.infrastructure.worker import Worker


@pytest.fixture
def publication(system, monkeypatch):
    return publication_fixture.__wrapped__(system, monkeypatch)


def recover(system, operation, remote, executor=None):
    return recover_publications(
        system["repo"], lambda *_: remote, executor or Builder(), operation["id"]
    )


def result(system, publication, operation):
    return system["repo"].operation(publication[2], operation["id"])


def due_now(system, operation):
    with system["repo"].sessions.begin() as db:
        job = db.scalar(select(Job).where(Job.operation_id == operation["id"]))
        job.available_at = utcnow() - timedelta(seconds=1)


def seed_legacy_failure(system, publication, operation, *, pending=False, candidate=None):
    with system["repo"].sessions.begin() as db:
        row = db.get(Operation, operation["id"])
        row.state, row.error_code = "reconciling", "MIGRATION_CHECKSUM_CONFLICT"
        row.result = {
            "external_effect_pending": True,
            "publication_failure": {
                "code": "MIGRATION_CHECKSUM_CONFLICT", "stage": "publication_provisioning",
                "message": "Uma migration publicada foi alterada.", "at": utcnow().isoformat(),
            },
            "activity": [{"stage": "publication_provisioning", "at": utcnow().isoformat()}],
        }
        job = db.scalar(select(Job).where(Job.operation_id == operation["id"]))
        job.state, job.lease_token, job.lease_until = "reconciling", None, None
        resource = db.get(PublicationResource, publication[3]["id"])
        resource.inventory = {
            "published_revision_id": "previous-revision", "url": "https://previous.example.workers.dev",
            **({"candidate_revision_id": candidate} if candidate else {}),
            **({"migration_in_progress": {"name": "migrations/0001_init.sql", "sha256": "a" * 64}}
               if pending else {}),
        }


def test_confirmed_d1_queries_then_checksum_conflict_fail_without_worker_upload(system, publication):
    operation = start(system, publication)
    remote = Remote()
    files = system["repo"].source_files(publication[2], publication[3]["id"], publication[3]["current_revision_id"])
    migration = next(name for name in files if name.startswith("migrations/"))
    remote.receipts[migration] = "0" * 64
    query = remote.query

    def request(database_id, sql, params=None):
        remote.before_request("POST")
        return query(database_id, sql, params)

    remote.query = request
    execute(system, operation, remote)
    current = result(system, publication, operation)
    assert current["state"] == "failed"
    assert current["error_code"] == "MIGRATION_CHECKSUM_CONFLICT"
    assert current["result_ref"]["external_effect_pending"] is False
    assert remote.calls.count("query") == 2 and "upload" not in remote.calls
    assert remote.db is not None and remote.remote is None
    with system["repo"].sessions() as db:
        resource = db.get(PublicationResource, publication[3]["id"])
        assert resource.database_id == remote.db["uuid"]
        assert db.scalar(select(Job).where(Job.operation_id == operation["id"])).state == "done"


def test_known_rejection_before_worker_put_does_not_enter_reconciliation(system, publication):
    operation = start(system, publication)
    remote = Remote()

    def reject(*args):
        remote.before_request("GET")
        failure = RuleError("CONFIGURATION_INVALID", "Synthetic rejection before upload")
        failure.provider_diagnostic = {"method": "GET", "status": 403}
        raise failure

    remote.upload = reject
    execute(system, operation, remote)
    current = result(system, publication, operation)
    assert current["state"] == "failed"
    assert current["result_ref"]["external_effect_pending"] is False
    assert current["result_ref"]["publication_attempt"]["upload_started"] is False
    assert remote.remote is None


def test_legacy_pre_upload_conflict_is_repaired_without_network_and_keeps_last_publication(system, publication):
    operation = start(system, publication)
    seed_legacy_failure(system, publication, operation)

    def no_gateway(*args):
        pytest.fail("A deterministic pre-upload failure must not contact Cloudflare")

    assert recover_publications(system["repo"], no_gateway, operation_id=operation["id"]) == operation["id"]
    current = result(system, publication, operation)
    assert current["state"] == "failed" and current["pending_reconciliation"] is False
    assert current["error_code"] == "MIGRATION_CHECKSUM_CONFLICT"
    metadata = current["result_ref"]["publication_recovery"]
    assert metadata["state"] == "resolved" and metadata["resolution"] == "pre_upload_failure"
    assert metadata["last_checked_at"] and metadata["next_attempt_at"] is None
    with system["repo"].sessions() as db:
        resource = db.get(PublicationResource, publication[3]["id"])
        assert resource.inventory["published_revision_id"] == "previous-revision"
        assert resource.inventory["url"] == "https://previous.example.workers.dev"


def test_legacy_conflict_with_candidate_evidence_is_not_assumed_safe(system, publication):
    operation = start(system, publication)
    seed_legacy_failure(system, publication, operation, candidate=publication[3]["current_revision_id"])
    remote = Remote()
    recover(system, operation, remote)
    current = result(system, publication, operation)
    assert current["state"] == "reconciling"
    assert current["result_ref"]["publication_recovery"]["state"] == "attention"
    assert "upload" not in remote.calls


def test_remote_confirmed_upload_recovers_automatically_without_redeploy(system, publication):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)
    assert result(system, publication, operation)["state"] == "reconciling"
    remote.mode = "success"
    recover(system, operation, remote)
    current = result(system, publication, operation)
    assert current["state"] == "succeeded" and current["error_code"] is None
    assert current["result_ref"]["url"].endswith(".synthetic-test.workers.dev")
    assert current["result_ref"]["external_effect_pending"] is False
    metadata = current["result_ref"]["publication_recovery"]
    assert metadata["state"] == "resolved" and metadata["resolution"] == "published"
    assert metadata["attempts"] == 1 and metadata["last_checked_at"]
    assert remote.calls.count("upload") == 1
    assert remote.calls[-2:] == ["enable", "verify"]
    assert recover(system, operation, remote) is None


def test_uncertain_database_creation_is_resumed_from_confirmed_owned_database(system, publication):
    operation = start(system, publication)
    remote = Remote("create_uncertain")
    execute(system, operation, remote)
    remote.mode = "success"
    recover(system, operation, remote)
    current = result(system, publication, operation)
    assert current["state"] == "queued"
    assert current["result_ref"]["external_effect_pending"] is False
    assert current["result_ref"]["publication_recovery"]["resolution"] == "resume_safe"
    execute(system, operation, remote)
    assert result(system, publication, operation)["state"] == "succeeded"
    assert remote.calls.count("create_database") == 1 and remote.calls.count("upload") == 1


def test_unknown_migration_receipt_requires_attention_and_never_replays_sql(system, publication):
    operation = start(system, publication)
    remote = Remote("migration_uncertain")
    execute(system, operation, remote)
    before = remote.calls.count("query")
    recover(system, operation, remote)
    current = result(system, publication, operation)
    assert current["state"] == "reconciling" and "url" not in current["result_ref"]
    metadata = current["result_ref"]["publication_recovery"]
    assert metadata["state"] == "attention" and metadata["last_error_code"] == "MIGRATION_RESULT_UNKNOWN"
    assert remote.calls.count("query") == before + 1
    assert "upload" not in remote.calls
    assert recover(system, operation, remote) is None


def test_absent_worker_is_polled_with_bounded_backoff_and_never_reuploaded(system, publication):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)
    remote.remote = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        due_now(system, operation)
        recover(system, operation, remote)
        current = result(system, publication, operation)
        assert current["state"] == "reconciling" and "url" not in current["result_ref"]
        metadata = current["result_ref"]["publication_recovery"]
        assert metadata["attempts"] == attempt
        if attempt < MAX_ATTEMPTS:
            assert metadata["state"] == "retry_wait"
            with system["repo"].sessions() as db:
                job = db.scalar(select(Job).where(Job.operation_id == operation["id"]))
                assert aware(job.available_at) > utcnow() + timedelta(seconds=20)
        else:
            assert metadata["state"] == "attention" and metadata["next_attempt_at"] is None
        assert recover(system, operation, remote) is None
    assert remote.calls.count("upload") == 1


def test_manual_reconciliation_does_not_queue_unknown_upload_when_worker_is_absent(system, publication):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)
    remote.remote = None
    with pytest.raises(PublicationUncertain):
        reconcile_publication(
            system["repo"], system["admin"], operation["id"], "inspect-uncertain",
            "Conferir o resultado sem repetir o envio", lambda *_: remote,
        )
    assert result(system, publication, operation)["state"] == "reconciling"
    assert remote.calls.count("upload") == 1


def test_recovery_survives_process_restart_and_expired_reconciliation_lease(system, publication):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)
    with system["repo"].sessions.begin() as db:
        row = db.get(Operation, operation["id"])
        row.result = {**row.result, "publication_recovery": {"state": "running", "attempts": 1}}
        job = db.scalar(select(Job).where(Job.operation_id == operation["id"]))
        job.lease_token, job.lease_until = "interrupted-recovery", utcnow() - timedelta(seconds=1)
    remote.mode = "success"
    recover(system, operation, remote)
    current = result(system, publication, operation)
    assert current["state"] == "succeeded"
    assert current["result_ref"]["publication_recovery"]["attempts"] == 2
    assert remote.calls.count("upload") == 1


def test_concurrent_recoveries_have_one_remote_owner(system, publication):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)
    remote.mode = "success"
    entered, release = Event(), Event()
    verify = remote.verify

    def blocked_verify(url, revision):
        entered.set()
        assert release.wait(10)
        return verify(url, revision)

    remote.verify = blocked_verify
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(recover, system, operation, remote)
        try:
            assert entered.wait(10)
            assert pool.submit(recover, system, operation, remote).result(timeout=5) is None
        finally:
            release.set()
        assert first.result(timeout=10) == operation["id"]
    assert result(system, publication, operation)["state"] == "succeeded"
    assert remote.calls.count("enable") == 1 and remote.calls.count("verify") == 1


def test_lost_recovery_lease_cannot_write_remote_success(system, publication):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)

    def changed_lease(*args):
        with system["repo"].sessions.begin() as db:
            job = db.scalar(select(Job).where(Job.operation_id == operation["id"]))
            job.lease_token = "replacement-recovery"
        return {"passed": True}

    remote.verify = changed_lease
    recover(system, operation, remote)
    current = result(system, publication, operation)
    assert current["state"] == "reconciling" and "url" not in current["result_ref"]
    with system["repo"].sessions() as db:
        resource = db.get(PublicationResource, publication[3]["id"])
        assert resource.state != "published"


def expire_configuration(system, registry):
    with system["repo"].sessions.begin() as db:
        config = registry.latest(db, "cloudflare")
        config.validated_at = utcnow() - timedelta(days=2)
        validation = deepcopy(config.validation)
        report = validation["qualification"]["report"]
        report["tested_at"] = (utcnow() - timedelta(days=2)).isoformat()
        report["expires_at"] = (utcnow() - timedelta(days=1)).isoformat()
        validation["qualification"]["digest"] = canonical_hash(report)
        config.validation = validation


def test_expired_configuration_does_not_block_readback_of_already_sent_revision(system, publication):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)
    expire_configuration(system, publication[0])
    remote.mode = "success"
    recover(system, operation, remote)
    assert result(system, publication, operation)["state"] == "succeeded"
    assert remote.calls.count("upload") == 1


def test_expiry_during_accepted_build_does_not_abort_the_publication(system, publication):
    operation = start(system, publication)
    builder, remote = Builder(), Remote()
    build = builder.build

    def expiring_build(*args):
        expire_configuration(system, publication[0])
        return build(*args)

    builder.build = expiring_build
    claim(system["repo"], operation["id"])
    execute_publication(Worker(system["repo"], None, None), operation["id"], "lease", lambda *_: remote, builder)
    assert result(system, publication, operation)["state"] == "succeeded"


def test_revoked_configuration_fences_recovery_before_routing(system, publication):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)
    with system["repo"].sessions.begin() as db:
        publication[0].latest(db, "cloudflare").state = "disabled"
    recover(system, operation, remote)
    current = result(system, publication, operation)
    assert current["state"] == "reconciling" and "url" not in current["result_ref"]
    assert current["result_ref"]["publication_recovery"]["last_error_code"] == "CONFIGURATION_INVALID"
    assert "enable" not in remote.calls and "verify" not in remote.calls


def test_configuration_rotation_during_verify_does_not_commit_stale_success(system, publication):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)

    def revoke_during_verify(*args):
        with system["repo"].sessions.begin() as db:
            publication[0].latest(db, "cloudflare").state = "disabled"
        return {"passed": True}

    remote.verify = revoke_during_verify
    recover(system, operation, remote)
    assert result(system, publication, operation)["state"] == "reconciling"


@pytest.mark.parametrize("setting,value", [("restore_hold", True), ("lifecycle_enabled", False)])
def test_recovery_respects_platform_maintenance(system, publication, setting, value):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)
    setattr(system["config"], setting, value)
    assert recover(system, operation, remote) is None
    assert remote.calls.count("upload") == 1 and "verify" not in remote.calls


def test_mismatched_remote_database_never_enables_or_confirms_publication(system, publication):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)
    remote.db = {**remote.db, "uuid": "unrelated-database"}
    recover(system, operation, remote)
    current = result(system, publication, operation)
    assert current["state"] == "reconciling"
    assert current["result_ref"]["publication_recovery"]["state"] == "attention"
    assert "enable" not in remote.calls and "verify" not in remote.calls


def test_reconciliation_never_overwrites_conflicting_local_migration_receipt(system, publication):
    operation = start(system, publication)
    remote = Remote("migration_uncertain")
    execute(system, operation, remote)
    with system["repo"].sessions.begin() as db:
        resource = db.get(PublicationResource, publication[3]["id"])
        pending = resource.inventory["migration_in_progress"]
        resource.inventory = {**resource.inventory, "migrations": {pending["name"]: "a" * 64}}
        remote.receipts[pending["name"]] = pending["sha256"]
    recover(system, operation, remote)
    current = result(system, publication, operation)
    assert current["state"] == "reconciling"
    assert current["result_ref"]["publication_recovery"]["state"] == "attention"
    assert current["result_ref"]["publication_recovery"]["last_error_code"] == "MIGRATION_CHECKSUM_CONFLICT"
    with system["repo"].sessions() as db:
        resource = db.get(PublicationResource, publication[3]["id"])
        assert resource.inventory["migrations"][pending["name"]] == "a" * 64
        assert resource.inventory["migration_in_progress"] == pending


def test_existing_target_revision_cannot_hide_a_legacy_migration_conflict(system, publication):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)
    with system["repo"].sessions.begin() as db:
        row = db.get(Operation, operation["id"])
        row.error_code = "MIGRATION_CHECKSUM_CONFLICT"
        row.result = {
            "external_effect_pending": True,
            "publication_failure": {"code": "MIGRATION_CHECKSUM_CONFLICT", "stage": "publication_provisioning"},
        }
    recover(system, operation, remote)
    current = result(system, publication, operation)
    assert current["state"] == "reconciling" and "url" not in current["result_ref"]
    assert current["result_ref"]["publication_recovery"]["state"] == "attention"
    assert "enable" not in remote.calls and "verify" not in remote.calls
    with pytest.raises(RuleError) as caught:
        reconcile_publication(
            system["repo"], system["admin"], operation["id"], "inspect-ledger-conflict",
            "Conferir revisão existente sem ocultar conflito", lambda *_: remote,
        )
    assert caught.value.code == "MIGRATION_CHECKSUM_CONFLICT"


def test_manual_reconciliation_rechecks_revocation_before_persisting_success(system, publication):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)

    def revoke_during_verify(*args):
        with system["repo"].sessions.begin() as db:
            publication[0].latest(db, "cloudflare").state = "disabled"
        return {"passed": True}

    remote.verify = revoke_during_verify
    with pytest.raises(RuleError) as caught:
        reconcile_publication(
            system["repo"], system["admin"], operation["id"], "inspect-revoked",
            "Conferir publicação durante revogação", lambda *_: remote,
        )
    assert caught.value.code == "CONFIGURATION_INVALID"
    assert result(system, publication, operation)["state"] == "reconciling"
    with system["repo"].sessions() as db:
        assert db.get(PublicationResource, publication[3]["id"]).state != "published"


@pytest.mark.parametrize("change", ["configuration", "entitlement"])
def test_worker_rechecks_authorization_at_terminal_commit(system, publication, monkeypatch, change):
    from viracase.infrastructure import publication_worker

    operation = start(system, publication)
    remote = Remote()
    progress = publication_worker.record_progress
    changed = False

    def revoke_after_last_checkpoint(row, stage):
        nonlocal changed
        progress(row, stage)
        if stage == "publication_verified" and not changed:
            changed = True
            db = object_session(row)
            if change == "configuration":
                publication[0].latest(db, "cloudflare").state = "disabled"
            else:
                system["repo"].account(db, publication[2]).entitlement_version += 1

    monkeypatch.setattr(publication_worker, "record_progress", revoke_after_last_checkpoint)
    execute(system, operation, remote)
    current = result(system, publication, operation)
    assert changed and current["state"] == "reconciling"
    assert "url" not in current["result_ref"]
    with system["repo"].sessions() as db:
        resource = db.get(PublicationResource, publication[3]["id"])
        assert resource.state != "published" and not resource.inventory.get("url")


@pytest.mark.parametrize("change", ["missing_assets", "extra_binding"])
def test_recovery_requires_exact_authorized_bindings_before_enabling(system, publication, change):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)
    if change == "missing_assets":
        remote.remote["bindings"] = remote.remote["bindings"][:1]
    else:
        remote.remote["bindings"].append({"type": "service", "name": "UNEXPECTED", "service": "other"})
    recover(system, operation, remote)
    current = result(system, publication, operation)
    assert current["state"] == "reconciling" and "url" not in current["result_ref"]
    expected = "PUBLICATION_BINDINGS_NOT_READY" if change == "missing_assets" else "PUBLICATION_BINDINGS_MISMATCH"
    assert current["result_ref"]["publication_recovery"]["last_error_code"] == expected
    assert "enable" not in remote.calls and "verify" not in remote.calls


def test_manual_reconciliation_owns_lease_until_remote_result_is_committed(system, publication):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)
    remote.mode = "success"
    entered, release = Event(), Event()
    verify = remote.verify

    def blocked_verify(url, revision):
        entered.set()
        assert release.wait(10)
        return verify(url, revision)

    remote.verify = blocked_verify
    with ThreadPoolExecutor(max_workers=2) as pool:
        manual = pool.submit(
            reconcile_publication, system["repo"], system["admin"], operation["id"], "manual-lease",
            "Conferir resultado com uma única execução", lambda *_: remote,
        )
        try:
            assert entered.wait(10)
            assert pool.submit(recover, system, operation, remote).result(timeout=5) is None
        finally:
            release.set()
        assert manual.result(timeout=10)["state"] == "succeeded"
    assert remote.calls.count("verify") == 1 and remote.calls.count("upload") == 1


def test_remote_health_cannot_override_candidate_migration_checksum(system, publication):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)
    with system["repo"].sessions.begin() as db:
        resource = db.get(PublicationResource, publication[3]["id"])
        name = next(iter(resource.inventory["migrations"]))
        resource.inventory = {**resource.inventory, "migrations": {name: "0" * 64}}
    recover(system, operation, remote)
    current = result(system, publication, operation)
    assert current["state"] == "reconciling" and "url" not in current["result_ref"]
    assert current["result_ref"]["publication_recovery"]["last_error_code"] == "MIGRATION_CHECKSUM_CONFLICT"
    assert "enable" not in remote.calls and "verify" not in remote.calls


def test_retention_purge_waits_for_active_publication_reconciliation_lease(system, publication, monkeypatch):
    from viracase.infrastructure import publication_lifecycle

    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)
    with system["repo"].sessions.begin() as db:
        account = system["repo"].account(db, publication[2])
        period = system["repo"].period(db, account)
        period.starts_at = utcnow() - timedelta(days=33)
        period.expires_at = utcnow() - timedelta(days=3)
        period.purge_at = utcnow() - timedelta(days=1)
        job = db.scalar(select(Job).where(Job.operation_id == operation["id"]))
        job.lease_token, job.lease_until = "active-reconciliation", utcnow() + timedelta(minutes=5)

    def no_purge(*args):
        pytest.fail("An active reconciliation must finish before deleting its remote resources")

    monkeypatch.setattr(publication_lifecycle, "purge_publications", no_purge)
    Worker(system["repo"], None, None).purge(publication[2])
    with system["repo"].sessions() as db:
        assert system["repo"].account(db, publication[2]).lifecycle == "purging"
        assert db.get(PublicationResource, publication[3]["id"]) is not None


def test_unexpected_recovery_failure_releases_lease_and_sanitizes_diagnostics(system, publication, caplog):
    operation = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, operation, remote)

    def broken_adapter(*args):
        raise RuntimeError("synthetic-sensitive-provider-content")

    remote.settings = broken_adapter
    recover(system, operation, remote)
    current = result(system, publication, operation)
    metadata = current["result_ref"]["publication_recovery"]
    assert current["state"] == "reconciling" and metadata["state"] == "retry_wait"
    assert metadata["last_error_code"] == "PUBLICATION_RECOVERY_FAILED"
    assert "synthetic-sensitive-provider-content" not in str(current)
    assert "synthetic-sensitive-provider-content" not in caplog.text
    with system["repo"].sessions() as db:
        job = db.scalar(select(Job).where(Job.operation_id == operation["id"]))
        assert job.lease_token is None and job.lease_until is None
