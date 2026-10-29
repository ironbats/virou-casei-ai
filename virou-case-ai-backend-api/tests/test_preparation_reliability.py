import copy
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from test_build_workflow import dispatch, save, select_template, workflow
from realistic_preparation_gateway import prompt_sources, select_phrase

from viracase.application.template_preparation import parse_preparation, preparation_prompt
from viracase.domain.rules import RuleError
from viracase.domain.rules import cost, utcnow
from viracase.domain.template_fields import normalized_fields
from viracase.infrastructure.learning import LearningLibrary
from viracase.infrastructure.providers import HttpModelGateway
from viracase.infrastructure.feature_policies import FeaturePolicies
from viracase.infrastructure.models import Attempt, Job, Ledger, Operation, Reservation
from viracase.infrastructure.recommendations import Recommendations
from viracase.infrastructure.worker import Worker
from viracase.infrastructure.template_preparations import TemplatePreparations

__all__ = ["workflow"]

AUTH_REQUEST = (
    "Ator: gerente da equipe. Ação permitida: ler e editar tarefas. Recurso: tarefas da própria equipe. "
    "Dono: ID da equipe da sessão. Permissão: proprietário da tarefa igual ao ID da equipe da sessão."
)
AUTH_VALUES = {
    "field_1": "gerente da equipe.",
    "field_2": "ler e editar tarefas.",
    "field_3": "tarefas da própria equipe.",
    "field_4": "ID da equipe da sessão.",
    "field_5": "proprietário da tarefa igual ao ID da equipe da sessão.",
    "field_6": "Proposta: negar a operação quando não houver permissão e manter os dados protegidos.",
    "field_7": "Proposta de teste: um gerente da equipe A tenta editar uma tarefa da equipe B e deve receber negação.",
    "field_8": "Proposta: exigir nova autenticação quando a sessão expirar, sem executar a alteração.",
    "field_9": "Proposta: responder que o recurso está indisponível sem revelar dados de outra equipe.",
    "field_10": "Proposta: registrar a ação e o resultado da autorização sem conteúdo privado ou credenciais.",
}


def prepare_authorization(flow, actor, project):
    value = save(
        flow, actor, project, lambda data: data.update(objective=AUTH_REQUEST, message_text=AUTH_REQUEST)
    )
    Recommendations(flow).recommend(
        actor, project["id"], "realistic-prepare", {"expected_draft_revision": value["version"]}
    )
    return select_template(flow, actor, project, "autorizacao")


def authorization_response(prompt, invalid=False):
    source = next(item for item in prompt_sources(prompt) if item["id"] == "request:message")
    if prompt.get("template_preparation_contract") == 3:
        items = {}
        for template in prompt["templates"]:
            fields = {}
            for definition in template["fields"]:
                key = definition["key"]
                fields[key] = (
                    {"proposal": AUTH_VALUES[key]}
                    if int(key.split("_")[1]) > 5
                    else {"extracts": [select_phrase(prompt, source["id"], AUTH_VALUES[key])]}
                )
                if invalid and key == "field_2":
                    fields[key] = {"extracts": [{"unit": 999999, "start": 0, "end": 0}]}
            items[template["attachment_id"]] = fields
        return json.dumps({"items": items})
    values = []
    for template in prompt["templates"]:
        fields = []
        for definition in template["fields"]:
            key = definition["key"]
            policy = int(key.split("_")[1]) > 5
            fields.append(
                provider_field(
                    prompt,
                    key,
                    AUTH_VALUES[key],
                    "" if policy else source["content"],
                    source_type="policy_suggestion" if policy else source["type"],
                    source_refs=[] if policy else [source["id"]],
                )
            )
            if invalid and key == "field_2":
                if prompt.get("template_preparation_contract", 1) == 2:
                    fields[-1]["citations"] = [{"source_ref": "outside:context", "quote": source["content"]}]
                else:
                    fields[-1]["source_refs"] = ["outside:context"]
        values.append({"attachment_id": template["attachment_id"], "fields": fields, "questions": []})
    return json.dumps({"items": values})


