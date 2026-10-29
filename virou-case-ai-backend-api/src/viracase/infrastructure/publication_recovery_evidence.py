import re
from hashlib import sha256

from viracase.domain.rules import RuleError, canonical_hash
from viracase.infrastructure.publication_d1_diagnostics import sanitize_provider_diagnostic
from viracase.infrastructure.publication_migration_validation import validate_migration_plan
from viracase.infrastructure.publication_migrations import (
    LEDGER,
    confirmed_migrations,
    migration_schema_fingerprint,
)
from viracase.infrastructure.publication_outcome import migration_identity
from viracase.infrastructure.publication_sql_review import _tokens, split_sql_statements, sql_review_findings

POLICY_VERSION = "migration_recovery_evidence_v2"
MAX_MIGRATIONS = 512
MAX_STATEMENTS = 2000
SECURITY_IDENTIFIER = re.compile(
    r"password|passwd|secret|token|salt|permission|role|admin|auth|credential|session|private_key|api_key|privilege",
    re.IGNORECASE,
)
MISSING_EVIDENCE = {
    "pending_migration_identity": "Identificação persistida da migration pendente.",
    "operation_identity": "Vínculo da tentativa com a operação e a revisão autorizadas.",
    "database_identity": "Identidade consistente do banco autorizado.",
    "immutable_migration_source": "Arquivo integral da migration na revisão imutável da tentativa.",
    "source_limits": "Histórico de migrations dentro dos limites de reconstrução isolada.",
    "matching_migration_hash": "Hash do arquivo correspondente ao hash persistido da tentativa.",
    "consistent_local_history": "Histórico local consistente com os arquivos imutáveis.",
    "valid_remote_history": "Recibos remotos completos e com estrutura válida.",
    "remote_ledger": "Tabela de recibos original disponível no banco autorizado.",
    "matching_remote_history": "Recibos remotos com os hashes exatos do histórico da revisão.",
    "complete_history_prefix": "Recibo confirmado de cada migration anterior à tentativa pendente.",
    "complete_execution_result": "Confirmação durável da execução de todas as instruções desta tentativa.",
    "reconstructable_schema": "Schema esperado reconstruído a partir do histórico imutável.",
    "matching_remote_schema": "Schema remoto completo correspondente ao resultado esperado ou à baseline.",
    "matching_saved_schema": "Schema remoto consistente com a observação já persistida após a execução.",
    "previously_confirmed_receipt": "Recibo remoto que já havia sido confirmado e agora está ausente.",
    "supported_structural_statements": "Prova específica da execução das instruções que não são DDL aditivo suportado.",
    "operator_structural_attestation": "Atestação administrativa vinculada a esta observação estrutural completa.",
    "authenticated_execution_evidence": "Evidência de execução autenticada independentemente da declaração do operador.",
}
ACTIONS = {
    "confirmed": (
        "confirm_receipt",
        "Confirmar o recibo desta mesma migration sem executar novamente seu SQL e continuar a reconciliação.",
    ),
    "no_effect": (
        "record_no_effect",
        "Registrar a tentativa comprovadamente sem efeito e encerrar seu impedimento sem alterar recibos.",
    ),
    "schema_applied": (
        "adopt_schema",
        (
            "O responsável pode atestar a adoção do estado estrutural integralmente verificado. "
            "A origem será administrativa; isso não representa uma resposta histórica de sucesso do provedor."
        ),
    ),
    "schema_unchanged": (
        "confirm_schema_unchanged",
        (
            "O responsável pode atestar que o estado estrutural observado permanece igual à baseline verificada. "
            "A ação encerra esta tentativa sem registrar a migration como aplicada."
        ),
    ),
    "unknown": (
        "collect_execution_evidence",
        (
            "Obtenha a evidência indicada para esta migration. Uma declaração, um hash ou a existência de uma coluna "
            "não confirma a execução; o SQL permanece bloqueado para repetição."
        ),
    ),
    "conflict": (
        "resolve_history_conflict",
        (
            "Confira a divergência entre a revisão imutável, os recibos e a tentativa. "
            "Preserve os hashes, os dados e o marcador enquanto a divergência não for resolvida."
        ),
    ),
}


def _safe_identity(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value) is not None


