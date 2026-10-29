import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from test_ai_configuration import create_connection, create_model, model_data, probe, registry
from test_clarification_continuation import (
    ATTACHMENT,
    ORIGINAL_REQUEST,
    QUESTION,
    ClarificationGateway,
    begin,
    clarify,
    client_for,
    implement,
    reply,
    workspace,
)
from test_conversation_runtime import login
from test_generation_call_limits import ContinuingGateway, start_generation
from test_template_preparations import PreparationGateway, preparation, request_for

from viracase.application.conversation import generation_batch_budget
from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError, aware, cost, utcnow
from viracase.infrastructure.agent_execution import validate_model_execution
from viracase.infrastructure.feature_policies import FeaturePolicies
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import (
    Attempt,
    IntegrationConfig,
    Job,
    Operation,
    Reservation,
    new_id,
)
from viracase.infrastructure.providers import HttpModelGateway, ProviderRejected, ProviderUncertain
from viracase.infrastructure.worker import Worker
from viracase.infrastructure.workflow_logging import safe_fields
from viracase.interfaces.ai_schemas import ModelExecution
from viracase.interfaces.api import create_app
from viracase.interfaces.feature_schemas import BuildPolicy

__all__ = ["preparation", "registry", "workspace"]


def save_construction(system, settings, version=0):
    return Integrations(system["repo"]).save(
        system["admin"],
        "jev",
        new_id(),
        version,
        {"settings": settings, "reason": "Configurar execução e recuperação da construção"},
    )


def test_two_confirmed_truncations_recover_inside_the_existing_call_budget(system):
    repo, actor, project, operation = start_generation(system)
    gateway = ContinuingGateway(repo, actor, project, complete_after=3, truncated_calls=(1, 2))
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "succeeded", final
    assert len(gateway.prompts) == 3
    assert final["result_ref"]["candidate"]
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    for prompt, budget in zip(gateway.prompts, (24000, 12000, 6000)):
        assert f"aproximadamente {budget} caracteres" in prompt
    with repo.sessions() as db:
        op = db.get(Operation, operation["id"])
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == op.id))
        assert attempt.actual_brl == cost(op.policy, {"input": 300, "cached": 0, "output": 300})
        assert len(attempt.usage["calls"]) == 3
        assert [call["output_error"] for call in attempt.usage["calls"]] == [
            "GENERATION_TRUNCATED",
            "GENERATION_TRUNCATED",
            None,
        ]


@pytest.mark.parametrize(
    "settings,cuts,complete_after,calls,state,error",
    [
        ({}, (1, 2, 3), 4, 4, "succeeded", None),
        ({}, (1, 2, 3, 4), 5, 4, "failed", "GENERATION_TRUNCATED"),
        ({"max_truncation_retries": 0}, (1,), 2, 1, "failed", "GENERATION_TRUNCATED"),
        (
            {"max_truncation_retries": 9, "max_generation_calls": 3},
            (1, 2, 3),
            4,
            3,
            "failed",
            "GENERATION_TRUNCATED",
        ),
        ({"max_truncation_retries": 9}, tuple(range(1, 10)), 10, 10, "succeeded", None),
    ],
)
def test_recovery_uses_configured_limits_and_never_exceeds_reserved_calls(
    system, settings, cuts, complete_after, calls, state, error
):
    save_construction(system, settings)
    repo, actor, project, operation = start_generation(system)
    gateway = ContinuingGateway(repo, actor, project, complete_after=complete_after, truncated_calls=cuts)
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == state
    assert final["error_code"] == error
    assert len(gateway.prompts) == calls
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    if error:
        assert "revision_id" not in final["result_ref"]
    with repo.sessions() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
        assert attempt.usage["input"] == 100 * calls
        assert attempt.usage["output"] == 100 * calls
        assert (
            db.scalar(
                select(func.count())
                .select_from(Reservation)
                .where(Reservation.operation_id == operation["id"])
            )
            == 1
        )


