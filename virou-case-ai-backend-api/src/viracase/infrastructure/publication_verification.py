import json
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.publication_build_diagnostics import BuildFailure
from viracase.infrastructure.publication_logging import emit, safe_text

VERIFICATION_FORMAT_VERSION = 2

PROBES = (
    ("frontend", "/"),
    ("revision", "/api/__viroucaseai_revision"),
    ("api", "/api/health"),
)
MESSAGES = {
    "PUBLICATION_FRONTEND_NOT_READY": "O frontend n\u00e3o respondeu com HTML v\u00e1lido dentro da janela de verifica\u00e7\u00e3o.",
    "PUBLICATION_REVISION_NOT_READY": "O endpoint da revis\u00e3o n\u00e3o confirmou o artefato esperado e o acesso ao D1.",
    "PUBLICATION_API_NOT_READY": "A API publicada n\u00e3o passou na verifica\u00e7\u00e3o HTTP e JSON.",
    "PUBLICATION_ACCESS_DENIED": "O endere\u00e7o p\u00fablico recusou o acesso. Confira Cloudflare Access e as regras de acesso ao Worker.",
    "PUBLICATION_REDIRECTED": "O teste p\u00fablico recebeu redirecionamento. Confira autentica\u00e7\u00e3o e roteamento; nenhum redirecionamento foi seguido.",
    "PUBLICATION_BINDINGS_MISMATCH": "Os bindings remotos n\u00e3o correspondem exatamente ao D1 DB e aos assets ASSETS autorizados.",
    "PUBLICATION_BINDINGS_NOT_READY": "Os bindings e a revis\u00e3o remotos n\u00e3o foram confirmados dentro da janela de verifica\u00e7\u00e3o.",
    "PUBLICATION_D1_PROBE_FAILED": "O D1 de diagn\u00f3stico n\u00e3o confirmou a escrita e leitura do teste operacional.",
}


class VerificationFailure(RuleError):
    def __init__(self, code, details):
        super().__init__(code, MESSAGES[code])
        self.diagnostic = details


def database_binding_matches(binding, database_id):
    if not isinstance(binding, dict) or binding.get("type") != "d1" or binding.get("name") != "DB":
        return False
    values = [binding[key] for key in ("database_id", "id") if binding.get(key) is not None]
    return bool(values) and all(value == database_id for value in values)


def binding_observation(settings, database_id, revision):
    if settings is None:
        return {"passed": False, "code": "SETTINGS_NOT_VISIBLE", "retryable": True}
    if not isinstance(settings, dict) or not isinstance(settings.get("bindings"), list):
        return {"passed": False, "code": "BINDINGS_RESPONSE_INVALID", "retryable": False}
    bindings = settings["bindings"]
    if any(not isinstance(b, dict) for b in bindings):
        return {"passed": False, "code": "BINDINGS_RESPONSE_INVALID", "retryable": False}
    d1 = [b for b in bindings if b.get("type") == "d1"]
    assets = [b for b in bindings if b.get("type") == "assets"]
    result = {
        "binding_count": len(bindings),
        "d1_matches": len(d1) == 1 and database_binding_matches(d1[0], database_id),
        "assets_matches": len(assets) == 1 and assets[0].get("name") == "ASSETS",
        "revision_matches": isinstance(settings.get("tags"), list) and revision in settings["tags"],
    }
    invalid = (
        len(d1) > 1 or len(assets) > 1 or len(bindings) != len(d1) + len(assets)
        or bool(d1) and not result["d1_matches"]
        or bool(assets) and not result["assets_matches"]
    )
    passed = not invalid and all(result[k] for k in ("d1_matches", "assets_matches", "revision_matches"))
    return {
        **result, "passed": passed,
        "code": "BINDINGS_CONFIRMED" if passed else "BINDINGS_MISMATCH" if invalid else "BINDINGS_NOT_VISIBLE",
        "retryable": not passed and not invalid,
    }


def confirm_bindings(gateway, name, database_id, revision, timeout_seconds, *, clock=None, sleep=None):
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    started = clock()
    deadline = started + min(30, max(5, timeout_seconds * 3))
    attempts = []
    for attempt in range(1, 6):
        observed = binding_observation(gateway.settings(name), database_id, revision)
        attempts.append({"attempt": attempt, **observed})
        emit("publication_bindings_observed", **attempts[-1])
        report = {"passed": observed["passed"], "attempts": attempts, "duration_ms": round((clock() - started) * 1000)}
        if observed["passed"]:
            return report
        delay = min(2 ** (attempt - 1), 8)
        if not observed["retryable"] or attempt == 5 or clock() + delay >= deadline:
            break
        sleep(delay)
    code = "PUBLICATION_BINDINGS_NOT_READY" if observed["retryable"] else "PUBLICATION_BINDINGS_MISMATCH"
    raise VerificationFailure(code, report)


