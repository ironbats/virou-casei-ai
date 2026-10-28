import json
import logging
import re
import sys
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

_context = ContextVar("publication_context", default={})
_lock = Lock()
_log = logging.getLogger("viracase.publication")


def safe_text(value, secrets=(), limit=2048):
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    for secret in sorted((s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)", "[REDACTED]", text, flags=re.S)
    text = re.sub(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(?:bearer\s+|basic\s+)?[^\s\"',;]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)\b(bearer|basic)\s+[^\s\"',;]+", r"\1 [REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|cookie|credential)[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", r"\1[REDACTED]", text)
    text = re.sub(r"([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@\s]+@", r"\1[REDACTED]@", text, flags=re.I)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?", "[REDACTED]", text)
    text = re.sub(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_-])", "[REDACTED]", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
    return text[:limit]


def configure_publication_logging():
    with _lock:
        if not _log.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            _log.addHandler(handler)
        _log.setLevel(logging.INFO)
        _log.propagate = False


@contextmanager
def publication_context(**values):
    token = _context.set({**_context.get(), **values})
    try:
        yield
    finally:
        _context.reset(token)


def emit(event, level="info", **fields):
    configure_publication_logging()
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "publication",
        "level": level,
        "event": event,
        **_context.get(),
        **fields,
    }
    correlation = record.get("operation_id") or record.get("request_id") or record.get("diagnostic_id")
    if correlation:
        record["correlation_id"] = correlation
    _log.log(getattr(logging, level.upper(), logging.INFO), json.dumps(record, ensure_ascii=False, default=str))


def exception_fields(exc):
    return {
        "exception_type": type(exc).__name__,
        "error_code": getattr(exc, "code", "UNEXPECTED_ERROR"),
        "frames": [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)[-8:]
        ],
    }
