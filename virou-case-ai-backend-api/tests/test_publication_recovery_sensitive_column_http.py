from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_publication_pipeline import Builder
from test_publication_resolution_http import (
    MIGRATION,
    admin_path,
    authenticated_client,
    counts,
    owner_path,
    prepare_plan,
    resolution_payload,
)
import test_publication_resolution_http as recovery_support

from viracase.infrastructure.models import Account, Job, Operation, PublicationResource, Revision


SENSITIVE_SQL = (
    "ALTER TABLE project_settings ADD COLUMN salt TEXT NOT NULL DEFAULT 'test-placeholder';"
)


@pytest.fixture
def sensitive_case(system, monkeypatch):
    monkeypatch.setattr(recovery_support, "SQL", SENSITIVE_SQL)
    monkeypatch.setattr("viracase.infrastructure.publication.WorkerBuildStatus", lambda repo: Builder())
    fixture = recovery_support.recovery_case.__wrapped__(system, monkeypatch, SimpleNamespace(param=False))
    case = next(fixture)
    try:
        with case["repo"].sessions.begin() as db:
            old_revision = db.scalar(
                select(Revision)
                .where(Revision.project_id == case["project"]["id"])
                .order_by(Revision.created_at, Revision.id)
            )
            resource = db.get(PublicationResource, case["project"]["id"])
            case["previous_publication"] = {
                "url": "https://previous-verified.example.test",
                "published_revision_id": old_revision.id,
                "published_at": old_revision.created_at.isoformat(),
            }
            resource.inventory = {**resource.inventory, **case["previous_publication"]}
            operation = db.get(Operation, case["operation"]["id"])
            case["original_failure"] = deepcopy(operation.result["publication_failure"])
        yield case
    finally:
        case["remote"].connection.close()
        fixture.close()


@pytest.fixture
def clients(system):
    yield from recovery_support.clients.__wrapped__(system)


def unchanged_payload(plan):
    payload = resolution_payload(plan)
    payload["attestation"]["observed_outcome"] = "schema_unchanged"
    return payload


def assert_remote_read_only(case):
    assert case["remote"].receipt_count() == 0
    assert all(call[0] != "query" or call[1].startswith("SELECT") for call in case["remote"].calls)


def assert_original_state(case, *, resolved=False):
    with case["repo"].sessions() as db:
        operation = db.get(Operation, case["operation"]["id"])
        job = db.scalar(select(Job).where(Job.operation_id == operation.id))
        resource = db.get(PublicationResource, case["project"]["id"])
        assert operation.error_code == "PUBLICATION_REJECTED"
        assert operation.result["publication_failure"] == case["original_failure"]
        assert operation.state == ("failed" if resolved else "reconciling")
        assert job.state == ("done" if resolved else "reconciling")
        assert job.lease_token is None and job.lease_until is None
        assert {key: resource.inventory[key] for key in case["previous_publication"]} == case["previous_publication"]
        assert MIGRATION not in resource.inventory["migrations"]
        assert bool(resource.inventory.get("migration_in_progress")) is not resolved


