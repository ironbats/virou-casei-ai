from datetime import timedelta

import pytest
from sqlalchemy import select

from viracase.domain.rules import Access, RuleError, aware, utcnow
from viracase.infrastructure.models import Account, Audit, Grant, Period, new_id


def grant(system, days=None):
    payload = {
        "amount_paid_brl": "75",
        "api_credit_brl": "50",
        "reference": new_id(),
        "reason": "Ativação de teste",
        "starts_at": utcnow().isoformat(),
    }
    if days is not None:
        payload["access_days"] = days
    return system["repo"].admin_command(system["admin"], "grant", system["users"][0], new_id(), payload)


@pytest.mark.parametrize("days", [1, 7, 45, 3650])
def test_grant_duration_and_grace_remain_separate(system, days):
    result = grant(system, days)
    with system["repo"].sessions() as db:
        account = db.get(Account, result["id"])
        period = db.get(Period, account.current_period_id)
        assert period.expires_at - period.starts_at == timedelta(days=days)
        assert period.purge_at - period.expires_at == timedelta(days=2)
    assert system["repo"].wallet(result["id"], True)["available_brl"] == "50.00000000"


def test_legacy_grant_defaults_to_30_days(system):
    result = grant(system)
    with system["repo"].sessions() as db:
        account = db.get(Account, result["id"])
        period = db.get(Period, account.current_period_id)
        assert period.expires_at - period.starts_at == timedelta(days=30)


@pytest.mark.parametrize("days", [0, -1, 3651, 1.5, True, "30"])
def test_bad_duration_rolls_back_financial_changes(system, days):
    before = system["repo"].wallet(system["users"][0])
    with pytest.raises(RuleError):
        grant(system, days)
    assert system["repo"].wallet(system["users"][0]) == before


def test_adjust_expiry_preserves_budget_period_suspension_and_audits(system):
    repo, uid = system["repo"], system["users"][0]
    with repo.sessions.begin() as db:
        account = db.get(Account, uid)
        period_id = account.current_period_id
        account.suspended = True
        before = [
            (g.id, g.total, g.available) for g in db.scalars(select(Grant).where(Grant.account_id == uid))
        ]
    end = utcnow() + timedelta(days=7)
    payload = {"expires_at": end.isoformat(), "expected_version": 1, "reason": "Prazo combinado com usuário"}
    result = repo.admin_command(system["admin"], "access-adjust", uid, "adjust-once", payload)
    assert result == repo.admin_command(system["admin"], "access-adjust", uid, "adjust-once", payload)
    with repo.sessions() as db:
        account = db.get(Account, uid)
        period = db.get(Period, period_id)
        assert account.current_period_id == period_id and account.suspended
        assert account.entitlement_version == 2 and period.version == 2
        assert aware(period.expires_at) == end and aware(period.purge_at) == end + timedelta(days=2)
        assert before == [
            (g.id, g.total, g.available) for g in db.scalars(select(Grant).where(Grant.account_id == uid))
        ]
        assert len(db.scalars(select(Audit).where(Audit.action == "access-adjust")).all()) == 1
    with pytest.raises(RuleError, match="acesso mudou"):
        repo.admin_command(system["admin"], "access-adjust", uid, new_id(), payload)


@pytest.mark.parametrize("state", ["purging", "reconciling", "purged"])
def test_access_adjust_never_resurrects_purged_data(system, state):
    repo, uid = system["repo"], system["users"][0]
    with repo.sessions.begin() as db:
        db.get(Account, uid).lifecycle = state
    with pytest.raises(RuleError):
        repo.admin_command(
            system["admin"],
            "access-adjust",
            uid,
            new_id(),
            {
                "expires_at": (utcnow() + timedelta(days=5)).isoformat(),
                "expected_version": 1,
                "reason": "Ajuste de teste",
            },
        )


def test_access_adjust_rejects_past_and_retains_read_only_grace():
    now = utcnow()
    access = Access.activate(now, 7)
    access.authorize("export", now + timedelta(days=8))
    with pytest.raises(RuleError):
        access.authorize("write", now + timedelta(days=8))
    with pytest.raises(RuleError):
        access.with_expiry(now - timedelta(days=1), now)
    with pytest.raises(RuleError):
        access.with_expiry(now + timedelta(days=3651), now)
