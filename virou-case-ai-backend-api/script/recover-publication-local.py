import argparse
import getpass
import json
import logging
import re
import sys
import unicodedata
import warnings
from copy import deepcopy
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from viracase.domain.rules import RuleError, canonical_hash
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway
from viracase.infrastructure.composition import compose
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Operation
from viracase.infrastructure.publication import PublicationService
from viracase.infrastructure.publication_logging import safe_text
from viracase.infrastructure.publication_resolution import (
    prepare_recovery_plan,
    recovery_snapshot,
    resolve_recovery_plan,
)

LOCAL_INTENTION_NAMESPACE = "viroucaseai:publication-recovery-local:v1"
ERROR_MESSAGES = {
    "UNAUTHENTICATED": "As credenciais administrativas não foram aceitas ou a sessão expirou.",
    "FORBIDDEN": "A conta não está autorizada para esta ação ou não preparou este plano.",
    "NOT_FOUND": "A publicação solicitada não está disponível para esta conta.",
    "RECOVERY_NOT_PENDING": "A publicação não está aguardando recuperação.",
    "RECOVERY_PLAN_STALE": "A observação, a revisão ou a conexão mudou. A resolução não foi confirmada; confira novamente a operação.",
    "RECOVERY_PLAN_EXPIRED": "A observação expirou. Execute novamente o comando para conferir o estado atual.",
    "RECOVERY_ATTESTATION_REQUIRED": "A evidência estrutural exige atestação administrativa integral.",
    "RECOVERY_ATTESTATION_INVALID": "A atestação não corresponde à observação preservada.",
    "RECOVERY_IN_PROGRESS": "Existe uma intenção em andamento. Retome o fluxo e o administrador que a iniciaram.",
    "OPERATION_CONFLICT": "Uma execução já está conferindo esta publicação. Aguarde sua conclusão e consulte novamente.",
    "LEASE_LOST": "Outra execução assumiu a operação. Consulte novamente o estado preservado.",
    "IDEMPOTENCY_CONFLICT": "A intenção existente identifica outro conteúdo e foi preservada.",
    "INTENTION_EXPIRED": "A intenção ultrapassou o prazo de retenção. A operação precisa de conferência administrativa.",
    "CONFIGURATION_INVALID": "A conexão autorizada precisa ser validada pelo responsável no backoffice.",
    "RESOURCE_CONFLICT": "O inventário remoto não corresponde ao destino autorizado.",
    "LIFECYCLE_LOCKED": "A recuperação aguarda o término da manutenção ou a regularização do acesso.",
    "ACCESS_EXPIRED": "O acesso do proprietário não permite concluir esta ação.",
    "RETENTION_EXPIRED": "O acesso aos arquivos deste projeto não está disponível.",
    "MIGRATION_RESULT_UNKNOWN": "A migration ainda não possui evidência suficiente para resolver a pendência.",
    "MIGRATION_CHECKSUM_CONFLICT": "O histórico ou o recibo diverge da migration preservada.",
    "VALIDATION_ERROR": "O serviço recusou o formato ou os limites da solicitação.",
    "RATE_LIMITED": "O limite de tentativas foi atingido. Aguarde antes de tentar novamente.",
}
CHECK_FIELDS = {key: "text" for key in ("identity", "source", "history", "receipt", "execution", "schema")}
MIGRATION_FIELDS = {"name": "text", "sha256": "hash"}
IDENTITY_FIELDS = {
    "operation_id": "text", "revision_id": "text", "database_id": "text",
    "migration": MIGRATION_FIELDS,
}
ASSESSMENT_FIELDS = {
    "version": "int", "policy_version": "text", "identity": IDENTITY_FIELDS,
    "checks": CHECK_FIELDS, "historical_provider_result_verified": "bool",
    "operator_evidence_verified": "bool", "artifact_sha256": "hash",
    "source_manifest_sha256": "hash", "statement_count": "int",
    "statement_manifest": [{"index": "int", "sha256": "hash", "kind": "text"}],
    "remote_history_sha256": "hash",
    "schema": {
        "baseline_sha256": "hash", "expected_sha256": "hash", "observed_sha256": "hash",
        "observed_object_count": "int",
    },
    "classification": "text", "origin": "text", "action": "text", "requires_operator": "bool",
    "missing_evidence": ["text"], "required_action": "text", "missing_evidence_messages": ["text"],
    "evidence_sha256": "hash",
}
PLAN_FIELDS = {
    "format_version": "int", "plan_id": "text", "operation_id": "text", "revision_id": "text",
    "prepared_by": "text", "created_at": "text", "expires_at": "text",
    "binding": {
        "operation_id": "text", "account_id": "text", "project_id": "text", "revision_id": "text",
        "source_hash": "hash", "database_id": "text", "remote_account_id": "text", "worker_name": "worker",
        "configuration_id": "text", "configuration_version": "int", "configuration_sha256": "hash",
        "pending_sha256": "hash", "history_sha256": "hash", "upload_started": "bool",
    },
    "files_sha256": "hash", "assessment": ASSESSMENT_FIELDS,
    "action": "text", "can_resolve": "bool", "reason": "text", "resulting_behavior": "text",
    "blocked_reason": "text", "plan_sha256": "hash",
}
SNAPSHOT_FIELDS = {
    "operation_id": "text", "project_id": "text", "revision_id": "text", "operation_state": "text",
    "recovery_state": "text", "classification": "text", "summary": "text", "can_check": "bool",
    "blocked_code": "text", "blocked_reason": "text", "operator_required": "bool",
    "required_action": "text", "missing_evidence": ["text"], "checks": CHECK_FIELDS,
    "migration": {**MIGRATION_FIELDS, "phase": "text", "effect_outcome": "text", "statement_count": "int"},
}
ATTESTATION_FIELDS = {
    "reason": "text",
    "attestation": {
        "reference": "text", "evidence_sha256": "hash", "statement_indices": ["int"],
        "observed_outcome": "text",
    },
}


