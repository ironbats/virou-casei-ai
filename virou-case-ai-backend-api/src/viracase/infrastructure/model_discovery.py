import json
import logging
import re
from time import monotonic

import httpx
from google.auth.exceptions import GoogleAuthError

from viracase.domain.rules import RuleError
from viracase.infrastructure.model_catalog import CATALOG_SOURCES, CATALOG_VERSION, local_models

MODEL_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,160}$")
LOGGER = logging.getLogger(__name__)
MESSAGES = {
    "AUTHENTICATION_FAILED": "O provedor não aceitou a autenticação. Confira a credencial configurada.",
    "PERMISSION_DENIED": "O provedor negou acesso ao catálogo. Confira as permissões da credencial e do projeto.",
    "SERVICE_DISABLED": "A API do provedor está desativada no projeto. Habilite a API Vertex AI no Google Cloud.",
    "USER_PROJECT_DENIED": "A credencial não pode usar o projeto informado para quota. Confira a permissão serviceusage.services.use nesse projeto.",
    "BILLING_DISABLED": "O faturamento do projeto está desativado. Confira a conta de faturamento no Google Cloud.",
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": "O token não possui o escopo exigido pelo provedor.",
    "CATALOG_NOT_FOUND": "O catálogo não está disponível nesse endpoint ou localização. Confira a região da conexão.",
    "QUOTA_EXCEEDED": "O provedor limitou a consulta. Confira a quota e tente novamente mais tarde.",
    "INVALID_REQUEST": "O provedor rejeitou os parâmetros da consulta. Confira projeto e localização.",
    "PROVIDER_UNAVAILABLE": "O catálogo do provedor está indisponível no momento. Tente novamente mais tarde.",
    "NETWORK_ERROR": "Não foi possível conectar ao provedor. Confira a saída HTTPS e tente novamente.",
    "TIMEOUT": "A consulta ao provedor excedeu o tempo limite. Tente novamente mais tarde.",
    "INVALID_RESPONSE": "O provedor retornou um catálogo em formato inesperado.",
    "RESPONSE_LIMIT": "A resposta do catálogo excedeu o limite de tamanho.",
    "INCOMPLETE_CATALOG": "A paginação do catálogo não foi concluída. Os modelos já recebidos foram preservados.",
    "CREDENTIAL_STORE_UNAVAILABLE": "Não foi possível ler a credencial protegida. Verifique o armazenamento de credenciais do backend.",
}


class DiscoveryFailure(Exception):
    def __init__(self, code, status=None, stage="catalog"):
        self.code, self.status, self.stage = code, status, stage
        super().__init__(code)