def test_sensitive_absent_column_resolves_old_attempt_and_requires_explicit_current_publication(
    sensitive_case, clients
):
    case = sensitive_case
    repo, actor, project = case["repo"], case["actor"], case["project"]
    original_revision = project["current_revision_id"]
    files = repo.source_files(actor, project["id"], original_revision)
    files["README.md"] += "\nRevisão posterior da documentação de teste.\n"
    case["project"] = repo.save_revision(
        actor,
        project["id"],
        "sensitive-recovery-new-revision",
        {"base_revision_id": original_revision, "files": files},
        project["version"],
    )["project"]
    current_revision = case["project"]["current_revision_id"]
    assert current_revision != original_revision
    before, wallet = counts(case), repo.wallet(actor, True)
    owner, owner_headers = clients["owner"]
    readiness_path = f"/api/v1/projects/{project['id']}/publication-readiness"
    assert owner.get(readiness_path).json()["can_publish"] is False

    plan = prepare_plan(case, clients)
    assert plan["revision_id"] == original_revision
    assert plan["assessment"]["classification"] == "schema_unchanged"
    assert plan["assessment"]["checks"]["schema"] == "matches_baseline"
    assert plan["assessment"]["historical_provider_result_verified"] is False
    assert plan["assessment"]["statement_count"] == 1
    assert plan["action"] == "close_no_effect" and plan["can_resolve"] is True
    assert prepare_plan(case, clients) == plan
    snapshot = owner.get(owner_path(case))
    assert snapshot.status_code == 200
    assert snapshot.json()["classification"] == "schema_unchanged"
    assert clients["other"][0].get(owner_path(case)).status_code == 404

    client, headers = clients["admin"]
    payload = unchanged_payload(plan)
    response = recovery_support.write(
        client, headers, admin_path(case, "resolve-recovery"), payload, "sensitive-column-close"
    )
    assert response.status_code == 200, response.text
    repeated = recovery_support.write(
        client, headers, admin_path(case, "resolve-recovery"), payload, "sensitive-column-close"
    )
    assert repeated.status_code == 200 and repeated.json() == response.json()
    assert_original_state(case, resolved=True)
    assert_remote_read_only(case)
    assert counts(case) == before and repo.wallet(actor, True) == wallet
    with repo.sessions() as db:
        operation = db.get(Operation, case["operation"]["id"])
        recovery = operation.result["publication_recovery"]
        attempt = operation.result["publication_resolution"]["attempt"]
        assert recovery["state"] == "resolved" and recovery["resolution"] == "migration_recovered"
        assert attempt["state"] == "completed" and attempt["action"] == "close_no_effect"
        assert attempt["origin"] == "operator_attested_schema_postconditions"
        assert operation.result["external_effect_pending"] is False

    readiness = owner.get(readiness_path)
    assert readiness.status_code == 200 and readiness.json()["can_publish"] is True
    assert readiness.json()["publication"] == {
        "url": case["previous_publication"]["url"],
        "revision_id": case["previous_publication"]["published_revision_id"],
        "published_at": case["previous_publication"]["published_at"],
    }
    assert owner.get(owner_path(case)).json()["classification"] == "resolved"
    new_publication = recovery_support.write(
        owner,
        owner_headers,
        f"/api/v1/projects/{project['id']}/publications",
        {"revision_id": current_revision},
        "explicit-current-publication",
    )
    assert new_publication.status_code == 202, new_publication.text
    assert new_publication.json()["id"] != case["operation"]["id"]
    assert new_publication.json()["state"] == "queued"
    with repo.sessions() as db:
        new_operation = db.get(Operation, new_publication.json()["id"])
        assert new_operation.payload["revision_id"] == current_revision
    assert counts(case) == {**before, "Operation": before["Operation"] + 1, "Job": before["Job"] + 1}
    assert repo.wallet(actor, True) == wallet
    assert_original_state(case, resolved=True)
    assert_remote_read_only(case)


@pytest.mark.parametrize("action", ["recovery-plan", "resolve-recovery"])
@pytest.mark.parametrize("violation", ["owner_session", "csrf", "origin"])
def test_sensitive_column_recovery_keeps_administrative_session_and_csrf_boundaries(
    sensitive_case, clients, system, action, violation
):
    case = sensitive_case
    if action == "resolve-recovery":
        plan = prepare_plan(case, clients)
        assert plan["can_resolve"] is True
        payload = unchanged_payload(plan)
    else:
        payload = {"reason": "Conferir a coluna ausente sem alterar o banco remoto."}
    client, headers = clients["admin"]
    headers = dict(headers)
    if violation == "owner_session":
        client = clients["anonymous_admin"][0]
        client.cookies.set("vira_admin", clients["owner"][0].cookies.get("vira_student"))
        headers = {"Origin": system["config"].admin_origin}
    elif violation == "csrf":
        headers.pop("X-CSRF-Token")
    else:
        headers["Origin"] = "https://untrusted.example.test"
    before, calls = counts(case), list(case["remote"].calls)
    response = recovery_support.write(client, headers, admin_path(case, action), payload, "invalid-sensitive-session")
    assert response.status_code == (401 if violation == "owner_session" else 403), response.text
    assert counts(case) == before and case["remote"].calls == calls
    assert_original_state(case)
    assert_remote_read_only(case)


