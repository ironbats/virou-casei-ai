import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from test_template_preparations import PreparationGateway, preparation, request_for, snapshot_contract

from viracase.application.ports import ModelResult
from viracase.application.template_preparation import (
    merge_preparation,
    parse_preparation,
    parse_preparation_result,
    preparation_prompt,
    preparation_schema,
)
from viracase.domain.rules import RuleError, cost
from viracase.domain.template_fields import normalized_fields
from viracase.infrastructure.feature_policies import FeaturePolicies
from viracase.infrastructure.models import Attempt, Ledger, Operation, Reservation
from viracase.infrastructure.worker import Worker

__all__ = ["preparation"]


def payload(version=1, kind="text", sources=None):
    definition = {
        "key": "answer",
        "label": "Resposta",
        "required": True,
        "max_length": 1000,
        "type": kind,
        "allowed_source_types": sources or ["request", "project_context", "user_answer", "policy_suggestion"],
    }
    if kind == "enum":
        definition["constraints"] = {"enum": ["true", "false"]}
    return {
        "template_preparation_contract": version,
        "items": [
            {
                "attachment_id": "selected",
                "source_version_id": "version-one",
                "attachment_revision": 1,
                "title": "Template de teste",
                "original_fields": {},
                "definitions": normalized_fields({"content": "Resposta: {{answer}}", "fields": [definition]}),
            }
        ],
        "context": [
            {
                "id": "request:message",
                "type": "request",
                "content": "O cliente cadastrado pode efetivar a compra quando estiver autenticado.",
            },
            {
                "id": "file:auth",
                "type": "project_context",
                "content": "A identidade confirmada é cliente cadastrado.",
            },
            {
                "id": "answer:owner",
                "type": "user_answer",
                "content": "Cliente cadastrado é o ator deste fluxo.",
            },
            {
                "id": "conversation:proposal",
                "type": "policy_suggestion",
                "content": "Administrador global foi sugerido anteriormente, sem validação.",
            },
        ],
    }


def outer(field):
    return json.dumps(
        {
            "items": [
                {"attachment_id": "selected", "fields": [{"field_id": "answer", **field}], "questions": []}
            ]
        }
    )


def v1_field(encoded):
    return {
        "value_json": encoded,
        "source_type": "request",
        "source_refs": ["request:message"],
        "evidence": "cliente cadastrado",
    }


def v2_field(value, citations=None):
    return {
        "value": value,
        "citations": citations
        if citations is not None
        else [{"source_ref": "request:message", "quote": "cliente cadastrado"}],
    }


def parsed(data, field):
    return parse_preparation(outer(field), data, "operation", partial=True)[0]


def test_legacy_schema_valid_string_can_still_fail_its_embedded_json_contract():
    schema = preparation_schema()["properties"]["items"]["items"]["properties"]["fields"]["items"]
    assert schema["properties"]["value_json"] == {"type": "string"}
    raw = "Cliente cadastrado"
    assert isinstance(raw, str)
    result = parsed(payload(), v1_field(raw))
    assert not result["suggested_fields"]
    assert result["errors"][0]["code"] == "PREPARATION_FIELD_TYPE"
    valid = parsed(payload(), v1_field(json.dumps(raw)))
    assert valid["suggested_fields"][0]["value"] == raw


@pytest.mark.parametrize(
    "encoded", ["123", "true", '["Cliente cadastrado"]', '{"value":"Cliente cadastrado"}']
)
def test_legacy_text_field_rejects_other_embedded_json_types(encoded):
    result = parsed(payload(), v1_field(encoded))
    assert not result["suggested_fields"]
    assert result["errors"][0]["code"] == "PREPARATION_FIELD_TYPE"


@pytest.mark.parametrize(
    "kind,value",
    [
        ("text", "Texto sem serialização interna"),
        ("boolean", False),
        ("string_list", ["Primeiro critério", "Segundo critério"]),
        ("enum", "false"),
    ],
)
def test_native_contract_preserves_the_declared_json_type_without_coercion(kind, value):
    result = parsed(payload(2, kind), v2_field(value, []))
    assert result["suggested_fields"][0]["value"] == value
    assert type(result["suggested_fields"][0]["value"]) is type(value)
    assert result["suggested_fields"][0]["source_type"] == "policy_suggestion"


