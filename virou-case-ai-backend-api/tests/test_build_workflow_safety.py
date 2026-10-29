import copy
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_build_workflow import dispatch, prepare, review, save, select_template, send_request, workflow
from test_template_preparations import PreparationGateway

from viracase.application.generation import GenerationService
from viracase.domain.rules import RuleError, cost, utcnow
from viracase.domain.template_fields import render
from viracase.infrastructure.credentials import initialize_key
from viracase.infrastructure.feature_policies import FeaturePolicies
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import (
    Attempt,
    Grant,
    Job,
    Ledger,
    Operation,
    RecommendationRun,
    Reservation,
)
from viracase.infrastructure.recommendations import Recommendations
from viracase.infrastructure.template_preparations import TemplatePreparations
from viracase.infrastructure.worker import Worker
from viracase.interfaces.api import create_app
from viracase.interfaces.feature_schemas import BuildPolicy

__all__ = ["workflow"]


class InterruptedProcess(BaseException):
    pass


def test_template_revoked_during_provider_call_preserves_edits_and_settles_actual_usage(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    selected = select_template(flow, actor, project)
    template = flow.templates.version(actor, selected["data"]["attachments"][0]["source_version_id"])
    field_id = template["fields"][0]["key"]
    manual_value = "Decisão humana preservada durante o preenchimento"
    identity = queue_preparation(system, flow, actor, project)

    def edit_then_revoke():
        def edit(data):
            attachment = data["attachments"][0]
            attachment["fields"][field_id] = manual_value
            attachment["edited_content"] = render(
                template["content"], template["fields"], attachment["fields"]
            )

        save(flow, actor, project, edit)
        flow.templates.save(
            system["admin"],
            "revoke-during-fill",
            {
                "slug": template["id"],
                "expected_version": template["version"],
                **{key: template[key] for key in ("title", "category", "summary", "content", "fields")},
                "available": False,
                "reason": "Revogação administrativa durante teste de concorrência",
            },
        )

    gateway = PreparationGateway(callback=edit_then_revoke)
    worker = Worker(system["repo"], gateway, system["catalog"])
    assert worker.tick(identity)
    assert not worker.tick(identity)
    assert len(gateway.calls) == 1
    current = flow.get(actor, project["id"])
    attachment = current["data"]["attachments"][0]
    assert attachment["fill_state"] == "failed"
    assert "revogado" in attachment["fill_message"]
    assert attachment["fields"] == {field_id: manual_value}
    assert attachment["field_values"][field_id]["edited_by_user"]
    with system["repo"].sessions() as db:
        operation = db.get(Operation, identity)
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == identity))
        expected = cost(operation.policy, {"input": 200, "cached": 0, "output": 80})
        assert operation.state == "succeeded" and operation.billing_status == "settled"
        assert attempt.state == "confirmed" and attempt.actual_brl == expected and expected > 0
        assert db.scalar(select(Job).where(Job.operation_id == identity)).state == "done"
        debits = db.scalars(
            select(Ledger).where(Ledger.operation_id == identity, Ledger.kind == "debit")
        ).all()
        assert len(debits) == 1 and debits[0].amount == expected
    with pytest.raises(RuleError) as error:
        review(flow, actor, project)
    assert error.value.code == "TEMPLATE_UNAVAILABLE"
    save(flow, actor, project, lambda data: data["attachments"][0].update(enabled=False))
    assert review(flow, actor, project, True)["data"]["can_build"]


def queue_preparation(system, flow, actor, project):
    repo = system["repo"]
    with repo.sessions.begin() as db:
        operation = db.scalar(
            select(Operation).where(
                Operation.project_id == project["id"],
                Operation.type == "template_autofill",
                Operation.state == "queued",
            )
        )
        db.scalar(select(Job).where(Job.operation_id == operation.id)).available_at = utcnow() - timedelta(
            seconds=1
        )
        identity = operation.id
    assert Worker(repo, PreparationGateway(), system["catalog"]).tick(identity)
    with repo.sessions() as db:
        operation = db.scalar(
            select(Operation).where(
                Operation.project_id == project["id"],
                Operation.type == "template_preparation",
                Operation.state == "queued",
            )
        )
        assert operation is not None
        return operation.id


