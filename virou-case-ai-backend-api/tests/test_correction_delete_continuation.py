import json

import pytest
from sqlalchemy import func, select
from test_correction_recovery import OBSOLETE, prepared_correction, response

from viracase.application.conversation_turn import InvalidTurn, parse_turn, turn_prompt
from viracase.application.generation import GenerationService
from viracase.application.ports import ModelResult
from viracase.domain.rules import cost
from viracase.infrastructure.clarifications import Clarifications
from viracase.infrastructure.drafts import Drafts
from viracase.infrastructure.models import Attempt, Revision
from viracase.infrastructure.proposal_review import ProposalReviews
from viracase.infrastructure.worker import Worker


class FollowupGateway:
    def __init__(self, responses):
        self.responses = responses
        self.contexts, self.policies, self.prompts = [], [], []

    def generate(self, policy, prompt):
        self.policies.append(dict(policy))
        self.prompts.append(prompt)
        self.contexts.append(json.loads(prompt[prompt.index('{"stack"'):]))
        body = self.responses[len(self.contexts) - 1]
        if callable(body):
            body = body(self.contexts[-1])
        if isinstance(body, ModelResult):
            return body
        return ModelResult(
            json.dumps(body, ensure_ascii=False),
            {"input": 100, "cached": 0, "output": 100},
            f"followup-synthetic-{len(self.contexts)}",
        )


def replacement(context, marker, **kwargs):
    return response(
        files=[{
            "path": "public/index.html",
            "content": context["files"]["public/index.html"] + f"\n<p>{marker}</p>",
        }],
        **kwargs,
    )


def test_only_confirmed_absent_deletions_can_be_repeated_atomically():
    files = {"src/current.ts": "const current = true;"}
    turn = parse_turn(
        json.dumps(response(
            files=[{"path": "src/current.ts", "content": "const current = false;"}],
            deletes=["src/obsolete.ts"],
        )),
        files,
        allow_deletions=True,
        confirmed_deletions={"src/obsolete.ts"},
    )
    assert turn.files == {"src/current.ts": "const current = false;"}
    assert files == {"src/current.ts": "const current = true;"}


@pytest.mark.parametrize("path", ["src/unknown.ts", "../outside.ts", "/tmp/outside.ts", "a/../b.ts"])
def test_unknown_or_unsafe_delete_never_becomes_an_implicit_success(path):
    files = {"src/current.ts": "const current = true;"}
    with pytest.raises(InvalidTurn) as caught:
        parse_turn(
            json.dumps(response(
                files=[{"path": "src/current.ts", "content": "discard this change"}],
                deletes=[path],
            )),
            files,
            allow_deletions=True,
            confirmed_deletions={"src/obsolete.ts", "../outside.ts", "/tmp/outside.ts", "a/../b.ts"},
        )
    assert caught.value.reason == "DELETE_TARGET_MISMATCH"
    assert caught.value.path == (path if path == "src/unknown.ts" else None)
    assert files == {"src/current.ts": "const current = true;"}


@pytest.mark.parametrize(
    "body,reason",
    [
        (response(deletes=["src/obsolete.ts", "src/obsolete.ts"]), "DUPLICATE_PATH"),
        (response(deletes=["src/obsolete.ts"], complete=False), "EMPTY_BATCH"),
        (response(deletes=["src/obsolete.ts"], action="clarify"), "INVALID_CLARIFICATION"),
        (response(
            deletes=["src/obsolete.ts"], files=[{"path": "src/obsolete.ts", "content": ""}],
        ), "DUPLICATE_PATH"),
        (response(
            deletes=["src/unknown.ts"], accepted_deletions=["src/unknown.ts"],
        ), "INVALID_CONTRACT"),
    ],
)
def test_repeated_deletions_preserve_conflict_and_progress_contracts(body, reason):
    with pytest.raises(InvalidTurn) as caught:
        parse_turn(
            json.dumps(body),
            {"src/current.ts": "preserved"},
            allow_deletions=True,
            confirmed_deletions={"src/obsolete.ts"},
        )
    assert caught.value.reason == reason


def test_confirmed_ledger_cannot_enable_deletion_in_an_ordinary_turn():
    with pytest.raises(InvalidTurn) as caught:
        parse_turn(
            json.dumps(response(deletes=["src/obsolete.ts"])),
            {"src/current.ts": "preserved"},
            confirmed_deletions={"src/obsolete.ts"},
        )
    assert caught.value.reason == "INVALID_CONTRACT"


