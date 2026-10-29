import json
import re

from viracase.domain.rules import RuleError
from viracase.domain.template_fields import empty, value_error

PLANNED_AUTHORIZATION = {
    "default_denial",
    "cross_account_test",
    "expired_session_result",
    "missing_resource_result",
    "audit_event",
}
FACTUAL_SOURCES = {"request", "project_context", "user_answer"}


def object_schema(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def native_schema(definition):
    kind = definition.get("type", "text")
    limits = definition.get("constraints", {})
    if kind == "boolean":
        return {"type": "boolean"}
    if kind == "enum":
        return {"type": "string"}
    text = {
        "type": "string",
        "maxLength": min(definition["max_length"], limits.get("max_length", definition["max_length"])),
    }
    if limits.get("min_length"):
        text["minLength"] = limits["min_length"]
    if kind == "string_list":
        return {
            "type": "array",
            "items": {"type": "string"},
            "minItems": limits.get("min_items", 0),
            "maxItems": limits.get("max_items", 100),
        }
    return text


def selection_fields(item):
    return [
        field
        for field in item["definitions"]
        if empty(item["original_fields"].get(field["key"]))
        and empty(item.get("context_fields", item["original_fields"]).get(field["key"]))
        and field["key"] not in item.get("protected_field_ids", [])
    ]


def build_selection(payload):
    catalog = []
    for source in payload["context"]:
        if source["type"] not in FACTUAL_SOURCES:
            continue
        content = source["content"]
        lines = (
            [match.group() for match in re.finditer(r"[^\n]+", content)]
            if source["type"] == "project_context"
            else [content]
        )
        for line in lines:
            boundaries = (
                list(re.finditer(r"(?<=[.!?])\s+(?=[A-ZÀ-Ý])", line))
                if source["type"] != "project_context"
                else []
            )
            boundaries = [
                boundary
                for boundary in boundaries
                if not re.match(r"(?:Somente|Apenas|Only|Unless|Desde que)\b", line[boundary.end() :], re.IGNORECASE)
            ]
            starts = [0, *[boundary.end() for boundary in boundaries]]
            ends = [*[boundary.start() for boundary in boundaries], len(line)]
            for start, end in zip(starts, ends, strict=True):
                text = line[start:end]
                tokens = list(re.finditer(r"\S+", text))
                if not tokens:
                    continue
                catalog.append(
                    {
                        "id": len(catalog),
                        "source_ref": source["id"],
                        "source_type": source["type"],
                        "text": text,
                        "tokens": [[index, token.group()] for index, token in enumerate(tokens)],
                        "offsets": [[token.start(), token.end()] for token in tokens],
                        **({"path": source["path"]} if source.get("path") else {}),
                    }
                )
    maximum = max((len(unit["tokens"]) for unit in catalog), default=1) - 1
    span = object_schema(
        {
            "unit": {"type": "integer", "minimum": 0, "maximum": max(0, len(catalog) - 1)},
            "start": {"type": "integer", "minimum": 0, "maximum": maximum},
            "end": {"type": "integer", "minimum": 0, "maximum": maximum},
        }
    )
    templates = {}
    for item in payload["items"]:
        fields = {}
        for definition in selection_fields(item):
            sources = definition["allowed_source_types"]
            planning = definition["semantic_id"] in PLANNED_AUTHORIZATION and "policy_suggestion" in sources
            choices = []
            if not planning and any(unit["source_type"] in sources for unit in catalog):
                choices.append(
                    object_schema(
                        {"extracts": {"type": "array", "items": span, "minItems": 1, "maxItems": 10}}
                    )
                )
            if "policy_suggestion" in sources:
                choices.append(object_schema({"proposal": native_schema(definition)}))
            choices.append(object_schema({"missing": {"type": "string", "minLength": 3, "maxLength": 500}}))
            fields[definition["key"]] = {"anyOf": choices}
        templates[item["attachment_id"]] = object_schema(fields)
    return {"catalog": catalog, "schema": object_schema({"items": object_schema(templates)})}


def selection_snapshot(payload):
    if "selection_catalog" in payload and "selection_schema" in payload:
        return {"catalog": payload["selection_catalog"], "schema": payload["selection_schema"]}
    return build_selection(payload)


def selection_call_schema(payload):
    snapshot = selection_snapshot(payload)
    templates = snapshot["schema"]["properties"]["items"]["properties"]
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


def visible_catalog(catalog):
    return [
        {key: unit[key] for key in ("id", "source_ref", "source_type", "tokens", "path") if key in unit}
        for unit in catalog
    ]


def selection_error(definition, reason, message, code="PREPARATION_INVALID_SOURCE"):
    error = RuleError(code, message)
    error.diagnostic = {
        "reason_code": reason,
        "expected_type": definition.get("type", "text"),
        "received_type": "object",
        "value_encoding": "source_selection",
        "refs_count": 0,
        "evidence_length": 0,
        "max_length": definition["max_length"],
    }
    return error


def clause_bounds(text, start, end):
    boundaries = [
        match for match in re.finditer(r"[,;]|\b(?:mas|porém|porem|entretanto|but|however)\b", text, re.IGNORECASE)
    ]
    lower = max((match.end() for match in boundaries if match.end() <= start), default=0)
    upper = min((match.start() for match in boundaries if match.start() >= end), default=len(text))
    return lower, upper


def guarded_span(unit, start, end, definition):
    lo, hi = unit["offsets"][start][0], unit["offsets"][end][1]
    text = unit["text"]
    lower, upper = clause_bounds(text, lo, hi)
    clause = text[lower:upper]
    prefix, suffix = text[lower:lo], text[hi:upper]
    semantic = definition["semantic_id"]
    redactions = [
        (match.start(), match.end())
        for match in re.finditer(r"\[SEGREDO REMOVIDO\]|\[CONEXÃO PROTEGIDA\]", text)
    ]
    if any(lo < end and hi > start for start, end in redactions):
        raise selection_error(
            definition, "SOURCE_REDACTED", "O trecho selecionado contém informação protegida."
        )
    if semantic in {"actor_identity", "resource_identity"}:
        absent_before = re.search(
            r"\b(?:não\s+(?:há|existe|tem)|nao\s+(?:ha|existe|tem)|nenhuma?|sem|without|not\s+an?|there\s+(?:is|are)\s+no)\b",
            prefix,
            re.IGNORECASE,
        )
        absent_after = re.search(
            r"^\s*(?:[,():-]\s*)?(?:não\s+(?:está|esta|existe|foi|é|e)\b|nao\s+(?:esta|existe|foi|e)\b|inexistente\b|ausente\b|not\s+(?:configured|present|available)\b|does\s+not\s+exist\b|is\s+not\s+(?:configured|present|available)\b)",
            suffix,
            re.IGNORECASE,
        )
        interposed_absence = re.search(
            r"\b(?:não\s+(?:há|existe|tem)|nao\s+(?:ha|existe|tem))(?:\s*,[^,]{1,120},)?\s*(?:(?:um|uma|nenhum|nenhuma)\s+)?$",
            text[:lo],
            re.IGNORECASE,
        )
        if absent_before or absent_after or interposed_absence:
            raise selection_error(
                definition,
                "VALUE_NEGATED_ENTITY",
                "A fonte indica ausência do ator ou recurso; informe o dado aplicável.",
                "PREPARATION_UNGROUNDED_VALUE",
            )
    if semantic == "tenant_source":
        unsafe = re.search(
            r"\b(?:(?:req(?:uest)?|ctx|context)\s*\.\s*)?(?:body|query|params|cookies?|headers)\s*(?:\.|\[)|\b(?:localStorage|sessionStorage|document\s*\.\s*cookie|location\s*\.\s*search)\b|\b(?:id|identidade|dono|tenant|owner)\b.{0,40}\b(?:enviado|informado|fornecido)\s+(?:pelo|por|by)\s+(?:cliente|client|browser|navegador)\b",
            clause,
            re.IGNORECASE,
        )
        if unsafe:
            raise selection_error(
                definition,
                "VALUE_UNTRUSTED_OWNER",
                "Uma entrada controlada pelo cliente não comprova a origem confiável do dono.",
                "PREPARATION_UNGROUNDED_VALUE",
            )
    if semantic in {"requested_action", "authorization_rule"}:
        restriction = re.search(
            r"\b(?:não|nao|nunca|jamais|not|never|cannot|somente|apenas|só|se|quando|caso|exceto|salvo|only|unless|within)\b|\b(?:desde que|da própria|do próprio|da propria|do proprio|mesma equipe|same team)\b",
            text,
            re.IGNORECASE,
        )
        if restriction:
            lo, hi = 0, len(text)
    if any(lo < end and hi > start for start, end in redactions):
        raise selection_error(
            definition, "SOURCE_REDACTED", "O trecho selecionado contém informação protegida."
        )
    return text[lo:hi]


def typed_value(parts, definition):
    kind = definition.get("type", "text")
    if kind == "string_list":
        if len(parts) == 1:
            try:
                parsed = json.loads(parts[0])
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, list):
                return parsed
        return parts
    if kind == "boolean":
        if len(parts) == 1 and parts[0] in {"true", "false"}:
            return parts[0] == "true"
        raise selection_error(
            definition,
            "VALUE_TYPE_MISMATCH",
            "Selecione um valor booleano explícito na fonte.",
            "PREPARATION_FIELD_TYPE",
        )
    if kind == "enum" and len(parts) != 1:
        raise selection_error(
            definition, "VALUE_TYPE_MISMATCH", "Selecione uma única opção do campo.", "PREPARATION_FIELD_TYPE"
        )
    return "; ".join(parts)