@pytest.mark.parametrize("value", [{"value": "Cliente cadastrado"}, 123, True, ["Cliente cadastrado"]])
def test_native_text_contract_does_not_stringify_wrong_types(value):
    result = parsed(payload(2), v2_field(value))
    assert not result["suggested_fields"]
    assert result["errors"][0]["code"] == "PREPARATION_FIELD_TYPE"
    assert result["errors"][0]["reason_code"] == "VALUE_TYPE_MISMATCH"


def test_native_null_leaves_a_required_question_without_fabricating_content():
    result = parsed(payload(2, sources=["request"]), v2_field(None, []))
    assert result["suggested_fields"] == []
    assert result["questions"][0]["field_ids"] == ["answer"]


def test_native_prompt_and_schema_bind_references_to_factual_context_ids():
    data = payload(2)
    prompt = json.loads(preparation_prompt(data))
    schema = prompt["schema"]
    field_schema = schema["properties"]["items"]["items"]["properties"]["fields"]["items"]
    assert set(field_schema["properties"]) == {"field_id", "value", "citations"}
    assert {item["type"] for item in field_schema["properties"]["value"]["anyOf"]} == {
        "string",
        "boolean",
        "array",
        "null",
    }
    refs = field_schema["properties"]["citations"]["items"]["properties"]["source_ref"]["enum"]
    assert refs == ["request:message", "file:auth", "answer:owner"]
    assert schema == preparation_schema(2, refs)
    assert "value_json" not in json.dumps(prompt)


def test_native_citation_derives_source_type_and_validates_quote_in_its_own_source():
    data = payload(2, sources=["project_context"])
    result = parsed(
        data,
        v2_field(
            "Cliente cadastrado",
            [{"source_ref": "file:auth", "quote": "A identidade confirmada é cliente cadastrado."}],
        ),
    )
    assert result["suggested_fields"][0]["source_type"] == "project_context"
    assert result["suggested_fields"][0]["source_refs"] == ["file:auth"]
    invalid = parsed(
        data, v2_field("Cliente cadastrado", [{"source_ref": "file:auth", "quote": "pode efetivar a compra"}])
    )
    assert not invalid["suggested_fields"]
    assert invalid["errors"][0]["reason_code"] == "EVIDENCE_NOT_FOUND"


@pytest.mark.parametrize(
    "citations,reason",
    [
        ([{"source_ref": "other:project", "quote": "cliente cadastrado"}], "SOURCE_REF_UNKNOWN"),
        ([{"source_ref": "conversation:proposal", "quote": "Administrador global"}], "SOURCE_NOT_FACTUAL"),
        (
            [
                {"source_ref": "request:message", "quote": "cliente cadastrado"},
                {"source_ref": "file:auth", "quote": "cliente cadastrado"},
            ],
            "SOURCE_TYPE_MISMATCH",
        ),
    ],
)
def test_native_source_errors_are_specific_and_do_not_promote_unknown_or_mixed_origins(citations, reason):
    result = parsed(payload(2), v2_field("Cliente cadastrado", citations))
    assert not result["suggested_fields"]
    assert result["errors"][0]["code"] == "PREPARATION_INVALID_SOURCE"
    assert result["errors"][0]["reason_code"] == reason


def test_native_contract_cannot_substitute_legacy_encoded_values():
    with pytest.raises(RuleError) as caught:
        parsed(payload(2), v1_field(json.dumps("Cliente cadastrado")))
    assert caught.value.code == "PREPARATION_INVALID_RESPONSE"


def test_native_factual_field_cannot_use_an_unsubstantiated_proposal():
    result = parsed(payload(2, sources=["request", "project_context"]), v2_field("Administrador global", []))
    assert not result["suggested_fields"]
    assert result["errors"][0]["reason_code"] == "SOURCE_TYPE_NOT_ALLOWED"


