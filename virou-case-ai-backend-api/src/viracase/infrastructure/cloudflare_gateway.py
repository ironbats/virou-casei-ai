import base64
import json
import mimetypes
import re
import time
from hashlib import sha256

import httpx

from viracase.domain.rules import RuleError
from viracase.infrastructure.publication_d1_diagnostics import d1_error_evidence
from viracase.infrastructure.publication_logging import emit, exception_fields
from viracase.infrastructure.publication_sql_review import split_sql_statements


class PublicationUncertain(RuleError):
    def __init__(self, diagnostic=None):
        super().__init__(
            "PUBLICATION_RESULT_UNKNOWN", "Verificando resultado da publicação. Não repita o envio."
        )
        if diagnostic:
            self.provider_diagnostic = diagnostic


class CloudflareGateway:
    def __init__(self, policy, token, transport=None, *, clock=None, sleep=None):
        self.policy, self.token, self.transport = policy, token, transport
        self.account_path = "/accounts/" + policy["account_id"]
        self.before_request = None
        self.verification_observer = None
        self.last_request_diagnostic = None
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep

    def request(self, method, path, **kwargs):
        headers = kwargs.pop("headers", {"Authorization": "Bearer " + self.token})
        route = self._route_label(path)
        started, status, error_codes, ray = time.monotonic(), None, [], None
        sql = kwargs.get("json", {}).get("sql") if isinstance(kwargs.get("json"), dict) else None
        is_d1 = route.endswith("/d1/database/{database}/query")
        envelope = None
        self.last_request_diagnostic = None
        if self.before_request:
            try:
                self.before_request(method)
            except Exception as exc:
                exc.provider_diagnostic = {
                    "method": method, "route": route, "status": None,
                    "provider_error_codes": [], "cf_ray": None,
                    "effect_outcome": "not_sent", "reason": "local_dispatch_blocked",
                    "message": "A conferência local interrompeu a solicitação antes do envio à Cloudflare.",
                }
                self.last_request_diagnostic = exc.provider_diagnostic
                raise
        completed = False
        try:
            with httpx.Client(
                transport=self.transport, timeout=self.policy["timeout_seconds"], follow_redirects=False
            ) as client:
                response = client.request(
                    method, "https://api.cloudflare.com/client/v4" + path, headers=headers, **kwargs
                )
            status = response.status_code
            ray_value = response.headers.get("cf-ray", "")
            ray = ray_value if re.fullmatch(r"[a-fA-F0-9]{8,32}(?:-[A-Z]{3})?", ray_value) else None
            try:
                envelope = response.json()
                if isinstance(envelope, dict) and isinstance(envelope.get("errors"), list):
                    error_codes = [
                        e["code"] for e in envelope["errors"][:10]
                        if isinstance(e, dict) and type(e.get("code")) is int
                    ]
            except ValueError:
                envelope = None
            if status == 404 and method == "GET":
                completed = True
                return None
            if status >= 500:
                if method != "GET":
                    raise PublicationUncertain()
                raise RuleError("DEPENDENCY_UNAVAILABLE", "Cloudflare indisponível. Tente validar novamente.", 503)
            if status == 429:
                raise RuleError("QUOTA_EXCEEDED", "A Cloudflare limitou as requisições. Aguarde antes de tentar novamente.", 429)
            if status in {401, 403}:
                raise RuleError("CONFIGURATION_INVALID", "Token inválido ou sem permissão para a conta/operação.", 409)
            if not isinstance(envelope, dict):
                raise TypeError("invalid_response_envelope")
            if not response.is_success or not envelope.get("success"):
                raise RuleError("PUBLICATION_REJECTED", "A Cloudflare recusou a operação. Consulte o diagnóstico desta tentativa.")
            completed = True
            self.last_request_diagnostic = {
                "method": method, "route": route, "status": status,
                "provider_error_codes": error_codes, "cf_ray": ray,
            }
            return envelope.get("result")
        except RuleError as exc:
            exc.provider_diagnostic = {
                "method": method, "route": route, "status": status,
                "provider_error_codes": error_codes, "cf_ray": ray,
                **(d1_error_evidence(envelope, sql) if is_d1 else {}),
            }
            if is_d1 and exc.code in {"PUBLICATION_RESULT_UNKNOWN", "QUOTA_EXCEEDED", "CONFIGURATION_INVALID"}:
                exc.provider_diagnostic["effect_outcome"] = "unknown"
            self.last_request_diagnostic = exc.provider_diagnostic
            emit("cloudflare_request_failed", "error", method=method, route=route, status=status,
                 provider_error_codes=error_codes, cf_ray=ray,
                 **({key: exc.provider_diagnostic[key] for key in ("reason", "effect_outcome")} if is_d1 else {}),
                 duration_ms=round((time.monotonic() - started) * 1000), **exception_fields(exc))
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            emit("cloudflare_transport_failed", "error", method=method, route=route, status=status,
                 duration_ms=round((time.monotonic() - started) * 1000), **exception_fields(exc))
            failure = PublicationUncertain() if method != "GET" else RuleError(
                "DEPENDENCY_UNAVAILABLE", "Não foi possível consultar a Cloudflare.", 503
            )
            failure.provider_diagnostic = {
                "method": method, "route": route, "status": status,
                "provider_error_codes": error_codes, "cf_ray": ray,
                "transport_error": type(exc).__name__,
                "effect_outcome": "not_sent" if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout)) else "unknown",
            }
            self.last_request_diagnostic = failure.provider_diagnostic
            raise failure from None
        except (ValueError, KeyError, TypeError) as exc:
            emit("cloudflare_response_invalid", "error", method=method, route=route, status=status,
                 duration_ms=round((time.monotonic() - started) * 1000), **exception_fields(exc))
            failure = PublicationUncertain() if method != "GET" else RuleError(
                "DEPENDENCY_UNAVAILABLE", "Resposta inválida da Cloudflare.", 503
            )
            failure.provider_diagnostic = {
                "method": method, "route": route, "status": status,
                "provider_error_codes": error_codes, "cf_ray": ray,
                "response_error": type(exc).__name__,
                **({"effect_outcome": "unknown"} if is_d1 else {}),
            }
            self.last_request_diagnostic = failure.provider_diagnostic
            raise failure from None
        finally:
            if completed:
                emit("cloudflare_request_completed", method=method, route=route, status=status, cf_ray=ray,
                     duration_ms=round((time.monotonic() - started) * 1000))

    @staticmethod
    def _route_label(path):
        path = path.split("?", 1)[0]
        path = re.sub(r"/accounts/[^/]+", "/accounts/{account}", path)
        path = re.sub(r"/workers/scripts/[^/]+", "/workers/scripts/{worker}", path)
        path = re.sub(r"/d1/database/[^/]+", "/d1/database/{database}", path)
        known = {
            "/user/tokens/verify", "/accounts/{account}", "/accounts/{account}/tokens/verify",
            "/accounts/{account}/workers/scripts", "/accounts/{account}/workers/subdomain",
            "/accounts/{account}/d1/database", "/accounts/{account}/d1/database/{database}",
            "/accounts/{account}/d1/database/{database}/query", "/accounts/{account}/workers/assets/upload",
        }
        suffixes = {"", "/deployments", "/assets-upload-session", "/settings", "/subdomain"}
        if path in known or any(path == "/accounts/{account}/workers/scripts/{worker}" + suffix for suffix in suffixes):
            return path
        return "cloudflare_api"

    def validate(self):
        try:
            path = (
                self.account_path + "/tokens/verify"
                if self.policy["token_type"] == "account"
                else "/user/tokens/verify"
            )
            token = self.request("GET", path)
            if not token or token.get("status") != "active":
                raise RuleError("CONFIGURATION_INVALID", "O token não está ativo.")
            account = self.request("GET", self.account_path)
            if not account or account.get("id") != self.policy["account_id"]:
                raise RuleError("CONFIGURATION_INVALID", "A conta configurada não foi confirmada.")
            workers = self.request("GET", self.account_path + "/workers/scripts")
            databases = self.request("GET", self.account_path + "/d1/database?per_page=100&page=1")
            subdomain = self.request("GET", self.account_path + "/workers/subdomain")
            if not isinstance(workers, list) or not isinstance(databases, list) or not subdomain:
                raise RuleError(
                    "CONFIGURATION_INVALID", "Confira acesso a Workers, D1 e ao subdomínio workers.dev."
                )
            label = subdomain.get("subdomain", "")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", label):
                raise RuleError("CONFIGURATION_INVALID", "Configure o subdomínio workers.dev na conta.")
            return {
                "ready": True,
                "connection_verified": True,
                "workers_count": len(workers),
                "databases_count": len(databases),
                "subdomain": label,
                "database_ids": [item["uuid"] for item in databases],
                "write_permissions": "not_proven_by_read_only_validation",
                "hosting_isolation": "requires_reviewed_evidence",
                "message": "Conta, token e consultas confirmados. Permissões de escrita e isolamento exigem evidência separada.",
            }
        except RuleError as exc:
            return {"ready": False, "code": exc.code, "message": exc.message}

    def database(self, name):
        rows = self.request("GET", self.account_path + "/d1/database?name=" + name + "&per_page=100") or []
        matches = [r for r in rows if r.get("name") == name]
        if len(matches) > 1:
            raise RuleError("RESOURCE_CONFLICT", "Inventário remoto ambíguo. Revise no backoffice.")
        return matches[0] if matches else None

    def create_database(self, name):
        return self.request("POST", self.account_path + "/d1/database", json={"name": name})

    def query(self, database_id, sql, params=None):
        try:
            result = self.request(
                "POST",
                self.account_path + "/d1/database/" + database_id + "/query",
                json={"sql": sql, "params": params or []},
            )
        except RuleError as exc:
            diagnostic = getattr(exc, "provider_diagnostic", {})
            if exc.code == "PUBLICATION_REJECTED" and diagnostic.get("effect_outcome") == "none":
                failure = RuleError("MIGRATION_SQL_REJECTED", diagnostic["message"])
                failure.provider_diagnostic = diagnostic
                raise failure from None
            raise
        if not isinstance(result, list) or not result or any(not isinstance(item, dict) or item.get("success") is not True for item in result):
            evidence = {**(self.last_request_diagnostic or {}), **d1_error_evidence({"result": result}, sql)}
            failure = RuleError(
                "MIGRATION_SQL_REJECTED" if evidence["effect_outcome"] == "none" else "MIGRATION_FAILED",
                evidence["message"],
            )
            failure.provider_diagnostic = evidence
            raise failure
        if len(result) != len(split_sql_statements(sql)):
            diagnostic = {
                **(self.last_request_diagnostic or {}), "effect_outcome": "unknown",
                "reason": "incomplete_results", "confirmed_statements": len(result),
                "statement_count": len(split_sql_statements(sql)),
            }
            raise PublicationUncertain(diagnostic)
        return result

    def upload(self, name, database_id, artifact):
        deployments_path = self.account_path + "/workers/scripts/" + name + "/deployments"
        before = self.request("GET", deployments_path)
        if before is not None and not isinstance(before, dict):
            raise RuleError("DEPENDENCY_UNAVAILABLE", "A Cloudflare retornou um inventário de deployments inválido.", 503)
        prior = (before or {}).get("deployments", [])
        if not isinstance(prior, list):
            raise RuleError("DEPENDENCY_UNAVAILABLE", "A Cloudflare retornou um inventário de deployments inválido.", 503)
        prior_ids = {item.get("id") for item in prior if isinstance(item, dict)}
        manifest, contents = {}, {}
        for path, content in artifact["assets"].items():
            encoded = base64.b64encode(content.encode()).decode()
            digest = sha256((encoded + path.rsplit(".", 1)[-1]).encode()).hexdigest()[:32]
            manifest["/" + path] = {"hash": digest, "size": len(content.encode())}
            contents[digest] = (path, encoded, mimetypes.guess_type(path)[0] or "application/octet-stream")
        session = self.request(
            "POST",
            self.account_path + "/workers/scripts/" + name + "/assets-upload-session",
            json={"manifest": manifest},
        )
        jwt = session["jwt"]
        for bucket in session["buckets"]:
            response = self.request(
                "POST",
                self.account_path + "/workers/assets/upload?base64=true",
                headers={"Authorization": "Bearer " + jwt},
                files={h: contents[h] for h in bucket},
            )
            if response and response.get("jwt"):
                jwt = response["jwt"]
        metadata = {
            "main_module": artifact["main_module"],
            "compatibility_date": self.policy["compatibility_date"],
            "compatibility_flags": artifact["compatibility_flags"],
            "bindings": [
                {"type": "d1", "name": "DB", "database_id": database_id},
                {"type": "assets", "name": "ASSETS"},
            ],
            "assets": {
                "jwt": jwt,
                "config": {
                    "run_worker_first": ["/api/*"],
                    "not_found_handling": "single-page-application" if self.policy["spa"] else "none",
                },
            },
            "tags": ["viroucaseai", artifact["revision_id"]],
        }
        files = {"metadata": (None, json.dumps(metadata), "application/json")}
        files.update(
            {
                name: (
                    name,
                    base64.b64decode(data["content"])
                    if data.get("encoding") == "base64"
                    else data["content"],
                    data["mime"],
                )
                for name, data in artifact["modules"].items()
            }
        )
        timeout = httpx.Timeout(self.policy["timeout_seconds"], read=120)
        uncertainty = None
        try:
            result = self.request("PUT", self.account_path + "/workers/scripts/" + name, files=files, timeout=timeout)
        except PublicationUncertain as exc:
            uncertainty, result = exc, None
        if isinstance(result, dict) and result.get("id") and result["id"] != name:
            raise PublicationUncertain({"code": "UPLOAD_SCRIPT_MISMATCH"})
        if not isinstance(result, dict):
            return self._confirm_upload(name, database_id, artifact["revision_id"], prior_ids, uncertainty)
        if isinstance(result.get("deployment_id"), str) and re.fullmatch(
            r"[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}", result["deployment_id"]
        ):
            return result["deployment_id"]
        return self._confirm_upload(name, database_id, artifact["revision_id"], prior_ids)

    def _confirm_upload(self, name, database_id, revision, prior_ids, uncertainty=None):
        from viracase.infrastructure.publication_verification import binding_observation

        started = self.clock()
        deadline = started + max(30, min(90, self.policy["timeout_seconds"] * 3))
        path = self.account_path + "/workers/scripts/" + name
        observations = []
        for attempt in range(1, 13):
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            item = {"attempt": attempt, "confirmed": False, "code": "DEPLOYMENT_NOT_VISIBLE"}
            retryable = True
            try:
                after = self.request("GET", path + "/deployments", timeout=min(self.policy["timeout_seconds"], remaining))
                if after is not None and (not isinstance(after, dict) or not isinstance(after.get("deployments"), list)):
                    item["code"], retryable = "DEPLOYMENTS_RESPONSE_INVALID", False
                else:
                    recent = [
                        deployment for deployment in (after or {}).get("deployments", [])
                        if isinstance(deployment, dict)
                        and isinstance(deployment.get("id"), str)
                        and re.fullmatch(r"[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}", deployment["id"])
                        and deployment["id"] not in prior_ids
                    ]
                    item["new_deployments"] = len(recent)
                    if len(recent) > 1:
                        item["code"], retryable = "DEPLOYMENTS_AMBIGUOUS", False
                    elif len(recent) == 1 and self.clock() < deadline:
                        settings = self.request("GET", path + "/settings", timeout=min(self.policy["timeout_seconds"], deadline - self.clock()))
                        bindings = binding_observation(settings, database_id, revision)
                        item.update(code=bindings["code"], confirmed=bindings["passed"])
                        retryable = bindings["retryable"]
                        if bindings["passed"]:
                            emit("cloudflare_upload_confirmed", attempt=attempt,
                                 recovered=uncertainty is not None,
                                 duration_ms=round((self.clock() - started) * 1000))
                            return recent[0]["id"]
            except RuleError as exc:
                item["code"] = exc.code
                retryable = exc.code == "DEPENDENCY_UNAVAILABLE"
                if exc.code in {"LEASE_LOST", "VERSION_CONFLICT", "FORBIDDEN"}:
                    raise
            observations.append(item)
            emit("cloudflare_upload_observed", **item)
            delay = min(2 ** (attempt - 1), 8)
            if not retryable or attempt == 12 or self.clock() + delay >= deadline:
                break
            self.sleep(delay)
        diagnostic = dict(getattr(uncertainty, "provider_diagnostic", {}) or {})
        diagnostic["upload_confirmation"] = {
            "confirmed": False, "attempts": observations,
            "duration_ms": round((self.clock() - started) * 1000),
        }
        raise PublicationUncertain(diagnostic)

    def enable(self, name, enabled=True):
        return self.request(
            "POST",
            self.account_path + "/workers/scripts/" + name + "/subdomain",
            json={"enabled": enabled, "previews_enabled": False},
        )

    def settings(self, name):
        return self.request("GET", self.account_path + "/workers/scripts/" + name + "/settings")

    def delete(self, name, database_id):
        if self.settings(name):
            self.request("DELETE", self.account_path + "/workers/scripts/" + name)
        path = self.account_path + "/d1/database/" + database_id
        if self.request("GET", path) is not None:
            self.request("DELETE", path)

    def verify(self, url, revision):
        from viracase.infrastructure.publication_verification import RemoteVerifier

        return RemoteVerifier(
            self.policy["timeout_seconds"], self.transport, self.before_request,
            self.verification_observer,
        ).verify(url, revision)
