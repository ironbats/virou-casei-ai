import re
import sqlite3
from dataclasses import dataclass

ANALYSIS_METHOD = "sql_lexical_v2"
RULES = {
    "drop_object": (
        "DROP remove uma estrutura do banco e pode eliminar seus dados.",
        "Preserve a estrutura aplicada e prepare uma evolução incremental sem remoção de dados.",
    ),
    "delete_rows": (
        "DELETE executável pode remover registros existentes.",
        "Revise a remoção de registros e prepare uma migration que preserve os dados publicados.",
    ),
    "truncate_rows": (
        "TRUNCATE solicita a remoção dos registros de uma tabela.",
        "Preserve os registros existentes; não use limpeza de dados na publicação automática.",
    ),
    "update_rows": (
        "UPDATE executável altera registros existentes e exige revisão do impacto nos dados.",
        "Revise o backfill e seus filtros; a publicação automática não aprova alterações de dados por inferência.",
    ),
    "replace_rows": (
        "REPLACE pode substituir registros existentes, inclusive removendo o registro anterior.",
        "Evite substituição de registros existentes e revise uma evolução incremental que preserve os dados.",
    ),
    "external_database": (
        "ATTACH ou DETACH altera os bancos acessíveis à conexão.",
        "Use somente o banco autorizado da aplicação; remova alterações de conexão desta migration.",
    ),
    "connection_setting": (
        "PRAGMA altera ou consulta configurações da conexão fora do fluxo de migrations autorizado.",
        "Revise a necessidade da configuração sem desativar as proteções de integridade do banco.",
    ),
    "ledger_access": (
        "A migration referencia o histórico de recibos reservado à plataforma.",
        "Preserve os recibos e checksums da plataforma; não acesse esse histórico pelo SQL do projeto.",
    ),
    "unrecognized_sql": (
        "O SQL contém uma sequência incompleta ou inválida que impede a conferência segura.",
        "Corrija as aspas, o comentário ou o caractere inválido na localização indicada antes de publicar.",
    ),
}
OPERATIONS = {
    "DROP": "drop_object",
    "DELETE": "delete_rows",
    "TRUNCATE": "truncate_rows",
    "UPDATE": "update_rows",
    "REPLACE": "replace_rows",
    "ATTACH": "external_database",
    "DETACH": "external_database",
    "PRAGMA": "connection_setting",
}
STATEMENT_TYPES = {
    "CREATE TABLE",
    "CREATE TRIGGER",
    "CREATE INDEX",
    "CREATE VIEW",
    "ALTER TABLE",
    "DROP",
    "DELETE",
    "TRUNCATE",
    "UPDATE",
    "REPLACE",
    "ATTACH",
    "DETACH",
    "PRAGMA",
    "INSERT",
    "SELECT",
    "WITH",
    "OTHER",
}
TOKEN = re.compile(
    r"(?P<space>[\s\ufeff]+)"
    r"|(?P<line_comment>--[^\n]*)"
    r"|(?P<block_comment>/\*[\s\S]*?(?:\*/|$))"
    r"|(?P<literal>'(?:[^'\x00]|'')*')"
    r'|(?P<double_quote>"(?:[^"\x00]|"")*")'
    r"|(?P<backtick>`(?:[^`\x00]|``)*`)"
    r"|(?P<bracket>\[[^\]\x00]*\])"
    r"|(?P<word>[^\W\d][\w$]*)"
    r"|(?P<symbol>.)",
    re.UNICODE,
)


@dataclass(frozen=True)
class Token:
    value: str
    kind: str
    line: int
    column: int
    depth: int


