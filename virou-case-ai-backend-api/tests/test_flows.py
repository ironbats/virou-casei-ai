from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import io
import json
import zipfile
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from viracase.application.generation import GenerationService
from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.models import (
    Account,
    Attempt,
    Grant,
    Job,
    Ledger,
    Operation,
    Period,
    Project,
    Revision,
    new_id,
)
from viracase.infrastructure.providers import ProviderUncertain
from viracase.infrastructure.templates import starter
from viracase.infrastructure.worker import Worker
from viracase.interfaces.api import create_app


def project(system, user=None, key=None):
    return system["repo"].create_project(
        user or system["users"][0],
        key or new_id(),
        {"name": "Projeto de teste", "objective": "Criar uma aplicação para validar ideias"},
    )


def generation(system, p, key=None):
    return GenerationService(system["repo"], system["catalog"]).start(
        system["users"][0],
        p["id"],
        key or new_id(),
        {
            "brief": "Melhorar o título e preservar os contratos",
            "base_revision_id": p["current_revision_id"],
            "policy_version": "2026-10-v1",
        },
    )


class Gateway:
    def __init__(self, mode="ok"):
        self.calls, self.mode = 0, mode

    def generate(self, policy, prompt):
        self.calls += 1
        if self.mode == "timeout":
            raise ProviderUncertain("after_dispatch")
        files = starter("hono-typescript-d1")
        files["public/app.js"] += "\nconst revision = 2;\n"
        usage = {"input": 100, "cached": 0, "output": 100 if self.mode != "overrun" else 10000000}
        return ModelResult(json.dumps({"files": files, "summary": "Atualizado"}), usage, "test-provider-id")


def test_project_quota_idempotency_and_scope(system):
    r, user = system["repo"], system["users"][0]
    p = project(system, key="same-intent")
    assert project(system, key="same-intent") == p
    with pytest.raises(RuleError) as e:
        r.create_project(user, "same-intent", {"name": "Different", "objective": "Outra aplicação distinta"})
    assert e.value.code == "IDEMPOTENCY_CONFLICT"
    project(system)
    with pytest.raises(RuleError) as e:
        project(system)
    assert e.value.code == "PROJECT_LIMIT_REACHED"
    with pytest.raises(RuleError) as e:
        r.project(system["users"][1], p["id"])
    assert e.value.status == 404


def test_source_revision_conflict_and_export(system):
    r, user = system["repo"], system["users"][0]
    p = project(system)
    files = r.source_files(user, p["id"], p["current_revision_id"])
    files["public/title.txt"] = "Teste"
    change = {"base_revision_id": p["current_revision_id"], "files": files}
    saved = r.save_revision(user, p["id"], new_id(), change, p["version"])
    with pytest.raises(RuleError) as e:
        r.save_revision(user, p["id"], new_id(), change, p["version"])
    assert e.value.status == 412
    assert "public/title.txt" not in r.source_files(user, p["id"], p["current_revision_id"])
    export = r.create_operation(user, p["id"], "export", new_id(), {"revision_id": saved["revision"]["id"]})
    gateway = Gateway()
    Worker(r, gateway, system["catalog"]).tick()
    raw, name = r.download(user, export["id"])
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        assert z.read("public/title.txt") == b"Teste"
        assert ".env" not in z.namelist()
        assert "migrations/0001.sql" in z.namelist()
    assert gateway.calls == 0
    with pytest.raises(RuleError):
        r.download(system["users"][1], export["id"])


def test_generation_durable_and_settled_once(system):
    r, user = system["repo"], system["users"][0]
    p = project(system)
    op = generation(system, p, "generation-intent")
    assert generation(system, p, "generation-intent")["id"] == op["id"]
    assert r.wallet(user)["reserved_ratio"] > 0
    gateway = Gateway()
    worker = Worker(r, gateway, system["catalog"])
    assert worker.tick()
    assert not worker.tick()
    result = r.operation(user, op["id"])
    assert result["state"] == "succeeded"
    assert result["billing_status"] == "settled"
    assert gateway.calls == 1
    with r.sessions() as db:
        debits = db.scalars(
            select(Ledger).where(Ledger.operation_id == op["id"], Ledger.kind == "debit")
        ).all()
        assert sum((d.amount for d in debits), Decimal(0)) == Decimal("0.0003")


def test_timeout_keeps_reservation_no_automatic_retry(system):
    r, user = system["repo"], system["users"][0]
    op = generation(system, project(system))
    gateway = Gateway("timeout")
    worker = Worker(r, gateway, system["catalog"])
    worker.tick()
    for _ in range(3):
        worker.tick()
    assert gateway.calls == 1
    assert r.operation(user, op["id"])["state"] == "reconciling"
    assert r.wallet(user)["reserved_ratio"] > 0
    assert r.wallet(user)["consumed_ratio"] == 0
    assert r.wallet(user)["pending_reconciliation"]