def test_previous_request_dispatcher_cannot_swallow_next_request_selection(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project)
    save(flow, actor, project, lambda data: data["attachments"][0].update(enabled=False))
    approved = review(flow, actor, project, True)
    flow.send(
        actor,
        project["id"],
        "first-build",
        send_request(approved, project),
        GenerationService(system["repo"], system["catalog"]),
    )
    prepare(flow, actor, project)
    save(flow, actor, project, lambda data: data["attachments"][0].update(enabled=True))
    current, gateway = dispatch(system, flow, actor, project)
    assert len(gateway.calls) == 1
    assert current["data"]["attachments"][0]["fill_state"] == "ready"
    assert current["data"]["attachments"][0]["enabled"]


def test_abandoned_recommendation_recovers_to_explicit_manual_catalog_without_new_call(
    system, workflow, monkeypatch
):
    flow, actor, project = workflow
    current = save(
        flow, actor, project, lambda data: data.update(message_text="Adicionar critérios claros à home")
    )

    def interrupt(*args, **kwargs):
        raise InterruptedProcess()

    with monkeypatch.context() as patch:
        patch.setattr("viracase.infrastructure.recommendations.TraceObserver", interrupt)
        with pytest.raises(InterruptedProcess):
            Recommendations(flow).recommend(
                actor, project["id"], "interrupted", {"expected_draft_revision": current["version"]}
            )
    with system["repo"].sessions.begin() as db:
        operation = db.scalar(select(RecommendationRun).where(RecommendationRun.project_id == project["id"]))
        assert operation.state == "pending"
        operation.created_at = utcnow() - timedelta(minutes=5)
    recovered = flow.get(actor, project["id"])
    assert recovered["data"]["stage"] == "templates"
    assert recovered["data"]["recommendation_status"] == "fallback"
    assert recovered["data"]["can_review"]
    with monkeypatch.context() as patch:
        patch.setattr("viracase.infrastructure.recommendations.TraceObserver", interrupt)
        result = Recommendations(flow).recommend(
            actor, project["id"], "safe-recovery", {"expected_draft_revision": recovered["version"]}
        )
    assert result["fallback_reason"] == "RECOMMENDATION_OUTCOME_UNKNOWN"
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reservation)) == 0


def test_operator_reconciliation_unblocks_retry_without_losing_draft(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project)
    current, gateway = dispatch(system, flow, actor, project, PreparationGateway(uncertain=True))
    attachment = current["data"]["attachments"][0]
    assert attachment["fill_state"] == "reconciling"
    client = TestClient(
        create_app(True, (system["config"], system["engine"], system["repo"], system["catalog"], None))
    )
    login = client.post(
        "/admin-api/v1/auth/login",
        json={"login": "operator", "password": "test-operator-password"},
        headers={"Origin": system["config"].admin_origin},
    )
    assert login.status_code == 200
    result = client.post(
        "/admin-api/v1/operations/" + attachment["preparation_id"] + "/reconcile",
        json={
            "confirmed_no_effect": True,
            "reason": "Conciliação sintética comprovada",
            "evidence_reference": "synthetic-provider-no-effect",
        },
        headers={
            "Origin": system["config"].admin_origin,
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": "reconcile-preparation",
        },
    )
    assert result.status_code == 200, result.text
    current = flow.get(actor, project["id"])
    assert current["data"]["attachments"][0]["fill_state"] == "failed"
    retry = flow.retry_fill(
        actor,
        project["id"],
        "after-reconciliation",
        {"expected_draft_revision": current["version"], "attachment_ids": [attachment["template_id"]]},
    )
    assert retry["data"]["attachments"][0]["fill_state"] == "queued"
    current, second = dispatch(system, flow, actor, project)
    assert current["data"]["attachments"][0]["fill_state"] == "ready"
    assert len(gateway.calls) == len(second.calls) == 1


