import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from test_workflow_diagnostics import diagnostic_records

from viracase.application.conversation import (
    INPUT_BOUND,
    INPUT_OVERHEAD,
    MAX_GENERATION_CONTEXT_BYTES,
    context_input_bound,
    context_input_budget,
)
from viracase.application.generation import GenerationService
from viracase.application.ports import ModelResult
from viracase.domain.rules import cost, money
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Attempt, Operation, Reservation, Revision, new_id
from viracase.infrastructure.worker import Worker
from viracase.interfaces.feature_schemas import BuildPolicy

__all__ = ["diagnostic_records"]


def start_generation(system, contract=4):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor, new_id(), {"name": "Padaria", "objective": "Vitrine, carrinho e compra autenticada"}
    )
    operation = GenerationService(repo, system["catalog"]).start(
        actor,
        project["id"],
        new_id(),
        {
            "brief": "Implemente a vitrine, o carrinho e a compra autenticada integralmente.",
            "base_revision_id": project["current_revision_id"],
            "policy_version": "2026-10-v1",
            "review_required": True,
        },
        contract_version=contract,
    )
    return repo, actor, project, operation


class ContinuingGateway:
    def __init__(self, repo, actor, project, complete_after=10, invalid_calls=(), truncated_calls=()):
        self.repo, self.actor, self.project = repo, actor, project
        self.complete_after = complete_after
        self.prompts, self.policies = [], []
        self.invalid_calls, self.truncated_calls = invalid_calls, truncated_calls
        self.accepted = []

    def generate(self, policy, prompt):
        self.prompts.append(prompt)
        self.policies.append(dict(policy))
        assert (
            self.repo.project(self.actor, self.project["id"])["current_revision_id"]
            == self.project["current_revision_id"]
        )
        number = len(self.prompts)
        current = json.loads(prompt[prompt.index('{"stack"') :])
        assert current["batch"] == number
        for previous in self.accepted:
            assert current["files"][f"public/step-{previous}.js"] == f"const step{previous} = true;\n"
        if number in self.invalid_calls or number in self.truncated_calls:
            return ModelResult(
                '{"files":',
                {"input": 100, "cached": 0, "output": 100},
                f"synthetic-call-{number}",
                "GENERATION_TRUNCATED" if number in self.truncated_calls else None,
            )
        response = {
            "files": [{"path": f"public/step-{number}.js", "content": f"const step{number} = true;\n"}],
            "edits": [],
            "complete": number == self.complete_after,
        }
        if policy["generation_contract_version"] == 4:
            response = {"action": "implement", "message": f"Etapa {number} implementada.", **response}
        self.accepted.append(number)
        return ModelResult(
            json.dumps(response), {"input": 100, "cached": 0, "output": 100}, f"synthetic-call-{number}"
        )


@pytest.mark.parametrize("contract", [3, 4])
def test_new_generation_can_complete_on_tenth_call_without_partial_application(system, contract):
    repo, actor, project, operation = start_generation(system, contract)
    original = repo.source_files(actor, project["id"], project["current_revision_id"])
    gateway = ContinuingGateway(repo, actor, project)
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "succeeded", result
    assert len(gateway.prompts) == 10
    assert not worker.tick(operation["id"])
    assert result["result_ref"]["candidate"]
    files = repo.source_files(actor, project["id"], result["result_ref"]["revision_id"])
    assert files == {
        **original,
        **{f"public/step-{number}.js": f"const step{number} = true;\n" for number in range(1, 11)},
    }
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    assert repo.wallet(actor)["reserved_ratio"] == 0
    for number, prompt in enumerate(gateway.prompts, 1):
        current = json.loads(prompt[prompt.index('{"stack"') :])
        assert current["batches_remaining"] == 11 - number
        assert gateway.policies[number - 1]["max_batches"] == 10
    with repo.sessions() as db:
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
        assert len(attempt.usage["calls"]) == 10
        assert attempt.actual_brl == cost(system["policy"], {"input": 1000, "cached": 0, "output": 1000})


@pytest.mark.parametrize("contract", [3, 4])
def test_incomplete_tenth_call_stops_without_an_eleventh_or_saved_partial(
    system, diagnostic_records, contract
):
    repo, actor, project, operation = start_generation(system, contract)
    original = repo.source_files(actor, project["id"], project["current_revision_id"])
    gateway = ContinuingGateway(repo, actor, project, complete_after=11)
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "failed"
    assert result["error_code"] == "GENERATION_LIMIT"
    assert result["billing_status"] == "settled"
    assert len(gateway.prompts) == 10
    assert "revision_id" not in result["result_ref"]
    assert repo.source_files(actor, project["id"], project["current_revision_id"]) == original
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    assert repo.wallet(actor)["reserved_ratio"] == 0
    terminal = next(
        record
        for record in reversed(diagnostic_records())
        if record.get("event") == "generation_finished" and record.get("operation_id") == operation["id"]
    )
    assert terminal["reason_code"] == "GENERATION_CALL_LIMIT"
    assert terminal["max_batches"] == terminal["provider_calls"] == terminal["accepted_batches"] == 10
    assert terminal["repair_attempts"] == terminal["truncation_retries"] == 0
    with repo.sessions() as db:
        assert (
            db.scalar(select(func.count()).select_from(Revision).where(Revision.project_id == project["id"]))
            == 1
        )
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"]))
        assert len(attempt.usage["calls"]) == 10
        assert attempt.actual_brl == cost(system["policy"], {"input": 1000, "cached": 0, "output": 1000})


