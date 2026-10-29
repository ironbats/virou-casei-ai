import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_build_workflow import dispatch, save, select_template, workflow

from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError, cost
from viracase.domain.template_fields import render
from viracase.infrastructure.models import Job, Ledger, Operation, Reservation
from viracase.infrastructure.recommendations import Recommendations
from viracase.infrastructure.worker import Worker
from viracase.interfaces.api import create_app

__all__ = ["workflow"]

SLUG = "field-generation-safety"
REQUEST = (
    "Organizar chamados de suporte da equipe Aurora, com uma fila por responsável. "
    "O colaborador autenticado deve visualizar somente seus próprios chamados."
)
OWNER_PLAN = (
    "Proposta para implementar: obter a identidade de uma sessão validada no servidor "
    "e comparar o responsável de cada chamado antes de permitir o acesso."
)
EXISTING_NOTE = "Proposta: mostrar a fila pessoal de chamados ao colaborador autenticado."
NEXT_OWNER_PLAN = (
    "Proposta para revisão: vincular cada chamado à identidade autenticada validada no servidor "
    "e negar o acesso quando o vínculo não corresponder ao colaborador."
)
INITIAL_RESPONSE = {
    "items": {
        SLUG: {
            "owner_plan": {"proposal": OWNER_PLAN},
            "ui_note": {"proposal": EXISTING_NOTE},
        }
    }
}
TARGET_RESPONSE = {"items": {SLUG: {"owner_plan": {"proposal": NEXT_OWNER_PLAN}}}}
USAGE = {"input": 77, "cached": 0, "output": 33}


class ProposalGateway:
    def __init__(self, response, callback=None):
        self.response = copy.deepcopy(response)
        self.callback = callback
        self.calls = []

    def generate(self, policy, prompt):
        self.calls.append((copy.deepcopy(policy), json.loads(prompt)))
        if self.callback:
            self.callback()
        return ModelResult(json.dumps(self.response), dict(USAGE), "synthetic-field-response")


def edit_fields(case, changes):
    flow, actor, project = case["workflow"]
    template = case["template"]

    def mutate(data):
        item = next(item for item in data["attachments"] if item["template_id"] == SLUG)
        item["fields"].update(changes)
        item["edited_content"] = render(template["content"], template["fields"], item["fields"])

    return save(flow, actor, project, mutate)


def attachment(draft):
    return next(item for item in draft["data"]["attachments"] if item["template_id"] == SLUG)


def counts(system):
    with system["repo"].sessions() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model)) for model in (Operation, Reservation, Ledger)
        )


def field_request(case, draft=None, field_id="owner_plan"):
    flow, actor, project = case["workflow"]
    draft = draft or flow.get(actor, project["id"])
    return {
        "expected_draft_revision": draft["version"],
        "attachment_ids": [SLUG],
        "target_field_id": field_id,
    }


