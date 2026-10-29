import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from test_guided_build import save_request, send_request

from viracase.application.conversation import INPUT_BOUND, MAX_GENERATION_CALLS
from viracase.application.conversation_turn import turn_prompt
from viracase.application.generation import GenerationService
from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError, cost, money
from viracase.infrastructure.guided_build import GuidedBuild
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.learning import LearningLibrary
from viracase.infrastructure.models import Attempt, Operation, Reservation
from viracase.infrastructure.publication_migration_context import published_migration_context
from viracase.infrastructure.worker import Worker
from viracase.interfaces.feature_schemas import BuildPolicy


def large_project(system):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor, "context-project", {"name": "Padaria", "objective": "Vitrine e pedidos da padaria"}
    )
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    files["README.md"] += "\n" + "a" * 49000
    repo.save_revision(
        actor,
        project["id"],
        "source-before-generation",
        {"base_revision_id": project["current_revision_id"], "files": files},
        project["version"],
    )
    return repo, actor, repo.project(actor, project["id"]), files


class GrowingGateway:
    def __init__(self, repo, actor, project):
        self.repo, self.actor, self.project = repo, actor, project
        self.prompts = []
        self.original = repo.source_files(actor, project["id"], project["current_revision_id"])

    def generate(self, policy, prompt):
        self.prompts.append(prompt)
        assert (
            self.repo.project(self.actor, self.project["id"])["current_revision_id"]
            == self.project["current_revision_id"]
        )
        current = json.loads(prompt[prompt.index('{"stack"') :])
        assert "Vitrine com preços, carrinho e login para finalizar a compra" in current["request"]
        if len(self.prompts) == 1:
            output = {
                "files": [
                    {
                        "path": "public/catalog.js",
                        "content": "const catalog = "
                        + json.dumps([{"id": n, "name": f"Produto {n}", "price": n + 1} for n in range(220)])
                        + ";\n",
                    }
                ],
                "edits": [],
                "complete": False,
            }
        else:
            assert "public/catalog.js" in current["files"]
            assert current["files"]["README.md"] == self.original["README.md"]
            output = {
                "files": [],
                "edits": [
                    {
                        "path": "public/index.html",
                        "old": "</body>",
                        "new": '<section id="catalog"></section><script src="/catalog.js"></script></body>',
                    }
                ],
                "complete": True,
            }
        if policy["generation_contract_version"] == 4:
            output = {"action": "implement", "message": "Integrei a vitrine.", **output}
        return ModelResult(
            json.dumps(output), {"input": 1000, "cached": 0, "output": 3000}, f"batch-{len(self.prompts)}"
        )


@pytest.mark.parametrize("contract", [3, 4])
def test_accumulated_changes_can_cross_initial_context_limit(system, contract):
    repo, actor, project, original = large_project(system)
    gateway = GrowingGateway(repo, actor, project)
    operation = GenerationService(repo, system["catalog"]).start(
        actor,
        project["id"],
        "growth",
        {
            "brief": "Vitrine com preços, carrinho e login para finalizar a compra",
            "base_revision_id": project["current_revision_id"],
            "policy_version": "2026-10-v1",
            "review_required": True,
        },
        contract_version=contract,
    )
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "succeeded", result
    assert len(gateway.prompts) == 2
    assert len(gateway.prompts[0].encode()) <= 60000 < len(gateway.prompts[1].encode())
    assert result["result_ref"]["candidate"]
    candidate = repo.source_files(actor, project["id"], result["result_ref"]["revision_id"])
    assert candidate["README.md"] == original["README.md"]
    assert "public/catalog.js" in candidate
    assert 'id="catalog"' in candidate["public/index.html"]
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    with repo.sessions() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
        assert attempt.usage["input"] == 2000
        assert attempt.usage["output"] == 6000
        assert attempt.actual_brl == cost(system["policy"], {"input": 2000, "cached": 0, "output": 6000})
    assert not Worker(repo, gateway, system["catalog"]).tick(operation["id"])


