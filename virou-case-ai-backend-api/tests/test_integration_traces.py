import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_guided_build import guided as guided
from test_guided_build import save_request

from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.credentials import initialize_key
from viracase.infrastructure.feature_policies import FeaturePolicies
from viracase.infrastructure.integration_traces import (
    PROTECTED,
    TraceStore,
    observe_integration,
    sanitized,
)
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import (
    Audit,
    IntegrationTrace,
    IntegrationTracePayload,
)
from viracase.infrastructure.providers import HttpModelGateway, ProviderUncertain
from viracase.infrastructure.recommendations import Recommendations
from viracase.interfaces.api import create_app
from viracase.interfaces.feature_schemas import BuildPolicy


def context(system, **extra):
    project = system["repo"].create_project(
        system["users"][0],
        "trace-project",
        {"name": "Trace local", "objective": "Projeto sintético para captura protegida"},
    )
    return {
        "trace_id": "journey-test",
        "operation_id": "operation-test",
        "account_id": system["users"][0],
        "project_id": project["id"],
        "stage": "preparation",
        "provider": "openai",
        "model": "gpt-6-luna",
        "attempt": 1,
        "identifiers": ["sdd", "objective"],
        **extra,
    }


def enable(system, **overrides):
    policies = FeaturePolicies(system["repo"], system["catalog"])
    current = policies.current("integration_capture")
    policies.save(
        system["admin"],
        "integration_capture",
        "enable-capture",
        current["version"],
        {
            "settings": {**current["settings"], "capture_enabled": True, **overrides},
            "reason": "Captura protegida para verificação local",
        },
    )
    policies.save(
        system["admin"],
        "payload_access",
        "grant",
        0,
        {"admin_ids": [system["admin"]], "reason": "Autorizar inspeção protegida de testes"},
    )
    return TraceStore(system["repo"])


def test_disabled_capture_and_specific_permission_are_explicit_and_audited(system):
    store = TraceStore(system["repo"])
    identity = store.put(context(system), "exchange", "received", {"request": {"input": "private text"}})
    listing = store.listing(system["admin"])
    assert not listing["can_inspect_payloads"]
    assert listing["items"][0]["capture_state"] == "disabled"
    assert not listing["items"][0]["available_sections"]
    with pytest.raises(RuleError) as denied:
        store.payload(system["admin"], identity, "request")
    assert denied.value.code == "PAYLOAD_ACCESS_DENIED"
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(IntegrationTracePayload)) == 0
        assert db.scalar(select(Audit).where(Audit.action == "trace:read:denied"))
    with pytest.raises(RuleError):
        store.listing(system["users"][0])


def test_recursive_sanitization_protects_free_text_keys_and_credentials_before_storage(system):
    store = enable(system)
    private = "private-user-content-9a6f"
    secret = system["config"].openai_api_key
    payload = {
        "model": "gpt-6-luna",
        "input": private,
        "headers": {"Authorization": "Bearer " + secret, "cookie": "secret-cookie"},
        "items": [
            {
                "attachment_id": "sdd",
                "field_id": "objective",
                "final_value": private,
                "state": "applied",
                "nested-" + private: secret,
            }
        ],
        "usage": {"input_tokens": 15, "output_tokens": 2},
    }
    identity = store.put(context(system), "fields", "applied", {"fields": payload})
    result = store.payload(system["admin"], identity, "fields")
    assert result["capture_state"] == "protected" and result["redacted_paths"]
    assert result["payload"]["items"][0]["field_id"] == "objective"
    assert result["payload"]["items"][0]["final_value"] == PROTECTED
    with system["repo"].sessions() as db:
        stored = json.dumps([row.content for row in db.scalars(select(IntegrationTracePayload))])
    assert private not in stored and secret not in stored and "secret-cookie" not in stored
    assert private not in json.dumps(store.listing(system["admin"]))
    assert private not in json.dumps(store.timeline(system["admin"], identity))


