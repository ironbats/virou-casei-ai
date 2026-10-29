import json

import pytest
from sqlalchemy import func, select

from viracase.application.conversation import prompt_for
from viracase.application.conversation_turn import turn_prompt
from viracase.application.generation import GenerationService
from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError, cost
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Attempt, Operation, Reservation, Revision
from viracase.infrastructure.providers import ProviderUncertain
from viracase.infrastructure.worker import Worker


@pytest.fixture
def growing_project(system):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor,
        "context-safety-project",
        {"name": "Padaria", "objective": "Catálogo de produtos, carrinho e compra autenticada"},
    )
    original = repo.source_files(actor, project["id"], project["current_revision_id"])
    original["public/catalog-data.js"] = "const catalogDescription = " + json.dumps("x" * 40000) + ";\n"
    repo.save_revision(
        actor,
        project["id"],
        "context-safety-base",
        {"base_revision_id": project["current_revision_id"], "files": original},
        project["version"],
    )
    project = repo.project(actor, project["id"])
    return repo, actor, project, original


def begin(system, growing_project, contract=4, key="context-safety-generation", **overrides):
    repo, actor, project, _ = growing_project
    payload = {
        "brief": "Melhorar a home com carrossel de produtos e preços; adicionar carrinho e exigir cadastro e login para comprar.",
        "base_revision_id": project["current_revision_id"],
        "policy_version": "2026-10-v1",
        "review_required": False,
        **overrides,
    }
    operation = GenerationService(repo, system["catalog"]).start(
        actor, project["id"], key, payload, contract_version=contract
    )
    return operation, payload


class GrowingGateway:
    def __init__(self, after_second=None, outcome="complete"):
        self.after_second = after_second
        self.outcome = outcome
        self.prompts = []
        self.first_content = "const cartDescription = " + json.dumps("y" * 22000) + ";\n"

    def generate(self, policy, prompt):
        self.prompts.append(prompt)
        current = json.loads(prompt[prompt.index('{"stack"'):])
        number = len(self.prompts)
        if number == 1:
            body = {
                "files": [{"path": "public/cart.js", "content": self.first_content}],
                "edits": [],
                "complete": False,
            }
        else:
            assert current["files"]["public/cart.js"] == self.first_content
            assert len(prompt.encode()) > 60000
            if self.after_second:
                self.after_second()
            if self.outcome == "uncertain":
                raise ProviderUncertain("synthetic-unknown-provider-result")
            body = {
                "files": [],
                "edits": [
                    {
                        "path": "public/index.html",
                        "old": "</body>",
                        "new": "<section id=\"catalog\">Produtos e carrinho</section></body>",
                    }
                ],
                "complete": True,
            }
        if policy["generation_contract_version"] == 4:
            body.update(action="implement", message="Alterações preparadas para revisão.")
        error = "GENERATION_TRUNCATED" if number > 1 and self.outcome == "truncated" else None
        return ModelResult(
            json.dumps(body),
            {"input": 100, "cached": 20, "output": 100},
            "context-safety-" + str(number),
            error,
        )


def assert_base_preserved(growing_project):
    repo, actor, project, original = growing_project
    current = repo.project(actor, project["id"])
    assert current["current_revision_id"] == project["current_revision_id"]
    assert repo.source_files(actor, project["id"], current["current_revision_id"]) == original
    with repo.sessions() as db:
        assert db.scalar(
            select(func.count()).select_from(Revision).where(Revision.project_id == project["id"])
        ) == 2


@pytest.mark.parametrize("contract", [3, 4])
def test_durable_legacy_context_policy_is_not_silently_expanded(system, growing_project, contract):
    repo, actor, _, _ = growing_project
    operation, _ = begin(system, growing_project, contract)
    with repo.sessions.begin() as db:
        row = db.get(Operation, operation["id"])
        row.policy = {
            key: value
            for key, value in row.policy.items()
            if key not in {"generation_context_version", "generation_input_budget"}
        }
    gateway = GrowingGateway()
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "failed"
    assert final["error_code"] == "CONTEXT_LIMIT_EXCEEDED"
    assert final["billing_status"] == "settled"
    assert len(gateway.prompts) == 1
    assert not worker.tick(operation["id"])
    assert_base_preserved(growing_project)


