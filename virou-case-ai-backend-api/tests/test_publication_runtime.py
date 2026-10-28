from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_publication_pipeline import (
    Builder,
    Remote,
    publication as publication_fixture,
    qualify,
    start,
    execute,
)
from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.models import Job, Operation, PublicationResource
from viracase.infrastructure.observability import heartbeat
from viracase.infrastructure.project_deletion import execute_deletion, request_deletion
from viracase.infrastructure.publication import PublicationService
from viracase.infrastructure.publication_check import execute_check, request_check, resume_check
from viracase.infrastructure.worker import Worker
from viracase.interfaces.api import create_app


@pytest.fixture
def publication(system, monkeypatch):
    return publication_fixture.__wrapped__(system, monkeypatch)


class ProbeBuilder(Builder):
    def verify_isolation(self):
        return {
            k: True
            for k in (
                "no_network",
                "no_host_secrets",
                "no_cross_project_files",
                "read_only_sources",
                "resource_limits",
                "no_nested_userns",
            )
        }


class ProbeRemote(Remote):
    def validate(self):
        return {"ready": True, "subdomain": "synthetic-test", "database_ids": [], "workers_count": 0}

    def query(self, database_id, sql, params=None):
        if sql.startswith("SELECT value"):
            return [{"success": True, "results": [{"value": "verified"}]}]
        return super().query(database_id, sql, params)

    def upload(self, name, database_id, artifact):
        result = super().upload(name, database_id, artifact)
        self.remote["bindings"].append({"type": "assets", "name": "ASSETS"})
        return result


def claim(repo, oid):
    with repo.sessions.begin() as db:
        job = db.scalar(select(Job).where(Job.operation_id == oid))
        job.state, job.lease_token = "running", "lease"
        job.lease_until = utcnow() + timedelta(minutes=5)


def verification(system, publication, key="check"):
    repo, registry = system["repo"], publication[0]
    heartbeat(
        repo, "worker:test", {"available": True, "fingerprint": "f" * 64, "stacks": ["hono-typescript-d1"]}
    )
    cfg = registry.get("cloudflare")
    op = request_check(
        repo, system["admin"], cfg["connection_id"], key, cfg["version"], "Teste operacional controlado"
    )
    claim(repo, op["id"])
    return op


def test_runtime_check_verifies_remote_build_and_retains_resources_before_releasing(publication, system):
    repo = system["repo"]
    registry, service, actor, project = publication
    op = verification(system, publication)
    remote = ProbeRemote()
    execute_check(Worker(repo, None, None), op["id"], "lease", lambda *_: remote, ProbeBuilder())
    assert remote.db is not None and remote.remote is not None
    assert "delete" not in remote.calls and "disable" not in remote.calls
    assert remote.calls.count("create_database") == 1
    assert repo.operation(system["admin"], op["id"])["state"] == "succeeded"
    assert PublicationService(repo).readiness(actor, project["id"])["can_publish"]
    registry.validate(system["admin"], "cloudflare", 1, "Revalidação de credenciais")
    assert registry.get("cloudflare")["state"] == "active"
    assert PublicationService(repo).readiness(actor, project["id"])["can_publish"]


def test_revalidation_preserves_formal_qualification(publication, system):
    registry, service, actor, project = publication
    qualify(system, registry)
    before = registry.get("cloudflare")["validation"]["qualification"]
    registry.validate(system["admin"], "cloudflare", 1, "Atualização da conectividade")
    assert registry.get("cloudflare")["validation"]["qualification"] == before
    assert service.readiness(actor, project["id"])["can_publish"]


def test_repeated_checks_reuse_owned_resources_without_deletion(publication, system):
    repo = system["repo"]
    op = verification(system, publication)
    remote = ProbeRemote()
    execute_check(Worker(repo, None, None), op["id"], "lease", lambda *_: remote, ProbeBuilder())
    first_database = dict(remote.db)
    first_revision = remote.remote["tags"][1]
    second = verification(system, publication, "check-again")
    execute_check(Worker(repo, None, None), second["id"], "lease", lambda *_: remote, ProbeBuilder())
    assert repo.operation(system["admin"], second["id"])["state"] == "succeeded"
    assert remote.db == first_database
    assert remote.remote["tags"][1] != first_revision
    assert remote.calls.count("create_database") == 1
    assert remote.calls.count("upload") == 2
    assert "delete" not in remote.calls and "disable" not in remote.calls


def test_check_restart_after_creation_requires_explicit_resume(publication, system):
    repo = system["repo"]
    op = verification(system, publication)
    with repo.sessions.begin() as db:
        db.get(Operation, op["id"]).result = {"creation_started": True}
        db.scalar(select(Job).where(Job.operation_id == op["id"])).lease_until = utcnow() - timedelta(
            seconds=1
        )
    Worker(repo, None, None).recover()
    assert repo.operation(system["admin"], op["id"])["state"] == "reconciling"


def test_no_worker_or_nonadmin_cannot_request_check(publication, system):
    cfg = publication[0].get("cloudflare")
    for actor, code in [(publication[2], "FORBIDDEN"), (system["admin"], "EXECUTOR_UNAVAILABLE")]:
        with pytest.raises(RuleError) as caught:
            request_check(
                system["repo"], actor, cfg["connection_id"], "check", cfg["version"], "Teste das permissões"
            )
        assert caught.value.code == code


def test_failed_probe_does_not_create_external_resources(publication, system):
    repo = system["repo"]
    op = verification(system, publication)
    builder, remote = ProbeBuilder(), ProbeRemote()

    def failure():
        raise RuleError("EXECUTOR_UNAVAILABLE", "Synthetic failed isolation")

    builder.verify_isolation = failure
    with pytest.raises(RuleError):
        execute_check(Worker(repo, None, None), op["id"], "lease", lambda *_: remote, builder)
    assert remote.calls == []


