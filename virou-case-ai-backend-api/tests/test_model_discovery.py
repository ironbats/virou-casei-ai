import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError
from test_ai_configuration import (
    KEY,
    connection_data,
    create_connection,
    create_model,
    google_key,
    model_data,
    probe,
)

from viracase.domain.rules import RuleError
from viracase.infrastructure.ai_registry import AIRegistry
from viracase.infrastructure.credentials import initialize_key
from viracase.infrastructure.model_catalog import MODEL_REFERENCES, local_models
from viracase.infrastructure.model_discovery import discover_models
from viracase.infrastructure.providers import ModelCatalog
from viracase.interfaces.api import create_app


@pytest.fixture
def registry(system):
    initialize_key(system["config"].credential_key_path, system["repo"].sessions)
    return AIRegistry(system["repo"])


def google_connection(system, registry, **options):
    return create_connection(
        system,
        registry,
        {
            **connection_data(),
            "provider": "gemini",
            "auth_mode": "service_account",
            "project_id": "synthetic-project",
            "credential": json.dumps(google_key()),
            **options,
        },
    )


def standalone(handler, provider="gemini", mode="api_key"):
    connection = SimpleNamespace(provider=provider, auth_mode=mode, options={"location": "global"})
    return discover_models(
        connection,
        lambda _: {"x-goog-api-key": KEY},
        lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
    )


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_default_catalog_needs_no_provider_or_readable_credentials(system, registry, provider):
    c = create_connection(system, registry, {**connection_data(), "provider": provider})
    system["config"].credential_key_path.unlink()
    registry.transport = httpx.MockTransport(lambda _: pytest.fail("No network needed for local catalog"))
    before = registry.listing()
    result = registry.local_catalog(c["id"])
    assert result["source"] == "local" and result["warning"] is None
    assert result["provider_count"] == 0
    assert len(result["items"]) == len(MODEL_REFERENCES[provider])
    assert len({item["id"] for item in result["items"]}) == len(result["items"])
    assert registry.listing() == before and registry.managed_providers() == set()
    assert not registry.eligible()
    fallback = registry.discover(c["id"])
    assert fallback["warning"]["code"] == "CREDENTIAL_STORE_UNAVAILABLE"
    assert fallback["items"] == result["items"]


@pytest.mark.parametrize("location", ["global", "us-central1", "southamerica-east1"])
def test_vertex_listing_regression_and_generation_v1(system, registry, location):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "synthetic-token", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert request.headers["x-goog-user-project"] == "synthetic-project"
        if request.method == "GET":
            if request.url.path != "/v1beta1/publishers/google/models":
                return httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})
            host = "us-central1" if location == "global" else location
            assert request.url.host == f"{host}-aiplatform.googleapis.com"
            assert request.url.params["listAllVersions"] == "true"
            assert request.url.params["pageSize"] == "100"
            return httpx.Response(
                200, json={"publisherModels": [{"name": "publishers/google/models/gemini-2.5-flash"}]}
            )
        assert (
            request.url.path
            == f"/v1/projects/synthetic-project/locations/{location}/publishers/google/models/gemini-2.5-flash:generateContent"
        )
        host = (
            "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
        )
        assert request.url.host == host
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": '{"ok":true}'}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            },
        )

    registry.transport = httpx.MockTransport(handler)
    c = google_connection(system, registry, location=location)
    result = registry.discover(c["id"])
    assert result["source"] == "provider" and result["provider_count"] == 1
    assert sum(item["id"] == "gemini-2.5-flash" for item in result["items"]) == 1
    assert (
        probe(system, registry, c, create_model(system, registry, c, model="gemini-2.5-flash"))["state"]
        == "verified"
    )
    assert sum(r.url.host == "oauth2.googleapis.com" for r in calls) == 1


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        (400, "", "INVALID_REQUEST"),
        (401, "", "AUTHENTICATION_FAILED"),
        (403, "", "PERMISSION_DENIED"),
        (403, "SERVICE_DISABLED", "SERVICE_DISABLED"),
        (403, "BILLING_DISABLED", "BILLING_DISABLED"),
        (403, "USER_PROJECT_DENIED", "USER_PROJECT_DENIED"),
        (403, "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"),
        (403, "secret-untrusted-reason", "PERMISSION_DENIED"),
        (404, "", "CATALOG_NOT_FOUND"),
        (429, "", "QUOTA_EXCEEDED"),
        (500, "", "PROVIDER_UNAVAILABLE"),
        (503, "", "PROVIDER_UNAVAILABLE"),
        (302, "", "PROVIDER_UNAVAILABLE"),
    ],
)
def test_fallback_classifies_status_without_leaking_provider_body(status, reason, expected, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"Location": "http://127.0.0.1/internal"},
            json={
                "error": {
                    "message": KEY + " private-project-debug",
                    "details": [{"reason": reason, "metadata": {"credential": KEY}}],
                },
            },
        )

    result = standalone(handler)
    assert len(calls) == 1
    assert result["source"] == "fallback" and result["items"] == local_models("gemini")
    assert result["warning"]["code"] == expected
    assert result["warning"]["upstream_status"] == status
    serialized = json.dumps(result) + caplog.text
    for secret in (KEY, "private-project-debug", "secret-untrusted-reason", "127.0.0.1/internal"):
        assert secret not in serialized
    assert expected in caplog.text