class RemoteVerifier:
    def __init__(self, timeout_seconds, transport=None, before_request=None, observer=None, *, clock=None, sleep=None):
        self.timeout = float(timeout_seconds)
        if not 0 < self.timeout <= 120:
            raise ValueError("invalid_verification_timeout")
        self.transport = transport
        self.before_request, self.observer = before_request, observer
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.budget = max(90, min(180, self.timeout * 6))
        self.max_attempts = 16

    def verify(self, url, revision):
        self._validate_target(url)
        started = self.clock()
        deadline = started + self.budget
        report = {"passed": False, "budget_seconds": self.budget, "attempts": []}
        with httpx.Client(transport=self.transport, follow_redirects=False, timeout=self.timeout) as client:
            for attempt in range(1, self.max_attempts + 1):
                checks = []
                for name, path in PROBES:
                    if self.clock() >= deadline:
                        checks.append({"name": name, "path": path, "passed": False, "code": "PROBE_DEADLINE", "retryable": False})
                        continue
                    checks.append(self._observe(client, url, name, path, revision, deadline))
                passed = all(check["passed"] for check in checks)
                item = {"attempt": attempt, "passed": passed, "checks": checks, "elapsed_ms": round((self.clock() - started) * 1000)}
                report = {**report, "passed": passed, "attempts": [*report["attempts"], item], "duration_ms": item["elapsed_ms"]}
                emit("publication_http_probe", **item)
                if self.observer:
                    self.observer(report)
                if passed:
                    emit("publication_http_verified", attempts=attempt, duration_ms=report["duration_ms"])
                    return report
                failures = [check for check in checks if not check["passed"]]
                delay = min(2 ** (attempt - 1), 10)
                retry_after = max((c.get("retry_after_seconds", 0) for c in checks), default=0)
                delay = max(delay, retry_after)
                if (
                    any(not check["retryable"] for check in failures)
                    or attempt == self.max_attempts or self.clock() + delay >= deadline
                ):
                    break
                emit("publication_http_wait", attempt=attempt, delay_seconds=delay)
                self.sleep(delay)
        failure = next((c for c in failures if not c["retryable"]), failures[0])
        code = (
            "PUBLICATION_ACCESS_DENIED" if failure["code"] == "HTTP_ACCESS_DENIED"
            else "PUBLICATION_REDIRECTED" if failure["code"] == "HTTP_REDIRECT"
            else {"frontend": "PUBLICATION_FRONTEND_NOT_READY", "revision": "PUBLICATION_REVISION_NOT_READY", "api": "PUBLICATION_API_NOT_READY"}[failure["name"]]
        )
        report["failed_probe"] = failure["name"]
        report["failure_code"] = failure["code"]
        raise VerificationFailure(code, report)

    @staticmethod
    def _validate_target(url):
        try:
            parsed = urlsplit(url)
        except (TypeError, ValueError):
            raise RuleError("CONFIGURATION_INVALID", "Destino de verifica\u00e7\u00e3o inv\u00e1lido.") from None
        label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        if (
            parsed.scheme != "https" or parsed.username or parsed.password
            or not re.fullmatch(label + r"\." + label + r"\.workers\.dev", parsed.netloc)
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
        ):
            raise RuleError("CONFIGURATION_INVALID", "Destino de verifica\u00e7\u00e3o fora do dom\u00ednio workers.dev autorizado.")

    def _observe(self, client, url, name, path, revision, deadline):
        result = {"name": name, "path": path, "passed": False, "retryable": False}
        if self.before_request:
            self.before_request("GET")
        remaining = deadline - self.clock()
        if remaining <= 0:
            return {**result, "code": "PROBE_DEADLINE"}
        client.cookies.clear()
        started = self.clock()
        try:
            with client.stream(
                "GET", url.rstrip("/") + path,
                params={"__viroucaseai_probe": uuid4().hex},
                headers={"Cache-Control": "no-cache", "Accept": "text/html" if name == "frontend" else "application/json"},
                timeout=min(self.timeout, remaining),
            ) as response:
                status = response.status_code
                media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                result.update(status=status, content_type=media if media in {"text/html", "application/json", "text/plain"} else "other")
                ray = response.headers.get("cf-ray", "")
                if re.fullmatch(r"[a-fA-F0-9]{8,32}(?:-[A-Z]{3})?", ray):
                    result["cf_ray"] = ray
                retry_after = response.headers.get("retry-after", "")
                if status == 429 and re.fullmatch(r"[0-9]{1,6}", retry_after):
                    result["retry_after_seconds"] = min(int(retry_after), 86400)
                if status != 200:
                    result.update(
                        code="HTTP_ACCESS_DENIED" if status in {401, 403} else "HTTP_REDIRECT" if 300 <= status < 400 else "HTTP_NOT_READY",
                        retryable=status in {404, 408, 425, 429} or 500 <= status < 600,
                    )
                elif name == "frontend":
                    result.update(passed=media == "text/html", code="HTML_CONFIRMED" if media == "text/html" else "CONTENT_TYPE_MISMATCH")
                elif media != "application/json":
                    result["code"] = "CONTENT_TYPE_MISMATCH"
                else:
                    content = bytearray()
                    for chunk in response.iter_bytes(chunk_size=4096):
                        content.extend(chunk)
                        if len(content) > 65536:
                            result["code"] = "RESPONSE_TOO_LARGE"
                            break
                        if self.clock() >= deadline:
                            result["code"] = "PROBE_DEADLINE"
                            break
                    else:
                        result.update(self._json_result(name, bytes(content), revision))
        except httpx.TimeoutException:
            result.update(code="HTTP_TIMEOUT", retryable=True)
        except (httpx.NetworkError, httpx.RemoteProtocolError):
            result.update(code="HTTP_NETWORK_ERROR", retryable=True)
        except httpx.DecodingError:
            result.update(code="HTTP_ENCODING_INVALID", retryable=False)
        result["duration_ms"] = round((self.clock() - started) * 1000)
        return result

    @staticmethod
    def _json_result(name, content, revision):
        try:
            body = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            return {"code": "INVALID_JSON"}
        if not isinstance(body, dict):
            return {"code": "INVALID_JSON_SHAPE"}
        if name == "revision":
            if body.get("revision_id") != revision:
                return {"code": "REVISION_MISMATCH", "revision_matches": False, "retryable": True}
            if body.get("database") is not True:
                return {"code": "DATABASE_UNCONFIRMED", "revision_matches": True, "database_confirmed": False}
            return {"code": "REVISION_CONFIRMED", "passed": True, "revision_matches": True, "database_confirmed": True}
        if body.get("ok") is False or body.get("database") is False:
            return {"code": "API_UNHEALTHY"}
        return {"code": "API_CONFIRMED", "passed": True}


