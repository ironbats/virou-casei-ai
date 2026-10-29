from viracase.application.template_preparation_selection import (
    native_schema,
    object_schema,
    selection_fields,
)
from viracase.domain.rules import RuleError
from viracase.domain.template_fields import empty


def proposal_value_schema(field, include_enum=True):
    schema = native_schema(field)
    limits = field.get("constraints", {})
    text_limits = {
        "minLength": max(1, limits.get("min_length", 0)),
        "maxLength": min(field["max_length"], limits.get("max_length", field["max_length"])),
    }
    if schema["type"] == "string":
        schema.update(text_limits)
        if field.get("type") == "enum" and include_enum:
            schema["enum"] = limits["enum"]
    elif schema["type"] == "array":
        schema["minItems"] = max(1, schema.get("minItems", 0))
        schema["maxItems"] = limits.get("max_items", 30)
        schema["items"] = {"type": "string", **text_limits}
    return schema


def build_proposals(payload):
    include_enum = (
        sum(
            len(field.get("constraints", {}).get("enum", []))
            for item in payload["items"]
            for field in selection_fields(item)
            if field.get("type") == "enum"
        )
        <= 1000
    )
    return object_schema(
        {
            "items": object_schema(
                {
                    item["attachment_id"]: object_schema(
                        {
                            field["key"]: object_schema(
                                {"proposal": proposal_value_schema(field, include_enum)}
                            )
                            for field in selection_fields(item)
                        }
                    )
                    for item in payload["items"]
                }
            )
        }
    )


def proposal_call_schema(payload):
    snapshot = payload.get("proposal_schema") or build_proposals(payload)
    templates = snapshot["properties"]["items"]["properties"]
    return object_schema(
        {
            "items": object_schema(
                {
                    item["attachment_id"]: object_schema(
                        {
                            field["key"]: templates[item["attachment_id"]]["properties"][field["key"]]
                            for field in selection_fields(item)
                        }
                    )
                    for item in payload["items"]
                }
            )
        }
    )


def decode_proposals(raw, payload):
    if not isinstance(raw, dict) or set(raw) != {"items"} or not isinstance(raw["items"], dict):
        raise RuleError(
            "PREPARATION_INVALID_RESPONSE", "A resposta de sugestões não corresponde ao contrato recebido."
        )
    snapshot = payload.get("proposal_schema") or build_proposals(payload)
    known = snapshot["properties"]["items"]["properties"]
    expected = {item["attachment_id"]: item for item in payload["items"]}
    if set(raw["items"]) - known.keys() or expected.keys() - raw["items"].keys():
        raise RuleError(
            "PREPARATION_INVALID_RESPONSE", "A resposta não corresponde aos templates selecionados."
        )
    converted, failures = [], {}
    for identity, source in expected.items():
        fields = raw["items"][identity]
        definitions = {field["key"]: field for field in selection_fields(source)}
        if not isinstance(fields, dict) or set(fields) - known[identity]["properties"].keys():
            raise RuleError(
                "PREPARATION_UNKNOWN_FIELD",
                "A resposta deve incluir os campos solicitados, sem campos adicionais.",
            )
        values = []
        for key, definition in definitions.items():
            choice = fields.get(key)
            value = None
            if not isinstance(choice, dict) or set(choice) != {"proposal"}:
                error = RuleError(
                    "PREPARATION_INVALID_RESPONSE", "O campo deve conter uma sugestão para revisão."
                )
                error.diagnostic = {
                    "reason_code": "PROPOSAL_FIELD_MISSING"
                    if key not in fields
                    else "PROPOSAL_FORMAT_INVALID",
                    "expected_type": definition.get("type", "text"),
                    "received_type": "missing"
                    if key not in fields
                    else "object"
                    if isinstance(choice, dict)
                    else "unknown",
                    "value_encoding": "native",
                    "max_length": definition["max_length"],
                }
                failures[(identity, key)] = error
            else:
                value = choice["proposal"]
            values.append({"field_id": key, "value": value, "citations": []})
        converted.append({"attachment_id": identity, "fields": values, "questions": []})
    return {"items": converted}, failures


def explicit_empty_target(source, key, value, metadata):
    target = source.get("explicit_empty_target")
    return bool(
        target
        and target["field_id"] == key
        and empty(value)
        and empty(target["value"])
        and metadata.get("field_revision", 1) == target["field_revision"]
        and value == target["value"]
    )
