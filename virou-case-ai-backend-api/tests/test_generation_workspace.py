import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from viracase.application.generation import GenerationService, generated_files
from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError, canonical_hash, utcnow
from viracase.infrastructure.models import Attempt, Idempotency, Job, Operation, Revision, new_id
from viracase.infrastructure.providers import HttpModelGateway
from viracase.infrastructure.worker import Worker
from viracase.interfaces.api import create_app


def start(system):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor, new_id(), {"name": "Construção", "objective": "Construir um projeto completo"}
    )
    payload = {
        "brief": "Criar título com café e aspas",
        "base_revision_id": project["current_revision_id"],
        "policy_version": "2026-10-v1",
    }
    operation = GenerationService(repo, system["catalog"]).start(actor, project["id"], "generation", payload)
    return repo, actor, project, operation, payload


class ResultGateway:
    def __init__(self, text, output_error=None, during=None):
        self.text, self.output_error, self.during, self.calls = text, output_error, during, 0

    def generate(self, policy, prompt):
        self.calls += 1
        if self.during:
            self.during()
        return ModelResult(
            self.text, {"input": 100, "cached": 0, "output": 100}, "synthetic", self.output_error
        )


@pytest.mark.parametrize("kind", ["map", "list", "fenced", "bom"])
def test_partial_response_preserves_existing_and_persists_progress(system, kind):
    repo, actor, project, operation, payload = start(system)
    original = repo.source_files(actor, project["id"], project["current_revision_id"])
    content = '<h1>Café "novo"</h1>\n<script>const value = {x: "olá"}</script>'
    changes = {"public/index.html": content, "public/new.txt": "novo"}
    document = {
        "files": changes if kind != "list" else [{"path": k, "content": v} for k, v in changes.items()]
    }
    text = json.dumps(document)
    if kind == "fenced":
        text = "```json\n" + text + "\n```"
    if kind == "bom":
        text = "\ufeff" + text
    observed = []
    gateway = ResultGateway(text, during=lambda: observed.append(repo.operation(actor, operation["id"])))
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick() and not worker.tick()
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "succeeded"
    assert result["request_text"] == payload["brief"]
    assert result["model_label"] is None
    assert observed[0]["progress_stage"] == "provider_sent"
    assert [event["stage"] for event in result["activity"]] == [
        "queued",
        "validating",
        "provider_sent",
        "validating_result",
        "saving_revision",
        "revision_ready",
    ]
    files = repo.source_files(actor, project["id"], result["result_ref"]["revision_id"])
    assert files == {**original, **changes}
    assert result["result_ref"]["changed_files"] == [
        {"path": "public/index.html", "change": "modified"},
        {"path": "public/new.txt", "change": "created"},
    ]
    assert repo.wallet(actor)["reserved_ratio"] == 0
    assert gateway.calls == 1
    replay = GenerationService(repo, system["catalog"]).start(actor, project["id"], "generation", payload)
    assert replay["state"] == "succeeded" and replay["id"] == operation["id"]


@pytest.mark.parametrize(
    "text,code",
    [
        ('{"files": {"public/index.html": "unfinished', "INVALID_GENERATION"),
        ('{"files": {"x":"one", "x":"two"}}', "INVALID_GENERATION"),
        ('{"files": [{"path":"a", "content":"one"}, {"path":"a", "content":"two"}]}', "INVALID_GENERATION"),
        ('{"files": {"x": null}}', "INVALID_GENERATION"),
        ('{"files": {}}', "INVALID_GENERATION"),
        ("[]", "INVALID_GENERATION"),
        ('{"files": {"../escape": "oops"}}', "INVALID_PATH"),
        ('{"files": {".env": "SECRET=value"}}', "INVALID_PATH"),
        ('{"files": {"src/index.ts": "broken"}}', "STACK_MISMATCH"),
        ('{"files": {"public/leak.txt": "-----BEGIN PRIVATE KEY-----"}}', "SECRET_DETECTED"),
    ],
)
def test_invalid_output_never_replaces_source_or_retries(system, text, code):
    repo, actor, project, operation, _ = start(system)
    gateway = ResultGateway(text)
    worker = Worker(repo, gateway, system["catalog"])
    worker.tick()
    assert not worker.tick()
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "failed" and result["error_code"] == code
    assert result["billing_status"] == "settled"
    assert gateway.calls == 1
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    assert result["progress_message"] != code
    assert "gemini" not in json.dumps(result) and "gpt-" not in json.dumps(result)
    with repo.sessions() as db:
        assert len(db.scalars(select(Revision).where(Revision.project_id == project["id"])).all()) == 1


