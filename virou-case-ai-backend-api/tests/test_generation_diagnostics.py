import json
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from test_integrated_workspace import workspace
from test_workflow_diagnostics import diagnostic_records

from viracase.application.generation import GenerationService
from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.models import Attempt, Job, Operation, new_id
from viracase.infrastructure.worker import Worker
from viracase.infrastructure.workflow_logging import emit

__all__ = ["diagnostic_records", "workspace"]


def create_operation(system, workspace):
    repo, actor, project, _ = workspace
    correlation, preparation = new_id(), new_id()
    operation = GenerationService(repo, system["catalog"]).start(
        actor,
        project["id"],
        new_id(),
        {
            "brief": "Melhore a página inicial. private-request-not-for-logs",
            "base_revision_id": project["current_revision_id"],
            "policy_version": "2026-10-v1",
            "review_required": True,
            "correlation_id": correlation,
            "preparation_id": preparation,
        },
        contract_version=4,
    )
    return operation, correlation, preparation


class DiagnosticGateway:
    def __init__(self, mode):
        self.mode, self.calls = mode, 0

    def generate(self, policy, prompt):
        self.calls += 1
        if self.mode == "exception":
            raise RuntimeError("private-exception-not-for-logs")
        if self.mode == "invalid":
            response = 'private-response-not-for-logs {"files":'
        else:
            response = json.dumps(
                {
                    "action": "implement",
                    "message": "Página atualizada.",
                    "files": [],
                    "edits": [
                        {
                            "path": "public/index.html",
                            "old": "</body>",
                            "new": "<p>Alteração validada</p></body>",
                        }
                    ],
                    "complete": True,
                }
            )
        return ModelResult(response, {"input": 100, "cached": 0, "output": 100}, "synthetic")


def test_generation_invalid_response_logs_terminal_cause_without_payload_capture(
    system, workspace, diagnostic_records
):
    repo, actor, project, _ = workspace
    operation, correlation, preparation = create_operation(system, workspace)
    gateway = DiagnosticGateway("invalid")
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    current = repo.operation(actor, operation["id"])
    assert current["state"] == "failed"
    assert current["error_code"] == "INVALID_GENERATION"
    terminal = [
        record
        for record in diagnostic_records()
        if record.get("operation_id") == operation["id"]
        and record.get("state") == "failed"
        and record.get("error_code") == "INVALID_GENERATION"
    ]
    assert terminal, "Falhas de validação precisam aparecer na saída estruturada do worker."
    record = terminal[-1]
    assert record["level"] == "error"
    assert record["correlation_id"] == correlation
    assert record["preparation_id"] == preparation
    assert record["transaction_state"] == "pending_commit"
    assert record["input_tokens"] == 100 * gateway.calls
    assert record["output_tokens"] == 100 * gateway.calls
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    serialized = json.dumps(diagnostic_records())
    assert "private-request-not-for-logs" not in serialized
    assert "private-response-not-for-logs" not in serialized


def test_unexpected_generation_failure_logs_correlated_structural_error_without_replay(
    system, workspace, diagnostic_records
):
    repo, actor, project, _ = workspace
    operation, correlation, preparation = create_operation(system, workspace)
    gateway = DiagnosticGateway("exception")
    worker = Worker(repo, gateway, system["catalog"])
    assert worker.tick(operation["id"])
    assert not worker.tick(operation["id"])
    assert gateway.calls == 1
    current = repo.operation(actor, operation["id"])
    assert current["state"] == "reconciling"
    assert current["error_code"] == "PROCESSING_FAILED"
    record = next(
        row for row in diagnostic_records() if row["event"] == "generation_worker_failed"
    )
    assert record["correlation_id"] == correlation
    assert record["preparation_id"] == preparation
    assert record["operation_id"] == operation["id"]
    assert record["exception_type"] == "RuntimeError"
    assert record["frames"]
    assert record["transaction_state"] == "pending_commit"
    assert "private-exception-not-for-logs" not in json.dumps(diagnostic_records())
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


@pytest.mark.parametrize(
    "attempt_state,result_state", [(None, "queued"), ("sent", "reconciling"), ("confirmed", "failed")]
)
def test_recovered_generation_exposes_known_state_without_redispatch(
    system, workspace, diagnostic_records, attempt_state, result_state
):
    repo, actor, _, _ = workspace
    operation, correlation, _ = create_operation(system, workspace)
    with repo.sessions.begin() as db:
        op = db.get(Operation, operation["id"])
        op.state = "running"
        job = db.scalar(select(Job).where(Job.operation_id == op.id))
        job.state, job.lease_token = "running", new_id()
        job.lease_until = utcnow() - timedelta(seconds=1)
        if attempt_state:
            db.add(
                Attempt(
                    operation_id=op.id,
                    provider=op.policy["provider"],
                    model=op.policy["model"],
                    state=attempt_state,
                    actual_brl=Decimal(0) if attempt_state == "confirmed" else None,
                )
            )
    gateway = DiagnosticGateway("exception")
    Worker(repo, gateway, system["catalog"]).recover()
    assert gateway.calls == 0
    assert repo.operation(actor, operation["id"])["state"] == result_state
    record = next(
        row for row in diagnostic_records() if row["event"] == "generation_worker_recovered"
    )
    assert record["correlation_id"] == correlation
    assert record["state"] == result_state
    assert record["phase"] == "recovery"
    assert record["transaction_state"] == "pending_commit"


def test_generation_context_diagnostics_accept_counts_and_reject_content(diagnostic_records):
    numbers = {
        "batch": 2,
        "max_batches": 4,
        "files_count": 12,
        "changed_count": 3,
        "context_bytes": 70800,
        "remaining_input_budget": 252000,
        "total_input_budget": 256384,
        "request_context_bytes": 4300,
        "request_context_limit_bytes": 60000,
        "generation_context_limit_bytes": 252288,
        "generation_context_version": 2,
    }
    emit(
        "generation_context_checked",
        **numbers,
        prompt="private-prompt",
        files={"private.txt": "private-source"},
        context={"value": "private-request"},
    )
    record = diagnostic_records()[0]
    assert all(record[key] == value for key, value in numbers.items())
    assert not {"prompt", "files", "context"} & set(record)
    assert "private-" not in json.dumps(record)


def test_revision_failure_never_logs_generation_success(
    system, workspace, diagnostic_records, monkeypatch
):
    from viracase.infrastructure import conversation_generation

    repo, actor, project, _ = workspace
    operation, _, _ = create_operation(system, workspace)

    def fail_save(*args, **kwargs):
        raise RuleError("SOURCE_STORAGE_FAILED", "private-storage-detail")

    monkeypatch.setattr(conversation_generation, "save_turn", fail_save)
    gateway = DiagnosticGateway("complete")
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    current = repo.operation(actor, operation["id"])
    assert current["state"] == "failed"
    assert current["error_code"] == "SOURCE_STORAGE_FAILED"
    terminal = [row for row in diagnostic_records() if row["event"] == "generation_finished"]
    assert terminal and all(row["state"] != "succeeded" for row in terminal)
    assert terminal[-1]["error_code"] == "SOURCE_STORAGE_FAILED"
    assert "private-storage-detail" not in json.dumps(diagnostic_records())
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
