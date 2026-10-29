import re
from dataclasses import dataclass
from itertools import pairwise

POLICY_VERSION = "developer_no_effect_v1"
EXECUTION_PROTOCOL = "d1_rest_single_query_v1"
MISSING_EVIDENCE = {
    "remote_identity": "Confirmar novamente a credencial, a conta e o banco D1 vinculados à tentativa.",
    "operation_binding": "Vincular a unidade de execução à operação e à revisão original preservadas.",
    "immutable_source": "Conferir o conteúdo integral e o hash da revisão original da tentativa.",
    "complete_history": "Confirmar todos os recibos anteriores e seus hashes, sem divergências.",
    "absent_receipt": "Descartar recibo ou confirmação de aplicação da migration pendente.",
    "execution_unit": "Obter o registro persistido da unidade SQL efetivamente enviada; a tentativa antiga não pode receber esse registro por suposição.",
    "pre_upload_checkpoint": "Obter um checkpoint persistido anterior ao upload, sem qualquer evidência contraditória de envio.",
    "no_additional_effects": "Descartar outra unidade de execução ou efeito de publicação ainda pendente.",
    "intact_baseline": "Reconstruir a baseline integral a partir do histórico imutável e vinculá-la à execução.",
    "complete_remote_schema": "Observar o schema remoto completo e consistente, sem resposta parcial.",
    "unchanged_schema": "Confirmar que o schema completo permanece igual à baseline original.",
    "absent_target_column": "Confirmar a ausência da coluna exata no inventário completo da tabela original.",
    "ordinary_target_table": "Confirmar uma tabela comum, sem mecanismo virtual ou efeito adicional não analisado.",
    "supported_statement": "A recuperação pública aceita somente a unidade estrutural suportada ou uma rejeição durável comprovada antes de qualquer efeito.",
    "single_statement": "A unidade precisa conter exatamente um comando; lotes e execuções parciais exigem evidência adicional.",
    "durable_execution_result": "Comprovar a rejeição anterior a qualquer efeito pelo protocolo de execução persistido.",
    "recorded_metadata": "Obter registros de execução íntegros; metadados malformados não comprovam ausência de efeitos.",
}


@dataclass(frozen=True)
class ColumnAddition:
    table: str
    column: str


def _word(token, value):
    return token.kind == "word" and token.value == value


def _identifier(token):
    return (
        token.kind in {"word", "identifier"}
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", token.value) is not None
        and not token.value.casefold().startswith(("sqlite_", "_cf_", "__viroucaseai_"))
    )


def constant_column_addition(tokens):
    tokens = list(tokens)
    if tokens and tokens[-1].kind == "symbol" and tokens[-1].value == ";":
        tokens.pop()
    if (
        len(tokens) < 6 or any(token.depth != 0 or token.kind == "invalid" for token in tokens)
        or not _word(tokens[0], "ALTER") or not _word(tokens[1], "TABLE")
        or not _identifier(tokens[2]) or not _word(tokens[3], "ADD")
    ):
        return None
    index = 5 if _word(tokens[4], "COLUMN") else 4
    if (
        len(tokens) <= index + 1 or not _identifier(tokens[index])
        or tokens[index + 1].kind != "word"
        or tokens[index + 1].value not in {"TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC"}
    ):
        return None
    result = ColumnAddition(tokens[2].value.casefold(), tokens[index].value.casefold())
    index += 2
    not_null = len(tokens) > index and _word(tokens[index], "NOT")
    if not_null:
        if len(tokens) <= index + 1 or not _word(tokens[index + 1], "NULL"):
            return None
        index += 2
    if index == len(tokens):
        return None if not_null else result
    if not _word(tokens[index], "DEFAULT"):
        return None
    value = tokens[index + 1:]
    if len(value) == 1 and value[0].kind == "literal":
        return result
    if len(value) == 1 and _word(value[0], "NULL"):
        return None if not_null else result
    if not value or any(
        current.line != previous.line or current.column != previous.column + len(previous.value)
        for previous, current in pairwise(value)
    ):
        return None
    if len(value) == 2 and _word(value[0], "X") and value[1].kind == "literal":
        return result if re.fullmatch(r"(?:[a-fA-F0-9]{2})*", value[1].value) is not None else None
    if (
        all(token.kind in {"word", "symbol"} for token in value)
        and re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:E[+-]?[0-9]+)?", "".join(token.value for token in value))
    ):
        return result
    return None


def decide_no_effect(proofs, *, classification, statement_count, contains_dml, structural, durable):
    common = (
        "remote_identity", "operation_binding", "immutable_source", "complete_history", "absent_receipt",
        "execution_unit", "pre_upload_checkpoint", "no_additional_effects", "intact_baseline",
        "complete_remote_schema", "recorded_metadata",
    )
    missing = [name for name in common if proofs.get(name) is not True]
    if contains_dml:
        reason, missing = "DML_UNCERTAIN", [*missing, "supported_statement"]
    elif statement_count != 1:
        reason, missing = "UNSUPPORTED_EFFECTS", [*missing, "single_statement"]
    elif classification in {"confirmed", "schema_applied"}:
        reason, missing = "SCHEMA_APPLIED", [*missing, "absent_receipt"]
    elif classification == "conflict":
        reason = "EVIDENCE_CHANGED"
        missing = [*missing, "complete_history"]
    elif missing:
        reason = "PROOF_INCOMPLETE"
    elif durable and proofs.get("durable_execution_result") is True:
        return {"eligible": True, "reason_code": "NO_EFFECT_PROVEN", "missing_evidence": [], "origin": "durable_no_effect"}
    elif not structural:
        reason, missing = "UNSUPPORTED_EFFECTS", ["supported_statement"]
    else:
        missing = [name for name in ("unchanged_schema", "absent_target_column", "ordinary_target_table") if proofs.get(name) is not True]
        if not missing and classification == "schema_unchanged":
            return {"eligible": True, "reason_code": "NO_EFFECT_PROVEN", "missing_evidence": [], "origin": "automated_no_effect_proof"}
        reason = "PROOF_INCOMPLETE"
        if not missing:
            missing = ["durable_execution_result"]
    return {"eligible": False, "reason_code": reason, "missing_evidence": list(dict.fromkeys(missing)), "origin": "unverified"}
