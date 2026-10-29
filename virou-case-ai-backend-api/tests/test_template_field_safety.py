import json
from copy import deepcopy
from pathlib import Path

import pytest

from viracase.application.template_preparation import parse_preparation, preparation_prompt
from viracase.domain.rules import RuleError
from viracase.domain.template_fields import (
    attachment_errors,
    normalized_fields,
    provenance,
    render,
    value_error,
)
from viracase.infrastructure.learning import LearningLibrary


def definitions():
    return normalized_fields(
        {
            "content": "Objetivo: {{goal}}\nAceite: {{acceptance}}",
            "fields": [
                {"key": "goal", "label": "Objetivo", "required": True, "max_length": 1000},
                {"key": "acceptance", "label": "Critérios de aceite", "required": True, "max_length": 1000},
            ],
        }
    )


def request(fields=None):
    return {
        "items": [
            {
                "attachment_id": "selected",
                "source_version_id": "version-one",
                "attachment_revision": 2,
                "title": "Pedido selecionado",
                "definitions": fields or definitions(),
                "original_fields": {},
            }
        ],
        "context": [{"id": "request:idea", "type": "request", "content": "Abrir o modal de login na home"}],
    }


def response(fields):
    return json.dumps({"items": [{"attachment_id": "selected", "fields": fields, "questions": []}]})


def suggestion(key="goal", value="Abrir o modal de login na home", **overrides):
    return {
        "field_id": key,
        "value_json": json.dumps(value),
        "source_type": "request",
        "source_refs": ["request:idea"],
        "evidence": "Abrir o modal de login na home",
        **overrides,
    }


@pytest.mark.parametrize(
    "value", ["A definir", "  A DEFINIR. ", "TBD", "[a preencher]", "...", "{{unknown}}", "Meta: {{goal}}"]
)
def test_unresolved_required_value_is_not_a_completed_answer(value):
    field = definitions()[0]
    assert value_error(field, value, True) == "FIELD_PLACEHOLDER"
    assert value_error(field, value, False) is None


def test_concrete_unicode_enum_and_false_values_remain_valid():
    field = definitions()[0]
    assert value_error(field, "Abrir o modal quando clicar em Entrar", True) is None
    assert value_error(field, "登录对话框", True) is None
    assert value_error({**field, "type": "boolean"}, False, True) is None
    assert value_error({**field, "type": "boolean"}, "false", True) == "FIELD_TYPE"
    assert (
        value_error(
            {**field, "type": "enum", "constraints": {"enum": ["Pendente", "Ativo"]}}, "Pendente", True
        )
        is None
    )
    assert (
        value_error({**field, "type": "string_list"}, ["Critério concreto", "A definir"], True)
        == "FIELD_PLACEHOLDER"
    )


def test_unrecognized_content_marker_blocks_the_attachment():
    attachment = {
        "template_id": "selected",
        "fields": {"goal": "Abrir o modal de login", "acceptance": "Modal fecha ao cancelar"},
        "edited_content": "Abrir o modal de login. Preservar {{missing_contract}}.",
    }
    assert attachment_errors(attachment, definitions()) == [
        {
            "attachment_id": "selected",
            "code": "FIELD_PLACEHOLDER",
            "message": "O conteúdo contém marcadores que não pertencem ao esquema deste anexo.",
            "action": "Editar conteúdo do anexo",
        }
    ]
    assert attachment_errors(attachment, definitions(), False) == []


def test_optional_field_renders_without_a_fictitious_placeholder():
    fields = [definitions()[0], {**definitions()[1], "required": False}]
    assert render("Meta: {{goal}}\nAceite: {{acceptance}}", fields, {}) == "Meta: {{goal}}\nAceite: "


def test_manual_body_cannot_replace_valid_fields_with_a_placeholder():
    attachment = {
        "template_id": "selected",
        "fields": {"goal": "Abrir o modal de login", "acceptance": "Modal fecha ao cancelar"},
        "edited_content": "A definir",
    }
    errors = attachment_errors(attachment, definitions())
    assert len(errors) == 1 and errors[0]["code"] == "FIELD_PLACEHOLDER"
    assert "field_id" not in errors[0]
    assert attachment_errors(attachment, definitions(), False) == []


def test_legacy_labels_follow_stable_markers_instead_of_current_list_position():
    fields = normalized_fields(
        {
            "content": "Entrada: {{field_1}}\nSaída: {{field_2}}",
            "fields": [
                {"key": "field_2", "label": "Resultado", "required": True, "max_length": 1000},
                {"key": "field_1", "label": "Resultado", "required": True, "max_length": 1000},
            ],
        },
        "custom",
    )
    assert [(field["key"], field["label"]) for field in fields] == [
        ("field_2", "Saída"),
        ("field_1", "Entrada"),
    ]
    assert [field["semantic_id"] for field in fields] == ["saida", "entrada"]


