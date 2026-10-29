import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_conversation_runtime import login
from test_integrated_workspace import workspace as workspace_fixture

from viracase.application.generation import GenerationService
from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError, canonical_hash
from viracase.infrastructure.clarifications import Clarifications
from viracase.infrastructure.guided_build import GuidedBuild
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Operation, Reservation, Revision
from viracase.infrastructure.providers import ProviderUncertain
from viracase.infrastructure.worker import Worker
from viracase.interfaces.api import create_app

workspace = workspace_fixture


ORIGINAL_REQUEST = "Implemente o carrinho autenticado e preserve a vitrine da padaria."
QUESTION = "Podemos usar uma sessão vinculada ao ID real do cliente?"
ATTACHMENT = {
    "id": "clarification-template-snapshot",
    "source_template_id": "autorizacao",
    "source_version_id": "clarification-template-v1",
    "title": "Autenticação revisada",
    "category": "seguranca",
    "version": 1,
    "content": "CONTEXTO_REVISADO_ORIGINAL: cada compra pertence ao cliente autenticado.",
    "fields": {},
    "attachment_revision": 1,
}


class ClarificationGateway:
    def __init__(self, responses):
        self.responses = responses
        self.contexts = []
        self.policies = []

    def generate(self, policy, prompt):
        self.contexts.append(json.loads(prompt[prompt.index('{"stack"') :]))
        self.policies.append(dict(policy))
        response = self.responses[len(self.contexts) - 1]
        if callable(response):
            response = response(self.contexts[-1])
        return ModelResult(
            json.dumps(response, ensure_ascii=False),
            {"input": 100, "cached": 0, "output": 100},
            f"clarification-synthetic-{len(self.contexts)}",
        )


def clarify(message=QUESTION):
    return {"action": "clarify", "message": message, "files": [], "edits": [], "complete": True}


def implement(marker="resposta-confirmada", complete=True):
    return {
        "action": "implement",
        "message": f"Preparei {marker} para revisão.",
        "files": [],
        "edits": [{"path": "public/index.html", "old": "</body>", "new": f"<p>{marker}</p></body>"}],
        "complete": complete,
    }


def begin(system, workspace, gateway, key="clarification-question", **overrides):
    repo, actor, project, _ = workspace
    operation = GenerationService(repo, system["catalog"]).start(
        actor,
        project["id"],
        key,
        {
            "brief": ORIGINAL_REQUEST,
            "base_revision_id": project["current_revision_id"],
            "policy_version": "2026-10-v1",
            "review_required": True,
            "attachment_snapshots": [copy.deepcopy(ATTACHMENT)],
            **overrides,
        },
        contract_version=4,
    )
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    return repo.operation(actor, operation["id"])


def client_for(system, gateway):
    return TestClient(
        create_app(
            dependencies=(system["config"], system["engine"], system["repo"], system["catalog"], gateway)
        )
    )


def reply(client, headers, project, question, key="clarification-answer", text="Sim", **overrides):
    return client.post(
        f"/api/v1/projects/{project['id']}/messages/{question['id']}/reply",
        headers={**headers, "Idempotency-Key": key},
        json={
            "message_text": text,
            "base_project_revision": project["current_revision_id"],
            "policy_version": "2026-10-v1",
            **overrides,
        },
    )


def test_legacy_reply_preserves_the_original_reviewed_attachment(system, workspace):
    question = begin(system, workspace, ClarificationGateway([clarify()]))
    answer_gateway = ClarificationGateway([implement()])
    operation = begin(
        system,
        workspace,
        answer_gateway,
        key="legacy-answer",
        brief="Sim",
        reply_to=question["id"],
        attachment_snapshots=[],
    )
    assert operation["state"] == "succeeded", operation
    serialized = json.dumps(answer_gateway.contexts[0], ensure_ascii=False)
    assert ATTACHMENT["content"] in serialized
    assert ORIGINAL_REQUEST in serialized and QUESTION in serialized


def test_legacy_reply_resumes_accepted_files_before_the_question(system, workspace):
    repo, actor, project, _ = workspace
    gateway = ClarificationGateway([implement("carrinho-parcial-preservado", False), clarify()])
    question = begin(system, workspace, gateway)
    assert question["state"] == "succeeded" and question["result_ref"]["kind"] == "clarification"
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    with repo.sessions() as db:
        assert (
            db.scalar(select(func.count()).select_from(Revision).where(Revision.project_id == project["id"]))
            == 1
        )
    answer_gateway = ClarificationGateway([implement()])
    operation = begin(
        system,
        workspace,
        answer_gateway,
        key="legacy-checkpoint-answer",
        brief="Sim",
        reply_to=question["id"],
        attachment_snapshots=[],
    )
    assert operation["state"] == "succeeded", operation
    assert "carrinho-parcial-preservado" in answer_gateway.contexts[0]["files"]["public/index.html"]
    files = repo.source_files(actor, project["id"], operation["result_ref"]["revision_id"])
    assert "carrinho-parcial-preservado" in files["public/index.html"]
    assert "resposta-confirmada" in files["public/index.html"]


