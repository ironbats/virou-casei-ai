import copy
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_conversational_apply import configure
from test_template_preparations import PreparationGateway, snapshot_contract

from viracase.application.generation import GenerationService
from viracase.domain.rules import RuleError, canonical_hash, utcnow
from viracase.infrastructure.guided_build import GuidedBuild
from viracase.infrastructure.learning import LearningLibrary
from viracase.infrastructure.models import Job, Operation, Project, Reservation
from viracase.infrastructure.recommendations import Recommendations
from viracase.infrastructure.worker import Worker


@pytest.fixture
def workflow(system):
    repo, actor = system["repo"], system["users"][0]
    configure(system)
    project = repo.create_project(
        actor, "flow-project", {"name": "Fluxo obrigatório", "objective": "Organizar tarefas da equipe"}
    )
    flow = GuidedBuild(repo, LearningLibrary(system["config"].content_root))
    return flow, actor, project


def save(flow, actor, project, mutate):
    current = flow.get(actor, project["id"])
    data = copy.deepcopy(current["data"])
    mutate(data)
    return flow.save(actor, project["id"], "save-" + str(current["version"]), current["version"], data)


def prepare(flow, actor, project):
    value = save(
        flow,
        actor,
        project,
        lambda data: data.update(message_text="Adicionar critérios claros à página inicial"),
    )
    Recommendations(flow).recommend(
        actor,
        project["id"],
        "prepare-" + str(value["version"]),
        {"expected_draft_revision": value["version"]},
    )
    return flow.get(actor, project["id"])


def select_template(flow, actor, project, slug="sdd"):
    template = next(item for item in flow.templates.list(actor)["items"] if item["id"] == slug)
    return save(
        flow,
        actor,
        project,
        lambda data: data["attachments"].append(
            {
                "template_id": template["id"],
                "source_version_id": template["version_id"],
                "fields": {},
                "edited_content": template["content"],
                "enabled": True,
            }
        ),
    )


def dispatch(system, flow, actor, project, gateway=None, contract_version=None):
    gateway = gateway or PreparationGateway()
    worker = Worker(system["repo"], gateway, system["catalog"])
    with system["repo"].sessions.begin() as db:
        schedules = db.scalars(
            select(Operation).where(Operation.type == "template_autofill", Operation.state == "queued")
        ).all()
        ids = [item.id for item in schedules]
        for identity in ids:
            db.scalar(select(Job).where(Job.operation_id == identity)).available_at = utcnow() - timedelta(
                seconds=1
            )
    for identity in ids:
        assert worker.tick(identity)
    with system["repo"].sessions() as db:
        jobs = db.scalars(
            select(Operation).where(Operation.type == "template_preparation", Operation.state == "queued")
        ).all()
        ids = [item.id for item in jobs]
    for identity in ids:
        if contract_version:
            snapshot_contract(system["repo"], identity, contract_version)
        assert worker.tick(identity)
    return flow.get(actor, project["id"]), gateway


def review(flow, actor, project, confirm_empty=False):
    current = flow.get(actor, project["id"])
    return flow.review(
        actor,
        project["id"],
        "review-" + str(current["version"]),
        {"expected_draft_revision": current["version"], "confirm_empty": confirm_empty},
    )


def send_request(value, project):
    return {
        "expected_draft_revision": value["version"],
        "base_project_revision": project["current_revision_id"],
        "policy_version": "2026-10-v1",
        "reply_to": None,
        "review_snapshot_hash": value["data"].get("review_snapshot", {}).get("hash", "0" * 64),
    }


def test_idea_cannot_jump_to_review_or_generation(system, workflow):
    flow, actor, project = workflow
    current = save(
        flow, actor, project, lambda data: data.update(stage="review", message_text="Editar a home")
    )
    assert current["data"]["stage"] == "idea"
    with pytest.raises(RuleError) as caught:
        flow.review(
            actor,
            project["id"],
            "forged",
            {"expected_draft_revision": current["version"], "confirm_empty": True},
        )
    assert caught.value.code == "TEMPLATES_STAGE_INCOMPLETE"
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(Operation)) == 0