def provider_field(prompt, key, value, evidence, source_type="request", source_refs=None):
    refs = source_refs if source_refs is not None else ["request:message"]
    if prompt.get("template_preparation_contract", 1) == 2:
        return {
            "field_id": key,
            "value": value,
            "citations": [{"source_ref": ref, "quote": evidence} for ref in refs]
            if source_type != "policy_suggestion"
            else [],
        }
    return field(key, value, evidence, source_type=source_type, source_refs=refs)


def provider_response(provider, text, truncated=False):
    if provider == "openai":
        return httpx.Response(
            200,
            json={
                "id": "synthetic-response",
                "status": "incomplete" if truncated else "completed",
                "incomplete_details": {"reason": "max_output_tokens"} if truncated else None,
                "usage": {"input_tokens": 200, "output_tokens": 100},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            },
        )
    return httpx.Response(
        200,
        json={
            "responseId": "synthetic-response",
            "usageMetadata": {"promptTokenCount": 200, "candidatesTokenCount": 80, "thoughtsTokenCount": 20},
            "candidates": [
                {
                    "content": {"parts": [{"text": text}]},
                    "finishReason": "MAX_TOKENS" if truncated else "STOP",
                }
            ],
        },
    )


def realistic_gateway(system, provider, transport):
    policy = {
        **system["policy"],
        "provider": provider,
        "model": "gemini-4-argon" if provider == "gemini" else "gpt-6-luna",
    }

    class Catalog:
        def eligible(self):
            return [policy]

    system["catalog"] = Catalog()
    return HttpModelGateway(system["config"], transport=httpx.MockTransport(transport))


def wire_prompt(body):
    return json.loads(body.get("input") or body["contents"][0]["parts"][0]["text"])


def payload_for(slug="autorizacao", text=None):
    guide = LearningLibrary(Path(__file__).parents[1] / "content").guides[slug]
    fields = normalized_fields(
        {
            "content": guide["application_template"],
            "fields": guide["fields"],
            "source_label": guide["template_version"],
        },
        slug,
    )
    return {
        "items": [
            {
                "attachment_id": slug,
                "source_version_id": "version-" + slug,
                "attachment_revision": 1,
                "title": guide["title"],
                "definitions": fields,
                "original_fields": {},
            }
        ],
        "context": [
            {
                "id": "request:message",
                "type": "request",
                "content": text
                or "O gerente pode ler e editar as tarefas da própria equipe. A sessão contém o ID da equipe.",
            }
        ],
    }


def field(key, value, evidence, **changes):
    return {
        "field_id": key,
        "value_json": json.dumps(value),
        "source_type": "request",
        "source_refs": ["request:message"],
        "evidence": evidence,
        **changes,
    }


def response(payload, fields, questions=None):
    return json.dumps(
        {
            "items": [
                {
                    "attachment_id": payload["items"][0]["attachment_id"],
                    "fields": fields,
                    "questions": questions or [],
                }
            ]
        }
    )


def test_grounded_authorization_extracts_accept_case_and_whitespace_variations():
    payload = payload_for()
    evidence = "O gerente pode ler e editar as tarefas da própria equipe."
    result = parse_preparation(
        response(
            payload,
            [
                field("field_1", "Gerente", evidence),
                field("field_2", "Ler e editar as tarefas da própria equipe", evidence),
                field("field_4", "A sessão contém o ID da equipe", "A sessão  contém o ID da equipe."),
            ],
        ),
        payload,
        "operation",
    )[0]
    assert [value["field_id"] for value in result["suggested_fields"]] == ["field_1", "field_2", "field_4"]
    assert {key for question in result["questions"] for key in question["field_ids"]} == {
        "field_3",
        "field_5",
        "field_6",
        "field_7",
        "field_8",
        "field_9",
        "field_10",
    }


