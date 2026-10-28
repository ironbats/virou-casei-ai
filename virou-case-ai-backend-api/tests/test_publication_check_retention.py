from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_publication_pipeline import publication as publication_fixture
from test_publication_runtime import ProbeBuilder, ProbeRemote, claim, verification

from viracase.domain.rules import RuleError, canonical_hash, utcnow
from viracase.infrastructure.cloudflare_gateway import PublicationUncertain
from viracase.infrastructure.models import IntegrationConfig, Job, Operation
from viracase.infrastructure.publication_check import execute_check, resume_check, runtime_verified
from viracase.infrastructure.publication_check_resources import verification_resource_name
from viracase.infrastructure.worker import Worker


@pytest.fixture
def publication(system, monkeypatch):
    return publication_fixture.__wrapped__(system, monkeypatch)


def run(system, operation, remote):
    execute_check(Worker(system["repo"], None, None), operation["id"], "lease", lambda *_: remote, ProbeBuilder())


def test_unknown_existing_resources_are_never_adopted_or_deleted(system, publication):
    config = publication[0].get("cloudflare")
    name = verification_resource_name(config["settings"], config["connection_id"], "hono-typescript-d1")
    remote = ProbeRemote()
    remote.db = {"name": name, "uuid": "foreign-database"}
    remote.remote = {"tags": ["viroucaseai", "foreign-revision"], "bindings": []}
    before = deepcopy((remote.db, remote.remote))
    operation = verification(system, publication)
    with pytest.raises(RuleError) as caught:
        run(system, operation, remote)
    assert caught.value.code == "RESOURCE_CONFLICT"
    assert (remote.db, remote.remote) == before
    assert remote.calls == []


def test_creation_intent_without_confirmed_database_id_cannot_adopt_an_existing_database(system, publication):
    config = publication[0].get("cloudflare")
    name = verification_resource_name(config["settings"], config["connection_id"], "hono-typescript-d1")
    operation = verification(system, publication)
    with system["repo"].sessions.begin() as db:
        op = db.get(Operation, operation["id"])
        op.result = {
            "resource_policy": "retain", "creation_started": True,
            "verification_resources": [{
                "stack": "hono-typescript-d1", "worker_name": name,
                "database_id": None, "creation_started": True, "state": "retained",
            }],
        }
    remote = ProbeRemote()
    remote.db = {"name": name, "uuid": "externally-created-database"}
    before = deepcopy(remote.db)
    with pytest.raises(RuleError) as caught:
        run(system, operation, remote)
    assert caught.value.code == "RESOURCE_CONFLICT"
    assert remote.db == before and remote.remote is None
    assert remote.calls == []


@pytest.mark.parametrize("changed", ["tags", "database", "binding"])
def test_reuse_requires_original_database_revision_and_bindings(system, publication, changed):
    remote = ProbeRemote()
    run(system, verification(system, publication), remote)
    if changed == "tags":
        remote.remote["tags"] = ["viroucaseai", "foreign-revision"]
    elif changed == "database":
        remote.db["uuid"] = "substituted-database"
    else:
        remote.remote["bindings"][0]["id"] = "foreign-database"
    before = deepcopy((remote.db, remote.remote, remote.calls))
    with pytest.raises(RuleError) as caught:
        run(system, verification(system, publication, "changed-owner"), remote)
    assert caught.value.code == "RESOURCE_CONFLICT"
    assert (remote.db, remote.remote, remote.calls) == before


def test_recheck_reuses_existing_capacity_at_remote_quota(system, publication):
    remote = ProbeRemote()
    run(system, verification(system, publication), remote)
    remote.validate = lambda: {
        "ready": True, "subdomain": "synthetic-test", "workers_count": 100,
        "database_ids": [remote.db["uuid"]] + [f"existing-{i}" for i in range(9)],
    }
    operation = verification(system, publication, "full-quota")
    run(system, operation, remote)
    assert system["repo"].operation(system["admin"], operation["id"])["state"] == "succeeded"
    assert remote.calls.count("create_database") == 1
    assert remote.calls.count("upload") == 2
    assert "delete" not in remote.calls and "disable" not in remote.calls


def test_failed_verification_still_counts_retained_resources(system, publication):
    remote = ProbeRemote("verify_failed")
    with pytest.raises(RuleError):
        run(system, verification(system, publication), remote)
    validation = publication[0].get("cloudflare")["validation"]
    assert validation["publication_check_blocked"] is True
    assert validation["database_ids"] == [remote.db["uuid"]]
    assert validation["databases_count"] == 1
    assert validation["workers_count"] == 1
    assert "runtime_verification" not in validation
    assert "delete" not in remote.calls and "disable" not in remote.calls


@pytest.mark.parametrize("uncertain,expected", [(False, 99), (True, 100)])
def test_worker_inventory_distinguishes_rejected_and_uncertain_uploads(system, publication, uncertain, expected):
    remote = ProbeRemote()
    remote.validate = lambda: {
        "ready": True, "subdomain": "synthetic-test", "workers_count": 99, "database_ids": [],
    }

    def rejected_upload(name, database_id, artifact):
        if uncertain:
            raise PublicationUncertain()
        raise RuleError("CONFIGURATION_INVALID", "Synthetic permission rejection", 403)

    remote.upload = rejected_upload
    with pytest.raises(RuleError):
        run(system, verification(system, publication), remote)
    validation = publication[0].get("cloudflare")["validation"]
    assert validation["workers_count"] == expected
    assert validation["database_ids"] == [remote.db["uuid"]]