def test_recommendation_does_not_select_or_fill_and_empty_requires_confirmation(system, workflow):
    flow, actor, project = workflow
    current = prepare(flow, actor, project)
    assert current["data"]["stage"] == "templates" and current["data"]["attachments"] == []
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(Operation)) == 0
        assert db.scalar(select(func.count()).select_from(Reservation)) == 0
    with pytest.raises(RuleError) as caught:
        review(flow, actor, project)
    assert caught.value.code == "EMPTY_SELECTION_UNCONFIRMED"
    accepted = review(flow, actor, project, True)
    assert accepted["data"]["can_build"]
    assert accepted["data"]["review_snapshot"]["empty_selection_confirmed"]


def test_selection_queues_automatic_fill_and_exact_review_snapshot(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    selected = select_template(flow, actor, project)
    assert selected["data"]["attachments"][0]["fill_state"] == "queued"
    current, gateway = dispatch(system, flow, actor, project)
    attachment = current["data"]["attachments"][0]
    assert len(gateway.calls) == 1
    assert attachment["fill_state"] == "ready", attachment
    assert not attachment["validation_errors"] and "{{" not in attachment["edited_content"]
    assert all(not field["edited_by_user"] for field in attachment["field_values"].values())
    approved = review(flow, actor, project)
    snapshot = approved["data"]["review_snapshot"]
    assert snapshot["hash"] == canonical_hash(
        {key: value for key, value in snapshot.items() if key != "hash"}
    )
    request = send_request(approved, project)
    operation = flow.send(
        actor, project["id"], "send", request, GenerationService(system["repo"], system["catalog"])
    )
    assert (
        flow.send(
            actor, project["id"], "send", request, GenerationService(system["repo"], system["catalog"])
        )["id"]
        == operation["id"]
    )
    with system["repo"].sessions() as db:
        persisted = db.get(Operation, operation["id"])
        assert persisted.payload["attachment_snapshots"] == snapshot["attachments"]
        assert persisted.payload["review_snapshot_hash"] == snapshot["hash"]
    next_request = flow.get(actor, project["id"])
    assert next_request["data"]["stage"] == "conversation" and not next_request["data"]["message_text"]
    assert not next_request["data"]["can_build"]


def test_rapid_selections_coalesce_and_fill_only_selected(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project, "sdd")
    select_template(flow, actor, project, "criterios-aceite")
    with system["repo"].sessions() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(Operation).where(Operation.type == "template_autofill")
            )
            == 1
        )
    current, gateway = dispatch(system, flow, actor, project)
    assert len(gateway.calls) == 1
    assert {item["template_id"] for item in current["data"]["attachments"]} == {"sdd", "criterios-aceite"}
    assert all(item["fill_state"] == "ready" for item in current["data"]["attachments"])


def test_remove_before_dispatch_does_not_call_provider(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project)
    save(flow, actor, project, lambda data: data["attachments"][0].update(enabled=False))
    current, gateway = dispatch(system, flow, actor, project)
    assert not gateway.calls
    assert not current["data"]["attachments"][0]["enabled"]


