import json
from pathlib import Path

import pytest

from viracase.application.template_preparation import parse_preparation
from viracase.application.template_preparation_selection import build_selection
from viracase.domain.template_fields import normalized_fields
from viracase.infrastructure.learning import LearningLibrary

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/selection_independent_responses.json").read_text()
)


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_independently_generated_response_materializes_available_request_facts(case):
    guide = LearningLibrary(Path(__file__).parents[1] / "content").guides["autorizacao"]
    fields = normalized_fields(
        {"content": guide["application_template"], "fields": guide["fields"]}, "autorizacao"
    )
    payload = {
        "template_preparation_contract": 3,
        "items": [
            {
                "attachment_id": "autorizacao",
                "source_version_id": "synthetic-v1",
                "attachment_revision": 1,
                "title": guide["title"],
                "definitions": fields,
                "original_fields": {},
            }
        ],
        "context": [{"id": "request:message", "type": "request", "content": case["request"]}],
    }
    profile = build_selection(payload)
    payload.update(selection_catalog=profile["catalog"], selection_schema=profile["schema"])
    item = parse_preparation(
        json.dumps(case["response"], ensure_ascii=False), payload, "independent-model", partial=True
    )[0]
    assert item["errors"] == []
    assert {key for question in item["questions"] for key in question["field_ids"]} == set(
        case["expected_pending"]
    )
    suggestions = {value["field_id"]: value for value in item["suggested_fields"]}
    assert set(suggestions) == {f"field_{number}" for number in range(1, 11)} - set(
        case["expected_pending"]
    )
    assert len({value["value"] for value in suggestions.values()}) >= 8
    for key, suggestion in suggestions.items():
        if int(key.split("_")[1]) <= 5:
            assert suggestion["source_type"] == "request"
            assert suggestion["source_refs"] == ["request:message"]
            assert all(part in case["request"] for part in suggestion["value"].split("; "))
        else:
            assert suggestion["source_type"] == "policy_suggestion"
            assert suggestion["source_refs"] == []
    if case["id"] == "padaria":
        assert "cliente" in suggestions["field_1"]["value"]
        assert "efetivar a compra" in suggestions["field_2"]["value"]
        assert "carrinho" in suggestions["field_3"]["value"]
        assert "cadastro e estar logado" in suggestions["field_5"]["value"]
    elif case["id"] == "agenda":
        assert "Pacientes" == suggestions["field_1"]["value"]
        assert "agendar consultas" in suggestions["field_2"]["value"]
        assert "somente para si" in suggestions["field_5"]["value"]
        assert "autenticado" in suggestions["field_5"]["value"]
        assert "tela" not in suggestions["field_2"]["value"]
    else:
        assert "Colaboradores" == suggestions["field_1"]["value"]
        assert "baixar documentos" in suggestions["field_2"]["value"]
        assert "própria equipe" in suggestions["field_2"]["value"]
        assert "ID de equipe da sessão autenticada" in suggestions["field_4"]["value"]
        assert "mesma equipe" in suggestions["field_5"]["value"]