def test_no_change_is_not_reported_as_success():
    with pytest.raises(RuleError, match="alterações") as error:
        generated_files('{"files":{"same":"content"}}', {"same": "content"})
    assert error.value.code == "GENERATION_NO_CHANGES"


def test_truncated_even_valid_json_never_publishes_revision(system):
    repo, actor, project, operation, _ = start(system)
    Worker(
        repo,
        ResultGateway('{"files":{"public/new.txt":"partial"}}', "GENERATION_TRUNCATED"),
        system["catalog"],
    ).tick()
    result = repo.operation(actor, operation["id"])
    assert result["error_code"] == "GENERATION_TRUNCATED"
    assert "limite" in result["progress_message"]
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


def test_cancellation_in_flight_settles_usage_without_applying_result(system):
    repo, actor, project, operation, _ = start(system)
    gateway = ResultGateway(
        '{"files":{"public/new.txt":"new"}}', during=lambda: repo.cancel(actor, operation["id"], "cancel")
    )
    Worker(repo, gateway, system["catalog"]).tick()
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "cancelled" and result["progress_stage"] == "cancelled_after_dispatch"
    assert result["billing_status"] == "settled"
    assert repo.wallet(actor)["consumed_ratio"] > 0
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


def test_lost_lease_does_not_apply_or_settle_a_late_result(system):
    repo, actor, project, operation, _ = start(system)

    def lose_lease():
        with repo.sessions.begin() as db:
            db.scalar(select(Job).where(Job.operation_id == operation["id"])).lease_token = new_id()

    Worker(
        repo, ResultGateway('{"files":{"public/new.txt":"new"}}', during=lose_lease), system["catalog"]
    ).tick()
    assert repo.wallet(actor)["reserved_ratio"] > 0
    assert repo.wallet(actor)["consumed_ratio"] == 0
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


def test_latest_pagination_and_active_filter_are_account_scoped(system):
    repo, actor, project, operation, _ = start(system)
    with repo.sessions.begin() as db:
        for index in range(60):
            db.add(
                Operation(
                    account_id=actor,
                    project_id=project["id"],
                    type="export",
                    state="succeeded",
                    created_at=utcnow() + timedelta(seconds=index + 1),
                )
            )
    page = repo.operations(actor, project["id"], latest=True)
    assert len(page["items"]) == 50 and page["next_cursor"]
    assert operation["id"] not in [item["id"] for item in page["items"]]
    second = repo.operations(actor, project["id"], latest=True, cursor=page["next_cursor"])
    assert len(second["items"]) == 11
    assert len({item["id"] for item in page["items"] + second["items"]}) == 61
    assert (
        repo.operations(actor, project["id"], state="active", latest=True)["items"][0]["id"]
        == operation["id"]
    )
    assert repo.operations(system["users"][1], project["id"], latest=True)["items"] == []
    with pytest.raises(RuleError):
        repo.operation(system["users"][1], operation["id"])
    with pytest.raises(RuleError):
        repo.operations(system["users"][1], latest=True, cursor=page["next_cursor"])
    assert repo.operations(system["admin"], admin=True)["items"]
    assert (
        next(
            item
            for item in repo.operations(system["admin"], admin=True, state="active")["items"]
            if item["id"] == operation["id"]
        )["model_label"]
        == system["policy"]["model"]
    )


def test_legacy_idempotency_does_not_leak_model(system):
    repo, actor, project, operation, payload = start(system)
    with repo.sessions.begin() as db:
        saved = db.get(Idempotency, canonical_hash([actor, f"generation:{project['id']}", "generation"]))
        saved.result = {**saved.result, "model_label": "private-model"}
    result = GenerationService(repo, system["catalog"]).start(actor, project["id"], "generation", payload)
    assert result["model_label"] is None