def test_late_response_preserves_human_field_and_remaining_fields(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    selected = select_template(flow, actor, project)
    template = flow.templates.version(actor, selected["data"]["attachments"][0]["source_version_id"])
    key = template["fields"][0]["key"]

    def edit():
        save(
            flow,
            actor,
            project,
            lambda data: data["attachments"][0]["fields"].update({key: "Decisão humana preservada"}),
        )

    current, gateway = dispatch(system, flow, actor, project, PreparationGateway(callback=edit))
    item = current["data"]["attachments"][0]
    assert item["fields"][key] == "Decisão humana preservada"
    assert item["field_values"][key]["edited_by_user"]
    assert all(value for field, value in item["fields"].items() if field != key)
    assert len(gateway.calls) == 1


def test_remove_during_call_never_reappears(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project)

    def remove():
        save(flow, actor, project, lambda data: data["attachments"][0].update(enabled=False))

    current, gateway = dispatch(system, flow, actor, project, PreparationGateway(callback=remove))
    assert len(gateway.calls) == 1 and not current["data"]["attachments"][0]["enabled"]
    assert current["data"]["attachments"][0]["fields"] == {}
    approved = review(flow, actor, project, True)
    assert approved["data"]["review_snapshot"]["attachments"] == []


def test_uncertain_result_blocks_retry_and_preserves_reservation(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project)
    current, gateway = dispatch(system, flow, actor, project, PreparationGateway(uncertain=True))
    assert current["data"]["attachments"][0]["fill_state"] == "reconciling"
    with pytest.raises(RuleError) as caught:
        flow.retry_fill(
            actor,
            project["id"],
            "retry",
            {"expected_draft_revision": current["version"], "attachment_ids": ["sdd"]},
        )
    assert caught.value.code == "FILL_PENDING"
    assert len(gateway.calls) == 1


def test_snapshot_stale_after_idea_change_and_human_edit_is_preserved(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project)
    dispatch(system, flow, actor, project)
    approved = review(flow, actor, project)
    save(flow, actor, project, lambda data: data.update(message_text="Novo pedido com outro objetivo"))
    with pytest.raises(RuleError) as caught:
        flow.send(
            actor,
            project["id"],
            "stale",
            send_request(approved, project),
            GenerationService(system["repo"], system["catalog"]),
        )
    assert caught.value.code == "DRAFT_CONFLICT"
    current = flow.get(actor, project["id"])
    assert current["data"]["stage"] == "idea" and not current["data"]["can_build"]


def test_context_revision_change_blocks_old_review(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    approved = review(flow, actor, project, True)
    repo = system["repo"]
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    files["public/updated.txt"] = "Outra revisão"
    repo.save_revision(
        actor,
        project["id"],
        "revision",
        {"base_revision_id": project["current_revision_id"], "files": files},
        project["version"],
    )
    with pytest.raises(RuleError) as caught:
        flow.send(
            actor,
            project["id"],
            "stale-context",
            send_request(approved, project),
            GenerationService(repo, system["catalog"]),
        )
    assert caught.value.code == "CONTEXT_STALE"


def test_new_empty_project_context_is_explicit(system, workflow):
    flow, actor, project = workflow
    with system["repo"].sessions.begin() as db:
        db.get(Project, project["id"]).current_revision_id = None
    project["current_revision_id"] = None
    current = prepare(flow, actor, project)
    assert current["data"]["context_empty"] and current["data"]["context_revision"] is None
    select_template(flow, actor, project)
    current, gateway = dispatch(system, flow, actor, project)
    assert current["data"]["attachments"][0]["fill_state"] == "ready"
    assert len(gateway.calls) == 1


def test_cross_account_and_reused_key_cannot_override_draft(system, workflow):
    flow, actor, project = workflow
    current = prepare(flow, actor, project)
    with pytest.raises(RuleError) as caught:
        flow.review(
            system["users"][1],
            project["id"],
            "foreign",
            {"expected_draft_revision": current["version"], "confirm_empty": True},
        )
    assert caught.value.status == 404
    request = {"expected_draft_revision": current["version"], "confirm_empty": True}
    flow.review(actor, project["id"], "one-key", request)
    with pytest.raises(RuleError) as caught:
        flow.review(actor, project["id"], "one-key", {**request, "confirm_empty": False})
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"


def test_returning_to_unchanged_idea_reuses_preparation_and_advances(workflow):
    flow, actor, project = workflow
    original = prepare(flow, actor, project)
    current = save(flow, actor, project, lambda data: data.update(stage="idea"))
    result = Recommendations(flow).recommend(
        actor,
        project["id"],
        "prepare-again",
        {
            "expected_draft_revision": current["version"],
            "base_project_revision": project["current_revision_id"],
        },
    )
    refreshed = flow.get(actor, project["id"])
    assert result["recommendation_id"] == original["data"]["recommendation_id"]
    assert refreshed["data"]["stage"] == "templates"
    assert refreshed["data"]["attachments"] == original["data"]["attachments"]


def test_preparation_refuses_a_revision_different_from_the_displayed_context(workflow):
    flow, actor, project = workflow
    current = save(flow, actor, project, lambda data: data.update(message_text="Melhorar a tela inicial"))
    with pytest.raises(RuleError) as caught:
        Recommendations(flow).recommend(
            actor,
            project["id"],
            "stale-visible",
            {
                "expected_draft_revision": current["version"],
                "base_project_revision": None,
            },
        )
    assert caught.value.code == "CONTEXT_STALE"
    assert flow.get(actor, project["id"])["data"]["stage"] == "idea"


def test_legacy_generation_routes_require_workflow_without_reservation(system, workflow):
    from fastapi.testclient import TestClient
    from viracase.interfaces.api import create_app

    _, actor, project = workflow
    repo = system["repo"]
    before = repo.wallet(actor, True)
    with TestClient(
        create_app(dependencies=(system["config"], system["engine"], repo, system["catalog"], None))
    ) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"login": "student0", "password": "test-student-password"},
            headers={"Origin": system["config"].public_origin},
        )
        headers = {"Origin": system["config"].public_origin, "X-CSRF-Token": response.json()["csrf_token"]}
        for route in ("generations", "conversation"):
            response = client.post(
                f"/api/v1/projects/{project['id']}/{route}",
                headers={**headers, "Idempotency-Key": route},
                json={
                    "brief": "Alterar a página inicial",
                    "base_revision_id": project["current_revision_id"],
                    "policy_version": "2026-10-v1",
                },
            )
            assert response.status_code == 409 and response.json()["error"]["code"] == "WORKFLOW_REQUIRED"
    assert repo.wallet(actor, True) == before
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Operation)) == 0