def _tokens(sql):
    result, offset, line, column, depth = [], 0, 1, 1, 0
    while offset < len(sql):
        if sql[offset] == "\x00":
            result.append(Token("", "invalid", line, column, depth))
            break
        match = TOKEN.match(sql, offset)
        if not match:
            result.append(Token("", "invalid", line, column, depth))
            break
        raw, kind = match.group(), match.lastgroup
        if kind == "symbol" and raw in {"'", '"', "`", "["}:
            result.append(Token("", "invalid", line, column, depth))
            break
        if kind not in {"space", "line_comment", "block_comment"}:
            if kind in {"literal", "double_quote", "backtick", "bracket"}:
                value = raw[1:-1]
                if kind != "bracket":
                    value = value.replace(raw[0] * 2, raw[0])
                kind = "literal" if kind == "literal" else "identifier"
            else:
                value = raw.upper() if kind == "word" else raw
            if kind == "symbol" and value == ")":
                depth -= 1
            result.append(Token(value, kind, line, column, depth))
            if kind == "symbol" and value == "(":
                depth += 1
            elif kind == "symbol" and value == ";":
                depth = 0
        newline = raw.rfind("\n")
        if newline >= 0:
            line += raw.count("\n")
            column = len(raw) - newline
        else:
            column += len(raw)
        offset = match.end()
    return result


def _word(token, *values):
    return token.kind == "word" and token.value in values


def _statement_type(tokens):
    first = tokens[0]
    if first.kind != "word":
        return "OTHER"
    if first.value == "CREATE":
        index = 1
        if len(tokens) > index and _word(tokens[index], "TEMP", "TEMPORARY", "UNIQUE"):
            index += 1
        if len(tokens) > index and _word(tokens[index], "TABLE", "TRIGGER", "INDEX", "VIEW"):
            return "CREATE " + tokens[index].value
    if first.value == "ALTER" and len(tokens) > 1 and _word(tokens[1], "TABLE"):
        return "ALTER TABLE"
    return first.value if first.value in STATEMENT_TYPES else "OTHER"


def _foreign_key_action(tokens, index, statement_type):
    if statement_type not in {"CREATE TABLE", "ALTER TABLE"} or index < 2 or index + 1 >= len(tokens):
        return False
    if not _word(tokens[index - 1], "ON"):
        return False
    action = tokens[index + 1]
    if not (
        _word(action, "CASCADE", "RESTRICT")
        or index + 2 < len(tokens)
        and (
            _word(action, "SET")
            and _word(tokens[index + 2], "NULL", "DEFAULT")
            or _word(action, "NO")
            and _word(tokens[index + 2], "ACTION")
        )
    ):
        return False
    depth = tokens[index].depth
    for previous in reversed(tokens[: index - 1]):
        if (
            previous.depth < depth
            or previous.kind == "symbol"
            and previous.value == ","
            and previous.depth == depth
        ):
            return False
        if previous.depth == depth and _word(previous, "REFERENCES"):
            return True
    return False


def _trigger_event(tokens, index, statement_type):
    if statement_type != "CREATE TRIGGER" or index + 1 >= len(tokens):
        return False
    header = tokens[:index]
    return (
        not any(_word(token, "BEGIN", "ON") for token in header)
        and index > 1
        and (
            _word(tokens[index + 1], "ON")
            or _word(tokens[index], "UPDATE")
            and _word(tokens[index + 1], "OF")
        )
    )


def _finding(token, statement_index, statement_type, operation, rule):
    explanation, hint = RULES[rule]
    return {
        "line": token.line,
        "column": token.column,
        "statement_index": statement_index,
        "statement_type": statement_type,
        "operation": operation,
        "rule": rule,
        "explanation": explanation,
        "hint": hint,
    }


def _review_statement(tokens, statement_index, ledger):
    statement_type = _statement_type(tokens)
    for index, token in enumerate(tokens):
        if token.kind == "invalid":
            return _finding(token, statement_index, statement_type, "UNKNOWN", "unrecognized_sql")
        if ledger.casefold() in token.value.casefold():
            return _finding(token, statement_index, statement_type, "LEDGER", "ledger_access")
        if token.kind != "word" or token.value not in OPERATIONS:
            continue
        if token.value in {"DELETE", "UPDATE"} and (
            _foreign_key_action(tokens, index, statement_type)
            or _trigger_event(tokens, index, statement_type)
        ):
            continue
        if token.value == "REPLACE" and index + 1 < len(tokens) and tokens[index + 1].value == "(":
            continue
        return _finding(token, statement_index, statement_type, token.value, OPERATIONS[token.value])
    return None