def test_admitted_policy_and_timeouts_are_frozen_despite_local_admin_changes(system):
    saved = save_construction(
        system, {"max_batch_characters": 18000, "batch_shrink_factor": 0.25, "max_truncation_retries": 2}
    )
    repo, actor, project, operation = start_generation(system)
    save_construction(system, {"max_generation_calls": 1, "max_truncation_retries": 0}, saved["version"])
    system["config"].provider_timeout = 15

    class Gateway(ContinuingGateway):
        def generate(self, policy, prompt):
            assert policy["max_batches"] == 10
            assert policy["max_truncation_retries"] == 2
            assert policy["request_timeout_seconds"] == 180
            assert policy["construction_policy_version"] == saved["version"]
            with repo.sessions() as db:
                job = db.scalar(select(Job).where(Job.operation_id == operation["id"]))
                assert aware(job.lease_until) > utcnow() + timedelta(seconds=460)
            return super().generate(policy, prompt)

    gateway = Gateway(repo, actor, project, complete_after=3, truncated_calls=(1, 2))
    Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    assert repo.operation(actor, operation["id"])["state"] == "succeeded"
    for prompt, budget in zip(gateway.prompts, (18000, 4500, 1125)):
        assert f"aproximadamente {budget} caracteres" in prompt


def test_admin_selected_model_is_used_without_changing_client_consent_policy(system):
    alternative = {
        **system["policy"],
        "model": "gpt-6.1-sol",
        "version": "selected-v1",
        "output_usd_per_million": "2",
    }
    system["config"].catalog_path.write_text(json.dumps({"models": [system["policy"], alternative]}))
    save_construction(system, {"generation_model_id": "openai:gpt-6.1-sol"})
    repo, actor, project, operation = start_generation(system)
    with repo.sessions() as db:
        saved = db.get(Operation, operation["id"])
        assert saved.policy["model"] == "gpt-6.1-sol"
        assert saved.policy["version"] == "selected-v1"
        assert saved.payload["policy_version"] == "2026-10-v1"
    gateway = ContinuingGateway(repo, actor, project, complete_after=1)
    Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    assert repo.operation(actor, operation["id"])["state"] == "succeeded"


def test_unavailable_selected_model_is_rejected_before_saving_policy(system):
    with pytest.raises(RuleError) as caught:
        save_construction(system, {"generation_model_id": "not-available"})
    assert caught.value.code == "MODEL_UNAVAILABLE"
    assert Integrations(system["repo"]).get("jev")["version"] == 0


def test_uncertain_result_is_never_replayed_by_truncation_recovery(system):
    save_construction(system, {"max_truncation_retries": 9})
    repo, actor, _, operation = start_generation(system)

    class Gateway:
        calls = 0

        def generate(self, policy, prompt):
            self.calls += 1
            raise ProviderUncertain("response_unknown")

    gateway = Gateway()
    worker = Worker(repo, gateway, system["catalog"])
    worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    assert gateway.calls == 1
    assert repo.operation(actor, operation["id"])["state"] == "reconciling"


def test_effective_character_budget_respects_output_context_and_minimum():
    policy = {
        "max_output_tokens": 65536,
        "generation_runtime_version": 1,
        **BuildPolicy(max_batch_characters=120000).model_dump(),
    }
    assert generation_batch_budget(policy, input_bound=252288) == 120000
    assert generation_batch_budget(policy, input_bound=64096) == 60000
    assert generation_batch_budget({**policy, "max_output_tokens": 4000}, input_bound=252288) == 8000
    assert generation_batch_budget(policy, 9, input_bound=252288) == 500
    assert generation_batch_budget({**policy, "max_output_tokens": 100}, 9) == 200


@pytest.mark.parametrize("state", ["active", "validated", "draft", "invalid", "disabled"])
def test_local_construction_changes_preserve_jev_state_and_validation(system, state):
    saved = save_construction(system, {})
    checked = utcnow()
    evidence = {"ready": True, "configuration_valid": True, "connectivity_verified": False}
    with system["repo"].sessions.begin() as db:
        row = db.get(IntegrationConfig, saved["id"])
        row.state, row.validation, row.validated_at = state, evidence, checked
        row.settings = {
            key: value
            for key, value in row.settings.items()
            if key not in {"max_truncation_retries", "max_response_repairs"}
        }
    changed = save_construction(system, {"max_truncation_retries": 4}, saved["version"])
    assert changed["state"] == state
    assert changed["validation"] == evidence
    assert changed["validated_at"] == checked.isoformat()
    assert changed["connection_id"] == saved["connection_id"]
    with system["repo"].sessions() as db:
        old = db.get(IntegrationConfig, saved["id"])
        assert old.state == ("disabled" if state == "active" else state)
        assert db.scalar(
            select(func.count()).select_from(IntegrationConfig).where(IntegrationConfig.state == "active")
        ) == int(state == "active")


