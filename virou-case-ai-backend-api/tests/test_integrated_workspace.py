import json
from decimal import Decimal
import httpx
import pytest
from sqlalchemy import select, func
from fastapi.testclient import TestClient
from viracase.application.conversation import apply_batch
from viracase.application.generation import GenerationService
from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError, cost
from viracase.infrastructure.drafts import Drafts
from viracase.infrastructure.learning import LearningLibrary
from viracase.infrastructure.models import Attempt, Operation, Revision, Job
from viracase.infrastructure.providers import HttpModelGateway, ProviderUncertain, ProviderRejected
from viracase.infrastructure.worker import Worker
from viracase.interfaces.api import create_app


@pytest.fixture
def workspace(system):
    repo = system["repo"]
    actor = system["users"][0]
    project = repo.create_project(
        actor, "project", {"name": "Workspace", "objective": "Aplicação de tarefas para a equipe"}
    )
    drafts = Drafts(repo, LearningLibrary(system["config"].content_root))
    return repo, actor, project, drafts


class Batches:
    def __init__(self, mode="complete"):
        self.calls, self.mode = 0, mode

    def generate(self, policy, prompt):
        self.calls += 1
        data = json.loads(prompt[prompt.index('{"stack"') :])
        usage = {"input": 100, "cached": 0, "output": 70}
        if self.mode == "uncertain" and self.calls == 2:
            raise ProviderUncertain("unknown")
        if self.mode == "rejected" and self.calls == 2:
            raise ProviderRejected("rejected")
        if self.mode == "truncated" and self.calls == 1:
            return ModelResult('{"files":[', usage, "part1", "GENERATION_TRUNCATED")
        if self.mode == "always_truncated":
            return ModelResult("", usage, "part", "GENERATION_TRUNCATED")
        if self.mode == "invalid":
            return ModelResult('{"edits":[]}', usage, "invalid")
        body = {
            "files": [],
            "edits": [
                {"path": "public/index.html", "old": "</body>", "new": f"<p>Etapa {self.calls}</p></body>"}
            ],
            "complete": self.mode not in {"multi", "uncertain", "rejected", "limit"}
            or self.calls >= 2
            and self.mode != "limit",
        }
        assert data["files"]["public/index.html"].count("</body>") == 1
        if self.calls > 1 and self.mode == "multi":
            assert "Etapa 1" in data["files"]["public/index.html"]
        return ModelResult(json.dumps(body), usage, f"batch{self.calls}")


def start(system, workspace, gateway):
    repo, actor, p, _ = workspace
    op = GenerationService(repo, system["catalog"]).start(
        actor,
        p["id"],
        "conversation",
        {
            "brief": "Implemente uma lista de tarefas completa",
            "base_revision_id": p["current_revision_id"],
            "policy_version": "2026-10-v1",
            "review_required": True,
        },
        contract_version=3,
    )
    assert Worker(repo, gateway, system["catalog"]).tick(op["id"])
    assert not Worker(repo, gateway, system["catalog"]).tick(op["id"])
    return repo.operation(actor, op["id"])


@pytest.mark.parametrize("mode,calls", [("complete", 1), ("multi", 2), ("truncated", 2)])
def test_complete_proposal_requires_explicit_apply(system, workspace, mode, calls):
    repo, actor, p, drafts = workspace
    gateway = Batches(mode)
    op = start(system, workspace, gateway)
    assert op["state"] == "succeeded", op
    assert gateway.calls == calls
    assert op["result_ref"]["candidate"]
    assert repo.project(actor, p["id"])["current_revision_id"] == p["current_revision_id"]
    with repo.sessions() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == op["id"]))
        assert attempt.usage["input"] == 100 * calls
        assert len(attempt.usage["calls"]) == calls
        assert attempt.actual_brl == cost(
            system["policy"], {"input": 100 * calls, "cached": 0, "output": 70 * calls}
        )
    result = drafts.apply(actor, p["id"], op["id"], "apply", p["version"])
    assert result == drafts.apply(actor, p["id"], op["id"], "apply", p["version"])
    assert repo.project(actor, p["id"])["current_revision_id"] == result["revision_id"]
    assert "Etapa" in repo.source_files(actor, p["id"], result["revision_id"])["public/index.html"]


