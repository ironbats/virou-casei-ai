import argparse
import getpass
import ipaddress
import json
import os
import re
import stat
import sys
import unicodedata
import warnings
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx

PREFIX = "/admin-api/v1"
MAX_JSON_BYTES = 2_000_000
CLASSIFICATIONS = {"unassessed", "confirmed", "no_effect", "schema_applied", "schema_unchanged", "unknown", "conflict", "resolved"}
PLAN_ACTIONS = {"confirmed": "record_receipt", "no_effect": "close_no_effect", "schema_applied": "adopt_schema", "schema_unchanged": "close_no_effect"}
ERROR_MESSAGES = {
    "UNAUTHENTICATED": "As credenciais administrativas não foram aceitas ou a sessão expirou.",
    "FORBIDDEN": "A conta não está autorizada para esta ação ou não preparou este plano.",
    "ORIGIN_DENIED": "A origem informada não está autorizada para a API administrativa.",
    "CSRF_DENIED": "A sessão administrativa não confirmou a proteção CSRF.",
    "NOT_FOUND": "A operação solicitada não está disponível para esta conta.",
    "RECOVERY_NOT_PENDING": "A operação não está aguardando recuperação.",
    "RECOVERY_PLAN_STALE": "O plano ou a evidência mudou. Inspecione a operação e prepare uma nova conferência.",
    "RECOVERY_PLAN_EXPIRED": "O plano expirou. Prepare uma nova conferência, preservando a intenção de uma resolução já iniciada.",
    "RECOVERY_ATTESTATION_REQUIRED": "O plano exige atestação da evidência estrutural e de todas as instruções.",
    "RECOVERY_ATTESTATION_INVALID": "A atestação não corresponde à evidência exigida por este plano.",
    "RECOVERY_IN_PROGRESS": "Retome a mesma chave de idempotência e o mesmo conteúdo da resolução já iniciada.",
    "OPERATION_CONFLICT": "Outra execução está conferindo esta operação.",
    "LEASE_LOST": "Outra execução assumiu a operação. Consulte seu estado antes de continuar.",
    "IDEMPOTENCY_CONFLICT": "A chave de idempotência já identifica outro conteúdo. Preserve a intenção original.",
    "INTENTION_EXPIRED": "A intenção ultrapassou o prazo de retenção. Inspecione a operação antes de preparar outra ação.",
    "CONFIGURATION_INVALID": "A conexão autorizada precisa ser validada pelo responsável.",
    "MIGRATION_RESULT_UNKNOWN": "O resultado da migration continua sem confirmação suficiente.",
    "MIGRATION_RECEIPT_PENDING": "A execução está confirmada, mas a confirmação do recibo ainda está pendente.",
    "MIGRATION_CHECKSUM_CONFLICT": "O histórico ou o recibo diverge da migration preservada.",
    "VALIDATION_ERROR": "O servidor recusou o formato ou os limites da solicitação.",
}


class CliError(Exception):
    def __init__(self, code, message, *, exit_code=2, status=None, request_id=None):
        self.code, self.message, self.exit_code = code, message, exit_code
        self.status, self.request_id = status, request_id
        super().__init__(message)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError("ARGUMENTS_INVALID", "Argumentos inválidos ou incompletos. Consulte --help. A senha é solicitada interativamente.")


def canonical_hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _uuid(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value) is None:
        raise CliError("IDENTITY_INVALID", "Informe um identificador UUID válido para a operação e o plano.")
    return str(UUID(value))


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _text(value, minimum, maximum, label):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise CliError("INPUT_INVALID", f"{label}: informe entre {minimum} e {maximum} caracteres.")
    result = value.strip()
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in result):
        raise CliError("INPUT_INVALID", f"{label}: caracteres de controle não são permitidos.")
    return result