@pytest.mark.parametrize(
    "change",
    [
        {"model": "jev-new"},
        {"timeout_seconds": 11},
        {"recommendation_enabled": True},
        {"daily_requests": 31},
        {"min_confidence": 0.6},
        {"token": "synthetic-new-jev-token"},
    ],
)
def test_remote_jev_changes_still_require_validation_and_activation(system, change):
    from viracase.infrastructure.credentials import initialize_key

    initialize_key(system["config"].credential_key_path, system["repo"].sessions)
    saved = save_construction(system, {})
    with system["repo"].sessions.begin() as db:
        row = db.get(IntegrationConfig, saved["id"])
        row.state, row.validation, row.validated_at = "active", {"ready": True}, utcnow()
    payload = {
        "settings": {key: value for key, value in change.items() if key != "token"},
        "reason": "Alterar recomendação remota",
    }
    if "token" in change:
        payload["token"] = change["token"]
    result = Integrations(system["repo"]).save(system["admin"], "jev", new_id(), saved["version"], payload)
    assert result["state"] == "draft"
    assert result["validation"] == {}
    assert result["validated_at"] is None


@pytest.mark.parametrize(
    "provider,model,output,execution,valid",
    [
        ("gemini", "gemini-2.5-flash", 65536, {"thinking_mode": "budget", "thinking_budget": 24576}, True),
        ("gemini", "gemini-2.5-flash", 16000, {"thinking_mode": "budget", "thinking_budget": 0}, True),
        ("gemini", "gemini-2.5-flash", 16000, {"thinking_mode": "budget", "thinking_budget": -1}, True),
        ("gemini", "gemini-2.5-flash", 16000, {"thinking_mode": "budget", "thinking_budget": 16000}, False),
        ("gemini", "gemini-2.5-flash", 65536, {"thinking_mode": "budget", "thinking_budget": 24577}, False),
        ("gemini", "gemini-2.5-pro", 65536, {"thinking_mode": "budget", "thinking_budget": 0}, False),
        ("gemini", "gemini-2.5-pro", 65536, {"thinking_mode": "budget", "thinking_budget": 128}, True),
        ("gemini", "gemini-2.5-flash", 16000, {"thinking_mode": "level", "thinking_level": "low"}, False),
        ("gemini", "gemini-3.8-flash", 16000, {"thinking_mode": "level", "thinking_level": "minimal"}, False),
        ("gemini", "gemini-3.8-flash", 16000, {"thinking_mode": "level", "thinking_level": "low"}, True),
        ("gemini", "gemini-3.8-flash", 16000, {"temperature": 0.1}, False),
        ("gemini", "gemini-4-argon", 65536, {}, False),
        ("gemini", "gemini-2.5-flash", 16000, {"reasoning_effort": "low"}, False),
        ("openai", "gpt-6-luna", 16000, {"reasoning_effort": "none", "temperature": 0.2}, True),
        ("openai", "gpt-6-luna", 16000, {"reasoning_effort": "high", "temperature": 0.2}, False),
        ("openai", "gpt-6.1-sol", 16000, {"reasoning_effort": "none"}, False),
        ("openai", "gpt-6.1-sol", 16000, {"reasoning_effort": "xhigh"}, True),
        ("openai", "gpt-6-luna", 16000, {"thinking_mode": "budget", "thinking_budget": 0}, False),
        ("openai", "future-text-model", 32000, {}, True),
        ("openai", "future-text-model", 65536, {}, False),
        ("openai", "future-text-model", 16000, {"reasoning_effort": "low"}, False),
    ],
)
def test_provider_specific_execution_compatibility(provider, model, output, execution, valid):
    policy = {"max_output_tokens": output, "execution": execution}
    if valid:
        assert validate_model_execution(provider, model, policy) == ModelExecution(**execution).model_dump()
    else:
        with pytest.raises(RuleError) as caught:
            validate_model_execution(provider, model, policy)
        assert caught.value.code == "INVALID_AGENT_EXECUTION"


@pytest.mark.parametrize(
    "options",
    [
        {"request_timeout_seconds": 601},
        {"connect_timeout_seconds": 31},
        {"request_timeout_seconds": 10, "connect_timeout_seconds": 20},
        {"thinking_budget": 0},
        {"thinking_mode": "budget"},
        {"thinking_mode": "level"},
        {"temperature": 0.1, "top_p": 0.8},
    ],
)
def test_invalid_execution_shapes_are_rejected(options):
    with pytest.raises(ValidationError):
        ModelExecution(**options)


