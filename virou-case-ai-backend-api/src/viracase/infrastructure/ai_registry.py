import json
import re
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from threading import Lock
from types import SimpleNamespace
from urllib.parse import quote

import httpx
from google.auth.exceptions import GoogleAuthError
from google.oauth2 import service_account
from sqlalchemy import select

from viracase.domain.rules import RuleError, aware, canonical_hash, utcnow
from viracase.infrastructure.agent_execution import validate_model_execution
from viracase.infrastructure.credentials import CredentialVault
from viracase.infrastructure.model_discovery import catalog_result, discover_models
from viracase.infrastructure.models import AIConnection, AIModel, Audit, Idempotency, new_id

MODEL_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,160}$")
TOKEN_URL = "https://oauth2.googleapis.com/token"
POLICY_KEYS = (
    "input_usd_per_million",
    "cached_usd_per_million",
    "output_usd_per_million",
    "fx_brl_per_usd",
    "fx_source",
    "evidence",
    "effective_at",
    "valid_until",
    "max_output_tokens",
    "execution",
)


def policy_ready(p, provider=None, model=None):
    try:
        if provider and model:
            validate_model_execution(provider, model, p)
        prices = [Decimal(p[k]) for k in POLICY_KEYS[:3]]
        return (
            all(x.is_finite() and 0 <= x <= 1_000_000 for x in prices)
            and any(x > 0 for x in prices)
            and Decimal(0) < Decimal(p["fx_brl_per_usd"]) < Decimal(100)
            and bool(p["fx_source"].strip())
            and bool(p["evidence"].strip())
            and aware(datetime.fromisoformat(p["effective_at"]))
            <= utcnow()
            < aware(datetime.fromisoformat(p["valid_until"]))
            and 1 <= p["max_output_tokens"] <= 65536
        )
    except (KeyError, ValueError, TypeError, ArithmeticError, RuleError):
        return False


def connection_dto(c):
    return {
        "id": c.id,
        "name": c.name,
        "provider": c.provider,
        "auth_mode": c.auth_mode,
        "enabled": c.enabled,
        "version": c.version,
        "credential_configured": bool(c.credential),
        "takes_over": c.takes_over,
        **c.options,
    }


def model_dto(m, c):
    verified = m.verified_at is not None and m.verified_connection_version == c.version
    return {
        "id": m.id,
        "connection_id": m.connection_id,
        "name": m.name,
        "model": m.model,
        "enabled": m.enabled,
        "version": m.version,
        "verified": verified,
        "verified_at": m.verified_at.isoformat() if m.verified_at else None,
        "ready": bool(c.enabled and m.enabled and verified and policy_ready(m.policy, c.provider, m.model)),
        **m.policy,
    }