def test_sensitive_column_unchanged_plan_requires_complete_attestation(sensitive_case, clients):
    case = sensitive_case
    plan = prepare_plan(case, clients)
    assert plan["can_resolve"] is True
    payload = unchanged_payload(plan)
    payload.pop("attestation")
    client, headers = clients["admin"]
    response = recovery_support.write(
        client, headers, admin_path(case, "resolve-recovery"), payload, "missing-sensitive-attestation"
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "RECOVERY_ATTESTATION_REQUIRED"
    assert_original_state(case)
    assert_remote_read_only(case)


def test_sensitive_column_unchanged_plan_is_bound_to_preparing_administrator(sensitive_case, clients, system):
    case = sensitive_case
    plan = prepare_plan(case, clients)
    assert plan["can_resolve"] is True
    with case["repo"].sessions.begin() as db:
        original = db.get(Account, system["admin"])
        db.add(Account(
            login="sensitive-second-operator",
            name="Segundo operador de recuperação",
            role="admin",
            password_hash=original.password_hash,
        ))
    with ExitStack() as stack:
        client, headers = authenticated_client(system, stack, "sensitive-second-operator", admin=True)
        response = recovery_support.write(
            client, headers, admin_path(case, "resolve-recovery"), unchanged_payload(plan), "other-sensitive-operator"
        )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert_original_state(case)
    assert_remote_read_only(case)


@pytest.mark.parametrize("remote_change", [SENSITIVE_SQL, "CREATE TABLE unrelated_change(id INTEGER);"])
def test_sensitive_column_unchanged_plan_rechecks_remote_schema_before_closing(
    sensitive_case, clients, remote_change
):
    case = sensitive_case
    plan = prepare_plan(case, clients)
    assert plan["can_resolve"] is True
    case["remote"].connection.executescript(remote_change)
    case["remote"].connection.commit()
    client, headers = clients["admin"]
    response = recovery_support.write(
        client, headers, admin_path(case, "resolve-recovery"), unchanged_payload(plan), "stale-sensitive-schema"
    )
    assert response.status_code == 412, response.text
    assert response.json()["error"]["code"] == "RECOVERY_PLAN_STALE"
    assert_original_state(case)
    assert_remote_read_only(case)


def test_sensitive_column_already_present_cannot_be_adopted_by_structural_attestation(sensitive_case, clients):
    case = sensitive_case
    case["remote"].connection.executescript(SENSITIVE_SQL)
    case["remote"].connection.commit()
    plan = prepare_plan(case, clients)
    assert plan["assessment"]["checks"]["schema"] == "matches_expected"
    assert plan["assessment"]["classification"] == "unknown"
    assert plan["can_resolve"] is False and plan["action"] is None
    payload = unchanged_payload(plan)
    payload["action"] = "adopt_schema"
    payload["attestation"]["observed_outcome"] = "schema_applied"
    client, headers = clients["admin"]
    response = recovery_support.write(
        client, headers, admin_path(case, "resolve-recovery"), payload, "reject-sensitive-adoption"
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "RECOVERY_ACTION_INVALID"
    assert_original_state(case)
    assert_remote_read_only(case)


def test_sensitive_column_unchanged_plan_still_requires_proven_pre_upload_stage(sensitive_case, clients):
    case = sensitive_case
    with case["repo"].sessions.begin() as db:
        operation = db.get(Operation, case["operation"]["id"])
        result = deepcopy(operation.result)
        result.pop("publication_attempt")
        result["publication_failure"].pop("stage")
        operation.result = result
        case["original_failure"] = deepcopy(result["publication_failure"])
    plan = prepare_plan(case, clients)
    assert plan["assessment"]["classification"] == "schema_unchanged"
    assert plan["can_resolve"] is False and plan["action"] is None
    assert plan["blocked_reason"]
    payload = unchanged_payload(plan)
    payload["action"] = "close_no_effect"
    client, headers = clients["admin"]
    response = recovery_support.write(
        client, headers, admin_path(case, "resolve-recovery"), payload, "unknown-sensitive-upload-stage"
    )
    assert response.status_code == 412, response.text
    assert_original_state(case)
    assert_remote_read_only(case)
