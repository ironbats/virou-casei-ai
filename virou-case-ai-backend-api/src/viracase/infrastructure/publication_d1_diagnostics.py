import re

from viracase.infrastructure.publication_sql_review import _statement_type, _tokens, split_sql_statements

REASONS = {
    "local_dispatch_blocked": "A conferência local interrompeu a solicitação antes do envio à Cloudflare.",
    "sql_syntax_error": "O banco recusou a sintaxe da instrução SQL.",
    "missing_table": "A instrução referencia uma tabela que não existe no schema atual.",
    "missing_column": "A instrução referencia uma coluna que não existe no schema atual.",
    "duplicate_column": "A coluna que a migration tenta criar já existe no schema atual.",
    "object_exists": "A estrutura que a migration tenta criar já existe no schema atual.",
    "incomplete_sql": "A instrução SQL está incompleta.",
    "empty_sql": "O banco não encontrou uma instrução SQL para executar.",
    "constraint_failed": "O banco recusou uma violação de restrição de integridade.",
    "sql_rejected": "O banco recusou a consulta; a resposta não comprova a ausência de efeitos.",
}
COMPILE_REASONS = {
    "sql_syntax_error", "missing_table", "missing_column", "duplicate_column",
    "object_exists", "incomplete_sql", "empty_sql",
}
UPLOAD_CODES = {
    "DEPLOYMENT_NOT_VISIBLE", "DEPLOYMENTS_RESPONSE_INVALID", "DEPLOYMENTS_AMBIGUOUS",
    "BINDINGS_CONFIRMED", "BINDINGS_MISMATCH", "BINDINGS_RESPONSE_INVALID", "BINDINGS_NOT_VISIBLE",
    "DEPENDENCY_UNAVAILABLE", "CONFIGURATION_INVALID", "QUOTA_EXCEEDED", "PUBLICATION_REJECTED",
    "RESOURCE_CONFLICT", "PUBLICATION_RESULT_UNKNOWN", "UPLOAD_SCRIPT_MISMATCH", "OBSERVATION_FAILED",
}
PATTERNS = (
    ("duplicate_column", r"^duplicate column name\s*:"),
    ("missing_table", r"^no such table\s*:"),
    ("missing_column", r"^(?:no such column\s*:|table\s+[^\s:;]+\s+has no column named\b)"),
    ("object_exists", r"^(?:table|index|view|trigger)\s+[^\s:;]+\s+already exists\b"),
    ("sql_syntax_error", r'^(?:(?:near\s+"[^"\r\n]{1,120}"\s*:\s*)?syntax error\b|unrecognized token\s*:)'),
    ("incomplete_sql", r"^incomplete input\b"),
    ("empty_sql", r"^no SQL statements detected\b"),
    ("constraint_failed", r"^(?:UNIQUE|NOT NULL|CHECK|FOREIGN KEY|PRIMARY KEY) constraint failed\b"),
)


def sql_error_summary(messages):
    for message in messages:
        if not isinstance(message, str):
            continue
        value = re.sub(r"^(?:(?:D1_ERROR|SQLITE_ERROR|SQL error)\s*:\s*)+", "", message[:16384].strip(), flags=re.IGNORECASE)
        for reason, pattern in PATTERNS:
            if re.search(pattern, value, re.IGNORECASE):
                result = {"reason": reason, "message": REASONS[reason]}
                line = re.search(r"\bError in line\s+([1-9][0-9]{0,6})\b", value, re.IGNORECASE)
                if line:
                    result["line"] = int(line[1])
                return result
    return {"reason": "sql_rejected", "message": REASONS["sql_rejected"]}


def d1_error_evidence(envelope, sql):
    errors = envelope.get("errors", []) if isinstance(envelope, dict) else []
    errors = errors if isinstance(errors, list) else []
    result = envelope.get("result") if isinstance(envelope, dict) else None
    rows = result if isinstance(result, list) else []
    messages = [item.get("message") for item in errors[:10] if isinstance(item, dict)]
    messages.extend(item.get("error") for item in rows[:1000] if isinstance(item, dict))
    summary = sql_error_summary(messages)
    statements = split_sql_statements(sql) if isinstance(sql, str) else []
    has_success = any(isinstance(item, dict) and item.get("success") is True for item in rows)
    has_change = any(
        isinstance(item, dict) and isinstance(item.get("meta"), dict)
        and (
            item["meta"].get("changed_db") is True
            or isinstance(item["meta"].get("rows_written"), (int, float)) and item["meta"]["rows_written"] > 0
            or isinstance(item["meta"].get("changes"), (int, float)) and item["meta"]["changes"] > 0
        )
        for item in rows
    )
    provider_error = bool(errors) and all(
        isinstance(item, dict) and type(item.get("code")) is int and isinstance(item.get("message"), str)
        for item in errors
    )
    statement_error = len(rows) == 1 and isinstance(rows[0], dict) and rows[0].get("success") is False and isinstance(rows[0].get("error"), str)
    recognized_messages = [sql_error_summary([message])["reason"] for message in messages if isinstance(message, str)]
    statement_type = _statement_type(_tokens(statements[0])) if len(statements) == 1 and _tokens(statements[0]) else None
    no_effect = (
        len(statements) == 1
        and statement_type in {"CREATE TABLE", "CREATE INDEX", "CREATE VIEW", "CREATE TRIGGER", "ALTER TABLE"}
        and summary["reason"] in COMPILE_REASONS
        and not has_success and not has_change
        and recognized_messages and all(reason in COMPILE_REASONS for reason in recognized_messages)
        and (provider_error or statement_error)
        and (result is None or isinstance(result, list) and len(result) <= 1)
    )
    return {
        **summary,
        "effect_outcome": "none" if no_effect else "unknown",
        "statement_count": len(statements),
        "confirmed_statements": sum(isinstance(item, dict) and item.get("success") is True for item in rows),
        **({"statement_index": 1} if len(statements) == 1 else {}),
    }