def test_cancellation_during_expanded_continuation_settles_confirmed_usage_only(system, growing_project):
    repo, actor, _, _ = growing_project
    operation, _ = begin(system, growing_project)
    gateway = GrowingGateway(after_second=lambda: repo.cancel(actor, operation["id"], "cancel-second"))
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "cancelled"
    assert final["billing_status"] == "settled"
    assert len(gateway.prompts) == 2
    assert not worker.tick(operation["id"])
    assert_base_preserved(growing_project)
    with repo.sessions() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
        assert attempt.state == "confirmed"
        assert attempt.usage["input"] == 200
        assert attempt.usage["cached"] == 40
        assert attempt.actual_brl == cost(system["policy"], {"input": 200, "cached": 40, "output": 200})
        assert all(
            item.state == "settled"
            for item in db.scalars(select(Reservation).where(Reservation.operation_id == operation["id"]))
        )


def test_unknown_continuation_keeps_reservation_and_never_replays_confirmed_chunk(system, growing_project):
    repo, actor, _, _ = growing_project
    operation, _ = begin(system, growing_project)
    gateway = GrowingGateway(outcome="uncertain")
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "reconciling"
    assert final["billing_status"] == "reconciliation_pending"
    assert final["error_code"] == "PROVIDER_RESULT_UNKNOWN"
    assert len(gateway.prompts) == 2
    assert not worker.tick(operation["id"])
    assert repo.wallet(actor)["pending_reconciliation"]
    assert_base_preserved(growing_project)
    with repo.sessions() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
        assert attempt.state == "uncertain"
        assert attempt.usage["input"] == 100
        assert len(attempt.usage["calls"]) == 1
        assert all(
            item.state == "reserved"
            for item in db.scalars(select(Reservation).where(Reservation.operation_id == operation["id"]))
        )


def test_manual_revision_during_expanded_continuation_remains_canonical(system, growing_project):
    repo, actor, project, original = growing_project
    operation, _ = begin(system, growing_project)
    manual = {**original, "public/index.html": original["public/index.html"] + "\n<p>Edição manual</p>"}

    def save_manual():
        repo.save_revision(
            actor,
            project["id"],
            "manual-during-continuation",
            {"base_revision_id": project["current_revision_id"], "files": manual},
            project["version"],
        )

    gateway = GrowingGateway(after_second=save_manual)
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "succeeded"
    assert len(gateway.prompts) == 2
    assert final["result_ref"]["candidate"] is True
    assert final["result_ref"]["apply_conflict"] is True
    assert "applied_revision_id" not in final["result_ref"]
    current = repo.project(actor, project["id"])
    assert repo.source_files(actor, project["id"], current["current_revision_id"]) == manual
    candidate = repo.source_files(actor, project["id"], final["result_ref"]["revision_id"])
    assert candidate["public/cart.js"] == gateway.first_content
    assert "Produtos e carrinho" in candidate["public/index.html"]
    assert "Edição manual" not in candidate["public/index.html"]
    with pytest.raises(RuleError) as denied:
        repo.operation(system["users"][1], operation["id"])
    assert denied.value.code == "NOT_FOUND"


def test_truncated_continuation_never_commits_valid_looking_partial_response(system, growing_project):
    repo, actor, _, _ = growing_project
    Integrations(repo).save(
        system["admin"], "jev", "bounded-truncated-continuation", 0,
        {"settings": {"max_truncation_retries": 1},
         "reason": "Limitar a recuperação da continuação para este cenário"},
    )
    operation, _ = begin(system, growing_project)
    gateway = GrowingGateway(outcome="truncated")
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "failed"
    assert final["error_code"] == "GENERATION_TRUNCATED"
    assert len(gateway.prompts) == 3
    assert final["billing_status"] == "settled"
    assert not worker.tick(operation["id"])
    assert_base_preserved(growing_project)


def test_explicit_small_context_cap_rejects_before_any_financial_hold(system, growing_project):
    repo, actor, project, _ = growing_project
    before = repo.wallet(actor, True)
    with pytest.raises(RuleError) as caught:
        begin(system, growing_project, context_limit_bytes=10000, brief="x" * 11000)
    assert caught.value.code == "CONTEXT_LIMIT_EXCEEDED"
    assert repo.wallet(actor, True) == before
    with repo.sessions() as db:
        assert db.scalar(
            select(func.count()).select_from(Operation).where(Operation.project_id == project["id"])
        ) == 0
        assert db.scalar(select(func.count()).select_from(Reservation)) == 0


