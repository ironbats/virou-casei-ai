import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from test_guided_build import save_request, send_request

from viracase.application.generation import GenerationService
from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError, cost, money
from viracase.infrastructure.guided_build import GuidedBuild
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.learning import LearningLibrary
from viracase.infrastructure.models import Attempt, Operation, Reservation, Revision
from viracase.infrastructure.providers import ProviderUncertain
from viracase.infrastructure.worker import Worker


@pytest.fixture
def call_workspace(system):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor,
        "call-safety-project",
        {"name": "Padaria", "objective": "Vitrine, carrinho e compra autenticada"},
    )
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    return repo, actor, project, files


def set_call_limit(system, count, key):
    integrations = Integrations(system["repo"])
    return integrations.save(
        system["admin"],
        "jev",
        key,
        integrations.get("jev")["version"],
        {
            "settings": {"max_generation_calls": count},
            "reason": "Limite sintético para validar a política congelada da construção.",
        },
    )


def begin_call_operation(system, call_workspace, key="call-safety-generation"):
    repo, actor, project, _ = call_workspace
    payload = {
        "brief": "Melhore a vitrine, o carrinho e a compra autenticada.",
        "base_revision_id": project["current_revision_id"],
        "policy_version": "2026-10-v1",
        "review_required": False,
    }
    operation = GenerationService(repo, system["catalog"]).start(
        actor, project["id"], key, payload, contract_version=4
    )
    return operation, payload


class ContinuedGateway:
    def __init__(self, complete_at=7, intervention=None, outcome=None):
        self.complete_at = complete_at
        self.intervention = intervention
        self.outcome = outcome
        self.calls = []

    def generate(self, policy, prompt):
        context = json.loads(prompt[prompt.index('{"stack"'):])
        self.calls.append({"context": context, "policy": dict(policy)})
        number = len(self.calls)
        assert context["batch"] == number
        if number > 1:
            assert f'id="call-stage-{number - 1}"' in context["files"]["public/index.html"]
        if self.intervention:
            self.intervention(number)
        if self.outcome == "uncertain" and number == 6:
            raise ProviderUncertain("synthetic-sixth-call-uncertain")
        usage = {"input": 120, "cached": 20, "output": 70}
        if self.outcome == "cost" and number == 6:
            usage["output"] = 1000000
        response = {
            "files": [],
            "edits": [
                {
                    "path": "public/index.html",
                    "old": "</body>",
                    "new": f'<section id="call-stage-{number}">Etapa {number}</section></body>',
                }
            ],
            "complete": number >= self.complete_at,
        }
        if policy["generation_contract_version"] == 4:
            response.update(action="implement", message=f"Etapa {number} preparada.")
        return ModelResult(json.dumps(response), usage, f"call-safety-{number}")


def assert_base_preserved(call_workspace):
    repo, actor, project, original = call_workspace
    current = repo.project(actor, project["id"])
    assert current["current_revision_id"] == project["current_revision_id"]
    assert repo.source_files(actor, project["id"], current["current_revision_id"]) == original
    with repo.sessions() as db:
        assert db.scalar(
            select(func.count()).select_from(Revision).where(Revision.project_id == project["id"])
        ) == 1


