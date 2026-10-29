import re
import unicodedata

from viracase.domain.rules import RuleError, canonical_hash, utcnow

FIELD_TYPES = {"text", "boolean", "enum", "string_list"}
SOURCE_TYPES = {"request", "project_context", "user_answer", "policy_suggestion"}
SENSITIVITIES = {"public", "project", "confidential", "secret"}
AUTHORIZATION_FIELDS = {
    "field_1": ("actor_identity", "Identidade e papel do ator"),
    "field_2": ("requested_action", "Ação permitida sobre o recurso"),
    "field_3": ("resource_identity", "Tipo e identificação do recurso"),
    "field_4": ("tenant_source", "Origem confiável do dono ou tenant"),
    "field_5": ("authorization_rule", "Condição de permissão"),
    "field_6": ("default_denial", "Resposta de negação por padrão"),
    "field_7": ("cross_account_test", "Teste entre contas diferentes"),
    "field_8": ("expired_session_result", "Resultado esperado para sessão expirada"),
    "field_9": ("missing_resource_result", "Resultado esperado para recurso inexistente"),
    "field_10": ("audit_event", "Evento de auditoria sem dados sensíveis"),
}
AUTHORIZATION_PREFIXES = {
    "field_1": "Ator",
    "field_2": "Ação",
    "field_3": "Recurso",
    "field_4": "Dono/tenant",
    "field_5": "Condição de permissão",
    "field_6": "Negação padrão",
    "field_7": "Teste cruzado",
    "field_8": "Sessão expirada",
    "field_9": "Recurso inexistente",
    "field_10": "Auditoria",
}
PLANNED_EVIDENCE_FIELDS = {
    ("decomposicao", "field_9"): ("Validação", "Evidência real", "Validação proposta"),
    ("dependencias", "field_10"): ("Verificação", "Build, teste e scan executados", "Verificação proposta"),
    ("prompt-injection", "field_10"): ("Evidência", "Resultado observado", "Evidência a coletar"),
    ("testes", "field_9"): ("Evidência", "Resultado real", "Evidência a coletar"),
    ("validacao", "field_11"): ("Evidência", "Teste positivo e negativo", "Evidência a coletar"),
}
PLACEHOLDER_VALUES = {
    "a definir",
    "a preencher",
    "a informar",
    "a confirmar",
    "indefinido",
    "indefinida",
    "nao definido",
    "nao definida",
    "nao informado",
    "nao informada",
    "pendente",
    "tbd",
    "todo",
    "nao aplicavel",
    "nao se aplica",
    "nao disponivel",
    "sem informacao",
    "nao sei",
    "n/a",
    "to be defined",
    "to be determined",
    "to do",
    "placeholder",
    "xxx",
    "...",
    "???",
}


def normalized_text(value):
    return " ".join(unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower().split())


def unresolved_value(value):
    text = normalized_text(value).strip(" []().:;!?")
    return (
        bool(re.search(r"\{\{[^{}\r\n]+\}\}", value))
        or text in PLACEHOLDER_VALUES
        or bool(re.fullmatch(r"[.?!…\s]+", value))
    )


