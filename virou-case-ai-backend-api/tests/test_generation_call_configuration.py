import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from test_conversation_runtime import login
from test_guided_build import guided, save_request, send_request

from viracase.application.generation import GenerationService
from viracase.domain.rules import RuleError
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import IntegrationConfig, Operation, Reservation
from viracase.infrastructure.workflow_logging import safe_fields
from viracase.interfaces.api import create_app
from viracase.interfaces.feature_schemas import BuildPolicy

__all__ = ["guided"]


def dependencies(system):
    return system["config"], system["engine"], system["repo"], system["catalog"], None


def save_policy(system, settings):
    return Integrations(system["repo"]).save(
        system["admin"],
        "jev",
        "generation-call-policy",
        0,
        {"settings": settings, "reason": "Dimensionar as chamadas da construção"},
    )


def test_stored_policy_without_new_field_defaults_to_ten_without_rewriting_data(system):
    registry = Integrations(system["repo"])
    assert registry.build_policy()["max_generation_calls"] == 10
    saved = save_policy(system, {"max_context_bytes": 12000})
    with system["repo"].sessions.begin() as db:
        row = db.get(IntegrationConfig, saved["id"])
        legacy = {key: value for key, value in row.settings.items() if key != "max_generation_calls"}
        row.settings = legacy
    current = registry.build_policy()
    assert current["max_generation_calls"] == 10
    assert current["max_context_bytes"] == 12000
    assert current["max_generation_context_bytes"] == 252288
    with system["repo"].sessions() as db:
        assert db.get(IntegrationConfig, saved["id"]).settings == legacy


@pytest.mark.parametrize("value", [1, 4, 10])
def test_administrative_call_limit_accepts_only_supported_integer_boundaries(value):
    assert BuildPolicy(max_generation_calls=value).max_generation_calls == value


@pytest.mark.parametrize("value", [0, 11, True, False, 1.5, "10", None])
def test_administrative_call_limit_rejects_unsupported_values(value):
    with pytest.raises(ValidationError):
        BuildPolicy(max_generation_calls=value)


def test_existing_admin_route_persists_and_versions_call_limit_with_csrf(system):
    client = TestClient(create_app(True, dependencies(system)))
    path = "/admin-api/v1/integrations/jev"
    assert client.get(path).status_code == 401
    client.headers.update({"Origin": system["config"].admin_origin})
    signed = client.post(
        "/admin-api/v1/auth/login",
        json={"login": "operator", "password": "test-operator-password"},
    )
    assert signed.status_code == 200
    assert client.get(path).json()["defaults"]["max_generation_calls"] == 10
    payload = {"settings": {"max_generation_calls": 7}, "reason": "Limite configurável de construção"}
    headers = {"If-Match": "0", "Idempotency-Key": "save-generation-limit"}
    assert client.put(path, headers=headers, json=payload).status_code == 403
    client.headers.update({"X-CSRF-Token": signed.json()["csrf_token"]})
    response = client.put(path, headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["max_generation_calls"] == 7
    assert client.put(path, headers=headers, json=payload).json() == response.json()
    current = client.get(path).json()
    assert current["settings"]["max_generation_calls"] == 7
    stale = client.put(
        path,
        headers={"If-Match": "0", "Idempotency-Key": "stale-generation-limit"},
        json={**payload, "settings": {"max_generation_calls": 6}},
    )
    assert stale.status_code == 412
    invalid = client.put(
        path,
        headers={"If-Match": str(current["version"]), "Idempotency-Key": "invalid-generation-limit"},
        json={**payload, "settings": {"max_generation_calls": 11}},
    )
    assert invalid.status_code == 400
    assert client.get(path).json()["version"] == current["version"]
    assert Integrations(system["repo"]).build_policy()["max_generation_calls"] == 7


def test_non_admin_cannot_change_generation_call_policy(system):
    registry = Integrations(system["repo"])
    with pytest.raises(RuleError) as caught:
        registry.save(
            system["users"][0],
            "jev",
            "unauthorized-call-limit",
            0,
            {"settings": {"max_generation_calls": 1}, "reason": "Tentativa sem perfil administrativo"},
        )
    assert caught.value.code == "FORBIDDEN"
    assert registry.build_policy()["max_generation_calls"] == 10
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(IntegrationConfig)) == 0


@pytest.mark.parametrize("configured", [None, 6])
def test_guided_send_and_capability_use_the_server_call_limit(system, guided, configured):
    flow, actor, project = guided
    expected = configured or 10
    if configured is not None:
        save_policy(system, {"max_generation_calls": configured})
    save_request(flow, actor, project, False)
    operation = send_request(system, flow, actor, project)
    with system["repo"].sessions() as db:
        stored = db.get(Operation, operation["id"])
        assert stored.payload["max_generation_calls"] == expected
        assert stored.policy["max_batches"] == expected
        assert stored.policy["generation_input_budget"] == 64096 * expected
        assert stored.payload["generation_context_limit_bytes"] == 252288
    client = TestClient(create_app(False, dependencies(system)))
    login(client, system)
    capability = client.get("/api/v1/capabilities")
    assert capability.status_code == 200
    assert capability.json()["guided_build"]["max_generation_calls"] == expected


@pytest.mark.parametrize("field", ["max_generation_calls", "max_batches", "generation_input_budget"])
def test_public_request_cannot_override_call_or_budget_policy(system, guided, field):
    _, actor, project = guided
    client = TestClient(create_app(False, dependencies(system)))
    headers = login(client, system)
    before = system["repo"].wallet(actor, True)
    response = client.post(
        f"/api/v1/projects/{project['id']}/messages",
        headers={**headers, "Idempotency-Key": "forged-" + field},
        json={
            "expected_draft_revision": 1,
            "base_project_revision": project["current_revision_id"],
            "policy_version": "2026-10-v1",
            "review_snapshot_hash": "0" * 64,
            field: 999,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert system["repo"].wallet(actor, True) == before
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(Operation)) == 0
        assert db.scalar(select(func.count()).select_from(Reservation)) == 0


def test_larger_reservation_still_rejects_insufficient_balance_before_dispatch(system, guided):
    _, actor, project = guided
    policy = {
        **system["policy"],
        "input_usd_per_million": "0",
        "cached_usd_per_million": "0",
        "output_usd_per_million": "125",
    }
    system["config"].catalog_path.write_text(json.dumps({"models": [policy]}))
    before = system["repo"].wallet(actor, True)
    with pytest.raises(RuleError) as caught:
        GenerationService(system["repo"], system["catalog"]).start(
            actor,
            project["id"],
            "insufficient-generation-reserve",
            {
                "brief": "Implemente as melhorias na página inicial.",
                "base_revision_id": project["current_revision_id"],
                "policy_version": "2026-10-v1",
                "review_required": True,
            },
            contract_version=4,
        )
    assert caught.value.code == "INSUFFICIENT_BALANCE"
    assert system["repo"].wallet(actor, True) == before
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(Operation)) == 0
        assert db.scalar(select(func.count()).select_from(Reservation)) == 0


def test_generation_call_diagnostics_keep_counters_without_private_content():
    counts = {"provider_calls": 10, "accepted_batches": 7, "repair_attempts": 2, "truncation_retries": 1}
    assert safe_fields({**counts, "prompt": "private-request", "files": {"private.js": "private-source"}}) == counts
    assert safe_fields({"provider_calls": True, "accepted_batches": -1, "repair_attempts": "private-source"}) == {}