def materialize_selection(choice, definition, catalog, context):
    ranges = choice["extracts"]
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= 10:
        raise selection_error(
            definition, "SELECTION_RANGES_INVALID", "Selecione de um a dez trechos recebidos."
        )
    if (
        definition["semantic_id"] in PLANNED_AUTHORIZATION
        and "policy_suggestion" in definition["allowed_source_types"]
    ):
        raise selection_error(
            definition,
            "SELECTION_PROPOSAL_REQUIRED",
            "Este campo descreve uma proposta de comportamento, ainda sujeita à revisão.",
        )
    parts, citations, origins, selected = [], [], set(), set()
    for span in ranges:
        if (
            not isinstance(span, dict)
            or set(span) != {"unit", "start", "end"}
            or any(type(value) is not int for value in span.values())
        ):
            raise selection_error(
                definition, "SELECTION_RANGE_INVALID", "O intervalo de seleção tem formato inválido."
            )
        number, start, end = span["unit"], span["start"], span["end"]
        if not 0 <= number < len(catalog) or catalog[number]["id"] != number:
            raise selection_error(
                definition,
                "SELECTION_UNIT_UNKNOWN",
                "A unidade selecionada não pertence ao contexto recebido.",
            )
        unit = catalog[number]
        entry = context.get(unit["source_ref"])
        if (
            not entry
            or unit["source_type"] not in FACTUAL_SOURCES
            or entry["type"] != unit["source_type"]
            or unit["text"] not in entry["content"]
        ):
            raise selection_error(
                definition, "SOURCE_NOT_FACTUAL", "A unidade não comprova um fato do contexto recebido."
            )
        if unit["source_type"] not in definition["allowed_source_types"]:
            raise selection_error(
                definition, "SOURCE_TYPE_NOT_ALLOWED", "A origem selecionada não é permitida para este campo."
            )
        if not 0 <= start <= end < len(unit["tokens"]):
            raise selection_error(
                definition, "SELECTION_RANGE_INVALID", "Os índices selecionados excedem a unidade recebida."
            )
        if (number, start, end) in selected:
            raise selection_error(
                definition, "SELECTION_RANGE_DUPLICATE", "O mesmo trecho foi selecionado mais de uma vez."
            )
        selected.add((number, start, end))
        value = guarded_span(unit, start, end, definition)
        if value not in parts:
            parts.append(value)
            citations.append({"source_ref": unit["source_ref"], "quote": value})
        origins.add(unit["source_type"])
    if len(origins) != 1:
        raise selection_error(
            definition, "SOURCE_TYPE_MISMATCH", "Use trechos da mesma origem para este campo."
        )
    value = typed_value(parts, definition)
    error = value_error(definition, value, True)
    if error:
        raise selection_error(
            definition,
            {
                "FIELD_LIMIT": "VALUE_LIMIT_EXCEEDED",
                "FIELD_CHOICE": "VALUE_CHOICE_INVALID",
                "FIELD_PLACEHOLDER": "VALUE_PLACEHOLDER",
            }.get(error, "VALUE_TYPE_MISMATCH"),
            "O trecho selecionado não respeita o tipo ou limite do campo.",
            "PREPARATION_FIELD_TYPE",
        )
    return {"value": value, "citations": citations}


