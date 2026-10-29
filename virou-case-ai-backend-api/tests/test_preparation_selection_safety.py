import copy
import json

import pytest

from viracase.application.template_preparation import parse_preparation
from viracase.domain.rules import RuleError
from viracase.infrastructure.workflow_logging import safe_fields


def definition(key="subject", semantic="actor_identity", kind="text", sources=None, constraints=None):
    return {
        "key": key,
        "label": key,
        "semantic_id": semantic,
        "required": True,
        "type": kind,
        "max_length": 1000,
        "constraints": constraints or {},
        "allowed_source_types": sources or ["request", "project_context", "user_answer"],
    }


def selection_case(text, field=None, source_type="request", extra_context=None, attachment="documents"):
    from viracase.application.template_preparation_selection import build_selection

    payload = {
        "template_preparation_contract": 3,
        "items": [
            {
                "attachment_id": attachment,
                "source_version_id": attachment + "-version-one",
                "attachment_revision": 1,
                "attachment_instance_id": attachment + "-instance",
                "title": attachment,
                "definitions": [field or definition()],
                "original_fields": {},
            }
        ],
        "context": [
            {"id": "input:primary", "type": source_type, "content": text},
            *(extra_context or []),
        ],
    }
    built = build_selection(payload)
    payload["selection_catalog"] = built["catalog"]
    payload["selection_schema"] = built["schema"]
    return payload


def visible_span(payload, phrase, source_ref="input:primary"):
    for unit in payload["selection_catalog"]:
        if unit["source_ref"] != source_ref:
            continue
        position = unit["text"].find(phrase)
        if position < 0:
            continue
        end = position + len(phrase)
        first = next(index for index, (lo, hi) in enumerate(unit["offsets"]) if lo <= position < hi)
        last = next(index for index, (lo, hi) in enumerate(unit["offsets"]) if lo < end <= hi)
        return {"unit": unit["id"], "start": first, "end": last}
    raise AssertionError("A fonte sintética deve estar visível no catálogo da operação")


def wire_response(payload, choice):
    item = payload["items"][0]
    key = item["definitions"][0]["key"]
    return json.dumps({"items": {item["attachment_id"]: {key: choice}}})


def selected(payload, choice):
    return parse_preparation(wire_response(payload, choice), payload, "selection-safety", partial=True)[0]


def rejected(payload, choice):
    try:
        result = selected(payload, choice)
    except RuleError as error:
        assert error.code.startswith("PREPARATION_")
        return
    assert result["suggested_fields"] == []
    assert result["errors"] or result["questions"]


@pytest.mark.parametrize(
    "attachment,source,actor",
    [
        (
            "appointments",
            "Pacientes cadastrados podem consultar os próprios agendamentos.",
            "Pacientes cadastrados",
        ),
        (
            "inventory",
            "Estoquistas autenticados podem consultar o saldo de produtos.",
            "Estoquistas autenticados",
        ),
        (
            "support",
            "Analistas de suporte podem responder aos chamados atribuídos à sua equipe.",
            "Analistas de suporte",
        ),
    ],
)
def test_selection_materializes_available_actors_in_distinct_domains(attachment, source, actor):
    payload = selection_case(source, attachment=attachment)
    result = selected(payload, {"extracts": [visible_span(payload, actor)]})
    assert result["errors"] == [] and result["questions"] == []
    assert result["suggested_fields"][0]["value"] == actor
    assert result["suggested_fields"][0]["source_type"] == "request"
    assert result["suggested_fields"][0]["source_refs"] == ["input:primary"]


@pytest.mark.parametrize(
    "source,actor",
    [
        ("Não existe administrador neste projeto.", "administrador"),
        ("Administrador não está configurado neste projeto.", "Administrador"),
        ("Não há, neste projeto, administrador.", "administrador"),
    ],
)
def test_actor_selection_cannot_turn_explicit_absence_into_identity(source, actor):
    payload = selection_case(source)
    rejected(payload, {"extracts": [visible_span(payload, actor)]})


def test_unrelated_negative_does_not_remove_an_explicit_actor():
    payload = selection_case("Sem alterar o catálogo, clientes cadastrados podem concluir compras.")
    result = selected(payload, {"extracts": [visible_span(payload, "clientes cadastrados")]})
    assert result["errors"] == [] and result["questions"] == []
    assert result["suggested_fields"][0]["value"] == "clientes cadastrados"


