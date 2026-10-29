from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from hashlib import sha256
from pathlib import PurePosixPath
import json
import re


class RuleError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, details=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.details = details or {}


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def money(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) > Decimal("1000000"):
            raise ValueError
        return result.quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)
    except (ValueError, InvalidOperation):
        raise RuleError("VALIDATION_ERROR", "Valor monetário inválido.", 400) from None


def canonical_hash(payload):
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Access:
    starts_at: datetime
    expires_at: datetime
    purge_at: datetime
    suspended: bool = False
    lifecycle: str = "active"

    @classmethod
    def activate(cls, start, access_days=30):
        if type(access_days) is not int or not 1 <= access_days <= 3650:
            raise RuleError("VALIDATION_ERROR", "Informe de 1 a 3650 dias de acesso.", 400)
        start = aware(start)
        end = start + timedelta(days=access_days)
        return cls(start, end, end + timedelta(days=2))

    def with_expiry(self, end, now):
        end, now = aware(end), aware(now)
        if end <= max(aware(self.starts_at), now) or end > now + timedelta(days=3650):
            raise RuleError("VALIDATION_ERROR", "O fim do acesso deve ser futuro, em até 3650 dias.", 400)
        return Access(aware(self.starts_at), end, end + timedelta(days=2), self.suspended, self.lifecycle)

    def status(self, now):
        if self.lifecycle in {"purging", "reconciling", "purged"}:
            return self.lifecycle
        if aware(now) >= aware(self.purge_at):
            return "purge_eligible"
        if self.suspended:
            return "suspended"
        if aware(now) < aware(self.starts_at):
            return "pending"
        return "active" if aware(now) < aware(self.expires_at) else "grace"

    def authorize(self, action, now):
        state = self.status(now)
        if state in {"purge_eligible", "purging", "reconciling", "purged"}:
            raise RuleError("RETENTION_EXPIRED", "O prazo de acesso aos arquivos terminou.", 403)
        if state != "active" and not (state in {"grace", "suspended"} and action in {"read", "export"}):
            raise RuleError("ACCESS_EXPIRED", "Acesso produtivo indisponível neste período.", 403)


def reserve(available, amount):
    amount = money(amount)
    if amount <= 0:
        raise RuleError("COMMERCIAL_CONFIGURATION_PENDING", "Política de consumo indisponível.")
    if money(available) < amount:
        raise RuleError("INSUFFICIENT_BALANCE", "Saldo disponível insuficiente para esta operação.", 402)
    return money(available) - amount


def settle(reserved, actual):
    reserved, actual = money(reserved), money(actual)
    if actual < 0 or actual > reserved:
        raise RuleError("COST_DISCREPANCY", "Consumo aguarda reconciliação operacional.")
    return actual, reserved - actual


def choose_stack(objective):
    if re.search(
        r"\b(java|spring|php|laravel|ruby|rails|c#|dotnet|django|rust|golang|kotlin|swift|scala|elixir|flutter|dart)\b",
        objective,
        re.I,
    ):
        raise RuleError(
            "STACK_UNSUPPORTED", "O catálogo suporta Hono JavaScript/TypeScript e FastAPI Python com D1.", 400
        )
    return (
        "fastapi-python-d1" if re.search(r"\b(python|fastapi)\b", objective, re.I) else "hono-typescript-d1"
    )


def validate_file_entries(files, secrets=()):
    if not isinstance(files, dict) or not 1 <= len(files) <= 80:
        raise RuleError("INVALID_MANIFEST", "O projeto deve conter de 1 a 80 arquivos.", 400)
    total = 0
    for name, content in files.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise RuleError("INVALID_MANIFEST", "Arquivos precisam ter nomes e conteúdo textual.", 400)
        parts = PurePosixPath(name).parts
        if (
            not name
            or len(name) > 180
            or name.startswith("/")
            or "\\" in name
            or any(p in {".", "..", ".git", "node_modules", ".venv"} for p in name.split("/"))
            or any(ord(c) < 32 for c in name)
            or ":" in name
            or "/".join(parts) != name
            or name.lower().endswith((".pem", ".key", ".p12"))
            or any(p == ".env" or (p.startswith(".env.") and p != ".env.example") for p in parts)
        ):
            raise RuleError("INVALID_PATH", "Caminho de arquivo não permitido.", 400)
        size = len(content.encode())
        if size > 200_000 or "\x00" in content:
            raise RuleError("FILE_LIMIT_EXCEEDED", "Arquivo fora dos limites de tamanho ou formato.", 400)
        total += size
        if any(s and len(s) > 12 and s in content for s in secrets) or re.search(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}|\bAIza[\w-]{30,}|\bghp_[\w]{30,}",
            content,
        ):
            raise RuleError(
                "SECRET_DETECTED", "Remova a credencial identificada antes de salvar ou exportar.", 400
            )
    if total > 2_000_000:
        raise RuleError("PROJECT_SIZE_EXCEEDED", "O projeto ultrapassa o limite de 2 MB.", 400)


def validate_files(files, stack, secrets=()):
    validate_file_entries(files, secrets)
    required = {"README.md", "migrations/0001.sql", "public/index.html", "wrangler.jsonc"}
    required |= (
        {"src/index.ts", "package.json"}
        if stack == "hono-typescript-d1"
        else {"src/entry.py", "pyproject.toml"}
    )
    if not required.issubset(files):
        raise RuleError(
            "STACK_MISMATCH",
            "A revisão precisa preservar frontend, backend, configuração e migration do catálogo.",
            400,
        )
    if stack == "hono-typescript-d1":
        try:
            package = json.loads(files["package.json"])
        except (ValueError, TypeError):
            raise RuleError("STACK_MISMATCH", "package.json inválido.", 400) from None
        if "hono" not in package.get("dependencies", {}) or "hono" not in files["src/index.ts"]:
            raise RuleError("STACK_MISMATCH", "O backend desta stack precisa usar Hono.", 400)
    elif "FastAPI" not in files["src/entry.py"]:
        raise RuleError("STACK_MISMATCH", "O backend desta stack precisa usar FastAPI.", 400)
    return canonical_hash(files)


def cost(policy, usage):
    prompt, cached, output = (int(usage[x]) for x in ("input", "cached", "output"))
    if min(prompt, cached, output) < 0 or cached > prompt:
        raise RuleError("USAGE_UNCERTAIN", "Apuração de consumo incompleta.")
    usd = (
        (Decimal(prompt - cached) * Decimal(policy["input_usd_per_million"]))
        + Decimal(cached) * Decimal(policy["cached_usd_per_million"])
        + Decimal(output) * Decimal(policy["output_usd_per_million"])
    ) / Decimal(1_000_000)
    return money(usd * Decimal(policy["fx_brl_per_usd"]))