@pytest.mark.parametrize(
    "mode,state,calls",
    [
        ("always_truncated", "failed", 2),
        ("invalid", "failed", 1),
        ("uncertain", "reconciling", 2),
        ("rejected", "failed", 2),
        ("limit", "failed", 4),
    ],
)
def test_failure_keeps_canonical_and_does_not_replay(system, workspace, mode, state, calls):
    repo, actor, p, _ = workspace
    gateway = Batches(mode)
    op = start(system, workspace, gateway)
    assert op["state"] == state, op
    assert gateway.calls == calls
    assert repo.project(actor, p["id"])["current_revision_id"] == p["current_revision_id"]
    with repo.sessions() as db:
        assert (
            db.scalar(select(func.count()).select_from(Revision).where(Revision.project_id == p["id"])) == 1
        )
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == op["id"]))
        if mode == "rejected":
            assert attempt.actual_brl > Decimal(0)
    if mode == "uncertain":
        assert repo.wallet(actor)["pending_reconciliation"]


def test_apply_rejects_changed_base_and_cross_account(system, workspace):
    repo, actor, p, drafts = workspace
    op = start(system, workspace, Batches())
    with pytest.raises(RuleError, match="indisponível"):
        drafts.apply(system["users"][1], p["id"], op["id"], "other", p["version"])
    files = repo.source_files(actor, p["id"], p["current_revision_id"])
    repo.save_revision(
        actor, p["id"], "manual", {"base_revision_id": p["current_revision_id"], "files": files}, p["version"]
    )
    with pytest.raises(RuleError) as error:
        drafts.apply(actor, p["id"], op["id"], "stale", p["version"])
    assert error.value.code == "VERSION_CONFLICT"


def test_drafts_survive_reload_keep_revisions_and_account_isolation(system, workspace):
    repo, actor, p, drafts = workspace
    data = {
        "base_revision_id": p["current_revision_id"],
        "files": {"incomplete.txt": "unfinished draft"},
        "prompt": "Pedido em elaboração",
    }
    result = drafts.save(actor, "project", p["id"], "save", data, 0)
    assert result["version"] == 1
    fresh = Drafts(repo, LearningLibrary(system["config"].content_root))
    assert fresh.get(actor, "project", p["id"])["data"]["files"] == data["files"]
    assert repo.project(actor, p["id"])["current_revision_id"] == p["current_revision_id"]
    with pytest.raises(RuleError):
        fresh.get(system["users"][1], "project", p["id"])
    with pytest.raises(RuleError) as error:
        drafts.save(actor, "project", p["id"], "stale", data, 0)
    assert error.value.code == "DRAFT_CONFLICT"


@pytest.mark.parametrize(
    "files",
    [{"../secret": "x"}, {".env": "secret"}, {"a": "x" * 200001}, {"key": "-----BEGIN PRIVATE KEY-----"}],
)
def test_draft_path_size_and_secret_boundaries(workspace, files):
    repo, actor, p, drafts = workspace
    with pytest.raises(RuleError):
        drafts.save(
            actor,
            "project",
            p["id"],
            "bad",
            {"base_revision_id": p["current_revision_id"], "files": files, "prompt": ""},
            0,
        )
    assert drafts.get(actor, "project", p["id"])["version"] == 0