def test_grounded_authorization_can_combine_explicit_excerpts_without_inventing_facts():
    payload = payload_for(
        text="Ator: gerente da equipe. Ação permitida: editar tarefas. Recurso: tarefas da própria equipe."
    )
    result = parse_preparation(
        response(
            payload,
            [field("field_2", "Editar tarefas; tarefas da própria equipe", payload["context"][0]["content"])],
        ),
        payload,
        "operation",
    )[0]
    assert result["suggested_fields"][0]["value"] == "Editar tarefas; tarefas da própria equipe"


def test_partial_response_keeps_valid_fields_and_explains_only_invalid_field():
    payload = payload_for("sdd")
    text = response(
        payload,
        [
            field(
                "objective", "Organizar as tarefas da própria equipe", "editar as tarefas da própria equipe"
            ),
            field("audience", "Administrador global", "Esse papel não foi informado"),
        ],
    )
    with pytest.raises(RuleError) as error:
        parse_preparation(text, payload, "operation")
    assert error.value.code == "PREPARATION_INVALID_SOURCE"
    result = parse_preparation(text, payload, "operation", partial=True)[0]
    assert [value["field_id"] for value in result["suggested_fields"]] == ["objective"]
    assert result["errors"][0]["field_id"] == "audience"
    assert result["errors"][0]["code"] == "PREPARATION_INVALID_SOURCE"
    assert all("audience" not in question["field_ids"] for question in result["questions"])
    assert result["questions"][0]["field_ids"] == ["acceptance"]


@pytest.mark.parametrize(
    "value",
    [
        "Administrador global",
        "Equipe tenant-778",
        "Consultar /api/private-admin",
        "account.owner_id",
        "123e4567-e89b-12d3-a456-426614174000",
    ],
)
def test_factual_authorization_cannot_invent_identity_or_route(value):
    payload = payload_for()
    result = parse_preparation(
        response(payload, [field("field_1", value, payload["context"][0]["content"])]),
        payload,
        "operation",
        partial=True,
    )[0]
    assert result["suggested_fields"] == []
    assert result["errors"][0]["field_id"] == "field_1"


def test_inferred_project_summary_still_rejects_a_new_route():
    payload = payload_for("sdd")
    result = parse_preparation(
        response(
            payload,
            [field("objective", "Gerenciar tarefas em /admin/tasks", payload["context"][0]["content"])],
        ),
        payload,
        "operation",
        partial=True,
    )[0]
    assert not result["suggested_fields"]
    assert result["errors"][0]["code"] == "PREPARATION_UNGROUNDED_VALUE"


def test_authorization_extraction_does_not_drop_explicit_negation():
    payload = payload_for(text="O gerente não pode exportar tarefas de outra equipe.")
    result = parse_preparation(
        response(
            payload,
            [field("field_2", "Pode exportar tarefas de outra equipe", payload["context"][0]["content"])],
        ),
        payload,
        "operation",
        partial=True,
    )[0]
    assert not result["suggested_fields"]
    assert result["errors"][0]["code"] == "PREPARATION_UNGROUNDED_VALUE"


def test_identity_extraction_does_not_promote_superadmin_to_admin():
    payload = payload_for(text="Somente o superadmin possui essa atribuição.")
    result = parse_preparation(
        response(payload, [field("field_1", "admin", payload["context"][0]["content"])]),
        payload,
        "operation",
        partial=True,
    )[0]
    assert not result["suggested_fields"]
    assert result["errors"][0]["code"] == "PREPARATION_UNGROUNDED_VALUE"


@pytest.mark.parametrize(
    "text,key,value",
    [
        ("Não há administrador configurado neste projeto.", "field_1", "Administrador"),
        ("O gerente pode editar tarefas somente da própria equipe.", "field_2", "Pode editar tarefas"),
        ("Se pertencer à equipe, o gerente pode editar tarefas.", "field_2", "O gerente pode editar tarefas"),
    ],
)
def test_authorization_extract_cannot_omit_negative_identity_or_permission_scope(text, key, value):
    payload = payload_for(text=text)
    result = parse_preparation(
        response(payload, [field(key, value, text)]), payload, "operation", partial=True
    )[0]
    assert result["suggested_fields"] == []
    assert result["errors"][0]["code"] == "PREPARATION_UNGROUNDED_VALUE"