def sql_review_findings(sql, ledger):
    findings, statement, number = [], [], 0
    for token in _tokens(sql):
        if token.kind == "symbol" and token.value == ";":
            if statement:
                number += 1
                finding = _review_statement(statement, number, ledger)
                if finding:
                    findings.append(finding)
                statement = []
        else:
            statement.append(token)
    if statement:
        finding = _review_statement(statement, number + 1, ledger)
        if finding:
            findings.append(finding)
    return findings


def split_sql_statements(sql):
    statements, start = [], 0
    for index, char in enumerate(sql):
        if char == ";" and sqlite3.complete_statement(sql[start:index + 1]):
            statement = sql[start:index + 1]
            if any(token.value != ";" for token in _tokens(statement)):
                statements.append(statement)
            start = index + 1
    remaining = sql[start:]
    if any(token.value != ";" for token in _tokens(remaining)):
        statements.append(remaining)
    return statements


def null_backfill_target(statement):
    tokens = _tokens(statement)
    if tokens and tokens[-1].value == ";":
        tokens = tokens[:-1]
    if len(tokens) < 10 or not _word(tokens[0], "UPDATE") or not _word(tokens[2], "SET"):
        return None
    if tokens[1].kind not in {"word", "identifier"} or tokens[3].kind not in {"word", "identifier"}:
        return None
    if tokens[4].value != "=":
        return None
    where = next((i for i, token in enumerate(tokens[5:], 5) if _word(token, "WHERE") and token.depth == 0), None)
    if where is None or where + 4 != len(tokens):
        return None
    predicate = tokens[where + 1:]
    if (
        predicate[0].kind not in {"word", "identifier"}
        or predicate[0].value.casefold() != tokens[3].value.casefold()
        or not _word(predicate[1], "IS") or not _word(predicate[2], "NULL")
    ):
        return None
    expression = tokens[5:where]
    if not expression or any(token.depth == 0 and token.value == "," for token in expression):
        return None
    forbidden = {"SELECT", "WITH", "RANDOM", "RANDOMBLOB", "LOAD_EXTENSION", "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME"}
    if any(token.kind == "invalid" or _word(token, *forbidden) for token in expression):
        return None
    if any(token.kind == "word" and token.value in OPERATIONS for token in expression):
        return None
    if any(token.value in {";", "?", ":", "@", "$"} for token in expression):
        return None
    return {"table": tokens[1].value.casefold(), "column": tokens[3].value.casefold()}


def review_evidence(value):
    rule, operation, statement_type = value.get("rule"), value.get("operation"), value.get("statement_type")
    if (
        not isinstance(rule, str)
        or rule not in RULES
        or not isinstance(operation, str)
        or operation not in {*OPERATIONS, "LEDGER", "UNKNOWN"}
        or not isinstance(statement_type, str)
        or statement_type not in STATEMENT_TYPES
        or any(
            type(value.get(key)) is not int or not 1 <= value[key] <= 10_000_000
            for key in ("line", "column", "statement_index")
        )
    ):
        return {}
    expected_rule = OPERATIONS.get(
        operation, {"LEDGER": "ledger_access", "UNKNOWN": "unrecognized_sql"}.get(operation)
    )
    if rule != expected_rule:
        return {}
    explanation, hint = RULES[rule]
    return {
        **{key: value[key] for key in ("line", "column", "statement_index")},
        "statement_type": statement_type,
        "operation": operation,
        "rule": rule,
        "explanation": explanation,
        "hint": hint,
    }
