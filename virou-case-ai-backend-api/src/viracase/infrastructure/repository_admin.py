from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import delete, select

from viracase.domain.rules import Access, RuleError, aware, money, reserve, utcnow
from viracase.infrastructure.models import Account, Audit, Grant, Payment, Period, Session, new_id
from viracase.infrastructure.repository_dto import iso


class RepositoryAdmin:
    def admin_accounts(self, cursor=None):
        with self.sessions() as db:
            q = select(Account).where(Account.role == "student")
            if cursor:
                q = q.where(Account.id > cursor)
            rows = db.scalars(q.order_by(Account.id).limit(51)).all()
            return {
                "items": [
                    {
                        "id": a.id,
                        "name": a.name,
                        "login": a.login,
                        "suspended": a.suspended,
                        "access": self.access(db, a),
                    }
                    for a in rows[:50]
                ],
                "next_cursor": rows[49].id if len(rows) > 50 else None,
            }

    def admin_command(self, actor, action, target, key, payload):
        from viracase.infrastructure.repository import HASHER

        def command(db, operator):
            if operator.role != "admin":
                raise RuleError("FORBIDDEN", "Ação administrativa não permitida.", 403)
            if action == "create-account":
                a = Account(
                    login=payload["login"].strip().lower(),
                    name=payload["name"],
                    password_hash=HASHER.hash(payload["password"]),
                )
                db.add(a)
                db.flush()
            else:
                a = self.account(db, target, True)
                if a.role != "student":
                    raise RuleError("FORBIDDEN", "Ação restrita a contas de alunos.", 403)
            if action in {"grant", "topup"}:
                paid = money(payload["amount_paid_brl"])
                credit = money(payload.get("api_credit_brl", "50"))
                if paid <= 0 or credit <= 0 or (action == "grant" and paid != Decimal(75)):
                    raise RuleError(
                        "VALIDATION_ERROR", "Confira o pagamento e o crédito. O valor do ciclo é R$ 75.", 400
                    )
                if db.scalar(select(Payment.id).where(Payment.reference == payload["reference"])):
                    raise RuleError("PAYMENT_ALREADY_RECORDED", "Referência de pagamento já registrada.")
                if action == "grant":
                    if a.lifecycle in {"purging", "reconciling"}:
                        raise RuleError(
                            "PURGE_IN_PROGRESS",
                            "Expurgo em curso. Não é possível garantir preservação de dados.",
                        )
                    start = aware(
                        payload["starts_at"]
                        if not isinstance(payload["starts_at"], str)
                        else datetime.fromisoformat(payload["starts_at"])
                    )
                    if abs((start - utcnow()).total_seconds()) > 300:
                        raise RuleError(
                            "INVALID_ACCESS_START",
                            "Ative o novo ciclo no instante atual; confirme as novas datas.",
                            400,
                        )
                    old = db.scalars(
                        select(Grant).where(Grant.account_id == a.id, Grant.closed.is_(False))
                    ).all()
                    for g in old:
                        self.ledger(db, g, "expire", g.available, f"close:{g.id}")
                        g.expired += g.available
                        g.available, g.closed = Decimal(0), True
                    access = Access.activate(start, payload.get("access_days", 30))
                    a.entitlement_version += 1
                    p = Period(
                        account_id=a.id,
                        starts_at=access.starts_at,
                        expires_at=access.expires_at,
                        purge_at=access.purge_at,
                        version=a.entitlement_version,
                    )
                    db.add(p)
                    db.flush()
                    a.current_period_id, a.lifecycle, a.suspended, a.fence = p.id, "active", False, None
                else:
                    self.access(db, a, "write")
                payment = Payment(
                    account_id=a.id,
                    operator_id=actor,
                    reference=payload["reference"],
                    amount=paid,
                    credit=credit,
                    kind=action,
                    reason=payload["reason"],
                )
                db.add(payment)
                db.flush()
                g = Grant(
                    account_id=a.id,
                    period_id=a.current_period_id,
                    kind="included" if action == "grant" else "topup",
                    total=credit,
                    available=credit,
                )
                db.add(g)
                db.flush()
                self.ledger(db, g, "credit", credit, f"payment:{payment.id}")
            elif action == "access-adjust":
                period = self.period(db, a)
                if not period or a.lifecycle != "active" or aware(period.purge_at) <= utcnow():
                    raise RuleError("ACCESS_EXPIRED", "Ative um novo ciclo para esta conta.", 409)
                if a.entitlement_version != payload["expected_version"]:
                    raise RuleError(
                        "VERSION_CONFLICT", "O acesso mudou. Atualize a conta e tente novamente.", 409
                    )
                end = payload["expires_at"]
                end = aware(datetime.fromisoformat(end) if isinstance(end, str) else end)
                access = Access(period.starts_at, period.expires_at, period.purge_at).with_expiry(
                    end, utcnow()
                )
                period.expires_at, period.purge_at = access.expires_at, access.purge_at
                a.entitlement_version += 1
                period.version = a.entitlement_version
            elif action == "suspend":
                a.suspended = True
            elif action == "reset":
                a.password_hash = HASHER.hash(payload["password"])
                db.execute(delete(Session).where(Session.account_id == a.id))
            elif action == "adjust":
                self.access(db, a, "write")
                value = money(payload["amount_brl"])
                if value == 0:
                    raise RuleError("VALIDATION_ERROR", "O ajuste precisa ser diferente de zero.", 400)
                if value > 0:
                    g = Grant(
                        account_id=a.id,
                        period_id=a.current_period_id,
                        kind="adjustment",
                        total=value,
                        available=value,
                    )
                    db.add(g)
                    db.flush()
                    self.ledger(db, g, "adjustment", value, f"adjust:{g.id}")
                else:
                    grants = db.scalars(
                        select(Grant)
                        .where(
                            Grant.account_id == a.id,
                            Grant.period_id == a.current_period_id,
                            Grant.closed.is_(False),
                        )
                        .order_by(Grant.id)
                    ).all()
                    reserve(sum((g.available for g in grants), Decimal(0)), -value)
                    remaining = -value
                    for g in grants:
                        part = min(remaining, g.available)
                        g.available -= part
                        g.expired += part
                        if part:
                            self.ledger(db, g, "adjustment", -part, f"adjust:{new_id()}")
                        remaining -= part
            db.add(
                Audit(
                    actor_id=actor,
                    action=action,
                    resource_id=a.id,
                    reason=payload.get("reason", "Provisionamento manual"),
                )
            )
            db.flush()
            return {"id": a.id, "name": a.name, "login": a.login, "access": self.access(db, a)}

        safe_payload = dict(payload)
        if "password" in safe_payload:
            safe_payload["password"] = sha256(safe_payload["password"].encode()).hexdigest()
        return self.mutate(actor, f"admin:{action}:{target or 'new'}", key, safe_payload, command)

    def audit_events(self, cursor=None):
        with self.sessions() as db:
            q = select(Audit)
            if cursor:
                q = q.where(Audit.id > cursor)
            rows = db.scalars(q.order_by(Audit.id).limit(51)).all()
            return {
                "items": [
                    {
                        "id": a.id,
                        "actor_id": a.actor_id,
                        "action": a.action,
                        "resource_id": a.resource_id,
                        "reason": a.reason,
                        "created_at": iso(a.created_at),
                    }
                    for a in rows[:50]
                ],
                "next_cursor": rows[49].id if len(rows) > 50 else None,
            }
