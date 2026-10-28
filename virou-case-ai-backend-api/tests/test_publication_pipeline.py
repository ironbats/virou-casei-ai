import json
from copy import deepcopy
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway, PublicationUncertain
from viracase.infrastructure.credentials import initialize_key
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Job, Operation, PublicationResource
from viracase.infrastructure.publication import PublicationService
from viracase.infrastructure.publication_lifecycle import purge_publications
from viracase.infrastructure.publication_qualification import REQUIRED_CHECKS, record_qualification
from viracase.infrastructure.publication_reconcile import reconcile_publication
from viracase.infrastructure.publication_worker import execute_publication
from viracase.infrastructure.worker import Worker

POLICY = {
    "account_id": "a" * 32,
    "token_type": "user",
    "environment": "test",
    "worker_prefix": "lab",
    "compatibility_date": "2026-10-27",
    "allowed_stacks": ["hono-typescript-d1"],
    "max_projects": 5,
    "validation_hours": 24,
    "timeout_seconds": 5,
    "max_concurrent": 2,
    "spa": False,
    "plan": "free",
    "allow_paid": False,
}


class Builder:
    def fingerprint(self):
        return "f" * 64

    def available(self):
        return True

    def build(self, files, stack, revision, secrets=()):
        return {
            "revision_id": revision,
            "main_module": "worker.js",
            "modules": {},
            "assets": {},
            "compatibility_flags": [],
            "migrations": {k: v for k, v in files.items() if k.startswith("migrations/")},
        }


class Remote:
    def __init__(self, mode="success"):
        self.db, self.remote, self.receipts, self.calls = None, None, {}, []
        self.mode = mode
        self.account_path = "/accounts/" + POLICY["account_id"]

    def database(self, name):
        return self.db

    def create_database(self, name):
        self.calls.append("create_database")
        self.db = {"name": name, "uuid": "synthetic-database"}
        if self.mode == "create_uncertain":
            raise PublicationUncertain()
        return self.db

    def query(self, database_id, sql, params=None):
        self.calls.append("query")
        if self.mode == "migration_uncertain" and "__viroucaseai_migrations" not in sql:
            raise PublicationUncertain()
        if sql.startswith("SELECT name, sha256"):
            return [
                {
                    "success": True,
                    "results": [
                        {"name": k, "sha256": v}
                        for k, v in self.receipts.items()
                        if not params or k == params[0]
                    ],
                }
            ]
        if sql.startswith("INSERT INTO __viroucaseai_migrations"):
            self.receipts[params[0]] = params[1]
        return [{"success": True, "results": []}]

    def upload(self, name, database_id, artifact):
        self.calls.append("upload")
        self.remote = {
            "tags": ["viroucaseai", artifact["revision_id"]],
            "bindings": [{"type": "d1", "name": "DB", "id": database_id}],
        }
        if self.mode == "upload_uncertain":
            raise PublicationUncertain()
        return "real-response-deployment-id-for-test"

    def enable(self, name, enabled=True):
        self.calls.append("enable" if enabled else "disable")

    def verify(self, url, revision):
        self.calls.append("verify")
        if self.mode == "verify_failed":
            raise RuleError("PUBLICATION_VERIFICATION_FAILED", "Synthetic verification failure")

    def settings(self, name):
        return self.remote

    def delete(self, name, database_id):
        self.calls.append("delete")
        self.db, self.remote = None, None

    def request(self, method, path):
        if path.endswith("/workers/subdomain"):
            return {"subdomain": "synthetic-test"}
        if "/d1/database/" in path:
            return self.db
        raise AssertionError(path)


def qualify(system, registry):
    c = registry.get("cloudflare")
    report = {
        "format_version": 1,
        "configuration_id": c["id"],
        "configuration_digest": c["configuration_digest"],
        "account_id": POLICY["account_id"],
        "tested_at": utcnow().isoformat(),
        "expires_at": (utcnow() + timedelta(hours=1)).isoformat(),
        "runner_version": "synthetic-test-fixture",
        "runner_sha256": "f" * 64,
        "executor_digest": "f" * 64,
        "stacks": ["hono-typescript-d1"],
        "checks": [
            {
                "code": code,
                "passed": True,
                "expected": "Synthetic expected result",
                "observed": "Synthetic observed result",
                "evidence_sha256": "e" * 64,
            }
            for code in REQUIRED_CHECKS
        ],
        "report_reference": "synthetic-only-not-production-evidence",
        "reason": "Fixture de contrato, não é evidência de produção",
    }
    return record_qualification(system["repo"], system["admin"], "qualification", c["version"], report)


