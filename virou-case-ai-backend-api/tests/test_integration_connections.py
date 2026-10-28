import json
from datetime import timedelta
from hashlib import sha256

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_guided_build import guided as guided
from test_guided_build import save_request
from test_publication_pipeline import POLICY, Remote, execute, start
from test_publication_pipeline import publication as publication

from viracase.domain.rules import RuleError, canonical_hash, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway
from viracase.infrastructure.credentials import initialize_key
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import (
    Account,
    Audit,
    Idempotency,
    IntegrationConfig,
    IntegrationConnection,
    IntegrationVersionBinding,
    Operation,
    PublicationResource,
    new_id,
)
from viracase.infrastructure.publication_lifecycle import (
    purge_publications,
    synchronize_publication_visibility,
)
from viracase.infrastructure.publication_qualification import REQUIRED_CHECKS, record_qualification
from viracase.infrastructure.publication_reconcile import reconcile_publication
from viracase.infrastructure.recommendations import Recommendations
from viracase.interfaces.api import create_app
from viracase.interfaces.feature_schemas import BuildPolicy

REASON = "Teste controlado de conexões independentes"


@pytest.fixture
def registry(system, monkeypatch):
    initialize_key(system["config"].credential_key_path, system["repo"].sessions)
    monkeypatch.setattr(
        CloudflareGateway,
        "validate",
        lambda self: {
            "ready": True,
            "subdomain": "synthetic-test",
            "database_ids": [],
            "workers_count": 0,
        },
    )
    return Integrations(system["repo"])


def payload(provider, name, account="b", token=True):
    return {
        "name": name,
        "settings": {**POLICY, "account_id": account * 32}
        if provider == "cloudflare"
        else BuildPolicy().model_dump(),
        "token": "synthetic-only-token-" + name if token else None,
        "reason": REASON,
    }


def create(system, registry, provider, name, account="b"):
    return registry.save(
        system["admin"], provider, new_id(), 0, payload(provider, name, account), create=True
    )


def activate(system, registry, provider, config):
    registry.validate(system["admin"], provider, config["version"], REASON, config["connection_id"])
    registry.activate(
        system["admin"], provider, new_id(), config["version"], REASON, connection_id=config["connection_id"]
    )


def qualify_connection(system, registry, config):
    report = {
        "format_version": 1,
        "configuration_id": config["id"],
        "configuration_digest": config["configuration_digest"],
        "account_id": config["settings"]["account_id"],
        "tested_at": utcnow().isoformat(),
        "expires_at": (utcnow() + timedelta(hours=1)).isoformat(),
        "runner_version": "synthetic-only-contract-test",
        "runner_sha256": "f" * 64,
        "executor_digest": "f" * 64,
        "stacks": ["hono-typescript-d1"],
        "checks": [
            {
                "code": code,
                "passed": True,
                "expected": "Synthetic expected result",
                "observed": "Synthetic observed result",
                "evidence_sha256": "e" * 64,
            }
            for code in REQUIRED_CHECKS
        ],
        "report_reference": "synthetic-only-not-production-evidence",
        "reason": REASON,
    }
    return record_qualification(
        system["repo"], system["admin"], new_id(), config["version"], report, config["connection_id"]
    )


@pytest.mark.parametrize("provider", ["cloudflare", "jev"])
def test_multiple_connections_isolate_credentials_versions_activation_and_default(system, registry, provider):
    first = create(system, registry, provider, "Conta A", "a")
    second = create(system, registry, provider, "Conta B", "b")
    assert first["is_default"] and not second["is_default"]
    assert registry.get(provider)["connection_id"] == first["connection_id"]
    activate(system, registry, provider, first)
    activate(system, registry, provider, second)
    changed = registry.save(
        system["admin"],
        provider,
        "rotate-a",
        first["version"],
        payload(provider, "Conta A", "a", token=False),
        first["connection_id"],
    )
    assert changed["version"] > second["version"] and changed["state"] == "draft"
    assert registry.get(provider, second["connection_id"])["state"] == "active"
    with system["repo"].sessions() as db:
        a, b = db.get(IntegrationConfig, changed["id"]), db.get(IntegrationConfig, second["id"])
        assert registry.vault.decrypt(a.id, a.credential) == "synthetic-only-token-Conta A"
        assert registry.vault.decrypt(b.id, b.credential) == "synthetic-only-token-Conta B"
        assert db.get(IntegrationConfig, first["id"]).state == "disabled"
    registry.set_default(
        system["admin"], provider, second["connection_id"], "new-default", second["version"], REASON
    )
    assert registry.get(provider)["connection_id"] == second["connection_id"]
    listing = registry.listing(system["admin"], provider)["items"]
    assert len(listing) == 2 and sum(c["is_default"] for c in listing) == 1
    assert "synthetic-only-token" not in json.dumps(listing)