@pytest.mark.parametrize("contract", [3, 4])
def test_confirmed_input_exhaustion_blocks_another_dispatch_without_partial_save(system, contract):
    repo, actor, project, original = large_project(system)

    class ExhaustingGateway(GrowingGateway):
        def generate(self, policy, prompt):
            result = super().generate(policy, prompt)
            return ModelResult(
                result.text,
                {"input": policy["generation_input_budget"] - 50000, "cached": 0, "output": 3000},
                result.external_id,
            )

    gateway = ExhaustingGateway(repo, actor, project)
    operation = GenerationService(repo, system["catalog"]).start(
        actor,
        project["id"],
        "exhaustion",
        {
            "brief": "Vitrine com preços, carrinho e login para finalizar a compra",
            "base_revision_id": project["current_revision_id"],
            "policy_version": "2026-10-v1",
            "review_required": True,
        },
        contract_version=contract,
    )
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "failed"
    assert result["error_code"] == "CONTEXT_LIMIT_EXCEEDED"
    assert result["billing_status"] == "settled"
    assert len(gateway.prompts) == 1
    assert "revision_id" not in result["result_ref"]
    assert (
        repo.source_files(actor, project["id"], repo.project(actor, project["id"])["current_revision_id"])
        == original
    )
    with repo.sessions() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
        assert attempt.actual_brl == cost(
            system["policy"],
            {"input": INPUT_BOUND * MAX_GENERATION_CALLS - 50000, "cached": 0, "output": 3000},
        )
        assert len(attempt.usage["calls"]) == 1
    assert not worker.tick(operation["id"])


@pytest.mark.parametrize("contract", [3, 4])
def test_context_envelope_preserves_reservation_ceiling_and_idempotent_intention(system, contract):
    repo, actor, project, _ = large_project(system)
    payload = {
        "brief": "Ampliar a vitrine",
        "base_revision_id": project["current_revision_id"],
        "policy_version": "2026-10-v1",
        "review_required": True,
    }
    generation = GenerationService(repo, system["catalog"])
    operation = generation.start(actor, project["id"], "reservation", payload, contract_version=contract)
    assert (
        generation.start(actor, project["id"], "reservation", payload, contract_version=contract)["id"]
        == operation["id"]
    )
    with repo.sessions() as db:
        saved = db.get(Operation, operation["id"])
        assert saved.policy["generation_context_version"] == 2
        assert saved.policy["max_batches"] == MAX_GENERATION_CALLS
        assert saved.policy["generation_input_budget"] == INPUT_BOUND * MAX_GENERATION_CALLS
        expected = money(
            cost(
                system["policy"],
                {
                    "input": INPUT_BOUND * MAX_GENERATION_CALLS,
                    "cached": 0,
                    "output": system["policy"]["max_output_tokens"] * MAX_GENERATION_CALLS,
                },
            )
            * Decimal("1.10")
        )
        assert (
            db.scalar(select(func.sum(Reservation.amount)).where(Reservation.operation_id == saved.id))
            == expected
        )
        assert (
            db.scalar(
                select(func.count()).select_from(Operation).where(Operation.project_id == project["id"])
            )
            == 1
        )


@pytest.mark.parametrize(
    "first_limit,later_limit,expected,calls", [(60000, 252288, "failed", 1), (252288, 10000, "succeeded", 2)]
)
def test_reviewed_generation_keeps_its_frozen_context_policy(
    system, first_limit, later_limit, expected, calls
):
    repo, actor, project, _ = large_project(system)
    integrations = Integrations(repo)
    policy = BuildPolicy(max_generation_context_bytes=first_limit).model_dump()
    integrations.save(
        system["admin"],
        "jev",
        "initial-generation-cap",
        0,
        {"settings": policy, "reason": "Limite explícito para a construção revisada"},
    )
    flow = GuidedBuild(repo, LearningLibrary(system["config"].content_root))
    data = save_request(flow, actor, project, False)
    data["message_text"] = "Vitrine com preços, carrinho e login para finalizar a compra"
    flow.save(actor, project["id"], "set-reviewed-request", 1, data)
    operation = send_request(system, flow, actor, project)
    with repo.sessions() as db:
        saved = db.get(Operation, operation["id"])
        assert saved.payload["generation_context_limit_bytes"] == first_limit
    integrations.save(
        system["admin"],
        "jev",
        "later-generation-cap",
        1,
        {
            "settings": {**policy, "max_generation_context_bytes": later_limit},
            "reason": "Alteração posterior não muda a solicitação já reservada",
        },
    )
    gateway = GrowingGateway(repo, actor, project)
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    result = repo.operation(actor, operation["id"])
    assert result["state"] == expected, result
    assert len(gateway.prompts) == calls
    if expected == "failed":
        assert result["error_code"] == "CONTEXT_LIMIT_EXCEEDED"
        assert "revision_id" not in result["result_ref"]