def test_direct_reply_does_not_require_new_preparation_or_change_the_build_draft(system, workspace):
    repo, actor, project, drafts = workspace
    question = begin(system, workspace, ClarificationGateway([clarify()]))
    flow = GuidedBuild(repo, None)
    initial = flow.get(actor, project["id"])
    flow.save(
        actor,
        project["id"],
        "next-idea-draft",
        0,
        {**initial["data"], "message_text": "Próximo pedido ainda em edição"},
    )
    draft_before = flow.get(actor, project["id"])
    gateway = ClarificationGateway([implement()])
    with client_for(system, gateway) as client:
        headers = login(client, system)
        response = reply(client, headers, project, question, text="  Sim  ")
        assert response.status_code == 202, response.text
        operation = response.json()
        assert operation["reply_to"] == question["id"] and operation["request_text"] == "Sim"
        assert GuidedBuild(repo, None).get(actor, project["id"]) == draft_before
        assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
        final = repo.operation(actor, operation["id"])
        assert final["state"] == "succeeded" and final["result_ref"]["candidate"]
        assert "applied_revision_id" not in final["result_ref"]
        assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
        applied = drafts.apply(
            actor, project["id"], operation["id"], "apply-clarification", project["version"]
        )
        assert (
            "resposta-confirmada"
            in repo.source_files(actor, project["id"], applied["revision_id"])["public/index.html"]
        )
    assert gateway.policies[0]["max_batches"] == 10


def test_guided_send_keeps_the_conversation_stage_after_reload(system, workspace):
    from workflow_http import reviewed_message

    repo, _, project, _ = workspace
    gateway = ClarificationGateway([clarify()])
    with client_for(system, gateway) as client:
        headers = login(client, system)
        response = reviewed_message(client, headers, project, "guided-question", ORIGINAL_REQUEST)
        assert response.status_code == 202, response.text
        assert Worker(repo, gateway, system["catalog"]).tick(response.json()["id"])
        draft = client.get(f"/api/v1/projects/{project['id']}/build-draft").json()
        assert draft["data"]["stage"] == "conversation"
        assert draft["data"]["message_text"] == ""


def test_several_questions_keep_the_entire_origin_and_all_answers(system, workspace):
    repo, actor, project, _ = workspace
    original = ORIGINAL_REQUEST + " Detalhamento aprovado." * 180 + " FIM_DO_PEDIDO_ORIGINAL"
    questions = [f"Confirme a decisão número {index}?" for index in range(7)]
    parent = begin(system, workspace, ClarificationGateway([clarify(questions[0])]), brief=original)
    gateway = ClarificationGateway([*(clarify(question) for question in questions[1:]), implement()])
    with client_for(system, gateway) as client:
        headers = login(client, system)
        for index in range(7):
            response = reply(
                client, headers, project, parent, key=f"answer-{index}", text=f"Sim, decisão {index}"
            )
            assert response.status_code == 202, response.text
            assert Worker(repo, gateway, system["catalog"]).tick(response.json()["id"])
            parent = repo.operation(actor, response.json()["id"])
            assert parent["state"] == "succeeded", parent
    final = gateway.contexts[-1]
    assert final["history"][0]["request"] == original
    assert [item["response"] for item in final["history"]] == questions
    assert [item["request"] for item in final["history"]][1:] == [
        f"Sim, decisão {index}" for index in range(6)
    ]
    assert json.loads(final["request"])["untrusted_user_attachments"] == [ATTACHMENT]


def test_checkpoint_stays_private_and_survives_two_questions_and_a_new_worker(system, workspace, caplog):
    repo, actor, project, _ = workspace
    parent = begin(
        system, workspace, ClarificationGateway([implement("trecho-privado-checkpoint", False), clarify()])
    )
    with repo.sessions() as db:
        persisted = db.get(Operation, parent["id"])
        assert (
            persisted.payload["clarification_checkpoint"]["files"]["public/index.html"].count(
                "trecho-privado-checkpoint"
            )
            == 1
        )
    assert "clarification_checkpoint" not in json.dumps(parent)
    assert "trecho-privado-checkpoint" not in json.dumps(parent)
    gateway = ClarificationGateway([clarify("Posso concluir agora?"), implement()])
    with client_for(system, gateway) as client:
        headers = login(client, system)
        for index in range(2):
            response = reply(client, headers, project, parent, key=f"checkpoint-answer-{index}")
            assert response.status_code == 202, response.text
            assert Worker(repo, gateway, system["catalog"]).tick(response.json()["id"])
            parent = repo.operation(actor, response.json()["id"])
            assert parent["state"] == "succeeded", parent
    assert all("trecho-privado-checkpoint" in ctx["files"]["public/index.html"] for ctx in gateway.contexts)
    assert "trecho-privado-checkpoint" not in caplog.text
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