def sql_result_evidence(results):
    result = {"confirmed_statements": len(results) if isinstance(results, list) else 0}
    for key in ("changes", "rows_written", "rows_read"):
        values = [
            item.get("meta", {}).get(key) for item in results
            if isinstance(item, dict) and isinstance(item.get("meta"), dict)
        ] if isinstance(results, list) else []
        if values and all(type(value) in {int, float} and 0 <= value <= 10 ** 15 for value in values):
            result[key] = sum(values)
    return result


def sanitize_provider_diagnostic(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    method = value.get("method")
    if isinstance(method, str) and method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        result["method"] = method
    route = value.get("route")
    if isinstance(route, str) and re.fullmatch(r"(?:cloudflare_api|/(?:accounts/\{account\}|user)(?:/[a-z0-9_-]+|/\{(?:database|worker)\})*)", route):
        result["route"] = route
    if type(value.get("status")) is int and 100 <= value["status"] <= 599:
        result["status"] = value["status"]
    codes = value.get("provider_error_codes")
    if isinstance(codes, list):
        result["provider_error_codes"] = [code for code in codes[:10] if type(code) is int and 0 <= code <= 2 ** 31]
    ray = value.get("cf_ray")
    if isinstance(ray, str) and re.fullmatch(r"[a-fA-F0-9]{8,32}(?:-[A-Z]{3})?", ray):
        result["cf_ray"] = ray
    reason = value.get("reason")
    if isinstance(reason, str) and reason in REASONS:
        result.update(reason=reason, message=REASONS[reason])
    elif reason == "incomplete_results":
        result.update(reason=reason, message="O banco não confirmou o resultado de todas as instruções enviadas.")
    for key in ("statement_index", "statement_count", "confirmed_statements", "line"):
        if type(value.get(key)) is int and 0 <= value[key] <= 10_000_000:
            result[key] = value[key]
    if isinstance(value.get("effect_outcome"), str) and value["effect_outcome"] in {"none", "not_sent", "unknown", "confirmed"}:
        result["effect_outcome"] = value["effect_outcome"]
    for key, allowed in (
        ("transport_error", {"ConnectError", "ConnectTimeout", "PoolTimeout", "ReadError", "ReadTimeout", "WriteError", "WriteTimeout", "RemoteProtocolError", "CloseError"}),
        ("response_error", {"ValueError", "KeyError", "TypeError", "JSONDecodeError"}),
    ):
        if isinstance(value.get(key), str) and value[key] in allowed:
            result[key] = value[key]
    code = value.get("code")
    if isinstance(code, str) and code in UPLOAD_CODES:
        result["code"] = code
    confirmation = _upload_confirmation(value.get("upload_confirmation"))
    if confirmation is not None:
        result["upload_confirmation"] = confirmation
    return result


def _upload_confirmation(value):
    if not isinstance(value, dict) or type(value.get("confirmed")) is not bool:
        return None
    result = {"confirmed": value["confirmed"], "attempts": []}
    attempts = value.get("attempts")
    for item in attempts[:12] if isinstance(attempts, list) else []:
        if not isinstance(item, dict) or type(item.get("attempt")) is not int or not 1 <= item["attempt"] <= 12:
            continue
        if type(item.get("confirmed")) is not bool:
            continue
        code = item.get("code")
        clean = {
            "attempt": item["attempt"], "confirmed": item["confirmed"],
            "code": code if isinstance(code, str) and code in UPLOAD_CODES else "OBSERVATION_FAILED",
        }
        if type(item.get("new_deployments")) is int and 0 <= item["new_deployments"] <= 1_000_000:
            clean["new_deployments"] = item["new_deployments"]
        if type(item.get("retryable")) is bool:
            clean["retryable"] = item["retryable"]
        if type(item.get("duration_ms")) is int and 0 <= item["duration_ms"] <= 3_600_000:
            clean["duration_ms"] = item["duration_ms"]
        result["attempts"].append(clean)
    if type(value.get("duration_ms")) is int and 0 <= value["duration_ms"] <= 3_600_000:
        result["duration_ms"] = value["duration_ms"]
    return result