@pytest.mark.parametrize(
    "error,expected", [(httpx.ConnectError, "NETWORK_ERROR"), (httpx.ReadTimeout, "TIMEOUT")]
)
def test_transport_failure_keeps_manual_catalog(error, expected):
    def handler(request):
        raise error(KEY, request=request)

    result = standalone(handler)
    assert result["source"] == "fallback" and result["items"]
    assert result["warning"]["code"] == expected
    assert KEY not in json.dumps(result)


def test_oauth_failure_does_not_attempt_catalog_and_is_distinct(caplog):
    def headers(_):
        raise RefreshError("invalid_grant " + KEY)

    result = discover_models(
        SimpleNamespace(provider="gemini"),
        headers,
        lambda: pytest.fail("Catalog must not run after failed authentication"),
    )
    assert result["warning"]["code"] == "AUTHENTICATION_FAILED"
    assert result["warning"]["stage"] == "authentication"
    assert KEY not in json.dumps(result) + caplog.text


@pytest.mark.parametrize(
    "payload", [[], {"models": {}}, {"models": [None]}, {"models": [{"name": 2}]}, {"error": "secret"}]
)
def test_invalid_catalog_response_is_not_reported_as_success(payload):
    result = standalone(lambda _: httpx.Response(200, json=payload))
    assert result["warning"]["code"] == "INVALID_RESPONSE"
    assert result["source"] == "fallback" and result["items"]


@pytest.mark.parametrize(
    "status,length,expected", [(200, 4_000_001, "RESPONSE_LIMIT"), (403, 65_537, "PERMISSION_DENIED")]
)
def test_oversized_response_is_bounded(status, length, expected):
    result = standalone(lambda _: httpx.Response(status, content=b"x" * length))
    assert result["warning"]["code"] == expected


def test_invalid_json_and_empty_catalog():
    result = standalone(lambda _: httpx.Response(200, text="invalid-json"))
    assert result["warning"]["code"] == "INVALID_RESPONSE"
    empty = standalone(lambda _: httpx.Response(200, json={}))
    assert empty["warning"] is None and empty["provider_count"] == 0 and empty["items"]


@pytest.mark.parametrize("failure", ["loop", "http", "bad-token"])
def test_partial_pages_preserved_and_local_catalog_merged(failure):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200, json={"models": [{"name": "models/future-model"}], "nextPageToken": "token-one"}
            )
        assert request.url.params["pageToken"] == "token-one"
        if failure == "http":
            return httpx.Response(429)
        return httpx.Response(
            200, json={"models": [], "nextPageToken": "token-one" if failure == "loop" else [] + ["bad"]}
        )

    result = standalone(handler)
    assert len(calls) == 2 and result["source"] == "fallback"
    assert result["provider_count"] == 1
    assert "future-model" in [item["id"] for item in result["items"]]
    assert "gemini-2.5-flash" in [item["id"] for item in result["items"]]
    assert result["warning"]["code"] == ("QUOTA_EXCEEDED" if failure == "http" else "INCOMPLETE_CATALOG")


def test_pagination_has_a_finite_limit():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"models": [], "nextPageToken": str(len(calls))})

    result = standalone(handler)
    assert len(calls) == 100
    assert result["warning"]["code"] == "INCOMPLETE_CATALOG"


def test_new_catalog_route_requires_admin_and_discovery_keeps_csrf(system, registry):
    c = create_connection(system, registry)
    registry.transport = httpx.MockTransport(lambda _: httpx.Response(403))
    deps = (
        system["config"],
        system["engine"],
        system["repo"],
        ModelCatalog(system["config"], registry),
        None,
    )
    client = TestClient(create_app(True, deps))
    route = "/admin-api/v1/ai/connections/" + c["id"]
    assert client.get(route + "/catalog").status_code == 401
    client.headers.update({"Origin": "http://127.0.0.1:5174"})
    login = client.post(
        "/admin-api/v1/auth/login", json={"login": "operator", "password": "test-operator-password"}
    )
    assert login.status_code == 200
    assert client.get(route + "/catalog").status_code == 200
    assert client.post(route + "/discover").status_code == 403
    client.headers.update({"X-CSRF-Token": login.json()["csrf_token"]})
    response = client.post(route + "/discover")
    assert response.status_code == 200 and response.json()["source"] == "fallback"
    assert client.get("/admin-api/v1/ai/connections/missing/catalog").status_code == 404
    assert client.post("/admin-api/v1/ai/connections/missing/discover").status_code == 404
    assert (
        TestClient(create_app(False, deps)).get("/api/v1/ai/connections/" + c["id"] + "/catalog").status_code
        == 404
    )
    student = TestClient(create_app(False, deps))
    student.headers.update({"Origin": "http://127.0.0.1:5173"})
    assert (
        student.post(
            "/api/v1/auth/login", json={"login": "student0", "password": "test-student-password"}
        ).status_code
        == 200
    )
    client.cookies.clear()
    client.cookies.update(student.cookies)
    assert client.get(route + "/catalog").status_code == 401
    with pytest.raises(RuleError):
        registry.save_model(
            system["admin"],
            c["id"],
            None,
            "untested-local",
            {
                **model_data(model="gpt-6-luna"),
                "enabled": True,
            },
        )
