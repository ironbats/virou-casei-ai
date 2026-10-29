import pytest
from sqlalchemy import select
from test_build_workflow import dispatch, prepare, save, select_template, workflow
from test_field_generation_safety import (
    NEXT_OWNER_PLAN,
    TARGET_RESPONSE,
    ProposalGateway,
    attachment,
    field_case,
    start_field,
)
from test_template_preparations import PreparationGateway

from viracase.infrastructure.models import Operation
from viracase.infrastructure.template_preparations import TemplatePreparations

__all__ = ["field_case", "workflow"]


def test_reselecting_an_attachment_discards_the_old_target_grant_and_can_generate_again(system, field_case):
    flow, actor, project = field_case["workflow"]
    queued = start_field(field_case)
    previous_instance = attachment(queued)["attachment_instance_id"]
    save(flow, actor, project, lambda data: data["attachments"][0].update(enabled=False))
    selected = save(flow, actor, project, lambda data: data["attachments"][0].update(enabled=True))
    assert attachment(selected)["attachment_instance_id"] != previous_instance
    current, gateway = dispatch(system, flow, actor, project, ProposalGateway(TARGET_RESPONSE))
    assert gateway.calls == []
    assert attachment(current)["fields"].get("owner_plan") is None
    assert attachment(current)["fill_state"] == "needs_input"
    assert not any(
        value["state"] in {"queued", "filling", "reconciling"}
        for value in attachment(current).get("field_generations", {}).values()
    )
    start_field(field_case, key="generate-after-reselect")
    final, gateway = dispatch(system, flow, actor, project, ProposalGateway(TARGET_RESPONSE))
    assert len(gateway.calls) == 1
    assert attachment(final)["fields"]["owner_plan"] == NEXT_OWNER_PLAN


def test_new_project_revision_before_target_dispatch_cancels_without_a_provider_call(system, field_case):
    flow, actor, project = field_case["workflow"]
    queued = start_field(field_case)
    schedule_id = attachment(queued)["field_generations"]["owner_plan"]["operation_id"]
    files = system["repo"].source_files(actor, project["id"], project["current_revision_id"])
    files["public/revision-note.txt"] = "Contexto sintético atualizado antes da geração focal."
    system["repo"].save_revision(
        actor,
        project["id"],
        "revision-before-field-dispatch",
        {"base_revision_id": project["current_revision_id"], "files": files},
        project["version"],
    )
    current, gateway = dispatch(system, flow, actor, project, ProposalGateway(TARGET_RESPONSE))
    assert gateway.calls == []
    assert not current["data"]["can_review"] and not current["data"]["can_build"]
    assert attachment(current)["fields"].get("owner_plan") is None
    assert attachment(current)["field_generations"]["owner_plan"]["state"] == "stale"
    with system["repo"].sessions() as db:
        assert db.get(Operation, schedule_id).state == "cancelled"
        preparations = db.scalars(select(Operation.id).where(Operation.type == "template_preparation")).all()
        assert set(preparations) == field_case["prior_preparations"]


def test_reselection_after_reservation_cannot_bind_the_old_child_to_the_new_instance(
    system, field_case, monkeypatch
):
    flow, actor, project = field_case["workflow"]
    queued = start_field(field_case)
    previous_instance = attachment(queued)["attachment_instance_id"]
    consumed = system["repo"].wallet(actor, True)["consumed_brl"]
    start = TemplatePreparations.start
    reserved = []

    def reserve_then_reselect(self, account, pid, key, request, **kwargs):
        result = start(self, account, pid, key, request, **kwargs)
        if request.get("target_field_id") and not reserved:
            reserved.append(result["id"])
            save(flow, actor, project, lambda data: data["attachments"][0].update(enabled=False))
            save(flow, actor, project, lambda data: data["attachments"][0].update(enabled=True))
        return result

    monkeypatch.setattr(TemplatePreparations, "start", reserve_then_reselect)
    current, gateway = dispatch(system, flow, actor, project, ProposalGateway(TARGET_RESPONSE))
    assert gateway.calls == [] and len(reserved) == 1
    assert attachment(current)["attachment_instance_id"] != previous_instance
    assert attachment(current).get("preparation_id") != reserved[0]
    assert not attachment(current).get("field_generations")
    current, gateway = dispatch(system, flow, actor, project, ProposalGateway(TARGET_RESPONSE))
    assert gateway.calls == []
    assert attachment(current)["fill_state"] == "needs_input"
    assert system["repo"].wallet(actor, True)["consumed_brl"] == consumed
    start_field(field_case, key="generate-after-reservation-race")
    final, gateway = dispatch(system, flow, actor, project, ProposalGateway(TARGET_RESPONSE))
    assert len(gateway.calls) == 1
    assert attachment(final)["fields"]["owner_plan"] == NEXT_OWNER_PLAN


@pytest.mark.parametrize("timing", ["before", "during"])
def test_explicit_target_fills_without_replacing_custom_content_or_a_sibling(system, field_case, timing):
    flow, actor, project = field_case["workflow"]
    manual = "Proposta revisada manualmente: consultar apenas os próprios pedidos autenticados."
    custom = "Conteúdo personalizado deste pedido. Preservar as decisões manuais e conferir os campos antes de construir."

    def edit_content():
        def change(data):
            item = data["attachments"][0]
            item["fields"]["ui_note"] = manual
            item["edited_content"] = custom

        return save(flow, actor, project, change)

    if timing == "before":
        edit_content()
    queued = start_field(field_case)
    assert attachment(queued)["fields"].get("owner_plan") is None
    final, gateway = dispatch(
        system,
        flow,
        actor,
        project,
        ProposalGateway(TARGET_RESPONSE, edit_content if timing == "during" else None),
    )
    item = attachment(final)
    assert len(gateway.calls) == 1
    assert item["fields"]["owner_plan"] == NEXT_OWNER_PLAN
    assert item["fields"]["ui_note"] == manual
    assert item["edited_content"] == custom
    assert item["field_values"]["ui_note"]["edited_by_user"] is True
    assert item["field_values"]["owner_plan"]["source_type"] == "policy_suggestion"
    assert item["field_generations"]["owner_plan"]["state"] == "succeeded"
    assert not item["validation_errors"] and final["data"]["can_review"]
    assert not final["data"]["can_build"]


def test_batch_autofill_still_preserves_custom_content_without_a_field_grant(system, workflow):
    flow, actor, project = workflow
    prepare(flow, actor, project)
    select_template(flow, actor, project)
    custom = "Decisão manual de conteúdo. Revisar individualmente os campos antes de incluir qualquer texto."
    save(flow, actor, project, lambda data: data["attachments"][0].update(edited_content=custom))
    final, gateway = dispatch(system, flow, actor, project, PreparationGateway())
    assert len(gateway.calls) == 1
    item = final["data"]["attachments"][0]
    assert item["fields"] == {}
    assert item["edited_content"] == custom
    assert not final["data"]["can_review"] and not final["data"]["can_build"]