@pytest.fixture
def publication(system, monkeypatch):
    repo = system["repo"]
    initialize_key(system["config"].credential_key_path, repo.sessions)
    registry = Integrations(repo)
    registry.save(
        system["admin"],
        "cloudflare",
        "save",
        0,
        {
            "settings": POLICY,
            "token": "synthetic-cloudflare-token",
            "reason": "Configuração controlada de testes",
        },
    )
    monkeypatch.setattr(
        CloudflareGateway,
        "validate",
        lambda self: {"ready": True, "subdomain": "synthetic-test", "database_ids": [], "workers_count": 0},
    )
    registry.validate(system["admin"], "cloudflare", 1, "Validação simulada do contrato")
    registry.activate(system["admin"], "cloudflare", "activate", 1, "Ativação simulada do contrato")
    actor = system["users"][0]
    project = repo.create_project(
        actor, "p", {"name": "Publicação", "objective": "Lista de tarefas da equipe"}
    )
    return registry, PublicationService(repo, Builder()), actor, project


def start(system, publication):
    registry, service, actor, project = publication
    qualify(system, registry)
    return service.start(actor, project["id"], "publish", {"revision_id": project["current_revision_id"]})


def execute(system, op, remote):
    repo = system["repo"]
    with repo.sessions.begin() as db:
        job = db.scalar(select(Job).where(Job.operation_id == op["id"]))
        job.state, job.lease_token = "running", "lease"
        job.lease_until = utcnow() + timedelta(minutes=5)
    execute_publication(
        Worker(repo, None, system["catalog"]), op["id"], "lease", lambda *_: remote, Builder()
    )


def test_missing_config_preserves_public_contract_with_specific_readiness(system):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor, "p", {"name": "Publicação", "objective": "Lista de tarefas da equipe"}
    )
    service = PublicationService(repo, Builder())
    assert service.readiness(actor, project["id"])["reasons"][0]["code"] == "CONFIGURATION_PENDING"
    with pytest.raises(RuleError) as caught:
        service.start(actor, project["id"], "publish", {"revision_id": project["current_revision_id"]})
    assert caught.value.code == "CAPABILITY_UNVALIDATED"


def test_connection_activation_alone_never_qualifies(publication, system):
    registry, service, actor, project = publication
    status = service.readiness(actor, project["id"])
    assert not status["can_publish"] and "CAPABILITY_UNVALIDATED" in [r["code"] for r in status["reasons"]]
    assert qualify(system, registry)["qualified"]
    assert service.readiness(actor, project["id"])["can_publish"]
    with system["repo"].sessions.begin() as db:
        row = registry.latest(db, "cloudflare")
        value = deepcopy(row.validation)
        value["qualification"]["report"]["checks"][0]["passed"] = False
        row.validation = value
    assert not service.readiness(actor, project["id"])["can_publish"]


def test_credentials_encrypted_and_rotation_invalidates_readiness(publication, system):
    registry, service, actor, project = publication
    qualify(system, registry)
    assert "synthetic-cloudflare-token" not in json.dumps(registry.get("cloudflare"))
    with system["repo"].sessions() as db:
        row = registry.latest(db, "cloudflare")
        assert "synthetic-cloudflare-token" not in row.credential
        assert registry.vault.decrypt(row.id, row.credential) == "synthetic-cloudflare-token"
    registry.save(
        system["admin"],
        "cloudflare",
        "rotate",
        1,
        {"settings": POLICY, "token": "synthetic-rotated-token", "reason": "Rotação de teste e invalidação"},
    )
    assert not service.readiness(actor, project["id"])["can_publish"]
    system["config"].credential_key_path.unlink()
    with pytest.raises(ValueError, match="Restaure"):
        initialize_key(system["config"].credential_key_path, system["repo"].sessions)


def test_publish_durable_idempotent_and_url_only_after_verification(publication, system):
    registry, service, actor, project = publication
    before = system["repo"].wallet(actor, True)
    op = start(system, publication)
    duplicate = service.start(
        actor, project["id"], "publish", {"revision_id": project["current_revision_id"]}
    )
    assert duplicate["id"] == op["id"] and "url" not in op["result_ref"]
    with pytest.raises(RuleError):
        service.start(actor, project["id"], "publish-again", {"revision_id": project["current_revision_id"]})
    remote = Remote()
    execute(system, op, remote)
    result = system["repo"].operation(actor, op["id"])
    assert result["state"] == "succeeded", result
    assert result["result_ref"]["url"].endswith(".synthetic-test.workers.dev")
    assert remote.calls[-1] == "verify" and remote.calls.count("create_database") == 1
    assert system["repo"].wallet(actor, True) == before
    assert not Worker(system["repo"], None, system["catalog"]).tick(op["id"])