def failure_record(exc, stage, stack=None, secrets=()):
    record = {
        "code": exc.code if isinstance(exc, RuleError) else "PUBLICATION_VERIFICATION_FAILED",
        "message": safe_text(exc.message, secrets=secrets, limit=500) if isinstance(exc, RuleError)
        else "Falha inesperada na verifica\u00e7\u00e3o. Consulte os logs pelo identificador da opera\u00e7\u00e3o.",
        "stage": stage,
        "stack": stack,
        "exception_type": type(exc).__name__,
        "at": utcnow().isoformat(),
    }
    if isinstance(exc, VerificationFailure):
        record["details"] = exc.diagnostic
    if isinstance(exc, BuildFailure):
        record["build"] = exc.diagnostic
    provider = getattr(exc, "provider_diagnostic", None)
    if isinstance(provider, dict):
        record["provider"] = provider
    return record


def verification_summary(operation):
    if not operation:
        return None
    result = operation.result or {}
    failure = result.get("verification_failure")
    return {
        "id": operation.id,
        "state": operation.state,
        "stage": operation.progress_stage,
        "error_code": operation.error_code,
        "failed_stage": failure.get("stage") if isinstance(failure, dict) else None,
        "failure": failure,
        "cleanup": result.get("verification_cleanup"),
        "resources": result.get("verification_resources", []),
        "stack": result.get("verification_stack"),
        "stack_results": result.get("stack_results", []),
        "remote_verification": result.get("remote_verification"),
        "activity": result.get("activity", []),
        "temporary_resources": result.get("resource_policy") != "retain",
    }