def test_retry_with_the_same_key_returns_the_original_operation_after_configuration_changes(
    system, workspace
):
    repo, _, project, _ = workspace
    parent = begin(system, workspace, ClarificationGateway([clarify()]))
    gateway = ClarificationGateway([implement()])
    with client_for(system, gateway) as client:
        headers = login(client, system)
        response = reply(client, headers, project, parent)
        assert response.status_code == 202, response.text
        oid = response.json()["id"]
        integrations = Integrations(repo)
        integrations.save(
            system["admin"],
            "jev",
            "reply-policy-change",
            integrations.get("jev")["version"],
            {"settings": {"max_generation_calls": 4}, "reason": "Política posterior à resposta já admitida"},
        )
        replay = reply(client, headers, project, parent)
        assert replay.status_code == 202 and replay.json()["id"] == oid
        assert Worker(repo, gateway, system["catalog"]).tick(oid)
        replay = reply(client, headers, project, parent)
        assert replay.status_code == 202 and replay.json()["id"] == oid
        changed = reply(client, headers, project, parent, text="Resposta diferente")
        assert changed.status_code == 409 and changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(gateway.contexts) == 1 and gateway.policies[0]["max_batches"] == 10
    with repo.sessions() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.payload["reply_to"].as_string() == parent["id"])
            )
            == 1
        )
        assert (
            db.scalar(select(func.count()).select_from(Reservation).where(Reservation.operation_id == oid))
            == 1
        )


@pytest.mark.parametrize("state", ["queued", "succeeded", "reconciling"])
def test_a_question_cannot_receive_a_second_active_or_successful_answer(system, workspace, state):
    repo, actor, project, _ = workspace
    parent = begin(system, workspace, ClarificationGateway([clarify()]))

    def unknown(_):
        raise ProviderUncertain("synthetic-clarification-unknown")

    gateway = ClarificationGateway([unknown if state == "reconciling" else clarify("Próxima decisão?")])
    with client_for(system, gateway) as client:
        headers = login(client, system)
        first = reply(client, headers, project, parent)
        assert first.status_code == 202, first.text
        if state != "queued":
            assert Worker(repo, gateway, system["catalog"]).tick(first.json()["id"])
        assert repo.operation(actor, first.json()["id"])["state"] == state
        wallet = repo.wallet(actor, True)
        duplicate = reply(client, headers, project, parent, key="duplicate-answer")
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "CLARIFICATION_ANSWERED"
        assert repo.wallet(actor, True) == wallet


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_a_failed_or_cancelled_reply_can_be_retried_without_replaying_its_provider_call(
    system, workspace, outcome
):
    repo, actor, project, _ = workspace
    parent = begin(system, workspace, ClarificationGateway([clarify()]))
    invalid = {"action": "implement", "message": "Inválido", "files": [], "edits": [], "complete": True}
    gateway = ClarificationGateway([invalid, invalid, invalid, implement()])
    with client_for(system, gateway) as client:
        headers = login(client, system)
        first = reply(client, headers, project, parent)
        assert first.status_code == 202, first.text
        if outcome == "cancelled":
            repo.cancel(actor, first.json()["id"], "cancel-answer")
            gateway.responses = [implement()]
        else:
            assert Worker(repo, gateway, system["catalog"]).tick(first.json()["id"])
        assert repo.operation(actor, first.json()["id"])["state"] == outcome
        assert not Worker(repo, gateway, system["catalog"]).tick(first.json()["id"])
        again = reply(client, headers, project, parent, key="retry-answer")
        assert again.status_code == 202 and again.json()["id"] != first.json()["id"], again.text
        assert Worker(repo, gateway, system["catalog"]).tick(again.json()["id"])
        assert repo.operation(actor, again.json()["id"])["state"] == "succeeded"
    assert len(gateway.contexts) == (1 if outcome == "cancelled" else 4)