def test_human_clear_and_aba_during_provider_response_are_preserved(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    selected = select_template(flow, actor, project)
    template = flow.templates.version(actor, selected["data"]["attachments"][0]["source_version_id"])
    field = template["fields"][0]["key"]

    def edit_and_clear():
        save(
            flow,
            actor,
            project,
            lambda data: data["attachments"][0]["fields"].update({field: "Edição transitória"}),
        )
        save(flow, actor, project, lambda data: data["attachments"][0]["fields"].update({field: None}))

    current, gateway = dispatch(system, flow, actor, project, PreparationGateway(callback=edit_and_clear))
    item = current["data"]["attachments"][0]
    assert item["fields"][field] is None
    assert item["field_values"][field]["edited_by_user"]
    assert item["field_values"][field]["field_revision"] == 3
    assert item["fill_state"] == "needs_input"
    assert any(key != field and value for key, value in item["fields"].items())
    assert len(gateway.calls) == 1


def test_queued_preparation_keeps_registered_policy_after_admin_update(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project)
    identity = queue_preparation(system, flow, actor, project)
    with system["repo"].sessions() as db:
        original = copy.deepcopy(db.get(Operation, identity).policy)
    policies = FeaturePolicies(system["repo"], system["catalog"])
    current = policies.current("template_preparation")
    policies.save(
        system["admin"],
        "template_preparation",
        "next-config",
        current["version"],
        {
            "settings": {**current["settings"], "enabled": False, "max_output_tokens": 256},
            "reason": "Alterar somente operações futuras",
        },
    )
    gateway = PreparationGateway()
    assert Worker(system["repo"], gateway, system["catalog"]).tick(identity)
    assert len(gateway.calls) == 1
    assert gateway.calls[0][0] == original
    assert flow.get(actor, project["id"])["data"]["attachments"][0]["fill_state"] == "ready"


def test_manual_completion_before_provider_dispatch_avoids_unnecessary_call_and_charge(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    selected = select_template(flow, actor, project)
    template = flow.templates.version(actor, selected["data"]["attachments"][0]["source_version_id"])
    identity = queue_preparation(system, flow, actor, project)
    values = {field["key"]: "Decisão humana para " + field["label"] for field in template["fields"]}

    def complete_manually(data):
        data["attachments"][0]["fields"] = values
        data["attachments"][0]["edited_content"] = render(template["content"], template["fields"], values)

    save(flow, actor, project, complete_manually)
    gateway = PreparationGateway()
    assert Worker(system["repo"], gateway, system["catalog"]).tick(identity)
    assert not gateway.calls
    current = flow.get(actor, project["id"])
    assert current["data"]["attachments"][0]["fields"] == values
    assert current["data"]["attachments"][0]["fill_state"] == "ready"
    assert current["data"]["can_review"]
    wallet = system["repo"].wallet(actor, True)
    assert Decimal(wallet["consumed_brl"]) == Decimal(wallet["reserved_brl"]) == 0


def test_cancelled_queued_preparation_can_retry_without_stuck_fill_or_double_charge(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    selected = select_template(flow, actor, project)
    identity = queue_preparation(system, flow, actor, project)
    service = TemplatePreparations(system["repo"], flow.learning, system["catalog"])
    cancelled = service.cancel(actor, project["id"], identity, "cancel-before-provider")
    assert cancelled["state"] == "cancelled"
    current = flow.get(actor, project["id"])
    assert current["data"]["attachments"][0]["fill_state"] in {"needs_input", "failed"}
    gateway = PreparationGateway()
    assert not Worker(system["repo"], gateway, system["catalog"]).tick(identity)
    assert not gateway.calls
    retry = flow.retry_fill(
        actor,
        project["id"],
        "retry-after-cancel",
        {
            "expected_draft_revision": current["version"],
            "attachment_ids": [selected["data"]["attachments"][0]["template_id"]],
        },
    )
    assert retry["data"]["attachments"][0]["fill_state"] == "queued"
    current, gateway = dispatch(system, flow, actor, project)
    assert len(gateway.calls) == 1
    assert current["data"]["attachments"][0]["fill_state"] == "ready"
    with system["repo"].sessions() as db:
        assert db.get(Operation, identity).billing_status == "released"
        assert (
            db.scalar(
                select(func.sum(Ledger.amount)).where(Ledger.operation_id == identity, Ledger.kind == "debit")
            )
            == 0
        )


def test_crash_after_dispatch_reservation_recovers_without_duplicate_call_or_charge(
    system, workflow, monkeypatch
):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project)
    original_start = TemplatePreparations.start

    def crash_after_start(self, *args, **kwargs):
        original_start(self, *args, **kwargs)
        raise InterruptedProcess()

    with system["repo"].sessions.begin() as db:
        dispatcher = db.scalar(
            select(Operation).where(
                Operation.type == "template_autofill", Operation.project_id == project["id"]
            )
        )
        job = db.scalar(select(Job).where(Job.operation_id == dispatcher.id))
        job.available_at = utcnow() - timedelta(seconds=1)
        dispatcher_id = dispatcher.id
    worker = Worker(system["repo"], PreparationGateway(), system["catalog"])
    with monkeypatch.context() as patch:
        patch.setattr(TemplatePreparations, "start", crash_after_start)
        with pytest.raises(InterruptedProcess):
            worker.tick(dispatcher_id)
    with system["repo"].sessions.begin() as db:
        db.scalar(select(Job).where(Job.operation_id == dispatcher_id)).lease_until = utcnow() - timedelta(
            seconds=1
        )
        preparation = db.scalar(
            select(Operation).where(
                Operation.type == "template_preparation", Operation.project_id == project["id"]
            )
        )
        preparation_id = preparation.id
    worker.recover()
    assert worker.tick(preparation_id)
    assert len(worker.gateway.calls) == 1
    assert flow.get(actor, project["id"])["data"]["attachments"][0]["fill_state"] == "ready"
    worker.tick(dispatcher_id)
    with system["repo"].sessions() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(Operation).where(Operation.type == "template_preparation")
            )
            == 1
        )
        assert db.scalar(select(func.count()).select_from(Reservation)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(Ledger)
                .where(Ledger.operation_id == preparation_id, Ledger.kind == "debit")
            )
            == 1
        )