def decode_selection(raw, payload):
    if not isinstance(raw, dict) or set(raw) != {"items"} or not isinstance(raw["items"], dict):
        raise RuleError(
            "PREPARATION_INVALID_RESPONSE", "A resposta de seleção não corresponde ao contrato recebido."
        )
    snapshot = selection_snapshot(payload)
    known = snapshot["schema"]["properties"]["items"]["properties"]
    expected = {item["attachment_id"]: item for item in payload["items"]}
    if set(raw["items"]) - known.keys() or expected.keys() - raw["items"].keys():
        raise RuleError(
            "PREPARATION_INVALID_RESPONSE", "A resposta não corresponde aos templates selecionados."
        )
    context = {entry["id"]: entry for entry in payload["context"]}
    converted, failures = [], {}
    for identity, source in expected.items():
        fields = raw["items"][identity]
        definitions = {field["key"]: field for field in selection_fields(source)}
        if (
            not isinstance(fields, dict)
            or set(fields) - known[identity]["properties"].keys()
            or definitions.keys() - fields.keys()
        ):
            raise RuleError(
                "PREPARATION_UNKNOWN_FIELD",
                "A resposta deve incluir cada campo do contrato, sem campos adicionais.",
            )
        values, questions = [], []
        for key, definition in definitions.items():
            choice = fields[key]
            value = {"field_id": key, "value": None, "citations": []}
            if key in source.get("protected_field_ids", []):
                values.append(value)
                continue
            try:
                if not isinstance(choice, dict) or len(choice) != 1:
                    raise selection_error(
                        definition,
                        "SELECTION_MODE_INVALID",
                        "Escolha uma seleção, proposta ou pergunta para o campo.",
                    )
                if set(choice) == {"missing"}:
                    question = choice["missing"]
                    if not isinstance(question, str) or not 3 <= len(question.strip()) <= 500:
                        raise selection_error(
                            definition,
                            "SELECTION_QUESTION_INVALID",
                            "Informe uma pergunta curta sobre o dado ausente.",
                            "PREPARATION_INVALID_QUESTION",
                        )
                    questions.append({"question": question.strip(), "field_ids": [key]})
                elif set(choice) == {"proposal"}:
                    if "policy_suggestion" not in definition["allowed_source_types"]:
                        raise selection_error(
                            definition,
                            "SOURCE_TYPE_NOT_ALLOWED",
                            "Este campo exige uma fonte factual ou uma pergunta.",
                        )
                    value["value"] = choice["proposal"]
                elif set(choice) == {"extracts"}:
                    value.update(materialize_selection(choice, definition, snapshot["catalog"], context))
                else:
                    raise selection_error(
                        definition,
                        "SELECTION_MODE_INVALID",
                        "O modo de preenchimento não pertence ao contrato recebido.",
                    )
            except RuleError as exc:
                failures[(identity, key)] = exc
            values.append(value)
        converted.append({"attachment_id": identity, "fields": values, "questions": questions})
    return {"items": converted}, failures
