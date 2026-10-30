from datetime import timedelta

from sqlalchemy import select

from viracase.domain.rules import utcnow
from viracase.infrastructure.models import Job, Operation, Project, RecommendationRun
from viracase.infrastructure.project_deletion import request_deletion
from viracase.infrastructure.worker import Worker


def create(system, key):
    return system["repo"].create_project(
        system["users"][0], key, {"name": "Projeto temporario", "objective": "Sistema temporario de teste"}
    )


def test_recent_recommendation_does_not_prevent_owner_deletion(system):
    repo, actor = system["repo"], system["users"][0]
    p = create(system, "recs")
    with repo.sessions.begin() as db:
        db.add(RecommendationRun(
            account_id=actor, project_id=p["id"], request_key="r1",
            payload_hash="hash", state="pending", created_at=utcnow(),
        ))
    result = request_deletion(repo, actor, p["id"], "delete", p["version"], p["name"])
    assert Worker(repo, None, None).tick(result["id"])
    assert repo.operation(actor, result["id"])["state"] == "succeeded"


def test_pending_job_is_cancelled_and_reservation_safe(system):
    repo, actor = system["repo"], system["users"][0]
    p = create(system, "pending")
    with repo.sessions.begin() as db:
        previous = Operation(account_id=actor, project_id=p["id"], type="generation", state="queued")
        db.add(previous)
        db.flush()
        db.add(Job(operation_id=previous.id))
        previous_id = previous.id
    request = request_deletion(repo, actor, p["id"], "delete", p["version"], p["name"])
    with repo.sessions() as db:
        old = db.get(Operation, previous_id)
        assert old.state == "cancelled"
        job = db.scalar(select(Job).where(Job.operation_id == previous_id))
        assert job.state == "done"
    assert Worker(repo, None, None).tick(request["id"])
    assert repo.operation(actor, request["id"])["state"] == "succeeded"


def test_live_job_defers_delete_and_completes_after_safe_terminal_state(system):
    repo, actor = system["repo"], system["users"][0]
    p = create(system, "live")
    with repo.sessions.begin() as db:
        previous = Operation(account_id=actor, project_id=p["id"], type="generation", state="running")
        db.add(previous)
        db.flush()
        db.add(Job(
            operation_id=previous.id, state="running", lease_token="worker-lease",
            lease_until=utcnow() + timedelta(minutes=15),
        ))
        previous_id = previous.id
    request = request_deletion(repo, actor, p["id"], "delete", p["version"], p["name"])
    duplicate = request_deletion(repo, actor, p["id"], "another-request", p["version"] + 1, p["name"])
    assert request["id"] == duplicate["id"]
    assert Worker(repo, None, None).tick(request["id"])
    assert repo.operation(actor, request["id"])["progress_stage"] == "deletion_waiting_operations"
    with repo.sessions.begin() as db:
        old = db.get(Operation, previous_id)
        old.state = "cancelled"
        prior_job = db.scalar(select(Job).where(Job.operation_id == previous_id))
        prior_job.state = "done"
        deletion_job = db.scalar(select(Job).where(Job.operation_id == request["id"]))
        deletion_job.available_at = utcnow() - timedelta(seconds=1)
    assert Worker(repo, None, None).tick(request["id"])
    assert repo.operation(actor, request["id"])["state"] == "succeeded"


def test_worker_does_not_run_queued_job_on_deleting_project(system):
    repo, actor = system["repo"], system["users"][0]
    p = create(system, "queued-after")
    result = request_deletion(repo, actor, p["id"], "delete", p["version"], p["name"])
    with repo.sessions.begin() as db:
        queued = Operation(account_id=actor, project_id=p["id"], type="generation", state="queued")
        db.add(queued)
        db.flush()
        db.add(Job(operation_id=queued.id, available_at=utcnow() - timedelta(minutes=5)))
        oid = queued.id
    assert Worker(repo, None, None).tick(oid)
    with repo.sessions() as db:
        assert db.get(Operation, oid).state == "cancelled"
        assert db.get(Project, p["id"]).state == "deleting"
    assert Worker(repo, None, None).tick(result["id"])
    assert repo.operation(actor, result["id"])["state"] == "succeeded"


def test_abandoned_publication_can_be_quiesced_for_remote_purge(system):
    from viracase.infrastructure.models import IntegrationConfig, PublicationResource
    from viracase.infrastructure.project_deletion import quiesce_operations

    repo, actor = system["repo"], system["users"][0]
    p = create(system, "publication-stale")
    with repo.sessions.begin() as db:
        old = Operation(account_id=actor, project_id=p["id"], type="publication", state="reconciling")
        db.add(old)
        db.flush()
        old_id = old.id
        db.add(Job(operation_id=old_id, state="reconciling"))
        cfg = IntegrationConfig(provider="cloudflare", version=1, state="active", settings={},
                                credential=None, created_by=actor)
        db.add(cfg)
        db.flush()
        db.add(PublicationResource(
            project_id=p["id"], account_id=actor, config_id=cfg.id,
            remote_account_id="cloudflare-account", worker_name="vca-" + p["id"].replace("-", ""),
            state="reserved", inventory={},
        ))
    with repo.sessions.begin() as db:
        assert quiesce_operations(repo, db, p["id"]) == []
    with repo.sessions() as db:
        assert db.get(Operation, old_id).state == "cancelled"
        assert db.scalar(select(Job).where(Job.operation_id == old_id)).state == "done"


def test_owner_can_request_deletion_even_after_access_expired(system):
    from viracase.infrastructure.models import Account, Period

    repo, actor = system["repo"], system["users"][0]
    p = create(system, "expired")
    with repo.sessions.begin() as db:
        account = db.get(Account, actor)
        period = db.get(Period, account.current_period_id)
        period.starts_at = utcnow() - timedelta(days=20)
        period.expires_at = utcnow() - timedelta(days=2)
        period.purge_at = utcnow() + timedelta(days=2)
    response = request_deletion(repo, actor, p["id"], "delete", p["version"], p["name"])
    assert response["state"] == "queued"
