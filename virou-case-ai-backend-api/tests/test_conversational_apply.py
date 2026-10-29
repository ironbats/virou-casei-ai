import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_integrated_workspace import workspace as workspace
from test_conversation_runtime import fund_generation, managed_provider, runtime, login, project_and_draft
from viracase.application.conversation_turn import InvalidTurn, parse_turn, turn_schema
from viracase.application.generation import GenerationService
from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError
from viracase.infrastructure.models import Attempt, Revision
from viracase.infrastructure.providers import ProviderUncertain
from viracase.infrastructure.worker import Worker
from viracase.interfaces.api import create_app
from workflow_http import reviewed_message


def response(action="implement", complete=True, **overrides):
    return {
        "action": action,
        "message": "Melhorei a página inicial com uma apresentação mais clara.",
        "files": [],
        "edits": [{"path": "public/index.html", "old": "</body>", "new": "<p>Melhoria aplicada</p></body>"}],
        "complete": complete,
        **overrides,
    }


@pytest.mark.parametrize("variant", ["schema", "fenced", "legacy_map", "legacy_list"])
def test_complete_responses_are_normalized_without_guessing_code(variant):
    data = response(files=[{"path": "public/new.js", "content": "const value = 1;\n"}], edits=[])
    if variant == "legacy_map":
        data = {"files": {"public/new.js": "const value = 1;\n"}, "summary": "Arquivo criado."}
    if variant == "legacy_list":
        data = {key: data[key] for key in ("files", "edits", "complete")}
    text = json.dumps(data)
    if variant == "fenced":
        text = "```json\n" + text + "\n```"
    turn = parse_turn(text, {"public/index.html": "<h1>Original</h1>"})
    assert turn.files["public/new.js"] == "const value = 1;\n"
    assert turn.files["public/index.html"] == "<h1>Original</h1>"
    assert turn.complete and turn.action == "implement"


@pytest.mark.parametrize(
    "text,reason",
    [
        ('{"files":', "INVALID_JSON"),
        ('{"complete":true,"complete":false}', "DUPLICATE_KEY"),
        ('["invalid"]', "INVALID_CONTRACT"),
        (json.dumps(response(complete="true")), "INVALID_CONTRACT"),
        (json.dumps(response(files=["wrong"])), "INVALID_FILE"),
        (
            json.dumps(
                response(edits=[{"path": "public/index.html", "old": "<h1> Original </h1>", "new": "x"}])
            ),
            "EDIT_TARGET_MISMATCH",
        ),
        (json.dumps(response(action="clarify")), "INVALID_CLARIFICATION"),
        (json.dumps(response(extra="ignored?")), "INVALID_CONTRACT"),
    ],
)
def test_unverified_responses_and_ambiguous_edits_are_rejected(text, reason):
    files = {"public/index.html": "<h1>Original</h1>"}
    with pytest.raises(InvalidTurn) as caught:
        parse_turn(text, files)
    assert caught.value.reason == reason
    assert files == {"public/index.html": "<h1>Original</h1>"}


class Gateway:
    def __init__(self, mode="complete", after_dispatch=None):
        self.mode, self.after_dispatch, self.prompts = mode, after_dispatch, []

    def generate(self, policy, prompt):
        self.prompts.append(prompt)
        assert policy["generation_contract_version"] == 4
        if self.after_dispatch:
            self.after_dispatch()
        count = len(self.prompts)
        usage = {"input": 100, "cached": 0, "output": 100}
        if self.mode == "uncertain":
            raise ProviderUncertain("synthetic-only")
        if self.mode == "cost":
            usage["output"] = policy["max_output_tokens"] * policy["max_batches"] * 2
        if self.mode == "invalid" or (self.mode == "repair" and count == 1):
            return ModelResult('{"files":', usage, "synthetic")
        if self.mode == "truncated" and count == 1:
            return ModelResult('{"files":', usage, "synthetic", "GENERATION_TRUNCATED")
        if self.mode == "clarify":
            data = response(
                action="clarify",
                files=[],
                edits=[],
                message="Posso priorizar a apresentação e os botões da homepage?",
            )
        elif (self.mode == "edit_mismatch" and count == 1) or (self.mode == "multi_repair" and count == 2):
            data = response(
                edits=[{"path": "public/index.html", "old": "not-the-existing-code", "new": "change"}]
            )
        elif self.mode == "empty" and count == 1:
            data = response(files=[], edits=[])
        else:
            data = response(complete=self.mode not in {"multi", "multi_repair"} or count > 1)
        if self.mode == "unsafe":
            data = response(files=[{"path": "../escape.txt", "content": "unsafe"}], edits=[])
        if self.mode == "secret":
            data = response(message="sk-proj-" + "x" * 40)
        return ModelResult(json.dumps(data), usage, "synthetic")