def test_deletion_feedback_explains_current_state_and_rejected_batch():
    prompt = turn_prompt(
        {"stack": "hono-typescript-d1", "objective": "Aplicação"},
        "Conclua a correção",
        {"src/current.ts": "preserved"},
        feedback="DELETE_TARGET_MISMATCH no arquivo src/unknown.ts",
        allow_deletions=True,
        confirmed_deletions={"src/obsolete.ts"},
    )
    context = json.loads(prompt[prompt.index('{"stack"'):])
    assert context["accepted_deletions"] == ["src/obsolete.ts"]
    assert "src/unknown.ts" in prompt
    assert "Reenvie também as alterações válidas do lote descartado" in prompt
    assert "Não recrie arquivos para poder excluí-los" in prompt


@pytest.mark.parametrize("repeat_only", [False, True])
def test_repeated_delete_after_accepted_batch_finishes_and_applies_one_complete_proposal(system, repeat_only):
    repo, actor, project, before, _, operation, _ = prepared_correction(system)
    deleted = list(OBSOLETE)[:2]
    gateway = FollowupGateway([
        response(deletes=deleted, complete=False),
        response(deletes=deleted) if repeat_only else lambda context: replacement(
            context, "correcao-concluida", deletes=deleted,
        ),
    ])
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    finished = repo.operation(actor, operation["id"])
    assert finished["state"] == "succeeded", finished
    assert finished["result_ref"]["candidate"]
    assert "applied_revision_id" not in finished["result_ref"]
    assert len(gateway.contexts) == 2
    assert len(gateway.contexts[1]["files"]) == 38
    assert gateway.contexts[0]["accepted_deletions"] == []
    assert gateway.contexts[1]["accepted_deletions"] == sorted(deleted)
    assert "Validação do último lote" not in gateway.prompts[1]
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    candidate = repo.source_files(actor, project["id"], finished["result_ref"]["revision_id"])
    assert set(candidate) == set(before) - set(deleted)
    for path, content in candidate.items():
        assert content == (
            before[path] + "\n<p>correcao-concluida</p>"
            if path == "public/index.html" and not repeat_only else before[path]
        )
    review = ProposalReviews(repo).get(actor, project["id"], operation["id"])
    assert {item["path"] for item in review["files"] if item["change"] == "deleted"} == set(deleted)
    drafts = Drafts(repo, None)
    applied = drafts.apply(actor, project["id"], operation["id"], "apply-followup", project["version"])
    assert repo.source_files(actor, project["id"], applied["revision_id"]) == candidate
    assert drafts.apply(actor, project["id"], operation["id"], "apply-followup", project["version"]) == applied
    with repo.sessions() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
        assert len(attempt.usage["calls"]) == 2
        assert attempt.actual_brl == cost(gateway.policies[0], {"input": 200, "cached": 0, "output": 200})


def test_confirmed_deletions_survive_question_and_answer_without_reapplying_code(system):
    repo, actor, project, before, analysis, operation, _ = prepared_correction(system)
    deleted = list(OBSOLETE)[:2]
    gateway = FollowupGateway([
        response(deletes=deleted, complete=False),
        response(action="clarify", message="Confirma a remoção dos módulos antigos?", deletes=[]),
        lambda context: replacement(context, "resposta-aplicavel", deletes=deleted),
    ])
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    question = repo.operation(actor, operation["id"])
    assert question["result_ref"]["kind"] == "clarification"
    assert repo.source_files(actor, project["id"], project["current_revision_id"]) == before
    reply = Clarifications(repo).reply(
        actor, project["id"], question["id"], "answer-correction",
        {
            "message_text": "Sim, preserve o restante e conclua.",
            "base_project_revision": project["current_revision_id"],
            "policy_version": "2026-10-v1",
        },
        GenerationService(repo, system["catalog"]),
    )
    assert Worker(repo, gateway, system["catalog"]).tick(reply["id"])
    final = repo.operation(actor, reply["id"])
    assert final["state"] == "succeeded" and final["result_ref"]["candidate"], final
    assert final["analysis_id"] == analysis["id"]
    assert final["reply_to"] == question["id"]
    assert gateway.contexts[-1]["accepted_deletions"] == sorted(deleted)
    assert all(path not in gateway.contexts[-1]["files"] for path in deleted)
    applied = Drafts(repo, None).apply(
        actor, project["id"], reply["id"], "apply-after-answer", project["version"],
    )
    files = repo.source_files(actor, project["id"], applied["revision_id"])
    assert set(files) == set(before) - set(deleted)
    assert "resposta-aplicavel" in files["public/index.html"]