def test_copy_rechecks_permission_and_records_each_read_or_copy(system):
    store = enable(system)
    identity = store.put(
        context(system),
        "exchange",
        "received",
        {"request": {"body": {"input": "private", "model": "gpt-6-luna"}}},
    )
    read = store.payload(system["admin"], identity, "request")
    assert store.payload(system["admin"], identity, "request", copying=True) == read
    policies = FeaturePolicies(system["repo"])
    policies.save(
        system["admin"],
        "payload_access",
        "revoke",
        1,
        {"admin_ids": [], "reason": "Revogar autorização imediatamente"},
    )
    with pytest.raises(RuleError) as denied:
        store.payload(system["admin"], identity, "request", copying=True)
    assert denied.value.status == 403
    with system["repo"].sessions() as db:
        actions = list(db.scalars(select(Audit.action).where(Audit.resource_id == identity)))
    assert actions == ["trace:read", "trace:copy", "trace:copy:denied"]


def test_expiry_physically_deletes_payload_and_project_purge_clears_content(system):
    store = enable(system)
    expired = store.put(context(system), "exchange", "received", {"request": {"input": "old private"}})
    with system["repo"].sessions.begin() as db:
        row = db.get(IntegrationTrace, expired)
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.get(IntegrationTracePayload, (expired, "request")).expires_at = row.expires_at
    store.prune()
    result = store.payload(system["admin"], expired, "request")
    assert result["capture_state"] == "expired" and result["payload"] is None
    current = store.put(
        context(system),
        "fields",
        "applied",
        {"fields": {"items": [{"field_id": "objective", "final_value": "private"}]}},
    )
    with system["repo"].sessions.begin() as db:
        store.purge_project(db, project_id=context(system)["project_id"])
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(IntegrationTracePayload)) == 0
        assert db.get(IntegrationTrace, current).project_id is None
    assert store.payload(system["admin"], current, "fields")["capture_state"] == "unavailable"


def test_truncation_keeps_valid_json_and_operation_quota_is_enforced(system):
    store = enable(system, max_payload_bytes=1024, max_operation_bytes=4096)
    identifiers = ["field_" + str(index) + "_" + "x" * 80 for index in range(30)]
    value = {"items": [{"field_id": name, "state": "suggested", "value": "private"} for name in identifiers]}
    identity = store.put(
        context(system, identifiers=identifiers), "normalized", "ready", {"normalized": value}
    )
    result = store.payload(system["admin"], identity, "normalized")
    assert result["capture_state"] == "truncated" and result["truncated_paths"]
    assert json.loads(json.dumps(result["payload"])) == result["payload"]
    for _ in range(15):
        store.put(context(system, identifiers=identifiers), "normalized", "ready", {"normalized": value})
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.sum(IntegrationTracePayload.size_bytes))) <= 4096
        assert db.scalar(select(IntegrationTrace.id).where(IntegrationTrace.capture_state == "unavailable"))