@pytest.mark.parametrize(
    "text",
    [
        "O gerente pode editar tarefas somente da própria equipe.",
        "Se pertencer à equipe, o gerente pode editar tarefas.",
    ],
)
def test_authorization_extract_preserves_the_complete_explicit_permission(text):
    payload = payload_for(text=text)
    result = parse_preparation(
        response(payload, [field("field_2", text, text)]), payload, "operation", partial=True
    )[0]
    assert result["suggested_fields"][0]["value"] == text
    assert result["errors"] == []


def test_partial_mode_does_not_accept_an_unselected_template_or_unknown_field():
    payload = payload_for()
    value = json.loads(response(payload, [field("unknown", "Gerente", "gerente")]))
    with pytest.raises(RuleError) as error:
        parse_preparation(json.dumps(value), payload, "operation", partial=True)
    assert error.value.code == "PREPARATION_UNKNOWN_FIELD"
    value["items"][0]["attachment_id"] = "unselected-private"
    with pytest.raises(RuleError) as error:
        parse_preparation(json.dumps(value), payload, "operation", partial=True)
    assert error.value.code == "PREPARATION_INVALID_RESPONSE"


def test_prompt_does_not_repeat_ui_metadata_or_filled_field_definitions():
    payload = payload_for()
    payload["items"][0]["original_fields"]["field_1"] = "Decisão humana"
    before = copy.deepcopy(payload)
    prompt = json.loads(preparation_prompt(payload))
    fields = prompt["templates"][0]["fields"]
    assert all(value["key"] != "field_1" for value in fields)
    assert all("display_order" not in value and "id" not in value for value in fields)
    assert prompt["templates"][0]["current_values"]["field_1"] == "Decisão humana"
    assert payload == before


@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-6-luna"),
        ("openai", "gpt-6.1-sol"),
        ("openai", "gpt-6-astra"),
        ("gemini", "gemini-3.5-flash-lite"),
        ("gemini", "gemini-3.8-flash"),
        ("gemini", "gemini-4-argon"),
    ],
)
def test_preparation_wire_limits_reasoning_without_raising_output_budget(provider, model):
    calls = []

    def transport(request):
        calls.append(json.loads(request.content))
        if provider == "openai":
            return httpx.Response(
                200,
                json={
                    "usage": {"input_tokens": 100, "output_tokens": 70},
                    "output": [],
                    "status": "completed",
                },
            )
        return httpx.Response(
            200,
            json={
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 50,
                    "thoughtsTokenCount": 20,
                },
                "candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}],
            },
        )

    policy = {
        "provider": provider,
        "model": model,
        "template_preparation_contract": 1,
        "max_output_tokens": 700,
        "input_usd_per_million": "0.1",
        "cached_usd_per_million": "0.01",
        "output_usd_per_million": "0.5",
        "fx_brl_per_usd": "5",
    }
    gateway = HttpModelGateway(
        SimpleNamespace(provider_timeout=5, openai_api_key="synthetic", gemini_api_key="synthetic"),
        transport=httpx.MockTransport(transport),
    )
    result = gateway.generate(policy, "Preparar os campos")
    assert len(calls) == 1
    if provider == "openai":
        assert calls[0]["reasoning"] == {"effort": "low"}
        assert calls[0]["max_output_tokens"] == 700
        assert calls[0]["text"]["format"]["strict"] is True
    else:
        assert calls[0]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "LOW"}
        assert calls[0]["generationConfig"]["maxOutputTokens"] == 700
        assert calls[0]["generationConfig"]["responseSchema"]["type"] == "OBJECT"
    assert result.usage == {"input": 100, "cached": 0, "output": 70}


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_real_http_partial_fields_persist_and_retry_only_unresolved_field(system, workflow, provider):
    flow, actor, project = workflow
    prepare_authorization(flow, actor, project)
    requests = []

    def transport(request):
        prompt = wire_prompt(json.loads(request.content))
        requests.append(prompt)
        return provider_response(provider, authorization_response(prompt, invalid=len(requests) == 1))

    gateway = realistic_gateway(system, provider, transport)
    current, _ = dispatch(system, flow, actor, project, gateway, contract_version=3)
    attachment = current["data"]["attachments"][0]
    assert attachment["fill_state"] == "failed"
    assert attachment["fill_error_code"] == "PREPARATION_INVALID_SOURCE"
    assert attachment["fields"] == {key: value for key, value in AUTH_VALUES.items() if key != "field_2"}
    assert not current["data"]["can_review"] and len(requests) == 1
    retry = {"expected_draft_revision": current["version"], "attachment_ids": ["autorizacao"]}
    flow.retry_fill(actor, project["id"], "retry-partial", retry)
    flow.retry_fill(actor, project["id"], "retry-partial", retry)
    final, _ = dispatch(system, flow, actor, project, gateway, contract_version=3)
    assert [field["key"] for field in requests[1]["templates"][0]["fields"]] == ["field_2"]
    assert final["data"]["attachments"][0]["fields"] == AUTH_VALUES
    assert final["data"]["attachments"][0]["fill_state"] == "ready"
    assert final["data"]["attachments"][0]["fill_error_code"] is None
    assert flow.get(actor, project["id"])["data"]["can_review"]
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reservation)) == 2
        assert db.scalar(select(func.count()).select_from(Ledger).where(Ledger.kind == "debit")) == 2
        attempts = db.scalars(select(Attempt)).all()
        assert len(attempts) == 2 and all(attempt.state == "confirmed" for attempt in attempts)


