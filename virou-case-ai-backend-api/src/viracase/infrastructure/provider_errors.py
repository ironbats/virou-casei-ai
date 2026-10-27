import json
import logging
import re


log = logging.getLogger("viracase.provider")


class ProviderRejected(Exception):
    def __init__(self, reason, *, error_code="PROVIDER_REJECTED", diagnostic=None):
        super().__init__(reason)
        self.error_code = error_code
        self.diagnostic = diagnostic or {"category": reason}


def configuration_rejection(reason_code, *, error_code="PROVIDER_CONFIGURATION_INVALID"):
    allowed = {
        "AI_REGISTRY_UNAVAILABLE",
        "CREDENTIAL_STORE_UNAVAILABLE",
        "AI_CONFIGURATION_PENDING",
        "AI_PROVIDER_UNAVAILABLE",
        "NOT_FOUND",
    }
    return ProviderRejected(
        "configuration",
        error_code=error_code,
        diagnostic={
            "category": "PROVIDER_CONFIGURATION_INVALID",
            "stage": "request_settings",
            "reason_code": reason_code if reason_code in allowed else "AI_CONFIGURATION_UNAVAILABLE",
        },
    )


def rejection(status, raw):
    codes = {
        400: "PROVIDER_CONFIGURATION_INVALID",
        401: "PROVIDER_ACCESS_DENIED",
        403: "PROVIDER_ACCESS_DENIED",
        404: "PROVIDER_MODEL_UNAVAILABLE",
        413: "PROVIDER_REQUEST_TOO_LARGE",
        422: "PROVIDER_CONFIGURATION_INVALID",
        429: "PROVIDER_CAPACITY_EXCEEDED",
    }
    field = None
    try:
        error = json.loads(raw).get("error", {})
        message = str(error.get("message", "")).lower()
        # Only an explicitly rejected optional field permits one compatibility retry.
        invalid = re.search(r"not supported|unsupported|unknown (?:name|field)|unrecognized", message)
        if status == 400 and invalid:
            if "thinking" in message:
                field = "thinkingConfig"
            elif any(name in message for name in ("responseschema", "response_schema", "json_schema")):
                field = "responseSchema"
    except (ValueError, TypeError, AttributeError):
        pass
    diagnostic = {"http_status": status, "category": codes.get(status, "PROVIDER_REJECTED")}
    if field:
        diagnostic["unsupported_field"] = field
    return ProviderRejected("http_rejected", error_code=diagnostic["category"], diagnostic=diagnostic)


def report_rejection(operation_id, exc):
    log.warning(
        json.dumps({"event": "generation_provider_rejected", "operation_id": operation_id, **exc.diagnostic})
    )