@pytest.mark.parametrize("limit", [1, 4, 6, 10])
def test_configured_call_limit_controls_prompts_dispatches_and_reservation(system, limit):
    Integrations(system["repo"]).save(
        system["admin"],
        "jev",
        new_id(),
        0,
        {
            "settings": BuildPolicy(max_generation_calls=limit).model_dump(),
            "reason": "Ajustar o orçamento autorizado por construção",
        },
    )
    repo, actor, project, operation = start_generation(system)
    with repo.sessions() as db:
        saved = db.get(Operation, operation["id"])
        assert saved.policy["max_batches"] == limit
        assert saved.policy["generation_input_budget"] == INPUT_BOUND * limit
        assert saved.policy["reserved_input_bound"] == INPUT_BOUND
        expected = money(
            cost(
                system["policy"],
                {
                    "input": INPUT_BOUND * limit,
                    "cached": 0,
                    "output": system["policy"]["max_output_tokens"] * limit,
                },
            )
            * Decimal("1.10")
        )
        assert (
            db.scalar(select(func.sum(Reservation.amount)).where(Reservation.operation_id == saved.id))
            == expected
        )
    gateway = ContinuingGateway(repo, actor, project, complete_after=limit)
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert repo.operation(actor, operation["id"])["state"] == "succeeded"
    assert len(gateway.prompts) == limit
    for number, prompt in enumerate(gateway.prompts, 1):
        current = json.loads(prompt[prompt.index('{"stack"') :])
        assert current["batches_remaining"] == limit - number + 1
    assert not worker.tick(operation["id"])


def test_larger_total_budget_does_not_expand_the_per_call_context_cap():
    policy = {
        "generation_context_version": 2,
        "generation_input_budget": 640960,
        "reserved_input_bound": INPUT_BOUND,
        "max_batches": 10,
    }
    assert MAX_GENERATION_CONTEXT_BYTES == 252288
    assert context_input_budget(policy) == 640960
    assert context_input_bound(policy, {}) == 252288 + INPUT_OVERHEAD
    assert context_input_bound(policy, {"generation_context_limit_bytes": 60000}) == 60000 + INPUT_OVERHEAD
    assert context_input_bound(policy, {}, input_tokens=600000) == 40960
    assert context_input_bound(policy, {}, input_tokens=640960) == 0


def test_repairs_and_truncation_share_the_ten_call_budget_with_separate_diagnostics(
    system, diagnostic_records
):
    repo, actor, project, operation = start_generation(system)
    gateway = ContinuingGateway(repo, actor, project, invalid_calls=(2, 3), truncated_calls=(1,))
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "succeeded", result
    assert len(gateway.prompts) == 10
    assert gateway.accepted == list(range(4, 11))
    assert not worker.tick(operation["id"])
    terminal = next(
        record
        for record in reversed(diagnostic_records())
        if record.get("event") == "generation_finished" and record.get("operation_id") == operation["id"]
    )
    assert terminal["max_batches"] == 10
    assert terminal["provider_calls"] == 10
    assert terminal["accepted_batches"] == 7
    assert terminal["repair_attempts"] == 2
    assert terminal["truncation_retries"] == 1
    assert terminal["input_tokens"] == 1000
    assert terminal["output_tokens"] == 1000


@pytest.mark.parametrize(
    "invalid,truncated,expected_calls,expected_error",
    [((1, 2, 3), (), 3, "INVALID_GENERATION"), ((), (1, 2), 2, "GENERATION_TRUNCATED")],
)
def test_additional_call_capacity_does_not_add_repair_or_truncation_retries(
    system, invalid, truncated, expected_calls, expected_error
):
    Integrations(system["repo"]).save(
        system["admin"], "jev", "bounded-recovery-policy", 0,
        {"settings": {"max_truncation_retries": 1, "max_response_repairs": 2},
         "reason": "Preservar limites explícitos de recuperação"},
    )
    repo, actor, project, operation = start_generation(system)
    gateway = ContinuingGateway(repo, actor, project, invalid_calls=invalid, truncated_calls=truncated)
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "failed"
    assert result["error_code"] == expected_error
    assert len(gateway.prompts) == expected_calls
    assert "revision_id" not in result["result_ref"]
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
