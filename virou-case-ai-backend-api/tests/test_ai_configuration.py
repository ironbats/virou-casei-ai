import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.ai_registry import AIRegistry
from viracase.infrastructure.credentials import initialize_key
from viracase.infrastructure.models import AIConnection, Audit, Idempotency, new_id
from viracase.infrastructure.providers import HttpModelGateway, ModelCatalog, ProviderRejected
from viracase.interfaces.ai_schemas import ConnectionInput, ModelInput
from viracase.interfaces.api import create_app

KEY = "synthetic-provider-key-for-contract-tests"


def connection_data(**overrides):
    return ConnectionInput(
        **dict(name="Conexão de teste", provider="openai", auth_mode="api_key", credential=KEY, **overrides)
    ).model_dump()


def model_data(**overrides):
    data = {
        "name": "Modelo dinâmico",
        "model": "future-text-model",
        "input_usd_per_million": "0.1",
        "cached_usd_per_million": "0.01",
        "output_usd_per_million": "0.5",
        "fx_brl_per_usd": "5",
        "fx_source": "synthetic-fx",
        "evidence": "synthetic-pricing",
        "effective_at": utcnow() - timedelta(days=1),
        "valid_until": utcnow() + timedelta(days=1),
        "max_output_tokens": 16000,
    }
    data.update(overrides)
    return ModelInput(**data).model_dump(mode="json")


def create_connection(system, registry, data=None):
    return registry.save_connection(system["admin"], None, new_id(), data or connection_data())


def create_model(system, registry, c, **kwargs):
    return registry.save_model(system["admin"], c["id"], None, new_id(), model_data(**kwargs))


def probe(system, registry, c, m, key=None):
    return registry.probe(
        system["admin"],
        c["id"],
        m["id"],
        key or new_id(),
        {"expected_version": m["version"], "reason": "Teste de contrato"},
    )


def openai_response(request):
    return httpx.Response(
        200,
        json={
            "id": "test-response",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )


@pytest.fixture
def registry(system):
    initialize_key(system["config"].credential_key_path, system["repo"].sessions)
    return AIRegistry(system["repo"], transport=httpx.MockTransport(openai_response))


def test_credentials_are_encrypted_masked_and_idempotent(system, registry):
    payload = connection_data()
    c = registry.save_connection(system["admin"], None, "one-connection", payload)
    assert c == registry.save_connection(system["admin"], None, "one-connection", payload)
    assert KEY not in json.dumps(registry.listing())
    with system["repo"].sessions() as db:
        stored = db.get(AIConnection, c["id"])
        assert KEY not in stored.credential
        assert registry.vault.decrypt(c["id"], stored.credential) == {"api_key": KEY}
        assert KEY not in str([(row.payload_hash, row.result) for row in db.scalars(select(Idempotency))])
        assert KEY not in str([row.reason for row in db.scalars(select(Audit))])
    old_key = system["config"].credential_key_path.read_bytes()
    initialize_key(system["config"].credential_key_path, system["repo"].sessions)
    assert system["config"].credential_key_path.read_bytes() == old_key
    assert system["config"].credential_key_path.stat().st_mode & 0o777 == 0o600
    system["config"].credential_key_path.unlink()
    with pytest.raises(ValueError, match="Restaure"):
        initialize_key(system["config"].credential_key_path, system["repo"].sessions)
    assert not system["config"].credential_key_path.exists()


def test_dynamic_model_probe_enable_generation_and_disable_without_env_fallback(system, registry):
    catalog = ModelCatalog(system["config"], registry)
    assert len(catalog.eligible()) == 1  # legacy provider still works until managed
    c = create_connection(system, registry)
    assert len(catalog.eligible()) == 1  # switch only after first model is enabled
    m = create_model(system, registry, c)
    assert probe(system, registry, c, m)["state"] == "verified"
    enabled = registry.save_model(
        system["admin"], c["id"], m["id"], new_id(), model_data(enabled=True, expected_version=m["version"])
    )
    assert enabled["ready"]
    policy, reserve = catalog.resolve("Teste de geração")
    assert policy["model"] == "future-text-model" and reserve > 0
    assert KEY not in json.dumps(policy)
    result = HttpModelGateway(system["config"], registry=registry, transport=registry.transport).generate(
        policy, "Return JSON"
    )
    assert json.loads(result.text) == {"ok": True}
    registry.save_connection(
        system["admin"],
        c["id"],
        new_id(),
        {**connection_data(), "credential": None, "enabled": False, "expected_version": c["version"]},
    )
    assert catalog.eligible() == []
    with pytest.raises(ProviderRejected):
        HttpModelGateway(system["config"], registry=registry).generate(policy, "No dispatch")


def test_unverified_model_cannot_be_enabled_and_update_is_versioned(system, registry):
    c = create_connection(system, registry)
    with pytest.raises(RuleError, match="Teste o modelo"):
        create_model(system, registry, c, enabled=True)
    m = create_model(system, registry, c)
    with pytest.raises(RuleError, match="modelo mudou"):
        registry.save_model(system["admin"], c["id"], m["id"], new_id(), model_data(expected_version=99))
    probe(system, registry, c, m)
    c2 = registry.save_connection(
        system["admin"], c["id"], new_id(), {**connection_data(), "credential": None, "expected_version": 1}
    )
    assert c2["version"] == 2
    assert not registry.listing()["items"][0]["models"][0]["verified"]


def test_probe_replay_and_uncertain_never_repeat_external_dispatch(system, registry):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic timeout")

    registry.transport = httpx.MockTransport(handler)
    c = create_connection(system, registry)
    m = create_model(system, registry, c)
    first = probe(system, registry, c, m, "same-paid-test")
    assert first["state"] == "uncertain"
    assert probe(system, registry, c, m, "same-paid-test") == first
    assert len(calls) == 1
    assert not registry.listing()["items"][0]["models"][0]["ready"]


@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_provider_error_is_sanitized(system, registry, status):
    registry.transport = httpx.MockTransport(
        lambda request: httpx.Response(status, text=KEY + " account-internal-debug")
    )
    c = create_connection(system, registry)
    result = registry.discover(c["id"])
    assert result["source"] == "fallback" and result["warning"]["upstream_status"] == status
    assert result["items"]
    assert KEY not in json.dumps(result) and "account-internal-debug" not in json.dumps(result)
    assert probe(system, registry, c, create_model(system, registry, c))["state"] == "failed"


def test_openai_service_account_project_headers_and_live_catalog(system, registry):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer " + KEY
        assert request.headers["OpenAI-Project"] == "proj_synthetic"
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "future-text-model"}, {"id": "another-model"}]})
        return openai_response(request)

    registry.transport = httpx.MockTransport(handler)
    data = {**connection_data(), "auth_mode": "service_account", "project_id": "proj_synthetic"}
    c = create_connection(system, registry, data)
    result = registry.discover(c["id"])
    assert result["source"] == "provider" and result["provider_count"] == 2
    assert [x["id"] for x in result["items"] if x["source"] == "provider"] == [
        "another-model",
        "future-text-model",
    ]
    assert probe(system, registry, c, create_model(system, registry, c))["state"] == "verified"
    assert len(calls) == 2