def normalized_fields(content, slug=""):
    result = []
    labels = [field.get("label", "") for field in content.get("fields", [])]
    for index, original in enumerate(content.get("fields", [])):
        field = dict(original)
        key = field["key"]
        semantic, label = key, field["label"]
        description = field.get("description")
        marker = "{{" + key + "}}"
        matching_lines = [line for line in content.get("content", "").splitlines() if marker in line]
        prefix = ""
        if len(matching_lines) == 1:
            match = re.fullmatch(r"\s*([^{}\n]+?):\s*" + re.escape(marker) + r"\s*", matching_lines[0])
            if match:
                prefix = match.group(1).strip(" :•-\t")
        legacy_authorization = (
            slug == "autorizacao"
            and key in AUTHORIZATION_FIELDS
            and "semantic_id" not in field
            and AUTHORIZATION_PREFIXES[key] + ": {{" + key + "}}" in content.get("content", "")
        )
        if legacy_authorization:
            semantic, label = AUTHORIZATION_FIELDS[key]
        elif (
            prefix
            and "semantic_id" not in field
            and (re.fullmatch(r"field_\d+", key) or labels.count(label) > 1)
        ):
            label = prefix
            semantic = re.sub(r"[^a-z0-9]+", "_", normalized_text(prefix)).strip("_")
        sources = field.get("allowed_source_types", sorted(SOURCE_TYPES))
        if legacy_authorization and key in {"field_1", "field_2", "field_3", "field_4", "field_5"}:
            sources = [value for value in sources if value != "policy_suggestion"]
        evidence = PLANNED_EVIDENCE_FIELDS.get((slug, key))
        if (
            evidence
            and content.get("source_label") == "1.0.0+apply.1"
            and "semantic_id" not in field
            and prefix == evidence[0]
            and original["label"] == evidence[1]
        ):
            label = evidence[2] + ", ainda não executada"
            semantic = "planned_evidence_" + key
            description = (
                "Descreva o critério e a evidência que deverão ser verificados após a construção. "
                "Esta etapa não executa testes. Não declare resultado obtido, aprovação ou execução."
            )
            sources = ["policy_suggestion"]
        result.append(
            {
                **field,
                "id": key,
                "semantic_id": field.get("semantic_id", semantic),
                "label": label,
                "description": description or label,
                "type": field.get("type", "text"),
                "constraints": {"max_length": field["max_length"], **field.get("constraints", {})},
                "sensitivity": field.get("sensitivity", "project"),
                "allowed_source_types": sources,
                "display_order": index,
            }
        )
    return result


def validate_definition(fields):
    keys = set()
    allowed = {
        "key",
        "label",
        "required",
        "max_length",
        "id",
        "semantic_id",
        "description",
        "type",
        "constraints",
        "sensitivity",
        "allowed_source_types",
        "display_order",
    }
    for field in fields:
        if not {"key", "label", "required", "max_length"} <= field.keys() or set(field) - allowed:
            raise ValueError("Definição de campo inválida")
        key = field["key"]
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,59}", key)
            or key in keys
            or field.get("id", key) != key
        ):
            raise ValueError("Identificador de campo inválido")
        keys.add(key)
        if (
            not isinstance(field["label"], str)
            or not 1 <= len(field["label"]) <= 160
            or not field["label"].strip()
            or type(field["required"]) is not bool
        ):
            raise ValueError("Rótulo ou obrigatoriedade inválidos")
        if type(field["max_length"]) is not int or not 1 <= field["max_length"] <= 12000:
            raise ValueError("Limite inválido")
        if (
            not isinstance(field.get("type", "text"), str)
            or field.get("type", "text") not in FIELD_TYPES
            or not isinstance(field.get("sensitivity", "project"), str)
            or field.get("sensitivity", "project") not in SENSITIVITIES
        ):
            raise ValueError("Tipo ou sensibilidade inválidos")
        for name in ("description", "semantic_id"):
            if name in field and (
                not isinstance(field[name], str) or len(field[name]) > (500 if name == "description" else 80)
            ):
                raise ValueError("Descrição inválida")
        sources = field.get("allowed_source_types", sorted(SOURCE_TYPES))
        if (
            not isinstance(sources, list)
            or not sources
            or len(sources) > len(SOURCE_TYPES)
            or any(not isinstance(source, str) or source not in SOURCE_TYPES for source in sources)
        ):
            raise ValueError("Origem inválida")
        if "display_order" in field and (
            type(field["display_order"]) is not int or not 0 <= field["display_order"] <= 100
        ):
            raise ValueError("Ordem de campo inválida")
        limits = field.get("constraints", {})
        if not isinstance(limits, dict) or set(limits) - {
            "min_length",
            "max_length",
            "min_items",
            "max_items",
            "enum",
        }:
            raise ValueError("Restrições inválidas")
        for name in ("min_length", "max_length", "min_items", "max_items"):
            if name in limits and (
                type(limits[name]) is not int
                or not 0 <= limits[name] <= (100 if "items" in name else field["max_length"])
            ):
                raise ValueError("Limite inválido")
        for lo, hi in (("min_length", "max_length"), ("min_items", "max_items")):
            if limits.get(lo, 0) > limits.get(hi, 12000):
                raise ValueError("Intervalo inválido")
        choices = limits.get("enum", [])
        if (
            not isinstance(choices, list)
            or len(choices) > 50
            or any(
                not isinstance(value, str) or not value or len(value) > field["max_length"]
                for value in choices
            )
            or len(set(choices)) != len(choices)
        ):
            raise ValueError("Opções inválidas")
        if field.get("type") == "enum" and not choices:
            raise ValueError("Informe opções para o campo")
    return fields