def start_field(case, key="generate-owner", request=None):
    response = case["client"].post(
        case["base"] + "/build-fill",
        json=request or field_request(case),
        headers={**case["headers"], "Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()


def assert_one_settlement(system, operation_id):
    with system["repo"].sessions() as db:
        operation = db.get(Operation, operation_id)
        assert operation.billing_status == "settled"
        expected = cost(operation.policy, USAGE)
        debits = db.scalars(
            select(Ledger).where(Ledger.operation_id == operation_id, Ledger.kind == "debit")
        ).all()
        assert len(debits) == 1 and debits[0].amount == expected and expected > 0
        assert db.scalar(select(Job).where(Job.operation_id == operation_id)).state == "done"


def new_preparation(system, prior_ids):
    with system["repo"].sessions() as db:
        items = db.scalars(select(Operation).where(Operation.type == "template_preparation")).all()
        created = [item.id for item in items if item.id not in prior_ids]
        assert len(created) == 1
        return created[0]


@pytest.fixture
def field_case(system, workflow):
    flow, actor, project = workflow
    template = flow.templates.save(
        system["admin"],
        "field-safety-template",
        {
            "slug": SLUG,
            "expected_version": 0,
            "title": "Planejamento de autorização de chamados",
            "category": "security",
            "summary": "Plano de acesso a chamados sujeito à revisão do desenvolvedor.",
            "content": (
                "Origem planejada: {{owner_plan}}\n"
                "Interface planejada: {{ui_note}}\n"
                "Credencial opcional: {{access_secret}}\n"
            ),
            "fields": [
                {
                    "key": "owner_plan",
                    "semantic_id": "tenant_source",
                    "label": "Origem do responsável",
                    "required": True,
                    "max_length": 800,
                    "allowed_source_types": ["request", "project_context", "user_answer"],
                },
                {"key": "ui_note", "label": "Interface", "required": True, "max_length": 800},
                {
                    "key": "access_secret",
                    "label": "Credencial privada",
                    "required": False,
                    "max_length": 200,
                    "sensitivity": "secret",
                },
            ],
            "available": True,
            "reason": "Template sintético para verificar geração de campo e proteção de edição humana.",
        },
    )
    draft = save(flow, actor, project, lambda data: data.update(message_text=REQUEST))
    Recommendations(flow).recommend(
        actor, project["id"], "field-safety-prepare", {"expected_draft_revision": draft["version"]}
    )
    select_template(flow, actor, project, SLUG)
    seeded, gateway = dispatch(system, flow, actor, project, ProposalGateway(INITIAL_RESPONSE))
    assert len(gateway.calls) == 1
    assert attachment(seeded)["fields"]["owner_plan"] == OWNER_PLAN
    assert attachment(seeded)["fields"]["ui_note"] == EXISTING_NOTE
    case = {"workflow": workflow, "template": template}
    empty_draft = edit_fields(case, {"owner_plan": None})
    assert attachment(empty_draft)["field_values"]["owner_plan"]["edited_by_user"] is True
    with system["repo"].sessions() as db:
        case["prior_preparations"] = set(
            db.scalars(select(Operation.id).where(Operation.type == "template_preparation"))
        )
    dependencies = (system["config"], system["engine"], system["repo"], system["catalog"], None)
    with TestClient(create_app(dependencies=dependencies)) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"login": "student0", "password": "test-student-password"},
            headers={"Origin": system["config"].public_origin},
        )
        assert response.status_code == 200, response.text
        case.update(
            client=client,
            headers={
                "Origin": system["config"].public_origin,
                "X-CSRF-Token": response.json()["csrf_token"],
            },
            base=f"/api/v1/projects/{project['id']}",
            dependencies=dependencies,
        )
        yield case


def test_field_generation_requires_session_csrf_and_project_owner(system, field_case):
    case = field_case
    before = counts(system)
    request = field_request(case)
    response = case["client"].post(
        case["base"] + "/build-fill",
        json=request,
        headers={"Origin": system["config"].public_origin, "Idempotency-Key": "missing-csrf"},
    )
    assert response.status_code == 403 and response.json()["error"]["code"] == "CSRF_DENIED"
    with TestClient(create_app(dependencies=case["dependencies"])) as other:
        response = other.post(
            case["base"] + "/build-fill",
            json=request,
            headers={"Origin": system["config"].public_origin, "Idempotency-Key": "anonymous"},
        )
        assert response.status_code == 401
        login = other.post(
            "/api/v1/auth/login",
            json={"login": "student1", "password": "test-student-password"},
            headers={"Origin": system["config"].public_origin},
        )
        assert login.status_code == 200
        response = other.post(
            case["base"] + "/build-fill",
            json=request,
            headers={
                "Origin": system["config"].public_origin,
                "X-CSRF-Token": login.json()["csrf_token"],
                "Idempotency-Key": "foreign-project",
            },
        )
        assert response.status_code == 404 and response.json()["error"]["code"] == "NOT_FOUND"
    assert counts(system) == before


@pytest.mark.parametrize(
    "target",
    ["not_a_field", "../owner_plan", "", "x" * 61, "ui_note", "access_secret"],
    ids=["unknown", "path", "empty", "oversize", "nonempty", "secret"],
)
def test_field_generation_denies_invalid_nonempty_or_secret_target_without_reserving(
    system, field_case, target
):
    before = counts(system)
    response = field_case["client"].post(
        field_case["base"] + "/build-fill",
        json=field_request(field_case, field_id=target),
        headers={**field_case["headers"], "Idempotency-Key": "invalid-target"},
    )
    assert response.status_code in {400, 409}, response.text
    assert counts(system) == before