class CliError(Exception):
    def __init__(self, code, message, exit_code=2):
        self.code, self.message, self.exit_code = code, message, exit_code
        super().__init__(message)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError("ARGUMENTS_INVALID", "Argumentos inválidos ou incompletos. Consulte --help; a senha é solicitada interativamente.")


def _uuid(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value) is None:
        raise CliError("IDENTITY_INVALID", "Informe o UUID completo da operação de publicação.")
    return str(UUID(value))


def _text(value, minimum, maximum, label):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise CliError("INPUT_INVALID", f"{label}: informe entre {minimum} e {maximum} caracteres.")
    result = value.strip()
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in result):
        raise CliError("INPUT_INVALID", f"{label}: caracteres de controle não são permitidos.")
    return result


def _safe_text(value, secrets):
    if not isinstance(value, str):
        return ""
    if re.search(r"(?i)\b(?:SELECT\s|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)", value):
        return "[Conteúdo SQL omitido.]"
    if re.search(r"(?i)\b(?:postgres(?:ql)?(?:\+[a-z]+)?|mysql(?:\+[a-z]+)?|sqlite)://", value):
        return "[Endereço de banco omitido.]"
    clean = safe_text(value, secrets=secrets, limit=1000)
    return "".join(char for char in clean if unicodedata.category(char) not in {"Cc", "Cf"})


def _project(value, fields, secrets):
    if value is None:
        return None
    if isinstance(fields, dict):
        if not isinstance(value, dict):
            return {}
        return {key: _project(value[key], spec, secrets) for key, spec in fields.items() if key in value}
    if isinstance(fields, list):
        return [_project(item, fields[0], secrets) for item in value[:2000]] if isinstance(value, list) else []
    if fields == "bool":
        return value if type(value) is bool else None
    if fields == "int":
        return value if type(value) is int and 0 <= value <= 1_000_000 else None
    if fields == "hash":
        return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) and not any(secret and secret in value for secret in secrets) else "[Hash indisponível.]"
    if fields == "worker":
        return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{1,17}-[a-f0-9]{32}", value) and not any(secret and secret in value for secret in secrets) else "[Identidade indisponível.]"
    return _safe_text(value, secrets)


def _emit(stage, **fields):
    print(json.dumps({"stage": stage, **fields}, ensure_ascii=False, indent=2), flush=True)


def _error(code, message, secrets):
    if not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) is None or any(secret and secret in code for secret in secrets):
        code = "RECOVERY_FAILED"
    print(json.dumps({"error": {"code": code, "message": _safe_text(message, secrets)}}, ensure_ascii=False), file=sys.stderr, flush=True)


def _known_secrets(config, repo):
    result = list(getattr(repo.sources, "secrets", ()))
    for name in ("database_url", "migration_database_url", "openai_api_key", "gemini_api_key", "app_db_password", "postgres_password", "admin_password"):
        value = getattr(config, name, "")
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        if isinstance(value, str) and value:
            result.append(value)
    return [value for value in result if isinstance(value, str) and value]


