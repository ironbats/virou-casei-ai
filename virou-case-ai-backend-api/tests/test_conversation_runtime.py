import json
import re
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

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
from viracase.infrastructure import composition
from viracase.infrastructure.ai_registry import AIRegistry
from viracase.infrastructure.credentials import initialize_key
from viracase.infrastructure.models import Attempt, new_id
from viracase.infrastructure.provider_errors import report_rejection
from viracase.infrastructure.providers import HttpModelGateway, ProviderRejected
from viracase.infrastructure.worker import Worker
from viracase.interfaces.api import create_app


def secret_mount(service):
    compose = (Path(__file__).parents[1] / "compose.yaml").read_text()
    block = re.search(rf"^  {service}:\n(.*?)(?=^  [\w-]+:|^volumes:)", compose, re.M | re.S)[1]
    return next(
        (
            volume
            for volume in re.split(r"^      - type: bind\n", block, flags=re.M)[1:]
            if re.search(r"^        target: /app/var/secrets$", volume, re.M)
        ),
        "",
    )


def test_public_conversation_runtime_shares_readonly_credential_mount():
    for service in ("public-api", "admin-api", "worker"):
        mount = secret_mount(service)
        assert mount, f"{service} executa gerações e precisa da chave existente"
        assert re.search(r"^        source: ./var/secrets$", mount, re.M)
        assert re.search(r"^        read_only: true$", mount, re.M)


def managed_provider(system, provider, mode):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            assert "assertion=" in request.content.decode()
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-runtime-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        if provider == "openai":
            assert request.headers["authorization"] == "Bearer " + KEY
        elif mode == "service_account":
            assert request.headers["authorization"] == "Bearer synthetic-runtime-token"
            assert request.url.host == "aiplatform.googleapis.com"
        else:
            assert request.headers["x-goog-api-key"] == KEY
        changes = json.dumps(
            {
                "files": [],
                "edits": [
                    {
                        "path": "public/index.html",
                        "old": "</body>",
                        "new": "<p>Implementação confirmada</p></body>",
                    }
                ],
                "complete": True,
            }
        )
        if provider == "openai":
            return httpx.Response(
                200,
                json={
                    "id": "runtime-result",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": changes}]}],
                    "usage": {"input_tokens": 10, "output_tokens": 100},
                },
            )
        return httpx.Response(
            200,
            json={
                "responseId": "runtime-result",
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": changes}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 100},
            },
        )

    transport = httpx.MockTransport(handler)
    initialize_key(system["config"].credential_key_path, system["repo"].sessions)
    registry = AIRegistry(system["repo"], transport=transport)
    data = {
        **connection_data(),
        "provider": provider,
        "auth_mode": mode,
        "project_id": "synthetic-project",
        "credential": json.dumps(google_key()) if mode == "service_account" else KEY,
    }
    connection = create_connection(system, registry, data)
    # Binary-exact synthetic amounts keep SQLite assertions independent of numeric rounding.
    pricing = {
        "model": "runtime-model",
        "input_usd_per_million": "0",
        "cached_usd_per_million": "0",
        "output_usd_per_million": "125",
    }
    model = create_model(system, registry, connection, **pricing)
    assert probe(system, registry, connection, model)["state"] == "verified"
    registry.save_model(
        system["admin"],
        connection["id"],
        model["id"],
        new_id(),
        model_data(**pricing, enabled=True, expected_version=model["version"]),
    )
    system["config"].catalog_path.write_text('{"models":[]}')
    calls.clear()
    return transport, calls


def runtime(system, monkeypatch, transport, key_path):
    config = system["config"].model_copy(update={"credential_key_path": key_path})
    monkeypatch.setattr(composition, "settings", lambda: config)
    dependencies = composition.compose()
    catalog, gateway = dependencies[3:]
    assert catalog.registry is gateway.registry
    catalog.registry.transport = transport
    gateway.transport = transport
    return dependencies