def test_bounded_repair_keeps_valid_fields_and_human_edit_under_one_reservation(system, workflow):
    flow, actor, project = workflow
    policies = FeaturePolicies(system["repo"], system["catalog"])
    value = policies.current("template_preparation")
    policies.save(
        system["admin"],
        "template_preparation",
        "allow-repair",
        value["version"],
        {
            "settings": {**value["settings"], "max_repairs": 1},
            "reason": "Reparo limitado na validação sintética",
        },
    )
    prepare_authorization(flow, actor, project)
    requests = []

    def transport(request):
        prompt = wire_prompt(json.loads(request.content))
        requests.append(prompt)
        if len(requests) == 2:
            save(
                flow,
                actor,
                project,
                lambda data: data["attachments"][0]["fields"].update(
                    field_1="Decisão humana durante o reparo"
                ),
            )
        return provider_response("openai", authorization_response(prompt, invalid=len(requests) == 1))

    current, _ = dispatch(
        system, flow, actor, project, realistic_gateway(system, "openai", transport), contract_version=3
    )
    assert len(requests) == 2
    assert [field["key"] for field in requests[1]["templates"][0]["fields"]] == ["field_2"]
    assert requests[1]["validation_feedback"][0]["code"] == "PREPARATION_INVALID_SOURCE"
    assert current["data"]["attachments"][0]["fields"] == {
        **AUTH_VALUES,
        "field_1": "Decisão humana durante o reparo",
    }
    assert current["data"]["attachments"][0]["field_values"]["field_1"]["edited_by_user"]
    with system["repo"].sessions() as db:
        operations = db.scalars(select(Operation).where(Operation.type == "template_preparation")).all()
        assert len(operations) == 1 and operations[0].billing_status == "settled"
        assert db.scalar(select(func.count()).select_from(Reservation)) == 1
        assert db.scalar(select(func.count()).select_from(Ledger).where(Ledger.kind == "debit")) == 1
        assert Decimal(system["repo"].wallet(actor, True)["consumed_brl"]) == cost(
            operations[0].policy, {"input": 400, "cached": 0, "output": 200}
        )


