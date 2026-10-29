from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from threading import Barrier, Event, get_ident

import pytest
from sqlalchemy import event, func, select
from test_publication_diagnostics import diagnostics_client
from test_publication_runtime import ProbeBuilder, ProbeRemote, claim
from test_publication_runtime import publication as publication_fixture

from viracase.domain.rules import RuleError, aware, canonical_hash, utcnow
from viracase.infrastructure import runtime_monitor
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.models import Job, Operation
from viracase.infrastructure.observability import heartbeat
from viracase.infrastructure.publication import PublicationService
from viracase.infrastructure.publication_check import execute_check, request_check
from viracase.infrastructure.publication_maintenance import schedule_automatic_verification
from viracase.infrastructure.worker import Worker


@pytest.fixture
def publication(system, monkeypatch):
    return publication_fixture.__wrapped__(system, monkeypatch)


def ready_worker(repo, fingerprint="f" * 64):
    heartbeat(
        repo,
        "worker:automatic-test",
        {
            "available": True,
            "fingerprint": fingerprint,
            "stacks": ["hono-typescript-d1"],
        },
    )


def run_check(system, identity, remote=None, builder=None):
    repo = system["repo"]
    claim(repo, identity)
    remote = remote or ProbeRemote()
    execute_check(Worker(repo, None, None), identity, "lease", lambda *_: remote, builder or ProbeBuilder())
    return remote


def first_success(system, publication):
    ready_worker(system["repo"])
    identity = schedule_automatic_verification(system["repo"])
    assert identity
    return identity, run_check(system, identity)


def make_due(system, registry, *, expired=False):
    now = utcnow()
    with system["repo"].sessions.begin() as db:
        row = registry.latest(db, "cloudflare")
        row.validated_at = now - timedelta(hours=25 if expired else 23, minutes=30)
        validation = deepcopy(row.validation)
        report = validation["runtime_verification"]["report"]
        report["tested_at"] = (now - timedelta(hours=24)).isoformat()
        report["expires_at"] = (now + timedelta(minutes=-1 if expired else 30)).isoformat()
        validation["runtime_verification"]["digest"] = canonical_hash(report)
        row.validation = validation


def retry_now(system, registry):
    with system["repo"].sessions.begin() as db:
        row = registry.latest(db, "cloudflare")
        row.validation = {
            **row.validation,
            "automatic_verification": {
                **row.validation["automatic_verification"],
                "next_attempt_at": (utcnow() - timedelta(seconds=1)).isoformat(),
            },
        }


def rejected_remote(code):
    remote = ProbeRemote()
    remote.validate = lambda: {"ready": False, "code": code, "message": "Synthetic failure"}
    return remote


def test_active_connection_queues_first_check_without_manual_request(system, publication):
    repo, registry = system["repo"], publication[0]
    ready_worker(repo)
    identity = schedule_automatic_verification(repo)
    assert identity and schedule_automatic_verification(repo) is None
    with repo.sessions() as db:
        operation = db.get(Operation, identity)
        assert operation.payload["automatic"] is True
        assert operation.account_id == system["admin"]
        assert operation.policy["config_id"] == registry.get("cloudflare")["id"]
        assert db.scalar(select(func.count()).select_from(Job)) == 1
    run_check(system, identity)
    status = PublicationService(repo).readiness(publication[2], publication[3]["id"])
    assert status["can_publish"]
    assert status["automatic_verification"]["state"] == "scheduled"
    assert status["automatic_verification"]["last_succeeded_at"]
    assert "operation_id" not in status["automatic_verification"]
    assert schedule_automatic_verification(repo) is None


@pytest.mark.parametrize("expired", [False, True])
def test_renewal_refreshes_both_deadlines_and_reuses_resources(system, publication, expired):
    repo, registry = system["repo"], publication[0]
    _, remote = first_success(system, publication)
    make_due(system, registry, expired=expired)
    before = registry.get("cloudflare")
    readiness = PublicationService(repo).readiness(publication[2], publication[3]["id"])
    assert readiness["can_publish"] is not expired
    identity = schedule_automatic_verification(repo)
    assert identity
    assert (
        PublicationService(repo).readiness(publication[2], publication[3]["id"])["can_publish"] is not expired
    )
    run_check(system, identity, remote)
    after = registry.get("cloudflare")
    assert datetime.fromisoformat(after["validated_at"]) > datetime.fromisoformat(before["validated_at"])
    assert after["validation"]["runtime_verification"] != before["validation"]["runtime_verification"]
    assert PublicationService(repo).readiness(publication[2], publication[3]["id"])["can_publish"]
    assert remote.calls.count("create_database") == 1
    assert remote.calls.count("upload") == 2
    assert "delete" not in remote.calls and "disable" not in remote.calls