def test_duplicate_names_stale_edits_and_idempotency_are_atomic(system, registry):
    body = payload("jev", "Produção")
    created = registry.save(system["admin"], "jev", "create", 0, body, create=True)
    assert registry.save(system["admin"], "jev", "create", 0, body, create=True) == created
    with pytest.raises(RuleError) as duplicate:
        create(system, registry, "jev", "produção")
    assert duplicate.value.code == "CONFLICT"
    with pytest.raises(RuleError) as stale:
        registry.save(system["admin"], "jev", "stale", 0, body, created["connection_id"])
    assert stale.value.status == 412
    with pytest.raises(RuleError):
        registry.get("cloudflare", created["connection_id"])
    with pytest.raises(RuleError):
        registry.listing(system["users"][0], "jev")
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(IntegrationConfig)) == 1
        assert db.scalar(select(func.count()).select_from(IntegrationConnection)) == 1
        assert db.scalar(select(func.count()).select_from(IntegrationVersionBinding)) == 1
        assert (
            db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "integration:jev:save"))
            == 1
        )
        intention = db.scalar(select(Idempotency).where(Idempotency.key == "create"))
        assert "synthetic-only-token" not in json.dumps(intention.result)


def test_legacy_idempotency_and_default_endpoints_keep_their_contract(system, registry):
    body = {k: v for k, v in payload("jev", "Legado").items() if k != "name"}
    created = registry.save(system["admin"], "jev", "legacy", 0, body)
    with system["repo"].sessions() as db:
        saved = db.get(Idempotency, canonical_hash([system["admin"], "integration:jev:save", "legacy"]))
        assert saved.payload_hash == canonical_hash(
            {**body, "token": sha256(body["token"].encode()).hexdigest(), "version": 0}
        )
    assert registry.save(system["admin"], "jev", "legacy", 0, body) == created
    registry.validate(system["admin"], "jev", created["version"], REASON)
    registry.activate(system["admin"], "jev", "legacy-activate", created["version"], REASON)
    with system["repo"].sessions() as db:
        saved = db.get(
            Idempotency, canonical_hash([system["admin"], "integration:jev:state", "legacy-activate"])
        )
        assert saved.payload_hash == canonical_hash(
            {"version": created["version"], "reason": REASON, "disable": False}
        )


def test_new_connection_never_inherits_other_connection_token(system, registry):
    create(system, registry, "cloudflare", "Conta existente")
    with pytest.raises(RuleError) as caught:
        registry.save(
            system["admin"],
            "cloudflare",
            "missing-token",
            0,
            payload("cloudflare", "Sem token", token=False),
            create=True,
        )
    assert caught.value.code == "CONFIGURATION_PENDING"
    assert len(registry.listing(system["admin"], "cloudflare")["items"]) == 1


def test_default_requires_valid_active_version_and_fresh_cloudflare_validation(system, registry):
    first = create(system, registry, "cloudflare", "Conta principal")
    other = create(system, registry, "cloudflare", "Conta alternativa", "c")
    with pytest.raises(RuleError):
        registry.set_default(
            system["admin"], "cloudflare", other["connection_id"], "draft-default", other["version"], REASON
        )
    activate(system, registry, "cloudflare", other)
    with system["repo"].sessions.begin() as db:
        db.get(IntegrationConfig, other["id"]).validated_at = utcnow() - timedelta(days=8)
    with pytest.raises(RuleError):
        registry.set_default(
            system["admin"], "cloudflare", other["connection_id"], "expired-default", other["version"], REASON
        )
    assert registry.get("cloudflare")["id"] == first["id"]


