import json

import pytest
from sqlalchemy import func, select
from test_conversation_runtime import fund_generation, managed_provider, runtime

from viracase.application.conversation_turn import InvalidTurn, parse_turn, turn_schema
from viracase.application.generation import GenerationService
from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError, cost
from viracase.infrastructure.code_analysis import CodeAnalysis
from viracase.infrastructure.drafts import Drafts
from viracase.infrastructure.models import Account, Attempt, Revision
from viracase.infrastructure.proposal_review import ProposalReviews
from viracase.infrastructure.worker import Worker

OBSOLETE = {
    "src/domain/user.ts": "",
    "src/application/authenticate-user.ts": "export const legacyAuthentication = true;\n",
    "src/infrastructure/d1-user-repository.ts": "export const legacyRepository = true;\n",
    "src/infrastructure/health-check-service.ts": "export const legacyHealth = true;\n",
}


def response(**changes):
    return {
        "action": "implement",
        "message": "Corrigi os arquivos identificados e preservei o restante do projeto.",
        "files": [],
        "edits": [],
        "complete": True,
        **changes,
    }


def test_explicit_deletion_handles_empty_files_without_changing_the_input():
    before = {**OBSOLETE, "public/preserved.js": "const preserved = true;\r\n"}
    original = dict(before)
    result = parse_turn(json.dumps(response(deletes=list(OBSOLETE))), before, allow_deletions=True)
    assert result.files == {"public/preserved.js": original["public/preserved.js"]}
    assert before == original
    assert result.complete


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"deletes": ["src/domain/user.ts", "src/domain/user.ts"]}, "DUPLICATE_PATH"),
        ({"deletes": ["src/not-present.ts"]}, "DELETE_TARGET_MISMATCH"),
        ({"deletes": ["../outside.ts"]}, "DELETE_TARGET_MISMATCH"),
        ({"deletes": ["/tmp/outside.ts"]}, "DELETE_TARGET_MISMATCH"),
        ({"deletes": [""]}, "INVALID_DELETION"),
        ({"deletes": [None]}, "INVALID_DELETION"),
        ({"deletes": "src/domain/user.ts"}, "INVALID_CONTRACT"),
        ({"deletes": ["src/domain/user.ts"] * 81}, "INVALID_CONTRACT"),
        (
            {"files": [{"path": "src/domain/user.ts", "content": ""}], "deletes": ["src/domain/user.ts"]},
            "DUPLICATE_PATH",
        ),
        (
            {
                "edits": [{"path": "src/application/authenticate-user.ts", "old": "true", "new": "false"}],
                "deletes": ["src/application/authenticate-user.ts"],
            },
            "DUPLICATE_PATH",
        ),
        ({"action": "clarify", "deletes": ["src/domain/user.ts"]}, "INVALID_CLARIFICATION"),
    ],
)
def test_invalid_or_conflicting_deletions_are_atomic(changes, reason):
    before = dict(OBSOLETE)
    with pytest.raises(InvalidTurn) as caught:
        parse_turn(json.dumps(response(**changes)), before, allow_deletions=True)
    assert caught.value.reason == reason
    assert before == OBSOLETE


def test_deletion_cannot_be_enabled_by_the_model_in_a_normal_conversation():
    with pytest.raises(InvalidTurn) as caught:
        parse_turn(json.dumps(response(deletes=["src/domain/user.ts"])), OBSOLETE)
    assert caught.value.reason == "INVALID_CONTRACT"
    assert "deletes" not in turn_schema()["properties"]


def test_full_file_recovery_rejects_even_a_valid_exact_edit():
    document = response(edits=[{"path": "src/a.ts", "old": "true", "new": "false"}])
    with pytest.raises(InvalidTurn) as caught:
        parse_turn(json.dumps(document), {"src/a.ts": "const active = true;"}, full_files=True)
    assert caught.value.reason == "FULL_FILES_REQUIRED"
    assert "edits" not in turn_schema(full_files=True)["properties"]


def test_empty_content_remains_a_file_in_full_file_recovery():
    original = {"src/a.ts": "const value = 1;\n", "src/b.ts": ""}
    result = parse_turn(
        json.dumps(
            response(files=[{"path": "src/a.ts", "content": ""}, {"path": "src/c.ts", "content": ""}])
        ),
        original,
        allow_deletions=True,
        full_files=True,
    )
    assert result.files == {"src/a.ts": "", "src/b.ts": "", "src/c.ts": ""}
    assert original["src/a.ts"] == "const value = 1;\n"


def test_clarification_can_return_an_empty_deletion_list_without_mutating_files():
    result = parse_turn(
        json.dumps(response(action="clarify", deletes=[], message="Deseja manter o módulo antigo?")),
        OBSOLETE,
        allow_deletions=True,
    )
    assert result.action == "clarify" and result.files == OBSOLETE