class AIRegistry:
    def __init__(self, repo, transport=None):
        self.repo, self.config, self.sessions = repo, repo.config, repo.sessions
        self.vault = CredentialVault(self.config.credential_key_path)
        self.transport = transport
        self.tokens = {}
        self.token_lock = Lock()

    @staticmethod
    def admin(account):
        if account.role != "admin":
            raise RuleError("FORBIDDEN", "Ação não permitida.", 403)

    @staticmethod
    def connection(db, cid, lock=False):
        query = select(AIConnection).where(AIConnection.id == cid)
        item = db.scalar(query.with_for_update() if lock else query)
        if not item:
            raise RuleError("NOT_FOUND", "Configuração indisponível.", 404)
        return item

    def listing(self):
        with self.sessions() as db:
            connections = db.scalars(select(AIConnection).order_by(AIConnection.name)).all()
            return {
                "items": [
                    {
                        **connection_dto(c),
                        "models": [
                            model_dto(m, c)
                            for m in db.scalars(
                                select(AIModel).where(AIModel.connection_id == c.id).order_by(AIModel.name)
                            )
                        ],
                    }
                    for c in connections
                ]
            }

    @staticmethod
    def validate_credential(provider, mode, value, options):
        if provider == "gemini" and mode == "service_account":
            try:
                raw = json.loads(value)
                if raw.get("type") != "service_account" or raw.get("token_uri") != TOKEN_URL:
                    raise ValueError
                if not re.fullmatch(
                    r"[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.gserviceaccount\.com", raw["client_email"]
                ):
                    raise ValueError
                info = {
                    k: raw[k]
                    for k in (
                        "type",
                        "project_id",
                        "private_key",
                        "private_key_id",
                        "client_email",
                        "token_uri",
                    )
                }
                service_account.Credentials.from_service_account_info(info)
                if not options["project_id"]:
                    raise ValueError
                return info
            except (ValueError, KeyError, TypeError, GoogleAuthError):
                raise RuleError(
                    "VALIDATION_ERROR",
                    "Informe um JSON de conta de serviço válido e o projeto Google Cloud.",
                    400,
                ) from None
        if not 12 <= len(value) <= 4096 or any(c.isspace() for c in value):
            raise RuleError("VALIDATION_ERROR", "Informe uma API key válida, sem espaços.", 400)
        if provider == "openai" and mode == "service_account" and not options["project_id"]:
            raise RuleError("VALIDATION_ERROR", "Informe o projeto da Service Account OpenAI.", 400)
        return {"api_key": value}

    def save_connection(self, actor, cid, key, data):
        safe = {**data, "credential": sha256((data.get("credential") or "").encode()).hexdigest()}

        def change(db, operator):
            self.admin(operator)
            c = self.connection(db, cid, True) if cid else None
            if c and c.version != data["expected_version"]:
                raise RuleError("VERSION_CONFLICT", "A conexão mudou. Atualize a página.", 409)
            if c and (c.provider != data["provider"] or c.auth_mode != data["auth_mode"]):
                raise RuleError(
                    "VALIDATION_ERROR", "Crie outra conexão para trocar provedor ou autenticação.", 400
                )
            options = {k: data[k] for k in ("project_id", "organization", "location")}
            secret = data.get("credential")
            if not c:
                if not secret:
                    raise RuleError("VALIDATION_ERROR", "Informe a credencial da nova conexão.", 400)
                c = AIConnection(
                    id=new_id(), provider=data["provider"], auth_mode=data["auth_mode"], version=1
                )
                db.add(c)
            else:
                c.version += 1
            if secret:
                parsed = self.validate_credential(c.provider, c.auth_mode, secret, options)
                c.credential = self.vault.encrypt(c.id, parsed)
            elif c.auth_mode == "service_account" and not options["project_id"]:
                raise RuleError("VALIDATION_ERROR", "Informe o projeto da conta de serviço.", 400)
            c.name, c.options, c.enabled = data["name"], options, data["enabled"]
            db.add(
                Audit(actor_id=actor, action="ai-connection-save", resource_id=c.id, reason=data["reason"])
            )
            db.flush()
            return connection_dto(c)

        return self.repo.mutate(actor, f"ai-connection:{cid or 'new'}", key, safe, change)

    def save_model(self, actor, cid, mid, key, data):
        def change(db, operator):
            self.admin(operator)
            c = self.connection(db, cid, True)
            execution = validate_model_execution(c.provider, data["model"], data)
            m = db.get(AIModel, mid) if mid else None
            if mid and (not m or m.connection_id != cid):
                raise RuleError("NOT_FOUND", "Modelo indisponível.", 404)
            if m and m.version != data["expected_version"]:
                raise RuleError("VERSION_CONFLICT", "O modelo mudou. Atualize a página.", 409)
            if not m:
                m = AIModel(id=new_id(), connection_id=cid, version=1)
                db.add(m)
            else:
                m.version += 1
                if m.model != data["model"] or m.policy.get("execution") != execution:
                    m.verified_at, m.verified_connection_version = None, None
            m.name, m.model = data["name"], data["model"]
            m.policy = {k: data.get(k) for k in POLICY_KEYS}
            m.policy["execution"] = execution
            if data["enabled"] and not (
                c.enabled
                and m.verified_at
                and m.verified_connection_version == c.version
                and policy_ready(m.policy, c.provider, m.model)
            ):
                raise RuleError(
                    "AI_CONFIGURATION_PENDING",
                    "Teste o modelo e preencha custos, câmbio e validade antes de habilitar.",
                    409,
                )
            m.enabled = data["enabled"]
            if m.enabled:
                c.takes_over = True
            db.add(Audit(actor_id=actor, action="ai-model-save", resource_id=m.id, reason=data["reason"]))
            db.flush()
            return model_dto(m, c)

        return self.repo.mutate(actor, f"ai-model:{cid}:{mid or 'new'}", key, data, change)

    def _client(self):
        return httpx.Client(
            timeout=httpx.Timeout(20, connect=10),
            transport=self.transport,
            follow_redirects=False,
            trust_env=False,
        )

    def headers(self, c):
        secret = self.vault.decrypt(c.id, c.credential)
        if c.provider == "openai":
            headers = {"Authorization": "Bearer " + secret["api_key"]}
            if c.options.get("project_id"):
                headers["OpenAI-Project"] = c.options["project_id"]
            if c.options.get("organization"):
                headers["OpenAI-Organization"] = c.options["organization"]
            return headers
        if c.auth_mode == "api_key":
            return {"x-goog-api-key": secret["api_key"]}
        with self.token_lock:
            cache_key = (c.id, c.version)
            credentials = self.tokens.get(cache_key)
            if not credentials:
                credentials = service_account.Credentials.from_service_account_info(
                    secret, scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
            if not credentials.valid:

                def token_request(url, method="GET", body=None, headers=None, timeout=10, **kwargs):
                    if url != TOKEN_URL:
                        raise ValueError("token_endpoint")
                    with self._client() as client:
                        response = client.request(method, url, content=body, headers=headers, timeout=10)
                        if len(response.content) > 100_000:
                            raise ValueError("token_response_limit")
                        return SimpleNamespace(
                            status=response.status_code, data=response.content, headers=response.headers
                        )

                credentials.refresh(token_request)
                if len(self.tokens) >= 100:
                    self.tokens.clear()
                self.tokens[cache_key] = credentials
            return {
                "Authorization": "Bearer " + credentials.token,
                "x-goog-user-project": c.options["project_id"],
            }

    def local_catalog(self, cid):
        with self.sessions() as db:
            c = self.connection(db, cid)
            return catalog_result(c.provider)

    def discover(self, cid):
        with self.sessions() as db:
            c = self.connection(db, cid)
        return discover_models(c, self.headers, self._client)

    def managed_providers(self):
        with self.sessions() as db:
            return set(
                db.scalars(select(AIConnection.provider).where(AIConnection.takes_over.is_(True)).distinct())
            )

    @staticmethod
    def policy(m, c):
        return {
            **m.policy,
            "provider": c.provider,
            "model": m.model,
            "connection_id": c.id,
            "model_id": m.id,
            "connection_version": c.version,
            "version": f"{m.id}:{m.version}:{c.version}",
        }

    def eligible(self):
        with self.sessions() as db:
            return [
                self.policy(m, c)
                for m, c in db.execute(select(AIModel, AIConnection).join(AIConnection)).all()
                if model_dto(m, c)["ready"]
            ]

    def request_settings(self, policy, probe=False):
        try:
            with self.sessions() as db:
                c = self.connection(db, policy["connection_id"])
                m = db.get(AIModel, policy["model_id"])
                if (
                    not m
                    or m.connection_id != c.id
                    or not c.enabled
                    or self.policy(m, c)["version"] != policy["version"]
                    or (not probe and not model_dto(m, c)["ready"])
                ):
                    raise RuleError("AI_CONFIGURATION_PENDING", "Configuração alterada ou desabilitada.")
                headers = self.headers(c)
                if c.provider == "openai":
                    return "https://api.openai.com/v1/responses", headers
                model = quote(m.model, safe="")
                if c.auth_mode == "api_key":
                    return (
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                        headers,
                    )
                location, project = c.options["location"], c.options["project_id"]
                host = (
                    "aiplatform.googleapis.com"
                    if location == "global"
                    else f"{location}-aiplatform.googleapis.com"
                )
                return (
                    f"https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent",
                    headers,
                )
        except (httpx.HTTPError, GoogleAuthError, ValueError, KeyError, TypeError):
            raise RuleError("AI_PROVIDER_UNAVAILABLE", "Credencial do provedor indisponível.", 502) from None

    def probe(self, actor, cid, mid, key, payload):
        from viracase.infrastructure.providers import HttpModelGateway, ProviderRejected, ProviderUncertain

        command = f"ai-probe:{cid}:{mid}"
        probe_id = new_id()

        def claim(db, operator):
            self.admin(operator)
            c = self.connection(db, cid, True)
            m = db.get(AIModel, mid)
            if not m or m.connection_id != cid:
                raise RuleError("NOT_FOUND", "Modelo indisponível.", 404)
            if m.version != payload["expected_version"] or not c.enabled:
                raise RuleError(
                    "VERSION_CONFLICT", "Atualize o modelo e habilite a conexão antes do teste.", 409
                )
            return {"id": probe_id, "state": "running", "policy": self.policy(m, c)}

        claimed = self.repo.mutate(actor, command, key, payload, claim)
        if claimed["id"] != probe_id:
            return {k: v for k, v in claimed.items() if k != "policy"}
        policy = claimed["policy"]
        state = "failed"
        try:
            execution = validate_model_execution(policy["provider"], policy["model"], policy)
            thinking_budget = (execution or {}).get("thinking_budget") or 0
            probe_limit = min(policy["max_output_tokens"], max(1024, thinking_budget + 1024))
            result = HttpModelGateway(self.config, transport=self.transport, registry=self).generate(
                {**policy, "max_output_tokens": probe_limit},
                'Return only a JSON object: {"ok":true}',
                probe=True,
            )
            if not result.output_error and isinstance(json.loads(result.text), dict):
                state = "verified"
        except ProviderUncertain:
            state = "uncertain"
        except (ProviderRejected, RuleError, ValueError):
            state = "failed"
        with self.sessions.begin() as db:
            c = self.connection(db, cid, True)
            m = db.get(AIModel, mid)
            if self.policy(m, c)["version"] != policy["version"]:
                state = "configuration_changed"
            if state == "verified":
                m.verified_at, m.verified_connection_version = utcnow(), c.version
            elif state != "configuration_changed":
                m.verified_at, m.verified_connection_version, m.enabled = None, None, False
            final = {"id": probe_id, "state": state}
            intent = db.get(Idempotency, canonical_hash([actor, command, key]))
            intent.result = final
            db.add(
                Audit(
                    actor_id=actor, action="ai-model-test:" + state, resource_id=mid, reason=payload["reason"]
                )
            )
        return final