@pytest.mark.parametrize("provider,model", [("openai", "gpt-6-luna"), ("gemini", "gemini-3.5-flash-lite")])
def test_real_http_adapter_boundary_captures_serialized_request_and_received_response(
    system, provider, model
):
    store = enable(system)
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        if provider == "openai":
            body = {
                "id": "response-real-boundary",
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"items":[]}'}]}],
                "usage": {"input_tokens": 12, "output_tokens": 2},
            }
        else:
            body = {
                "responseId": "response-real-boundary",
                "candidates": [{"content": {"parts": [{"text": '{"items":[]}'}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 2},
            }
        return httpx.Response(200, json=body, headers={"x-request-id": "request-observed"})

    policy = {**system["policy"], "provider": provider, "model": model, "template_preparation_contract": 1}
    gateway = HttpModelGateway(system["config"], httpx.MockTransport(handle))
    with observe_integration(system["repo"], context(system, provider=provider, model=model)):
        result = gateway.generate(policy, "private prompt exactly sent")
    assert result.usage == {"input": 12, "cached": 0, "output": 2}
    assert len(seen) == 1
    metadata = store.listing(system["admin"])["items"][0]
    assert metadata["http_status"] == 200 and metadata["provider_request_id"] == "request-observed"
    assert metadata["status"] == "received"
    assert "?" not in metadata["endpoint"]
    request = store.payload(system["admin"], metadata["id"], "request")["payload"]["body"]
    response = store.payload(system["admin"], metadata["id"], "response")["payload"]
    if provider == "openai":
        assert seen[0]["input"] == "private prompt exactly sent"
        assert seen[0]["text"]["format"]["schema"]["properties"]["items"]
        assert request["input"] == PROTECTED and request["model"] == model
        assert response["usage"]["input_tokens"] == 12
    else:
        assert seen[0]["contents"][0]["parts"][0]["text"] == "private prompt exactly sent"
        assert request["contents"][0]["parts"][0]["text"] == PROTECTED
        assert response["usageMetadata"]["promptTokenCount"] == 12
    assert "private prompt exactly sent" not in json.dumps(request)


def test_jev_http_boundary_keeps_wire_payloads_separate_from_normalized_and_protects_secrets(system, guided):
    flow, actor, project = guided
    data = save_request(flow, actor, project, False)
    private_objective = "Objetivo privado sintético Jev 7f4d"
    data["objective"] = private_objective
    data["message_text"] = private_objective
    flow.save(actor, project["id"], "private-objective", 1, data)
    initialize_key(system["config"].credential_key_path, system["repo"].sessions)
    registry = Integrations(system["repo"])
    policy = BuildPolicy(recommendation_enabled=True).model_dump()
    secret, private_response = "synthetic-jev-capture-secret", "Resposta privada sintética Jev 81ae"
    registry.save(
        system["admin"],
        "jev",
        "config",
        0,
        {"settings": policy, "token": secret, "reason": "Captura local da fronteira HTTP Jev"},
    )
    registry.validate(system["admin"], "jev", 1, "Validação local de teste")
    registry.activate(system["admin"], "jev", "activate", 1, "Ativação local de teste")
    store = enable(system)
    before = system["repo"].wallet(actor, True)
    calls = []

    def handler(request):
        body = json.loads(request.content)
        assert request.method == "POST" and str(request.url) == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["authorization"] == "Bearer " + secret
        assert body["state"] == {"objective": private_objective}
        assert body["model"] == policy["model"]
        assert "planning" in body["questions"]
        response_body = {
            "model": policy["model"],
            "answers": {
                category: {
                    "type": "choice",
                    "choice": "0" if category == "planning" else "none",
                    "confidence": 0.97,
                }
                for category in body["questions"]
            },
            "usage": {"input_tokens": 37, "output_tokens": 9, "total_tokens": 46},
            "secret": secret,
            "message": private_response,
        }
        raw_response = json.dumps(response_body, ensure_ascii=False, indent=2).encode()
        calls.append({"request": request.content, "response": raw_response})
        return httpx.Response(
            200,
            content=raw_response,
            headers={"content-type": "application/json", "x-request-id": "jev-known-request"},
        )

    recommender = Recommendations(flow, httpx.MockTransport(handler))
    request = {"expected_draft_revision": 2}
    result = recommender.recommend(actor, project["id"], "prepare", request)
    assert result["source"] == "jev" and result["status"] == "ready" and len(result["items"]) == 1
    assert recommender.recommend(actor, project["id"], "prepare", request) == result
    assert len(calls) == 1 and system["repo"].wallet(actor, True) == before
    feed = store.listing(system["admin"], correlation=result["recommendation_id"], phase="recommendation")
    assert feed["can_inspect_payloads"] and len(feed["items"]) == 2
    exchange = next(item for item in feed["items"] if item["direction"] == "exchange")
    normalized = next(item for item in feed["items"] if item["direction"] == "normalized")
    assert exchange["id"] != normalized["id"] and normalized["parent_span_id"] == exchange["id"]
    assert exchange["trace_id"] == normalized["trace_id"] == result["recommendation_id"]
    assert exchange["available_sections"] == ["request", "response"]
    assert normalized["available_sections"] == ["normalized"]
    assert exchange["provider"] == "jev" and exchange["http_status"] == 200
    assert exchange["provider_request_id"] == "jev-known-request"
    assert exchange["endpoint"] == "https://api.typesafe.ai/v1/systemone"
    captured_request = store.payload(system["admin"], exchange["id"], "request")
    captured_response = store.payload(system["admin"], exchange["id"], "response")
    captured_normalized = store.payload(system["admin"], normalized["id"], "normalized")
    sent, received = json.loads(calls[0]["request"]), json.loads(calls[0]["response"])
    request_body, response_body = captured_request["payload"]["body"], captured_response["payload"]
    assert captured_request["payload"]["method"] == "POST"
    assert captured_request["payload"]["headers"] == {"content-type": "application/json"}
    assert set(request_body) == set(sent)
    assert request_body["model"] == sent["model"]
    assert request_body["state"]["objective"] == PROTECTED
    assert set(request_body["questions"]) == set(sent["questions"])
    for category, question in sent["questions"].items():
        assert set(request_body["questions"][category]["criteria"]) == set(question["criteria"])
        assert response_body["answers"][category]["choice"] == received["answers"][category]["choice"]
        assert response_body["answers"][category]["confidence"] == received["answers"][category]["confidence"]
    assert response_body["usage"] == received["usage"]
    assert response_body["secret"] == PROTECTED and response_body["message"] == PROTECTED
    assert "$.body.state.objective" in captured_request["redacted_paths"]
    assert {"$.secret", "$.message"} <= set(captured_response["redacted_paths"])
    assert captured_normalized["payload"] == {
        "state": "ready",
        "selected": [item["template_id"] for item in result["items"]],
        "usage": received["usage"],
        "error": {"code": None},
    }
    assert "answers" not in captured_normalized["payload"]
    with system["repo"].sessions() as db:
        stored = json.dumps(
            [row.content for row in db.scalars(select(IntegrationTracePayload))], ensure_ascii=False
        )
    assert all(value not in stored for value in (secret, private_objective, private_response))
    assert all(
        value not in json.dumps(feed, ensure_ascii=False)
        for value in (secret, private_objective, private_response)
    )


def test_timeout_and_non_json_response_do_not_fabricate_payload_or_redispatch(system):
    store = enable(system)
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic private timeout", request=request)

    gateway = HttpModelGateway(system["config"], httpx.MockTransport(handle))
    with observe_integration(system["repo"], context(system)), pytest.raises(ProviderUncertain):
        gateway.generate(system["policy"], "private input")
    assert len(calls) == 1
    row = store.listing(system["admin"])["items"][0]
    assert row["status"] == "uncertain" and row["available_sections"] == ["request"]
    assert store.payload(system["admin"], row["id"], "response")["capture_state"] == "not_captured"


def test_http_admin_permissions_policies_filters_and_copy_use_real_contracts(system):
    dependencies = (system["config"], system["engine"], system["repo"], system["catalog"], None)
    admin = TestClient(create_app(True, dependencies))
    origin = system["config"].admin_origin
    login = admin.post(
        "/admin-api/v1/auth/login",
        json={"login": "operator", "password": "test-operator-password"},
        headers={"Origin": origin},
    )
    assert login.status_code == 200
    headers = {
        "Origin": origin,
        "X-CSRF-Token": login.json()["csrf_token"],
        "Idempotency-Key": "capture",
        "If-Match": "0",
    }
    policy = admin.get("/admin-api/v1/observability/policy").json()
    assert not policy["can_inspect_payloads"]
    saved = admin.put(
        "/admin-api/v1/observability/policy",
        json={
            "settings": {**policy["settings"], "capture_enabled": True},
            "reason": "Captura protegida autorizada",
        },
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    assert (
        admin.put(
            "/admin-api/v1/observability/policy",
            json={"settings": policy["settings"], "reason": "Versão antiga rejeitada"},
            headers={**headers, "Idempotency-Key": "conflict"},
        ).status_code
        == 412
    )
    identity = TraceStore(system["repo"]).put(
        context(system), "exchange", "received", {"request": {"input": "protected"}}
    )
    assert (
        admin.get(f"/admin-api/v1/observability/traces/{identity}/payload?section=request").status_code == 403
    )
    grant = admin.put(
        "/admin-api/v1/observability/payload-access",
        json={"admin_ids": [system["admin"]], "reason": "Autorizar inspeção específica"},
        headers={**headers, "Idempotency-Key": "grant"},
    )
    assert grant.status_code == 200
    payload = admin.get(f"/admin-api/v1/observability/traces/{identity}/payload?section=request")
    copied = admin.post(
        f"/admin-api/v1/observability/traces/{identity}/copy",
        json={"section": "request"},
        headers={**headers, "Idempotency-Key": "copy"},
    )
    assert copied.status_code == 200 and copied.json() == payload.json()
    feed = admin.get("/admin-api/v1/observability/traces?phase=preparation&state=received&limit=1").json()
    assert feed["items"][0]["id"] == identity
    assert admin.get("/admin-api/v1/observability/traces?phase=unknown").status_code == 400
    assert admin.get("/admin-api/v1/template-preparation-policy").json()["available_models"]


def test_non_json_response_has_metadata_only_and_no_reconstructed_payload(system):
    store = enable(system)
    gateway = HttpModelGateway(
        system["config"],
        httpx.MockTransport(lambda request: httpx.Response(200, content=b"<html>private unavailable</html>")),
    )
    with observe_integration(system["repo"], context(system)), pytest.raises(ProviderUncertain):
        gateway.generate(system["policy"], "private input")
    row = store.listing(system["admin"])["items"][0]
    assert row["http_status"] == 200
    assert row["section_states"]["response"] == "unavailable"
    assert row["capture_reason"] == "NON_JSON_RESPONSE"
    payload = store.payload(system["admin"], row["id"], "response")
    assert payload["payload"] is None and payload["capture_state"] == "unavailable"
    with system["repo"].sessions() as db:
        assert db.get(IntegrationTracePayload, (row["id"], "response")) is None


def test_category_classification_and_aggregate_truncation_are_truthful(system):
    value = {"usage": {"input_tokens": 42}, "max_output_tokens": 500, "input": 12345678901}
    technical = sanitized(value, ["technical_metadata"])["payload"]
    usage = sanitized(value, ["usage"])["payload"]
    assert technical["usage"]["input_tokens"] == PROTECTED
    assert technical["max_output_tokens"] == 500 and technical["input"] == PROTECTED
    assert usage["usage"]["input_tokens"] == 42 and usage["max_output_tokens"] == PROTECTED
    store = enable(system, max_payload_bytes=1024)
    identifiers = ["field_" + str(index) + "x" * 100 for index in range(40)]
    trace = context(system, identifiers=identifiers)
    identity = store.put(
        trace, "exchange", "pending", {"request": {"items": [{"field_id": name} for name in identifiers]}}
    )
    store.put(trace, "exchange", "received", {"response": {"usage": {"input_tokens": 2}}}, identity=identity)
    row = store.listing(system["admin"])["items"][0]
    assert row["truncated"] and row["capture_state"] == "truncated"
    assert row["section_states"] == {"request": "truncated", "response": "captured"}


def test_late_provider_response_cannot_recreate_payload_after_project_purge(system):
    from viracase.infrastructure.integration_traces import TraceObserver

    store = enable(system)
    trace = context(system)
    observer = TraceObserver(system["repo"], trace)
    request = httpx.Request("POST", "https://api.openai.com/v1/responses", json={"input": "private"})
    observer.request(request)
    with system["repo"].sessions.begin() as db:
        store.purge_project(db, project_id=trace["project_id"])
    observer.response(httpx.Response(200), b'{"usage":{"input_tokens":10},"output":[]}')
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(IntegrationTracePayload)) == 0
        row = db.get(IntegrationTrace, observer.identity)
        assert row.capture_state == "unavailable" and row.project_id is None


def test_known_short_secret_is_not_exposed_as_provider_request_id(system):
    store = enable(system)
    system["config"].openai_api_key = "shortkey"
    response = {"usage": {"input_tokens": 2, "output_tokens": 1}, "output": []}
    gateway = HttpModelGateway(
        system["config"],
        httpx.MockTransport(
            lambda request: httpx.Response(200, json=response, headers={"x-request-id": "shortkey"})
        ),
    )
    with observe_integration(system["repo"], context(system)):
        gateway.generate(system["policy"], "private")
    assert "shortkey" not in json.dumps(store.listing(system["admin"]))


def test_compatibility_retry_has_distinct_real_attempt_and_reason(system):
    store = enable(system)
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(400, json={"error": {"message": "Unknown name responseSchema"}})
        return httpx.Response(
            200,
            json={
                "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1},
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "{}"}]}}],
            },
        )

    policy = {
        **system["policy"],
        "provider": "gemini",
        "model": "gemini-3.5-flash-lite",
        "template_preparation_contract": 1,
    }
    with observe_integration(system["repo"], context(system, provider="gemini", model=policy["model"])):
        HttpModelGateway(system["config"], httpx.MockTransport(handle)).generate(policy, "private")
    rows = sorted(store.listing(system["admin"])["items"], key=lambda row: row["sequence"])
    assert [row["attempt"] for row in rows] == [1, 2]
    assert rows[0]["level"] == "error" and rows[0]["status"] == "rejected"
    assert rows[1]["retry_reason"] == "UNSUPPORTED_OPTIONAL_PARAMETER"
    assert rows[1]["parent_span_id"] == rows[0]["id"]
    assert len(store.listing(system["admin"], level="error")["items"]) == 1