def configure(system):
    fund_generation(system)
    policy = {
        **system["policy"],
        "input_usd_per_million": "0",
        "cached_usd_per_million": "0",
        "output_usd_per_million": "125",
    }
    system["config"].catalog_path.write_text(json.dumps({"models": [policy]}))


def start(system, workspace, gateway, key="turn", **payload):
    repo, actor, project, _ = workspace
    configure(system)
    operation = GenerationService(repo, system["catalog"]).start(
        actor,
        project["id"],
        key,
        {
            "brief": "Melhore a apresentação da homepage",
            "base_revision_id": project["current_revision_id"],
            "policy_version": "2026-10-v1",
            "auto_apply": True,
            "review_required": False,
            **payload,
        },
        contract_version=4,
    )
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    assert not Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    return repo.operation(actor, operation["id"])


@pytest.mark.parametrize(
    "mode,calls",
    [
        ("complete", 1),
        ("multi", 2),
        ("multi_repair", 3),
        ("repair", 2),
        ("empty", 2),
        ("edit_mismatch", 2),
        ("truncated", 2),
    ],
)
def test_validated_changes_are_applied_once_and_explained(system, workspace, mode, calls):
    repo, actor, project, drafts = workspace
    drafts.save(
        actor,
        "project",
        project["id"],
        "draft",
        {"base_revision_id": project["current_revision_id"], "files": None, "prompt": "Próxima ideia"},
        0,
    )
    gateway = Gateway(mode)
    operation = start(system, workspace, gateway)
    assert operation["state"] == "succeeded", operation
    assert len(gateway.prompts) == calls
    result = operation["result_ref"]
    assert not result["candidate"] and result["kind"] == "implementation"
    assert result["message"] == response()["message"]
    assert result["applied_revision_id"] == result["revision_id"]
    current = repo.project(actor, project["id"])
    assert current["current_revision_id"] == result["revision_id"]
    assert current["version"] == project["version"] + 1
    assert (
        "Melhoria aplicada"
        in repo.source_files(actor, project["id"], result["revision_id"])["public/index.html"]
    )
    draft = drafts.get(actor, "project", project["id"])
    assert draft["data"]["base_revision_id"] == result["revision_id"] and draft["data"]["files"] is None
    assert draft["data"]["prompt"] == "Próxima ideia"
    with repo.sessions() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
        assert attempt.actual_brl == Decimal("0.0625") * calls
        assert (
            db.scalar(select(func.count()).select_from(Revision).where(Revision.project_id == project["id"]))
            == 2
        )
    if mode == "multi_repair":
        assert "Melhoria aplicada" in gateway.prompts[1] and "Melhoria aplicada" in gateway.prompts[2]
        assert "EDIT_TARGET_MISMATCH" in gateway.prompts[2]
    if mode == "edit_mismatch":
        assert "EDIT_TARGET_MISMATCH" in gateway.prompts[1]
        assert "not-the-existing-code" not in gateway.prompts[1]


@pytest.mark.parametrize(
    "mode,state,calls",
    [
        ("invalid", "failed", 3),
        ("uncertain", "reconciling", 1),
        ("cost", "reconciling", 1),
        ("unsafe", "failed", 1),
        ("secret", "failed", 1),
    ],
)
def test_failed_unknown_or_excess_cost_never_changes_the_project(system, workspace, mode, state, calls):
    gateway = Gateway(mode)
    operation = start(system, workspace, gateway)
    repo, actor, project, _ = workspace
    assert operation["state"] == state, operation
    assert len(gateway.prompts) == calls
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    with repo.sessions() as db:
        assert (
            db.scalar(select(func.count()).select_from(Revision).where(Revision.project_id == project["id"]))
            == 1
        )


@pytest.mark.parametrize("edit", ["draft", "revision", "cancel"])
def test_concurrent_edits_or_cancellation_are_preserved(system, workspace, edit):
    repo, actor, project, drafts = workspace
    original = repo.source_files(actor, project["id"], project["current_revision_id"])
    manual = {**original, "public/index.html": original["public/index.html"] + "\n<p>Manual</p>"}

    def change():
        if edit == "draft":
            drafts.save(
                actor,
                "project",
                project["id"],
                "manual-draft",
                {"base_revision_id": project["current_revision_id"], "files": manual, "prompt": ""},
                0,
            )
        elif edit == "revision":
            repo.save_revision(
                actor,
                project["id"],
                "manual-revision",
                {"base_revision_id": project["current_revision_id"], "files": manual},
                project["version"],
            )
        else:
            operation = repo.operations(actor, project_id=project["id"], latest=True)["items"][0]
            repo.cancel(actor, operation["id"], "cancel")

    operation = start(system, workspace, Gateway(after_dispatch=change))
    if edit == "cancel":
        assert operation["state"] == "cancelled"
    else:
        assert operation["state"] == "succeeded", operation
        assert operation["result_ref"]["candidate"] and operation["result_ref"]["apply_conflict"]
        assert "applied_revision_id" not in operation["result_ref"]
        if edit == "draft":
            assert drafts.get(actor, "project", project["id"])["data"]["files"] == manual
        else:
            current = repo.project(actor, project["id"])
            assert repo.source_files(actor, project["id"], current["current_revision_id"]) == manual