def _finish(report, classification, origin="unverified", missing=(), pending_update=None):
    action, required_action = ACTIONS[classification]
    report.update(
        classification=classification,
        origin=origin,
        action=action,
        requires_operator=classification in {"schema_applied", "schema_unchanged"},
        missing_evidence=list(dict.fromkeys(missing)),
        required_action=required_action,
    )
    report["missing_evidence_messages"] = [MISSING_EVIDENCE[code] for code in report["missing_evidence"]]
    report["evidence_sha256"] = canonical_hash(report)
    if pending_update is not None:
        pending_update["recovery_evidence"] = {
            "version": 1,
            "policy_version": POLICY_VERSION,
            "origin": origin,
            "evidence_sha256": report["evidence_sha256"],
            "source_manifest_sha256": report["source_manifest_sha256"],
            "expected_schema_sha256": report["schema"]["expected_sha256"],
        }
        report["pending_update"] = pending_update
    return report


def _remote_receipts(gateway, database_id):
    rows = gateway.query(database_id, f"SELECT name, sha256 FROM {LEDGER} ORDER BY name LIMIT 513")
    if (
        not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
        or rows[0].get("success") is not True or not isinstance(rows[0].get("results"), list)
        or len(rows[0]["results"]) > MAX_MIGRATIONS
    ):
        return None
    receipts = {}
    for row in rows[0]["results"]:
        identity = migration_identity(row)
        if identity is None or identity["name"] in receipts:
            return None
        receipts[identity["name"]] = identity["sha256"]
    return receipts


def _word(token, value):
    return token.kind == "word" and token.value == value


def _identifier(token):
    return token.kind in {"word", "identifier"}


def _structural_statement(statement):
    tokens = _tokens(statement)
    if tokens and tokens[-1].value == ";":
        tokens = tokens[:-1]
    if not tokens or any(token.kind == "invalid" for token in tokens):
        return "unsupported"
    if any(token.kind in {"word", "identifier"} and SECURITY_IDENTIFIER.search(token.value) for token in tokens):
        return "security_sensitive"
    if any(token.kind == "word" and token.value in {"LOAD_EXTENSION", "READFILE", "WRITEFILE"} for token in tokens):
        return "unsupported"
    if sql_review_findings(statement, LEDGER):
        return "unsupported"
    if _word(tokens[0], "ALTER"):
        if (
            len(tokens) >= 6 and _word(tokens[1], "TABLE") and _identifier(tokens[2])
            and _word(tokens[3], "ADD")
        ):
            index = 5 if _word(tokens[4], "COLUMN") else 4
            if len(tokens) > index + 1 and _identifier(tokens[index]):
                return "add_column"
        return "unsupported"
    if not _word(tokens[0], "CREATE") or len(tokens) < 4:
        return "unsupported"
    index = 1
    if _word(tokens[index], "UNIQUE"):
        index += 1
        if not _word(tokens[index], "INDEX"):
            return "unsupported"
    kind = tokens[index].value if tokens[index].kind == "word" else None
    if kind not in {"TABLE", "INDEX", "VIEW"}:
        return "unsupported"
    index += 1
    if len(tokens) > index + 2 and all(
        _word(token, value) for token, value in zip(tokens[index:index + 3], ("IF", "NOT", "EXISTS"), strict=True)
    ):
        index += 3
    if len(tokens) <= index + 1 or not _identifier(tokens[index]):
        return "unsupported"
    following = tokens[index + 1]
    if kind == "TABLE":
        if following.value != "(" or following.kind != "symbol":
            return "unsupported"
        if any(_word(token, "SELECT") for token in tokens):
            return "unsupported"
        return "create_table"
    if kind == "INDEX" and _word(following, "ON"):
        return "create_index"
    if kind == "VIEW" and (_word(following, "AS") or following.value == "("):
        return "create_view"
    return "unsupported"


def _simple_column_addition(statement):
    tokens = _tokens(statement)
    if (
        any(token.kind == "invalid" for token in tokens)
        or len(split_sql_statements(statement)) != 1 or sql_review_findings(statement, LEDGER)
    ):
        return False
    if tokens and tokens[-1].kind == "symbol" and tokens[-1].value == ";":
        tokens = tokens[:-1]
    if (
        len(tokens) < 6 or any(token.depth != 0 for token in tokens)
        or not _word(tokens[0], "ALTER") or not _word(tokens[1], "TABLE")
        or not _identifier(tokens[2]) or not _word(tokens[3], "ADD")
    ):
        return False
    index = 5 if _word(tokens[4], "COLUMN") else 4
    if (
        len(tokens) <= index + 1 or not _identifier(tokens[index])
        or tokens[index + 1].kind != "word"
        or tokens[index + 1].value not in {"TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC"}
    ):
        return False
    index += 2
    if len(tokens) > index and _word(tokens[index], "NOT"):
        if len(tokens) <= index + 1 or not _word(tokens[index + 1], "NULL"):
            return False
        index += 2
    if index == len(tokens):
        return True
    if not _word(tokens[index], "DEFAULT"):
        return False
    value = tokens[index + 1:]
    if len(value) == 1 and (value[0].kind == "literal" or _word(value[0], "NULL")):
        return True
    if not value or any(
        current.line != previous.line or current.column != previous.column + len(previous.value)
        for previous, current in zip(value, value[1:])
    ):
        return False
    if len(value) == 2 and _word(value[0], "X") and value[1].kind == "literal":
        return re.fullmatch(r"(?:[a-fA-F0-9]{2})*", value[1].value) is not None
    return (
        all(token.kind in {"word", "symbol"} for token in value)
        and re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:E[+-]?[0-9]+)?", "".join(token.value for token in value)) is not None
    )