class CorrectionGateway:
    def __init__(self, mode="delete", on_correction=None):
        self.mode, self.on_correction = mode, on_correction
        self.calls, self.policies, self.contexts = [], [], []

    def generate(self, policy, prompt):
        self.calls.append((dict(policy), prompt))
        context = json.loads(prompt[prompt.index('{"stack"') :])
        if policy.get("code_analysis_contract"):
            body = {
                "summary": "Foram identificados arquivos redundantes que podem ser removidos.",
                "issues": [
                    {
                        "severity": "error",
                        "path": path,
                        "line": 0,
                        "title": "Arquivo redundante",
                        "explanation": "A revisão contém uma implementação antiga sem uso.",
                        "suggestion": "Remova somente este arquivo preservando os demais.",
                    }
                    for path in OBSOLETE
                ],
                "limitations": ["Resposta sintética para validar o contrato de correção."],
            }
        else:
            self.policies.append(dict(policy))
            self.contexts.append(context)
            number = len(self.policies)
            if self.on_correction:
                self.on_correction(number)
            if self.mode in {"recover", "persistent_edits"} and number == 1:
                body = response(edits=[{"path": "src/domain/user.ts", "old": "", "new": ""}])
            elif self.mode == "persistent_edits":
                body = response(
                    edits=[{"path": "public/index.html", "old": "</body>", "new": "<p>x</p></body>"}]
                )
            elif self.mode == "required":
                body = response(deletes=["src/index.ts"])
            elif self.mode == "partial":
                paths = list(OBSOLETE)
                body = response(deletes=paths[:2] if number == 1 else paths[2:], complete=number > 1)
            else:
                body = response(deletes=list(OBSOLETE))
                if self.mode == "recover":
                    body.pop("edits")
                    body["files"] = [
                        {
                            "path": "public/index.html",
                            "content": context["files"]["public/index.html"] + "\n<p>Corrigido</p>",
                        }
                    ]
        return ModelResult(
            json.dumps(body), {"input": 100, "cached": 0, "output": 100}, "synthetic-correction"
        )


def prepared_correction(system, mode="delete", on_correction=None):
    fund_generation(system)
    policy = {
        **system["policy"],
        "input_usd_per_million": "0",
        "cached_usd_per_million": "0",
        "output_usd_per_million": "125",
    }
    system["config"].catalog_path.write_text(json.dumps({"models": [policy]}))
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor, "correction-project", {"name": "Correção", "objective": "Aplicação de tarefas"}
    )
    before = repo.source_files(actor, project["id"], project["current_revision_id"])
    before.update(OBSOLETE)
    while len(before) < 40:
        number = len(before)
        before[f"public/preserved-{number}.js"] = (
            f"export const value{number} = " + json.dumps("x" * 1500) + ";\n"
        )
    saved = repo.save_revision(
        actor,
        project["id"],
        "seed-redundant-files",
        {"base_revision_id": project["current_revision_id"], "files": before},
        project["version"],
    )
    project = saved["project"]
    gateway = CorrectionGateway(mode, on_correction)
    service = CodeAnalysis(repo, system["catalog"])
    analysis = service.start(
        actor,
        project["id"],
        "analysis-for-correction",
        {
            "revision_id": project["current_revision_id"],
            "source": "publication",
            "publication_operation_id": None,
            "runtime_error": None,
            "policy_version": "2026-10-v1",
        },
    )
    assert Worker(repo, gateway, system["catalog"]).tick(analysis["id"])
    analysis = repo.operation(actor, analysis["id"])
    assert analysis["state"] == "succeeded", analysis
    operation = service.correct(
        actor,
        project["id"],
        analysis["id"],
        "correction",
        {"base_project_revision": project["current_revision_id"], "policy_version": "2026-10-v1"},
        GenerationService(repo, system["catalog"]),
    )
    return repo, actor, project, before, analysis, operation, gateway