def failure_from_response(status, data):
    code = {
        400: "INVALID_REQUEST",
        401: "AUTHENTICATION_FAILED",
        403: "PERMISSION_DENIED",
        404: "CATALOG_NOT_FOUND",
        429: "QUOTA_EXCEEDED",
    }.get(status, "PROVIDER_UNAVAILABLE")
    error = data.get("error") if isinstance(data, dict) else None
    if status == 403 and isinstance(error, dict):
        details = error.get("details", [])
        if isinstance(details, list):
            for detail in details:
                if isinstance(detail, dict) and detail.get("reason") in (
                    "SERVICE_DISABLED",
                    "USER_PROJECT_DENIED",
                    "BILLING_DISABLED",
                    "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                ):
                    code = detail["reason"]
                    break
    return DiscoveryFailure(code, status)


def read_catalog(client, url, headers, params):
    with client.stream("GET", url, headers=headers, params=params) as response:
        raw = bytearray()
        limit = 4_000_000 if response.status_code == 200 else 65_536
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > limit:
                if response.status_code != 200:
                    raise failure_from_response(response.status_code, {})
                raise DiscoveryFailure("RESPONSE_LIMIT", 200)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            if response.status_code != 200:
                raise failure_from_response(response.status_code, {}) from None
            raise DiscoveryFailure("INVALID_RESPONSE", 200) from None
        if response.status_code != 200:
            raise failure_from_response(response.status_code, data)
        if not isinstance(data, dict):
            raise DiscoveryFailure("INVALID_RESPONSE", 200)
        return data


def catalog_result(provider, remote=(), warning=None, source="local"):
    items = {item["id"]: item for item in local_models(provider)}
    for item in remote:
        items[item["id"]] = {**items.get(item["id"], {}), **item}
    return {
        "items": sorted(items.values(), key=lambda item: item["id"]),
        "source": source,
        "provider_count": len(remote),
        "catalog_version": CATALOG_VERSION,
        "reference_url": CATALOG_SOURCES[provider],
        "warning": warning,
    }


def discover_models(connection, headers_factory, client_factory):
    items, seen = {}, set()
    stage = "authentication"
    started = monotonic()
    try:
        headers = headers_factory(connection)
        stage = "catalog"
        vertex = connection.provider == "gemini" and connection.auth_mode == "service_account"
        if connection.provider == "openai":
            url, collection, params = "https://api.openai.com/v1/models", "data", {}
        elif vertex:
            location = connection.options["location"]
            host = "us-central1" if location == "global" else location
            url = f"https://{host}-aiplatform.googleapis.com/v1beta1/publishers/google/models"
            collection, params = "publisherModels", {"pageSize": 100, "listAllVersions": "true"}
        else:
            url = "https://generativelanguage.googleapis.com/v1beta/models"
            collection, params = "models", {"pageSize": 1000}
        with client_factory() as client:
            for _ in range(100):
                if monotonic() - started > 40:
                    raise DiscoveryFailure("TIMEOUT")
                data = read_catalog(client, url, headers, params)
                entries = data.get(collection, [])
                if not isinstance(entries, list) or (collection == "data" and collection not in data):
                    raise DiscoveryFailure("INVALID_RESPONSE", 200)
                if collection not in data and any(k not in ("nextPageToken",) for k in data):
                    raise DiscoveryFailure("INVALID_RESPONSE", 200)
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise DiscoveryFailure("INVALID_RESPONSE", 200)
                    name = entry.get("id" if collection == "data" else "name")
                    if not isinstance(name, str):
                        raise DiscoveryFailure("INVALID_RESPONSE", 200)
                    model = name if collection == "data" else name.split("/")[-1]
                    if not MODEL_ID.fullmatch(model):
                        continue
                    display = entry.get("displayName")
                    methods = entry.get("supportedGenerationMethods", [])
                    items[model] = {
                        "id": model,
                        "name": display[:160] if isinstance(display, str) and display else model,
                        "methods": [x for x in methods if isinstance(x, str) and len(x) <= 100][:20]
                        if isinstance(methods, list)
                        else [],
                        "source": "provider",
                    }
                    if len(items) > 10_000:
                        raise DiscoveryFailure("INCOMPLETE_CATALOG")
                token = data.get("nextPageToken")
                if not token:
                    return catalog_result(connection.provider, list(items.values()), source="provider")
                if not isinstance(token, str) or len(token) > 4096 or token in seen or collection == "data":
                    raise DiscoveryFailure("INCOMPLETE_CATALOG")
                seen.add(token)
                params["pageToken"] = token
            raise DiscoveryFailure("INCOMPLETE_CATALOG")
    except DiscoveryFailure as error:
        failure = error
    except httpx.TimeoutException:
        failure = DiscoveryFailure("TIMEOUT", stage=stage)
    except httpx.HTTPError:
        failure = DiscoveryFailure("NETWORK_ERROR", stage=stage)
    except GoogleAuthError:
        failure = DiscoveryFailure("AUTHENTICATION_FAILED", stage="authentication")
    except (ValueError, KeyError, TypeError):
        failure = DiscoveryFailure(
            "AUTHENTICATION_FAILED" if stage == "authentication" else "INVALID_RESPONSE", stage=stage
        )
    except RuleError as error:
        if error.code != "CREDENTIAL_STORE_UNAVAILABLE":
            raise
        failure = DiscoveryFailure(error.code, stage="authentication")
    LOGGER.warning(
        "ai_model_discovery_fallback provider=%s code=%s stage=%s upstream_status=%s partial_count=%s",
        connection.provider,
        failure.code,
        failure.stage,
        failure.status,
        len(items),
    )
    warning = {
        "code": failure.code,
        "message": MESSAGES[failure.code],
        "stage": failure.stage,
        "upstream_status": failure.status,
    }
    return catalog_result(connection.provider, list(items.values()), warning, "fallback")
