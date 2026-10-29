import json
from pathlib import Path

import pytest

from viracase.application.template_preparation import parse_preparation
from viracase.application.template_preparation_proposals import build_proposals
from viracase.domain.template_fields import normalized_fields
from viracase.infrastructure.learning import LearningLibrary

FIXTURE = json.loads((Path(__file__).parent / "fixtures/proposal_independent_responses.json").read_text())


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_independent_proposals_fill_every_field_and_keep_human_review_provenance(case):
    guide = LearningLibrary(Path(__file__).parents[1] / "content").guides["autorizacao"]
    fields = normalized_fields(
        {"content": guide["application_template"], "fields": guide["fields"]}, "autorizacao"
    )
    payload = {
        "template_preparation_contract": 4,
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
    payload["proposal_schema"] = build_proposals(payload)
    result = parse_preparation(
        json.dumps(case["response"], ensure_ascii=False), payload, "independent-proposals", partial=True
    )[0]
    assert result["state"] == "ready" and not result["errors"] and not result["questions"]
    suggestions = {field["field_id"]: field for field in result["suggested_fields"]}
    assert set(suggestions) == {field["key"] for field in fields}
    assert len({field["value"] for field in suggestions.values()}) == 10
    assert all(
        field["state"] == "suggested"
        and field["source_type"] == "policy_suggestion"
        and field["source_refs"] == []
        and case["request"] != field["value"]
        for field in suggestions.values()
    )
    assert all("policy_suggestion" not in field["allowed_source_types"] for field in fields[:5])