def test_stale_verification_cannot_overwrite_recovered_job(publication, system):
    repo = system["repo"]
    op = verification(system, publication)
    with repo.sessions.begin() as db:
        operation = db.get(Operation, op["id"])
        operation.result = {"creation_started": True}
        operation.state = "reconciling"
        job = db.scalar(select(Job).where(Job.operation_id == op["id"]))
        job.state, job.lease_token = "reconciling", None
    remote = ProbeRemote()
    execute_check(Worker(repo, None, None), op["id"], "lease", lambda *_: remote, ProbeBuilder())
    assert remote.calls == []
    with repo.sessions() as db:
        assert db.get(Operation, op["id"]).state == "reconciling"
        job = db.scalar(select(Job).where(Job.operation_id == op["id"]))
        assert job.state == "reconciling" and job.lease_token is None


def test_remote_delete_must_be_confirmed_before_project_vacancy(publication, system):
    repo = system["repo"]
    published = start(system, publication)
    remote = Remote()
    execute(system, published, remote)
    p, actor = publication[3], publication[2]
    op = request_deletion(repo, actor, p["id"], "delete", p["version"], p["name"])
    claim(repo, op["id"])
    original = remote.delete
    remote.delete = lambda *_: None
    with pytest.raises(RuleError) as caught:
        execute_deletion(Worker(repo, None, None), op["id"], "lease", lambda *_: remote)
    assert caught.value.code == "PURGE_INCOMPLETE"
    assert repo.projects(actor)["quota"]["used_projects"] == 1
    assert (repo.sources.root / actor / p["id"]).exists()
    remote.delete = original
    execute_deletion(Worker(repo, None, None), op["id"], "lease", lambda *_: remote)
    assert repo.projects(actor)["quota"]["used_projects"] == 0
    with repo.sessions() as db:
        assert db.get(PublicationResource, p["id"]).state == "purged"


def test_failed_reverification_invalidates_previous_release(publication, system):
    registry, service, actor, project = publication
    qualify(system, registry)
    assert service.readiness(actor, project["id"])["can_publish"]
    verification(system, publication)
    assert not service.readiness(actor, project["id"])["can_publish"]
    registry.validate(system["admin"], "cloudflare", 1, "Não ignorar verificação em andamento")
    assert not service.readiness(actor, project["id"])["can_publish"]


def test_rotation_preserves_diagnostic_resources_without_verifying_old_config(publication, system):
    repo, registry = system["repo"], publication[0]
    op = verification(system, publication)
    remote = ProbeRemote()
    execute_check(Worker(repo, None, None), op["id"], "lease", lambda *_: remote, ProbeBuilder())
    with repo.sessions.begin() as db:
        db.get(Operation, op["id"]).state = "reconciling"
        db.scalar(select(Job).where(Job.operation_id == op["id"])).state = "reconciling"
    cfg = registry.get("cloudflare")
    registry.save(system["admin"], "cloudflare", "rotate", cfg["version"], {
        "settings": cfg["settings"], "token": "synthetic-rotated-credential",
        "reason": "Rotação durante a verificação interrompida",
    })
    before = list(remote.calls)
    resume_check(repo, system["admin"], op["id"], "resume", "Retomar sem excluir recursos")
    claim(repo, op["id"])
    with pytest.raises(RuleError) as changed:
        execute_check(Worker(repo, None, None), op["id"], "lease", lambda *_: remote, ProbeBuilder())
    assert changed.value.code == "VERSION_CONFLICT"
    assert remote.calls == before
    assert remote.remote is not None and remote.db is not None
    assert not registry.get("cloudflare")["validation"].get("runtime_verification")


def test_diagnostic_finds_pending_connection_after_many_unrelated_checks(publication, system):
    repo = system["repo"]
    pending = verification(system, publication)
    with repo.sessions.begin() as db:
        db.add_all(
            Operation(
                account_id=system["admin"],
                type="publication_check",
                state="succeeded",
                payload={"connection_id": "unrelated"},
                created_at=utcnow() + timedelta(seconds=1),
            )
            for _ in range(101)
        )
    dependencies = (system["config"], system["engine"], repo, system["catalog"], None)
    admin = TestClient(create_app(True, dependencies))
    login = admin.post(
        "/admin-api/v1/auth/login",
        json={"login": "operator", "password": "test-operator-password"},
        headers={"Origin": system["config"].admin_origin},
    )
    assert login.status_code == 200
    identity = publication[0].get("cloudflare")["connection_id"]
    response = admin.get(f"/admin-api/v1/integration-connections/{identity}/publication-diagnostics")
    assert response.status_code == 200
    assert response.json()["verification"]["id"] == pending["id"]
    assert not response.json()["can_publish"]


def test_runtime_verification_accepts_current_d1_database_id_binding(publication, system):
    repo = system["repo"]
    op = verification(system, publication)
    remote = ProbeRemote()
    upload = remote.upload

    def current_api_binding(name, database_id, artifact):
        result = upload(name, database_id, artifact)
        binding = remote.remote["bindings"][0]
        binding["database_id"] = binding.pop("id")
        return result

    remote.upload = current_api_binding
    execute_check(Worker(repo, None, None), op["id"], "lease", lambda *_: remote, ProbeBuilder())
    assert repo.operation(system["admin"], op["id"])["state"] == "succeeded"
    assert PublicationService(repo).readiness(publication[2], publication[3]["id"])["can_publish"]