def empty(value):
    return (
        value is None or isinstance(value, str) and not value.strip() or isinstance(value, list) and not value
    )


def display_value(value):
    if value is None:
        return ""
    if type(value) is bool:
        return "Sim" if value else "Não"
    return "\n".join(value) if isinstance(value, list) else value


def value_error(field, value, required=False):
    if empty(value):
        return "FIELD_REQUIRED" if required and field["required"] else None
    kind, limits = field.get("type", "text"), field.get("constraints", {})
    if kind == "boolean":
        return None if type(value) is bool else "FIELD_TYPE"
    if kind == "string_list":
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            return "FIELD_TYPE"
        if not limits.get("min_items", 1) <= len(value) <= limits.get("max_items", 30):
            return "FIELD_LIMIT"
        values = value
    else:
        if not isinstance(value, str):
            return "FIELD_TYPE"
        if kind == "enum" and value not in limits.get("enum", []):
            return "FIELD_CHOICE"
        values = [value]
    if any(
        not limits.get("min_length", 0)
        <= len(item)
        <= min(field["max_length"], limits.get("max_length", field["max_length"]))
        for item in values
    ):
        return "FIELD_LIMIT"
    if required and kind != "enum" and any(unresolved_value(item) for item in values):
        return "FIELD_PLACEHOLDER"
    return None


def render(content, fields, values):
    replacements = {
        field["key"]: display_value(values.get(field["key"]))
        for field in fields
        if not empty(values.get(field["key"])) or not field["required"]
    }
    return re.sub(
        r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}",
        lambda match: replacements.get(match.group(1), match.group(0)),
        content,
    )


def attachment_errors(item, fields, required=True):
    messages = {
        "FIELD_REQUIRED": "Preencha este campo obrigatório.",
        "FIELD_TYPE": "Use o tipo esperado para este campo.",
        "FIELD_LIMIT": "Confira os limites deste campo.",
        "FIELD_CHOICE": "Escolha uma opção válida.",
        "FIELD_PLACEHOLDER": "Substitua o marcador ou texto pendente por uma informação específica.",
    }
    errors = []
    for field in fields:
        value = item.get("fields", {}).get(field["key"])
        code = value_error(field, value, required)
        if not code and required and "{{" + field["key"] + "}}" in item.get("edited_content", ""):
            code = "FIELD_PLACEHOLDER"
        if code:
            errors.append(
                {
                    "attachment_id": item["template_id"],
                    "field_id": field["key"],
                    "code": code,
                    "message": messages[code],
                    "action": "Revisar " + field["label"],
                }
            )
    if required:
        known = {field["key"] for field in fields}
        content = item.get("edited_content", "")
        unresolved = set(re.findall(r"\{\{([^{}\r\n]+)\}\}", content)) - known
        if unresolved:
            errors.append(
                {
                    "attachment_id": item["template_id"],
                    "code": "FIELD_PLACEHOLDER",
                    "message": "O conteúdo contém marcadores que não pertencem ao esquema deste anexo.",
                    "action": "Editar conteúdo do anexo",
                }
            )
        elif content.strip() and unresolved_value(content) and not errors:
            errors.append(
                {
                    "attachment_id": item["template_id"],
                    "code": "FIELD_PLACEHOLDER",
                    "message": "O conteúdo revisado ainda está pendente de preenchimento.",
                    "action": "Editar conteúdo do anexo",
                }
            )
    return errors


def field_error(errors, code="ATTACHMENT_INCOMPLETE"):
    return RuleError(code, "Revise os campos indicados antes de enviar.", 400, {"fields": errors})


def provenance(field, value, previous=None):
    if previous and previous.get("value") == value:
        return previous
    result = {
        "field_id": field["key"],
        "field_revision": (previous or {}).get("field_revision", 0) + 1,
        "value": value,
        "state": "empty" if empty(value) else "user_edited",
        "source_type": "user_edit",
        "source_refs": [],
        "suggestion_version": None,
        "edited_by_user": bool(previous) or not empty(value),
        "updated_at": utcnow().isoformat(),
    }
    if previous and previous.get("preparation_id"):
        result["preparation_id"] = previous["preparation_id"]
    return result


def attachment_signature(item):
    return canonical_hash(
        {key: item.get(key) for key in ("source_version_id", "fields", "edited_content", "enabled")}
    )