def test_learning_transfer_is_atomic_idempotent_free_and_preserves_source(workspace):
    repo, actor, p, drafts = workspace
    before = repo.wallet(actor, True)
    source = {
        "fields": {
            "objective": "Lista de tarefas",
            "audience": "Equipe interna",
            "acceptance": "Cadastrar e concluir uma tarefa",
        },
        "prompt": "Implemente o cadastro completo de tarefas.",
        "manual": True,
    }
    drafts.save(actor, "learning", "sdd", "learning", source, 0)
    original = drafts.get(actor, "learning", "sdd")["data"]["guide"]
    drafts.learning.guides["sdd"] = {**original, "template_version": "future"}
    drafts.save(actor, "learning", "sdd", "edit", source, 1)
    assert (
        drafts.get(actor, "learning", "sdd")["data"]["guide"]["template_version"]
        == original["template_version"]
    )
    drafts.save(
        actor,
        "project",
        p["id"],
        "existing",
        {"base_revision_id": p["current_revision_id"], "files": None, "prompt": "Rascunho anterior"},
        0,
    )
    payload = {"project_id": p["id"], "source_version": 2, "target_version": 1, "mode": "append"}
    first = drafts.transfer(actor, "sdd", "transfer", payload)
    assert drafts.transfer(actor, "sdd", "transfer", payload) == first
    target = drafts.get(actor, "project", p["id"])
    assert target["version"] == 2
    assert target["data"]["prompt"] == "Rascunho anterior\n\n" + source["prompt"]
    assert target["data"]["provenance"][0]["version"] == original["template_version"]
    assert repo.wallet(actor, True) == before
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Operation)) == 0
    with pytest.raises(RuleError):
        drafts.transfer(actor, "sdd", "conflict", payload)
    assert drafts.get(actor, "project", p["id"])["data"] == target["data"]


def test_transfer_rejects_missing_fields_and_cross_account(system, workspace):
    repo, actor, p, drafts = workspace
    drafts.save(
        actor,
        "learning",
        "sdd",
        "empty",
        {"fields": {}, "prompt": "Texto válido mas sem campos", "manual": True},
        0,
    )
    with pytest.raises(RuleError):
        drafts.transfer(
            actor,
            "sdd",
            "missing",
            {"project_id": p["id"], "source_version": 1, "target_version": 0, "mode": "replace"},
        )
    with pytest.raises(RuleError):
        drafts.transfer(
            system["users"][1],
            "sdd",
            "foreign",
            {"project_id": p["id"], "source_version": 1, "target_version": 0, "mode": "replace"},
        )


def test_exact_edits_reject_ambiguity_and_duplicate_keys():
    for text in [
        '{"files":[],"edits":[],"complete":true,"complete":false}',
        json.dumps({"files": [], "edits": [{"path": "a", "old": "x", "new": "y"}], "complete": True}),
    ]:
        with pytest.raises(RuleError):
            apply_batch(text, {"a": "xx"})


def test_gemini_low_thinking_and_usage_accounting(system):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 50,
                    "thoughtsTokenCount": 20,
                },
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"thought": True, "text": "private"}, {"text": "{}"}]},
                    }
                ],
            },
        )

    policy = {
        **system["policy"],
        "provider": "gemini",
        "model": "gemini-3.8-flash",
        "generation_contract_version": 3,
    }
    result = HttpModelGateway(system["config"], transport=httpx.MockTransport(handler)).generate(
        policy, "JSON"
    )
    assert captured[0]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "LOW"}
    assert result.usage["output"] == 70 and result.text == "{}"


def test_authenticated_api_conversation_and_draft_validation(system, workspace):
    repo, actor, p, _ = workspace
    client = TestClient(
        create_app(dependencies=(system["config"], system["engine"], repo, system["catalog"], Batches()))
    )
    auth = client.post(
        "/api/v1/auth/login",
        json={"login": "student0", "password": "test-student-password"},
        headers={"Origin": system["config"].public_origin},
    ).json()
    headers = {
        "Origin": system["config"].public_origin,
        "X-CSRF-Token": auth["csrf_token"],
        "Idempotency-Key": "api",
        "If-Match": "0",
    }
    url = f"/api/v1/projects/{p['id']}/draft"
    body = {"base_revision_id": p["current_revision_id"], "files": None, "prompt": "Rascunho persistente"}
    assert client.put(url, json=body, headers={"Origin": system["config"].public_origin}).status_code == 403
    assert client.put(url, json=body, headers=headers).status_code == 200
    assert client.put(url, json={**body, "prompt": "x" * 12001}, headers=headers).status_code == 400
    assert client.get(url).json()["data"]["prompt"] == body["prompt"]
    response = client.post(
        f"/api/v1/projects/{p['id']}/conversation",
        headers={**headers, "Idempotency-Key": "chat"},
        json={
            "brief": "Implemente tarefas completas",
            "base_revision_id": p["current_revision_id"],
            "policy_version": "2026-10-v1",
        },
    )
    assert response.status_code == 202, response.text
    op = client.get("/api/v1/operations/" + response.json()["id"]).json()
    assert op["state"] == "succeeded", op
    assert op["model_label"] is None and "policy" not in op
    with repo.sessions() as db:
        assert db.scalar(select(Job).where(Job.operation_id == op["id"])).state == "done"