def _operation_context(repo, actor, oid):
    with repo.sessions() as db:
        Integrations(repo).require_admin(repo.account(db, actor))
        operation = db.get(Operation, oid)
        if not operation or operation.type != "publication":
            raise RuleError("NOT_FOUND", "Publicação indisponível.", 404)
        repo.owned_project(db, operation.account_id, operation.project_id)
        result = operation.result if isinstance(operation.result, dict) else {}
        return {
            "owner": operation.account_id, "project_id": operation.project_id, "state": operation.state,
            "resolution": deepcopy(result.get("publication_resolution") or {}),
        }


def _resolution_key(actor, oid, payload):
    return str(uuid5(NAMESPACE_URL, f"{LOCAL_INTENTION_NAMESPACE}:{actor}:{oid}:{canonical_hash(payload)}"))


def _validate_plan(plan, actor, oid):
    if (
        not isinstance(plan, dict) or plan.get("operation_id") != oid or plan.get("prepared_by") != actor
        or plan.get("plan_sha256") != canonical_hash({key: value for key, value in plan.items() if key != "plan_sha256"})
    ):
        raise CliError("PLAN_INVALID", "O plano não corresponde à operação, ao administrador ou ao hash preservado.", 3)


def _eligible(plan):
    assessment = plan.get("assessment")
    return (
        isinstance(assessment, dict) and assessment.get("classification") == "schema_unchanged"
        and plan.get("action") == "close_no_effect" and plan.get("can_resolve") is True
        and type(assessment.get("statement_count")) is int and 1 <= assessment["statement_count"] <= 2000
    )


def _resume_payload(actor, oid, plan, attempt):
    if attempt.get("actor_id") != actor:
        raise CliError("RECOVERY_IN_PROGRESS", "A resolução em andamento pertence a outro administrador. Retome o fluxo original.")
    payload = {key: attempt.get(key) for key in ("plan_id", "plan_sha256", "action", "reason", "attestation")}
    key = _resolution_key(actor, oid, payload)
    attestation = payload.get("attestation")
    if (
        not _eligible(plan) or payload["plan_id"] != plan.get("plan_id") or payload["plan_sha256"] != plan.get("plan_sha256")
        or payload["action"] != "close_no_effect" or attempt.get("payload_hash") != canonical_hash(payload)
        or attempt.get("intention_id") != canonical_hash([actor, "publication:recovery:resolve:" + oid, key])
        or not isinstance(attestation, dict) or attestation.get("evidence_sha256") != canonical_hash(plan)
        or attestation.get("observed_outcome") != "schema_unchanged"
        or attestation.get("statement_indices") != list(range(1, plan["assessment"]["statement_count"] + 1))
    ):
        raise CliError("RECOVERY_IN_PROGRESS", "A intenção em andamento não corresponde a uma atestação deste assistente local. Preserve-a e retome o fluxo original.")
    return key, payload


def _show_plan(plan, secrets, *, resuming=False):
    displayed = _project(plan, PLAN_FIELDS, secrets)
    _emit("plan", resuming=resuming, plan=displayed, blocked_reason=displayed.get("blocked_reason"), plan_evidence_sha256=_project(canonical_hash(plan), "hash", secrets))
    if _eligible(plan) and displayed != plan:
        raise CliError("PLAN_UNSAFE", "O plano contém campos fora do contrato seguro de exibição. A atestação foi bloqueada; preserve a operação para análise.", 3)


def _show_attestation(payload, secrets):
    original = {"reason": payload.get("reason"), "attestation": payload.get("attestation")}
    displayed = _project(original, ATTESTATION_FIELDS, secrets)
    _emit("attestation", **displayed)
    if displayed != original:
        raise CliError("ATTESTATION_UNSAFE", "A atestação contém campos fora do contrato seguro de exibição. Preserve a intenção original para análise.", 3)


def _report_readiness(repo, authorize, context, publication_factory, secrets):
    print("A pendência está encerrada. Conferindo a disponibilidade da revisão atual.", flush=True)
    authorize()
    readiness = publication_factory(repo).readiness(context["owner"], context["project_id"])
    if not isinstance(readiness, dict) or type(readiness.get("can_publish")) is not bool:
        raise CliError("READINESS_INVALID", "A pendência foi encerrada, mas a disponibilidade atual não pôde ser confirmada.", 3)
    _emit(
        "readiness", recovery_completed=True,
        current_revision_id=_project(readiness.get("revision_id"), "text", secrets),
        can_publish=readiness["can_publish"],
        reasons=_project(readiness.get("reasons", []), [{"code": "text", "message": "text"}], secrets),
    )
    if readiness["can_publish"]:
        print("Pendência encerrada. A revisão atual está disponível para uma nova publicação pela plataforma.", flush=True)
        return 0
    print("Pendência encerrada. A publicação da revisão atual depende dos impedimentos listados acima.", flush=True)
    return 4