def test_resource_names_isolate_connections_accounts_and_stacks(publication):
    config = publication[0].get("cloudflare")
    policy, identity = config["settings"], config["connection_id"]
    names = {
        verification_resource_name(policy, identity, "hono-typescript-d1"),
        verification_resource_name(policy, identity, "fastapi-python-d1"),
        verification_resource_name(policy, "another-connection", "hono-typescript-d1"),
        verification_resource_name({**policy, "account_id": "b" * 32}, identity, "hono-typescript-d1"),
    }
    assert len(names) == 4


def test_interrupted_check_resumes_without_deleting_or_duplicating_resources(system, publication):
    repo, remote = system["repo"], ProbeRemote()
    operation = verification(system, publication)
    run(system, operation, remote)
    with repo.sessions.begin() as db:
        op = db.get(Operation, operation["id"])
        op.state = "running"
        job = db.scalar(select(Job).where(Job.operation_id == operation["id"]))
        job.state, job.lease_until = "running", utcnow() - timedelta(seconds=1)
    Worker(repo, None, None).recover()
    status = repo.operation(system["admin"], operation["id"])
    assert status["state"] == "reconciling"
    assert status["error_code"] == "VERIFICATION_RESUME_REQUIRED"
    resume_check(repo, system["admin"], operation["id"], "resume-retained", "Retomar mantendo os recursos")
    claim(repo, operation["id"])
    run(system, operation, remote)
    assert repo.operation(system["admin"], operation["id"])["state"] == "succeeded"
    assert remote.calls.count("create_database") == 1
    assert remote.calls.count("upload") == 1
    assert "delete" not in remote.calls and "disable" not in remote.calls


def resume_interrupted(system, operation):
    repo = system["repo"]
    with repo.sessions.begin() as db:
        job = db.scalar(select(Job).where(Job.operation_id == operation["id"]))
        job.lease_until = utcnow() - timedelta(seconds=1)
    Worker(repo, None, None).recover()
    resume_check(repo, system["admin"], operation["id"], "resume-upload", "Confirmar o envio interrompido")
    claim(repo, operation["id"])


def test_resume_confirms_upload_after_crash_without_repeating_put(system, publication):
    remote, operation = ProbeRemote(), verification(system, publication)
    upload = remote.upload

    def crash_after_upload(*args):
        upload(*args)
        raise SystemExit("Simulated process termination")

    remote.upload = crash_after_upload
    with pytest.raises(SystemExit):
        run(system, operation, remote)
    remote.upload = upload
    resume_interrupted(system, operation)
    run(system, operation, remote)
    status = system["repo"].operation(system["admin"], operation["id"])
    assert status["state"] == "succeeded"
    assert remote.calls.count("upload") == remote.calls.count("create_database") == 1
    assert "delete" not in remote.calls and "disable" not in remote.calls


def test_unconfirmed_upload_is_not_repeated_by_resume(system, publication):
    remote, operation, attempted = ProbeRemote(), verification(system, publication), []

    def unknown_upload(name, database_id, artifact):
        attempted.append(name)
        raise PublicationUncertain()

    remote.upload = unknown_upload
    with pytest.raises(PublicationUncertain):
        run(system, operation, remote)
    resume_interrupted(system, operation)
    with pytest.raises(PublicationUncertain):
        run(system, operation, remote)
    assert len(attempted) == 1
    assert remote.calls.count("create_database") == 1
    assert "delete" not in remote.calls and "disable" not in remote.calls


def test_resume_does_not_certify_an_upload_from_another_executor(system, publication):
    remote, operation = ProbeRemote(), verification(system, publication)
    run(system, operation, remote)
    with system["repo"].sessions.begin() as db:
        op = db.get(Operation, operation["id"])
        op.state = "running"
        db.scalar(select(Job).where(Job.operation_id == operation["id"])).state = "running"
    resume_interrupted(system, operation)
    builder = ProbeBuilder()
    builder.fingerprint = lambda: "b" * 64
    with pytest.raises(RuleError) as caught:
        execute_check(Worker(system["repo"], None, None), operation["id"], "lease", lambda *_: remote, builder)
    assert caught.value.code == "VERSION_CONFLICT"
    assert remote.calls.count("upload") == 1


@pytest.mark.parametrize("invalid", ["empty", "future", "expired", "missing_check", "false_check"])
def test_runtime_evidence_rejects_empty_future_expired_or_incomplete_reports(system, publication, invalid):
    run(system, verification(system, publication), ProbeRemote())
    config = publication[0].get("cloudflare")
    with system["repo"].sessions.begin() as db:
        row = db.get(IntegrationConfig, config["id"])
        report = deepcopy(row.validation["runtime_verification"]["report"])
        if invalid == "empty":
            report["stacks"] = []
        elif invalid == "future":
            report["tested_at"] = (utcnow() + timedelta(minutes=1)).isoformat()
        elif invalid == "expired":
            report["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
        elif invalid == "missing_check":
            del report["checks"]["resource_retention"]
        else:
            report["checks"]["binding_isolation"] = False
        row.validation = {**row.validation, "runtime_verification": {"report": report, "digest": canonical_hash(report)}}
        assert not runtime_verified(row, None, "f" * 64)
        assert not runtime_verified(row, "hono-typescript-d1", "f" * 64)


def test_existing_valid_cleanup_evidence_remains_accepted(system, publication):
    run(system, verification(system, publication), ProbeRemote())
    config = publication[0].get("cloudflare")
    with system["repo"].sessions.begin() as db:
        row = db.get(IntegrationConfig, config["id"])
        report = deepcopy(row.validation["runtime_verification"]["report"])
        report["checks"]["cleanup"] = report["checks"].pop("resource_retention")
        row.validation = {**row.validation, "runtime_verification": {"report": report, "digest": canonical_hash(report)}}
        assert runtime_verified(row, "hono-typescript-d1", "f" * 64)
