import json
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_publication_diagnostics import diagnostics_client
from test_publication_pipeline import POLICY
from test_publication_pipeline import publication as publication_fixture
from test_publication_runtime import ProbeBuilder, ProbeRemote, verification

from viracase.application.verification_progress import begin_verification_attempt, record_verification_step
from viracase.domain.rules import utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway
from viracase.infrastructure.models import Audit, IntegrationConfig, Job, Operation
from viracase.infrastructure.observability import heartbeat
from viracase.infrastructure.publication import PublicationService
from viracase.infrastructure.publication_check import execute_check, resume_check
from viracase.infrastructure.publication_management import publication_diagnostics
from viracase.infrastructure.worker import Worker


@pytest.fixture
def publication(system, monkeypatch):
    return publication_fixture.__wrapped__(system, monkeypatch)


def live_worker(repo):
    heartbeat(
        repo,
        "worker:management-test",
        {"available": True, "fingerprint": "f" * 64, "stacks": POLICY["allowed_stacks"]},
    )


def scenario(system, publication, *, running=True):
    registry, repo = publication[0], system["repo"]
    config = registry.get("cloudflare")
    live_worker(repo)
    before = utcnow() - timedelta(minutes=4)
    failure = {
        "code": "PUBLICATION_API_NOT_READY",
        "message": "A API não confirmou HTTP e JSON.",
        "stage": "verification_remote",
        "stack": POLICY["allowed_stacks"][0],
        "at": before.isoformat(),
        "details": {
            "passed": False,
            "attempts": [
                {
                    "attempt": 1,
                    "passed": False,
                    "checks": [
                        {
                            "name": "api",
                            "path": "/api/health",
                            "passed": False,
                            "status": 200,
                            "code": "INVALID_JSON",
                            "observed_at": before.isoformat(),
                            "duration_ms": 12,
                        }
                    ],
                }
            ],
        },
    }
    with repo.sessions.begin() as db:
        previous = Operation(
            account_id=system["admin"],
            type="publication_check",
            state="failed",
            progress_stage="failed",
            error_code=failure["code"],
            payload={"connection_id": config["connection_id"]},
            policy={"config_id": config["id"]},
            created_at=before,
            updated_at=before,
            result={
                "verification_failure": failure,
                "verification_stack": failure["stack"],
                "resource_policy": "retain",
                "verification_finished_at": before.isoformat(),
                "verification_started_at": (before - timedelta(minutes=1)).isoformat(),
                "stack_results": [{"stack": failure["stack"], "passed": False, "failure": failure}],
            },
        )
        db.add(previous)
        db.flush()
        previous_id = previous.id
        current_id = None
        if running:
            current = Operation(
                account_id=system["admin"],
                type="publication_check",
                state="running",
                progress_stage="verification_remote",
                policy={"config_id": config["id"]},
                payload={"connection_id": config["connection_id"]},
                result={"resource_policy": "retain"},
            )
            db.add(current)
            db.flush()
            begin_verification_attempt(current)
            record_verification_step(current, "verification_isolation")
            record_verification_step(current, "verification_isolation", state="succeeded")
            record_verification_step(current, "verification_configuration")
            record_verification_step(current, "verification_configuration", state="succeeded")
            record_verification_step(current, "verification_remote", failure["stack"])
            current.result = {**current.result, "verification_stack": failure["stack"]}
            current_id = current.id
        row = db.get(IntegrationConfig, config["id"])
        row.validation = {
            **row.validation,
            "automatic_verification": {
                "state": "running" if running else "retry_wait",
                "operation_id": current_id or previous_id,
                "consecutive_failures": 3,
                "next_attempt_at": (utcnow() + timedelta(minutes=6)).isoformat(),
                "last_error_code": failure["code"],
                "executor_digest": "f" * 64,
            },
        }
    return config, current_id, previous_id


def read(system, identity):
    with diagnostics_client(system, system["admin"]) as client:
        response = client.get(f"/admin/integration-connections/{identity}/publication-diagnostics")
    assert response.status_code == 200
    return response.json()


