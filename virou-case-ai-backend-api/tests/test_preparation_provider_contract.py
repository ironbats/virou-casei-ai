import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from viracase.application.template_preparation import parse_preparation, preparation_prompt
from viracase.infrastructure.providers import HttpModelGateway, ProviderRejected


def payload(version=2):
    definitions = [
        {
            "key": "interaction",
            "label": "Interação desejada",
            "type": "text",
            "required": True,
            "max_length": 1000,
            "semantic_id": "interaction",
            "allowed_source_types": ["request", "project_context", "user_answer"],
        },
        {
            "key": "enabled",
            "label": "Habilitar opção",
            "type": "boolean",
            "required": True,
            "max_length": 20,
            "semantic_id": "enabled",
            "allowed_source_types": ["policy_suggestion"],
        },
        {
            "key": "criteria",
            "label": "Critérios propostos",
            "type": "string_list",
            "required": True,
            "max_length": 1000,
            "semantic_id": "planned_evidence_criteria",
            "allowed_source_types": ["policy_suggestion"],
        },
        {
            "key": "entry",
            "label": "Entrada",
            "type": "enum",
            "required": True,
            "max_length": 100,
            "constraints": {"enum": ["Página inicial", "Menu"]},
            "semantic_id": "entry",
            "allowed_source_types": ["request"],
        },
        {
            "key": "optional",
            "label": "Observação opcional",
            "type": "text",
            "required": False,
            "max_length": 100,
            "semantic_id": "optional",
            "allowed_source_types": ["policy_suggestion"],
        },
    ]
    return {
        "template_preparation_contract": version,
        "items": [
            {
                "attachment_id": "selected-template",
                "source_version_id": "selected-version",
                "attachment_revision": 1,
                "title": "Interação",
                "definitions": definitions,
                "original_fields": {},
            }
        ],
        "context": [
            {
                "id": "request:message",
                "type": "request",
                "content": "Adicionar botão na Página inicial para abrir um modal.",
            },
            {"id": "file:page", "type": "project_context", "content": "<h1>Padaria</h1>"},
            {"id": "answer:q1", "type": "user_answer", "content": "Abrir modal."},
            {
                "id": "old:suggestion",
                "type": "policy_suggestion",
                "content": "Proposta anterior não verificada.",
            },
        ],
    }


def native_result():
    return {
        "items": [
            {
                "attachment_id": "selected-template",
                "fields": [
                    {
                        "field_id": "interaction",
                        "value": "Adicionar botão na Página inicial para abrir um modal.",
                        "citations": [
                            {
                                "source_ref": "request:message",
                                "quote": "Adicionar botão na Página inicial para abrir um modal.",
                            }
                        ],
                    },
                    {"field_id": "enabled", "value": False, "citations": []},
                    {
                        "field_id": "criteria",
                        "value": ["Verificar abertura do modal após a construção."],
                        "citations": [],
                    },
                    {
                        "field_id": "entry",
                        "value": "Página inicial",
                        "citations": [{"source_ref": "request:message", "quote": "Página inicial"}],
                    },
                    {"field_id": "optional", "value": None, "citations": []},
                ],
                "questions": [],
            }
        ]
    }


def policy(provider="gemini", model="gemini-2.5-flash", version=2, managed=True):
    value = {
        "provider": provider,
        "model": model,
        "max_output_tokens": 16000,
        "input_usd_per_million": "0.1",
        "cached_usd_per_million": "0.01",
        "output_usd_per_million": "0.5",
        "fx_brl_per_usd": "5",
    }
    if version is not None:
        value["template_preparation_contract"] = version
        value["preparation_source_refs"] = ["request:message", "file:page", "answer:q1"]
    if managed:
        value.update(connection_id="managed-connection", model_id="managed-model", version="managed-v1")
    return value


class ManagedRegistry:
    def request_settings(self, value, probe=False):
        assert value["connection_id"] == "managed-connection"
        assert not probe
        if value["provider"] == "openai":
            return "https://api.openai.com/v1/responses", {"Authorization": "Bearer synthetic"}
        return (
            "https://generativelanguage.googleapis.com/v1beta/models/" + value["model"] + ":generateContent",
            {"x-goog-api-key": "synthetic"},
        )


