import re
import unicodedata
from pathlib import PurePosixPath

from viracase.infrastructure.publication_logging import safe_text

MIGRATION_PHASES = {
    "prepared", "sql_sending", "sql_confirmed", "receipt_sending", "receipt_confirmed", "sql_rejected",
}
EFFECT_OUTCOMES = {"not_sent", "unknown", "confirmed", "none"}


def migration_identity(value):
    if not isinstance(value, dict):
        return None
    name, digest = value.get("name"), value.get("sha256")
    if (
        not isinstance(name, str) or len(name) > 180 or not name.startswith("migrations/")
        or not name.endswith(".sql") or "\\" in name or ":" in name
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or "/".join(PurePosixPath(name).parts) != name
        or any(unicodedata.category(char) in {"Cc", "Cf"} for char in name)
        or safe_text(name, limit=181) != name
        or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        return None
    return {"name": name, "sha256": digest}


def migration_needs_reconciliation(pending):
    if not pending:
        return False
    if not isinstance(pending, dict) or not migration_identity(pending):
        return True
    if pending.get("protocol_version") != 2:
        return True
    phase, effect = pending.get("phase"), pending.get("effect_outcome")
    if not isinstance(phase, str) or not isinstance(effect, str):
        return True
    return (phase, effect) not in {
        ("prepared", "not_sent"), ("sql_rejected", "none"), ("sql_rejected", "not_sent"),
    }


def _error_summary(value):
    from viracase.infrastructure.publication_d1_diagnostics import sanitize_provider_diagnostic

    if not isinstance(value, dict):
        return None
    code = value.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
        return None
    result = {"code": code, "message": safe_text(value.get("message"), limit=500)}
    provider = sanitize_provider_diagnostic(value.get("provider"))
    if provider:
        result["provider"] = provider
    return result


def publication_diagnostic(result, inventory=None, *, stage=None, failure=None, operation_id=None, revision_id=None):
    previous = result.get("publication_diagnostic")
    previous = previous if isinstance(previous, dict) and previous.get("version") == 1 else {}
    diagnostic = {"version": 1}
    for field, allowed in (("phase", MIGRATION_PHASES), ("effect_status", EFFECT_OUTCOMES)):
        value = previous.get(field)
        if isinstance(value, str) and (value in allowed or field == "phase" and re.fullmatch(r"publication_[a-z_]{1,50}", value)):
            diagnostic[field] = value
    original_error = _error_summary(previous.get("initial_error"))
    if original_error:
        diagnostic["initial_error"] = original_error
    pending = (inventory or {}).get("migration_in_progress")
    latest = pending or (inventory or {}).get("migration_last_attempt")
    if not isinstance(latest, dict):
        latest = {}
    if not pending and (
        operation_id and latest.get("operation_id") != operation_id
        or revision_id and latest.get("revision_id") != revision_id
    ):
        latest = {}
    identity = migration_identity(latest)
    original_failure = result.get("publication_failure") or failure
    terminal_migration_failure = bool(
        stage == "publication_failed"
        and isinstance(original_failure, dict) and original_failure.get("stage") == "publication_migrating"
    )
    migration_stage = pending or stage == "publication_migrating" or terminal_migration_failure
    if identity:
        diagnostic["migration"] = identity
    phase = latest.get("phase")
    if isinstance(phase, str) and phase in MIGRATION_PHASES and migration_stage:
        diagnostic["phase"] = phase
    elif stage:
        diagnostic["phase"] = stage
    else:
        diagnostic.setdefault("phase", "unknown")
    effect = latest.get("effect_outcome")
    if isinstance(effect, str) and effect in EFFECT_OUTCOMES and migration_stage:
        diagnostic["effect_status"] = effect
    else:
        diagnostic.setdefault("effect_status", "unknown" if result.get("external_effect_pending") else "none")
    original = _error_summary(result.get("publication_failure") or failure)
    if not diagnostic.get("initial_error") and original:
        diagnostic["initial_error"] = original
    if stage and not migration_stage:
        diagnostic.pop("migration", None)
    diagnostic["detail_available"] = bool(
        identity and latest.get("protocol_version") == 2
        or diagnostic.get("initial_error", {}).get("provider", {}).get("message")
        or diagnostic.get("initial_error", {}).get("provider", {}).get("reason")
    )
    return diagnostic


def recovery_guidance(code, pending=None, *, exhausted=False):
    identity = migration_identity(pending)
    resource = identity["name"] if identity else "a migration desta tentativa"
    guidance = {
        "reason": "remote_observation_pending",
        "owner": "platform",
        "required_action": "Aguarde a próxima conferência automática. Atualizar status apenas consulta o acompanhamento.",
        "missing_evidence": ["Confirmação dos recursos remotos e da revisão publicada."],
    }
    if code == "MIGRATION_RECEIPT_PENDING":
        guidance.update(
            reason="sql_confirmed_receipt_pending", owner="operator",
            required_action=(
                f"O SQL de {resource} foi confirmado, mas falta validar seu recibo. "
                "O responsável deve conferir a evidência de execução e o schema; não executar novamente a migration."
            ),
            missing_evidence=["Recibo com o nome e hash esperados.", "Schema remoto compatível com a execução confirmada."],
        )
    elif code in {"MIGRATION_RESULT_UNKNOWN", "MIGRATION_RECEIPT_CONFLICT"}:
        guidance.update(
            reason="migration_result_unconfirmed", owner="operator",
            required_action=(
                f"O responsável deve conferir o recibo e o resultado de {resource} no banco autorizado. "
                "Sem essa evidência, uma nova execução pode duplicar efeitos."
            ),
            missing_evidence=["Recibo remoto com o nome e hash esperados ou prova verificável de ausência de efeitos."],
        )
    elif code in {"MIGRATION_CHECKSUM_CONFLICT", "MIGRATION_BASELINE_INCONSISTENT", "MIGRATION_BASELINE_UNVERIFIABLE"}:
        guidance.update(
            reason="migration_history_conflict", owner="operator",
            required_action="Confira a divergência entre os arquivos protegidos, o inventário e o recibo remoto. Não substitua hashes históricos.",
            missing_evidence=["Histórico de migrations consistente com os recibos confirmados."],
        )
    elif code in {"MIGRATION_REVIEW_REQUIRED", "MIGRATION_VALIDATION_FAILED", "MIGRATION_HISTORY_IMMUTABLE", "MIGRATION_ORDER_INVALID"}:
        guidance.update(
            reason="migration_plan_invalid", owner="user",
            required_action="Confira o diagnóstico no editor e corrija a migration nova, preservando o histórico. A análise de código com IA é opcional.",
            missing_evidence=["Plano incremental válido para a revisão selecionada."],
        )
    elif code in {"CONFIGURATION_INVALID", "FORBIDDEN", "PUBLICATION_ACCESS_DENIED"}:
        guidance.update(
            reason="access_confirmation_required", owner="operator",
            required_action="O responsável pela hospedagem deve conferir a autorização da conexão ou do recurso indicado no diagnóstico.",
            missing_evidence=["Acesso autorizado ao recurso que precisa ser conferido."],
        )
    elif code in {"RESOURCE_CONFLICT", "PUBLICATION_BINDINGS_MISMATCH", "PUBLICATION_REDIRECTED"}:
        guidance.update(
            reason="remote_resource_mismatch", owner="operator",
            required_action="O responsável deve comparar os recursos remotos, os bindings e o destino esperado desta revisão.",
            missing_evidence=["Identidade e configuração dos recursos compatíveis com a publicação."],
        )
    elif exhausted or code == "PUBLICATION_RECOVERY_EXHAUSTED":
        guidance.update(
            reason="automatic_observation_exhausted", owner="operator",
            required_action="O limite de conferências foi atingido. O responsável deve verificar os recursos e usar a reconciliação existente após resolver o impedimento.",
        )
    if identity:
        guidance["migration"] = identity
    return guidance
