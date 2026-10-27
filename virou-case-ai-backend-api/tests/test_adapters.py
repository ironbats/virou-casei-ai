from datetime import timedelta
import json
import httpx
import pytest
from sqlalchemy import select
from viracase.application.generation import GenerationService
from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.models import Account, Period, Operation
from viracase.infrastructure.providers import HttpModelGateway, ProviderRejected, ProviderUncertain


def test_recover_accepted_intention_after_catalog_and_access_expire(system):
    repo, aid = system["repo"], system["users"][0]
    p = repo.create_project(
        aid, "project-intent", {"name": "Recovery", "objective": "Criar aplicação funcional"}
    )
    payload = {
        "brief": "Criar fluxo completo de demonstração",
        "base_revision_id": p["current_revision_id"],
        "policy_version": "2026-10-v1",
    }
    service = GenerationService(repo, system["catalog"])
    op = service.start(aid, p["id"], "stable-intent", payload)
    system["config"].catalog_path.write_text('{"models": []}')
    with repo.sessions.begin() as db:
        period = db.get(Period, db.get(Account, aid).current_period_id)
        period.starts_at = utcnow() - timedelta(days=31)
        period.expires_at = utcnow() - timedelta(days=1)
    assert service.start(aid, p["id"], "stable-intent", payload)["id"] == op["id"]
    with pytest.raises(RuleError) as error:
        service.start(
            aid, p["id"], "stable-intent", {**payload, "brief": "Outro conteúdo para a mesma intenção"}
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    with repo.sessions() as db:
        assert len(db.scalars(select(Operation)).all()) == 1


def test_openai_contract_cache_and_reasoning_not_double_counted(system):
    calls = []

    def request(req):
        calls.append(req)
        body = json.loads(req.content)
        assert body["model"] == "gpt-6-luna"
        assert body["store"] is False and "tools" not in body
        assert req.url.path == "/v1/responses"
        return httpx.Response(
            200,
            json={
                "id": "external-test-id",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "input_tokens_details": {"cached_tokens": 20},
                    "output_tokens_details": {"reasoning_tokens": 30},
                },
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": '{"files": {}}'}]}
                ],
            },
        )

    result = HttpModelGateway(system["config"], httpx.MockTransport(request)).generate(
        system["policy"], "context"
    )
    assert result.usage == {"input": 100, "cached": 20, "output": 50}
    assert result.external_id == "external-test-id" and len(calls) == 1


def test_gemini_contract_thoughts_included_once(system):
    policy = {**system["policy"], "provider": "gemini", "model": "gemini-3.5-flash-lite"}

    def request(req):
        assert req.url.path.endswith("/gemini-3.5-flash-lite:generateContent")
        assert "tools" not in json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "responseId": "gemini-test-id",
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "cachedContentTokenCount": 10,
                    "candidatesTokenCount": 20,
                    "thoughtsTokenCount": 30,
                },
                "candidates": [
                    {"content": {"parts": [{"thought": True, "text": "private"}, {"text": '{"files": {}}'}]}}
                ],
            },
        )

    result = HttpModelGateway(system["config"], httpx.MockTransport(request)).generate(policy, "context")
    assert result.usage == {"input": 100, "cached": 10, "output": 50}
    assert "private" not in result.text


@pytest.mark.parametrize("status,expected", [(429, ProviderRejected), (503, ProviderUncertain)])
def test_provider_error_never_automatically_retries(system, status, expected):
    calls = []

    def request(req):
        calls.append(req)
        return httpx.Response(status, json={"error": "synthetic"})

    with pytest.raises(expected):
        HttpModelGateway(system["config"], httpx.MockTransport(request)).generate(system["policy"], "context")
    assert len(calls) == 1
