import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from viracase.domain.rules import RuleError
from viracase.infrastructure.integration_traces import TraceObserver, TraceStore
from viracase.infrastructure.models import IntegrationTracePayload, Operation
from viracase.infrastructure.observability import events
from viracase.infrastructure.workflow_logging import current_context, emit, workflow_context
from viracase.interfaces.api import create_app


@pytest.fixture
def diagnostic_records(monkeypatch):
    stream = io.StringIO()
    logger = logging.getLogger("viracase.workflow")
    monkeypatch.setattr(logger, "handlers", [logging.StreamHandler(stream)])
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "propagate", False)
    return lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_rule_error_logs_safe_code_route_and_existing_request_id(system, diagnostic_records):
    app = create_app(False, (system["config"], system["engine"], system["repo"], system["catalog"], None))
    secret = "private-request-and-exception-content-9f21"

    @app.get("/api/v1/projects/{pid}/diagnostic-probe")
    def diagnostic_probe(pid: str):
        raise RuleError("PREPARATION_INVALID_RESPONSE", secret, 409)

    response = TestClient(app).get(f"/api/v1/projects/{uuid4()}/diagnostic-probe", params={"private": secret})
    assert response.status_code == 409
    identity = response.json()["error"]["request_id"]
    assert identity == response.headers["X-Request-ID"]
    record = next(
        (row for row in diagnostic_records() if row.get("error_code") == "PREPARATION_INVALID_RESPONSE"),
        None,
    )
    assert record is not None, "A recusa HTTP não produziu diagnóstico operacional com o código da causa."
    assert record["request_id"] == identity
    assert record["route"] == "/api/v1/projects/{pid}/diagnostic-probe"
    assert record["phase"] == "api"
    assert secret not in json.dumps(diagnostic_records())


def test_unexpected_http_failure_has_structural_cause_without_exception_values(system, diagnostic_records):
    app = create_app(False, (system["config"], system["engine"], system["repo"], system["catalog"], None))
    secret = "Bearer synthetic-private-token-and-prompt"

    @app.get("/api/v1/diagnostic-failure")
    def diagnostic_failure():
        try:
            raise ValueError(secret)
        except ValueError as cause:
            raise RuntimeError("private-code-body") from cause

    response = TestClient(app).get("/api/v1/diagnostic-failure")
    assert response.status_code == 503
    record = next((row for row in diagnostic_records() if row.get("frames")), None)
    assert record is not None, "A falha HTTP não preservou fase/frames seguros para diagnóstico."
    assert record["request_id"] == response.headers["X-Request-ID"]
    assert record["error_code"] == "DEPENDENCY_UNAVAILABLE"
    assert record["exception_type"] == "RuntimeError"
    assert any(cause["exception_type"] == "ValueError" for cause in record["causes"])
    assert any(frame["function"] == "diagnostic_failure" for frame in record["frames"])
    serialized = json.dumps(diagnostic_records())
    assert secret not in serialized and "private-code-body" not in serialized


def test_disabled_payload_capture_still_logs_phase_and_safe_failure_metadata(system, diagnostic_records):
    actor = system["users"][0]
    project = system["repo"].create_project(
        actor, "diagnostic-project", {"name": "Diagnóstico", "objective": "Contexto sintético"}
    )
    context = {
        "trace_id": str(uuid4()),
        "request_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "account_id": actor,
        "project_id": project["id"],
        "stage": "preparation",
        "provider": "openai",
        "model": "gpt-6-luna",
        "attempt": 1,
        "template_versions": [
            {"attachment_id": "autorizacao", "source_version_id": str(uuid4()), "attachment_revision": 2}
        ],
    }
    secret = "private-template-field-and-prompt-d1ea"
    TraceStore(system["repo"]).put(
        context,
        "normalized",
        "invalid",
        {"normalized": {"message": secret, "response": secret}},
        {"error_code": "PREPARATION_FIELD_TYPE", "unsafe_detail": secret},
    )
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(IntegrationTracePayload)) == 0
    record = next(
        (row for row in diagnostic_records() if row.get("error_code") == "PREPARATION_FIELD_TYPE"), None
    )
    assert record is not None, "Captura desabilitada ocultou também o diagnóstico operacional seguro."
    assert record["capture_state"] == "disabled"
    assert record["operation_id"] == context["operation_id"]
    assert record["request_id"] == context["request_id"]
    assert record["provider"] == "openai"
    assert record["template_versions"] == context["template_versions"]
    assert secret not in json.dumps(diagnostic_records())