@pytest.mark.parametrize("code", ["DEPENDENCY_UNAVAILABLE", "QUOTA_EXCEEDED", "PUBLICATION_RESULT_UNKNOWN"])
def test_transient_failure_preserves_unexpired_evidence_and_applies_backoff(system, publication, code):
    repo, registry = system["repo"], publication[0]
    first_success(system, publication)
    make_due(system, registry)
    before = registry.get("cloudflare")
    identity = schedule_automatic_verification(repo)
    with pytest.raises(RuleError) as caught:
        run_check(system, identity, rejected_remote(code))
    assert caught.value.code == code
    after = registry.get("cloudflare")
    assert after["validated_at"] == before["validated_at"]
    assert after["validation"]["runtime_verification"] == before["validation"]["runtime_verification"]
    maintenance = after["validation"]["automatic_verification"]
    assert maintenance["state"] == "retry_wait" and maintenance["consecutive_failures"] == 1
    assert datetime.fromisoformat(maintenance["next_attempt_at"]) > utcnow() + timedelta(seconds=50)
    assert maintenance["last_error_code"] == code
    assert schedule_automatic_verification(repo) is None
    assert PublicationService(repo).readiness(publication[2], publication[3]["id"])["can_publish"]
    make_due(system, registry, expired=True)
    assert not PublicationService(repo).readiness(publication[2], publication[3]["id"])["can_publish"]
    assert schedule_automatic_verification(repo) is None


def test_invalid_token_blocks_previous_release_and_recovers_automatically(system, publication):
    repo, registry = system["repo"], publication[0]
    _, remote = first_success(system, publication)
    make_due(system, registry)
    identity = schedule_automatic_verification(repo)
    with pytest.raises(RuleError):
        run_check(system, identity, rejected_remote("CONFIGURATION_INVALID"))
    saved = registry.get("cloudflare")
    assert saved["state"] == "active"
    assert saved["validation"]["ready"] is False
    assert saved["validation"]["publication_check_blocked"] is True
    assert not PublicationService(repo).readiness(publication[2], publication[3]["id"])["can_publish"]
    retry_now(system, registry)
    retry = schedule_automatic_verification(repo)
    assert retry and retry != identity
    run_check(system, retry, remote)
    assert PublicationService(repo).readiness(publication[2], publication[3]["id"])["can_publish"]
    maintenance = registry.get("cloudflare")["validation"]["automatic_verification"]
    assert maintenance["consecutive_failures"] == 0 and maintenance["last_error_code"] is None


def test_failed_isolation_blocks_release_without_contacting_cloudflare(system, publication):
    repo, registry = system["repo"], publication[0]
    first_success(system, publication)
    make_due(system, registry)
    identity = schedule_automatic_verification(repo)
    builder, remote = ProbeBuilder(), ProbeRemote()
    builder.verify_isolation = lambda: {"no_network": False}
    with pytest.raises(RuleError):
        run_check(system, identity, remote, builder)
    assert remote.calls == []
    assert not PublicationService(repo).readiness(publication[2], publication[3]["id"])["can_publish"]


def test_runtime_change_requires_new_success_without_reusing_old_proof(system, publication):
    repo = system["repo"]
    _, remote = first_success(system, publication)
    ready_worker(repo, "e" * 64)
    assert not PublicationService(repo).readiness(publication[2], publication[3]["id"])["can_publish"]
    identity = schedule_automatic_verification(repo)
    builder = ProbeBuilder()
    builder.fingerprint = lambda: "e" * 64
    run_check(system, identity, remote, builder)
    assert PublicationService(repo).readiness(publication[2], publication[3]["id"])["can_publish"]


def test_automatic_backoff_increases_and_never_exceeds_one_hour(system, publication):
    repo, registry = system["repo"], publication[0]
    ready_worker(repo)
    for attempt in range(1, 9):
        if attempt > 1:
            retry_now(system, registry)
        identity = schedule_automatic_verification(repo)
        with pytest.raises(RuleError):
            run_check(system, identity, rejected_remote("DEPENDENCY_UNAVAILABLE"))
        status = registry.get("cloudflare")["validation"]["automatic_verification"]
        delay = (datetime.fromisoformat(status["next_attempt_at"]) - utcnow()).total_seconds()
        expected = min(3600, 60 * 2 ** (attempt - 1))
        assert expected - 10 < delay <= expected
        assert status["consecutive_failures"] == attempt
        assert schedule_automatic_verification(repo) is None