@pytest.mark.parametrize("mode,calls", [("delete", 1), ("partial", 2), ("recover", 2)])
def test_correction_removes_redundant_files_and_recovers_as_complete_files(system, mode, calls):
    repo, actor, project, before, _, operation, gateway = prepared_correction(system, mode)
    checkpoints = []

    def capture_progress(number):
        if mode == "partial" and number == 2:
            checkpoints.extend(repo.operation(actor, operation["id"])["result_ref"]["working_files"])

    gateway.on_correction = capture_progress
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "succeeded", result
    assert len(gateway.policies) == calls
    assert all(policy["generation_allow_deletions"] for policy in gateway.policies)
    assert result["result_ref"]["candidate"] and "applied_revision_id" not in result["result_ref"]
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    candidate = repo.source_files(actor, project["id"], result["result_ref"]["revision_id"])
    assert set(candidate) == set(before) - set(OBSOLETE)
    for path in candidate:
        expected = (
            before[path] + "\n<p>Corrigido</p>"
            if mode == "recover" and path == "public/index.html"
            else before[path]
        )
        assert candidate[path] == expected
    changed = {item["path"]: item["change"] for item in result["result_ref"]["changed_files"]}
    assert all(changed[path] == "deleted" for path in OBSOLETE)
    review = ProposalReviews(repo).get(actor, project["id"], operation["id"])
    deleted = [item for item in review["files"] if item["change"] == "deleted"]
    assert {item["path"] for item in deleted} == set(OBSOLETE)
    empty = next(item for item in deleted if item["path"] == "src/domain/user.ts")
    detail = ProposalReviews(repo).file(
        actor, project["id"], operation["id"], empty["id"], review["proposal_version"]
    )
    assert detail["before"] == "" and detail["after"] is None
    with pytest.raises(RuleError) as denied:
        ProposalReviews(repo).get(system["users"][1], project["id"], operation["id"])
    assert denied.value.code == "NOT_FOUND"
    applied = Drafts(repo, None).apply(
        actor, project["id"], operation["id"], "apply-correction", project["version"]
    )
    assert repo.source_files(actor, project["id"], applied["revision_id"]) == candidate
    assert (
        Drafts(repo, None).apply(
            actor, project["id"], operation["id"], "apply-correction", project["version"]
        )
        == applied
    )
    with repo.sessions() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
        assert attempt.actual_brl == cost(
            gateway.policies[0], {"input": 100 * calls, "cached": 0, "output": 100 * calls}
        )
        assert len(attempt.usage["calls"]) == calls
    if mode == "partial":
        assert len(gateway.contexts[1]["files"]) == 38
        assert checkpoints == sorted(list(OBSOLETE)[:2])
    if mode == "recover":
        assert gateway.policies[0]["generation_full_files"] is False
        assert gateway.policies[1]["generation_full_files"] is True
        prompt = gateway.calls[-1][1]
        assert "EDIT_TARGET_MISMATCH" in prompt and "src/domain/user.ts" in prompt
        assert "action, message, files, deletes, complete" in prompt
        assert "edits é uma lista" not in prompt


@pytest.mark.parametrize(
    "mode,code,calls",
    [("required", "STACK_MISMATCH", 1), ("persistent_edits", "GENERATION_EDIT_MISMATCH", 3)],
)
def test_invalid_correction_never_saves_a_partial_revision_or_extends_retries(system, mode, code, calls):
    repo, actor, project, before, _, operation, gateway = prepared_correction(system, mode)
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "failed" and result["error_code"] == code, result
    assert len(gateway.policies) == calls
    assert "revision_id" not in result["result_ref"]
    assert repo.source_files(actor, project["id"], project["current_revision_id"]) == before
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    with repo.sessions() as db:
        assert (
            db.scalar(select(func.count()).select_from(Revision).where(Revision.project_id == project["id"]))
            == 2
        )


def test_permission_revoked_during_correction_prevents_deletions(system):
    repo, actor = system["repo"], system["users"][0]

    def suspend(_number):
        with repo.sessions.begin() as db:
            db.get(Account, actor).suspended = True

    repo, actor, project, before, _, operation, gateway = prepared_correction(system, on_correction=suspend)
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "failed" and result["error_code"] == "ACCESS_EXPIRED"
    assert len(gateway.policies) == 1
    assert repo.source_files(actor, project["id"], project["current_revision_id"]) == before
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


@pytest.mark.parametrize(
    "provider,mode", [("openai", "api_key"), ("gemini", "api_key"), ("gemini", "service_account")]
)
def test_provider_recovery_schema_removes_edits_and_keeps_deletion_order(system, monkeypatch, provider, mode):
    transport, calls = managed_provider(system, provider, mode)
    dependencies = runtime(system, monkeypatch, transport, system["config"].credential_key_path)
    catalog, gateway = dependencies[3:]
    policy, _ = catalog.resolve("Contrato de recuperação de correção")
    gateway.generate(
        {
            **policy,
            "generation_contract_version": 4,
            "generation_allow_deletions": True,
            "generation_full_files": True,
        },
        "Teste de contrato",
    )
    body = json.loads(calls[-1].content)
    schema = (
        body["text"]["format"]["schema"]
        if provider == "openai"
        else body["generationConfig"]["responseSchema"]
    )
    expected = ["action", "message", "files", "deletes", "complete"]
    assert list(schema["properties"]) == expected
    assert schema["required"] == expected
    assert schema["properties"]["deletes"]["items"]["type"].lower() == "string"
    if provider == "gemini":
        assert schema["propertyOrdering"] == expected
    dependencies[1].dispose()