def normalize_url(value, *, origin=False, allow_local_http=False):
    if not isinstance(value, str) or any(char.isspace() or unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise CliError("URL_INVALID", "Informe uma URL absoluta sem credenciais, parâmetros ou caracteres de controle.")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise CliError("URL_INVALID", "A URL ou a porta informada é inválida.") from None
    if (
        parsed.scheme not in {"https", "http"} or not host or parsed.username is not None
        or parsed.password is not None or parsed.query or parsed.fragment
        or parsed.path.rstrip("/") not in ({""} if origin else {"", PREFIX})
        or port is not None and not 1 <= port <= 65535
        or "%" in parsed.netloc or "\\" in parsed.netloc
    ):
        raise CliError("URL_INVALID", "Use o endereço HTTPS do serviço ou /admin-api/v1, sem credenciais nem parâmetros; Origin deve conter apenas a origem.")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.casefold() == "localhost"
        if not allow_local_http or not loopback:
            raise CliError("HTTPS_REQUIRED", "HTTP é permitido somente em loopback para desenvolvimento, com --allow-local-http. Use HTTPS em produção.")
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), "", "", ""))


def _safe_message(value, secrets=(), limit=500):
    if not isinstance(value, str):
        return ""
    for secret in sorted((item for item in secrets if isinstance(item, str) and item), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)\b(?:bearer|basic)\s+\S+", "[REDACTED]", value)
    value = re.sub(r"(?i)\b(?:password|token|cookie|secret|credential|authorization)\s*[:=]\s*\S+", "[REDACTED]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?", "[REDACTED]", value)
    if re.search(r"(?i)\b(?:SELECT\s|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)", value):
        return "[Conteúdo SQL omitido da saída.]"
    return "".join(char for char in value if unicodedata.category(char) not in {"Cc", "Cf"})[:limit]


def _json_object(raw):
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise CliError("JSON_INVALID", "O conteúdo recebido ou o arquivo de plano não contém JSON válido.") from None
    if not isinstance(value, dict):
        raise CliError("JSON_INVALID", "Era esperado um objeto JSON.")
    return value


class AdministrativeClient:
    def __init__(self, base_url, origin, *, timeout=60, transport=None):
        self.client = httpx.Client(
            base_url=base_url, headers={"Origin": origin, "Accept": "application/json"},
            timeout=timeout, follow_redirects=False, trust_env=False, transport=transport,
        )
        self.secrets = []
        self.authenticated = False

    def request(self, method, path, *, payload=None, intention=None):
        headers = {"Idempotency-Key": intention} if intention else {}
        try:
            with self.client.stream(method, PREFIX + path, json=payload, headers=headers) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_JSON_BYTES:
                        raise CliError("RESPONSE_TOO_LARGE", "A resposta excedeu o limite de leitura segura.", exit_code=3)
                status = response.status_code
                request_id = response.headers.get("X-Request-ID")
        except httpx.HTTPError:
            message = "Não foi possível confirmar a resposta HTTP."
            if intention:
                message += " Preserve esta chave de idempotência e o mesmo conteúdo antes de retomar; não crie outra intenção por causa do timeout."
            raise CliError("HTTP_RESULT_UNKNOWN", message, exit_code=3) from None
        if status == 204:
            return {}
        try:
            value = _json_object(body)
        except CliError:
            raise CliError("RESPONSE_INVALID", "O serviço retornou uma resposta inesperada; seu corpo foi omitido.", exit_code=3, status=status) from None
        if not 200 <= status < 300:
            error = value.get("error")
            error = error if isinstance(error, dict) else {}
            code = error.get("code")
            if not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) is None:
                code = "HTTP_REJECTED"
            if any(secret and secret in code for secret in self.secrets):
                code = "HTTP_REJECTED"
            request_id = error.get("request_id") or request_id
            try:
                request_id = _uuid(request_id) if request_id and request_id not in self.secrets else None
            except CliError:
                request_id = None
            raise CliError(
                code, ERROR_MESSAGES.get(code, "O servidor recusou a solicitação. Consulte a operação e o identificador da requisição."),
                exit_code=3, status=status, request_id=request_id,
            )
        return value

    def login(self, login, password):
        self.secrets.append(password)
        result = self.request("POST", "/auth/login", payload={"login": login, "password": password})
        csrf = result.get("csrf_token")
        try:
            cookie = self.client.cookies.get("vira_admin")
        except httpx.CookieConflict:
            cookie = None
        if (
            result.get("role") != "admin" or not isinstance(csrf, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,512}", csrf) is None
            or not isinstance(cookie, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,512}", cookie) is None
        ):
            raise CliError("ADMIN_SESSION_INVALID", "A resposta não estabeleceu uma sessão administrativa válida.", exit_code=3)
        self.secrets.extend([csrf, cookie])
        self.client.headers["X-CSRF-Token"] = csrf
        self.authenticated = True

    def close(self):
        try:
            if self.authenticated:
                self.client.timeout = httpx.Timeout(5)
                self.request("POST", "/auth/logout")
        except CliError:
            pass
        finally:
            self.client.close()