@pytest.mark.parametrize("attachments", [[SLUG, SLUG], [SLUG, "sdd"], []])
def test_field_generation_accepts_exactly_one_selected_attachment(system, field_case, attachments):
    before = counts(system)
    response = field_case["client"].post(
        field_case["base"] + "/build-fill",
        json={**field_request(field_case), "attachment_ids": attachments},
        headers={**field_case["headers"], "Idempotency-Key": "invalid-scope"},
    )
    assert response.status_code == 400, response.text
    assert counts(system) == before


def test_explicit_field_intent_fills_human_cleared_target_once_and_keeps_request_context(
    system, field_case
):
    case = field_case
    flow, actor, project = case["workflow"]
    initial = flow.get(actor, project["id"])
    initial_field = attachment(initial)["field_values"]["owner_plan"]
    initial_other = copy.deepcopy(attachment(initial)["field_values"]["ui_note"])
    request = field_request(case, initial)
    queued = start_field(case, request=request)
    repeated = start_field(case, request=request)
    assert queued == repeated
    metadata = attachment(queued)["field_generations"]["owner_plan"]
    assert metadata["state"] == "queued" and metadata["operation_id"]
    assert attachment(queued)["field_values"]["owner_plan"] == initial_field
    before_second_key = counts(system)
    busy = case["client"].post(
        case["base"] + "/build-fill",
        json=field_request(case),
        headers={**case["headers"], "Idempotency-Key": "different-intent-same-active-field"},
    )
    assert busy.status_code == 409, busy.text
    assert counts(system) == before_second_key
    gateway = ProposalGateway(TARGET_RESPONSE)
    completed, _ = dispatch(system, flow, actor, project, gateway)
    item = attachment(completed)
    assert len(gateway.calls) == 1
    policy, prompt = gateway.calls[0]
    assert policy["template_preparation_contract"] == 4
    assert REQUEST in json.dumps(prompt, ensure_ascii=False)
    assert [(template["attachment_id"], [field["key"] for field in template["fields"]]) for template in prompt["templates"]] == [
        (SLUG, ["owner_plan"])
    ]
    assert item["fields"]["owner_plan"] == NEXT_OWNER_PLAN
    assert item["fields"]["ui_note"] == EXISTING_NOTE
    assert item["field_values"]["ui_note"] == initial_other
    assert item["field_values"]["owner_plan"]["source_type"] == "policy_suggestion"
    assert item["field_values"]["owner_plan"]["source_refs"] == []
    assert item["field_values"]["owner_plan"]["field_revision"] > initial_field["field_revision"]
    assert item["field_generations"]["owner_plan"]["state"] == "succeeded"
    assert not completed["data"]["can_build"] and not completed["data"].get("review_snapshot")
    identity = new_preparation(system, case["prior_preparations"])
    assert_one_settlement(system, identity)
    assert not Worker(system["repo"], gateway, system["catalog"]).tick(identity)
    assert len(gateway.calls) == 1
    assert_one_settlement(system, identity)


def test_client_cannot_forge_field_generation_or_remove_human_revision_protection(system, field_case):
    case = field_case
    flow, actor, project = case["workflow"]
    queued = start_field(case)
    original = copy.deepcopy(attachment(queued))
    payload = copy.deepcopy(queued["data"])
    fake = attachment({"data": payload})
    fake["field_generations"] = {
        "owner_plan": {
            "state": "ready",
            "operation_id": "11111111-1111-1111-1111-111111111111",
            "message": "Conteúdo aprovado pelo cliente",
            "error_code": None,
        }
    }
    fake["field_values"]["owner_plan"].update(
        field_revision=999999, edited_by_user=False, source_type="project_context"
    )
    before = counts(system)
    response = case["client"].put(
        case["base"] + "/build-draft",
        json=payload,
        headers={
            **case["headers"],
            "Idempotency-Key": "forge-field-metadata",
            "If-Match": str(queued["version"]),
        },
    )
    assert response.status_code == 200, response.text
    current = attachment(response.json())
    assert current["field_generations"] == original["field_generations"]
    assert current["field_values"]["owner_plan"] == original["field_values"]["owner_plan"]
    assert current["fields"].get("owner_plan") is None
    assert counts(system) == before
    completed, gateway = dispatch(system, flow, actor, project, ProposalGateway(TARGET_RESPONSE))
    assert len(gateway.calls) == 1
    assert attachment(completed)["fields"]["owner_plan"] == NEXT_OWNER_PLAN


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Concorrência exige PostgreSQL real")
@pytest.mark.parametrize("same_key", [True, False], ids=["same-intent", "competing-intents"])
def test_parallel_field_clicks_schedule_and_bill_once(system, field_case, same_key):
    case = field_case
    flow, actor, project = case["workflow"]
    request = field_request(case)
    gate = Barrier(2)
    before = counts(system)

    def click(index):
        gate.wait(timeout=10)
        try:
            key = "parallel-same-intent" if same_key else "parallel-intent-" + str(index)
            return flow.retry_fill(actor, project["id"], key, copy.deepcopy(request))
        except RuleError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(click, (1, 2)))
    if same_key:
        assert all(isinstance(result, dict) for result in results)
        assert results[0] == results[1]
    else:
        assert sum(isinstance(result, dict) for result in results) == 1
        errors = [result for result in results if isinstance(result, RuleError)]
        assert len(errors) == 1 and errors[0].code in {"DRAFT_CONFLICT", "FILL_PENDING"}
    scheduled = counts(system)
    assert scheduled == (before[0] + 1, before[1], before[2])
    completed, gateway = dispatch(system, flow, actor, project, ProposalGateway(TARGET_RESPONSE))
    assert len(gateway.calls) == 1
    assert attachment(completed)["fields"]["owner_plan"] == NEXT_OWNER_PLAN
    assert_one_settlement(system, new_preparation(system, case["prior_preparations"]))