def test_crash_after_uncertain_dispatch_never_redispatches(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project)
    identity = queue_preparation(system, flow, actor, project)
    with system["repo"].sessions.begin() as db:
        operation = db.get(Operation, identity)
        operation.state = "running"
        job = db.scalar(select(Job).where(Job.operation_id == identity))
        job.state, job.lease_token = "running", "interrupted-worker"
        job.lease_until = utcnow() - timedelta(seconds=1)
        db.add(
            Attempt(
                operation_id=identity,
                provider=operation.policy["provider"],
                model=operation.policy["model"],
                state="sent",
            )
        )
    gateway = PreparationGateway()
    worker = Worker(system["repo"], gateway, system["catalog"])
    worker.recover()
    assert not worker.tick(identity)
    current = flow.get(actor, project["id"])
    assert current["data"]["attachments"][0]["fill_state"] == "reconciling"
    assert not gateway.calls
    wallet = system["repo"].wallet(actor, True)
    assert Decimal(wallet["reserved_brl"]) > 0
    assert Decimal(wallet["consumed_brl"]) == 0


def test_forged_server_metadata_cannot_promote_idea_to_review(system, workflow):
    flow, actor, project = workflow
    current = flow.get(actor, project["id"])
    forged = {
        **current["data"],
        "message_text": "Pedido forjado",
        "stage": "review",
        "workflow_version": 1,
        "templates_completed": True,
        "empty_selection_confirmed": True,
        "prepared_fingerprint": "f" * 64,
        "review_snapshot": {"hash": "f" * 64},
        "can_build": True,
    }
    saved = flow.save(actor, project["id"], "forged-server-state", current["version"], forged)
    assert saved["data"]["stage"] == "idea"
    assert saved["data"]["review_snapshot"] is None
    assert not saved["data"]["can_build"]
    assert not saved["data"]["templates_completed"]


