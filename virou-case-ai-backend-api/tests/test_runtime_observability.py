import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.models import Operation, RecommendationRun, RuntimeEvent, RuntimeStatus
from viracase.infrastructure.observability import WorkerBuildStatus, events, heartbeat, prune
from viracase.interfaces.api import create_app


def test_feed_tracks_operations_and_jev_without_payloads_and_filters(system):
    repo, actor = system["repo"], system["users"][0]
    p = repo.create_project(actor, "p", {"name": "Monitoria", "objective": "Construção observável e privada"})
    with repo.sessions.begin() as db:
        op = Operation(
            account_id=actor,
            project_id=p["id"],
            type="generation",
            state="queued",
            payload={"brief": "never expose this prompt", "token": "private-secret"},
        )
        db.add(op)
        db.flush()
        oid = op.id
    with repo.sessions.begin() as db:
        op = db.get(Operation, oid)
        op.state = "running"
        record_progress(op, "provider_sent")
    with repo.sessions.begin() as db:
        op = db.get(Operation, oid)
        op.state, op.error_code = "failed", "PROVIDER_ACCESS_DENIED"
        record_progress(op, "provider_rejected")
        db.add(
            RecommendationRun(
                account_id=actor,
                project_id=p["id"],
                request_key="jev",
                payload_hash="h" * 64,
                state="fallback",
                result={"fallback_reason": "RECOMMENDER_UNAVAILABLE", "text": "private-secret"},
                telemetry={"error_code": "JEV_ACCESS_DENIED", "raw": "private-secret"},
            )
        )
    result = events(repo, system["admin"])
    assert {x["service"] for x in result["items"]} == {"agents", "jev"}
    assert "private-secret" not in json.dumps(result) and "never expose" not in json.dumps(result)
    assert len(events(repo, system["admin"], correlation=oid)["items"]) == 3
    assert (
        events(repo, system["admin"], service="agents", level="error")["items"][0]["error_code"]
        == "PROVIDER_ACCESS_DENIED"
    )
    assert events(repo, system["admin"], service="jev")["items"][0]["error_code"] == "JEV_ACCESS_DENIED"
    page = events(repo, system["admin"], limit=2)
    second = events(repo, system["admin"], limit=2, before=page["next_cursor"])
    assert len(second["items"]) == 2
    assert not {r["id"] for r in page["items"]} & {r["id"] for r in second["items"]}
    with pytest.raises(RuleError) as denied:
        events(repo, actor)
    assert denied.value.status == 403


def test_events_rollback_with_business_transaction_and_expire(system):
    repo = system["repo"]
    with pytest.raises(ValueError), repo.sessions.begin() as db:
        db.add(Operation(account_id=system["admin"], type="publication_check", state="queued"))
        db.flush()
        raise ValueError("rollback")
    assert not events(repo, system["admin"])["items"]
    with repo.sessions.begin() as db:
        db.add(
            RuntimeEvent(
                service="agents",
                level="info",
                correlation_id="old",
                stage="queued",
                state="queued",
                created_at=utcnow() - timedelta(days=8),
            )
        )
    prune(repo)
    with repo.sessions() as db:
        assert not db.scalar(select(RuntimeEvent.id))


def test_worker_status_expires_and_mixed_executors_never_allow_publication(system):
    repo = system["repo"]
    assert not WorkerBuildStatus(repo).available()
    detail = {"available": True, "fingerprint": "a" * 64, "stacks": ["hono-typescript-d1"]}
    heartbeat(repo, "worker:one", detail)
    assert WorkerBuildStatus(repo).available()
    assert WorkerBuildStatus(repo).supports("hono-typescript-d1")
    assert not WorkerBuildStatus(repo).supports("fastapi-python-d1")
    heartbeat(repo, "worker:two", {**detail, "fingerprint": "b" * 64})
    assert WorkerBuildStatus(repo).fingerprint() is None
    with repo.sessions.begin() as db:
        for row in db.scalars(select(RuntimeStatus)):
            row.checked_at = utcnow() - timedelta(minutes=2)
    assert not WorkerBuildStatus(repo).available()


def test_http_monitor_is_admin_only_and_validates_filters(system):
    dependencies = (system["config"], system["engine"], system["repo"], system["catalog"], None)
    admin = TestClient(create_app(True, dependencies))
    assert admin.get("/admin-api/v1/observability/events").status_code == 401
    login = admin.post(
        "/admin-api/v1/auth/login",
        json={"login": "operator", "password": "test-operator-password"},
        headers={"Origin": system["config"].admin_origin},
    )
    assert login.status_code == 200
    assert admin.get("/admin-api/v1/observability/events").status_code == 200
    assert admin.get("/admin-api/v1/observability/events?limit=999999").status_code == 400
    assert admin.get("/admin-api/v1/observability/events?service=secret").status_code == 400
    assert admin.get("/admin-api/v1/observability/runtime").json() == {"workers": []}
    public = TestClient(create_app(False, dependencies))
    assert public.get("/api/v1/observability/events").status_code == 404
