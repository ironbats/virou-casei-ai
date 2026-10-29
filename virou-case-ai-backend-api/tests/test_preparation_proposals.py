import copy
import json

import pytest
from test_preparation_provider_values import payload
from test_preparation_reliability import payload_for

from viracase.application.template_preparation import (
    merge_preparation,
    parse_preparation,
    preparation_input_bytes,
    preparation_prompt,
    preparation_schema,
)
from viracase.application.template_preparation_proposals import build_proposals, proposal_call_schema
from viracase.domain.rules import RuleError

PROPOSALS = {
    "field_1": "Cliente autenticado que solicita concluir sua compra.",
    "field_2": "Permitir concluir a compra após confirmar cadastro, autenticação e propriedade do pedido.",
    "field_3": "Pedido de compra e seus itens do carrinho, vinculados ao cliente autenticado.",
    "field_4": "Obter no servidor a identidade da sessão autenticada e conferir a propriedade do pedido, sem confiar no dono enviado pelo cliente.",
    "field_5": "Autorizar a compra somente com sessão válida e pedido pertencente ao cliente autenticado.",
    "field_6": "Negar por padrão quando a identidade, a sessão ou a propriedade não forem confirmadas.",
    "field_7": "Verificar que um cliente não consegue concluir, ler nem alterar o pedido de outro cliente.",
    "field_8": "Exigir nova autenticação quando a sessão expirar e preservar os itens para nova tentativa.",
    "field_9": "Informar indisponibilidade do pedido sem revelar dados de outros clientes.",
    "field_10": "Registrar a ação solicitada e a decisão de autorização sem credenciais ou conteúdo pessoal.",
}


def proposal_response(values, attachment="selected"):
    return json.dumps({"items": {attachment: {key: {"proposal": value} for key, value in values.items()}}})


def test_planning_fills_all_authorization_fields_without_claiming_an_existing_owner_source():
    data = payload_for(
        text="Melhorar a página inicial e o carrinho; o cliente deve cadastrar e entrar para concluir a compra."
    )
    data["template_preparation_contract"] = 4
    before = copy.deepcopy(data["items"][0]["definitions"])
    assert all("policy_suggestion" not in field["allowed_source_types"] for field in before[:5])
    result = parse_preparation(proposal_response(PROPOSALS, "autorizacao"), data, "proposal", partial=True)[0]
    assert result["state"] == "ready" and not result["errors"] and not result["questions"]
    assert {field["field_id"]: field["value"] for field in result["suggested_fields"]} == PROPOSALS
    assert all(
        field["source_type"] == "policy_suggestion" and field["source_refs"] == []
        for field in result["suggested_fields"]
    )
    assert data["items"][0]["definitions"] == before


@pytest.mark.parametrize(
    "kind,value",
    [
        ("text", "Propor acesso limitado às tarefas da equipe."),
        ("boolean", False),
        ("string_list", ["Validar sessão", "Conferir propriedade"]),
        ("enum", "false"),
    ],
)
def test_proposals_preserve_native_types_and_source_semantics(kind, value):
    data = payload(4, kind, ["project_context"])
    result = parse_preparation(proposal_response({"answer": value}), data, "proposal", partial=True)[0]
    assert len(result["suggested_fields"]) == 1 and not result["questions"] and not result["errors"]
    field = result["suggested_fields"][0]
    assert field["value"] == value and type(field["value"]) is type(value)
    assert field["source_type"] == "policy_suggestion" and field["source_refs"] == []


@pytest.mark.parametrize(
    "kind,value,reason",
    [
        ("text", None, "PROPOSAL_EMPTY"),
        ("text", "", "PROPOSAL_EMPTY"),
        ("text", "  \t", "PROPOSAL_EMPTY"),
        ("string_list", [], "PROPOSAL_EMPTY"),
        ("text", "A definir", "VALUE_PLACEHOLDER"),
        ("text", "{{dono}}", "VALUE_PLACEHOLDER"),
        ("text", {"value": "Proposta"}, "VALUE_TYPE_MISMATCH"),
        ("text", True, "VALUE_TYPE_MISMATCH"),
        ("boolean", "false", "VALUE_TYPE_MISMATCH"),
        ("string_list", [""], "VALUE_TYPE_MISMATCH"),
        ("enum", "não permitido", "VALUE_CHOICE_INVALID"),
    ],
)
def test_empty_placeholder_and_wrong_type_are_field_errors_without_a_question(kind, value, reason):
    data = payload(4, kind)
    result = parse_preparation(proposal_response({"answer": value}), data, "proposal", partial=True)[0]
    assert result["state"] == "needs_input"
    assert result["suggested_fields"] == [] and result["questions"] == []
    assert result["errors"][0]["reason_code"] == reason


@pytest.mark.parametrize("value", ["Testes passaram", "Build aprovado", "Executamos os testes", "12 passed"])
def test_proposals_cannot_claim_execution_in_a_regular_field(value):
    result = parse_preparation(proposal_response({"answer": value}), payload(4), "proposal", partial=True)[0]
    assert not result["suggested_fields"]
    assert result["errors"][0]["reason_code"] == "VALUE_UNGROUNDED_EXECUTION"