def test_new_jev_call_is_blocked_when_available_budget_is_zero(system, workflow):
    flow, actor, project = workflow
    current = save(
        flow, actor, project, lambda data: data.update(message_text="Adicionar critérios claros à home")
    )
    initialize_key(system["config"].credential_key_path, system["repo"].sessions)
    registry = Integrations(system["repo"])
    registry.save(
        system["admin"],
        "jev",
        "zero-budget-config",
        0,
        {
            "settings": BuildPolicy(recommendation_enabled=True).model_dump(),
            "token": "synthetic-jev-secret",
            "reason": "Validar bloqueio de uso sem saldo",
        },
    )
    registry.validate(system["admin"], "jev", 1, "Validação sintética de contrato")
    registry.activate(system["admin"], "jev", "zero-budget-active", 1, "Ativação sintética de contrato")
    with system["repo"].sessions.begin() as db:
        grant = db.scalar(select(Grant).where(Grant.account_id == actor))
        grant.consumed += grant.available
        grant.available = Decimal(0)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    recommender = Recommendations(flow, httpx.MockTransport(handler))
    with pytest.raises(RuleError) as error:
        recommender.recommend(
            actor, project["id"], "no-budget", {"expected_draft_revision": current["version"]}
        )
    assert error.value.code == "BUDGET_INSUFFICIENT"
    assert not calls
    assert flow.get(actor, project["id"])["data"]["message_text"] == current["data"]["message_text"]
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(RecommendationRun)) == 0


@pytest.mark.parametrize("recommendation_state", ["ready", "fallback"])
def test_back_to_idea_then_prepare_reuses_unchanged_recommendation_and_advances(
    system, workflow, monkeypatch, recommendation_state
):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project)
    ready, gateway = dispatch(system, flow, actor, project)
    original = copy.deepcopy(ready["data"]["attachments"])
    with system["repo"].sessions.begin() as db:
        recommendation = db.get(RecommendationRun, ready["data"]["recommendation_id"])
        recommendation.state = recommendation_state
        recommendation.result = {
            **recommendation.result,
            "status": recommendation_state,
            "fallback_reason": "RECOMMENDATION_OUTCOME_UNKNOWN"
            if recommendation_state == "fallback"
            else None,
        }
    current = save(flow, actor, project, lambda data: data.update(stage="idea"))
    assert current["data"]["stage"] == "idea"

    def no_second_recommendation(*args, **kwargs):
        raise AssertionError("A recomendação válida não deve chamar o Jev novamente")

    with monkeypatch.context() as patch:
        patch.setattr("viracase.infrastructure.recommendations.TraceObserver", no_second_recommendation)
        result = Recommendations(flow).recommend(
            actor, project["id"], "reuse-unchanged", {"expected_draft_revision": current["version"]}
        )
    reopened = flow.get(actor, project["id"])
    assert reopened["data"]["stage"] == "templates"
    assert reopened["data"]["attachments"] == original
    assert result["recommendation_id"] == ready["data"]["recommendation_id"]
    assert len(gateway.calls) == 1
    with system["repo"].sessions() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(Operation).where(Operation.type == "template_preparation")
            )
            == 1
        )


