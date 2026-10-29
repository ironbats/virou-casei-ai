from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_developer_recovery_acceptance import ACTION, CONSENT, stored
from test_developer_recovery_acceptance import clients as clients
from test_developer_recovery_acceptance import developer_case as developer_case

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, canonical_hash, utcnow
from viracase.infrastructure.models import (
    Account,
    Audit,
    IntegrationConfig,
    Job,
    Operation,
    PublicationResource,
)
from viracase.infrastructure.publication_developer_recovery import request_developer_recovery
from viracase.infrastructure.publication_recovery import recover_publications
from viracase.infrastructure.publication_resolution import recovery_snapshot


def accept(case, key="service-recovery"):
    snapshot = recovery_snapshot(case["repo"], case["actor"], case["operation"]["id"])
    payload = {
        "expected_state_version": snapshot["state_version"],
        "action": ACTION,
        "consent": CONSENT,
    }
    accepted = request_developer_recovery(case["repo"], case["actor"], case["operation"]["id"], key, payload)
    return accepted, payload


def run(case):
    return recover_publications(case["repo"], operation_id=case["operation"]["id"])


def assert_unresolved(case, before, reason):
    after = stored(case)
    assert after["operation_state"] == after["job_state"] == "reconciling"
    assert after["result"]["external_effect_pending"] is True
    assert after["result"]["publication_developer_recovery"]["state"] == "attention"
    assert after["result"]["publication_developer_recovery"]["reason_code"] == reason
    assert after["inventory"] == before["inventory"]
    assert after["result"]["publication_failure"] == before["result"]["publication_failure"]
    assert all(call[1].startswith("SELECT") for call in case["remote"].calls if call[0] == "query")


def test_configuration_changed_after_click_requires_new_contextual_command(developer_case):
    case = developer_case
    before = stored(case)
    accept(case)
    with case["repo"].sessions.begin() as db:
        cfg = db.scalar(select(IntegrationConfig).where(IntegrationConfig.provider == "cloudflare"))
        cfg.version += 1
    assert run(case) == case["operation"]["id"]
    assert_unresolved(case, before, "CONFIGURATION_CHANGED")
    assert not case["remote"].calls


def test_access_revocation_after_click_prevents_provider_queries(developer_case):
    case = developer_case
    before = stored(case)
    accept(case)
    with case["repo"].sessions.begin() as db:
        db.get(Account, case["actor"]).suspended = True
    run(case)
    assert_unresolved(case, before, "ACCESS_DENIED")
    assert not case["remote"].calls


def test_material_target_change_does_not_expand_original_consent(developer_case):
    case = developer_case
    before = stored(case)
    accept(case)
    with case["repo"].sessions.begin() as db:
        op = db.get(Operation, case["operation"]["id"])
        op.payload = {**op.payload, "revision_id": "unapproved-revision"}
    run(case)
    assert_unresolved(case, before, "ACCESS_DENIED")
    assert not case["remote"].calls


def test_expired_internal_plans_retry_boundedly_without_finalization(developer_case, monkeypatch):
    case = developer_case
    before = stored(case)
    accept(case)
    monkeypatch.setattr("viracase.infrastructure.publication_resolution.PLAN_VALIDITY", timedelta(seconds=-1))
    run(case)
    assert_unresolved(case, before, "PLAN_EXPIRED")
    request = stored(case)["result"]["publication_developer_recovery"]
    assert request["evaluation"] == 2
    assert len(request["previous_plans"]) == 2
    assert request["plan"] is None
    assert request["decision"]["eligible"] is False


def test_changing_remote_evidence_never_commits_an_old_plan(developer_case, monkeypatch):
    from viracase.infrastructure import publication_developer_proof

    case = developer_case
    before = stored(case)
    accept(case)
    evaluate = publication_developer_proof.evaluate_developer_no_effect
    calls = []

    def changing(context, assessment):
        decision = evaluate(context, assessment)
        calls.append(decision)
        return {**decision, "evidence_sha256": canonical_hash([decision["evidence_sha256"], len(calls)])}

    monkeypatch.setattr(publication_developer_proof, "evaluate_developer_no_effect", changing)
    run(case)
    assert len(calls) == 4
    assert all(item["eligible"] for item in calls)
    assert_unresolved(case, before, "EVIDENCE_CHANGED")