def test_followup_request_can_evolve_a_successfully_expanded_project(system, growing_project):
    repo, actor, project, _ = growing_project
    operation, first_payload = begin(system, growing_project)
    initial_gateway = GrowingGateway()
    assert Worker(repo, initial_gateway, system["catalog"]).tick(operation["id"])
    initial = repo.operation(actor, operation["id"])
    assert initial["state"] == "succeeded"
    current = repo.project(actor, project["id"])
    current_files = repo.source_files(actor, project["id"], current["current_revision_id"])
    assert sum(len(value.encode()) for value in current_files.values()) > 60000
    followup_request = "Agora acrescente o botão de cadastro de cliente na home."
    followup, _ = begin(
        system,
        (repo, actor, current, current_files),
        key="context-safety-followup",
        brief=followup_request,
    )
    prompts = []

    class FollowupGateway:
        def generate(self, policy, prompt):
            prompts.append(prompt)
            data = json.loads(prompt[prompt.index('{"stack"'):])
            assert len(prompt.encode()) > 60000
            assert data["files"] == current_files
            assert data["request"] == followup_request
            assert data["history"][-1]["request"] == first_payload["brief"]
            return ModelResult(
                json.dumps(
                    {
                        "action": "implement",
                        "message": "Adicionei o acesso ao cadastro de cliente.",
                        "files": [],
                        "edits": [
                            {
                                "path": "public/index.html",
                                "old": "</body>",
                                "new": '<a href="/cadastro.html">Criar conta</a></body>',
                            }
                        ],
                        "complete": True,
                    }
                ),
                {"input": 200, "cached": 0, "output": 100},
                "context-safety-followup-result",
            )

    assert Worker(repo, FollowupGateway(), system["catalog"]).tick(followup["id"])
    final = repo.operation(actor, followup["id"])
    assert final["state"] == "succeeded"
    assert len(prompts) == 1
    latest = repo.project(actor, project["id"])
    latest_files = repo.source_files(actor, project["id"], latest["current_revision_id"])
    assert "Criar conta" in latest_files["public/index.html"]
    assert latest_files["public/cart.js"] == initial_gateway.first_content
    for path, content in current_files.items():
        if path != "public/index.html":
            assert latest_files[path] == content


def test_configured_full_project_cap_rejects_before_creating_operation(system, growing_project):
    repo, actor, project, _ = growing_project
    before = repo.wallet(actor, True)
    with pytest.raises(RuleError) as caught:
        begin(system, growing_project, generation_context_limit_bytes=10000)
    assert caught.value.code == "CONTEXT_LIMIT_EXCEEDED"
    assert repo.wallet(actor, True) == before
    with repo.sessions() as db:
        assert db.scalar(
            select(func.count()).select_from(Operation).where(Operation.project_id == project["id"])
        ) == 0
        assert db.scalar(select(func.count()).select_from(Reservation)) == 0


@pytest.mark.parametrize("contract", [3, 4])
def test_context_boundary_counts_utf8_and_json_escapes_without_losing_content(contract):
    project = {"stack": "hono-typescript-d1", "objective": "Padaria: pão e café"}
    request = 'Mantenha o rótulo "Ação 😀" e os preços.\nInclua carrinho e autenticação.'
    files = {"public/catalog.js": 'const label = "Ação 😀";\nconst separator = "\\\\";\n' * 1800}
    make = turn_prompt if contract == 4 else prompt_for
    options = {"history": [{"request": "Quero pão.", "response": "Café também?"}]} if contract == 4 else {}
    prompt = make(project, request, files, input_bound=256384, request_limit_bytes=60000, **options)
    data = json.loads(prompt[prompt.index('{"stack"'):])
    assert data["files"] == files
    assert data["request"] == request
    assert data["objective"] == project["objective"]
    if contract == 4:
        assert data["history"] == options["history"]
    prompt_bytes = len(prompt.encode())
    assert prompt_bytes > len(prompt) and prompt_bytes > 60000
    assert make(
        project,
        request,
        files,
        input_bound=prompt_bytes + 4096,
        request_limit_bytes=60000,
        **options,
    ) == prompt
    with pytest.raises(RuleError) as caught:
        make(
            project,
            request,
            files,
            input_bound=prompt_bytes + 4095,
            request_limit_bytes=60000,
            **options,
        )
    assert caught.value.code == "CONTEXT_LIMIT_EXCEEDED"
    assert caught.value.details["context_bytes"] == prompt_bytes
    assert request not in json.dumps(caught.value.details, ensure_ascii=False)