def test_legacy_apply_does_not_overwrite_human_aba_and_keeps_field_revisions(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    selected = select_template(flow, actor, project)
    item = selected["data"]["attachments"][0]
    template = flow.templates.version(actor, item["source_version_id"])
    protected = template["fields"][0]["key"]
    service = TemplatePreparations(system["repo"], flow.learning, system["catalog"])
    operation = service.start(
        actor,
        project["id"],
        "legacy-prepare",
        {
            "expected_draft_revision": selected["version"],
            "base_project_revision": project["current_revision_id"],
            "attachment_ids": [item["template_id"]],
            "expected_attachment_revisions": {item["template_id"]: item["attachment_revision"]},
        },
    )

    def edit_and_clear():
        save(
            flow,
            actor,
            project,
            lambda data: data["attachments"][0]["fields"].update({protected: "Edição temporária"}),
        )
        save(flow, actor, project, lambda data: data["attachments"][0]["fields"].update({protected: None}))

    assert Worker(system["repo"], PreparationGateway(callback=edit_and_clear), system["catalog"]).tick(
        operation["id"]
    )
    prepared = service.get(actor, project["id"], operation["id"])
    saved = flow.get(actor, project["id"])
    result = service.apply(
        actor,
        project["id"],
        operation["id"],
        "legacy-apply",
        {
            "expected_state_version": prepared["state_version"],
            "expected_draft_revision": saved["version"],
            "expected_attachment_revisions": {
                item["template_id"]: saved["data"]["attachments"][0]["attachment_revision"]
            },
            "accepted_fields": [
                {"attachment_id": item["template_id"], "field_id": field["field_id"]}
                for field in prepared["items"][0]["suggested_fields"]
            ],
        },
    )
    attachment = result["draft"]["data"]["attachments"][0]
    assert attachment["fields"][protected] is None
    assert attachment["field_values"][protected]["field_revision"] == 3
    assert any(conflict["field_id"] == protected for conflict in result["conflicts"])
    assert len(result["applied_fields"]) == len(template["fields"]) - 1
    assert all(
        meta["field_revision"] == 2 for key, meta in attachment["field_values"].items() if key != protected
    )


def test_removing_last_reviewed_attachment_requires_new_empty_confirmation(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project)
    dispatch(system, flow, actor, project)
    approved = review(flow, actor, project)
    current = save(flow, actor, project, lambda data: data["attachments"][0].update(enabled=False))
    stale_request = {**send_request(approved, project), "expected_draft_revision": current["version"]}
    with pytest.raises(RuleError) as error:
        flow.send(
            actor,
            project["id"],
            "stale-attachments",
            stale_request,
            GenerationService(system["repo"], system["catalog"]),
        )
    assert error.value.code == "REVIEW_STALE"
    with pytest.raises(RuleError) as error:
        review(flow, actor, project)
    assert error.value.code == "EMPTY_SELECTION_UNCONFIRMED"
    empty = review(flow, actor, project, True)
    assert empty["data"]["review_snapshot"]["attachments"] == []
    assert empty["data"]["review_snapshot"]["hash"] != approved["data"]["review_snapshot"]["hash"]


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="Bloqueio real por linha exige PostgreSQL",
)
def test_concurrent_builds_freeze_once_and_reserve_once(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    approved = review(flow, actor, project, True)
    request = send_request(approved, project)
    barrier = Barrier(2)

    def send(key):
        barrier.wait(timeout=10)
        try:
            return flow.send(
                actor, project["id"], key, request, GenerationService(system["repo"], system["catalog"])
            )
        except RuleError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(send, "concurrent-a")
        second = executor.submit(send, "concurrent-b")
        results = [first.result(timeout=30), second.result(timeout=30)]
    assert sum(isinstance(result, dict) for result in results) == 1
    assert [result.code for result in results if isinstance(result, RuleError)] == ["DRAFT_CONFLICT"]
    with system["repo"].sessions() as db:
        assert (
            db.scalar(select(func.count()).select_from(Operation).where(Operation.type == "generation")) == 1
        )
        assert db.scalar(select(func.count()).select_from(Reservation)) == 1