def google_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {
        "type": "service_account",
        "project_id": "synthetic-project",
        "private_key_id": "test-id",
        "private_key": key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode(),
        "client_email": "test@synthetic-project.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


@pytest.mark.parametrize("mode", ["api_key", "service_account"])
def test_google_modes_pagination_oauth_and_generation_contract(system, registry, mode):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            assert "assertion=" in request.content.decode()
            return httpx.Response(
                200,
                json={"access_token": "synthetic-oauth-token", "expires_in": 3600, "token_type": "Bearer"},
            )
        if mode == "api_key":
            assert request.headers["x-goog-api-key"] == KEY
            assert request.url.host == "generativelanguage.googleapis.com"
        else:
            assert request.headers["authorization"] == "Bearer synthetic-oauth-token"
            assert request.headers["x-goog-user-project"] == "synthetic-project"
            assert request.url.host == (
                "us-central1-aiplatform.googleapis.com"
                if request.method == "GET"
                else "aiplatform.googleapis.com"
            )
        if request.method == "GET":
            assert request.url.path == (
                "/v1beta1/publishers/google/models" if mode == "service_account" else "/v1beta/models"
            )
            name = "future-gemini" if not request.url.params.get("pageToken") else "second-model"
            payload = {
                "publisherModels" if mode == "service_account" else "models": [
                    {
                        "name": "publishers/google/models/" + name,
                        "supportedGenerationMethods": ["generateContent"],
                    }
                ]
            }
            if name == "future-gemini":
                payload["nextPageToken"] = "next-page"
            return httpx.Response(200, json=payload)
        if mode == "service_account":
            assert (
                "/v1/projects/synthetic-project/locations/global/publishers/google/models/future-gemini:generateContent"
                == request.url.path
            )
        assert json.loads(request.content)["generationConfig"]["responseMimeType"] == "application/json"
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": '{"ok":true}'}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            },
        )

    registry.transport = httpx.MockTransport(handler)
    payload = {
        **connection_data(),
        "provider": "gemini",
        "auth_mode": mode,
        "project_id": "synthetic-project",
        "credential": json.dumps(google_key()) if mode == "service_account" else KEY,
    }
    c = create_connection(system, registry, payload)
    result = registry.discover(c["id"])
    assert result["provider_count"] == 2 and result["warning"] is None
    m = create_model(system, registry, c, model="future-gemini")
    assert probe(system, registry, c, m)["state"] == "verified"
    assert sum(r.url.host == "oauth2.googleapis.com" for r in calls) == (
        1 if mode == "service_account" else 0
    )


