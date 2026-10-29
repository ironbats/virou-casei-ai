from copy import deepcopy

import pytest
from test_publication_resolution_http import clients as clients_fixture
from test_publication_resolution_http import recovery_case as recovery_case_fixture

from viracase.domain.rules import utcnow
from viracase.infrastructure.models import Operation
from viracase.infrastructure.repository_dto import operation_dto

PRIVATE_RESULT = {
    "publication_resolution": {
        "plan": {"binding": {"database_id": "ADMIN_DATABASE_BINDING_SENTINEL", "configuration_sha256": "ADMIN_CONFIGURATION_SENTINEL"}},
        "attempt": {"attestation": {"reference": "ADMIN_ATTESTATION_SENTINEL"}},
    },
    "publication_recovery_assessment": {
        "classification": "unknown",
        "identity": {"database_id": "ADMIN_ASSESSMENT_DATABASE_SENTINEL"},
        "pending_update": {"provider": {"reference": "ADMIN_PENDING_UPDATE_SENTINEL"}},
    },
}
PUBLIC_RESULT = {
    "publication_recovery_request": {"requested_at": "2026-10-29T12:00:00+00:00"},
    "publication_recovery": {"version": 2, "state": "attention", "last_error_code": "MIGRATION_RESULT_UNKNOWN"},
    "publication_diagnostic": {"version": 1, "phase": "sql_sending", "effect_status": "unknown"},
    "publication_failure": {"code": "PUBLICATION_REJECTED", "stage": "publication_migrating", "message": "Falha original preservada."},
    "existing_extension": {"value": "OWNER_PUBLIC_RESULT_SENTINEL"},
}


@pytest.fixture
def recovery_case(system, monkeypatch, request):
    yield from recovery_case_fixture.__wrapped__(system, monkeypatch, request)


@pytest.fixture
def clients(system):
    yield from clients_fixture.__wrapped__(system)


def _operation(kind="publication"):
    now = utcnow()
    return Operation(
        id="1b0ae37e-a43c-469e-947d-184b17e9736f",
        account_id="6b535d04-00f1-4c17-84cd-e7653503dece",
        project_id="fbf2ea89-13d0-4e09-88c1-767742bb75ba",
        type=kind, state="reconciling", progress_stage="publication_reconciling",
        billing_status="not_applicable", cancel_requested=False,
        payload={"revision_id": "cd89361a-afc6-48dc-9c0a-3b580220cfb2"},
        policy={"model": "existing-model-label"}, result=deepcopy(PUBLIC_RESULT),
        error_code="PUBLICATION_REJECTED", created_at=now, updated_at=now,
    )


def test_public_projection_excludes_only_new_administrative_fields_without_mutating_persistence():
    operation = _operation()
    before = operation_dto(operation)
    operation.result = {**operation.result, **deepcopy(PRIVATE_RESULT)}
    persisted = deepcopy(operation.result)
    assert operation_dto(operation) == before
    assert operation.result == persisted
    public = operation_dto(operation)["result_ref"]
    for field, value in PUBLIC_RESULT.items():
        assert public[field] == value
    assert public["revision_id"] == operation.payload["revision_id"]


def test_administrative_projection_retains_complete_resolution_and_assessment():
    operation = _operation()
    operation.result = {**operation.result, **deepcopy(PRIVATE_RESULT)}
    result = operation_dto(operation, admin=True)
    for field, value in {**PUBLIC_RESULT, **PRIVATE_RESULT}.items():
        assert result["result_ref"][field] == value
    assert result["model_label"] == "existing-model-label"


@pytest.mark.parametrize("kind", ["generation", "export", "preview", "publication_check"])
def test_other_operation_types_keep_their_existing_result_projection(kind):
    operation = _operation(kind)
    operation.result = {**operation.result, **deepcopy(PRIVATE_RESULT)}
    assert operation_dto(operation)["result_ref"] == operation.result


def test_owner_status_and_operation_lists_hide_admin_metadata_while_admin_listing_retains_it(recovery_case, clients):
    case, private = recovery_case, deepcopy(PRIVATE_RESULT)
    oid, project_id = case["operation"]["id"], case["project"]["id"]
    with case["repo"].sessions.begin() as db:
        row = db.get(Operation, oid)
        row.result = {**row.result, **deepcopy(PUBLIC_RESULT), **private}
        db.flush()
        persisted = deepcopy(row.result)
        expected = operation_dto(row)
    owner, _ = clients["owner"]
    for path in (
        f"/api/v1/operations/{oid}",
        f"/api/v1/operations?project_id={project_id}",
        f"/api/v1/operations?project_id={project_id}&kind=publication&latest=true",
        f"/api/v1/usage?project_id={project_id}",
    ):
        response = owner.get(path)
        assert response.status_code == 200, response.text
        payload = response.json()
        operation = next(item for item in payload["items"] if item["id"] == oid) if "items" in payload else payload
        assert operation == expected
        assert "publication_resolution" not in response.text
        assert "publication_recovery_assessment" not in response.text
        assert "ADMIN_" not in response.text
        assert "OWNER_PUBLIC_RESULT_SENTINEL" in response.text
    admin, _ = clients["admin"]
    response = admin.get("/admin-api/v1/operations")
    assert response.status_code == 200, response.text
    operation = next(item for item in response.json()["items"] if item["id"] == oid)
    for field, value in private.items():
        assert operation["result_ref"][field] == value
    with case["repo"].sessions() as db:
        assert db.get(Operation, oid).result == persisted
    assert case["remote"].calls == []