def _read_plan(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise CliError("PLAN_FILE_INVALID", "O plano deve ser um arquivo regular.")
            raw = handle.read(MAX_JSON_BYTES + 1)
    except OSError:
        raise CliError("PLAN_FILE_UNAVAILABLE", "Não foi possível ler o arquivo de plano informado.") from None
    if len(raw) > MAX_JSON_BYTES:
        raise CliError("PLAN_FILE_INVALID", "O arquivo de plano excede o limite permitido.")
    return _json_object(raw)


def validate_plan(plan, *, operation_id=None):
    if (
        not isinstance(plan, dict) or type(plan.get("format_version")) is not int
        or plan["format_version"] != 1 or not _digest(plan.get("plan_sha256"))
    ):
        raise CliError("PLAN_INVALID", "O arquivo não contém um plano de recuperação compatível.")
    if canonical_hash({key: value for key, value in plan.items() if key != "plan_sha256"}) != plan["plan_sha256"]:
        raise CliError("PLAN_HASH_INVALID", "O plano foi alterado ou está incompleto. Prepare um plano integral novamente.")
    oid = _uuid(plan.get("operation_id"))
    _uuid(plan.get("plan_id"))
    if operation_id is not None and oid != operation_id:
        raise CliError("PLAN_IDENTITY_INVALID", "O plano retornado não corresponde à operação solicitada.")
    assessment = plan.get("assessment")
    if (
        not isinstance(assessment, dict) or not isinstance(assessment.get("classification"), str)
        or assessment["classification"] not in CLASSIFICATIONS
    ):
        raise CliError("PLAN_INVALID", "O plano não apresenta uma classificação compatível.")
    if type(plan.get("can_resolve")) is not bool:
        raise CliError("PLAN_INVALID", "O plano não informa se a resolução está autorizada.")
    action = plan.get("action")
    if action is not None and (not isinstance(action, str) or action not in set(PLAN_ACTIONS.values())):
        raise CliError("PLAN_INVALID", "O plano apresenta uma ação não reconhecida.")
    if plan["can_resolve"] and (action is None or action != PLAN_ACTIONS.get(assessment["classification"])):
        raise CliError("PLAN_INVALID", "A ação do plano não corresponde à classificação técnica.")
    try:
        expires = datetime.fromisoformat(plan["expires_at"])
        if expires.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise CliError("PLAN_INVALID", "O plano não contém uma validade com fuso horário.") from None
    return expires


def _file_digest(path):
    result = sha256()
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size < 1:
                raise CliError("EVIDENCE_FILE_INVALID", "A evidência deve ser um arquivo regular.")
            remaining = before.st_size
            while remaining:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    raise CliError("EVIDENCE_FILE_CHANGED", "O arquivo de evidência mudou durante a leitura. Use uma cópia estável do registro revisado.")
                result.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(handle.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise CliError("EVIDENCE_FILE_CHANGED", "O arquivo de evidência mudou durante a leitura. Use uma cópia estável do registro revisado.")
    except OSError:
        raise CliError("EVIDENCE_FILE_UNAVAILABLE", "Não foi possível ler o arquivo local de evidência.") from None
    return result.hexdigest()


def resolution_payload(plan, reason, *, evidence=None, reference=None):
    validate_plan(plan)
    if plan["can_resolve"] is not True:
        raise CliError("PLAN_NOT_RESOLVABLE", "O plano não autoriza resolução. Inspecione as evidências faltantes; nenhum comando será enviado.")
    payload = {
        "plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"],
        "action": plan["action"], "reason": _text(reason, 8, 300, "Razão"),
    }
    assessment = plan["assessment"]
    classification = assessment["classification"]
    if classification in {"schema_applied", "schema_unchanged"}:
        if not evidence or not reference:
            raise CliError("ATTESTATION_REQUIRED", "Este plano exige --evidence com o arquivo local revisado e --reference com sua referência administrativa.")
        reference = _text(reference, 3, 300, "Referência")
        count, manifest = assessment.get("statement_count"), assessment.get("statement_manifest")
        if (
            type(count) is not int or not 1 <= count <= 2000 or not isinstance(manifest, list)
            or len(manifest) != count or any(
                not isinstance(item, dict) or type(item.get("index")) is not int
                or item["index"] != index or not _digest(item.get("sha256"))
                for index, item in enumerate(manifest, 1)
            )
        ):
            raise CliError("STATEMENT_MANIFEST_INVALID", "O plano não contém o manifesto integral das instruções que exigem atestação.")
        payload["attestation"] = {
            "reference": reference, "evidence_sha256": _file_digest(evidence),
            "statement_indices": [item["index"] for item in manifest], "observed_outcome": classification,
        }
    elif evidence is not None or reference is not None:
        raise CliError("ATTESTATION_NOT_ALLOWED", "Este plano utiliza evidência técnica e não admite substituição por atestação administrativa.")
    return payload


def _save_plan(plan, path, secrets=()):
    raw = (json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    serialized = raw.decode()
    if (
        any(isinstance(secret, str) and len(secret) >= 8 and secret in serialized for secret in secrets)
        or re.search(r'"(?:password|token|csrf_token|cookie|authorization|credential|secret|raw_body|sql)"\s*:', serialized, re.IGNORECASE)
    ):
        raise CliError("PLAN_SENSITIVE_CONTENT", "O plano retornado contém campos que não devem ser persistidos pelo CLI. Seu conteúdo foi omitido; consulte o responsável pela API.")
    if len(raw) > MAX_JSON_BYTES:
        raise CliError("PLAN_FILE_INVALID", "O plano excede o limite permitido para armazenamento local.")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise CliError("PLAN_FILE_EXISTS", "O destino já existe. Informe um arquivo novo; o plano existente foi preservado.") from None
    except OSError:
        raise CliError("PLAN_FILE_UNAVAILABLE", "Não foi possível salvar o plano. Preserve a chave de idempotência exibida para retomar com outro arquivo de destino.") from None


def _snapshot_output(value, secrets):
    result = {}
    for key in ("operation_id", "project_id", "revision_id"):
        if value.get(key):
            try:
                result[key] = _uuid(value[key])
            except CliError:
                pass
    for key in ("classification", "operation_state", "recovery_state", "blocked_code"):
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r"[A-Za-z_]{1,80}", item):
            result[key] = item
    for key in ("can_check", "operator_required"):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key in ("summary", "required_action", "blocked_reason"):
        if value.get(key):
            result[key] = _safe_message(value[key], secrets)
    missing = value.get("missing_evidence")
    if isinstance(missing, list):
        result["missing_evidence"] = [_safe_message(item, secrets, 250) for item in missing[:20] if isinstance(item, str)]
    migration = value.get("migration")
    if isinstance(migration, dict) and _digest(migration.get("sha256")):
        result["migration"] = {"name": _safe_message(migration.get("name"), secrets, 180), "sha256": migration["sha256"]}
        for key in ("phase", "effect_outcome"):
            if isinstance(migration.get(key), str) and re.fullmatch(r"[a-z_]{1,50}", migration[key]):
                result["migration"][key] = migration[key]
    checks = value.get("checks")
    if isinstance(checks, dict):
        result["checks"] = {
            key: _safe_message(item, secrets, 180) for key, item in list(checks.items())[:20]
            if isinstance(key, str) and re.fullmatch(r"[a-z_]{1,80}", key) and isinstance(item, str)
        }
    diagnostic = value.get("diagnostic")
    if isinstance(diagnostic, dict):
        result["diagnostic"] = {}
        for field in ("phase", "effect_status"):
            item = diagnostic.get(field)
            if isinstance(item, str) and re.fullmatch(r"[a-z_]{1,80}", item):
                result["diagnostic"][field] = item
        for field in ("initial_error", "current_error"):
            error = diagnostic.get(field)
            if not isinstance(error, dict):
                continue
            code = error.get("code")
            if not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) is None:
                continue
            result["diagnostic"][field] = {"code": code, "message": _safe_message(error.get("message"), secrets)}
            provider = error.get("provider")
            if isinstance(provider, dict):
                safe = {}
                if provider.get("method") in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                    safe["method"] = provider["method"]
                if type(provider.get("status")) is int and 100 <= provider["status"] <= 599:
                    safe["status"] = provider["status"]
                ray = provider.get("cf_ray")
                if isinstance(ray, str) and re.fullmatch(r"[a-fA-F0-9]{8,32}(?:-[A-Z]{3})?", ray):
                    safe["cf_ray"] = ray
                codes = provider.get("provider_error_codes")
                if isinstance(codes, list):
                    safe["provider_error_codes"] = [item for item in codes[:10] if type(item) is int and 0 <= item <= 2 ** 31]
                if provider.get("message"):
                    safe["message"] = _safe_message(provider["message"], secrets)
                if safe:
                    result["diagnostic"][field]["provider"] = safe
    return result