def _run(args, repo, token, actor, secrets, gateway_factory, publication_factory):
    def authorize():
        authenticated = repo.authenticate(token, "admin")
        if authenticated.get("id") != actor or authenticated.get("role") != "admin":
            raise RuleError("FORBIDDEN", "A sessão administrativa não corresponde ao operador.", 403)
        return actor

    def gateway(policy, credential):
        if isinstance(credential, str) and credential:
            secrets.append(credential)
        return gateway_factory(policy, credential)

    def prepare_gateway(policy, credential):
        claimed = _operation_context(repo, authorize(), args.operation_id)
        if (claimed["resolution"].get("attempt") or {}).get("state") == "applying":
            raise CliError("RECOVERY_IN_PROGRESS", "Uma intenção de resolução começou durante a conferência. Execute novamente para consultar ou retomar a intenção preservada.")
        return gateway(policy, credential)

    context = _operation_context(repo, authorize(), args.operation_id)
    snapshot = recovery_snapshot(repo, authorize(), args.operation_id, admin=True)
    _emit("snapshot", snapshot=_project(snapshot, SNAPSHOT_FIELDS, secrets))
    if snapshot.get("classification") == "resolved" and snapshot.get("recovery_state") == "resolved":
        return _report_readiness(repo, authorize, context, publication_factory, secrets)
    if context["state"] != "reconciling":
        raise CliError("RECOVERY_NOT_PENDING", "A operação não está aguardando a recuperação atendida por este comando.")
    resolution = context["resolution"]
    attempt = resolution.get("attempt") or {}
    resumed = attempt.get("state") == "applying"
    if resumed:
        plan = resolution.get("plan")
        _validate_plan(plan, actor, args.operation_id)
        key, payload = _resume_payload(actor, args.operation_id, plan, attempt)
        _show_plan(plan, secrets, resuming=True)
        _show_attestation(payload, secrets)
        if args.inspect:
            print("Inspeção concluída. A atestação já preservada poderá ser retomada pelo mesmo comando sem --inspect.", flush=True)
            return 2
        print("Retomando a mesma intenção e a atestação administrativa já preservadas para este plano.", flush=True)
    else:
        prepare_reason = args.reason or f"Conferência administrativa local da pendência {args.operation_id}."
        if _safe_text(prepare_reason, secrets) != prepare_reason:
            raise CliError("REASON_UNSAFE", "A justificativa contém conteúdo que não pode constar do registro seguro. Informe somente a razão operacional.")
        plan = prepare_recovery_plan(repo, authorize(), args.operation_id, str(uuid4()), prepare_reason, gateway_factory=prepare_gateway)
        _validate_plan(plan, actor, args.operation_id)
        _show_plan(plan, secrets)
        if not _eligible(plan):
            raise CliError("RECOVERY_NOT_ELIGIBLE", "Este comando só encerra uma observação schema_unchanged com close_no_effect e can_resolve=true. Siga o impedimento e a ação indicados no plano.")
        if args.inspect:
            print("Inspeção concluída. Execute o mesmo comando sem --inspect para obter e atestar uma conferência atualizada.", flush=True)
            return 2
        reason = args.reason or f"Atestação administrativa da observação estrutural do plano {plan['plan_id']}; encerramento da tentativa {args.operation_id}."
        reference = f"Recuperação local da operação {args.operation_id}; plano {plan['plan_id']}; observação estrutural integral schema_unchanged."
        if _safe_text(reason, secrets) != reason or _safe_text(reference, secrets) != reference:
            raise CliError("ATTESTATION_UNSAFE", "A referência da atestação não pode ser preservada sem alterações na saída segura.", 3)
        payload = {
            "plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"], "action": "close_no_effect",
            "reason": reason,
            "attestation": {
                "reference": reference, "evidence_sha256": canonical_hash(plan),
                "statement_indices": list(range(1, plan["assessment"]["statement_count"] + 1)),
                "observed_outcome": "schema_unchanged",
            },
        }
        print("A atestação confirma a observação estrutural integral exibida, vinculada ao hash do plano. Ela não declara execução histórica do SQL. A ação encerra esta tentativa sem registrar a migration como aplicada.", flush=True)
        _show_attestation(payload, secrets)
        confirmation = input(f"Digite ENCERRAR {args.operation_id} para atestar esta observação e encerrar a tentativa: ")
        authorize()
        if confirmation != f"ENCERRAR {args.operation_id}":
            raise CliError("CANCELLED", "Confirmação não recebida. A tentativa permanece pendente.")
        key = _resolution_key(actor, args.operation_id, payload)
    resolve_recovery_plan(repo, authorize(), args.operation_id, key, payload, gateway_factory=gateway)
    snapshot = recovery_snapshot(repo, authorize(), args.operation_id, admin=True)
    _emit("snapshot", snapshot=_project(snapshot, SNAPSHOT_FIELDS, secrets))
    if snapshot.get("classification") != "resolved" or snapshot.get("recovery_state") != "resolved":
        raise CliError("RESOLUTION_UNCONFIRMED", "O encerramento ainda não foi confirmado. Execute novamente o mesmo comando para consultar ou retomar a intenção preservada.", 3)
    return _report_readiness(repo, authorize, context, publication_factory, secrets)