@pytest.mark.parametrize("first_result", ["invalid", "truncated"])
def test_repair_instructions_do_not_reject_an_already_accepted_request_at_its_cap(system, first_result):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor, "bounded-request", {"name": "Padaria", "objective": "Melhorar a vitrine"}
    )
    brief = "Melhorar a vitrine com preços e compra autenticada. " * 200
    with repo.sessions() as db:
        current = repo.owned_project(db, actor, project["id"])
        files = repo.sources.read(
            repo.owned_revision(db, actor, project["id"], project["current_revision_id"]), current.stack
        )
        migrations = published_migration_context(repo, db, current, files)
    prompt = turn_prompt(
        project, brief, {}, max_batches=MAX_GENERATION_CALLS, published_migrations=migrations
    )
    request_cap = len(prompt.encode()) - len(json.dumps(migrations, ensure_ascii=False).encode()) + 2
    assert 10000 < request_cap < 60000
    with pytest.raises(RuleError) as caught:
        turn_prompt(
            project, brief, {}, max_batches=MAX_GENERATION_CALLS,
            published_migrations=migrations, request_limit_bytes=request_cap - 1,
        )
    assert caught.value.details["reason_code"] == "REQUEST_CONTEXT_LIMIT"
    assert caught.value.details["request_context_bytes"] == request_cap

    class RepairGateway:
        def __init__(self):
            self.prompts = []

        def generate(self, policy, prompt):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return ModelResult(
                    '{"files":',
                    {"input": 1000, "cached": 0, "output": 100},
                    "discarded",
                    "GENERATION_TRUNCATED" if first_result == "truncated" else None,
                )
            document = json.loads(prompt[prompt.index('{"stack"') :])
            assert document["request"] == brief
            assert "descartado" in prompt
            output = {
                "action": "implement",
                "message": "Vitrine ajustada.",
                "files": [],
                "edits": [
                    {
                        "path": "public/index.html",
                        "old": "</body>",
                        "new": '<section id="catalog"></section></body>',
                    }
                ],
                "complete": True,
            }
            return ModelResult(json.dumps(output), {"input": 1000, "cached": 0, "output": 100}, "complete")

    operation = GenerationService(repo, system["catalog"]).start(
        actor,
        project["id"],
        "bounded-repair",
        {
            "brief": brief,
            "base_revision_id": project["current_revision_id"],
            "policy_version": "2026-10-v1",
            "context_limit_bytes": request_cap,
            "review_required": True,
        },
        contract_version=4,
    )
    gateway = RepairGateway()
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "succeeded", result
    assert len(gateway.prompts) == 2
    assert result["result_ref"]["candidate"]


def test_request_history_limit_is_checked_before_reservation(system):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor, "history-context", {"name": "Padaria", "objective": "Melhorar a vitrine"}
    )
    with repo.sessions.begin() as db:
        for n in range(4):
            db.add(
                Operation(
                    account_id=actor,
                    project_id=project["id"],
                    type="generation",
                    state="succeeded",
                    payload={"brief": str(n) * 3000, "base_revision_id": project["current_revision_id"]},
                    result={"kind": "clarification", "message": "a" * 2000},
                )
            )
    before = repo.wallet(actor, True)
    with pytest.raises(RuleError) as caught:
        GenerationService(repo, system["catalog"]).start(
            actor,
            project["id"],
            "history-overflow",
            {
                "brief": "Ajustar a vitrine",
                "base_revision_id": project["current_revision_id"],
                "policy_version": "2026-10-v1",
                "context_limit_bytes": 10000,
            },
            contract_version=4,
        )
    assert caught.value.code == "CONTEXT_LIMIT_EXCEEDED"
    assert caught.value.details["reason_code"] == "REQUEST_CONTEXT_LIMIT"
    assert repo.wallet(actor, True) == before
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reservation)) == 0