@pytest.mark.parametrize("state", ["draft", "invalid", "disabled"])
def test_unapproved_or_disabled_configuration_is_not_automatically_activated(system, publication, state):
    repo, registry = system["repo"], publication[0]
    ready_worker(repo)
    with repo.sessions.begin() as db:
        registry.latest(db, "cloudflare").state = state
    assert schedule_automatic_verification(repo) is None
    assert registry.get("cloudflare")["state"] == state


@pytest.mark.parametrize("setting,value", [("restore_hold", True), ("lifecycle_enabled", False)])
def test_maintenance_does_not_schedule_when_lifecycle_is_locked(system, publication, setting, value):
    ready_worker(system["repo"])
    setattr(system["config"], setting, value)
    assert schedule_automatic_verification(system["repo"]) is None


def test_scheduler_requires_healthy_homogeneous_workers(system, publication):
    repo = system["repo"]
    assert schedule_automatic_verification(repo) is None
    ready_worker(repo)
    heartbeat(repo, "worker:different", {"available": True, "fingerprint": "e" * 64})
    assert schedule_automatic_verification(repo) is None


def test_rotation_cancels_queued_old_check_without_qualifying_new_credentials(system, publication):
    repo, registry = system["repo"], publication[0]
    ready_worker(repo)
    identity = schedule_automatic_verification(repo)
    before = registry.get("cloudflare")
    registry.save(
        system["admin"],
        "cloudflare",
        "rotate-automatic",
        before["version"],
        {
            "settings": before["settings"],
            "token": "new-synthetic-credential",
            "reason": "Rotação de credenciais durante a espera",
        },
    )
    assert schedule_automatic_verification(repo) is None
    with repo.sessions() as db:
        assert db.get(Operation, identity).state == "cancelled"
        assert db.scalar(select(Job).where(Job.operation_id == identity)).state == "done"
    after = registry.get("cloudflare")
    assert after["state"] == "draft" and after["validation"] == {}
    assert not PublicationService(repo).readiness(publication[2], publication[3]["id"])["can_publish"]


def test_restarted_automatic_check_is_resumed_with_same_resources_and_backoff(system, publication):
    repo = system["repo"]
    ready_worker(repo)
    identity = schedule_automatic_verification(repo)
    claim(repo, identity)
    with repo.sessions.begin() as db:
        db.get(Operation, identity).result = {"creation_started": True}
        db.scalar(select(Job).where(Job.operation_id == identity)).lease_until = utcnow() - timedelta(
            seconds=1
        )
    Worker(repo, None, None).recover()
    assert schedule_automatic_verification(repo) is None
    with repo.sessions() as db:
        assert db.get(Operation, identity).state == "queued"
        job = db.scalar(select(Job).where(Job.operation_id == identity))
        assert job.state == "queued" and job.lease_token is None
        assert aware(job.available_at) > utcnow() + timedelta(seconds=50)
        assert (
            db.scalar(
                select(func.count()).select_from(Operation).where(Operation.type == "publication_check")
            )
            == 1
        )


def test_revalidation_preserves_automatic_state(system, publication):
    registry = publication[0]
    first_success(system, publication)
    before = registry.get("cloudflare")["validation"]["automatic_verification"]
    registry.validate(system["admin"], "cloudflare", 1, "Revalidação manual compatível")
    assert registry.get("cloudflare")["validation"]["automatic_verification"] == before


def test_diagnostics_exposes_read_only_automatic_state_to_admin(system, publication):
    repo, registry = system["repo"], publication[0]
    ready_worker(repo)
    config = registry.get("cloudflare")
    path = "/admin/integration-connections/" + config["connection_id"] + "/publication-diagnostics"
    with diagnostics_client(system, system["admin"]) as client:
        for _ in range(3):
            response = client.get(path)
            assert response.status_code == 200
            assert response.json()["automatic_verification"]["enabled"] is True
            assert response.json()["automatic_verification"]["state"] == "scheduled"
    with repo.sessions() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(Operation).where(Operation.type == "publication_check")
            )
            == 0
        )
    with diagnostics_client(system, publication[2]) as client:
        assert client.get(path).status_code == 403


def test_runtime_monitor_schedules_checks_after_heartbeat(system, publication, monkeypatch):
    class Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            self.stopped = True

    class Build:
        def diagnostics(self):
            return {"available": True, "fingerprint": "f" * 64, "stacks": ["hono-typescript-d1"]}

    monkeypatch.setattr(runtime_monitor, "IsolatedBuild", Build)
    thread = runtime_monitor.start_monitor(system["repo"], Stop())
    thread.join(5)
    assert not thread.is_alive()
    with system["repo"].sessions() as db:
        operation = db.scalar(select(Operation).where(Operation.type == "publication_check"))
        assert operation and operation.payload["automatic"] is True