def test_snapshot_is_additive_and_separates_running_from_previous_failure(system, publication):
    config, current, previous = scenario(system, publication)
    data = read(system, config["connection_id"])
    snap = data["snapshot"]
    assert snap["schema_version"] == 1
    assert snap["revision_order"] == "request_generation"
    assert snap["connection"]["id"] == config["connection_id"]
    assert snap["connection"]["configuration_version"] == config["version"]
    assert snap["current_run"]["id"] == current
    assert snap["previous_result"]["id"] == previous
    assert snap["current_run"]["error_code"] is None
    assert snap["previous_result"]["error_code"] == "PUBLICATION_API_NOT_READY"
    assert snap["publication"]["allowed"] == data["can_publish"] is False
    assert data["verification"]["id"] == current
    assert snap["profiles"][0]["verification_state"] == "running"
    assert snap["retry"]["scheduled"] is False
    assert snap["polling"] == {"active": True, "interval_ms": 3000}
    assert not next(action for action in snap["actions"] if action["key"] == "verify")["enabled"]


def test_only_explicit_step_outcomes_receive_success_and_http_200_can_fail_json(system, publication):
    config, _, _ = scenario(system, publication)
    snap = read(system, config["connection_id"])["snapshot"]
    steps = snap["current_run"]["steps"]
    assert [step["stage"] for step in steps if step["state"] == "succeeded"] == [
        "verification_isolation",
        "verification_configuration",
    ]
    assert next(step for step in steps if step["stage"] == "verification_uploading")["state"] == "not_started"
    probes = next(
        step for step in snap["previous_result"]["steps"] if step["stage"] == "verification_remote"
    )["probes"]
    assert probes[0]["observed_http_status"] == 200
    assert probes[0]["state"] == "failed"
    assert probes[0]["error_code"] == "INVALID_JSON"
    assert snap["previous_result"]["steps"][0]["state"] == "unknown"


def test_observations_have_no_remote_calls_writes_or_verification_side_effects(
    system, publication, monkeypatch, caplog
):
    config, _, _ = scenario(system, publication)
    repo = system["repo"]
    with repo.sessions() as db:
        before = [db.scalar(select(func.count()).select_from(model)) for model in (Operation, Job, Audit)]
        validation = deepcopy(db.get(IntegrationConfig, config["id"]).validation)

    def forbidden(*args, **kwargs):
        raise AssertionError("A leitura não pode consultar a Cloudflare")

    monkeypatch.setattr(CloudflareGateway, "__init__", forbidden)
    for _ in range(3):
        data = read(system, config["connection_id"])
        assert data["snapshot"]["publication"]["allowed"] == data["can_publish"]
    with repo.sessions() as db:
        after = [db.scalar(select(func.count()).select_from(model)) for model in (Operation, Job, Audit)]
        assert before == after
        assert db.get(IntegrationConfig, config["id"]).validation == validation
    logs = [
        json.loads(record.getMessage())
        for record in caplog.records
        if "publication_management_snapshot_read" in record.getMessage()
    ]
    assert len(logs) == 3
    assert all(
        entry["remote_calls"] == 0 and entry["database_queries"] > 0 and entry["payload_bytes"] > 0
        for entry in logs
    )


def test_retry_requires_persisted_scheduler_evidence_and_reports_count_scope(system, publication):
    config = publication[0].get("cloudflare")
    live_worker(system["repo"])
    snap = read(system, config["connection_id"])["snapshot"]
    assert snap["retry"]["scheduled"] is False
    assert snap["retry"]["next_attempt_at"] is None
    assert snap["retry"]["failure_count"] is None
    assert snap["current_run"] is None and snap["previous_result"] is None
    assert snap["profiles"][0]["verification_state"] == "not_started"
    scenario(system, publication, running=False)
    snap = read(system, config["connection_id"])["snapshot"]
    assert snap["retry"]["scheduled"] is True
    assert snap["retry"]["failure_count"] == 3
    assert snap["retry"]["count_scope"] == "consecutive_failures"
    assert snap["poll_after_ms"] == 15000