def test_native_repair_keeps_an_explicit_question_for_an_essential_fact():
    data = payload(2, sources=["request", "project_context", "user_answer"])
    first = parse_preparation(
        outer(v2_field("Identidade de sessão não informada", [])), data, "operation", partial=True
    )
    repair = json.loads(outer(v2_field(None, [])))
    repair["items"][0]["questions"] = [
        {"question": "Qual fonte confiável identifica o dono deste recurso?", "field_ids": ["answer"]}
    ]
    current, explicit = parse_preparation_result(json.dumps(repair), data, "operation", partial=True)
    result = merge_preparation(first, current, explicit)[0]
    assert result["errors"] == []
    assert result["suggested_fields"] == []
    assert result["questions"][0]["field_ids"] == ["answer"]
    assert result["state"] == "needs_input"


def test_synthesized_question_cannot_hide_an_unresolved_provider_error():
    data = payload(2, sources=["request"])
    first = parse_preparation(outer(v2_field("Proposta sem fonte", [])), data, "operation", partial=True)
    current, explicit = parse_preparation_result(outer(v2_field(None, [])), data, "operation", partial=True)
    assert current[0]["questions"] and not explicit
    result = merge_preparation(first, current, explicit)[0]
    assert result["errors"][0]["reason_code"] == "SOURCE_TYPE_NOT_ALLOWED"
    assert result["questions"] == []


def test_question_does_not_hide_a_current_invalid_value_or_a_protected_field():
    data = payload(2, sources=["request"])
    first = parse_preparation(outer(v2_field("Proposta sem fonte", [])), data, "operation", partial=True)
    repair = json.loads(outer(v2_field({"invalid": "object"})))
    repair["items"][0]["questions"] = [
        {"question": "Qual informação devemos utilizar?", "field_ids": ["answer"]}
    ]
    current, explicit = parse_preparation_result(json.dumps(repair), data, "operation", partial=True)
    assert not explicit
    result = merge_preparation(first, current, explicit)[0]
    assert result["errors"][0]["reason_code"] == "VALUE_TYPE_MISMATCH" and not result["questions"]
    data["items"][0]["protected_field_ids"] = ["answer"]
    data["items"][0]["original_fields"] = {"answer": "Decisão humana preservada"}
    protected, explicit = parse_preparation_result(json.dumps(repair), data, "operation", partial=True)
    assert not explicit and not protected[0]["questions"] and not protected[0]["suggested_fields"]
    assert data["items"][0]["original_fields"]["answer"] == "Decisão humana preservada"


def test_worker_persists_an_explicit_repair_question_under_the_same_reservation(system, preparation):
    service, _, actor, project, template = preparation
    policies = FeaturePolicies(system["repo"], system["catalog"])
    current = policies.current("template_preparation")
    policies.save(
        system["admin"],
        "template_preparation",
        "one-repair",
        current["version"],
        {
            "settings": {**current["settings"], "max_repairs": 1},
            "reason": "Validar pergunta essencial durante reparo limitado",
        },
    )
    first_key = template["fields"][0]["key"]

    class QuestionAfterErrorGateway(PreparationGateway):
        def generate(self, policy, prompt):
            result = super().generate(policy, prompt)
            body = json.loads(result.text)
            if policy.get("template_preparation_contract") == 3:
                attachment = next(iter(body["items"].values()))
                attachment[first_key] = (
                    {"extracts": [{"unit": 999999, "start": 0, "end": 0}]}
                    if len(self.calls) == 1
                    else {"missing": "Qual informação confiável devemos usar neste campo?"}
                )
                return ModelResult(json.dumps(body), result.usage, result.external_id)
            if len(self.calls) == 1:
                body["items"][0]["fields"][0]["citations"] = [
                    {"source_ref": "not-received", "quote": "fonte ausente"}
                ]
            else:
                body["items"][0]["fields"] = [{"field_id": first_key, "value": None, "citations": []}]
                body["items"][0]["questions"] = [
                    {
                        "question": "Qual informação confiável devemos usar neste campo?",
                        "field_ids": [first_key],
                    }
                ]
            return ModelResult(json.dumps(body), result.usage, result.external_id)

    initial = service.start(actor, project["id"], "repair-question", request_for(preparation))
    snapshot_contract(system["repo"], initial["id"], 3)
    gateway = QuestionAfterErrorGateway()
    assert Worker(system["repo"], gateway, system["catalog"]).tick(initial["id"])
    result = service.get(actor, project["id"], initial["id"])
    assert result["state"] == "needs_input" and len(gateway.calls) == 2
    assert result["items"][0]["errors"] == []
    assert result["items"][0]["questions"][0]["field_ids"] == [first_key]
    assert len(result["items"][0]["suggested_fields"]) == len(template["fields"]) - 1
    with system["repo"].sessions() as db:
        operation = db.get(Operation, initial["id"])
        assert operation.billing_status == "settled"
        assert db.scalar(select(func.count()).select_from(Reservation)) == 1
        assert db.scalar(select(func.count()).select_from(Ledger).where(Ledger.kind == "debit")) == 1
        assert Decimal(system["repo"].wallet(actor, True)["consumed_brl"]) == cost(
            operation.policy, {"input": 400, "cached": 0, "output": 160}
        )