def test_question_is_not_a_failed_generation_and_short_answer_keeps_context(system, workspace):
    repo, actor, project, _ = workspace
    gateway = Gateway("clarify")
    question = start(system, workspace, gateway)
    assert question["state"] == "succeeded" and question["progress_stage"] == "awaiting_answer"
    assert question["result_ref"]["kind"] == "clarification"
    assert "revision_id" not in question["result_ref"]
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    gateway.mode = "complete"
    with TestClient(
        create_app(dependencies=(system["config"], system["engine"], repo, system["catalog"], gateway))
    ) as client:
        headers = login(client, system)
        result = reviewed_message(client, headers, project, "answer", "sim", question["id"])
        assert result.status_code == 202, result.text
        assert Worker(repo, gateway, system["catalog"]).tick(result.json()["id"])
        operation = client.get("/api/v1/operations/" + result.json()["id"]).json()
        assert operation["state"] == "succeeded", operation
    assert question["result_ref"]["message"] in gateway.prompts[-1]
    assert "Melhore a apresentação da homepage" in gateway.prompts[-1]


def test_reply_cannot_access_another_account_question(system, workspace):
    question = start(system, workspace, Gateway("clarify"))
    repo = system["repo"]
    actor = system["users"][1]
    project = repo.create_project(
        actor, "second-project", {"name": "Outro", "objective": "Outro projeto isolado"}
    )
    with pytest.raises(RuleError) as caught:
        GenerationService(repo, system["catalog"]).start(
            actor,
            project["id"],
            "answer",
            {
                "brief": "sim",
                "base_revision_id": project["current_revision_id"],
                "policy_version": "2026-10-v1",
                "reply_to": question["id"],
                "auto_apply": True,
            },
            4,
        )
    assert caught.value.code == "NOT_FOUND"
    assert repo.wallet(actor)["reserved_ratio"] == 0


@pytest.mark.parametrize(
    "provider,mode", [("openai", "api_key"), ("gemini", "api_key"), ("gemini", "service_account")]
)
def test_managed_provider_runs_new_contract_from_authenticated_api(system, monkeypatch, provider, mode):
    transport, calls = managed_provider(system, provider, mode)
    dependencies = runtime(system, monkeypatch, transport, system["config"].credential_key_path)
    with TestClient(create_app(dependencies=dependencies)) as client:
        headers = login(client, system)
        project = project_and_draft(client, headers)
        result = reviewed_message(client, headers, project, "reviewed", "Melhore a apresentação da homepage")
        assert result.status_code == 202, result.text
        assert Worker(dependencies[2], dependencies[4], dependencies[3]).tick(result.json()["id"])
        operation = client.get("/api/v1/operations/" + result.json()["id"]).json()
        assert operation["state"] == "succeeded", operation
        assert operation["result_ref"]["candidate"] and "applied_revision_id" not in operation["result_ref"]
        applied = client.post(
            f"/api/v1/projects/{project['id']}/proposals/{operation['id']}/apply",
            headers={**headers, "Idempotency-Key": "apply-reviewed", "If-Match": str(project["version"])},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["revision_id"]
        body = json.loads(calls[-1].content)
        schema = (
            body["text"]["format"]["schema"]
            if provider == "openai"
            else body["generationConfig"]["responseSchema"]
        )
        assert schema["properties"]["action"]["enum"] == ["implement", "clarify"]
        assert set(schema["required"]) == set(turn_schema()["properties"])


def test_google_property_order_matches_the_turn_contract(system, monkeypatch):
    transport, calls = managed_provider(system, "gemini", "service_account")
    dependencies = runtime(system, monkeypatch, transport, system["config"].credential_key_path)
    catalog, gateway = dependencies[3:]
    policy, _ = catalog.resolve("Teste de contrato")
    gateway.generate({**policy, "generation_contract_version": 4}, "Teste de contrato")
    schema = json.loads(calls[-1].content)["generationConfig"]["responseSchema"]
    assert schema["propertyOrdering"] == ["action", "message", "files", "edits", "complete"]
    assert schema["properties"]["files"]["items"]["propertyOrdering"] == ["path", "content"]
    assert schema["properties"]["edits"]["items"]["propertyOrdering"] == ["path", "old", "new"]
    dependencies[1].dispose()