def test_selection_and_review_are_traced_without_enabling_payload_capture(system, workflow):
    from viracase.infrastructure.models import IntegrationTrace, IntegrationTracePayload

    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project)
    dispatch(system, flow, actor, project)
    approved = review(flow, actor, project)
    with system["repo"].sessions() as db:
        events = db.scalars(
            select(IntegrationTrace).where(IntegrationTrace.project_id == project["id"])
        ).all()
        statuses = {item.status for item in events}
        assert {"selection_changed", "review_ready", "applied"} <= statuses
        snapshot = next(item for item in events if item.status == "review_ready")
        assert snapshot.details["snapshot_id"] == approved["data"]["review_snapshot"]["hash"]
        assert all(item.capture_state == "disabled" for item in events)
        assert db.scalar(select(func.count()).select_from(IntegrationTracePayload)) == 0


def test_answer_to_essential_question_continues_automatic_preparation(system, workflow):
    from viracase.infrastructure.template_preparations import TemplatePreparations

    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project)
    current, gateway = dispatch(
        system, flow, actor, project, PreparationGateway(question=True), contract_version=3
    )
    attachment = current["data"]["attachments"][0]
    assert attachment["fill_state"] == "needs_input"
    service = TemplatePreparations(system["repo"], flow.learning, system["catalog"])
    prior = service.get(actor, project["id"], attachment["preparation_id"])
    answer = "A pessoa deve conseguir organizar suas tarefas com critérios claros."
    followup = service.answer(
        actor,
        project["id"],
        prior["id"],
        "essential-answer",
        {
            "expected_state_version": prior["state_version"],
            "expected_draft_revision": current["version"],
            "answers": [
                {"question_id": question["id"], "value": answer}
                for item in prior["items"]
                for question in item["questions"]
            ],
        },
    )
    gateway.question = False
    assert Worker(system["repo"], gateway, system["catalog"]).tick(followup["id"])
    filled = flow.get(actor, project["id"])["data"]["attachments"][0]
    assert filled["fill_state"] == "ready"
    assert set(filled["fields"].values()) == {answer}
    assert len(gateway.calls) == 2