def _updated_pending(pending, schema):
    result = {
        key: pending[key] for key in (
            "protocol_version", "name", "sha256", "operation_id", "revision_id",
            "phase", "effect_outcome", "statement_count",
        )
    }
    result["sql_result"] = {
        key: value for key, value in pending["sql_result"].items()
        if key in {"confirmed_statements", "changes", "rows_written", "rows_read"}
        and type(value) in {int, float} and 0 <= value <= 10 ** 15
    }
    result["schema_after"] = schema
    provider = sanitize_provider_diagnostic(pending.get("provider"))
    if provider:
        result["provider"] = provider
    code = pending.get("code")
    if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
        result["code"] = code
    return result


def assess_migration_recovery(
    gateway, database_id, files, inventory, *, operation_id, revision_id, operator_evidence=None,
):
    report = {
        "version": 1,
        "policy_version": POLICY_VERSION,
        "identity": {},
        "checks": {},
        "historical_provider_result_verified": False,
        "operator_evidence_verified": False,
    }
    if not all(_safe_identity(value) for value in (database_id, operation_id, revision_id)):
        return _finish(report, "conflict", missing=["operation_identity"])
    report["identity"] = {"operation_id": operation_id, "revision_id": revision_id, "database_id": database_id}
    pending = inventory.get("migration_in_progress") if isinstance(inventory, dict) else None
    identity = migration_identity(pending)
    if identity is None:
        return _finish(report, "unknown", missing=["pending_migration_identity"])
    report["identity"]["migration"] = identity
    if inventory.get("database_id") not in (None, database_id):
        return _finish(report, "conflict", missing=["database_identity"])
    if any(pending.get(key) not in (None, value) for key, value in (
        ("operation_id", operation_id), ("revision_id", revision_id),
    )):
        return _finish(report, "conflict", missing=["operation_identity"])
    report["checks"]["identity"] = "matched"
    if not isinstance(files, dict) or any(not isinstance(path, str) or not isinstance(sql, str) for path, sql in files.items()):
        return _finish(report, "unknown", missing=["immutable_migration_source"])
    migrations = {path: sql for path, sql in sorted(files.items()) if path.startswith("migrations/") and path.endswith(".sql")}
    if len(migrations) > MAX_MIGRATIONS or sum(len(sql.encode()) for sql in migrations.values()) > 4_000_000:
        return _finish(report, "unknown", missing=["source_limits"])
    manifest = {path: sha256(sql.encode()).hexdigest() for path, sql in migrations.items()}
    if any(migration_identity({"name": path, "sha256": digest}) is None for path, digest in manifest.items()):
        return _finish(report, "conflict", missing=["immutable_migration_source"])
    name, digest = identity["name"], identity["sha256"]
    if name not in migrations:
        return _finish(report, "conflict", missing=["immutable_migration_source"])
    if manifest[name] != digest:
        return _finish(report, "conflict", missing=["matching_migration_hash"])
    report.update(artifact_sha256=canonical_hash(files), source_manifest_sha256=canonical_hash(manifest))
    report["checks"]["source"] = "matched"
    statements = split_sql_statements(migrations[name])
    if not statements or len(statements) > MAX_STATEMENTS:
        return _finish(report, "unknown", missing=["source_limits"])
    report["statement_count"] = len(statements)
    report["statement_manifest"] = [
        {"index": index, "sha256": sha256(statement.encode()).hexdigest(), "kind": _structural_statement(statement)}
        for index, statement in enumerate(statements, 1)
    ]
    try:
        local = confirmed_migrations(inventory)
    except RuleError:
        return _finish(report, "conflict", missing=["consistent_local_history"])
    if any(manifest.get(path) != recorded or path > name for path, recorded in local.items()):
        return _finish(report, "conflict", missing=["consistent_local_history"])
    try:
        remote = _remote_receipts(gateway, database_id)
    except RuleError as exc:
        provider = sanitize_provider_diagnostic(getattr(exc, "provider_diagnostic", None))
        if provider.get("reason") == "missing_table":
            return _finish(report, "unknown", missing=["remote_ledger"])
        raise
    if remote is None:
        return _finish(report, "conflict", missing=["valid_remote_history"])
    report["remote_history_sha256"] = canonical_hash(remote)
    if any(manifest.get(path) != recorded or path > name for path, recorded in remote.items()):
        return _finish(report, "conflict", missing=["matching_remote_history"])
    if any(remote.get(path) != recorded for path, recorded in local.items()):
        return _finish(report, "conflict", missing=["matching_remote_history"])
    prefix = {path: sql for path, sql in migrations.items() if path < name}
    if any(remote.get(path) != manifest[path] for path in prefix):
        return _finish(report, "conflict", missing=["complete_history_prefix"])
    report["checks"]["history"] = "matched"
    if remote.get(name) == digest:
        report["checks"]["receipt"] = "confirmed"
        return _finish(report, "confirmed", "remote_receipt")
    report["checks"]["receipt"] = "absent"
    if pending.get("phase") == "receipt_confirmed":
        return _finish(report, "unknown", missing=["previously_confirmed_receipt"])
    bound = pending.get("operation_id") == operation_id and pending.get("revision_id") == revision_id
    phase, effect = pending.get("phase"), pending.get("effect_outcome")
    protocol_v2 = type(pending.get("protocol_version")) is int and pending["protocol_version"] == 2
    if protocol_v2 and bound and (
        (phase == "prepared" and effect == "not_sent")
        or (phase == "sql_rejected" and effect in ("none", "not_sent"))
    ):
        report["checks"]["execution"] = "durable_no_effect"
        return _finish(report, "no_effect", "durable_no_effect")
    sql_result = pending.get("sql_result")
    durable = (
        protocol_v2 and bound and phase in ("sql_confirmed", "receipt_sending")
        and effect == "confirmed" and isinstance(sql_result, dict)
        and type(pending.get("statement_count")) is int and pending["statement_count"] == len(statements)
        and type(sql_result.get("confirmed_statements")) is int
        and sql_result["confirmed_statements"] == len(statements)
    )
    report["checks"]["execution"] = "durable_confirmed" if durable else "unconfirmed"
    try:
        baseline = validate_migration_plan(prefix, baseline_files=prefix, revision_id=revision_id)
        history = {**prefix, name: migrations[name]}
        expected = validate_migration_plan(history, baseline_files=history, revision_id=revision_id)
    except RuleError:
        return _finish(report, "unknown", missing=["reconstructable_schema"])
    actual = migration_schema_fingerprint(gateway, database_id)
    report["schema"] = {
        "baseline_sha256": baseline["schema_sha256"],
        "expected_sha256": expected["schema_sha256"],
        "observed_sha256": actual["sha256"],
        "observed_object_count": actual["object_count"],
    }
    matches_expected = actual["sha256"] == expected["schema_sha256"]
    matches_baseline = actual["sha256"] == baseline["schema_sha256"]
    report["checks"]["schema"] = "matches_expected" if matches_expected else "matches_baseline" if matches_baseline else "mismatch"
    saved = pending.get("schema_after")
    if saved is not None and saved != actual:
        return _finish(report, "unknown", missing=["matching_saved_schema"])
    if durable:
        if not matches_expected:
            return _finish(report, "unknown", missing=["matching_remote_schema"])
        return _finish(
            report, "confirmed", "durable_execution_reconstructed_schema",
            pending_update=_updated_pending(pending, actual),
        )
    if (
        matches_baseline and not matches_expected and len(statements) == 1
        and report["statement_manifest"][0]["kind"] == "security_sensitive"
        and _simple_column_addition(statements[0])
    ):
        return _finish(report, "schema_unchanged", "schema_postconditions", missing=["operator_structural_attestation"])
    supported = all(item["kind"] not in {"unsupported", "security_sensitive"} for item in report["statement_manifest"])
    if not supported:
        return _finish(report, "unknown", missing=["complete_execution_result", "supported_structural_statements"])
    if matches_expected:
        return _finish(report, "schema_applied", "schema_postconditions", missing=["operator_structural_attestation"])
    if matches_baseline:
        return _finish(report, "schema_unchanged", "schema_postconditions", missing=["operator_structural_attestation"])
    return _finish(report, "unknown", missing=["matching_remote_schema", "authenticated_execution_evidence"])
