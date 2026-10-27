from datetime import datetime
from decimal import Decimal
import json
import httpx
from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError, aware, cost, money, utcnow
from viracase.infrastructure.provider_errors import ProviderRejected, configuration_rejection, rejection, log
from viracase.application.conversation import response_schema
from viracase.application.conversation_turn import turn_schema


ALLOWED = {
    "openai": {"gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"},
    "gemini": {"gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-4-argon"},
}


class ModelCatalog:
    def __init__(self, config, registry=None):
        self.config, self.registry = config, registry

    def eligible(self):
        managed = self.registry.managed_providers() if self.registry else set()
        result = self.registry.eligible() if self.registry else []
        try:
            raw = json.loads(self.config.catalog_path.read_text())
            for p in raw["models"]:
                if p.get("provider") in managed:
                    continue
                if not p.get("enabled"):
                    continue
                if p["model"] not in ALLOWED.get(p["provider"], set()) or not p.get("verified_account"):
                    continue
                if (
                    not aware(datetime.fromisoformat(p["effective_at"]))
                    <= utcnow()
                    < aware(datetime.fromisoformat(p["valid_until"]))
                ):
                    continue
                if (
                    not p.get("fx_source")
                    or not p.get("evidence")
                    or not (Decimal(0) < Decimal(p["fx_brl_per_usd"]) < Decimal(100))
                ):
                    continue
                if any(
                    Decimal(p[k]) < 0
                    for k in ("input_usd_per_million", "cached_usd_per_million", "output_usd_per_million")
                ):
                    continue
                if not 1 <= int(p["max_output_tokens"]) <= 32000:
                    continue
                if not getattr(self.config, p["provider"] + "_api_key", ""):
                    continue
                result.append(p)
            return result
        except (OSError, ValueError, KeyError, TypeError):
            return result

    def resolve(self, prompt):
        policies = self.eligible()
        if not policies:
            raise RuleError(
                "COMMERCIAL_CONFIGURATION_PENDING",
                "O operador precisa validar e habilitar a integração de IA.",
            )
        ceilings = [
            (
                cost(
                    p, {"input": len(prompt.encode()) + 4096, "cached": 0, "output": p["max_output_tokens"]}
                ),
                p,
            )
            for p in policies
        ]
        ceiling, policy = min(ceilings, key=lambda pair: pair[0])
        policy = dict(policy)
        policy["reserved_input_bound"] = len(prompt.encode()) + 4096
        return policy, money(ceiling * Decimal("1.10"))


class ProviderUncertain(Exception):
    pass