@pytest.mark.parametrize(
    "slug,key",
    [
        ("decomposicao", "field_9"),
        ("dependencias", "field_10"),
        ("prompt-injection", "field_10"),
        ("testes", "field_9"),
        ("validacao", "field_11"),
    ],
)
def test_seeded_preconstruction_schema_asks_for_planned_evidence(slug, key):
    learning = LearningLibrary(Path(__file__).parents[1] / "content")
    guide = learning.guides[slug]
    content = {
        "content": guide["application_template"],
        "fields": guide["fields"],
        "source_label": guide["template_version"],
    }
    fields = normalized_fields(content, slug)
    evidence = next(field for field in fields if field["key"] == key)
    assert evidence["semantic_id"] == "planned_evidence_" + key
    assert "ainda não executada" in evidence["label"]
    assert evidence["allowed_source_types"] == ["policy_suggestion"]
    assert "Não declare resultado obtido" in evidence["description"]
    later = normalized_fields({**content, "source_label": "2.0.0+apply.1"}, slug)
    assert not next(field for field in later if field["key"] == key)["semantic_id"].startswith(
        "planned_evidence_"
    )


def test_partial_fill_retains_valid_fields_and_asks_only_for_placeholder():
    items = parse_preparation(
        response([suggestion(), suggestion("acceptance", "A definir")]), request(), "operation"
    )
    assert items[0]["state"] == "needs_input"
    assert [field["field_id"] for field in items[0]["suggested_fields"]] == ["goal"]
    assert [question["field_ids"] for question in items[0]["questions"]] == [["acceptance"]]


@pytest.mark.parametrize(
    "change,code",
    [
        ({"field_id": "unknown"}, "PREPARATION_UNKNOWN_FIELD"),
        ({"value_json": "{}"}, "PREPARATION_FIELD_TYPE"),
        ({"source_type": {"untrusted": "request"}}, "PREPARATION_INVALID_SOURCE"),
        ({"source_refs": ["another-project:private"]}, "PREPARATION_INVALID_SOURCE"),
        ({"evidence": "A test that was never run"}, "PREPARATION_INVALID_SOURCE"),
        ({"source_type": "policy_suggestion"}, "PREPARATION_INVALID_SOURCE"),
    ],
)
def test_provider_output_cannot_promote_invalid_fields_or_unavailable_sources(change, code):
    with pytest.raises(RuleError) as error:
        parse_preparation(response([suggestion(**change)]), request(), "operation")
    assert error.value.code == code


def test_provider_output_cannot_select_another_attachment():
    value = json.loads(response([suggestion()]))
    value["items"][0]["attachment_id"] = "unselected-private-template"
    with pytest.raises(RuleError) as error:
        parse_preparation(json.dumps(value), request(), "operation")
    assert error.value.code == "PREPARATION_INVALID_RESPONSE"


def test_existing_human_value_is_never_replaced_by_suggested_fields():
    payload = request()
    payload["items"][0]["original_fields"]["goal"] = "Decisão humana preservada"
    items = parse_preparation(response([suggestion(), suggestion("acceptance")]), payload, "operation")
    assert [field["field_id"] for field in items[0]["suggested_fields"]] == ["acceptance"]
    assert payload["items"][0]["original_fields"]["goal"] == "Decisão humana preservada"


def test_deeply_invalid_provider_json_is_a_contract_error():
    with pytest.raises(RuleError) as error:
        parse_preparation("[" * 2000 + "0" + "]" * 2000, request(), "operation")
    assert error.value.code == "PREPARATION_INVALID_RESPONSE"


def test_planned_evidence_does_not_accept_fabricated_execution():
    definition = {
        **definitions()[0],
        "semantic_id": "planned_evidence_goal",
        "allowed_source_types": ["policy_suggestion"],
    }
    payload = request([definition])
    fabricated = suggestion(
        value="Todos os testes passaram. Resultado: aprovado.",
        source_type="policy_suggestion",
        source_refs=[],
        evidence="",
    )
    items = parse_preparation(response([fabricated]), payload, "operation")
    assert not items[0]["suggested_fields"]
    assert items[0]["questions"][0]["field_ids"] == ["goal"]
    planned = suggestion(
        value="Após a construção, verificar abertura e fechamento do modal com um teste de interação.",
        source_type="policy_suggestion",
        source_refs=[],
        evidence="",
    )
    items = parse_preparation(response([planned]), payload, "operation")
    assert items[0]["state"] == "ready"
    assert items[0]["suggested_fields"][0]["source_type"] == "policy_suggestion"
    prompt = json.loads(preparation_prompt(payload))
    assert "nenhum teste, build, verificação ou publicação é executado" in prompt["task"]


def test_human_edit_revision_distinguishes_edit_and_revert_from_original_value():
    field = definitions()[0]
    initial = provenance(field, "A")
    repeated = provenance(field, "A", initial)
    changed = provenance(field, "B", repeated)
    reverted = provenance(field, "A", changed)
    assert repeated == initial
    assert [item["field_revision"] for item in (initial, changed, reverted)] == [1, 2, 3]
    assert reverted["value"] == initial["value"]
    assert reverted["edited_by_user"] is True
    assert provenance(field, None, reverted)["edited_by_user"] is True
    assert provenance(field, None)["edited_by_user"] is False
    legacy = deepcopy(initial)
    legacy.pop("field_revision")
    assert provenance(field, "B", legacy)["field_revision"] == 1