def response(provider, value):
    text = json.dumps(value, ensure_ascii=False)
    if provider == "openai":
        return httpx.Response(
            200,
            json={
                "id": "synthetic-output",
                "status": "completed",
                "usage": {
                    "input_tokens": 70,
                    "input_tokens_details": {"cached_tokens": 10},
                    "output_tokens": 30,
                    "output_tokens_details": {"reasoning_tokens": 5},
                },
                "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            },
        )
    return httpx.Response(
        200,
        json={
            "responseId": "synthetic-output",
            "usageMetadata": {
                "promptTokenCount": 70,
                "cachedContentTokenCount": 10,
                "candidatesTokenCount": 25,
                "thoughtsTokenCount": 5,
            },
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"thought": True, "text": "private"}, {"text": text}]},
                }
            ],
        },
    )


def gateway(handler):
    return HttpModelGateway(
        SimpleNamespace(provider_timeout=5, openai_api_key="synthetic", gemini_api_key="synthetic"),
        registry=ManagedRegistry(),
        transport=httpx.MockTransport(handler),
    )


def normalize_schema(value):
    if isinstance(value, dict):
        return {
            key: item.lower() if key == "type" else normalize_schema(item)
            for key, item in value.items()
            if key not in {"additionalProperties", "propertyOrdering"}
        }
    if isinstance(value, list):
        return [normalize_schema(item) for item in value]
    return value


@pytest.mark.parametrize("provider,model", [("gemini", "gemini-2.5-flash"), ("openai", "gpt-5.4-mini")])
def test_native_contract_is_enforced_on_wire_and_matches_prompt(provider, model):
    data = payload()
    prompt = preparation_prompt(data)
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return response(provider, native_result())

    result = gateway(transport).generate(policy(provider, model), prompt)
    assert len(captured) == 1
    wire = captured[0]
    if provider == "openai":
        output_format = wire["text"]["format"]
        assert output_format["type"] == "json_schema" and output_format["strict"] is True
        schema = output_format["schema"]
        assert schema == json.loads(prompt)["schema"]
        assert wire["max_output_tokens"] == 16000 and wire["store"] is False
        assert wire["service_tier"] == "default"
    else:
        config = wire["generationConfig"]
        schema = config["responseSchema"]
        assert normalize_schema(schema) == normalize_schema(json.loads(prompt)["schema"])
        assert config["thinkingConfig"] == {"thinkingBudget": 0}
        assert config["maxOutputTokens"] == 16000 and config["candidateCount"] == 1
    assert "tools" not in wire and "value_json" not in json.dumps(schema)
    field_schema = schema["properties"]["items"]["items"]["properties"]["fields"]["items"]
    assert set(field_schema["properties"]) == {"field_id", "value", "citations"}
    assert {entry["type"].lower() for entry in field_schema["properties"]["value"]["anyOf"]} == {
        "string",
        "boolean",
        "array",
        "null",
    }
    citation = field_schema["properties"]["citations"]["items"]
    assert set(citation["properties"]["source_ref"]["enum"]) == {"request:message", "file:page", "answer:q1"}
    parsed = parse_preparation(result.text, data, "operation", partial=True)[0]
    assert parsed["errors"] == [] and parsed["questions"] == []
    values = {item["field_id"]: item["value"] for item in parsed["suggested_fields"]}
    assert values["enabled"] is False
    assert values["entry"] == "Página inicial"
    assert values["criteria"] == ["Verificar abertura do modal após a construção."]
    assert "optional" not in values
    assert result.usage == {"input": 70, "cached": 10, "output": 30}
    assert "private" not in result.text


@pytest.mark.parametrize("version", [1, 2])
def test_managed_gemini_25_flash_preparation_disables_thinking_only_with_supported_budget(version):
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return response("gemini", {"items": []})

    gateway(transport).generate(policy(version=version), "Preparar campos sem gerar código.")
    assert captured[0]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert len(captured) == 1