@pytest.mark.parametrize("variant", ["other_account", "other_project", "not_question", "unknown"])
def test_reply_rejects_unavailable_or_unowned_questions_before_reserving(system, workspace, variant):
    repo, actor, project, _ = workspace
    parent = begin(
        system, workspace, ClarificationGateway([implement() if variant == "not_question" else clarify()])
    )
    if variant == "unknown":
        parent = {"id": "00000000-0000-0000-0000-000000000000"}
    if variant in {"other_account", "other_project"}:
        if variant == "other_account":
            actor = system["users"][1]
        project = repo.create_project(
            actor,
            "other-reply-project",
            {"name": "Outro contexto", "objective": "Projeto separado da pergunta original"},
        )
    wallet = repo.wallet(actor, True)
    with pytest.raises(RuleError) as caught:
        Clarifications(repo).reply(
            actor,
            project["id"],
            parent["id"],
            "unowned-reply",
            {
                "message_text": "Sim",
                "base_project_revision": project["current_revision_id"],
                "policy_version": "2026-10-v1",
            },
            GenerationService(repo, system["catalog"]),
        )
    assert caught.value.code == "NOT_FOUND"
    assert repo.wallet(actor, True) == wallet


@pytest.mark.parametrize("variant", ["stale_revision", "unsaved_draft", "pending_proposal"])
def test_reply_preserves_local_edits_and_existing_proposals(system, workspace, variant):
    repo, actor, project, drafts = workspace
    parent = begin(system, workspace, ClarificationGateway([clarify()]))
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    files["public/index.html"] += "\n<!-- edição manual preservada -->"
    expected = "DRAFT_NOT_SAVED"
    if variant == "stale_revision":
        repo.save_revision(
            actor,
            project["id"],
            "manual-during-question",
            {"base_revision_id": project["current_revision_id"], "files": files},
            project["version"],
        )
        expected = "CONTEXT_CHANGED"
    elif variant == "unsaved_draft":
        drafts.save(
            actor,
            "project",
            project["id"],
            "manual-unsaved",
            {"base_revision_id": project["current_revision_id"], "files": files, "prompt": ""},
            0,
        )
    else:
        begin(system, workspace, ClarificationGateway([implement()]), key="other-proposal")
        expected = "PROPOSAL_PENDING"
    wallet = repo.wallet(actor, True)
    with client_for(system, ClarificationGateway([])) as client:
        response = reply(client, login(client, system), project, parent)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == expected
    assert repo.wallet(actor, True) == wallet
    if variant == "unsaved_draft":
        assert drafts.get(actor, "project", project["id"])["data"]["files"] == files


@pytest.mark.parametrize("variant", ["hash", "base", "empty", "unsafe_path"])
def test_corrupt_checkpoints_are_rejected_without_exposure_or_new_consumption(system, workspace, variant):
    repo, actor, project, _ = workspace
    parent = begin(
        system, workspace, ClarificationGateway([implement("checkpoint-original", False), clarify()])
    )
    with repo.sessions.begin() as db:
        operation = db.get(Operation, parent["id"])
        payload = copy.deepcopy(operation.payload)
        checkpoint = payload["clarification_checkpoint"]
        if variant == "hash":
            checkpoint["files"]["public/index.html"] += "alterado"
        elif variant == "base":
            checkpoint["base_hash"] = "0" * 64
        elif variant == "empty":
            payload["clarification_checkpoint"] = {}
        else:
            checkpoint["files"]["../private"] = "invalid"
            checkpoint["hash"] = canonical_hash({k: v for k, v in checkpoint.items() if k != "hash"})
        operation.payload = payload
    wallet = repo.wallet(actor, True)
    with client_for(system, ClarificationGateway([])) as client:
        response = reply(client, login(client, system), project, parent)
        assert response.status_code in {400, 409}, response.text
        assert response.json()["error"]["code"] == (
            "INVALID_PATH" if variant == "unsafe_path" else "GENERATION_CHECKPOINT_INVALID"
        )
        assert "checkpoint-original" not in response.text
    assert repo.wallet(actor, True) == wallet


@pytest.mark.parametrize(
    "payload",
    [
        {"message_text": " "},
        {"message_text": "x" * 12001},
        {"history": []},
        {"clarification_checkpoint": {}},
        {"max_generation_calls": 100},
        {"attachment_snapshots": []},
    ],
)
def test_reply_schema_does_not_accept_context_or_policy_overrides(system, workspace, payload):
    repo, actor, project, _ = workspace
    parent = begin(system, workspace, ClarificationGateway([clarify()]))
    wallet = repo.wallet(actor, True)
    with client_for(system, ClarificationGateway([])) as client:
        response = reply(client, login(client, system), project, parent, **payload)
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert repo.wallet(actor, True) == wallet