def test_overrun_does_not_debit_extra(system):
    r, user = system["repo"], system["users"][0]
    op = generation(system, project(system))
    Worker(r, Gateway("overrun"), system["catalog"]).tick()
    assert r.operation(user, op["id"])["state"] == "reconciling"
    wallet = r.wallet(user, True)
    assert Decimal(wallet["available_brl"]) >= 0
    assert Decimal(wallet["consumed_brl"]) == 0


def test_cancel_before_dispatch_releases_reservation(system):
    r, user = system["repo"], system["users"][0]
    op = generation(system, project(system))
    r.cancel(user, op["id"], "cancel-intent")
    gateway = Gateway()
    assert not Worker(r, gateway, system["catalog"]).tick()
    assert gateway.calls == 0
    assert r.operation(user, op["id"])["state"] == "cancelled"
    assert r.wallet(user)["available_ratio"] == 1


def test_generation_candidate_preserves_manual_revision(system):
    r, user = system["repo"], system["users"][0]
    p = project(system)
    op = generation(system, p)
    files = r.source_files(user, p["id"], p["current_revision_id"])
    files["public/title.txt"] = "manual"
    saved = r.save_revision(
        user, p["id"], new_id(), {"base_revision_id": p["current_revision_id"], "files": files}, p["version"]
    )
    Worker(r, Gateway(), system["catalog"]).tick()
    assert r.operation(user, op["id"])["result_ref"]["candidate"]
    assert r.project(user, p["id"])["current_revision_id"] == saved["revision"]["id"]


def test_worker_crash_after_send_is_reconciled(system):
    r, user = system["repo"], system["users"][0]
    op = generation(system, project(system))
    with r.sessions.begin() as db:
        job = db.scalar(select(Job).where(Job.operation_id == op["id"]))
        job.state, job.lease_until, job.lease_token = "running", utcnow() - timedelta(seconds=1), new_id()
        db.add(Attempt(operation_id=op["id"], provider="openai", model="gpt-6-luna", state="sent"))
    gateway = Gateway()
    Worker(r, gateway, system["catalog"]).tick()
    assert gateway.calls == 0
    assert r.operation(user, op["id"])["billing_status"] == "reconciliation_pending"


def test_renewal_topup_no_rollover_old_reservation(system):
    r, user, admin = system["repo"], system["users"][0], system["admin"]
    op = generation(system, project(system))
    old_period = r.wallet(user)["period_id"]
    with r.sessions() as db:
        before = db.get(Period, old_period).expires_at
    r.admin_command(
        admin,
        "topup",
        user,
        "topup-1",
        {
            "amount_paid_brl": "20",
            "api_credit_brl": "10",
            "reference": "receipt-topup",
            "reason": "Negociação confirmada",
        },
    )
    with r.sessions() as db:
        assert db.get(Period, old_period).expires_at == before
    payload = {
        "amount_paid_brl": "75",
        "api_credit_brl": "50",
        "reference": "receipt-renew",
        "reason": "Pagamento confirmado",
        "starts_at": utcnow().isoformat(),
    }
    a = r.admin_command(admin, "grant", user, "renew", payload)
    assert r.admin_command(admin, "grant", user, "renew", payload) == a
    assert a["access"]["period_id"] != old_period
    with r.sessions.begin() as db:
        operation = db.get(Operation, op["id"])
        r.settle_operation(db, operation, Decimal("0.01"))
    assert r.wallet(user)["available_ratio"] == 1
    with r.sessions() as db:
        grants = db.scalars(select(Grant).where(Grant.period_id == old_period)).all()
        assert all(g.closed and g.available == 0 for g in grants)