class HttpModelGateway:
    def __init__(self, config, transport=None, registry=None):
        self.config, self.transport, self.registry = config, transport, registry

    def generate(self, policy, prompt, probe=False):
        provider, model = policy["provider"], policy["model"]
        managed = bool(policy.get("connection_id"))
        if managed:
            if not self.registry:
                raise configuration_rejection("AI_REGISTRY_UNAVAILABLE", error_code="PROVIDER_REJECTED")
            try:
                url, headers = self.registry.request_settings(policy, probe=probe)
            except RuleError as exc:
                raise configuration_rejection(exc.code) from None
        elif model not in ALLOWED.get(provider, set()):
            raise ProviderRejected("catalog")
        if provider == "openai":
            if not managed:
                url = "https://api.openai.com/v1/responses"
                headers = {"Authorization": "Bearer " + self.config.openai_api_key}
            body = {
                "model": model,
                "input": prompt,
                "max_output_tokens": policy["max_output_tokens"],
                "store": False,
                "service_tier": "default",
                "text": {"format": {"type": "json_object"}},
            }
            if policy.get("generation_contract_version") in {3, 4}:
                body["text"]["format"] = {
                    "type": "json_schema",
                    "name": "project_changes",
                    "strict": True,
                    "schema": turn_schema()
                    if policy.get("generation_contract_version") == 4
                    else response_schema(),
                }
        else:
            if not managed:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                headers = {"x-goog-api-key": self.config.gemini_api_key}
            body = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "maxOutputTokens": policy["max_output_tokens"],
                    "candidateCount": 1,
                },
            }
            if policy.get("generation_contract_version") in {3, 4} and model in {
                "gemini-3.8-flash",
                "gemini-3.5-flash-lite",
            }:
                body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "LOW"}
            if policy.get("generation_contract_version") in {3, 4}:

                def gemini_schema(value):
                    if isinstance(value, dict):
                        converted = {
                            k: v.upper() if k == "type" else gemini_schema(v)
                            for k, v in value.items()
                            if k != "additionalProperties"
                        }
                        if policy.get("generation_contract_version") == 4 and value.get("type") == "object":
                            converted["propertyOrdering"] = list(value["properties"])
                        return converted
                    if isinstance(value, list):
                        return [gemini_schema(v) for v in value]
                    return value

                body["generationConfig"]["responseSchema"] = gemini_schema(
                    turn_schema() if policy.get("generation_contract_version") == 4 else response_schema()
                )
        try:
            with httpx.Client(
                timeout=httpx.Timeout(self.config.provider_timeout, connect=10),
                transport=self.transport,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                for dispatch in range(2):
                    with client.stream("POST", url, headers=headers, json=body) as response:
                        rejected = response.status_code in {400, 401, 403, 404, 413, 422, 429}
                        if response.status_code != 200 and not rejected:
                            raise ProviderUncertain("provider_status")
                        raw = bytearray()
                        for chunk in response.iter_bytes():
                            raw.extend(chunk)
                            if len(raw) > (64000 if rejected else 4_000_000):
                                if rejected:
                                    raw = bytearray()
                                    break
                                raise ProviderUncertain("response_limit")
                        if not rejected:
                            break
                        exc = rejection(response.status_code, raw)
                        field = exc.diagnostic.get("unsupported_field")
                        optional = body.get("generationConfig", {})
                        if dispatch == 0 and field in optional:
                            optional.pop(field)
                            log.warning(
                                json.dumps({"event": "provider_parameter_fallback", **exc.diagnostic})
                            )
                            continue
                        raise exc
            data = json.loads(raw)
            output_error = None
            if provider == "openai":
                u = data["usage"]
                details = u.get("input_tokens_details", {})
                if any(details.get(k, 0) for k in ("cache_write_tokens", "cache_creation_tokens")):
                    raise ProviderUncertain("unsupported_billing_category")
                usage = {
                    "input": u["input_tokens"],
                    "cached": details.get("cached_tokens", 0),
                    "output": u["output_tokens"],
                }
                text = "".join(
                    c.get("text", "")
                    for item in data.get("output", [])
                    if item.get("type") == "message"
                    for c in item.get("content", [])
                    if c.get("type") == "output_text"
                )
                external = data.get("id")
                reason = (data.get("incomplete_details") or {}).get("reason")
                if data.get("status") == "incomplete":
                    output_error = (
                        "GENERATION_TRUNCATED" if reason == "max_output_tokens" else "GENERATION_BLOCKED"
                    )
                elif data.get("status") in {"failed", "cancelled"}:
                    output_error = "GENERATION_INCOMPLETE"
                elif any(
                    c.get("type") == "refusal"
                    for item in data.get("output", [])
                    for c in item.get("content", [])
                ):
                    output_error = "GENERATION_BLOCKED"
            else:
                u = data["usageMetadata"]
                if u.get("toolUsePromptTokenCount", 0):
                    raise ProviderUncertain("unsupported_billing_category")
                usage = {
                    "input": u["promptTokenCount"],
                    "cached": u.get("cachedContentTokenCount", 0),
                    "output": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0),
                }
                candidate = next(iter(data.get("candidates", [])), {})
                text = "".join(
                    p.get("text", "")
                    for p in candidate.get("content", {}).get("parts", [])
                    if not p.get("thought")
                )
                external = data.get("responseId")
                reason = candidate.get("finishReason")
                if reason == "MAX_TOKENS":
                    output_error = "GENERATION_TRUNCATED"
                elif reason not in {None, "STOP"} or data.get("promptFeedback", {}).get("blockReason"):
                    output_error = "GENERATION_BLOCKED"
            cost(policy, usage)
            return ModelResult(text, usage, external, output_error)
        except ProviderRejected:
            raise
        except (httpx.HTTPError, KeyError, ValueError, TypeError, RuleError) as exc:
            raise ProviderUncertain(type(exc).__name__) from None