class ManagedRegistry:
    def request_settings(self, policy, probe=False):
        if policy["provider"] == "openai":
            return "https://api.openai.com/v1/responses", {"Authorization": "Bearer synthetic-test"}
        return (
            "https://aiplatform.googleapis.com/v1/projects/synthetic/locations/global/publishers/google/models/"
            + policy["model"]
            + ":generateContent",
            {"Authorization": "Bearer synthetic-test"},
        )


def gemini_response(reason="STOP", text='{"ok":true}'):
    return {
        "responseId": "synthetic-response",
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 30},
        "candidates": [{"finishReason": reason, "content": {"parts": [{"text": text}]}}],
    }


@pytest.mark.parametrize(
    "mode,expected",
    [("budget", {"thinkingBudget": 2048}), ("provider_default", None), ("auto", {"thinkingBudget": 0})],
)
def test_gemini_execution_reaches_vertex_wire_without_overriding_legacy_defaults(system, mode, expected):
    captured = []

    def transport(request):
        captured.append(request)
        return httpx.Response(200, json=gemini_response("MAX_TOKENS", '{"files":'))

    execution = ModelExecution(
        request_timeout_seconds=450,
        connect_timeout_seconds=20,
        thinking_mode=mode,
        thinking_budget=2048 if mode == "budget" else None,
        temperature=0.2,
    ).model_dump()
    policy = {
        **system["policy"],
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "connection_id": "synthetic",
        "max_output_tokens": 65536,
        "template_preparation_contract": 1,
        "execution": execution,
    }
    result = HttpModelGateway(
        system["config"], registry=ManagedRegistry(), transport=httpx.MockTransport(transport)
    ).generate(policy, "private-request")
    assert len(captured) == 1
    request = captured[0]
    config = json.loads(request.content)["generationConfig"]
    assert config.get("thinkingConfig") == expected
    assert config["maxOutputTokens"] == 65536
    assert config["temperature"] == 0.2
    assert "reasoning" not in config
    assert request.extensions["timeout"]["read"] == 450
    assert request.extensions["timeout"]["connect"] == 20
    assert result.output_error == "GENERATION_TRUNCATED"
    assert result.usage["output"] == 50
    assert result.diagnostics == {
        "finish_reason": "MAX_TOKENS",
        "http_status": 200,
        "reasoning_tokens": 30,
        "visible_output_tokens": 20,
        "provider_dispatches": 1,
        "output_characters": 9,
        "effective_thinking_mode": "budget" if expected else "provider_default",
        **({"effective_thinking_budget": expected["thinkingBudget"]} if expected else {}),
    }
    assert "private-request" not in json.dumps(result.diagnostics)


def test_explicit_thinking_configuration_is_not_silently_removed_after_rejection(system):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(400, json={"error": {"message": "thinkingConfig unsupported private-secret"}})

    policy = {
        **system["policy"],
        "provider": "gemini",
        "model": "gemini-3.8-flash",
        "execution": ModelExecution(thinking_mode="level", thinking_level="low").model_dump(),
    }
    with pytest.raises(ProviderRejected):
        HttpModelGateway(system["config"], transport=httpx.MockTransport(transport)).generate(
            policy, "pedido"
        )
    assert len(calls) == 1


def test_openai_execution_does_not_receive_gemini_parameters(system):
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 100,
                    "output_tokens_details": {"reasoning_tokens": 90},
                },
            },
        )

    policy = {
        **system["policy"],
        "execution": ModelExecution(reasoning_effort="none", top_p=0.8).model_dump(),
    }
    result = HttpModelGateway(system["config"], transport=httpx.MockTransport(transport)).generate(
        policy, "pedido"
    )
    assert captured[0]["reasoning"] == {"effort": "none"}
    assert captured[0]["top_p"] == 0.8
    assert "thinkingConfig" not in captured[0] and "generationConfig" not in captured[0]
    assert result.diagnostics["finish_reason"] == "max_output_tokens"
    assert result.diagnostics["reasoning_tokens"] == 90
    assert result.diagnostics["visible_output_tokens"] == 10


def test_model_execution_change_requires_revalidation(system, registry):
    connection = create_connection(system, registry)
    model = create_model(system, registry, connection, model="gpt-6-luna")
    assert probe(system, registry, connection, model)["state"] == "verified"
    changed = registry.save_model(
        system["admin"],
        connection["id"],
        model["id"],
        new_id(),
        model_data(
            model="gpt-6-luna", expected_version=model["version"], execution={"reasoning_effort": "low"}
        ),
    )
    assert changed["verified"] is False
    assert changed["execution"]["reasoning_effort"] == "low"


