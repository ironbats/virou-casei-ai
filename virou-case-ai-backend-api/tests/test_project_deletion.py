import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from viracase.domain.rules import RuleError
from viracase.infrastructure.drafts import Drafts
from viracase.infrastructure.models import Audit, Job, Operation, Project, Revision
from viracase.infrastructure.project_deletion import request_deletion
from viracase.infrastructure.worker import Worker
from viracase.interfaces.api import create_app


def project(system, actor=None, key="p"):
    return system["repo"].create_project(
        actor or system["users"][0],
        key,
        {"name": "Projeto para excluir", "objective": "Aplicação de tarefas da equipe"},
    )


def test_deletion_is_owned_versioned_idempotent_and_releases_quota_only_after_cleanup(system):
    repo, actor = system["repo"], system["users"][0]
    p, other = project(system), project(system, key="p2")
    before = repo.wallet(actor, True)
    draft = Drafts(repo, None)
    draft.save(
        actor,
        "project",
        p["id"],
        "draft",
        {
            "base_revision_id": p["current_revision_id"],
            "files": None,
            "prompt": "private-text",
            "provenance": [],
        },
        0,
    )
    exported = repo.create_operation(
        actor, p["id"], "export", "export", {"revision_id": p["current_revision_id"]}
    )
    Worker(repo, None, None).tick(exported["id"])
    with pytest.raises(RuleError) as forbidden:
        request_deletion(repo, system["users"][1], p["id"], "delete", p["version"], p["name"])
    assert forbidden.value.status == 404
    with pytest.raises(RuleError) as stale:
        request_deletion(repo, actor, p["id"], "delete", p["version"] - 1, p["name"])
    assert stale.value.status == 412
    with pytest.raises(RuleError):
        request_deletion(repo, actor, p["id"], "delete", p["version"], "wrong")
    result = request_deletion(repo, actor, p["id"], "delete", p["version"], p["name"])
    assert request_deletion(repo, actor, p["id"], "delete", p["version"], p["name"]) == result
    assert repo.projects(actor)["quota"]["used_projects"] == 2
    with pytest.raises(RuleError):
        repo.project(actor, p["id"])
    Worker(repo, None, None).tick(result["id"])
    assert repo.operation(actor, result["id"])["state"] == "succeeded"
    assert repo.projects(actor)["quota"]["used_projects"] == 1
    assert not (repo.sources.root / actor / p["id"]).exists()
    assert repo.project(actor, other["id"])["id"] == other["id"]
    assert repo.wallet(actor, True) == before
    with repo.sessions() as db:
        assert db.get(Project, p["id"]).state == "deleted"
        assert not db.scalar(select(func.count()).select_from(Revision).where(Revision.project_id == p["id"]))
        assert db.scalar(
            select(Audit.id).where(Audit.action == "project:deleted", Audit.resource_id == p["id"])
        )
        assert db.get(Operation, exported["id"]).payload == {}
    with pytest.raises(RuleError):
        repo.download(actor, exported["id"])
    assert project(system, key="replacement")


@pytest.mark.parametrize("state", ["queued", "running", "reconciling"])
def test_inflight_operations_prevent_delete(system, state):
    repo, actor, p = system["repo"], system["users"][0], project(system)
    with repo.sessions.begin() as db:
        db.add(Operation(account_id=actor, project_id=p["id"], type="generation", state=state))
    with pytest.raises(RuleError) as caught:
        request_deletion(repo, actor, p["id"], "delete", p["version"], p["name"])
    assert caught.value.code == "OPERATION_CONFLICT"
    assert repo.project(actor, p["id"])["state"] == "draft"


def test_failed_local_cleanup_can_resume_without_releasing_quota(system, monkeypatch):
    repo, actor, p = system["repo"], system["users"][0], project(system)
    original = repo.sources.remove_project

    def fail(*args):
        raise OSError("synthetic failure")

    monkeypatch.setattr(repo.sources, "remove_project", fail)
    result = request_deletion(repo, actor, p["id"], "delete", p["version"], p["name"])
    Worker(repo, None, None).tick(result["id"])
    assert repo.operation(actor, result["id"])["state"] == "failed"
    pending = repo.projects(actor)["items"][0]
    assert pending["state"] == "deleting"
    monkeypatch.setattr(repo.sources, "remove_project", original)
    retry = request_deletion(repo, actor, p["id"], "retry", pending["version"], p["name"])
    Worker(repo, None, None).tick(retry["id"])
    assert repo.projects(actor)["items"] == []


def test_delete_http_requires_session_csrf_confirmation_and_version(system):
    repo, p = system["repo"], project(system)
    app = create_app(False, (system["config"], system["engine"], repo, system["catalog"], None))
    client = TestClient(app)
    route = "/api/v1/projects/" + p["id"]
    origin = system["config"].public_origin
    assert client.delete(route, headers={"Origin": origin}).status_code == 401
    login = client.post(
        "/api/v1/auth/login",
        json={"login": "student0", "password": "test-student-password"},
        headers={"Origin": origin},
    )
    headers = {"Origin": origin, "Idempotency-Key": "delete", "If-Match": str(p["version"])}
    assert (
        client.request("DELETE", route, json={"confirmation": p["name"]}, headers=headers).status_code == 403
    )
    headers["X-CSRF-Token"] = login.json()["csrf_token"]
    result = client.request("DELETE", route, json={"confirmation": p["name"]}, headers=headers)
    assert result.status_code == 202, result.text
    assert (
        client.request("DELETE", route, json={"confirmation": p["name"]}, headers=headers).json()
        == result.json()
    )
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 1
