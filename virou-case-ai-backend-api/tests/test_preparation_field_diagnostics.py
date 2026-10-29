import io
import json
import logging

import pytest

from viracase.application.template_preparation import parse_preparation
from viracase.domain.rules import RuleError
from viracase.infrastructure.workflow_logging import emit, safe_fields


@pytest.fixture
def field_diagnostic_records(monkeypatch):
    stream = io.StringIO()
    logger = logging.getLogger("viracase.workflow")
    monkeypatch.setattr(logger, "handlers", [logging.StreamHandler(stream)])
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "propagate", False)
    return lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line]


def diagnostic_payload(**definition_changes):
    definition = {
        "key": "actor",
        "label": "Ator",
        "semantic_id": "actor_identity",
        "required": True,
        "type": "text",
        "max_length": 120,
        "constraints": {"max_length": 120},
        "allowed_source_types": ["request", "project_context", "user_answer"],
        **definition_changes,
    }
    return {
        "items": [
            {
                "attachment_id": "authorization-test",
                "source_version_id": "version-1",
                "attachment_revision": 1,
                "title": "Autorização",
                "definitions": [definition],
                "original_fields": {},
            }
        ],
        "context": [
            {
                "id": "request:message",
                "type": "request",
                "content": "O cliente precisa fazer cadastro e estar logado para comprar produtos.",
            },
            {
                "id": "project:objective",
                "type": "project_context",
                "content": "Catálogo de produtos da padaria.",
            },
        ],
    }


def diagnostic_field(**changes):
    return {
        "field_id": "actor",
        "value_json": json.dumps("cliente"),
        "source_type": "request",
        "source_refs": ["request:message"],
        "evidence": "O cliente precisa fazer cadastro e estar logado para comprar produtos.",
        **changes,
    }


def diagnostic_response(field):
    return json.dumps(
        {
            "items": [
                {
                    "attachment_id": "authorization-test",
                    "fields": [field],
                    "questions": [],
                }
            ]
        }
    )


@pytest.mark.parametrize(
    "changes,definition,code,reason",
    [
        ({"value_json": "cliente"}, {}, "PREPARATION_FIELD_TYPE", "VALUE_JSON_INVALID"),
        ({"value_json": '{"role":"cliente"}'}, {}, "PREPARATION_FIELD_TYPE", "VALUE_TYPE_MISMATCH"),
        ({"value_json": "true"}, {}, "PREPARATION_FIELD_TYPE", "VALUE_TYPE_MISMATCH"),
        ({"value_json": json.dumps("c" * 121)}, {}, "PREPARATION_FIELD_TYPE", "VALUE_LIMIT_EXCEEDED"),
        (
            {},
            {"type": "enum", "constraints": {"enum": ["gerente"]}},
            "PREPARATION_FIELD_TYPE",
            "VALUE_CHOICE_INVALID",
        ),
        ({"value_json": json.dumps("A definir")}, {}, "PREPARATION_FIELD_PLACEHOLDER", "VALUE_PLACEHOLDER"),
        ({"source_type": "policy_suggestion"}, {}, "PREPARATION_INVALID_SOURCE", "SOURCE_TYPE_NOT_ALLOWED"),
        ({"source_refs": "request:message"}, {}, "PREPARATION_INVALID_SOURCE", "SOURCE_REFS_INVALID"),
        ({"source_refs": ["not-received"]}, {}, "PREPARATION_INVALID_SOURCE", "SOURCE_REF_UNKNOWN"),
        ({"source_refs": []}, {}, "PREPARATION_INVALID_SOURCE", "SOURCE_REFS_REQUIRED"),
        ({"source_refs": ["project:objective"]}, {}, "PREPARATION_INVALID_SOURCE", "SOURCE_TYPE_MISMATCH"),
        ({"evidence": []}, {}, "PREPARATION_INVALID_SOURCE", "EVIDENCE_INVALID_TYPE"),
        ({"evidence": "x" * 4001}, {}, "PREPARATION_INVALID_SOURCE", "EVIDENCE_LIMIT_EXCEEDED"),
        ({"evidence": " "}, {}, "PREPARATION_INVALID_SOURCE", "EVIDENCE_REQUIRED"),
        ({"evidence": "O cliente é administrador."}, {}, "PREPARATION_INVALID_SOURCE", "EVIDENCE_NOT_FOUND"),
        (
            {"source_type": "policy_suggestion"},
            {"semantic_id": "default_denial", "allowed_source_types": ["policy_suggestion"]},
            "PREPARATION_INVALID_SOURCE",
            "POLICY_SOURCE_REFS_FORBIDDEN",
        ),
        (
            {"value_json": json.dumps("Consultar /private/administration")},
            {},
            "PREPARATION_UNGROUNDED_VALUE",
            "VALUE_UNGROUNDED_IDENTIFIER",
        ),
        (
            {"value_json": json.dumps("administrador")},
            {},
            "PREPARATION_UNGROUNDED_VALUE",
            "VALUE_UNGROUNDED_EXTRACT",
        ),
    ],
)
def test_field_diagnostics_distinguish_rejection_without_returning_content(changes, definition, code, reason):
    payload = diagnostic_payload(**definition)
    result = parse_preparation(
        diagnostic_response(diagnostic_field(**changes)), payload, "diagnostic-operation", partial=True
    )[0]
    assert result["suggested_fields"] == []
    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["code"] == code
    assert error["reason_code"] == reason
    assert error["attachment_id"] == "authorization-test"
    assert error["field_id"] == "actor"
    assert "value_json" not in error and "evidence" not in error and "source_refs" not in error