def test_account_credential_presence_and_formal_qualification_are_not_publication(system, publication):
    config = publication[0].get("cloudflare")
    live_worker(system["repo"])
    with system["repo"].sessions.begin() as db:
        row = db.get(IntegrationConfig, config["id"])
        row.validated_at = None
    snap = read(system, config["connection_id"])["snapshot"]
    assert snap["connection"]["active"] is True
    assert snap["credential"]["state"] == "unknown"
    assert snap["qualification"]["state"] == "not_started"
    assert snap["publication"]["allowed"] is False


def test_configuration_changes_mark_history_without_reusing_old_gate(system, publication):
    config, _, previous = scenario(system, publication, running=False)
    publication[0].save(
        system["admin"],
        "cloudflare",
        "new-config",
        config["version"],
        {
            "settings": {**config["settings"], "environment": "changed"},
            "reason": "Teste de isolamento de evidências entre versões",
        },
        config["connection_id"],
    )
    snap = read(system, config["connection_id"])["snapshot"]
    assert snap["connection"]["configuration_version"] > config["version"]
    assert snap["previous_result"]["id"] == previous
    assert snap["previous_result"]["configuration_version"] == config["version"]
    assert snap["previous_result"]["configuration_current"] is False
    assert snap["publication"]["allowed"] is False
    assert all(profile["verification_state"] == "not_started" for profile in snap["profiles"])


def test_unknown_error_and_diagnostics_are_sanitized_before_reaching_client(system, publication):
    config, _, previous = scenario(system, publication, running=False)
    with system["repo"].sessions.begin() as db:
        op = db.get(Operation, previous)
        failure = {
            **op.result["verification_failure"],
            "code": "UNKNOWN_PROVIDER_ERROR",
            "message": "Authorization: Bearer SECRET-SENTINEL token=PRIVATE-VALUE",
            "details": {
                "Authorization": "SECRET-SENTINEL",
                "cookie": "PRIVATE-COOKIE",
                "access_token": "PRIVATE-VALUE",
                "url": "https://user:password@example.test/path?token=PRIVATE-VALUE#PRIVATE-VALUE",
            },
        }
        op.result = {**op.result, "verification_failure": failure, "stack_results": []}
        op.error_code = "UNKNOWN_PROVIDER_ERROR"
    data = read(system, config["connection_id"])
    encoded = json.dumps(data)
    assert all(
        secret not in encoded
        for secret in ("SECRET-SENTINEL", "PRIVATE-COOKIE", "PRIVATE-VALUE", "user:password")
    )
    assert data["snapshot"]["previous_result"]["error_code"] == "UNKNOWN_PROVIDER_ERROR"
    assert (
        data["snapshot"]["previous_result"]["error_summary"]
        == "A verificação falhou; consulte o diagnóstico."
    )
    assert data["snapshot"]["previous_result"]["failure"]["details"]["url"] == "https://example.test/path"


def test_administrative_scope_is_enforced_before_snapshot_read(system, publication):
    config = publication[0].get("cloudflare")
    for account in system["users"]:
        with diagnostics_client(system, account) as client:
            response = client.get(
                f"/admin/integration-connections/{config['connection_id']}/publication-diagnostics"
            )
        assert response.status_code == 403
        assert "snapshot" not in response.text
    with diagnostics_client(system, system["admin"]) as client:
        response = client.get(
            "/admin/integration-connections/00000000-0000-0000-0000-000000000000/publication-diagnostics"
        )
    assert response.status_code == 404


def test_real_executor_instrumentation_records_confirmed_steps_and_distinct_resumed_attempts(
    system, publication
):
    repo = system["repo"]
    operation = verification(system, publication)
    remote = ProbeRemote()
    execute_check(Worker(repo, None, None), operation["id"], "lease", lambda *_: remote, ProbeBuilder())
    config = publication[0].get("cloudflare")
    first = read(system, config["connection_id"])["snapshot"]
    assert first["previous_result"]["state"] == "succeeded"
    assert all(step["state"] == "succeeded" for step in first["previous_result"]["steps"])
    with repo.sessions.begin() as db:
        op = db.get(Operation, operation["id"])
        op.state = "reconciling"
        job = db.scalar(select(Job).where(Job.operation_id == op.id))
        job.state = "reconciling"
    resume_check(repo, system["admin"], operation["id"], "resume-snapshot", "Retomar preservando a evidência")
    queued = read(system, config["connection_id"])["snapshot"]
    assert queued["current_run"]["id"] != first["previous_result"]["id"]
    assert queued["current_run"]["events"] == []
    assert all(step["state"] == "not_started" for step in queued["current_run"]["steps"])
    assert queued["previous_result"]["id"] == first["previous_result"]["id"]