def test_untrusted_google_token_endpoint_rejected_before_network(system, registry):
    secret = google_key()
    secret["token_uri"] = "http://127.0.0.1/internal"
    payload = {
        **connection_data(),
        "provider": "gemini",
        "auth_mode": "service_account",
        "project_id": "synthetic-project",
        "credential": json.dumps(secret),
    }
    with pytest.raises(RuleError, match="JSON de conta"):
        create_connection(system, registry, payload)
    assert registry.listing() == {"items": []}


def test_admin_routes_authentication_csrf_and_no_public_management(system, registry):
    catalog = ModelCatalog(system["config"], registry)
    deps = (system["config"], system["engine"], system["repo"], catalog, None)
    client = TestClient(create_app(True, deps))
    prefix = "/admin-api/v1"
    assert client.get(prefix + "/ai/connections").status_code == 401
    client.headers.update({"Origin": "http://127.0.0.1:5174"})
    login = client.post(
        prefix + "/auth/login", json={"login": "operator", "password": "test-operator-password"}
    )
    assert login.status_code == 200
    payload = connection_data()
    assert (
        client.post(
            prefix + "/ai/connections", json=payload, headers={"Idempotency-Key": "route-test"}
        ).status_code
        == 403
    )
    client.headers.update({"X-CSRF-Token": login.json()["csrf_token"], "Idempotency-Key": "route-test"})
    created = client.post(prefix + "/ai/connections", json=payload)
    assert created.status_code == 201, created.text
    assert KEY not in created.text and KEY not in client.get(prefix + "/ai/connections").text
    assert TestClient(create_app(False, deps)).get("/api/v1/ai/connections").status_code == 404
    with pytest.raises(RuleError):
        registry.save_connection(system["users"][0], None, new_id(), payload)


def test_stale_probe_does_not_overwrite_concurrent_configuration(system, registry):
    c = create_connection(system, registry)
    m = create_model(system, registry, c)

    def handler(request):
        registry.save_model(
            system["admin"],
            c["id"],
            m["id"],
            new_id(),
            model_data(expected_version=1, name="Editado durante teste"),
        )
        return openai_response(request)

    registry.transport = httpx.MockTransport(handler)
    assert probe(system, registry, c, m)["state"] == "configuration_changed"
    assert registry.listing()["items"][0]["models"][0]["name"] == "Editado durante teste"


def test_managed_generation_worker_settles_and_config_change_prevents_dispatch(system, registry):
    from viracase.application.generation import GenerationService
    from viracase.infrastructure.templates import starter
    from viracase.infrastructure.worker import Worker

    c = create_connection(system, registry)
    m = create_model(system, registry, c)
    probe(system, registry, c, m)
    registry.save_model(
        system["admin"], c["id"], m["id"], new_id(), model_data(enabled=True, expected_version=1)
    )
    catalog = ModelCatalog(system["config"], registry)
    calls = []
    files = starter("hono-typescript-d1")
    files["public/app.js"] += "\nconst managedIntegrationVerified = true;\n"

    def handler(request):
        calls.append(request)
        response = json.loads(openai_response(request).content)
        response["output"][0]["content"][0]["text"] = json.dumps(
            {"files": files, "summary": "Projeto atualizado"}
        )
        return httpx.Response(200, json=response)

    gateway = HttpModelGateway(system["config"], registry=registry, transport=httpx.MockTransport(handler))
    repo, uid = system["repo"], system["users"][0]
    project = repo.create_project(
        uid,
        new_id(),
        {"name": "Integração gerenciada", "objective": "Criar projeto para validar geração gerenciada"},
    )
    payload = {
        "brief": "Melhorar o projeto mantendo contratos",
        "base_revision_id": project["current_revision_id"],
        "policy_version": "2026-10-v1",
    }
    operation = GenerationService(repo, catalog).start(uid, project["id"], new_id(), payload)
    worker = Worker(repo, gateway, catalog)
    assert worker.tick()
    result = repo.operation(uid, operation["id"])
    assert result["state"] == "succeeded" and result["billing_status"] == "settled"
    assert len(calls) == 1
    project = repo.project(uid, project["id"])
    payload["base_revision_id"] = project["current_revision_id"]
    operation = GenerationService(repo, catalog).start(uid, project["id"], new_id(), payload)
    registry.save_connection(
        system["admin"],
        c["id"],
        new_id(),
        {**connection_data(), "credential": None, "expected_version": 1, "enabled": False},
    )
    assert worker.tick()
    assert repo.operation(uid, operation["id"])["state"] == "failed"
    assert len(calls) == 1
    assert Decimal(repo.wallet(uid, True)["reserved_brl"]) == 0