def test_lost_lease_between_remote_queries_prevents_old_worker_commit(developer_case, monkeypatch):
    case = developer_case
    before = stored(case)
    accept(case)
    query = case["remote"].query
    changed = []

    def steal_lease(database, sql, params=None):
        result = query(database, sql, params)
        if not changed:
            with case["repo"].sessions.begin() as db:
                job = db.scalar(select(Job).where(Job.operation_id == case["operation"]["id"]))
                job.lease_token = "different-worker"
            changed.append(True)
        return result

    monkeypatch.setattr(case["remote"], "query", steal_lease)
    run(case)
    after = stored(case)
    assert changed
    assert after["inventory"] == before["inventory"]
    assert after["operation_state"] == "reconciling"
    assert after["result"]["publication_developer_recovery"]["state"] == "running"
    with case["repo"].sessions() as db:
        job = db.scalar(select(Job).where(Job.operation_id == case["operation"]["id"]))
        assert job.lease_token == "different-worker"
    assert run(case) is None


class WorkerCrash(BaseException):
    pass


def test_worker_crash_before_commit_resumes_same_authorized_request(developer_case, monkeypatch):
    from viracase.infrastructure import publication_developer_recovery

    case = developer_case
    accepted, payload = accept(case)
    complete = publication_developer_recovery._complete

    def crash(*args, **kwargs):
        raise WorkerCrash()

    monkeypatch.setattr(publication_developer_recovery, "_complete", crash)
    with pytest.raises(WorkerCrash):
        run(case)
    incomplete = stored(case)
    assert incomplete["operation_state"] == "reconciling"
    assert incomplete["result"]["publication_developer_recovery"]["state"] == "resolving"
    with case["repo"].sessions.begin() as db:
        job = db.scalar(select(Job).where(Job.operation_id == case["operation"]["id"]))
        job.lease_until = utcnow() - timedelta(seconds=1)
    monkeypatch.setattr(publication_developer_recovery, "_complete", complete)
    run(case)
    result = request_developer_recovery(case["repo"], case["actor"], case["operation"]["id"], "service-recovery", payload)
    assert result["recovery_request_id"] == accepted["recovery_request_id"]
    assert result["request"]["state"] == "resolved"
    assert result["operation_state"] == "failed"
    with case["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "publication:developer_recovery:committed")) == 1


def test_worker_crash_after_commit_returns_persisted_result_without_repeating_effects(developer_case, monkeypatch):
    from viracase.infrastructure import publication_developer_recovery

    case = developer_case
    accepted, payload = accept(case)
    complete = publication_developer_recovery._complete

    def crash(*args, **kwargs):
        complete(*args, **kwargs)
        raise WorkerCrash()

    monkeypatch.setattr(publication_developer_recovery, "_complete", crash)
    with pytest.raises(WorkerCrash):
        run(case)
    remote_count = len(case["remote"].calls)
    assert run(case) is None
    result = request_developer_recovery(case["repo"], case["actor"], case["operation"]["id"], "service-recovery", payload)
    assert result["recovery_request_id"] == accepted["recovery_request_id"]
    assert result["recovery_state"] == "resolved"
    assert result["request"]["state"] == "resolved"
    assert len(case["remote"].calls) == remote_count


def test_finalization_error_rolls_back_operation_job_inventory_and_success_audit(developer_case, monkeypatch):
    from viracase.infrastructure import publication_developer_recovery, publication_resolution

    case = developer_case
    before = stored(case)
    accept(case)
    apply = publication_resolution.apply_resolution_result
    events = []

    def failing(*args, **kwargs):
        apply(*args, **kwargs)
        raise RuntimeError("synthetic commit interruption")

    monkeypatch.setattr(publication_resolution, "apply_resolution_result", failing)
    monkeypatch.setattr(publication_developer_recovery, "emit", lambda event, **fields: events.append(event))
    run(case)
    assert_unresolved(case, before, "REMOTE_UNAVAILABLE")
    with case["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "publication:developer_recovery:committed")) == 0
    assert "publication_developer_recovery_committed" not in events
    assert "publication_developer_recovery_readiness_updated" not in events


def test_second_command_and_manual_plan_cannot_dispute_accepted_request(developer_case, system):
    from viracase.infrastructure.publication_resolution import prepare_recovery_plan

    case = developer_case
    accept(case)
    with pytest.raises(RuleError) as denied:
        accept(case, key="second-request")
    assert denied.value.code == "OPERATION_BUSY"
    with pytest.raises(RuleError) as denied:
        prepare_recovery_plan(case["repo"], system["admin"], case["operation"]["id"], "admin-race", "Conferir tentativa pendente.")
    assert denied.value.code == "OPERATION_CONFLICT"
    assert not case["remote"].calls


def test_remote_failure_after_plan_invalidates_stale_eligible_proof(developer_case, monkeypatch):
    case = developer_case
    before = stored(case)
    accept(case)
    original = case["remote"].request
    observations = []

    def unavailable_on_revalidation(method, path):
        if path.endswith("/tokens/verify"):
            observations.append(path)
            if len(observations) == 2:
                raise RuleError("DEPENDENCY_UNAVAILABLE", "Synthetic transient unavailability", 503)
        return original(method, path)

    monkeypatch.setattr(case["remote"], "request", unavailable_on_revalidation)
    run(case)
    assert_unresolved(case, before, "REMOTE_UNAVAILABLE")
    request = stored(case)["result"]["publication_developer_recovery"]
    assert request["plan"] is None
    assert request["previous_plans"][0]["reason_code"] == "REMOTE_UNAVAILABLE"
    assert request["decision"]["eligible"] is False
    assert request["decision"]["proofs"] == {}
    assert request["decision"]["evidence_sha256"] is None
    public = recovery_snapshot(case["repo"], case["actor"], case["operation"]["id"])
    assert public["resolution"]["eligibility"] == "ineligible"
    assert public["resolution"]["evidence_hash"] is None


def test_admin_snapshot_retains_legacy_contract_during_public_recovery(developer_case, system):
    case = developer_case
    accept(case)
    snapshot = recovery_snapshot(case["repo"], system["admin"], case["operation"]["id"], admin=True)
    assert "resolution" not in snapshot
    assert "capabilities" not in snapshot
    assert "ACCESS_DENIED" not in str(snapshot)


def test_old_idempotency_key_returns_its_terminal_request_after_new_request(developer_case, monkeypatch):
    case = developer_case
    accepted, payload = accept(case)
    original = case["remote"].request

    def unavailable(*args):
        raise RuleError("DEPENDENCY_UNAVAILABLE", "Synthetic transient unavailability", 503)

    monkeypatch.setattr(case["remote"], "request", unavailable)
    run(case)
    monkeypatch.setattr(case["remote"], "request", original)
    with case["repo"].sessions.begin() as db:
        op = db.get(Operation, case["operation"]["id"])
        op.result = {
            **op.result,
            "publication_developer_recovery": {
                **op.result["publication_developer_recovery"],
                "requested_at": (utcnow() - timedelta(minutes=2)).isoformat(),
            },
        }
    second, _ = accept(case, key="second-request")
    assert second["request"]["state"] == "queued"
    prior = request_developer_recovery(case["repo"], case["actor"], case["operation"]["id"], "service-recovery", payload)
    assert prior["recovery_request_id"] == accepted["recovery_request_id"]
    assert prior["request"]["state"] == "attention"
    assert prior["request"]["reason_code"] == "REMOTE_UNAVAILABLE"


def test_later_external_resolution_updates_snapshot_without_rewriting_request_authorship(developer_case):
    case = developer_case
    accepted, _ = accept(case)
    with case["repo"].sessions.begin() as db:
        op = db.get(Operation, case["operation"]["id"])
        op.state = "failed"
        op.result = {
            **op.result,
            "publication_developer_recovery": {
                **op.result["publication_developer_recovery"],
                "state": "attention", "reason_code": "PROOF_INCOMPLETE",
            },
            "publication_recovery": {
                **op.result["publication_recovery"], "state": "resolved", "resolution": "migration_recovered",
            },
        }
        db.scalar(select(Job).where(Job.operation_id == op.id)).state = "done"
    snapshot = recovery_snapshot(case["repo"], case["actor"], case["operation"]["id"])
    assert snapshot["resolution"]["eligibility"] == "resolved"
    assert snapshot["resolution"]["reason_code"] == "RECOVERY_ALREADY_RESOLVED"
    assert snapshot["recovery_request_id"] == accepted["recovery_request_id"]
    assert snapshot["request"]["state"] == "attention"
    assert "outra resolução" in snapshot["summary"]
    assert "readiness" in snapshot
    assert snapshot["capabilities"]["can_check_and_resolve"] is False


@pytest.mark.parametrize("activity", [
    None, "publication_uploading", ["publication_uploading"],
    [{"stage": ["publication_uploading"]}], [{"stage": None}],
    [{"stage": "unrecognized_stage"}], [{"stage": "publication_migrating", "at": {}}],
])
def test_malformed_activity_is_rejected_before_admission_without_erasing_evidence(developer_case, activity):
    case = developer_case
    with case["repo"].sessions.begin() as db:
        op = db.get(Operation, case["operation"]["id"])
        op.result = {**op.result, "activity": activity}
    before = stored(case)
    snapshot = recovery_snapshot(case["repo"], case["actor"], case["operation"]["id"])
    assert snapshot["capabilities"]["can_check_and_resolve"] is False
    assert snapshot["resolution"]["reason_code"] == "PROOF_INCOMPLETE"
    assert "pre_upload_checkpoint" in snapshot["resolution"]["missing_evidence_codes"]
    with pytest.raises(RuleError) as denied:
        accept(case)
    assert denied.value.code == "PROOF_INCOMPLETE"
    assert stored(case) == before
    assert not case["remote"].calls


@pytest.mark.parametrize("field,value", [
    ("publication_attempt", None), ("publication_attempt", []),
    ("publication_failure", "publication_uploading"),
    ("publication_failure", {"stage": ["publication_uploading"]}),
    ("deployment_id", {}), ("verified_at", []),
])
def test_malformed_checkpoint_fields_do_not_enter_the_worker(developer_case, field, value):
    case = developer_case
    with case["repo"].sessions.begin() as db:
        op = db.get(Operation, case["operation"]["id"])
        op.result = {**op.result, field: value}
    before = stored(case)
    with pytest.raises(RuleError) as denied:
        accept(case)
    assert denied.value.code == "PROOF_INCOMPLETE"
    assert stored(case) == before


def upload_history():
    return [
        {"stage": "publication_uploading", "at": "2026-10-29T01:00:00+00:00"},
        *[
            {"stage": "publication_building" if index % 2 else "publication_migrating", "at": "2026-10-29T01:00:01+00:00"}
            for index in range(23)
        ],
    ]


def test_admission_preserves_upload_contradiction_that_progress_would_trim(developer_case):
    case = developer_case
    activity = upload_history()
    with case["repo"].sessions.begin() as db:
        op = db.get(Operation, case["operation"]["id"])
        op.result = {**op.result, "activity": activity}
    before = stored(case)
    with pytest.raises(RuleError) as denied:
        accept(case)
    assert denied.value.code == "PROOF_INCOMPLETE"
    assert stored(case) == before
    assert stored(case)["result"]["activity"][0]["stage"] == "publication_uploading"
    assert "publication_developer_recovery" not in stored(case)["result"]


@pytest.mark.parametrize("activity", [None, ["publication_uploading"], upload_history()])
def test_worker_preserves_malformed_or_upload_evidence_introduced_after_admission(developer_case, activity):
    case = developer_case
    accept(case)
    with case["repo"].sessions.begin() as db:
        op = db.get(Operation, case["operation"]["id"])
        op.result = {**op.result, "activity": activity}
    before = stored(case)
    assert run(case) == case["operation"]["id"]
    assert_unresolved(case, before, "PROOF_INCOMPLETE")
    assert stored(case)["result"]["activity"] == activity
    assert not case["remote"].calls


def test_normal_progress_does_not_change_the_bound_checkpoint(developer_case):
    from viracase.infrastructure.publication_developer_recovery import _current_binding

    case = developer_case
    accept(case)
    with case["repo"].sessions.begin() as db:
        op = db.get(Operation, case["operation"]["id"])
        bound = deepcopy(op.result["publication_developer_recovery"]["binding"])
        for index in range(40):
            record_progress(op, "publication_reconciling" if index % 2 else "publication_migrating")
        resource = db.get(PublicationResource, op.project_id)
        assert _current_binding(case["repo"], db, op, resource) == bound
        assert len(op.result["activity"]) == 24
        assert len(bound["publication_checkpoint_sha256"]) == 64
    run(case)
    assert stored(case)["result"]["publication_developer_recovery"]["state"] == "resolved"


def test_changed_checkpoint_hash_prevents_using_an_accepted_binding(developer_case):
    case = developer_case
    accept(case)
    with case["repo"].sessions.begin() as db:
        op = db.get(Operation, case["operation"]["id"])
        op.result = {
            **op.result,
            "publication_attempt": {**op.result["publication_attempt"], "checkpoint_reference": "different-evidence"},
        }
    before = stored(case)
    run(case)
    assert_unresolved(case, before, "EVIDENCE_CHANGED")
    assert not case["remote"].calls


def test_final_commit_rechecks_upload_history_without_trimming_it(developer_case, monkeypatch):
    from viracase.infrastructure import publication_developer_recovery

    case = developer_case
    accept(case)
    complete = publication_developer_recovery._complete
    activity = upload_history()

    def changed_before_commit(*args, **kwargs):
        with case["repo"].sessions.begin() as db:
            op = db.get(Operation, case["operation"]["id"])
            op.result = {**op.result, "activity": activity}
        return complete(*args, **kwargs)

    before = stored(case)
    monkeypatch.setattr(publication_developer_recovery, "_complete", changed_before_commit)
    run(case)
    assert_unresolved(case, before, "PROOF_INCOMPLETE")
    assert stored(case)["result"]["activity"] == activity
    assert stored(case)["result"]["publication_developer_recovery"]["plan"] is None


@pytest.mark.parametrize("activity", [
    upload_history(), None,
    [{"stage": None}, *upload_history()[1:]],
    [{"stage": "unrecognized_stage"}, *upload_history()[1:]],
])
def test_legacy_check_preserves_evidence_before_future_developer_resolution(developer_case, clients, activity):
    case = developer_case
    with case["repo"].sessions.begin() as db:
        op = db.get(Operation, case["operation"]["id"])
        op.result = {**op.result, "activity": activity}
    before = stored(case)
    client, headers = clients["owner"]
    path = f"/api/v1/operations/{case['operation']['id']}/publication-recovery"
    snapshot = client.get(path)
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["capabilities"]["can_check_and_resolve"] is False
    checked = client.post(path + "/check", headers={**headers, "Idempotency-Key": "preserve-evidence-check"}, json={})
    assert checked.status_code == 202, checked.text
    assert stored(case)["result"]["activity"] == activity
    assert run(case) == case["operation"]["id"]
    after = stored(case)
    assert after["operation_state"] == after["job_state"] == "reconciling"
    assert after["result"]["activity"] == activity
    assert after["result"]["publication_failure"] == before["result"]["publication_failure"]
    assert after["result"]["external_effect_pending"] is True
    assert after["inventory"]["migration_in_progress"]
    snapshot = client.get(path)
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["resolution"]["reason_code"] == "PROOF_INCOMPLETE"
    assert snapshot.json()["capabilities"]["can_check_and_resolve"] is False
    denied = client.post(
        path + "/resolve", headers={**headers, "Idempotency-Key": "must-preserve-evidence"},
        json={"expected_state_version": snapshot.json()["state_version"], "action": ACTION, "consent": CONSENT},
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "PROOF_INCOMPLETE"
    assert stored(case) == after
    assert all(call[1].startswith("SELECT") for call in case["remote"].calls if call[0] == "query")


@pytest.mark.parametrize("timing", ["queued", "after_final_proof"])
@pytest.mark.parametrize("location,field", [
    ("inventory", "pending_execution_units"), ("inventory", "migration_last_attempt"),
    ("inventory", "resource_effect_pending"),
    ("result", "pending_execution_units"), ("result", "migration_batch_pending"),
    ("result", "resource_effect_pending"), ("result", "upload_in_progress"),
    ("result", "receipt_write_started"), ("resolution", "receipt_write_started"),
])
def test_additional_effect_evidence_changes_invalidate_accepted_scope(developer_case, monkeypatch, timing, location, field):
    from viracase.infrastructure import publication_developer_proof

    case = developer_case
    accept(case)
    observations = []
    introduced = []

    def introduce_evidence():
        with case["repo"].sessions.begin() as db:
            op = db.get(Operation, case["operation"]["id"])
            value = {
                "operation_id": op.id, "name": "migrations/0006_uncertain.sql",
                "phase": "sql_sending", "effect_outcome": "unknown",
            } if field == "migration_last_attempt" else True
            if location == "inventory":
                resource = db.get(PublicationResource, op.project_id)
                resource.inventory = {**resource.inventory, field: value}
            elif location == "result":
                op.result = {**op.result, field: value}
            else:
                op.result = {
                    **op.result, "publication_resolution": {
                        **op.result.get("publication_resolution", {}), "attempt": {"receipt_write_started": True},
                    },
                }
        introduced.append(stored(case))

    if timing == "queued":
        introduce_evidence()
    else:
        evaluate = publication_developer_proof.evaluate_developer_no_effect

        def change_after_observation(context, assessment):
            decision = evaluate(context, assessment)
            assert decision["eligible"] is True
            observations.append(decision)
            if len(observations) == 2:
                introduce_evidence()
            return decision

        monkeypatch.setattr(publication_developer_proof, "evaluate_developer_no_effect", change_after_observation)
    assert run(case) == case["operation"]["id"]
    after = stored(case)
    if timing == "after_final_proof":
        assert len(observations) == 2
    else:
        assert not case["remote"].calls
    assert_unresolved(case, introduced[0], "EVIDENCE_CHANGED")
    if location == "inventory":
        assert after["inventory"][field]
    elif location == "result":
        assert after["result"][field] is True
    else:
        assert after["result"]["publication_resolution"]["attempt"]["receipt_write_started"] is True
    assert after["result"]["publication_developer_recovery"].get("plan") is None