@pytest.mark.parametrize(
    "model",
    ["gemini-2.5-pro", "gemini-2.5-flash-lite", "gemini-2.5-flash-image", "gemini-2.5-flash-preview-tts"],
)
def test_flash_budget_does_not_match_other_managed_model_families(model):
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return response("gemini", {"items": []})

    gateway(transport).generate(policy(model=model), "Preparar campos.")
    assert "thinkingConfig" not in captured[0]["generationConfig"]


def test_gemini_25_code_generation_keeps_existing_configuration():
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return response("gemini", {})

    value = policy(version=None)
    value["generation_contract_version"] = 4
    gateway(transport).generate(value, "Construir aplicação.")
    assert "thinkingConfig" not in captured[0]["generationConfig"]
    schema = captured[0]["generationConfig"]["responseSchema"]
    assert "citations" not in json.dumps(schema)


def test_non_managed_model_allowlist_is_not_expanded():
    calls = []

    def transport(request):
        calls.append(request)
        return response("gemini", {})

    with pytest.raises(ProviderRejected):
        gateway(transport).generate(policy(managed=False), "Preparar campos.")
    assert calls == []


@pytest.mark.parametrize("provider,model", [("gemini", "gemini-3.8-flash"), ("openai", "gpt-6-luna")])
def test_legacy_preparation_wire_remains_compatible(provider, model):
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return response(provider, {"items": []})

    gateway(transport).generate(policy(provider, model, version=1, managed=False), "Preparar campos.")
    wire = captured[0]
    schema = (
        wire["text"]["format"]["schema"]
        if provider == "openai"
        else wire["generationConfig"]["responseSchema"]
    )
    fields = schema["properties"]["items"]["items"]["properties"]["fields"]["items"]["properties"]
    assert set(fields) == {"field_id", "value_json", "source_type", "source_refs", "evidence"}
    if provider == "gemini":
        assert wire["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "LOW"}
    else:
        assert wire["reasoning"] == {"effort": "low"}


def test_legacy_plain_string_inner_json_reproduces_field_type_rejection():
    data = payload(1)
    value = {
        "field_id": "interaction",
        "value_json": "Adicionar botão na Página inicial para abrir um modal.",
        "source_type": "request",
        "source_refs": ["request:message"],
        "evidence": "Adicionar botão na Página inicial para abrir um modal.",
    }
    raw = {"items": [{"attachment_id": "selected-template", "fields": [value], "questions": []}]}
    rejected = parse_preparation(json.dumps(raw), data, "operation", partial=True)[0]
    assert rejected["errors"][0]["code"] == "PREPARATION_FIELD_TYPE"
    valid = copy.deepcopy(raw)
    valid["items"][0]["fields"][0]["value_json"] = json.dumps(value["value_json"])
    accepted = parse_preparation(json.dumps(valid), data, "operation", partial=True)[0]
    assert accepted["errors"] == [] and accepted["suggested_fields"][0]["value"] == value["value_json"]


def test_legacy_source_category_mismatch_reproduces_invalid_source_rejection():
    data = payload(1)
    raw = {
        "items": [
            {
                "attachment_id": "selected-template",
                "fields": [
                    {
                        "field_id": "interaction",
                        "value_json": json.dumps("Adicionar botão na Página inicial para abrir um modal."),
                        "source_type": "project_context",
                        "source_refs": ["request:message"],
                        "evidence": "Adicionar botão na Página inicial para abrir um modal.",
                    }
                ],
                "questions": [],
            }
        ]
    }
    result = parse_preparation(json.dumps(raw), data, "operation", partial=True)[0]
    assert result["errors"][0]["code"] == "PREPARATION_INVALID_SOURCE"


def test_explicit_schema_rejection_preserves_v2_prompt_in_existing_single_retry():
    captured = []
    prompt = preparation_prompt(payload())

    def transport(request):
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            return httpx.Response(400, json={"error": {"message": "responseSchema is not supported"}})
        return response("gemini", native_result())

    result = gateway(transport).generate(policy(), prompt)
    assert len(captured) == 2
    assert "responseSchema" in captured[0]["generationConfig"]
    assert "responseSchema" not in captured[1]["generationConfig"]
    assert captured[0]["contents"] == captured[1]["contents"]
    assert parse_preparation(result.text, payload(), "operation", partial=True)[0]["errors"] == []