@pytest.mark.parametrize(
    "provider,reason,error",
    [
        ("gemini", "MAX_TOKENS", "GENERATION_TRUNCATED"),
        ("gemini", "SAFETY", "GENERATION_BLOCKED"),
        ("openai", "max_output_tokens", "GENERATION_TRUNCATED"),
        ("openai", "content_filter", "GENERATION_BLOCKED"),
    ],
)
def test_provider_finish_reason_is_not_lost(system, provider, reason, error):
    policy = {
        **system["policy"],
        "provider": provider,
        "model": "gemini-3.8-flash" if provider == "gemini" else "gpt-6-luna",
    }

    def response(request):
        if provider == "gemini":
            return httpx.Response(
                200,
                json={
                    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
                    "candidates": [{"finishReason": reason, "content": {"parts": [{"text": "{}"}]}}],
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "incomplete",
                "incomplete_details": {"reason": reason},
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "output": [],
            },
        )

    result = HttpModelGateway(system["config"], httpx.MockTransport(response)).generate(policy, "JSON")
    assert result.output_error == error
    assert result.usage == {"input": 10, "cached": 0, "output": 5}


def test_public_http_contract_has_request_progress_and_no_model(system):
    repo, actor, project, operation, _ = start(system)
    app = create_app(
        False, (system["config"], system["engine"], repo, system["catalog"], ResultGateway("{}"))
    )
    with TestClient(app) as client:
        logged = client.post(
            "/api/v1/auth/login",
            json={"login": "student0", "password": "test-student-password"},
            headers={"Origin": system["config"].public_origin},
        )
        assert logged.status_code == 200
        response = client.get(f"/api/v1/operations?project_id={project['id']}&latest=true&state=active")
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["id"] == operation["id"] and item["request_text"]
        assert item["activity"][0]["stage"] == "queued" and item["model_label"] is None
        assert client.get(f"/api/v1/operations/{operation['id']}").json()["model_label"] is None


def test_recovery_after_confirmed_usage_does_not_resend(system):
    repo, actor, project, operation, _ = start(system)
    gateway = ResultGateway('{"files":{"public/new.txt":"new"}}')
    Worker(repo, gateway, system["catalog"]).tick()
    with repo.sessions.begin() as db:
        op = db.get(Operation, operation["id"])
        op.state, op.progress_stage = "running", "validating_result"
        job = db.scalar(select(Job).where(Job.operation_id == op.id))
        job.state, job.lease_until = "running", utcnow() - timedelta(seconds=1)
    Worker(repo, gateway, system["catalog"]).tick()
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "failed" and result["billing_status"] == "settled"
    assert gateway.calls == 1
    with repo.sessions() as db:
        assert db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"])).state == "confirmed"


def test_active_generation_filter_excludes_exports(system):
    repo, actor, project, operation, _ = start(system)
    with repo.sessions.begin() as db:
        for _ in range(51):
            db.add(
                Operation(
                    account_id=actor,
                    project_id=project["id"],
                    type="export",
                    state="queued",
                    created_at=utcnow(),
                )
            )
    result = repo.operations(actor, project["id"], state="active", latest=True, kind="generation")
    assert [item["id"] for item in result["items"]] == [operation["id"]]


def test_cancel_retry_returns_current_state_without_legacy_model(system):
    repo, actor, project, operation, _ = start(system)
    repo.cancel(actor, operation["id"], "cancel")
    with repo.sessions.begin() as db:
        intent = db.get(Idempotency, canonical_hash([actor, f"cancel:{operation['id']}", "cancel"]))
        intent.result = {**intent.result, "model_label": "private-model", "state": "running"}
    response = repo.cancel(actor, operation["id"], "cancel")
    assert response["state"] == "cancelled" and response["model_label"] is None


def test_queued_operation_from_previous_release_uses_reserved_contract(system):
    from viracase.application.generation import generation_prompt

    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor, "legacy-project", {"name": "Anterior", "objective": "Aplicação anterior à atualização"}
    )
    payload = {
        "brief": "Melhorar a página existente",
        "base_revision_id": project["current_revision_id"],
        "policy_version": "2026-10-v1",
    }
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    prompt = generation_prompt(project, payload["brief"], files, contract_version=1)
    policy, ceiling = system["catalog"].resolve(prompt)
    assert "generation_contract_version" not in policy
    operation = repo.create_operation(
        actor, project["id"], "generation", "legacy-generation", payload, policy, ceiling
    )

    class LegacyGateway:
        def generate(self, actual_policy, actual_prompt):
            assert actual_prompt == prompt
            return ModelResult(
                json.dumps({"files": {**files, "public/new.txt": "novo"}}),
                {"input": 10, "cached": 0, "output": 10},
                "legacy",
            )

    Worker(repo, LegacyGateway(), system["catalog"]).tick()
    assert repo.operation(actor, operation["id"])["state"] == "succeeded"
