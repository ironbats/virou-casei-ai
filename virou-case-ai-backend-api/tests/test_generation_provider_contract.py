import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from viracase.application.conversation import prompt_for, response_schema
from viracase.application.generation import GenerationService
from viracase.infrastructure.models import Attempt
from viracase.infrastructure.providers import HttpModelGateway, ProviderRejected, ProviderUncertain
from viracase.infrastructure.templates import starter
from viracase.infrastructure.worker import Worker


def gemini_policy(system):
    return {
        **system["policy"],
        "provider": "gemini",
        "model": "gemini-3.8-flash",
        "generation_contract_version": 3,
    }


def valid_response():
    changes = {
        "files": [],
        "edits": [
            {
                "path": "public/index.html",
                "old": "<h1>Seu projeto</h1>",
                "new": "<h1>Página inicial implementada</h1>",
            }
        ],
        "complete": True,
    }
    return {
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(changes)}]}}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 70, "thoughtsTokenCount": 20},
        "responseId": "verified-test-response",
    }


@pytest.mark.parametrize(
    "field,message",
    [
        ("thinkingConfig", "Unknown name thinkingConfig in generationConfig. private-value-must-not-appear"),
        (
            "responseSchema",
            "responseSchema is not supported for this endpoint. private-value-must-not-appear",
        ),
    ],
)
def test_explicit_optional_parameter_rejection_retries_once_and_generates(system, caplog, field, message):
    requests = []

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert field in body["generationConfig"]
            return httpx.Response(400, json={"error": {"message": message, "status": "INVALID_ARGUMENT"}})
        assert field not in body["generationConfig"]
        assert body["contents"] == requests[0]["contents"]
        return httpx.Response(200, json=valid_response())

    result = HttpModelGateway(system["config"], transport=httpx.MockTransport(transport)).generate(
        gemini_policy(system), "pedido"
    )
    assert len(requests) == 2 and result.output_error is None
    assert result.usage == {"input": 100, "cached": 0, "output": 90}
    assert "provider_parameter_fallback" in caplog.text
    assert "private-value-must-not-appear" not in caplog.text


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "PROVIDER_CONFIGURATION_INVALID"),
        (401, "PROVIDER_ACCESS_DENIED"),
        (403, "PROVIDER_ACCESS_DENIED"),
        (404, "PROVIDER_MODEL_UNAVAILABLE"),
        (413, "PROVIDER_REQUEST_TOO_LARGE"),
        (422, "PROVIDER_CONFIGURATION_INVALID"),
        (429, "PROVIDER_CAPACITY_EXCEEDED"),
    ],
)
def test_rejection_is_actionable_and_never_blindly_retried(system, status, code):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(
            status, json={"error": {"message": "sensitive data secret-payload", "status": "INVALID_ARGUMENT"}}
        )

    with pytest.raises(ProviderRejected) as error:
        HttpModelGateway(system["config"], transport=httpx.MockTransport(transport)).generate(
            gemini_policy(system), "pedido"
        )
    assert error.value.error_code == code and len(calls) == 1
    assert "secret-payload" not in str(error.value.diagnostic)


def test_compatibility_retry_has_a_hard_limit(system):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(400, json={"error": {"message": "thinkingConfig is unsupported"}})

    with pytest.raises(ProviderRejected):
        HttpModelGateway(system["config"], transport=httpx.MockTransport(transport)).generate(
            gemini_policy(system), "pedido"
        )
    assert len(calls) == 2


def test_network_uncertainty_during_fallback_is_not_replayed(system):
    calls = []

    def transport(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(400, json={"error": {"message": "thinkingConfig not supported"}})
        raise httpx.ReadTimeout("test timeout")

    with pytest.raises(ProviderUncertain):
        HttpModelGateway(system["config"], transport=httpx.MockTransport(transport)).generate(
            gemini_policy(system), "pedido"
        )
    assert len(calls) == 2


def test_schema_is_sent_for_both_providers(system):
    captured = []

    def transport(request):
        body = json.loads(request.content)
        captured.append(body)
        if "generationConfig" in body:
            return httpx.Response(200, json=valid_response())
        return httpx.Response(
            200,
            json={"usage": {"input_tokens": 100, "output_tokens": 70}, "output": [], "status": "completed"},
        )

    gateway = HttpModelGateway(system["config"], transport=httpx.MockTransport(transport))
    gateway.generate({**system["policy"], "generation_contract_version": 3}, "pedido")
    gateway.generate(gemini_policy(system), "pedido")
    assert captured[0]["text"]["format"]["schema"] == response_schema()
    assert captured[0]["text"]["format"]["strict"] is True
    assert captured[1]["generationConfig"]["responseSchema"]["required"] == ["files", "edits", "complete"]
    assert captured[1]["generationConfig"]["responseSchema"]["type"] == "OBJECT"


@pytest.mark.parametrize("reject", [False, True])
def test_http_adapter_worker_and_revision_application(system, caplog, reject):
    from viracase.infrastructure.drafts import Drafts
    from viracase.infrastructure.learning import LearningLibrary

    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor, "project", {"name": "Contrato HTTP", "objective": "Implementar página inicial"}
    )
    policy = gemini_policy(system)

    class Catalog:
        def eligible(self):
            return [policy]

        def resolve(self, prompt):
            return policy, Decimal("1")

    calls = []

    def transport(request):
        calls.append(request)
        if reject:
            return httpx.Response(403, json={"error": {"message": "IAM denied private-test-resource"}})
        if len(calls) == 1:
            return httpx.Response(400, json={"error": {"message": "Unknown name thinkingConfig"}})
        return httpx.Response(200, json=valid_response())

    op = GenerationService(repo, Catalog()).start(
        actor,
        project["id"],
        "generation",
        {
            "brief": "Implemente a página inicial",
            "base_revision_id": project["current_revision_id"],
            "policy_version": "2026-10-v1",
            "review_required": True,
        },
        contract_version=3,
    )
    Worker(
        repo, HttpModelGateway(system["config"], transport=httpx.MockTransport(transport)), Catalog()
    ).tick(op["id"])
    final = repo.operation(actor, op["id"])
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    assert final["billing_status"] == ("released" if reject else "settled")
    if reject:
        assert final["error_code"] == "PROVIDER_ACCESS_DENIED" and len(calls) == 1
        assert "generation_provider_rejected" in caplog.text and op["id"] in caplog.text
        assert "private-test-resource" not in caplog.text
        with repo.sessions() as db:
            assert db.scalar(select(Attempt).where(Attempt.operation_id == op["id"])).actual_brl == 0
    else:
        assert final["state"] == "succeeded" and len(calls) == 2
        drafts = Drafts(repo, LearningLibrary(system["config"].content_root))
        drafts.apply(actor, project["id"], op["id"], "apply", project["version"])
        current = repo.project(actor, project["id"])
        files = repo.source_files(actor, project["id"], current["current_revision_id"])
        assert "Página inicial implementada" in files["public/index.html"]


def test_generation_budget_and_starters_remain_readable():
    for stack in ("hono-typescript-d1", "fastapi-python-d1"):
        files = starter(stack)
        for path in ("public/index.html", "public/style.css", "public/app.js"):
            assert len(files[path].splitlines()) > 5
        prompt = prompt_for(
            {"stack": stack, "objective": "Site"}, "Crie uma página", files, output_tokens=16000
        )
        assert "24000 caracteres" in prompt and "não minifique" in prompt
        assert "até 4000 caracteres" not in prompt