def _plan_output(plan, path, secrets):
    assessment = plan["assessment"]
    missing = assessment.get("missing_evidence_messages")
    return {
        "operation_id": plan["operation_id"], "plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"],
        "classification": assessment["classification"], "action": plan["action"], "can_resolve": plan["can_resolve"],
        "expires_at": datetime.fromisoformat(plan["expires_at"]).isoformat(),
        "statement_count": assessment.get("statement_count") if type(assessment.get("statement_count")) is int else None,
        "saved_file": _safe_message(Path(path).name, secrets, 200),
        "required_action": _safe_message(assessment.get("required_action"), secrets),
        "missing_evidence": [_safe_message(item, secrets, 250) for item in missing[:20] if isinstance(item, str)] if isinstance(missing, list) else [],
    }


def _parser():
    parser = SafeParser(
        description="Recuperação assistida de publicações com sessão administrativa. Não executa SQL do projeto.",
        epilog=(
            "Exemplos após --base-url HTTPS --origin ORIGIN: inspect UUID; "
            "plan UUID --reason 'Conferir a migration pendente' --output plano.json; "
            "resolve --plan plano.json --reason 'Resolver a evidência revisada' "
            "--evidence evidencia.json --reference 'INC-123 revisado'. "
            "A senha é solicitada sem eco. A evidência local é resumida em SHA-256; seu conteúdo não é enviado."
        ),
    )
    parser.add_argument("--base-url", required=True, help="Endereço da API administrativa; HTTPS é obrigatório em produção.")
    parser.add_argument("--origin", required=True, help="Origem administrativa já autorizada no serviço.")
    parser.add_argument("--login", help="Login administrativo; se omitido, será solicitado.")
    parser.add_argument("--allow-local-http", action="store_true", help="Permite HTTP somente em loopback para desenvolvimento.")
    parser.add_argument("--timeout", type=int, default=60, help="Timeout HTTP em segundos, de 1 a 300; padrão 60.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    inspect = commands.add_parser("inspect", help="Consulta o diagnóstico atual sem executar reconciliação.")
    inspect.add_argument("operation_id")
    plan = commands.add_parser("plan", help="Prepara e salva o plano de resolução retornado pelo servidor.")
    plan.add_argument("operation_id")
    plan.add_argument("--reason", required=True)
    plan.add_argument("--output", required=True)
    plan.add_argument("--idempotency-key")
    resolve = commands.add_parser("resolve", help="Executa exclusivamente a ação autorizada no plano salvo.")
    resolve.add_argument("--plan", required=True)
    resolve.add_argument("--reason", required=True)
    resolve.add_argument("--evidence", help="Arquivo local de evidência revisada; somente seu SHA-256 é enviado.")
    resolve.add_argument("--reference", help="Referência administrativa da evidência revisada.")
    resolve.add_argument("--idempotency-key", help="Chave original para retomar a mesma intenção após falha de comunicação.")
    return parser


def main(argv=None, *, transport=None):
    client = None
    try:
        args = _parser().parse_args(argv)
        base = normalize_url(args.base_url, allow_local_http=args.allow_local_http)
        origin = normalize_url(args.origin, origin=True, allow_local_http=args.allow_local_http)
        if not 1 <= args.timeout <= 300:
            raise CliError("TIMEOUT_INVALID", "O timeout deve estar entre 1 e 300 segundos.")
        payload, plan, intention = None, None, None
        if args.command == "resolve":
            plan = _read_plan(args.plan)
            expires = validate_plan(plan)
            if expires <= datetime.now(UTC) and not args.idempotency_key:
                raise CliError("PLAN_EXPIRED", "O plano expirou. Prepare outro plano ou informe a chave original com --idempotency-key para retomar uma resolução já iniciada.")
            operation_id = _uuid(plan["operation_id"])
            payload = resolution_payload(plan, args.reason, evidence=args.evidence, reference=args.reference)
            intention = args.idempotency_key or str(uuid5(NAMESPACE_URL, base + operation_id + canonical_hash(payload)))
        else:
            operation_id = _uuid(args.operation_id)
            if args.command == "plan":
                payload = {"reason": _text(args.reason, 8, 300, "Razão")}
                if Path(args.output).exists() or Path(args.output).is_symlink():
                    raise CliError("PLAN_FILE_EXISTS", "O destino já existe. Informe um arquivo novo; o plano existente foi preservado.")
                intention = args.idempotency_key or str(uuid4())
        if intention and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", intention) is None:
            raise CliError("IDEMPOTENCY_KEY_INVALID", "A chave deve conter de 1 a 120 letras, números ou caracteres _.:-.")
        login = _text(args.login if args.login is not None else input("Login administrativo: "), 3, 160, "Login")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                password = getpass.getpass("Senha administrativa: ")
            except getpass.GetPassWarning:
                raise CliError("SECURE_INPUT_UNAVAILABLE", "Use um terminal interativo que permita digitar a senha sem eco.") from None
        if not isinstance(password, str) or not 1 <= len(password) <= 128:
            raise CliError("PASSWORD_INVALID", "A senha deve conter de 1 a 128 caracteres.")
        client = AdministrativeClient(base, origin, timeout=args.timeout, transport=transport)
        client.login(login, password)
        route = "/publications/" + operation_id
        if intention:
            print(json.dumps({"idempotency_key": intention}, ensure_ascii=False), file=sys.stderr)
        if args.command == "inspect":
            result = client.request("GET", route + "/recovery")
            if result.get("operation_id") != operation_id:
                raise CliError("RESPONSE_IDENTITY_INVALID", "O diagnóstico não corresponde à operação solicitada.", exit_code=3)
            output = _snapshot_output(result, client.secrets)
        elif args.command == "plan":
            result = client.request("POST", route + "/recovery-plan", payload=payload, intention=intention)
            validate_plan(result, operation_id=operation_id)
            _save_plan(result, args.output, client.secrets)
            output = _plan_output(result, args.output, client.secrets)
        else:
            result = client.request("POST", route + "/resolve-recovery", payload=payload, intention=intention)
            if (
                result.get("id") != operation_id or not isinstance(result.get("state"), str)
                or result["state"] not in {"failed", "succeeded", "reconciling", "queued", "running", "cancelled"}
            ):
                raise CliError("RESPONSE_IDENTITY_INVALID", "A resposta não confirmou o estado esperado da operação. Preserve a chave para consultar ou retomar.", exit_code=3)
            reference = result.get("result_ref")
            recovery = reference.get("publication_recovery") if isinstance(reference, dict) else None
            recovery = recovery if isinstance(recovery, dict) else {}
            output = {
                "operation_id": operation_id, "operation_state": result["state"], "action": plan["action"],
                "recovery_state": _safe_message(recovery.get("state"), client.secrets, 50),
                "message": (
                    "A resolução encerrou esta tentativa como falha preservada. Uma nova publicação será explícita "
                    "da revisão atual, quando o readiness autorizar."
                ) if result["state"] == "failed" and recovery.get("state") == "resolved" else (
                    "Consulte o estado da operação e o readiness da revisão atual antes de iniciar outra publicação."
                ),
            }
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (EOFError, KeyboardInterrupt):
        print(json.dumps({"error": {"code": "INPUT_CANCELLED", "message": "Execução interrompida. Preserve a chave de idempotência já exibida e consulte a operação antes de retomar."}}, ensure_ascii=False), file=sys.stderr)
        return 2
    except CliError as exc:
        error = {"code": exc.code, "message": exc.message}
        if exc.status is not None:
            error["status"] = exc.status
        if exc.request_id:
            error["request_id"] = exc.request_id
        print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
        return exc.exit_code
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
