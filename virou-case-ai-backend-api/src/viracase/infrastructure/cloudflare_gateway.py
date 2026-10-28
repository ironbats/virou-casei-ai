import base64
import json
import mimetypes
import re
from hashlib import sha256

import httpx

from viracase.domain.rules import RuleError


class PublicationUncertain(RuleError):
    def __init__(self):
        super().__init__(
            "PUBLICATION_RESULT_UNKNOWN", "Verificando resultado da publicação. Não repita o envio."
        )


class CloudflareGateway:
    def __init__(self, policy, token, transport=None):
        self.policy, self.token, self.transport = policy, token, transport
        self.account_path = "/accounts/" + policy["account_id"]
        self.before_request = None

    def request(self, method, path, **kwargs):
        if self.before_request:
            self.before_request(method)
        headers = kwargs.pop("headers", {"Authorization": "Bearer " + self.token})
        try:
            with httpx.Client(
                transport=self.transport, timeout=self.policy["timeout_seconds"], follow_redirects=False
            ) as client:
                response = client.request(
                    method, "https://api.cloudflare.com/client/v4" + path, headers=headers, **kwargs
                )
            if response.status_code == 404 and method == "GET":
                return None
            if response.status_code >= 500:
                if method != "GET":
                    raise PublicationUncertain()
                raise RuleError(
                    "DEPENDENCY_UNAVAILABLE", "Cloudflare indisponível. Tente validar novamente.", 503
                )
            if response.status_code == 429:
                raise RuleError(
                    "QUOTA_EXCEEDED",
                    "A Cloudflare limitou as requisições. Aguarde antes de tentar novamente.",
                    429,
                )
            if response.status_code in {401, 403}:
                raise RuleError(
                    "CONFIGURATION_INVALID", "Token inválido ou sem permissão para a conta/operação.", 409
                )
            value = response.json()
            if not response.is_success or not value.get("success"):
                raise RuleError(
                    "PUBLICATION_REJECTED",
                    "A Cloudflare recusou a operação. Revise as permissões e o perfil no backoffice.",
                )
            return value.get("result")
        except (httpx.TimeoutException, httpx.NetworkError):
            if method != "GET":
                raise PublicationUncertain() from None
            raise RuleError(
                "DEPENDENCY_UNAVAILABLE", "Não foi possível consultar a Cloudflare.", 503
            ) from None
        except (ValueError, KeyError, TypeError):
            raise RuleError("DEPENDENCY_UNAVAILABLE", "Resposta inválida da Cloudflare.", 503) from None

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
        result = self.request(
            "POST",
            self.account_path + "/d1/database/" + database_id + "/query",
            json={"sql": sql, "params": params or []},
        )
        if not isinstance(result, list) or any(not item.get("success") for item in result):
            raise RuleError("MIGRATION_FAILED", "D1 não confirmou a consulta/migration.")
        return result

    def upload(self, name, database_id, artifact):
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
                {"type": "d1", "name": "DB", "id": database_id},
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
        result = self.request("PUT", self.account_path + "/workers/scripts/" + name, files=files)
        if not result or not result.get("deployment_id"):
            raise PublicationUncertain()
        return result["deployment_id"]

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
        with httpx.Client(
            transport=self.transport, timeout=self.policy["timeout_seconds"], follow_redirects=False
        ) as client:
            page = client.get(url)
            health = client.get(url + "/api/__viroucaseai_revision")
            api = client.get(url + "/api/health")
        try:
            if page.status_code != 200 or "text/html" not in page.headers.get("content-type", ""):
                raise ValueError()
            if (
                health.status_code != 200
                or health.json().get("revision_id") != revision
                or not health.json().get("database")
            ):
                raise ValueError()
            if api.status_code != 200 or "application/json" not in api.headers.get("content-type", ""):
                raise ValueError()
        except (ValueError, TypeError):
            raise RuleError(
                "PUBLICATION_VERIFICATION_FAILED",
                "Upload concluído, mas frontend, API ou D1 ainda não passaram na verificação externa.",
            ) from None