def test_concurrent_monitors_enqueue_only_one_verification(system, publication):
    if system["engine"].dialect.name != "postgresql":
        pytest.skip("A trava distribuída usa PostgreSQL.")
    repo = system["repo"]
    ready_worker(repo)
    barrier = Barrier(2)

    def run():
        barrier.wait(timeout=5)
        return schedule_automatic_verification(repo)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert len([value for value in results if value]) == 1
    with repo.sessions() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(Operation).where(Operation.type == "publication_check")
            )
            == 1
        )


def test_manual_success_clears_automatic_retry_state(system, publication):
    repo, registry = system["repo"], publication[0]
    _, remote = first_success(system, publication)
    make_due(system, registry)
    identity = schedule_automatic_verification(repo)
    with pytest.raises(RuleError):
        run_check(system, identity, rejected_remote("DEPENDENCY_UNAVAILABLE"))
    config = registry.get("cloudflare")
    manual = request_check(
        repo,
        system["admin"],
        config["connection_id"],
        "manual-after-auto",
        config["version"],
        "Verificar recuperação manualmente",
    )
    run_check(system, manual["id"], remote)
    maintenance = registry.get("cloudflare")["validation"]["automatic_verification"]
    assert maintenance["state"] == "scheduled"
    assert maintenance["last_error_code"] is None and maintenance["consecutive_failures"] == 0
    assert schedule_automatic_verification(repo) is None


def test_lost_lease_never_marks_automatic_verification_as_succeeded(system, publication):
    repo, registry = system["repo"], publication[0]
    ready_worker(repo)
    identity = schedule_automatic_verification(repo)
    claim(repo, identity)
    with repo.sessions.begin() as db:
        operation = db.get(Operation, identity)
        operation.state = "reconciling"
        job = db.scalar(select(Job).where(Job.operation_id == identity))
        job.state, job.lease_token = "reconciling", None
    remote = ProbeRemote()
    execute_check(Worker(repo, None, None), identity, "lease", lambda *_: remote, ProbeBuilder())
    assert remote.calls == []
    assert not registry.get("cloudflare")["validation"].get("runtime_verification")
    assert not registry.get("cloudflare")["validation"]["automatic_verification"].get("last_succeeded_at")
    with repo.sessions() as db:
        assert db.get(Operation, identity).state == "reconciling"


def test_disabled_connection_fences_external_requests_in_progress(system, publication):
    repo, registry = system["repo"], publication[0]
    ready_worker(repo)
    identity = schedule_automatic_verification(repo)
    remote = ProbeRemote()
    validate = remote.validate

    def disable_during_validation():
        with repo.sessions.begin() as db:
            registry.latest(db, "cloudflare").state = "disabled"
        remote.before_request("GET")
        return validate()

    remote.validate = disable_during_validation
    with pytest.raises(RuleError) as caught:
        run_check(system, identity, remote)
    assert caught.value.code == "VERSION_CONFLICT"
    assert remote.calls == []
    assert registry.get("cloudflare")["state"] == "disabled"
    assert not registry.get("cloudflare")["validation"].get("runtime_verification")


def test_expired_readiness_describes_automatic_recovery_without_manual_instruction(system, publication):
    repo, registry = system["repo"], publication[0]
    first_success(system, publication)
    make_due(system, registry, expired=True)
    schedule_automatic_verification(repo)
    status = PublicationService(repo).readiness(publication[2], publication[3]["id"])
    assert not status["can_publish"]
    assert all("automática" in reason["message"] for reason in status["reasons"])
    assert all("backoffice" not in reason["message"] for reason in status["reasons"])


def test_scheduler_does_not_deadlock_with_administrator_holding_its_creator_account(system, publication):
    if system["engine"].dialect.name != "postgresql":
        pytest.skip("O cenário valida a ordem de locks e a FK no PostgreSQL.")
    repo, engine = system["repo"], system["engine"]
    ready_worker(repo)
    administrator_locked, scheduler_waiting = Event(), Event()
    scheduler_thread = [None]

    def observe(connection, cursor, statement, parameters, context, executemany):
        sql = statement.lower()
        if get_ident() == scheduler_thread[0] and (
            "insert into operations" in sql or "from accounts" in sql and "for update" in sql
        ):
            scheduler_waiting.set()

    def administrator():
        with repo.sessions.begin() as db:
            repo.account(db, system["admin"], True)
            administrator_locked.set()
            assert scheduler_waiting.wait(10)
            configuration_lock(db, "integration:cloudflare")
        return True

    def scheduler():
        assert administrator_locked.wait(10)
        scheduler_thread[0] = get_ident()
        return schedule_automatic_verification(repo)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admin_result = pool.submit(administrator)
            scheduled = pool.submit(scheduler)
            assert admin_result.result(timeout=20)
            assert scheduled.result(timeout=20)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