def test_gate_equivalence_for_all_supported_profiles_and_expired_evidence(system, publication):
    config = publication[0].get("cloudflare")
    live_worker(system["repo"])
    data = read(system, config["connection_id"])
    with system["repo"].sessions() as db:
        row = db.get(IntegrationConfig, config["id"])
        service = PublicationService(system["repo"])
        assert data["snapshot"]["publication"]["allowed"] == (not service.configuration_reasons(row))
        for profile in data["snapshot"]["profiles"]:
            assert profile["publication"]["allowed"] == (
                not service.configuration_reasons(row, profile["id"])
            )
    assert [profile["id"] for profile in data["snapshot"]["profiles"]] == config["settings"]["allowed_stacks"]


def test_snapshot_uses_one_repeatable_database_view_during_concurrent_completion(
    system, publication, monkeypatch
):
    if system["engine"].dialect.name != "postgresql":
        pytest.skip("A consistência concorrente é verificada em PostgreSQL")
    config, current, previous = scenario(system, publication)
    import viracase.infrastructure.publication_management as management

    original = management.management_snapshot

    def complete_during_read(*args, **kwargs):
        with system["repo"].sessions.begin() as db:
            op = db.get(Operation, current)
            op.state, op.error_code = "failed", "PUBLICATION_API_NOT_READY"
        return original(*args, **kwargs)

    monkeypatch.setattr(management, "management_snapshot", complete_during_read)
    result = publication_diagnostics(system["repo"], system["admin"], config["connection_id"])
    assert result["verification"]["state"] == "running"
    assert result["snapshot"]["current_run"]["id"] == current
    assert result["snapshot"]["current_run"]["state"] == "running"
    assert result["snapshot"]["previous_result"]["id"] == previous


def test_legacy_resume_preserves_diagnostic_and_does_not_infer_finished_timestamp(system, publication):
    from viracase.application.verification_progress import queue_verification_attempt

    config, _, previous = scenario(system, publication, running=False)
    with system["repo"].sessions.begin() as db:
        op = db.get(Operation, previous)
        op.result = {
            key: value
            for key, value in op.result.items()
            if key not in {"verification_started_at", "verification_finished_at"}
        }
    old = read(system, config["connection_id"])["snapshot"]["previous_result"]
    assert old["finished_at"] is None
    assert old["duration_ms"] is None
    with system["repo"].sessions.begin() as db:
        op = db.get(Operation, previous)
        op.state, op.error_code = "reconciling", "VERIFICATION_RESUME_REQUIRED"
        queue_verification_attempt(op)
        op.state, op.error_code = "queued", None
    snap = read(system, config["connection_id"])["snapshot"]
    assert snap["current_run"]["id"] == previous + ":2"
    assert snap["current_run"]["error_code"] is None
    assert snap["current_run"]["events"] == []
    assert snap["previous_result"]["id"] == previous
    assert snap["previous_result"]["failure"]["code"] == "PUBLICATION_API_NOT_READY"
    assert snap["previous_result"]["error_code"] == "PUBLICATION_API_NOT_READY"
    assert snap["previous_result"]["state"] == "unknown"
    assert snap["previous_result"]["raw_state"] == "reconciling"
    assert snap["previous_result"]["interrupted"] is True