def login(client, system):
    origin = system["config"].public_origin
    response = client.post(
        "/api/v1/auth/login",
        headers={"Origin": origin},
        json={"login": "student0", "password": "test-student-password"},
    )
    assert response.status_code == 200
    return {"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]}


def project_and_draft(client, headers):
    response = client.post(
        "/api/v1/projects",
        headers={**headers, "Idempotency-Key": "project"},
        json={"name": "Projeto integrado", "objective": "Implementar uma página de tarefas"},
    )
    assert response.status_code == 201, response.text
    project = response.json()
    response = client.put(
        f"/api/v1/projects/{project['id']}/draft",
        headers={
            **headers,
            "Idempotency-Key": "draft",
            "If-Match": "0",
        },
        json={
            "base_revision_id": project["current_revision_id"],
            "files": None,
            "prompt": "Implementar tarefas",
        },
    )
    assert response.status_code == 200, response.text
    return project


def converse(client, headers, project):
    response = client.post(
        f"/api/v1/projects/{project['id']}/conversation",
        headers={
            **headers,
            "Idempotency-Key": "conversation",
        },
        json={
            "brief": "Implemente uma página de tarefas",
            "policy_version": "2026-10-v1",
            "base_revision_id": project["current_revision_id"],
        },
    )
    assert response.status_code == 202, response.text
    response = client.get("/api/v1/operations/" + response.json()["id"])
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize(
    "provider,mode",
    [
        ("openai", "api_key"),
        ("gemini", "api_key"),
        ("gemini", "service_account"),
    ],
)
def test_compose_public_runtime_generates_and_applies_managed_code(
    system, monkeypatch, tmp_path, provider, mode
):
    transport, calls = managed_provider(system, provider, mode)
    key_path = (
        system["config"].credential_key_path if secret_mount("public-api") else tmp_path / "unmounted.key"
    )
    dependencies = runtime(system, monkeypatch, transport, key_path)
    repo, catalog, gateway = dependencies[2:]
    actor = system["users"][0]
    with TestClient(create_app(dependencies=dependencies)) as client:
        headers = login(client, system)
        project = project_and_draft(client, headers)
        operation = converse(client, headers, project)
        assert operation["state"] == "succeeded", operation
        assert operation["billing_status"] == "settled"
        assert operation["model_label"] is None and "policy" not in operation
        assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
        assert converse(client, headers, project)["id"] == operation["id"]
        assert not Worker(repo, gateway, catalog).tick(operation["id"])
        assert len(calls) == (2 if mode == "service_account" else 1)
        with repo.sessions() as db:
            attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
            assert attempt.actual_brl == Decimal("0.0625")
        response = client.post(
            f"/api/v1/projects/{project['id']}/proposals/{operation['id']}/apply",
            headers={
                **headers,
                "Idempotency-Key": "apply",
                "If-Match": str(project["version"]),
            },
        )
        assert response.status_code == 200, response.text
        files = repo.source_files(actor, project["id"], response.json()["revision_id"])
        assert "Implementação confirmada" in files["public/index.html"]


@pytest.mark.parametrize("failure", ["missing", "wrong", "invalid"])
def test_missing_or_unreadable_key_reproduces_pre_dispatch_failure_without_charge(
    system,
    monkeypatch,
    tmp_path,
    caplog,
    failure,
):
    transport, calls = managed_provider(system, "gemini", "service_account")
    key_path = tmp_path / "isolated-runtime" / "credentials.key"
    if failure != "missing":
        key_path.parent.mkdir()
        key_path.write_bytes(Fernet.generate_key() if failure == "wrong" else b"invalid-key")
    dependencies = runtime(system, monkeypatch, transport, key_path)
    repo = dependencies[2]
    actor = system["users"][0]
    with TestClient(create_app(dependencies=dependencies)) as client:
        headers = login(client, system)
        project = project_and_draft(client, headers)
        wallet = repo.wallet(actor)
        operation = converse(client, headers, project)
        assert operation["state"] == "failed"
        assert operation["error_code"] == "PROVIDER_CONFIGURATION_INVALID"
        assert operation["billing_status"] == "released"
        assert calls == []
        assert repo.wallet(actor) == wallet
        assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
        assert converse(client, headers, project)["id"] == operation["id"]
    events = [json.loads(record.message) for record in caplog.records if record.name == "viracase.provider"]
    assert events[-1] == {
        "event": "generation_provider_rejected",
        "operation_id": operation["id"],
        "category": "PROVIDER_CONFIGURATION_INVALID",
        "stage": "request_settings",
        "reason_code": "CREDENTIAL_STORE_UNAVAILABLE",
    }
    assert str(key_path) not in caplog.text and KEY not in caplog.text


@pytest.mark.parametrize(
    "reason",
    [
        "AI_CONFIGURATION_PENDING",
        "AI_PROVIDER_UNAVAILABLE",
        "NOT_FOUND",
        "untrusted-secret-value",
    ],
)
def test_configuration_diagnostics_allow_only_safe_codes(system, caplog, reason):
    class UnavailableRegistry:
        def request_settings(self, *args, **kwargs):
            raise RuleError(reason, "private-credential-details")

    gateway = HttpModelGateway(system["config"], registry=UnavailableRegistry())
    with pytest.raises(ProviderRejected) as caught:
        gateway.generate({**system["policy"], "connection_id": "managed"}, "private-prompt")
    report_rejection("synthetic-operation", caught.value)
    expected = reason if reason != "untrusted-secret-value" else "AI_CONFIGURATION_UNAVAILABLE"
    assert caught.value.diagnostic["reason_code"] == expected
    assert "private-credential-details" not in caplog.text and "private-prompt" not in caplog.text
    assert "untrusted-secret-value" not in caplog.text


def test_missing_registry_has_specific_safe_diagnostic(system):
    with pytest.raises(ProviderRejected) as caught:
        HttpModelGateway(system["config"]).generate(
            {**system["policy"], "connection_id": "managed"},
            "private-prompt",
        )
    assert caught.value.error_code == "PROVIDER_REJECTED"
    assert caught.value.diagnostic["reason_code"] == "AI_REGISTRY_UNAVAILABLE"