@pytest.mark.parametrize(
    "mode", ["create_uncertain", "migration_uncertain", "upload_uncertain", "verify_failed"]
)
def test_external_uncertainty_never_reports_success_or_blindly_replays(publication, system, mode):
    op = start(system, publication)
    remote = Remote(mode)
    execute(system, op, remote)
    actor = publication[2]
    result = system["repo"].operation(actor, op["id"])
    assert result["state"] == "reconciling" and "url" not in result["result_ref"]
    before = list(remote.calls)
    assert not Worker(system["repo"], None, system["catalog"]).tick(op["id"])
    assert remote.calls == before
    if mode == "migration_uncertain":
        with pytest.raises(RuleError) as caught:
            reconcile_publication(
                system["repo"], system["admin"], op["id"], "reconcile", "Inspeção de teste", lambda *_: remote
            )
        assert caught.value.code == "MIGRATION_RESULT_UNKNOWN"
    if mode == "upload_uncertain":
        remote.mode = "success"
        reconciled = reconcile_publication(
            system["repo"], system["admin"], op["id"], "reconcile", "Inspeção de teste", lambda *_: remote
        )
        assert reconciled["state"] == "succeeded"
        assert remote.calls.count("upload") == 1
        calls = list(remote.calls)
        assert (
            reconcile_publication(
                system["repo"], system["admin"], op["id"], "reconcile", "Inspeção de teste", lambda *_: remote
            )
            == reconciled
        )
        assert remote.calls == calls


def test_lease_recovery_after_side_effect_requires_reconciliation(publication, system):
    op = start(system, publication)
    with system["repo"].sessions.begin() as db:
        row = db.get(Operation, op["id"])
        row.state, row.result = "running", {"external_effect_pending": True}
        job = db.scalar(select(Job).where(Job.operation_id == op["id"]))
        job.state, job.lease_token, job.lease_until = "running", "old", utcnow() - timedelta(seconds=1)
    Worker(system["repo"], None, system["catalog"]).recover()
    assert system["repo"].operation(publication[2], op["id"])["state"] == "reconciling"


def test_purge_verifies_absence_and_checks_fence(publication, system):
    op = start(system, publication)
    remote = Remote()
    execute(system, op, remote)
    actor = publication[2]
    with system["repo"].sessions.begin() as db:
        account = system["repo"].account(db, actor)
        account.fence, account.lifecycle = "authorized-fence", "purging"
    with pytest.raises(RuleError):
        purge_publications(system["repo"], actor, "wrong", lambda *_: remote)
    assert remote.db
    evidence = purge_publications(system["repo"], actor, "authorized-fence", lambda *_: remote)
    assert evidence["resources_confirmed_absent"] == 1
    assert remote.calls[-2:] == ["disable", "delete"]
    assert remote.db is None and remote.remote is None


def test_purge_waits_for_inflight_publication_before_removing_inventory(publication, system):
    op = start(system, publication)
    actor = publication[2]
    with system["repo"].sessions.begin() as db:
        account = system["repo"].account(db, actor)
        period = system["repo"].period(db, account)
        period.starts_at = utcnow() - timedelta(days=33)
        period.expires_at = utcnow() - timedelta(days=3)
        period.purge_at = utcnow() - timedelta(days=1)
        job = db.scalar(select(Job).where(Job.operation_id == op["id"]))
        job.state = "running"
        db.get(Operation, op["id"]).state = "running"
    Worker(system["repo"], None, system["catalog"]).purge(actor)
    with system["repo"].sessions() as db:
        assert system["repo"].account(db, actor).lifecycle == "purging"
        assert db.get(PublicationResource, publication[3]["id"]) is not None


@pytest.mark.parametrize(
    "code,error",
    [(403, "CONFIGURATION_INVALID"), (429, "QUOTA_EXCEEDED"), (503, "PUBLICATION_RESULT_UNKNOWN")],
)
def test_gateway_sanitizes_errors_and_marks_unknown_writes(code, error):
    gateway = CloudflareGateway(
        POLICY,
        "never-leak-token",
        httpx.MockTransport(lambda request: httpx.Response(code, json={"secret": "never-leak-token"})),
    )
    with pytest.raises(RuleError) as caught:
        gateway.create_database("safe-name")
    assert caught.value.code == error and "never-leak-token" not in str(caught.value)