def test_formal_evidence_respects_publication_policy_block_flags(system, publication):
    from test_publication_pipeline import qualify

    config = publication[0].get("cloudflare")
    qualify(system, publication[0])
    live_worker(system["repo"])
    first = read(system, config["connection_id"])["snapshot"]
    assert first["profiles"][0]["evidence_state"] == "valid"
    assert first["publication"]["allowed"] is True
    for blocked in (
        {"publication_check_blocked": True},
        {"publication_check_blocked_stacks": POLICY["allowed_stacks"]},
    ):
        with system["repo"].sessions.begin() as db:
            row = db.get(IntegrationConfig, config["id"])
            row.validation = {
                key: value
                for key, value in row.validation.items()
                if key not in {"publication_check_blocked", "publication_check_blocked_stacks"}
            }
            row.validation = {**row.validation, **blocked}
        snap = read(system, config["connection_id"])["snapshot"]
        assert snap["profiles"][0]["evidence_state"] == "invalid"
        assert snap["publication"]["allowed"] is False
        assert snap["qualification"]["state"] == "valid"


def test_recovery_before_resource_creation_archives_attempt_without_fabricating_completion(
    system, publication
):
    config, current, _previous = scenario(system, publication)
    with system["repo"].sessions.begin() as db:
        op = db.get(Operation, current)
        op.result = {**op.result, "verification_stack": None}
        db.add(
            Job(
                operation_id=current,
                state="running",
                lease_token="expired-lease",
                lease_until=utcnow() - timedelta(seconds=1),
            )
        )
    Worker(system["repo"], None, None).recover()
    snap = read(system, config["connection_id"])["snapshot"]
    assert snap["current_run"]["id"] == current + ":2"
    assert snap["current_run"]["state"] == "queued"
    assert snap["current_run"]["events"] == []
    assert snap["previous_result"]["id"] == current
    assert snap["previous_result"]["state"] == "unknown"
    assert snap["previous_result"]["finished_at"] is None


def test_partial_snapshot_does_not_release_failed_profiles(system, publication):
    from test_publication_stack_readiness import HONO, PYTHON, partial_verification

    partial_verification(system, publication[0])
    config = publication[0].get("cloudflare")
    data = read(system, config["connection_id"])
    snap = data["snapshot"]
    assert snap["publication"]["state"] == "partial"
    assert snap["publication"]["allowed"] == data["can_publish"] is True
    profiles = {profile["id"]: profile for profile in snap["profiles"]}
    assert profiles[HONO]["publication"]["allowed"] is True
    assert profiles[HONO]["evidence_state"] == "valid"
    assert profiles[PYTHON]["publication"]["allowed"] is False
    assert profiles[PYTHON]["evidence_state"] == "invalid"


@pytest.mark.parametrize("changed,expected", [("expired", "expired"), ("executor", "invalid")])
def test_expired_or_incompatible_executor_evidence_is_never_current(system, publication, changed, expected):
    from viracase.domain.rules import canonical_hash

    operation = verification(system, publication)
    execute_check(
        Worker(system["repo"], None, None), operation["id"], "lease", lambda *_: ProbeRemote(), ProbeBuilder()
    )
    config = publication[0].get("cloudflare")
    if changed == "expired":
        with system["repo"].sessions.begin() as db:
            row = db.get(IntegrationConfig, config["id"])
            validation = deepcopy(row.validation)
            report = validation["runtime_verification"]["report"]
            report["expires_at"] = (utcnow() - timedelta(minutes=1)).isoformat()
            validation["runtime_verification"]["digest"] = canonical_hash(report)
            row.validation = validation
    else:
        heartbeat(
            system["repo"],
            "worker:test",
            {"available": True, "fingerprint": "c" * 64, "stacks": POLICY["allowed_stacks"]},
        )
    snap = read(system, config["connection_id"])["snapshot"]
    assert snap["publication"]["allowed"] is False
    assert snap["profiles"][0]["evidence_state"] == expected
    assert snap["previous_result"]["state"] == "succeeded"


def test_overdue_retry_does_not_invent_running_operation(system, publication):
    config, _, _ = scenario(system, publication, running=False)
    with system["repo"].sessions.begin() as db:
        row = db.get(IntegrationConfig, config["id"])
        maintenance = {
            **row.validation["automatic_verification"],
            "next_attempt_at": (utcnow() - timedelta(minutes=3)).isoformat(),
        }
        row.validation = {**row.validation, "automatic_verification": maintenance}
    snap = read(system, config["connection_id"])["snapshot"]
    assert snap["retry"]["scheduled"] is True
    assert snap["retry"]["overdue"] is True
    assert snap["current_run"] is None