def test_jev_default_selects_matching_model_token_and_guided_limits(system, registry, guided):
    flow, actor, project = guided
    save_request(flow, actor, project, False)
    first = create(system, registry, "jev", "Jev A")
    body = payload("jev", "Jev B")
    body["settings"].update(model="jev-custom-b", recommendation_enabled=True, max_objective_chars=3500)
    second = registry.save(system["admin"], "jev", "second", 0, body, create=True)
    activate(system, registry, "jev", first)
    activate(system, registry, "jev", second)
    assert registry.build_policy()["max_objective_chars"] == 2000
    registry.set_default(
        system["admin"], "jev", second["connection_id"], "default-b", second["version"], REASON
    )
    assert registry.build_policy()["max_objective_chars"] == 3500
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request.headers["authorization"], body["model"]))
        return httpx.Response(
            200,
            json={
                "answers": {
                    k: {"type": "choice", "choice": "none", "confidence": 1} for k in body["questions"]
                }
            },
        )

    result = Recommendations(flow, httpx.MockTransport(handler)).recommend(
        actor, project["id"], "recommend", {"expected_draft_revision": 1}
    )
    assert result["status"] == "ready"
    assert calls == [("Bearer synthetic-only-token-Jev B", "jev-custom-b")]


def test_default_change_preserves_queued_and_existing_publication_account(system, publication):
    registry, service, actor, project = publication
    op = start(system, publication)
    first = registry.get("cloudflare")
    second = create(system, registry, "cloudflare", "Outra Cloudflare")
    activate(system, registry, "cloudflare", second)
    qualify_connection(system, registry, second)
    registry.set_default(
        system["admin"], "cloudflare", second["connection_id"], "default-b", second["version"], REASON
    )
    remote = Remote()
    execute(system, op, remote)
    assert system["repo"].operation(actor, op["id"])["state"] == "succeeded"
    with system["repo"].sessions() as db:
        resource = db.get(PublicationResource, project["id"])
        assert resource.config_id == first["id"] and resource.remote_account_id == POLICY["account_id"]
    assert service.readiness(actor, project["id"])["configuration_version"] == first["version"]
    new_project = system["repo"].create_project(
        actor, "other-project", {"name": "Novo projeto", "objective": "Outro projeto de teste"}
    )
    assert service.readiness(actor, new_project["id"])["configuration_version"] == second["version"]
    new_op = service.start(
        actor, new_project["id"], "publish-b", {"revision_id": new_project["current_revision_id"]}
    )
    with system["repo"].sessions() as db:
        assert db.get(Operation, new_op["id"]).policy["config_id"] == second["id"]
        assert db.get(PublicationResource, new_project["id"]).remote_account_id == "b" * 32
    with pytest.raises(RuleError) as caught:
        registry.save(
            system["admin"],
            "cloudflare",
            "move-account",
            first["version"],
            payload("cloudflare", first["name"], "c"),
            first["connection_id"],
        )
    assert caught.value.code == "CONNECTION_IN_USE"


def test_rotation_and_lifecycle_use_bound_account_even_after_default_changes(system, publication):
    registry, service, actor, project = publication
    op = start(system, publication)
    remote = Remote()
    execute(system, op, remote)
    first = registry.get("cloudflare")
    second = create(system, registry, "cloudflare", "Outra conta")
    activate(system, registry, "cloudflare", second)
    registry.set_default(
        system["admin"], "cloudflare", second["connection_id"], "default-b", second["version"], REASON
    )
    rotated = registry.save(
        system["admin"],
        "cloudflare",
        "rotate-a",
        first["version"],
        payload("cloudflare", "Conta original", "a"),
        first["connection_id"],
    )
    calls = []

    def factory(policy, secret):
        calls.append((policy["account_id"], secret))
        return remote

    with system["repo"].sessions.begin() as db:
        db.get(Account, actor).suspended = True
    remote.request = lambda *args: {"enabled": False}
    synchronize_publication_visibility(system["repo"], factory)
    assert calls == [("a" * 32, "synthetic-only-token-Conta original")]
    assert not service.readiness(actor, project["id"])["can_publish"]
    assert registry.get("cloudflare", second["connection_id"])["state"] == "active"
    remote.request = lambda *args: None
    with system["repo"].sessions.begin() as db:
        account = db.get(Account, actor)
        account.lifecycle = "purging"
        fence = account.fence
        resource = db.get(PublicationResource, project["id"])
        assert registry.for_resource(db, resource).id == rotated["id"]
    purge_publications(system["repo"], actor, fence, factory)
    assert all(account_id == "a" * 32 for account_id, _ in calls)