def test_portuguese_preposition_no_does_not_negate_an_available_resource():
    payload = selection_case("Há produtos no carrinho", definition(semantic="resource_identity"))
    result = selected(payload, {"extracts": [visible_span(payload, "carrinho")]})
    assert result["errors"] == [] and result["questions"] == []
    assert result["suggested_fields"][0]["value"] == "carrinho"


def test_layout_condition_does_not_turn_an_available_action_into_missing_information():
    payload = selection_case(
        "Analistas podem editar documentos e, quando a tela for pequena, o botão deve ocupar toda a largura.",
        definition(semantic="requested_action"),
    )
    result = selected(payload, {"extracts": [visible_span(payload, "editar documentos")]})
    assert result["errors"] == [] and result["questions"] == []
    assert "editar documentos" in result["suggested_fields"][0]["value"]


@pytest.mark.parametrize(
    "source,fragment,condition",
    [
        (
            "Analistas podem editar documentos somente da própria equipe.",
            "editar documentos",
            "somente da própria equipe",
        ),
        (
            "Se o analista pertencer à equipe, poderá editar documentos.",
            "editar documentos",
            "Se o analista pertencer à equipe",
        ),
        (
            "Analistas podem consultar documentos apenas enquanto a autorização estiver ativa.",
            "consultar documentos",
            "apenas enquanto a autorização estiver ativa",
        ),
    ],
)
def test_short_action_selection_cannot_erase_the_permission_condition(source, fragment, condition):
    payload = selection_case(source, definition(semantic="requested_action"))
    try:
        result = selected(payload, {"extracts": [visible_span(payload, fragment)]})
    except RuleError as error:
        assert error.code.startswith("PREPARATION_")
        return
    if result["suggested_fields"]:
        assert condition.casefold() in result["suggested_fields"][0]["value"].casefold()
    else:
        assert result["errors"] or result["questions"]


@pytest.mark.parametrize(
    "source",
    [
        "Analistas podem editar documentos somente da própria equipe.",
        "Se o analista pertencer à equipe, poderá editar documentos.",
        "Analistas podem consultar documentos apenas enquanto a autorização estiver ativa.",
    ],
)
def test_complete_permission_requirement_remains_fillable(source):
    payload = selection_case(source, definition(semantic="authorization_rule"))
    result = selected(payload, {"extracts": [visible_span(payload, source)]})
    assert result["errors"] == [] and result["questions"] == []
    assert result["suggested_fields"][0]["value"].strip(" .") == source.strip(" .")


@pytest.mark.parametrize(
    "source,origin",
    [
        ("const ownerId = request.body.userId;", "request.body.userId"),
        ("const tenantId = request.query.tenantId;", "request.query.tenantId"),
        ('const ownerId = request.cookies["userId"];', 'request.cookies["userId"]'),
    ],
)
def test_presence_of_unvalidated_client_input_is_not_a_trusted_owner_source(source, origin):
    payload = selection_case(source, definition(semantic="tenant_source"), source_type="project_context")
    rejected(payload, {"extracts": [visible_span(payload, origin)]})


def test_explicit_owner_requirement_does_not_require_an_existing_database_identifier():
    requirement = "Obter o dono pela identidade autenticada e validada no servidor."
    payload = selection_case(requirement, definition(semantic="tenant_source"))
    result = selected(payload, {"extracts": [visible_span(payload, requirement)]})
    assert result["errors"] == [] and result["questions"] == []
    assert result["suggested_fields"][0]["value"].strip(" .") == requirement.strip(" .")
    assert result["suggested_fields"][0]["source_type"] == "request"


@pytest.mark.parametrize(
    "bad_range",
    [
        {"unit": -1, "start": 0, "end": 0},
        {"unit": 9999, "start": 0, "end": 0},
        {"unit": True, "start": 0, "end": 0},
        {"unit": 0, "start": -1, "end": 0},
        {"unit": 0, "start": 0, "end": 9999},
        {"unit": 0, "start": 1, "end": 0},
        {"unit": 0, "start": 0, "end": 1.0},
    ],
)
def test_selection_rejects_foreign_or_malformed_ranges(bad_range):
    payload = selection_case("Clientes autenticados podem consultar pedidos.")
    rejected(payload, {"extracts": [bad_range]})