def test_confirmed_checkpoint_crash_settles_known_usage_without_resend(system, workspace):
    from datetime import timedelta
    from viracase.domain.rules import utcnow

    repo, actor, p, _ = workspace
    op = GenerationService(repo, system["catalog"]).start(
        actor,
        p["id"],
        "crash",
        {
            "brief": "Implementar tarefas completas",
            "base_revision_id": p["current_revision_id"],
            "policy_version": "2026-10-v1",
            "review_required": True,
        },
        3,
    )
    usage = {"input": 100, "cached": 0, "output": 70}
    with repo.sessions.begin() as db:
        job = db.scalar(select(Job).where(Job.operation_id == op["id"]))
        job.state, job.lease_until = "running", utcnow() - timedelta(seconds=1)
        db.add(
            Attempt(
                operation_id=op["id"],
                provider="openai",
                model="gpt-6-luna",
                state="confirmed",
                usage=usage,
                actual_brl=cost(system["policy"], usage),
            )
        )
    gateway = Batches()
    assert not Worker(repo, gateway, system["catalog"]).tick()
    assert gateway.calls == 0
    final = repo.operation(actor, op["id"])
    assert final["state"] == "failed" and final["billing_status"] == "settled"
    assert repo.project(actor, p["id"])["current_revision_id"] == p["current_revision_id"]


def test_expiry_during_provider_call_discards_proposal(system, workspace):
    from datetime import timedelta
    from viracase.domain.rules import utcnow
    from viracase.infrastructure.models import Account, Period

    repo, actor, p, _ = workspace

    class Expiring(Batches):
        def generate(self, policy, prompt):
            result = super().generate(policy, prompt)
            with repo.sessions.begin() as db:
                account = db.get(Account, actor)
                period = db.get(Period, account.current_period_id)
                period.starts_at = utcnow() - timedelta(days=30)
                period.expires_at = utcnow() - timedelta(seconds=1)
            return result

    op = start(system, workspace, Expiring())
    assert op["state"] == "failed" and op["billing_status"] == "settled"
    assert repo.project(actor, p["id"])["current_revision_id"] == p["current_revision_id"]


def test_retention_purges_learning_and_project_drafts(system, workspace):
    from datetime import timedelta
    from viracase.domain.rules import utcnow
    from viracase.infrastructure.models import Account, Period, Draft

    repo, actor, p, drafts = workspace
    drafts.save(
        actor,
        "project",
        p["id"],
        "project",
        {"base_revision_id": p["current_revision_id"], "files": None, "prompt": "rascunho"},
        0,
    )
    drafts.save(
        actor, "learning", "sdd", "learning", {"fields": {}, "prompt": "rascunho", "manual": False}, 0
    )
    with repo.sessions.begin() as db:
        account = db.get(Account, actor)
        period = db.get(Period, account.current_period_id)
        period.starts_at = utcnow() - timedelta(days=35)
        period.expires_at = utcnow() - timedelta(days=3)
        period.purge_at = utcnow() - timedelta(days=1)
    Worker(repo, Batches(), system["catalog"]).purge(actor)
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Draft).where(Draft.account_id == actor)) == 0