def test_resumed_success_correlates_exact_attempt_without_reusing_older_operation_evidence(
    system, publication
):
    from test_publication_runtime import claim

    repo, remote = system["repo"], ProbeRemote()
    operation = verification(system, publication)
    execute_check(Worker(repo, None, None), operation["id"], "lease", lambda *_: remote, ProbeBuilder())
    config = publication[0].get("cloudflare")
    first = read(system, config["connection_id"])["snapshot"]
    assert first["profiles"][0]["evidence_run_id"] == first["previous_result"]["id"] == operation["id"]
    with repo.sessions.begin() as db:
        op = db.get(Operation, operation["id"])
        op.state = "reconciling"
        db.scalar(select(Job).where(Job.operation_id == op.id)).state = "reconciling"
    resume_check(
        repo,
        system["admin"],
        operation["id"],
        "correlated-resume",
        "Retomar mantendo histórico por tentativa",
    )
    second = read(system, config["connection_id"])["snapshot"]
    assert second["current_run"]["id"] == operation["id"] + ":2"
    assert second["profiles"][0]["evidence_run_id"] == operation["id"]
    assert second["profiles"][0]["evidence_run_id"] != second["current_run"]["id"]
    claim(repo, operation["id"])
    execute_check(Worker(repo, None, None), operation["id"], "lease", lambda *_: remote, ProbeBuilder())
    completed = read(system, config["connection_id"])["snapshot"]
    assert completed["current_run"] is None
    assert completed["profiles"][0]["evidence_state"] == "valid"
    assert (
        completed["profiles"][0]["evidence_run_id"]
        == completed["previous_result"]["id"]
        == operation["id"] + ":2"
    )
    assert completed["profiles"][0]["evidence_correlation"] == "attempt"
    with repo.sessions() as db:
        report = db.get(IntegrationConfig, config["id"]).validation["runtime_verification"]["report"]
        assert report["verification_attempt"] == 2
        assert report["run_id"] == operation["id"] + ":2"
    assert remote.calls.count("upload") == 1


def test_legacy_evidence_without_attempt_correlation_keeps_gate_but_does_not_claim_current_run(
    system, publication
):
    from viracase.domain.rules import canonical_hash

    operation = verification(system, publication)
    execute_check(
        Worker(system["repo"], None, None), operation["id"], "lease", lambda *_: ProbeRemote(), ProbeBuilder()
    )
    config = publication[0].get("cloudflare")
    with system["repo"].sessions.begin() as db:
        row = db.get(IntegrationConfig, config["id"])
        validation = deepcopy(row.validation)
        report = validation["runtime_verification"]["report"]
        report.pop("run_id")
        report.pop("verification_attempt")
        validation["runtime_verification"]["digest"] = canonical_hash(report)
        row.validation = validation
    snap = read(system, config["connection_id"])["snapshot"]
    assert snap["publication"]["allowed"] is True
    assert snap["profiles"][0]["evidence_state"] == "valid"
    assert snap["profiles"][0]["evidence_run_id"] is None
    assert snap["profiles"][0]["evidence_operation_id"] == operation["id"]
    assert snap["profiles"][0]["evidence_correlation"] == "unknown"


def test_legacy_sanitizer_preserves_integrity_hashes_and_redacts_credentials():
    from viracase.infrastructure.publication_management import sanitize

    original = {
        "checks": {"no_host_secrets": True},
        "migration": {"issues": [{"expected_sha256": "a" * 64, "actual_sha256": "b" * 64}]},
        "executor_digest": "c" * 64,
        "details": {"Authorization": "PRIVATE-VALUE", "stderr": "Synthetic build diagnostic"},
    }
    cleaned = sanitize(original)
    assert cleaned["migration"] == original["migration"]
    assert cleaned["checks"] == original["checks"]
    assert cleaned["executor_digest"] == original["executor_digest"]
    assert cleaned["details"] == {"stderr": "Synthetic build diagnostic"}
    assert sanitize({"expected_sha256": "Bearer PRIVATE-VALUE"})["expected_sha256"] == "Bearer [REDACTED]"