def test_repeating_the_same_range_does_not_multiply_the_evidence():
    payload = selection_case("Clientes autenticados podem consultar pedidos.")
    span = visible_span(payload, "Clientes autenticados")
    rejected(payload, {"extracts": [span, span]})


def test_previous_policy_text_is_not_an_eligible_factual_unit():
    payload = selection_case(
        "Clientes podem consultar pedidos.",
        extra_context=[
            {
                "id": "old:assistant-proposal",
                "type": "policy_suggestion",
                "content": "Um administrador global terá acesso a todos os pedidos.",
            }
        ],
    )
    assert all(unit["source_type"] != "policy_suggestion" for unit in payload["selection_catalog"])
    assert all(unit["source_ref"] != "old:assistant-proposal" for unit in payload["selection_catalog"])
    rejected(payload, {"extracts": [{"unit": len(payload["selection_catalog"]), "start": 0, "end": 0}]})


@pytest.mark.parametrize(
    "marker,fragment",
    [
        ("[SEGREDO REMOVIDO]", "REMOVIDO]"),
        ("[CONEXÃO PROTEGIDA]", "PROTEGIDA]"),
    ],
)
def test_a_fragment_overlapping_a_redaction_marker_is_not_a_factual_identity(marker, fragment):
    payload = selection_case("Responsável: " + marker)
    result = selected(payload, {"extracts": [visible_span(payload, fragment)]})
    assert result["suggested_fields"] == []
    assert any(error.get("reason_code") == "SOURCE_REDACTED" for error in result["errors"])


def test_permission_expansion_cannot_introduce_a_redaction_marker_into_the_final_value():
    payload = selection_case(
        "Somente [SEGREDO REMOVIDO] pode aprovar pedidos.",
        definition(semantic="requested_action"),
    )
    result = selected(payload, {"extracts": [visible_span(payload, "aprovar pedidos")]})
    assert result["suggested_fields"] == []
    assert any(error.get("reason_code") == "SOURCE_REDACTED" for error in result["errors"])


def test_unselected_redaction_does_not_block_an_available_unrelated_fact():
    payload = selection_case(
        "Usuários podem consultar pedidos, referência interna: [SEGREDO REMOVIDO].",
        definition(semantic="requested_action"),
    )
    result = selected(payload, {"extracts": [visible_span(payload, "consultar pedidos")]})
    assert result["errors"] == [] and result["questions"] == []
    assert "consultar pedidos" in result["suggested_fields"][0]["value"]
    assert "REMOVIDO" not in result["suggested_fields"][0]["value"]


def test_a_valid_unit_from_a_disallowed_source_does_not_become_a_user_answer():
    payload = selection_case(
        "Clientes autenticados podem consultar pedidos.",
        definition(sources=["user_answer"]),
    )
    rejected(payload, {"extracts": [visible_span(payload, "Clientes autenticados")]})


@pytest.mark.parametrize(
    "source,kind,phrases,expected,constraints",
    [
        ("Habilitar a opção: false", "boolean", ["false"], False, {}),
        (
            "Critérios:\nabrir tela\npreservar filtros",
            "string_list",
            ["abrir tela", "preservar filtros"],
            ["abrir tela", "preservar filtros"],
            {"min_items": 2},
        ),
        ("Formato desejado: compacto", "enum", ["compacto"], "compacto", {"enum": ["compacto", "amplo"]}),
    ],
)
def test_typed_factual_selections_preserve_the_declared_value(source, kind, phrases, expected, constraints):
    payload = selection_case(
        source,
        definition(semantic="requested_setting", kind=kind, constraints=constraints),
    )
    result = selected(payload, {"extracts": [visible_span(payload, phrase) for phrase in phrases]})
    assert result["errors"] == [] and result["questions"] == []
    field = result["suggested_fields"][0]
    assert field["value"] == expected and type(field["value"]) is type(expected)
    assert field["source_type"] == "request" and field["source_refs"] == ["input:primary"]