def test_probe_does_not_mark_valid_json_from_a_truncated_response_verified(system, registry):
    connection = create_connection(system, registry)
    model = create_model(system, registry, connection)
    registry.transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )
    )
    assert probe(system, registry, connection, model)["state"] == "failed"
    assert registry.listing()["items"][0]["models"][0]["verified"] is False


def test_probe_reserves_visible_output_above_explicit_thinking_budget(system, registry):
    from test_ai_configuration import connection_data

    connection = create_connection(system, registry, {**connection_data(), "provider": "gemini"})
    model = create_model(
        system,
        registry,
        connection,
        model="gemini-2.5-flash",
        execution={"thinking_mode": "budget", "thinking_budget": 4096},
    )
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=gemini_response())

    registry.transport = httpx.MockTransport(transport)
    assert probe(system, registry, connection, model)["state"] == "verified"
    assert requests[0]["generationConfig"]["maxOutputTokens"] == 5120
    assert requests[0]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 4096}


def test_preparation_effective_output_rejects_incompatible_explicit_thinking_budget(system, preparation):
    service, _, actor, project, _ = preparation
    policy = {
        **system["policy"],
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "execution": ModelExecution(thinking_mode="budget", thinking_budget=4096).model_dump(),
    }
    service.catalog = type("Catalog", (), {"eligible": lambda self: [policy]})()
    before = system["repo"].wallet(actor, True)
    with pytest.raises(RuleError) as caught:
        service.start(actor, project["id"], new_id(), request_for(preparation))
    assert caught.value.code == "INVALID_AGENT_EXECUTION"
    assert system["repo"].wallet(actor, True) == before
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(Operation)) == 0


def test_preparation_three_repairs_use_frozen_config_and_one_reservation(system, preparation):
    service, _, actor, project, _ = preparation
    policies = FeaturePolicies(system["repo"], system["catalog"])
    policies.save(
        system["admin"],
        "template_preparation",
        new_id(),
        0,
        {"settings": {"max_repairs": 3}, "reason": "Recuperar saídas truncadas de preenchimento"},
    )
    operation = service.start(actor, project["id"], new_id(), request_for(preparation))
    policies.save(
        system["admin"],
        "template_preparation",
        new_id(),
        1,
        {"settings": {"max_repairs": 0}, "reason": "Configuração para próximos pedidos"},
    )

    class Gateway(PreparationGateway):
        def generate(self, policy, prompt):
            result = super().generate(policy, prompt)
            if len(self.calls) < 4:
                return ModelResult('{"items":', result.usage, result.external_id, "GENERATION_TRUNCATED")
            return result

    gateway = Gateway()
    worker = Worker(system["repo"], gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    assert len(gateway.calls) == 4
    final = service.get(actor, project["id"], operation["id"])
    assert final["state"] == "ready", final
    with system["repo"].sessions() as db:
        saved = db.get(Operation, operation["id"])
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == saved.id))
        assert saved.payload["preparation_settings"]["max_repairs"] == 3
        assert attempt.usage["output"] == 320
        assert (
            db.scalar(
                select(func.count()).select_from(Reservation).where(Reservation.operation_id == saved.id)
            )
            == 1
        )


def test_configuration_schema_is_admin_only_and_reports_real_constraints(system):
    app = create_app(True, (system["config"], system["engine"], system["repo"], system["catalog"], None))
    client = TestClient(app)
    path = "/admin-api/v1/ai/configuration-schema"
    assert client.get(path).status_code == 401
    client.headers.update({"Origin": system["config"].admin_origin})
    assert (
        client.post(
            "/admin-api/v1/auth/login", json={"login": "operator", "password": "test-operator-password"}
        ).status_code
        == 200
    )
    response = client.get(path)
    assert response.status_code == 200
    document = response.json()
    assert document["model"]["defaults"]["execution"]["thinking_mode"] == "auto"
    assert document["construction"]["schema"]["properties"]["max_generation_calls"]["maximum"] == 10
    assert document["preparation"]["schema"]["properties"]["max_repairs"]["maximum"] == 3
    assert (
        document["provider_capabilities"]["gemini"]["models"]["gemini-2.5-flash"]["max_output_tokens"]
        == 65536
    )
    assert document["available_models"][0]["max_output_tokens"] == 16000
    assert system["config"].openai_api_key not in response.text