def test_autofill_dispatcher_is_visible_in_the_admin_preparation_feed(system):
    repo = system["repo"]
    actor = system["users"][0]
    project = repo.create_project(
        actor, "diagnostic-feed", {"name": "Fila", "objective": "Preenchimento observável"}
    )
    with repo.sessions.begin() as db:
        operation = Operation(
            account_id=actor,
            project_id=project["id"],
            type="template_autofill",
            state="failed",
            error_code="TEMPLATE_UNAVAILABLE",
            payload={"prompt": "private-source-not-for-monitor"},
        )
        db.add(operation)
        db.flush()
        identity = operation.id
    records = events(repo, system["admin"], phase="preparation", correlation=identity)["items"]
    assert records, "O dispatcher de autofill desaparece do feed administrativo de preparação."
    assert records[0]["error_code"] == "TEMPLATE_UNAVAILABLE"
    assert "private-source-not-for-monitor" not in json.dumps(records)


def test_diagnostics_allow_only_metadata_without_serializing_exception_or_payload(diagnostic_records):
    secret = "SYNTHETIC_SECRET_NEVER_LOGGED"

    class UnsafeString(Exception):
        def __str__(self):
            raise AssertionError("O logger não pode serializar mensagens de exceção.")

    operation = SimpleNamespace(
        id=str(uuid4()),
        type="template_preparation",
        state="running",
        billing_status="reserved",
        account_id=str(uuid4()),
        project_id=str(uuid4()),
        payload={
            "http_request_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "context": [{"content": secret}],
            "items": [
                {
                    "attachment_id": "autorizacao",
                    "source_version_id": str(uuid4()),
                    "attachment_revision": 2,
                    "value": secret,
                }
            ],
        },
        policy={"provider": "openai", "model": "gpt-6-luna", "credential": secret},
    )
    try:
        raise UnsafeString(secret)
    except UnsafeString as error:
        error.error_code = secret
        error.diagnostic = {"http_status": 429, "reason_code": "PROVIDER_CAPACITY_EXCEEDED", "body": secret}
        emit(
            "preparation_failed",
            "error",
            operation=operation,
            error=error,
            secrets=(secret,),
            phase="provider",
            prompt=secret,
            payload=operation.payload,
            code=secret,
            token=secret,
            source=secret,
            evidence=secret,
            provider_request_id=secret,
            actual_brl=Decimal("0.0012"),
            input_tokens=10**5000,
        )
    record = diagnostic_records()[0]
    assert record["operation_id"] == operation.id
    assert record["workflow_request_id"] == operation.payload["request_id"]
    assert record["request_id"] == operation.payload["http_request_id"]
    assert record["model"] == "gpt-6-luna"
    assert record["exception_type"] == "UnsafeString"
    assert record["error_code"] == "UNEXPECTED_ERROR"
    assert record["http_status"] == 429
    assert record["frames"]
    assert record["actual_brl"] == "0.0012"
    assert "input_tokens" not in record
    assert secret not in json.dumps(diagnostic_records())
    assert not {"prompt", "payload", "code", "token", "source", "evidence", "credential"} & set(record)