def main(argv=None, *, components_factory=compose, gateway_factory=CloudflareGateway, publication_factory=PublicationService):
    engine = repo = token = None
    secrets = []
    exit_code = 3
    publication_logger = logging.getLogger("viracase.publication")
    logging_disabled = publication_logger.disabled
    publication_logger.disabled = True
    try:
        parser = SafeParser(description="Conferir e encerrar administrativamente uma pendência schema_unchanged no backend instalado.", allow_abbrev=False)
        parser.add_argument("operation_id", help="UUID da operação de publicação pendente")
        parser.add_argument("--inspect", action="store_true", help="exibir a conferência sem atestar nem resolver")
        parser.add_argument("--login", help="login administrativo; solicitado quando omitido")
        parser.add_argument("--reason", help="justificativa operacional de 8 a 300 caracteres, sem credenciais")
        args = parser.parse_args(argv)
        args.operation_id = _uuid(args.operation_id)
        if args.login is not None:
            args.login = _text(args.login, 3, 254, "Login")
        if args.reason is not None:
            args.reason = _text(args.reason, 8, 300, "Justificativa")
        config, engine, repo, _, _ = components_factory()
        secrets.extend(_known_secrets(config, repo))
        login = args.login or _text(input("Login administrativo: "), 3, 254, "Login")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                password = getpass.getpass("Senha administrativa: ")
            except getpass.GetPassWarning:
                raise CliError("TTY_REQUIRED", "Execute em um terminal interativo para informar a senha sem exibi-la.") from None
        if not isinstance(password, str) or not password:
            raise CliError("PASSWORD_REQUIRED", "Informe a senha administrativa no terminal interativo.")
        secrets.append(password)
        token, session = repo.login(login, password, "admin", "127.0.0.1")
        secrets.extend(value for value in (token, session.get("csrf_token")) if isinstance(value, str) and value)
        authenticated = repo.authenticate(token, "admin")
        exit_code = _run(args, repo, token, authenticated["id"], secrets, gateway_factory, publication_factory)
    except CliError as exc:
        _error(exc.code, exc.message, secrets)
        exit_code = exc.exit_code
    except RuleError as exc:
        _error(exc.code, ERROR_MESSAGES.get(exc.code, "O serviço recusou a ação. A operação e sua evidência foram preservadas; confira o diagnóstico administrativo."), secrets)
        exit_code = 3
    except (EOFError, KeyboardInterrupt):
        _error("CANCELLED", "Entrada interrompida. Consulte o estado ao executar novamente; uma intenção já iniciada será preservada.", secrets)
        exit_code = 2
    except SystemExit as exc:
        exit_code = 0 if exc.code == 0 else 2
    except Exception:
        _error("RECOVERY_FAILED", "Não foi possível confirmar esta etapa. Execute novamente o mesmo comando para consultar ou retomar a intenção preservada. Consulte os logs administrativos para a causa técnica.", secrets)
        exit_code = 3
    finally:
        cleanup_failed = False
        if repo is not None and token is not None:
            try:
                repo.logout(token)
            except Exception:
                cleanup_failed = True
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            _error("CLEANUP_FAILED", "Não foi possível confirmar o encerramento da sessão administrativa ou da conexão local. Confira a sessão no backoffice; preserve o resultado da operação exibido acima.", secrets)
            exit_code = 3
        publication_logger.disabled = logging_disabled
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