def test_execution_diagnostic_allowlist_preserves_limits_but_rejects_private_content():
    values = {
        "finish_reason": "MAX_TOKENS",
        "thinking_budget": -1,
        "reasoning_tokens": 345,
        "visible_output_tokens": 20,
        "request_timeout_seconds": 450,
        "max_truncation_retries": 3,
    }
    assert (
        safe_fields({**values, "prompt": "private", "response": "secret", "credential": "hidden"}) == values
    )
    assert safe_fields({"finish_reason": "secret request content", "thinking_budget": -2}) == {}


def test_reply_after_partial_implementation_survives_two_truncations_with_checkpoint(system, workspace):
    repo, actor, project, _ = workspace
    question = begin(
        system,
        workspace,
        ClarificationGateway([implement("carrinho-ja-construido", False), clarify()]),
    )

    class AnswerGateway(ClarificationGateway):
        def generate(self, policy, prompt):
            result = super().generate(policy, prompt)
            if len(self.contexts) <= 2:
                return ModelResult('{"action":', result.usage, result.external_id, "GENERATION_TRUNCATED")
            return result

    gateway = AnswerGateway([implement("opcao-2"), implement("opcao-2"), implement("opcao-2")])
    with client_for(system, gateway) as client:
        response = reply(client, login(client, system), project, question, text="Vamos seguir com a opção 2")
        assert response.status_code == 202, response.text
        operation = response.json()
        worker = Worker(repo, gateway, system["catalog"])
        assert worker.tick(operation["id"])
        assert not worker.tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "succeeded", final
    assert len(gateway.contexts) == 3
    for context in gateway.contexts:
        serialized = json.dumps(context, ensure_ascii=False)
        assert ORIGINAL_REQUEST in serialized and QUESTION in serialized
        assert ATTACHMENT["content"] in serialized
        assert "Vamos seguir com a opção 2" in serialized
        assert "carrinho-ja-construido" in context["files"]["public/index.html"]
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    files = repo.source_files(actor, project["id"], final["result_ref"]["revision_id"])
    assert "carrinho-ja-construido" in files["public/index.html"]
    assert "opcao-2" in files["public/index.html"]


@pytest.mark.parametrize(
    "execution", [None, {"thinking_mode": "auto"}, {"thinking_mode": "provider_default"}]
)
def test_unknown_model_omits_unsupported_execution_parameters(system, execution):
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200, json={"status": "completed", "output": [], "usage": {"input_tokens": 10, "output_tokens": 5}}
        )

    policy = {
        **system["policy"],
        "model": "future-text-model",
        "connection_id": "synthetic",
        "execution": execution,
    }
    HttpModelGateway(
        system["config"], registry=ManagedRegistry(), transport=httpx.MockTransport(transport)
    ).generate(policy, "pedido")
    assert len(captured) == 1
    assert not {"reasoning", "thinkingConfig", "temperature", "top_p"} & captured[0].keys()


def test_queued_legacy_policy_keeps_one_truncation_retry_and_original_batch_budget(system):
    repo, actor, project, operation = start_generation(system)
    with repo.sessions.begin() as db:
        saved = db.get(Operation, operation["id"])
        saved.policy = {
            key: value
            for key, value in saved.policy.items()
            if key
            not in {
                "generation_runtime_version",
                "max_truncation_retries",
                "max_response_repairs",
                "max_batch_characters",
                "minimum_batch_characters",
                "batch_shrink_factor",
            }
        }
    gateway = ContinuingGateway(repo, actor, project, complete_after=3, truncated_calls=(1, 2))
    Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    assert repo.operation(actor, operation["id"])["error_code"] == "GENERATION_TRUNCATED"
    assert len(gateway.prompts) == 2
    assert "aproximadamente 24000 caracteres" in gateway.prompts[0]
    assert "aproximadamente 16000 caracteres" in gateway.prompts[1]


@pytest.mark.parametrize("repairs,expected_calls,state", [(0, 1, "failed"), (3, 4, "succeeded")])
def test_response_contract_repairs_follow_the_persisted_policy(system, repairs, expected_calls, state):
    save_construction(system, {"max_response_repairs": repairs})
    repo, actor, project, operation = start_generation(system)
    gateway = ContinuingGateway(repo, actor, project, complete_after=4, invalid_calls=(1, 2, 3))
    Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    assert repo.operation(actor, operation["id"])["state"] == state
    assert len(gateway.prompts) == expected_calls
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