def test_manual_completion_of_last_pending_field_avoids_a_repair_call(system, workflow):
    flow, actor, project = workflow
    policies = FeaturePolicies(system["repo"], system["catalog"])
    value = policies.current("template_preparation")
    policies.save(
        system["admin"],
        "template_preparation",
        "allow-one-repair",
        value["version"],
        {
            "settings": {**value["settings"], "max_repairs": 1},
            "reason": "Validar reparo limitado com edição concorrente",
        },
    )
    prepare_authorization(flow, actor, project)
    requests = []

    def transport(request):
        prompt = wire_prompt(json.loads(request.content))
        requests.append(prompt)
        save(
            flow,
            actor,
            project,
            lambda data: data["attachments"][0]["fields"].update(field_2=AUTH_VALUES["field_2"]),
        )
        return provider_response("openai", authorization_response(prompt, invalid=True))

    current, _ = dispatch(
        system, flow, actor, project, realistic_gateway(system, "openai", transport), contract_version=3
    )
    assert len(requests) == 1
    attachment = current["data"]["attachments"][0]
    assert attachment["fields"] == AUTH_VALUES and attachment["fill_state"] == "ready"
    assert attachment["field_values"]["field_2"]["edited_by_user"]
    with system["repo"].sessions() as db:
        operation = db.scalar(select(Operation).where(Operation.type == "template_preparation"))
        assert db.scalar(select(func.count()).select_from(Reservation)) == 1
        assert Decimal(system["repo"].wallet(actor, True)["consumed_brl"]) == cost(
            operation.policy, {"input": 200, "cached": 0, "output": 100}
        )


def test_repair_prompt_respects_a_human_clearing_a_previously_suggested_field(system, workflow):
    flow, actor, project = workflow
    policies = FeaturePolicies(system["repo"], system["catalog"])
    value = policies.current("template_preparation")
    policies.save(
        system["admin"],
        "template_preparation",
        "allow-one-repair",
        value["version"],
        {
            "settings": {**value["settings"], "max_repairs": 1},
            "reason": "Validar limpeza humana durante reparo limitado",
        },
    )
    prepare_authorization(flow, actor, project)
    requests = []

    def transport(request):
        prompt = wire_prompt(json.loads(request.content))
        requests.append(prompt)
        if len(requests) == 1:
            save(
                flow,
                actor,
                project,
                lambda data: data["attachments"][0]["fields"].update(field_1="Valor temporário humano"),
            )
            save(flow, actor, project, lambda data: data["attachments"][0]["fields"].update(field_1=""))
        return provider_response("openai", authorization_response(prompt, invalid=len(requests) == 1))

    current, _ = dispatch(
        system, flow, actor, project, realistic_gateway(system, "openai", transport), contract_version=3
    )
    assert len(requests) == 2
    assert requests[1]["templates"][0]["current_values"]["field_1"] == ""
    assert [field["key"] for field in requests[1]["templates"][0]["fields"]] == ["field_2"]
    attachment = current["data"]["attachments"][0]
    assert attachment["fields"] == {**AUTH_VALUES, "field_1": ""}
    assert attachment["field_values"]["field_1"]["edited_by_user"]
    assert attachment["fill_state"] == "needs_input" and not current["data"]["can_review"]