def test_expired_access_read_and_export_then_purge_cutoff(system):
    r, user = system["repo"], system["users"][0]
    p = project(system)
    with r.sessions.begin() as db:
        a = db.get(Account, user)
        period = db.get(Period, a.current_period_id)
        period.starts_at -= timedelta(days=31)
        period.expires_at -= timedelta(days=31)
        period.purge_at -= timedelta(days=31)
    assert r.project(user, p["id"])
    with pytest.raises(RuleError) as e:
        project(system)
    assert e.value.code == "ACCESS_EXPIRED"
    op = r.create_operation(user, p["id"], "export", new_id(), {"revision_id": p["current_revision_id"]})
    Worker(r, Gateway(), system["catalog"]).tick()
    r.download(user, op["id"])
    with r.sessions.begin() as db:
        period = db.get(Period, db.get(Account, user).current_period_id)
        period.purge_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(RuleError) as e:
        r.download(user, op["id"])
    assert e.value.code == "RETENTION_EXPIRED"
    Worker(r, Gateway(), system["catalog"]).purge(user)
    assert not (system["config"].source_root / user).exists()
    with r.sessions() as db:
        assert not db.scalar(select(Project.id).where(Project.account_id == user))
        assert not db.scalar(select(Revision.id).where(Revision.account_id == user))
        assert db.get(Account, user).lifecycle == "reconciling"
        assert db.scalar(select(Grant.id).where(Grant.account_id == user))
    with pytest.raises(RuleError) as e:
        r.admin_command(
            system["admin"],
            "grant",
            user,
            new_id(),
            {
                "amount_paid_brl": "75",
                "api_credit_brl": "50",
                "reference": "renew-after-fence",
                "reason": "Pagamento confirmado",
                "starts_at": utcnow().isoformat(),
            },
        )
    assert e.value.code == "PURGE_IN_PROGRESS"


def test_api_security_financial_privacy_and_learning(system):
    deps = (system["config"], system["engine"], system["repo"], system["catalog"], Gateway())
    public = TestClient(create_app(dependencies=deps))
    admin = TestClient(create_app(admin=True, dependencies=deps))
    assert (
        public.post(
            "/api/v1/auth/login",
            json={"login": "student0", "password": "test-student-password"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    auth = public.post(
        "/api/v1/auth/login",
        json={"login": "student0", "password": "test-student-password"},
        headers={"Origin": system["config"].public_origin},
    )
    assert auth.status_code == 200
    headers = {
        "Origin": system["config"].public_origin,
        "X-CSRF-Token": auth.json()["csrf_token"],
        "Idempotency-Key": "api-project",
    }
    assert (
        public.post(
            "/api/v1/projects",
            json={"name": "Teste", "objective": "Aplicação funcional"},
            headers={"Origin": system["config"].public_origin},
        ).status_code
        == 403
    )
    response = public.post(
        "/api/v1/projects", json={"name": "Teste", "objective": "Aplicação funcional"}, headers=headers
    )
    assert response.status_code == 201, response.text
    p = response.json()
    assert public.get("/admin-api/v1/accounts").status_code == 404
    assert admin.get("/api/v1/projects").status_code == 404
    assert admin.get("/admin-api/v1/accounts").status_code == 401
    before = system["repo"].wallet(system["users"][0], True)
    guides = public.get("/api/v1/learning/guides").json()["items"]
    assert len(guides) == 14
    assert public.get("/api/v1/learning/guides/sdd").json()["template"]
    assert before == system["repo"].wallet(system["users"][0], True)
    for path in [
        "/offer",
        "/me",
        "/capabilities",
        "/wallet",
        "/wallet/entries",
        "/usage",
        "/operations",
        "/projects",
    ]:
        data = public.get("/api/v1" + path).json()
        serialized = json.dumps(data)
        for field in [
            "api_budget_brl",
            "fx_brl_per_usd",
            "input_usd_per_million",
            "actual_brl",
            "available_brl",
            "password_hash",
        ]:
            assert field not in serialized
    blocked = public.post(
        f"/api/v1/projects/{p['id']}/publications",
        json={"revision_id": p["current_revision_id"]},
        headers={**headers, "Idempotency-Key": "publish"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "CAPABILITY_UNVALIDATED"
    assert public.get("/health/ready").status_code == 200
    assert public.get("/api/v1/me", headers={"host": "evil.example"}).status_code == 400
    public.post("/api/v1/auth/logout", headers=headers)
    assert public.get("/api/v1/me").status_code == 401


def test_postgres_concurrent_quota_and_reservations(system):
    if system["engine"].dialect.name != "postgresql":
        pytest.skip("Concorrência exige PostgreSQL real; configure TEST_DATABASE_URL")

    def create(_):
        try:
            return project(system)["id"]
        except RuleError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, range(8)))
    assert results.count("PROJECT_LIMIT_REACHED") == 6
    r, user = system["repo"], system["users"][0]
    p = r.projects(user)["items"]
    policy = {**system["policy"], "reserved_input_bound": 65000}

    def spend(project):
        try:
            r.create_operation(
                user,
                project["id"],
                "generation",
                new_id(),
                {"brief": "Criar fluxo completo", "base_revision_id": project["current_revision_id"]},
                policy,
                Decimal(40),
            )
            return "reserved"
        except RuleError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(spend, p))
    assert sorted(results) == ["INSUFFICIENT_BALANCE", "reserved"]
    assert Decimal(r.wallet(user, True)["available_brl"]) == Decimal(10)