def test_later_human_aba_blocks_target_response_but_settles_observed_usage(system, field_case):
    case = field_case
    flow, actor, project = case["workflow"]
    queued = start_field(case)
    captured_revision = attachment(queued)["field_values"]["owner_plan"]["field_revision"]

    def human_aba():
        edit_fields(case, {"owner_plan": "Decisão humana transitória sobre identidade"})
        edit_fields(case, {"owner_plan": None})

    completed, gateway = dispatch(
        system, flow, actor, project, ProposalGateway(TARGET_RESPONSE, human_aba)
    )
    current = attachment(completed)
    assert len(gateway.calls) == 1
    assert current["fields"]["owner_plan"] is None
    assert current["field_values"]["owner_plan"]["edited_by_user"] is True
    assert current["field_values"]["owner_plan"]["field_revision"] >= captured_revision + 2
    assert current["fields"]["ui_note"] == EXISTING_NOTE
    assert current["field_generations"]["owner_plan"]["state"] not in {
        "queued",
        "running",
        "ready",
        "succeeded",
    }
    assert_one_settlement(system, new_preparation(system, case["prior_preparations"]))


def test_editing_sibling_during_generation_preserves_it_without_discarding_target(system, field_case):
    case = field_case
    flow, actor, project = case["workflow"]
    start_field(case)
    sibling = "Decisão do desenvolvedor: a interface deve mostrar os chamados em ordem de prioridade."

    def edit_sibling():
        edit_fields(case, {"ui_note": sibling})

    completed, gateway = dispatch(
        system, flow, actor, project, ProposalGateway(TARGET_RESPONSE, edit_sibling)
    )
    current = attachment(completed)
    assert len(gateway.calls) == 1
    assert current["fields"]["owner_plan"] == NEXT_OWNER_PLAN
    assert current["fields"]["ui_note"] == sibling
    assert current["field_values"]["ui_note"]["edited_by_user"] is True
    assert current["field_values"]["owner_plan"]["source_type"] == "policy_suggestion"
    assert_one_settlement(system, new_preparation(system, case["prior_preparations"]))


def test_changed_request_during_generation_cannot_receive_old_target_suggestion(system, field_case):
    case = field_case
    flow, actor, project = case["workflow"]
    start_field(case)
    next_request = "Criar uma agenda pública de plantões, sem exibir chamados individuais."

    def change_request():
        save(flow, actor, project, lambda data: data.update(message_text=next_request, stage="idea"))

    completed, gateway = dispatch(
        system, flow, actor, project, ProposalGateway(TARGET_RESPONSE, change_request)
    )
    assert len(gateway.calls) == 1
    assert completed["data"]["message_text"] == next_request
    assert attachment(completed)["fields"].get("owner_plan") is None
    assert not completed["data"]["can_build"]
    assert_one_settlement(system, new_preparation(system, case["prior_preparations"]))
