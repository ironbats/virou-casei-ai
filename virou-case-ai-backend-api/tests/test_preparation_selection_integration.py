import copy

import pytest
from sqlalchemy import func, select
from test_bakery_autofill_integration import (
    BAKERY_REQUEST,
    OWNER_ANSWER,
    VALUES,
    legacy_selection,
    managed_bakery_gateway,
)
from test_build_workflow import dispatch, review, save, select_template, workflow

from viracase.domain.rules import cost
from viracase.infrastructure.feature_policies import FeaturePolicies
from viracase.infrastructure.models import Attempt, Ledger, Operation, Reservation
from viracase.infrastructure.recommendations import Recommendations

__all__ = ["legacy_selection", "workflow"]


@pytest.mark.parametrize("auth_mode", ["api_key", "service_account"])
@pytest.mark.parametrize("fault", ["unknown_unit", "outside_range", "forbidden_proposal", "omitted_field"])
def test_selection_repair_reuses_catalog_and_only_retries_pending_fields(
    system, workflow, auth_mode, fault, legacy_selection
):
    flow, actor, project = workflow
    policies = FeaturePolicies(system["repo"], system["catalog"])
    current_policy = policies.current("template_preparation")
    policies.save(
        system["admin"],
        "template_preparation",
        "selection-one-repair",
        current_policy["version"],
        {
            "settings": {**current_policy["settings"], "max_repairs": 1},
            "reason": "Verificar o reparo limitado e a cobrança única do contrato de seleção",
        },
    )
    observations = []

    def transform(result, prompt, body):
        observations.append(
            (copy.deepcopy(prompt), copy.deepcopy(body["generationConfig"]["responseSchema"]))
        )
        if len(observations) != 1:
            return result
        fields = result["items"]["autorizacao"]
        if fault == "unknown_unit":
            fields["field_1"] = {"extracts": [{"unit": 999999, "start": 0, "end": 0}]}
        elif fault == "outside_range":
            fields["field_1"]["extracts"][0]["start"] = -1
        elif fault == "forbidden_proposal":
            fields["field_1"] = {"proposal": "Administrador global"}
        else:
            del fields["field_1"]
        return result

    gateway, requests = managed_bakery_gateway(system, auth_mode, transform=transform)
    current = save(
        flow,
        actor,
        project,
        lambda data: data.update(
            objective="Melhorar a loja da padaria", message_text=BAKERY_REQUEST + " " + OWNER_ANSWER
        ),
    )
    Recommendations(flow).recommend(
        actor,
        project["id"],
        "selection-repair-prepare",
        {"expected_draft_revision": current["version"]},
    )
    select_template(flow, actor, project, "autorizacao")
    current, _ = dispatch(system, flow, actor, project, gateway)
    attachment = current["data"]["attachments"][0]
    assert attachment["fill_state"] == "ready", attachment
    assert attachment["fill_error_code"] is None
    assert attachment["fields"] == VALUES
    assert attachment["validation_errors"] == []
    assert len(requests) == len(observations) == 2
    first, second = [item[0] for item in observations]
    assert first["selection_catalog"] == second["selection_catalog"]
    expected_pending = set(VALUES) if fault == "omitted_field" else {"field_1"}
    assert {field["key"] for field in second["templates"][0]["fields"]} == expected_pending
    second_fields = observations[1][1]["properties"]["items"]["properties"]["autorizacao"]
    assert set(second_fields["required"]) == expected_pending
    assert set(second_fields["properties"]) == expected_pending
    assert second["validation_feedback"]
    assert review(flow, actor, project)["data"]["can_build"]
    with system["repo"].sessions() as db:
        operations = db.scalars(select(Operation).where(Operation.type == "template_preparation")).all()
        assert len(operations) == 1
        operation = operations[0]
        assert operation.state == "succeeded" and operation.billing_status == "settled"
        assert operation.payload["selection_schema"] == operation.policy["preparation_selection_schema"]
        assert set(
            operation.policy["preparation_selection_schema"]["properties"]["items"]["properties"][
                "autorizacao"
            ]["required"]
        ) == set(VALUES)
        assert (
            db.scalar(
                select(func.count()).select_from(Reservation).where(Reservation.operation_id == operation.id)
            )
            == 1
        )
        debits = db.scalars(
            select(Ledger).where(Ledger.operation_id == operation.id, Ledger.kind == "debit")
        ).all()
        assert len(debits) == 1
        assert debits[0].amount == cost(operation.policy, {"input": 400, "cached": 0, "output": 200})
        attempts = db.scalars(select(Attempt).where(Attempt.operation_id == operation.id)).all()
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt.state == "confirmed"
        assert attempt.usage["input"] == 400
        assert attempt.usage["output"] == 200
        assert len(attempt.usage["calls"]) == 2
        assert all(
            call["usage"] == {"input": 200, "cached": 0, "output": 100}
            for call in attempt.usage["calls"]
        )
        assert (
            db.scalar(select(func.count()).select_from(Operation).where(Operation.type == "generation")) == 0
        )