def test_not_found_payload_read_is_audited(system):
    store = enable(system)
    with pytest.raises(RuleError) as caught:
        store.payload(system["admin"], "missing-trace", "request")
    assert caught.value.status == 404
    with system["repo"].sessions() as db:
        assert db.scalar(
            select(Audit.id).where(Audit.resource_id == "missing-trace", Audit.action == "trace:read")
        )


def test_worker_monitor_prunes_expired_payload_without_admin_traffic(system, monkeypatch):
    import threading
    from types import SimpleNamespace

    from viracase.infrastructure import runtime_monitor

    store = enable(system)
    identity = store.put(context(system), "exchange", "received", {"request": {"input": "private"}})
    with system["repo"].sessions.begin() as db:
        db.get(IntegrationTracePayload, (identity, "request")).expires_at = utcnow() - timedelta(seconds=1)
        db.get(IntegrationTrace, identity).expires_at = utcnow() - timedelta(seconds=1)
    monkeypatch.setattr(
        runtime_monitor,
        "IsolatedBuild",
        lambda: SimpleNamespace(diagnostics=lambda: {"available": False, "code": "SYNTHETIC_DISABLED"}),
    )
    monkeypatch.setattr(runtime_monitor.time, "monotonic", lambda: 7200)

    class Once(threading.Event):
        def wait(self, timeout=None):
            self.set()
            return True

    monitor = runtime_monitor.start_monitor(system["repo"], Once())
    monitor.join(timeout=5)
    assert not monitor.is_alive()
    with system["repo"].sessions() as db:
        assert db.get(IntegrationTracePayload, (identity, "request")) is None