def test_diagnostic_context_is_isolated_between_concurrent_requests(diagnostic_records):
    gate = Barrier(2)
    identities = [str(uuid4()), str(uuid4())]

    def process(identity):
        assert current_context() == {}
        with workflow_context(request_id=identity, phase="api", payload="private"):
            gate.wait(timeout=5)
            emit("request_probe", operation_id=identity)
        assert current_context() == {}

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(process, identities))
    records = diagnostic_records()
    assert len(records) == 2
    assert {record["request_id"] for record in records} == set(identities)
    assert all(record["request_id"] == record["operation_id"] for record in records)
    assert "private" not in json.dumps(records)


def test_diagnostic_failures_do_not_replace_business_processing(diagnostic_records, monkeypatch):
    from viracase.infrastructure import workflow_logging

    class DetachedMetadata:
        @property
        def payload(self):
            raise RuntimeError("private detached metadata")

    emit("metadata_probe", operation=DetachedMetadata(), phase="settle")
    assert diagnostic_records()[0]["metadata_state"] == "unavailable"
    assert "private detached metadata" not in json.dumps(diagnostic_records())

    def unavailable(*args, **kwargs):
        raise OSError("private collector details")

    monkeypatch.setattr(workflow_logging._log, "log", unavailable)
    emit("collector_probe", phase="settle", error_code="COST_DISCREPANCY")


def test_provider_transport_metadata_is_logged_without_headers_body_or_query(system, diagnostic_records):
    actor = system["users"][0]
    project = system["repo"].create_project(
        actor, "wire-diagnostic", {"name": "Wire", "objective": "Teste sintético"}
    )
    context = {
        "trace_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "account_id": actor,
        "project_id": project["id"],
        "stage": "preparation",
        "provider": "openai",
        "model": "gpt-6-luna",
    }
    secret = "synthetic-wire-secret-5941"
    observer = TraceObserver(system["repo"], context)
    request = httpx.Request(
        "POST",
        "https://api.synthetic.test/responses?token=" + secret,
        headers={"Authorization": "Bearer " + secret},
        json={"input": secret},
    )
    observer.request(request)
    observer.response(
        httpx.Response(429, headers={"x-request-id": secret}, request=request),
        json.dumps({"error": {"message": secret}}).encode(),
    )
    records = diagnostic_records()
    assert any(row.get("status") == "pending" and row.get("phase") == "provider" for row in records)
    assert any(row.get("http_status") == 429 and row.get("status") == "rejected" for row in records)
    assert secret not in json.dumps(records)
    assert "api.synthetic.test" not in json.dumps(records)
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(IntegrationTracePayload)) == 0


def test_early_http_rejection_has_request_correlation_and_preserves_origin_control(
    system, diagnostic_records
):
    app = create_app(False, (system["config"], system["engine"], system["repo"], system["catalog"], None))
    response = TestClient(app).post(
        "/api/v1/auth/login",
        headers={"Origin": "https://untrusted.invalid"},
        json={"login": "private-login", "password": "private-password"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORIGIN_DENIED"
    identity = response.json()["error"]["request_id"]
    assert response.headers["X-Request-ID"] == identity
    assert any(
        row.get("request_id") == identity and row.get("error_code") == "ORIGIN_DENIED"
        for row in diagnostic_records()
    )
    serialized = json.dumps(diagnostic_records())
    assert "private-login" not in serialized and "private-password" not in serialized
    assert "untrusted.invalid" not in serialized


def test_request_validation_diagnostic_excludes_rejected_input(system, diagnostic_records):
    app = create_app(False, (system["config"], system["engine"], system["repo"], system["catalog"], None))

    @app.get("/api/v1/diagnostic-validation")
    def diagnostic_validation(value: int):
        return {"value": value}

    secret = "private-invalid-input"
    response = TestClient(app).get("/api/v1/diagnostic-validation", params={"value": secret})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert any(record.get("error_code") == "VALIDATION_ERROR" for record in diagnostic_records())
    assert secret not in json.dumps(diagnostic_records())