def test_uncertain_repair_preserves_known_fields_without_replay_or_zero_settlement(system, workflow):
    flow, actor, project = workflow
    policies = FeaturePolicies(system["repo"], system["catalog"])
    value = policies.current("template_preparation")
    policies.save(
        system["admin"],
        "template_preparation",
        "allow-one-repair",
        value["version"],
        {
            "settings": {**value["settings"], "max_repairs": 1},
            "reason": "Validar conciliação após resposta parcial",
        },
    )
    prepare_authorization(flow, actor, project)
    requests = []

    def transport(request):
        prompt = wire_prompt(json.loads(request.content))
        requests.append(prompt)
        if len(requests) == 2:
            raise httpx.ReadTimeout("synthetic private timeout")
        return provider_response("openai", authorization_response(prompt, invalid=True))

    gateway = realistic_gateway(system, "openai", transport)
    current, _ = dispatch(system, flow, actor, project, gateway, contract_version=3)
    attachment = current["data"]["attachments"][0]
    assert attachment["fields"] == {key: value for key, value in AUTH_VALUES.items() if key != "field_2"}
    assert (
        attachment["fill_state"] == "reconciling"
        and attachment["fill_error_code"] == "PROVIDER_RESULT_UNKNOWN"
    )
    assert len(requests) == 2 and not Worker(system["repo"], gateway, system["catalog"]).tick(
        attachment["preparation_id"]
    )
    with pytest.raises(RuleError) as error:
        flow.retry_fill(
            actor,
            project["id"],
            "blocked-retry",
            {"expected_draft_revision": current["version"], "attachment_ids": ["autorizacao"]},
        )
    assert error.value.code == "FILL_PENDING"
    wallet = system["repo"].wallet(actor, True)
    assert Decimal(wallet["reserved_brl"]) > 0 and Decimal(wallet["consumed_brl"]) == 0
    with system["repo"].sessions() as db:
        operation = db.get(Operation, attachment["preparation_id"])
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation.id))
        assert operation.state == "reconciling" and operation.billing_status == "reconciliation_pending"
        assert attempt.state == "uncertain" and attempt.usage["output"] == 100
        assert attempt.actual_brl == cost(operation.policy, {"input": 200, "cached": 0, "output": 100})
        assert db.scalar(select(func.count()).select_from(Ledger).where(Ledger.kind == "debit")) == 0