def test_reconciliation_retains_original_account_after_switch(system, publication):
    registry, service, actor, project = publication
    op = start(system, publication)
    remote = Remote("upload_uncertain")
    execute(system, op, remote)
    second = create(system, registry, "cloudflare", "Outra conta")
    activate(system, registry, "cloudflare", second)
    registry.set_default(
        system["admin"], "cloudflare", second["connection_id"], "default-b", second["version"], REASON
    )
    seen = []

    def factory(policy, secret):
        seen.append((policy["account_id"], secret))
        return remote

    result = reconcile_publication(system["repo"], system["admin"], op["id"], "reconcile-a", REASON, factory)
    assert result["state"] == "succeeded"
    assert seen == [("a" * 32, "synthetic-cloudflare-token")]


def test_routes_are_private_csrf_protected_and_scope_versions(system, registry):
    deps = (system["config"], system["engine"], system["repo"], system["catalog"], None)
    client = TestClient(create_app(True, deps))
    prefix = "/admin-api/v1/integrations/jev/connections"
    assert client.get(prefix).status_code == 401
    client.headers.update({"Origin": "http://127.0.0.1:5174"})
    login = client.post(
        "/admin-api/v1/auth/login", json={"login": "operator", "password": "test-operator-password"}
    )
    assert login.status_code == 200
    body = payload("jev", "Jev via API")
    assert client.post(prefix, json=body, headers={"Idempotency-Key": "create"}).status_code == 403
    client.headers.update({"X-CSRF-Token": login.json()["csrf_token"], "Idempotency-Key": "create"})
    response = client.post(prefix, json=body)
    assert response.status_code == 201, response.text
    config = response.json()
    path = prefix + "/" + config["connection_id"]
    assert client.get(path).json()["name"] == body["name"]
    assert client.post(prefix, json=body).json() == config
    assert body["token"] not in client.get(prefix).text
    invalid = {**body, "name": "  ", "settings": {"max_attachments": 300}}
    assert client.post(prefix, json=invalid).status_code in {400, 422}
    assert (
        client.put(path, json=body, headers={"If-Match": "0", "Idempotency-Key": "stale"}).status_code == 412
    )
    for action in ("validate", "activate", "disable"):
        result = client.post(
            path + "/" + action,
            json={"reason": REASON},
            headers={"If-Match": str(config["version"]), "Idempotency-Key": action},
        )
        assert result.status_code == 200, result.text
    assert client.get(path).json()["state"] == "disabled"
    assert client.get(path.replace("/jev/", "/cloudflare/")).status_code == 404
    public = TestClient(create_app(False, deps))
    assert public.get("/api/v1/integrations/jev/connections").status_code == 404


def test_publication_concurrency_is_limited_per_cloudflare_account(system, publication):
    registry, service, actor, project = publication
    with system["repo"].sessions.begin() as db:
        first = registry.latest(db, "cloudflare")
        first.settings = {**first.settings, "max_concurrent": 1}
    start(system, publication)
    body = payload("cloudflare", "Segunda conta")
    body["settings"]["max_concurrent"] = 1
    second = registry.save(system["admin"], "cloudflare", "create-b", 0, body, create=True)
    activate(system, registry, "cloudflare", second)
    qualify_connection(system, registry, second)
    registry.set_default(
        system["admin"], "cloudflare", second["connection_id"], "default-b", second["version"], REASON
    )
    other_actor = system["users"][1]
    new_project = system["repo"].create_project(
        other_actor, "first-b", {"name": "Projeto B", "objective": "Teste de conta independente"}
    )
    assert (
        service.start(
            other_actor, new_project["id"], "publish-b", {"revision_id": new_project["current_revision_id"]}
        )["state"]
        == "queued"
    )
    extra = system["repo"].create_project(
        other_actor, "second-b", {"name": "Extra B", "objective": "Teste de limite por conta"}
    )
    with pytest.raises(RuleError) as quota:
        service.start(
            other_actor, extra["id"], "publish-extra", {"revision_id": extra["current_revision_id"]}
        )
    assert quota.value.code == "QUOTA_EXCEEDED"


def test_validation_of_one_connection_does_not_mark_another_as_valid(system, registry, monkeypatch):
    first = create(system, registry, "cloudflare", "Primeira conta", "a")

    def validate_remote(self):
        create(system, registry, "cloudflare", "Nova conta simultânea", "b")
        return {"ready": True}

    monkeypatch.setattr(CloudflareGateway, "validate", validate_remote)
    registry.validate(system["admin"], "cloudflare", first["version"], REASON, first["connection_id"])
    states = {c["name"]: c["state"] for c in registry.listing(system["admin"], "cloudflare")["items"]}
    assert states == {"Primeira conta": "validated", "Nova conta simultânea": "draft"}