def test_ledger_tracks_created_deleted_and_recreated_files_between_accepted_batches(system):
    repo, actor, project, before, _, operation, _ = prepared_correction(system)
    obsolete = next(iter(OBSOLETE))
    temporary = "public/temporary-correction.js"
    gateway = FollowupGateway([
        response(files=[{"path": temporary, "content": "const temporary = true;"}], complete=False),
        response(deletes=[obsolete, temporary], complete=False),
        response(files=[{"path": obsolete, "content": "const restored = true;"}], complete=False),
        response(deletes=[obsolete, temporary]),
    ])
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "succeeded", final
    assert gateway.contexts[2]["accepted_deletions"] == sorted([obsolete, temporary])
    assert gateway.contexts[3]["accepted_deletions"] == [temporary]
    assert obsolete in gateway.contexts[3]["files"]
    candidate = repo.source_files(actor, project["id"], final["result_ref"]["revision_id"])
    assert candidate == {path: content for path, content in before.items() if path != obsolete}


def test_unknown_delete_repairs_with_exact_path_and_discards_the_entire_invalid_batch(system):
    repo, actor, project, before, _, operation, _ = prepared_correction(system)
    first, second = list(OBSOLETE)[:2]
    unknown = "src/not-in-this-project.ts"
    gateway = FollowupGateway([
        response(deletes=[first], complete=False),
        lambda context: replacement(context, "invalid-batch-must-disappear", deletes=[second, unknown]),
        lambda context: replacement(context, "valid-repaired-batch", deletes=[second]),
    ])
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "succeeded", final
    assert "DELETE_TARGET_MISMATCH" in gateway.prompts[2] and unknown in gateway.prompts[2]
    assert gateway.contexts[2]["accepted_deletions"] == [first]
    assert second in gateway.contexts[2]["files"]
    assert gateway.contexts[2]["files"]["public/index.html"] == before["public/index.html"]
    candidate = repo.source_files(actor, project["id"], final["result_ref"]["revision_id"])
    assert set(candidate) == set(before) - {first, second}
    assert "valid-repaired-batch" in candidate["public/index.html"]
    assert "invalid-batch-must-disappear" not in candidate["public/index.html"]


def test_truncated_deletion_is_never_confirmed_or_reused(system):
    repo, actor, project, before, _, operation, _ = prepared_correction(system)
    deleted = next(iter(OBSOLETE))
    gateway = FollowupGateway([
        ModelResult(
            json.dumps(response(deletes=[deleted], complete=False)),
            {"input": 100, "cached": 0, "output": 100},
            "synthetic-truncated",
            output_error="GENERATION_TRUNCATED",
        ),
        response(deletes=[deleted]),
    ])
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "succeeded", final
    assert gateway.contexts[1]["accepted_deletions"] == []
    assert gateway.contexts[1]["files"] == before
    candidate = repo.source_files(actor, project["id"], final["result_ref"]["revision_id"])
    assert set(candidate) == set(before) - {deleted}


def test_persistent_unknown_delete_stops_at_existing_limit_and_preserves_saved_revision(system):
    repo, actor, project, before, _, operation, _ = prepared_correction(system)
    gateway = FollowupGateway([
        response(deletes=[next(iter(OBSOLETE))], complete=False),
        *[response(deletes=["src/never-existed.ts"])] * 3,
    ])
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "failed" and final["error_code"] == "GENERATION_DELETE_MISMATCH", final
    assert "A IA solicitou a exclusão de um arquivo" in final["progress_message"]
    assert "Sua revisão foi preservada" in final["progress_message"]
    assert "src/never-existed.ts" not in final["progress_message"]
    assert "revision_id" not in final["result_ref"]
    assert len(gateway.contexts) == 4
    assert repo.source_files(actor, project["id"], project["current_revision_id"]) == before
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Revision).where(
            Revision.project_id == project["id"],
        )) == 2
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
        assert len(attempt.usage["calls"]) == 4
        assert attempt.actual_brl == cost(gateway.policies[0], {"input": 400, "cached": 0, "output": 400})