@pytest.mark.parametrize(
    "kind,value,constraints",
    [
        ("boolean", False, {}),
        (
            "string_list",
            ["Verificar isolamento após implementar", "Verificar sessão expirada"],
            {"min_items": 2},
        ),
        ("enum", "confirmar", {"enum": ["confirmar", "cancelar"]}),
    ],
)
def test_proposals_preserve_native_types_and_their_unverified_origin(kind, value, constraints):
    payload = selection_case(
        "Definir o comportamento da nova funcionalidade.",
        definition(
            semantic="planned_behavior", kind=kind, sources=["policy_suggestion"], constraints=constraints
        ),
    )
    result = selected(payload, {"proposal": value})
    assert result["errors"] == [] and result["questions"] == []
    field = result["suggested_fields"][0]
    assert field["value"] == value and type(field["value"]) is type(value)
    assert field["source_type"] == "policy_suggestion" and field["source_refs"] == []


@pytest.mark.parametrize(
    "kind,value,constraints",
    [
        ("text", {"value": "texto"}, {}),
        ("boolean", "false", {}),
        ("string_list", ["Primeiro item", False], {}),
        ("enum", "inventado", {"enum": ["confirmar", "cancelar"]}),
    ],
)
def test_proposals_cannot_coerce_invalid_values_into_the_declared_field_type(kind, value, constraints):
    payload = selection_case(
        "Definir o comportamento da nova funcionalidade.",
        definition(
            semantic="planned_behavior", kind=kind, sources=["policy_suggestion"], constraints=constraints
        ),
    )
    rejected(payload, {"proposal": value})


def test_planned_session_result_is_materialized_as_a_proposal_without_claiming_execution():
    payload = selection_case(
        "Pacientes precisam estar autenticados para confirmar agendamentos.",
        definition(semantic="expired_session_result", sources=["request", "policy_suggestion"]),
        attachment="appointments",
    )
    proposal = "Solicitar nova autenticação quando a sessão expirar, sem confirmar o agendamento."
    result = selected(payload, {"proposal": proposal})
    assert result["errors"] == [] and result["questions"] == []
    assert result["suggested_fields"][0]["value"] == proposal
    assert result["suggested_fields"][0]["source_type"] == "policy_suggestion"


def test_factual_identity_cannot_be_replaced_with_an_unsubstantiated_proposal():
    payload = selection_case("Clientes autenticados podem consultar pedidos.")
    rejected(payload, {"proposal": "Administrador global com acesso a todas as contas"})


def test_free_value_cannot_override_the_selected_source_text():
    payload = selection_case("Clientes autenticados podem consultar pedidos.")
    rejected(
        payload,
        {
            "extracts": [visible_span(payload, "Clientes autenticados")],
            "value": "Administrador global",
        },
    )


def test_duplicate_field_key_is_rejected_instead_of_choosing_the_last_claim():
    payload = selection_case("Clientes autenticados podem consultar pedidos.")
    extract = json.dumps({"extracts": [visible_span(payload, "Clientes autenticados")]})
    text = '{"items":{"documents":{"subject":' + extract + ',"subject":' + extract + "}}}"
    with pytest.raises(RuleError) as error:
        parse_preparation(text, payload, "selection-safety", partial=True)
    assert error.value.code.startswith("PREPARATION_")


def test_selection_does_not_overwrite_a_human_field_changed_after_the_catalog_snapshot():
    from viracase.application.template_preparation_selection import build_selection

    payload = selection_case("Analistas autenticados podem editar documentos.", definition(key="actor"))
    payload["items"][0]["definitions"].append(definition(key="action", semantic="requested_action"))
    built = build_selection(payload)
    payload.update(selection_catalog=built["catalog"], selection_schema=built["schema"])
    payload["items"][0]["protected_field_ids"] = ["actor"]
    payload["items"][0]["context_fields"] = {"actor": "Escolha humana preservada"}
    before = copy.deepcopy(payload)
    text = json.dumps(
        {
            "items": {
                "documents": {
                    "actor": {"extracts": [visible_span(payload, "Analistas autenticados")]},
                    "action": {"extracts": [visible_span(payload, "editar documentos")]},
                }
            }
        }
    )
    result = parse_preparation(text, payload, "selection-safety", partial=True)[0]
    assert {field["field_id"] for field in result["suggested_fields"]} == {"action"}
    assert all("actor" not in question["field_ids"] for question in result["questions"])
    assert payload == before


def test_selection_encoding_is_observable_without_accepting_arbitrary_metadata_strings():
    assert safe_fields({"value_encoding": "source_selection"}) == {"value_encoding": "source_selection"}
    assert safe_fields({"value_encoding": "synthetic-private-response-content"}) == {}