@pytest.mark.parametrize("uncertain", [False, True])
def test_removing_an_attachment_before_failed_repair_keeps_remaining_fields_and_billing(
    system, workflow, uncertain
):
    flow, actor, project = workflow
    policies = FeaturePolicies(system["repo"], system["catalog"])
    value = policies.current("template_preparation")
    policies.save(
        system["admin"],
        "template_preparation",
        "allow-one-repair",
        value["version"],
        {
            "settings": {**value["settings"], "max_repairs": 1},
            "reason": "Validar remoção de anexo durante reparo limitado",
        },
    )
    prepare_authorization(flow, actor, project)
    select_template(flow, actor, project, "sdd")
    requests = []

    def transport(request):
        prompt = wire_prompt(json.loads(request.content))
        requests.append(prompt)
        if len(requests) == 2:
            if uncertain:
                raise httpx.ReadTimeout("synthetic timeout")
            return httpx.Response(400, json={"error": {"message": "synthetic rejection"}})
        version = prompt.get("template_preparation_contract", 1)
        result = {} if version == 3 else []
        for item in prompt["templates"]:
            if item["attachment_id"] == "autorizacao":
                authorization = json.loads(authorization_response({**prompt, "templates": [item]}))["items"]
                if version == 3:
                    result.update(authorization)
                else:
                    result.extend(authorization)
            elif version == 3:
                result["sdd"] = {
                    "objective": {"proposal": "Organizar as tarefas da própria equipe"},
                    "audience": {"proposal": "Gerente da equipe"},
                    "acceptance": {"extracts": [{"unit": 999999, "start": 0, "end": 0}]},
                }
            else:
                result.append(
                    {
                        "attachment_id": "sdd",
                        "fields": [
                            provider_field(
                                prompt,
                                "objective",
                                "Organizar as tarefas da própria equipe",
                                "tarefas da própria equipe",
                            ),
                            provider_field(prompt, "audience", "Gerente da equipe", "gerente da equipe"),
                            provider_field(
                                prompt,
                                "acceptance",
                                "Ações permitidas respeitam a equipe",
                                "evidência não recebida",
                            ),
                        ],
                        "questions": [],
                    }
                )
        save(
            flow,
            actor,
            project,
            lambda data: data.update(
                attachments=[item for item in data["attachments"] if item["template_id"] == "sdd"]
            ),
        )
        return provider_response("openai", json.dumps({"items": result}))

    gateway = realistic_gateway(system, "openai", transport)
    current, _ = dispatch(system, flow, actor, project, gateway, contract_version=3)
    assert len(requests) == 2 and [item["attachment_id"] for item in requests[1]["templates"]] == ["sdd"]
    assert len(current["data"]["attachments"]) == 1
    attachment = current["data"]["attachments"][0]
    assert attachment["template_id"] == "sdd"
    assert attachment["fields"] == {
        "objective": "Organizar as tarefas da própria equipe",
        "audience": "Gerente da equipe",
    }
    assert attachment["fill_state"] == ("reconciling" if uncertain else "failed")
    preparation = TemplatePreparations(system["repo"], None, system["catalog"]).get(
        actor, project["id"], attachment["preparation_id"]
    )
    assert [item["attachment_id"] for item in preparation["items"]] == ["sdd"]
    wallet = system["repo"].wallet(actor, True)
    if uncertain:
        assert Decimal(wallet["reserved_brl"]) > 0 and Decimal(wallet["consumed_brl"]) == 0
    else:
        assert Decimal(wallet["reserved_brl"]) == 0
        with system["repo"].sessions() as db:
            operation = db.get(Operation, attachment["preparation_id"])
            assert Decimal(wallet["consumed_brl"]) == cost(
                operation.policy, {"input": 200, "cached": 0, "output": 100}
            )
    assert not Worker(system["repo"], gateway, system["catalog"]).tick(attachment["preparation_id"])


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_provider_truncation_is_actionable_and_settles_observed_usage_once(system, workflow, provider):
    flow, actor, project = workflow
    prepare_authorization(flow, actor, project)
    calls = []

    def transport(request):
        calls.append(request)
        return provider_response(provider, '{"items":[', truncated=True)

    current, _ = dispatch(
        system, flow, actor, project, realistic_gateway(system, provider, transport), contract_version=3
    )
    attachment = current["data"]["attachments"][0]
    assert len(calls) == 1 and attachment["fields"] == {}
    assert attachment["fill_state"] == "failed" and attachment["fill_error_code"] == "PREPARATION_TRUNCATED"
    assert "limite de saída" in attachment["fill_message"]
    with system["repo"].sessions() as db:
        op = db.scalar(select(Operation).where(Operation.type == "template_preparation"))
        assert op.billing_status == "settled" and op.state == "failed"
        assert db.scalar(select(Attempt).where(Attempt.operation_id == op.id)).usage["output"] == 100
        assert Decimal(system["repo"].wallet(actor, True)["consumed_brl"]) == cost(
            op.policy, {"input": 200, "cached": 0, "output": 100}
        )


def test_failed_dispatcher_does_not_leave_attachment_permanently_queued(system, workflow):
    flow, actor, project = workflow
    prepare_authorization(flow, actor, project)

    class FailingCatalog:
        def eligible(self):
            raise ValueError("synthetic private error not for the UI")

    worker = Worker(system["repo"], None, FailingCatalog())
    with system["repo"].sessions.begin() as db:
        operation = db.scalar(select(Operation).where(Operation.type == "template_autofill"))
        identity = operation.id
        db.scalar(select(Job).where(Job.operation_id == identity)).available_at = utcnow() - timedelta(
            seconds=1
        )
    assert worker.tick(identity)
    current = flow.get(actor, project["id"])
    attachment = current["data"]["attachments"][0]
    assert attachment["fill_state"] == "failed" and attachment["fill_error_code"] == "PROCESSING_FAILED"
    assert "synthetic private error" not in attachment["fill_message"]
    retried = flow.retry_fill(
        actor,
        project["id"],
        "retry-dispatch",
        {"expected_draft_revision": current["version"], "attachment_ids": ["autorizacao"]},
    )
    assert retried["data"]["attachments"][0]["fill_state"] == "queued"
    assert Decimal(system["repo"].wallet(actor, True)["consumed_brl"]) == 0
