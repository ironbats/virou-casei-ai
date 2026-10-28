from copy import deepcopy
from datetime import timedelta

import pytest
from test_publication_diagnostics import diagnostics_client
from test_publication_pipeline import publication as publication_fixture
from test_publication_pipeline import qualify

from viracase.domain.rules import RuleError, canonical_hash, utcnow
from viracase.infrastructure.models import Project
from viracase.infrastructure.observability import heartbeat
from viracase.infrastructure.publication_check import CHECKS
from viracase.infrastructure.publication_qualification import qualified
from viracase.infrastructure.publication_verification import VERIFICATION_FORMAT_VERSION

HONO = "hono-typescript-d1"
PYTHON = "fastapi-python-d1"


@pytest.fixture
def publication(system, monkeypatch):
    return publication_fixture.__wrapped__(system, monkeypatch)


def partial_verification(system, registry):
    with system["repo"].sessions.begin() as db:
        row = registry.latest(db, "cloudflare")
        row.settings = {**row.settings, "allowed_stacks": [HONO, PYTHON]}
        report = {
            "verification_format_version": VERIFICATION_FORMAT_VERSION,
            "configuration_id": row.id,
            "configuration_digest": canonical_hash(row.settings),
            "executor_digest": "f" * 64,
            "stacks": [HONO],
            "checks": {key: True for key in CHECKS},
            "tested_at": utcnow().isoformat(),
            "expires_at": (utcnow() + timedelta(hours=1)).isoformat(),
        }
        row.validation = {
            **row.validation,
            "publication_check_blocked": False,
            "publication_check_blocked_stacks": [PYTHON],
            "runtime_verification": {"report": report, "digest": canonical_hash(report)},
        }
    heartbeat(system["repo"], "worker:stack-test", {
        "available": True, "fingerprint": "f" * 64, "stacks": [HONO, PYTHON],
    })


def test_partial_verification_allows_only_the_approved_project_stack(system, publication):
    registry, service, actor, project = publication
    partial_verification(system, registry)
    assert service.readiness(actor, project["id"])["can_publish"]
    assert service.capability()["enabled"]
    with system["repo"].sessions.begin() as db:
        db.get(Project, project["id"]).stack = PYTHON
    readiness = service.readiness(actor, project["id"])
    assert not readiness["can_publish"]
    assert [reason["code"] for reason in readiness["reasons"]] == ["CAPABILITY_UNVALIDATED"]
    with pytest.raises(RuleError) as caught:
        service.start(actor, project["id"], "blocked-python", {"revision_id": project["current_revision_id"]})
    assert caught.value.code == "CAPABILITY_UNVALIDATED"


def test_partial_stack_diagnostic_matches_the_project_gate_and_is_admin_only(system, publication):
    registry, _service, actor, _project = publication
    partial_verification(system, registry)
    config = registry.get("cloudflare")
    path = "/admin/integration-connections/" + config["connection_id"] + "/publication-diagnostics"
    with diagnostics_client(system, system["admin"]) as client:
        response = client.get(path)
    assert response.status_code == 200
    data = response.json()
    assert data["can_publish"] and data["can_verify"]
    assert data["stack_readiness"] == [
        {"stack": HONO, "can_publish": True, "reasons": []},
        {"stack": PYTHON, "can_publish": False, "reasons": [{
            "code": "CAPABILITY_UNVALIDATED",
            "message": "A publicação aguarda a verificação operacional na conexão Cloudflare do backoffice.",
        }]},
    ]
    with diagnostics_client(system, actor) as client:
        rejected = client.get(path)
    assert rejected.status_code == 403
    assert "stack_readiness" not in rejected.text


def test_connection_revalidation_preserves_failed_stack_block(system, publication):
    registry, service, actor, project = publication
    partial_verification(system, registry)
    original = registry.get("cloudflare")["validation"]["runtime_verification"]
    registry.validate(system["admin"], "cloudflare", 1, "Conferir conectividade sem liberar perfil reprovado")
    saved = registry.get("cloudflare")["validation"]
    assert saved["publication_check_blocked_stacks"] == [PYTHON]
    assert saved["runtime_verification"] == original
    assert service.readiness(actor, project["id"])["can_publish"]
    with system["repo"].sessions() as db:
        assert not qualified(registry.latest(db, "cloudflare"), PYTHON, "f" * 64)


def test_failed_stack_cannot_fall_back_to_an_older_manual_qualification(system, publication):
    registry, _service, _actor, _project = publication
    partial_verification(system, registry)
    qualify(system, registry)
    with system["repo"].sessions.begin() as db:
        row = registry.latest(db, "cloudflare")
        validation = deepcopy(row.validation)
        manual = validation["qualification"]["report"]
        manual["stacks"] = [HONO, PYTHON]
        validation["qualification"]["digest"] = canonical_hash(manual)
        row.validation = validation
    with system["repo"].sessions() as db:
        row = registry.latest(db, "cloudflare")
        assert qualified(row, HONO, "f" * 64)
        assert qualified(row, None, "f" * 64)
        assert not qualified(row, PYTHON, "f" * 64)


def test_manual_qualification_for_only_a_blocked_profile_does_not_release_global_capability(system, publication):
    registry, service, actor, project = publication
    partial_verification(system, registry)
    qualify(system, registry)
    with system["repo"].sessions.begin() as db:
        row = registry.latest(db, "cloudflare")
        validation = deepcopy(row.validation)
        manual = validation["qualification"]["report"]
        manual["stacks"] = [PYTHON]
        validation["qualification"]["digest"] = canonical_hash(manual)
        runtime = validation["runtime_verification"]["report"]
        runtime["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
        validation["runtime_verification"]["digest"] = canonical_hash(runtime)
        row.validation = validation
    assert not service.capability()["enabled"]
    assert not service.readiness(actor, project["id"])["can_publish"]


def test_valid_manual_qualification_keeps_its_previous_behavior(system, publication):
    registry, service, actor, project = publication
    qualify(system, registry)
    assert service.capability()["enabled"]
    assert service.readiness(actor, project["id"])["can_publish"]


def test_partial_verification_does_not_bypass_project_ownership(system, publication):
    registry, service, actor, project = publication
    partial_verification(system, registry)
    with pytest.raises(RuleError):
        service.start(system["users"][1], project["id"], "foreign-project", {
            "revision_id": project["current_revision_id"],
        })
    assert service.readiness(actor, project["id"])["can_publish"]