def test_two_concurrent_replies_admit_only_one_child_operation(system, workspace):
    repo, actor, project, _ = workspace
    parent = begin(system, workspace, ClarificationGateway([clarify()]))
    barrier = Barrier(2)

    def send(index):
        barrier.wait(timeout=10)
        try:
            result = Clarifications(repo).reply(
                actor,
                project["id"],
                parent["id"],
                f"race-answer-{index}",
                {
                    "message_text": "Sim",
                    "base_project_revision": project["current_revision_id"],
                    "policy_version": "2026-10-v1",
                },
                GenerationService(repo, system["catalog"]),
            )
            return result["state"]
        except RuleError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, range(2)))
    assert results.count("queued") == 1
    assert any(result in {"CLARIFICATION_ANSWERED", "OPERATION_CONFLICT"} for result in results)
    with repo.sessions() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.payload["reply_to"].as_string() == parent["id"])
            )
            == 1
        )


def test_parent_is_checked_again_inside_the_admission_transaction(system, workspace, monkeypatch):
    repo, actor, project, _ = workspace
    parent = begin(system, workspace, ClarificationGateway([clarify()]))
    original_start = GenerationService.start

    def changed_before_admission(service, *args, **kwargs):
        with repo.sessions.begin() as db:
            operation = db.get(Operation, parent["id"])
            operation.result = {**operation.result, "kind": "implementation"}
        return original_start(service, *args, **kwargs)

    monkeypatch.setattr(GenerationService, "start", changed_before_admission)
    wallet = repo.wallet(actor, True)
    with client_for(system, ClarificationGateway([])) as client:
        response = reply(client, login(client, system), project, parent)
        assert response.status_code == 404, response.text
    assert repo.wallet(actor, True) == wallet


def test_reply_enforces_the_administrative_message_limit_before_reserving(system, workspace):
    repo, actor, project, _ = workspace
    parent = begin(system, workspace, ClarificationGateway([clarify()]))
    integrations = Integrations(repo)
    integrations.save(
        system["admin"],
        "jev",
        "short-reply-limit",
        integrations.get("jev")["version"],
        {"settings": {"max_message_chars": 100}, "reason": "Limite sintético de tamanho da resposta"},
    )
    wallet = repo.wallet(actor, True)
    with client_for(system, ClarificationGateway([])) as client:
        headers = login(client, system)
        response = reply(client, headers, project, parent, text="x" * 101)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "DRAFT_LIMIT_EXCEEDED"
        assert repo.wallet(actor, True) == wallet
        accepted = reply(client, headers, project, parent, key="within-reply-limit", text="x" * 100)
        assert accepted.status_code == 202, accepted.text


def test_reply_context_limit_is_enforced_without_dropping_the_original_request(system, workspace):
    repo, actor, project, _ = workspace
    original = "ORIGEM " + "a" * 11993
    question = "PERGUNTA " + "b" * 3991
    parent = begin(system, workspace, ClarificationGateway([clarify(question)]), brief=original)
    gateway = ClarificationGateway([clarify(question), clarify(question)])
    with client_for(system, gateway) as client:
        headers = login(client, system)
        for index in range(2):
            response = reply(client, headers, project, parent, key=f"long-answer-{index}", text="c" * 12000)
            assert response.status_code == 202, response.text
            assert Worker(repo, gateway, system["catalog"]).tick(response.json()["id"])
            parent = repo.operation(actor, response.json()["id"])
        wallet = repo.wallet(actor, True)
        rejected = reply(client, headers, project, parent, key="long-answer-rejected", text="c" * 12000)
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["error"]["code"] == "CONTEXT_LIMIT_EXCEEDED"
        assert repo.wallet(actor, True) == wallet
    assert len(gateway.contexts) == 2
    assert gateway.contexts[-1]["history"][0]["request"] == original


@pytest.mark.parametrize("guard", ["unauthenticated", "csrf", "origin"])
def test_reply_keeps_existing_session_csrf_and_origin_protection(system, workspace, guard):
    repo, actor, project, _ = workspace
    parent = begin(system, workspace, ClarificationGateway([clarify()]))
    wallet = repo.wallet(actor, True)
    with client_for(system, ClarificationGateway([])) as client:
        headers = login(client, system)
        if guard == "unauthenticated":
            client.cookies.clear()
        elif guard == "csrf":
            headers = {**headers, "X-CSRF-Token": "invalid-csrf"}
        else:
            headers = {**headers, "Origin": "https://outside.invalid"}
        response = reply(client, headers, project, parent)
        assert response.status_code == (401 if guard == "unauthenticated" else 403), response.text
    assert repo.wallet(actor, True) == wallet