def test_invalid_response_envelope_is_distinct_from_invalid_serialized_field_value():
    payload = diagnostic_payload()
    with pytest.raises(RuleError) as raised:
        parse_preparation("not a JSON object", payload, "diagnostic-operation", partial=True)
    assert raised.value.code == "PREPARATION_INVALID_RESPONSE"
    result = parse_preparation(
        diagnostic_response(diagnostic_field(value_json="not a JSON value")),
        payload,
        "diagnostic-operation",
        partial=True,
    )[0]
    assert result["errors"][0]["code"] == "PREPARATION_FIELD_TYPE"
    assert result["errors"][0]["reason_code"] == "VALUE_JSON_INVALID"


def test_structural_diagnostics_are_logged_without_values_or_exception_text(field_diagnostic_records):
    private = "synthetic-project-secret-8b31"

    class PrivateError(RuleError):
        def __str__(self):
            raise AssertionError("The logger must not serialize the exception message")

    error = PrivateError("PREPARATION_FIELD_TYPE", private)
    error.diagnostic = {
        "reason_code": "VALUE_TYPE_MISMATCH",
        "expected_type": "text",
        "received_type": "object",
        "value_encoding": "json_string",
        "refs_count": 1,
        "evidence_length": 23,
        "max_length": 120,
        "value": private,
        "evidence": private,
        "source_refs": [private],
    }
    emit(
        "preparation_field_rejected",
        "warning",
        error=error,
        phase="parse",
        operation_id="diagnostic-operation",
        field_id="actor",
        preparation_state="needs_input",
        prompt=private,
        token=private,
    )
    record = field_diagnostic_records()[0]
    for key in (
        "reason_code",
        "expected_type",
        "received_type",
        "value_encoding",
        "refs_count",
        "evidence_length",
        "max_length",
    ):
        assert record[key] == error.diagnostic[key]
    assert record["preparation_state"] == "needs_input"
    assert record["error_code"] == "PREPARATION_FIELD_TYPE"
    assert private not in json.dumps(record)
    assert "value" not in record and "evidence" not in record and "source_refs" not in record


def test_shape_metadata_rejects_arbitrary_strings_and_unbounded_counters():
    assert (
        safe_fields(
            {
                "expected_type": "private-secret-value",
                "received_type": "private-secret-value",
                "value_encoding": "private-secret-value",
                "preparation_state": "private-secret-value",
                "refs_count": True,
                "evidence_length": -1,
                "max_length": float("inf"),
                "response": "private-secret-value",
            }
        )
        == {}
    )
    assert safe_fields(
        {
            "expected_type": "string_list",
            "received_type": "array",
            "value_encoding": "native",
            "preparation_state": "needs_input",
            "refs_count": 2,
            "evidence_length": 0,
            "max_length": 120,
        }
    ) == {
        "expected_type": "string_list",
        "received_type": "array",
        "value_encoding": "native",
        "preparation_state": "needs_input",
        "refs_count": 2,
        "evidence_length": 0,
        "max_length": 120,
    }


@pytest.mark.parametrize("private_location", ["value_json", "source_refs", "evidence"])
def test_rejected_field_diagnostics_do_not_expose_private_response_content(private_location):
    private = "synthetic-private-content-1e924"
    change = {private_location: [private] if private_location == "source_refs" else private}
    result = parse_preparation(
        diagnostic_response(diagnostic_field(**change)),
        diagnostic_payload(),
        "diagnostic-operation",
        partial=True,
    )[0]
    assert result["errors"]
    assert private not in json.dumps(result["errors"])


@pytest.mark.parametrize(
    "source,value,semantic",
    [
        ("Não há administrador configurado neste projeto.", "administrador", "actor_identity"),
        (
            "O gerente pode editar tarefas somente da própria equipe.",
            "pode editar tarefas",
            "requested_action",
        ),
    ],
)
def test_native_quote_cannot_drop_negation_or_permission_scope_from_its_source(source, value, semantic):
    payload = diagnostic_payload(semantic_id=semantic)
    payload["template_preparation_contract"] = 2
    payload["context"][0]["content"] = source
    result = parse_preparation(
        diagnostic_response(
            {
                "field_id": "actor",
                "value": value,
                "citations": [{"source_ref": "request:message", "quote": value}],
            }
        ),
        payload,
        "diagnostic-operation",
        partial=True,
    )[0]
    assert result["suggested_fields"] == []
    assert result["errors"][0]["code"] == "PREPARATION_UNGROUNDED_VALUE"
    assert result["errors"][0]["reason_code"] == "VALUE_UNGROUNDED_EXTRACT"


def test_native_quote_preserves_a_complete_permission_condition():
    source = "O gerente pode editar tarefas somente da própria equipe."
    payload = diagnostic_payload(semantic_id="requested_action")
    payload["template_preparation_contract"] = 2
    payload["context"][0]["content"] = source
    result = parse_preparation(
        diagnostic_response(
            {
                "field_id": "actor",
                "value": source,
                "citations": [{"source_ref": "request:message", "quote": source}],
            }
        ),
        payload,
        "diagnostic-operation",
        partial=True,
    )[0]
    assert result["errors"] == []
    assert result["suggested_fields"][0]["value"] == source
    assert result["suggested_fields"][0]["source_refs"] == ["request:message"]