def test_missing_one_proposal_preserves_nine_and_repairs_only_the_missing_field():
    data = payload_for(text="Organizar o fluxo de compra autenticada de clientes.")
    data["template_preparation_contract"] = 4
    data["proposal_schema"] = build_proposals(data)
    initial = parse_preparation(
        proposal_response(
            {key: value for key, value in PROPOSALS.items() if key != "field_4"}, "autorizacao"
        ),
        data,
        "proposal",
        partial=True,
    )
    assert len(initial[0]["suggested_fields"]) == 9
    assert initial[0]["errors"][0]["field_id"] == "field_4"
    assert initial[0]["errors"][0]["reason_code"] == "PROPOSAL_FIELD_MISSING"
    assert not initial[0]["questions"]
    data["items"][0]["context_fields"] = {
        field["field_id"]: field["value"] for field in initial[0]["suggested_fields"]
    }
    assert list(
        proposal_call_schema(data)["properties"]["items"]["properties"]["autorizacao"]["properties"]
    ) == ["field_4"]
    repaired = parse_preparation(
        proposal_response({"field_4": PROPOSALS["field_4"]}, "autorizacao"), data, "proposal", partial=True
    )
    final = merge_preparation(initial, repaired)[0]
    assert final["state"] == "ready" and len(final["suggested_fields"]) == 10
    assert not final["errors"] and not final["questions"]


@pytest.mark.parametrize(
    "choice",
    [
        {"missing": "Qual é o dono?"},
        {"extracts": [{"unit": 0, "start": 0, "end": 1}]},
        {"proposal": "Sugestão", "source_type": "project_context"},
    ],
)
def test_proposal_contract_does_not_accept_legacy_modes_or_claimed_provenance(choice):
    result = parse_preparation(
        json.dumps({"items": {"selected": {"answer": choice}}}), payload(4), "proposal", partial=True
    )[0]
    assert not result["suggested_fields"] and not result["questions"]
    assert result["errors"][0]["reason_code"] == "PROPOSAL_FORMAT_INVALID"


def test_proposal_schema_binds_fields_but_has_no_ranges_quotes_or_missing_branch():
    data = payload(4)
    schema = build_proposals(data)
    data["proposal_schema"] = schema
    assert preparation_schema(4, proposal_schema=schema) == schema
    field = schema["properties"]["items"]["properties"]["selected"]["properties"]["answer"]
    assert field["required"] == ["proposal"] and not field["additionalProperties"]
    assert field["properties"]["proposal"]["minLength"] == 1
    listed = build_proposals(payload(4, "string_list"))
    assert (
        listed["properties"]["items"]["properties"]["selected"]["properties"]["answer"]["properties"][
            "proposal"
        ]["minItems"]
        == 1
    )
    prompt = json.loads(preparation_prompt(data))
    assert prompt["context"] == data["context"]
    assert "schema" not in prompt and "selection_catalog" not in prompt
    assert prompt["templates"][0]["fields"][0]["suggestion_mode"] == "planning_proposal"
    assert "allowed_source_types" not in prompt["templates"][0]["fields"][0]
    assert preparation_input_bytes(data) == len(preparation_prompt(data).encode()) + len(
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()
    )
    with pytest.raises(RuleError) as caught:
        preparation_schema(4)
    assert caught.value.code == "PREPARATION_INVALID_CONTRACT"


def test_proposal_unknown_field_is_rejected_and_protected_value_is_never_returned():
    data = payload(4)
    data["proposal_schema"] = build_proposals(data)
    with pytest.raises(RuleError) as caught:
        parse_preparation(
            proposal_response({"answer": "Proposta", "foreign": "Outro campo"}),
            data,
            "proposal",
            partial=True,
        )
    assert caught.value.code == "PREPARATION_UNKNOWN_FIELD"
    data["items"][0]["protected_field_ids"] = ["answer"]
    result = parse_preparation(proposal_response({"answer": "Valor tardio"}), data, "proposal", partial=True)[
        0
    ]
    assert not result["suggested_fields"] and not result["errors"] and not result["questions"]


def test_proposal_list_schema_enforces_each_item_and_matches_default_item_limit():
    data = payload(4, "string_list")
    field = data["items"][0]["definitions"][0]
    field["constraints"].update(min_length=3, max_length=80)
    schema = build_proposals(data)["properties"]["items"]["properties"]["selected"]["properties"]["answer"][
        "properties"
    ]["proposal"]
    assert schema == {
        "type": "array",
        "minItems": 1,
        "maxItems": 30,
        "items": {"type": "string", "minLength": 3, "maxLength": 80},
    }


def test_proposal_enum_is_explicit_until_the_global_wire_limit_and_always_validated_locally():
    data = payload(4, "enum")
    field = data["items"][0]["definitions"][0]
    field["constraints"]["enum"] = ["Opção " + str(index) for index in range(50)]
    data["items"][0]["definitions"] = [{**field, "key": "answer_" + str(index)} for index in range(20)]
    schema = build_proposals(data)["properties"]["items"]["properties"]["selected"]["properties"]
    assert sum(len(value["properties"]["proposal"]["enum"]) for value in schema.values()) == 1000
    data["items"][0]["definitions"].append({**field, "key": "answer_20"})
    schema = build_proposals(data)["properties"]["items"]["properties"]["selected"]["properties"]
    assert all("enum" not in value["properties"]["proposal"] for value in schema.values())
    prompt = json.loads(preparation_prompt(data))
    assert all(
        value["constraints"]["enum"] == field["constraints"]["enum"]
        for value in prompt["templates"][0]["fields"]
    )
    values = {definition["key"]: "Opção 0" for definition in data["items"][0]["definitions"]}
    values["answer_20"] = "Escolha não cadastrada"
    result = parse_preparation(proposal_response(values), data, "proposal", partial=True)[0]
    assert len(result["suggested_fields"]) == 20
    assert result["errors"][0]["reason_code"] == "VALUE_CHOICE_INVALID"