@pytest.mark.parametrize("contract,explicit_count", [(3, True), (4, True), (4, False)])
def test_legacy_queued_operations_keep_four_calls_and_original_reservation(
    system, call_workspace, contract, explicit_count
):
    repo, actor, project, _ = call_workspace
    policy = {
        **system["policy"],
        "generation_contract_version": contract,
        "reserved_input_bound": 64096,
    }
    if explicit_count:
        policy.update(generation_context_version=2, generation_input_budget=256384, max_batches=4)
    ceiling = money(
        cost(policy, {"input": 256384, "cached": 0, "output": policy["max_output_tokens"] * 4})
        * Decimal("1.10")
    )
    operation = repo.create_operation(
        actor,
        project["id"],
        "generation",
        "queued-before-upgrade",
        {
            "brief": "Complete a vitrine e o carrinho em etapas.",
            "base_revision_id": project["current_revision_id"],
            "policy_version": "2026-10-v1",
            "review_required": True,
        },
        policy,
        ceiling,
    )
    set_call_limit(system, 10, "new-administrative-default")
    gateway = ContinuedGateway(complete_at=5)
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "failed"
    assert final["error_code"] == "GENERATION_LIMIT"
    assert final["billing_status"] == "settled"
    assert len(gateway.calls) == 4
    assert [call["context"]["batches_remaining"] for call in gateway.calls] == [4, 3, 2, 1]
    with repo.sessions() as db:
        stored = db.get(Operation, operation["id"])
        assert stored.policy == policy
        assert db.scalar(
            select(func.sum(Reservation.amount)).where(Reservation.operation_id == stored.id)
        ) == ceiling
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == stored.id))
        assert attempt.actual_brl == cost(policy, {"input": 480, "cached": 80, "output": 280})
    assert_base_preserved(call_workspace)


@pytest.mark.parametrize("initial,later,expected,calls", [(10, 1, "succeeded", 6), (1, 10, "failed", 1)])
def test_reviewed_operation_keeps_call_limit_when_admin_changes_it_during_execution(
    system, call_workspace, initial, later, expected, calls
):
    repo, actor, project, _ = call_workspace
    set_call_limit(system, initial, "initial-administrative-limit")
    guided = GuidedBuild(repo, LearningLibrary(system["config"].content_root))
    save_request(guided, actor, project, attachments=False)
    operation = send_request(system, guided, actor, project, key="frozen-call-limit")

    def change_policy(number):
        if number == 1:
            set_call_limit(system, later, "changed-administrative-limit")

    gateway = ContinuedGateway(complete_at=6, intervention=change_policy)
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == expected
    assert len(gateway.calls) == calls
    assert [call["context"]["batches_remaining"] for call in gateway.calls] == list(
        range(initial, initial - calls, -1)
    )
    assert all(call["policy"]["max_batches"] == initial for call in gateway.calls)
    assert Integrations(repo).build_policy()["max_generation_calls"] == later
    with repo.sessions() as db:
        stored = db.get(Operation, operation["id"])
        assert stored.policy["max_batches"] == initial
        assert stored.policy["generation_input_budget"] == 64096 * initial
    if expected == "failed":
        assert final["error_code"] == "GENERATION_LIMIT"
        assert_base_preserved(call_workspace)
    else:
        assert final["result_ref"]["candidate"] is True
        assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
        candidate = repo.source_files(actor, project["id"], final["result_ref"]["revision_id"])
        assert 'id="call-stage-6"' in candidate["public/index.html"]


def test_cancellation_on_sixth_call_settles_only_confirmed_usage_and_preserves_code(
    system, call_workspace
):
    repo, actor, _, _ = call_workspace
    operation, _ = begin_call_operation(system, call_workspace)

    def cancel(number):
        if number == 6:
            repo.cancel(actor, operation["id"], "cancel-on-sixth-call")

    gateway = ContinuedGateway(intervention=cancel)
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "cancelled"
    assert final["billing_status"] == "settled"
    assert len(gateway.calls) == 6
    with repo.sessions() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
        assert attempt.state == "confirmed"
        assert attempt.usage["input"] == 720
        assert len(attempt.usage["calls"]) == 6
        assert attempt.actual_brl == cost(system["policy"], {"input": 720, "cached": 120, "output": 420})
        assert all(
            row.state == "settled"
            for row in db.scalars(select(Reservation).where(Reservation.operation_id == operation["id"]))
        )
    assert_base_preserved(call_workspace)