@pytest.mark.parametrize("contract", [1, 2, 3])
def test_already_reserved_legacy_operation_keeps_its_original_contract_and_charge(
    system, preparation, contract
):
    service, _, actor, project, _ = preparation
    initial = service.start(actor, project["id"], "legacy-durable", request_for(preparation))
    snapshot_contract(system["repo"], initial["id"], contract)
    gateway = PreparationGateway()
    assert Worker(system["repo"], gateway, system["catalog"]).tick(initial["id"])
    current = service.get(actor, project["id"], initial["id"])
    assert current["state"] == "ready" and len(gateway.calls) == 1
    policy, prompt = gateway.calls[0]
    assert policy["template_preparation_contract"] == contract
    assert json.loads(prompt)["template_preparation_contract"] == contract
    with system["repo"].sessions() as db:
        operation = db.get(Operation, initial["id"])
        assert operation.billing_status == "settled"
        assert db.scalar(select(func.count()).select_from(Reservation)) == 1
        assert db.scalar(select(func.count()).select_from(Ledger).where(Ledger.kind == "debit")) == 1
        assert db.scalar(select(Attempt).where(Attempt.operation_id == operation.id)).state == "confirmed"
    assert Decimal(system["repo"].wallet(actor, True)["consumed_brl"]) == cost(
        policy, {"input": 200, "cached": 0, "output": 80}
    )


@pytest.mark.parametrize("legacy_contract", [1, 2])
def test_new_operation_rejects_legacy_response_without_coercion_or_extra_calls(
    system, preparation, legacy_contract
):
    service, _, actor, project, _ = preparation
    initial = service.start(actor, project["id"], "native-durable", request_for(preparation))

    class LegacyResponseGateway(PreparationGateway):
        def generate(self, policy, prompt):
            assert policy["template_preparation_contract"] == 4
            request = json.loads(prompt)
            assert request["template_preparation_contract"] == 4
            self.calls.append((policy, prompt))
            items = []
            for item in request["templates"]:
                fields = [
                    {
                        "field_id": definition["key"],
                        **(
                            {"value": "Proposta de tarefa", "citations": []}
                            if legacy_contract == 2
                            else {
                                "value_json": json.dumps("Proposta de tarefa"),
                                "source_type": "policy_suggestion",
                                "source_refs": [],
                                "evidence": "",
                            }
                        ),
                    }
                    for definition in item["fields"]
                ]
                items.append({"attachment_id": item["attachment_id"], "fields": fields, "questions": []})
            return ModelResult(
                json.dumps({"items": items}), {"input": 200, "cached": 0, "output": 80}, "synthetic-response"
            )

    gateway = LegacyResponseGateway()
    assert Worker(system["repo"], gateway, system["catalog"]).tick(initial["id"])
    current = service.get(actor, project["id"], initial["id"])
    assert current["state"] == "failed" and current["error_code"] == "PREPARATION_INVALID_RESPONSE"
    assert len(gateway.calls) == 1 and not current["can_apply"]
    with system["repo"].sessions() as db:
        operation = db.get(Operation, initial["id"])
        assert operation.policy["template_preparation_contract"] == 4
        assert operation.payload["template_preparation_contract"] == 4
        assert operation.billing_status == "settled"
        assert db.scalar(select(func.count()).select_from(Ledger).where(Ledger.kind == "debit")) == 1
        assert Decimal(system["repo"].wallet(actor, True)["consumed_brl"]) == cost(
            operation.policy, {"input": 200, "cached": 0, "output": 80}
        )