def test_uncertain_sixth_call_keeps_reservation_and_is_never_replayed(system, call_workspace):
    repo, actor, _, _ = call_workspace
    operation, _ = begin_call_operation(system, call_workspace)
    gateway = ContinuedGateway(outcome="uncertain")
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "reconciling"
    assert final["billing_status"] == "reconciliation_pending"
    assert final["error_code"] == "PROVIDER_RESULT_UNKNOWN"
    assert len(gateway.calls) == 6
    assert repo.wallet(actor)["pending_reconciliation"]
    with repo.sessions() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
        assert attempt.state == "uncertain"
        assert attempt.usage["input"] == 600
        assert len(attempt.usage["calls"]) == 5
        assert all(
            row.state == "reserved"
            for row in db.scalars(select(Reservation).where(Reservation.operation_id == operation["id"]))
        )
    assert_base_preserved(call_workspace)


def test_manual_revision_during_sixth_call_remains_canonical_and_proposal_is_isolated(
    system, call_workspace
):
    repo, actor, project, original = call_workspace
    operation, _ = begin_call_operation(system, call_workspace)
    manual = {**original, "public/index.html": original["public/index.html"] + "\n<p>Edição manual</p>"}

    def edit_manual(number):
        if number == 6:
            repo.save_revision(
                actor,
                project["id"],
                "manual-during-sixth-call",
                {"base_revision_id": project["current_revision_id"], "files": manual},
                project["version"],
            )

    gateway = ContinuedGateway(complete_at=6, intervention=edit_manual)
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "succeeded"
    assert len(gateway.calls) == 6
    assert final["result_ref"]["candidate"] is True
    assert final["result_ref"]["apply_conflict"] is True
    assert "applied_revision_id" not in final["result_ref"]
    current = repo.project(actor, project["id"])
    assert repo.source_files(actor, project["id"], current["current_revision_id"]) == manual
    candidate = repo.source_files(actor, project["id"], final["result_ref"]["revision_id"])
    assert 'id="call-stage-6"' in candidate["public/index.html"]
    assert "Edição manual" not in candidate["public/index.html"]
    with pytest.raises(RuleError) as denied:
        repo.operation(system["users"][1], operation["id"])
    assert denied.value.code == "NOT_FOUND"


def test_duplicate_reviewed_request_does_not_reserve_new_call_limit(system, call_workspace):
    repo, actor, project, _ = call_workspace
    set_call_limit(system, 4, "first-call-policy")
    guided = GuidedBuild(repo, LearningLibrary(system["config"].content_root))
    save_request(guided, actor, project, attachments=False)
    first = send_request(system, guided, actor, project, key="same-reviewed-intention")
    before = repo.wallet(actor, True)
    set_call_limit(system, 10, "second-call-policy")
    second = send_request(system, guided, actor, project, key="same-reviewed-intention")
    assert first["id"] == second["id"]
    assert repo.wallet(actor, True) == before
    with repo.sessions() as db:
        stored = db.get(Operation, first["id"])
        assert stored.policy["max_batches"] == 4
        assert stored.policy["generation_input_budget"] == 256384
        assert db.scalar(
            select(func.count()).select_from(Operation).where(Operation.project_id == project["id"])
        ) == 1
        assert db.scalar(
            select(func.count()).select_from(Reservation).where(Reservation.operation_id == first["id"])
        ) == 1


def test_cost_discrepancy_on_sixth_call_never_saves_partial_or_completed_response(
    system, call_workspace
):
    repo, actor, _, _ = call_workspace
    operation, _ = begin_call_operation(system, call_workspace)
    gateway = ContinuedGateway(complete_at=6, outcome="cost")
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "reconciling"
    assert final["billing_status"] == "reconciliation_pending"
    assert final["error_code"] == "COST_DISCREPANCY"
    assert len(gateway.calls) == 6
    with repo.sessions() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
        assert attempt.state == "confirmed"
        assert attempt.usage["output"] == 1000350
        assert len(attempt.usage["calls"]) == 6
        assert all(
            row.state == "reserved"
            for row in db.scalars(select(Reservation).where(Reservation.operation_id == operation["id"]))
        )
    assert_base_preserved(call_workspace)
