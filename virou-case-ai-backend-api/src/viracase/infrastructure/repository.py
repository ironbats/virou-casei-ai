from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import io
import secrets
import zipfile
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from sqlalchemy import select, func, delete, and_, or_
from sqlalchemy.exc import IntegrityError
from viracase.application.progress import STAGES, progress_message, record_progress
from viracase.domain.rules import (
    Access,
    RuleError,
    aware,
    canonical_hash,
    choose_stack,
    money,
    reserve,
    settle,
    utcnow,
)
from viracase.infrastructure.models import (
    Account,
    Attempt,
    Audit,
    Grant,
    Idempotency,
    Job,
    Ledger,
    Operation,
    Payment,
    Period,
    Project,
    RateBucket,
    Reservation,
    Revision,
    Session,
    new_id,
)


HASHER = PasswordHasher()
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))
TERMINAL = {"succeeded", "failed", "cancelled"}


def iso(value):
    return aware(value).isoformat() if value else None


def project_dto(p):
    return {
        "id": p.id,
        "name": p.name,
        "objective": p.objective,
        "stack": p.stack,
        "version": p.version,
        "current_revision_id": p.current_revision_id,
        "state": p.state,
        "created_at": iso(p.created_at),
    }


def revision_dto(r):
    return {
        "id": r.id,
        "project_id": r.project_id,
        "source_hash": r.source_hash,
        "files": r.manifest,
        "origin": r.origin,
        "candidate": r.candidate,
        "created_at": iso(r.created_at),
    }


def operation_dto(o, admin=False):
    return {
        "id": o.id,
        "type": o.type,
        "project_id": o.project_id,
        "state": o.state,
        "progress_stage": o.progress_stage,
        "billing_status": o.billing_status,
        "pending_reconciliation": o.billing_status == "reconciliation_pending" or o.state == "reconciling",
        "cancel_requested": o.cancel_requested,
        "result_ref": {k: v for k, v in o.result.items() if k != "activity"},
        "request_text": o.payload.get("brief") if o.type == "generation" else None,
        "progress_message": progress_message(o),
        "activity": [
            {**event, "message": STAGES.get(event["stage"], progress_message(o))}
            for event in o.result.get("activity", [])
        ],
        "error_code": o.error_code,
        "model_label": o.policy.get("model") if admin else None,
        "created_at": iso(o.created_at),
        "updated_at": iso(o.updated_at),
    }


class Repository:
    def __init__(self, sessions, sources, config):
        self.sessions, self.sources, self.config = sessions, sources, config

    def account(self, db, account_id, lock=False):
        stmt = select(Account).where(Account.id == account_id)
        a = db.scalar(stmt.with_for_update() if lock else stmt)
        if not a:
            raise RuleError("NOT_FOUND", "Recurso indisponível.", 404)
        return a

    def period(self, db, a):
        return db.get(Period, a.current_period_id) if a.current_period_id else None

    def access(self, db, a, action=None, now=None):
        now = now or utcnow()
        p = self.period(db, a)
        if not p:
            if action:
                raise RuleError("ACCESS_EXPIRED", "A conta ainda não possui acesso liberado.", 403)
            return {"status": "pending", "allowed_actions": [], "period_id": None}
        value = Access(p.starts_at, p.expires_at, p.purge_at, a.suspended, a.lifecycle)
        if action:
            value.authorize(action, now)
        actions = []
        for candidate in ("read", "export", "write", "generate", "learn", "publish"):
            try:
                value.authorize(candidate, now)
                actions.append(candidate)
            except RuleError:
                pass
        return {
            "period_id": p.id,
            "entitlement_version": a.entitlement_version,
            "starts_at": iso(p.starts_at),
            "access_expires_at": iso(p.expires_at),
            "hosting_suspend_at": iso(p.purge_at),
            "purge_at": iso(p.purge_at),
            "status": value.status(now),
            "allowed_actions": actions,
        }

    def owned_project(self, db, account_id, project_id):
        p = db.scalar(select(Project).where(Project.id == project_id, Project.account_id == account_id))
        if not p:
            raise RuleError("NOT_FOUND", "Recurso indisponível.", 404)
        return p

    def owned_revision(self, db, account_id, project_id, revision_id):
        r = db.scalar(
            select(Revision).where(
                Revision.id == revision_id,
                Revision.project_id == project_id,
                Revision.account_id == account_id,
            )
        )
        if not r:
            raise RuleError("NOT_FOUND", "Recurso indisponível.", 404)
        return r

    def mutate(self, actor_id, command, key, payload, fn, action=None):
        if not key or len(key) > 120:
            raise RuleError("IDEMPOTENCY_KEY_REQUIRED", "Informe uma chave de intenção válida.", 400)
        digest = canonical_hash(payload)
        identity = canonical_hash([actor_id, command, key])
        try:
            with self.sessions.begin() as db:
                actor = self.account(db, actor_id, True)
                previous = db.get(Idempotency, identity)
                if previous:
                    if previous.payload_hash != digest:
                        raise RuleError("IDEMPOTENCY_CONFLICT", "A chave já identifica outro conteúdo.")
                    if previous.result.get("expired"):
                        raise RuleError(
                            "INTENTION_EXPIRED",
                            "O resultado desta intenção ultrapassou o prazo de retenção.",
                            410,
                        )
                    return previous.result
                if action:
                    self.access(db, actor, action)
                result = fn(db, actor)
                db.add(
                    Idempotency(
                        id=identity,
                        actor_id=actor_id,
                        command=command,
                        key=key,
                        payload_hash=digest,
                        result=result,
                    )
                )
                return result
        except IntegrityError:
            raise RuleError(
                "CONFLICT", "Já existe um registro com esta identificação ou referência."
            ) from None

    def recover_intention(self, actor_id, command, key, payload):
        if not key or len(key) > 120:
            raise RuleError("IDEMPOTENCY_KEY_REQUIRED", "Informe uma chave de intenção válida.", 400)
        with self.sessions() as db:
            self.account(db, actor_id)
            previous = db.get(Idempotency, canonical_hash([actor_id, command, key]))
            if previous:
                if previous.payload_hash != canonical_hash(payload):
                    raise RuleError("IDEMPOTENCY_CONFLICT", "A chave já identifica outro conteúdo.")
                if previous.result.get("expired"):
                    raise RuleError(
                        "INTENTION_EXPIRED",
                        "O resultado desta intenção ultrapassou o prazo de retenção.",
                        410,
                    )
                return previous.result
        return None

    def rate_limit(self, key, maximum=10, seconds=300):
        digest = sha256(key.encode()).hexdigest()
        now = utcnow()
        for _ in range(2):
            try:
                with self.sessions.begin() as db:
                    b = db.scalar(select(RateBucket).where(RateBucket.key == digest).with_for_update())
                    if not b:
                        db.add(RateBucket(key=digest, count=1, resets_at=now + timedelta(seconds=seconds)))
                    else:
                        if aware(b.resets_at) <= now:
                            b.count, b.resets_at = 0, now + timedelta(seconds=seconds)
                        if b.count >= maximum:
                            raise RuleError(
                                "QUOTA_EXCEEDED", "Limite temporário. Aguarde alguns minutos.", 429
                            )
                        b.count += 1
                return
            except IntegrityError:
                continue
        raise RuleError("QUOTA_EXCEEDED", "Aguarde antes de tentar novamente.", 429)

    def login(self, login, password, audience, ip):
        self.rate_limit(f"login:{audience}:{ip}", 20)
        self.rate_limit(f"identity:{audience}:{login.lower()}", 10)
        with self.sessions.begin() as db:
            query = select(Account).where(Account.login == login.strip().lower())
            a = db.scalar(query.with_for_update() if audience == "admin" else query)
            try:
                HASHER.verify(a.password_hash if a else DUMMY_HASH, password)
            except (VerificationError, InvalidHashError):
                raise RuleError("UNAUTHENTICATED", "Credenciais inválidas.", 401) from None
            if not a or a.role != ("admin" if audience == "admin" else "student"):
                raise RuleError("UNAUTHENTICATED", "Credenciais inválidas.", 401)
            token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
            if HASHER.check_needs_rehash(a.password_hash):
                a.password_hash = HASHER.hash(password)
            db.add(
                Session(
                    token_hash=sha256(token.encode()).hexdigest(),
                    account_id=a.id,
                    audience=audience,
                    csrf=csrf,
                    expires_at=utcnow() + timedelta(hours=self.config.session_hours),
                )
            )
            return token, {"id": a.id, "name": a.name, "role": a.role, "csrf_token": csrf}

    def authenticate(self, token, audience):
        if not token:
            raise RuleError("UNAUTHENTICATED", "Entre para continuar.", 401)
        with self.sessions() as db:
            s = db.get(Session, sha256(token.encode()).hexdigest())
            if not s or s.audience != audience or aware(s.expires_at) <= utcnow():
                raise RuleError("UNAUTHENTICATED", "Sua sessão terminou. Entre novamente.", 401)
            a = self.account(db, s.account_id)
            if audience == "admin" and a.role != "admin":
                raise RuleError("FORBIDDEN", "Ação não permitida.", 403)
            return {
                "id": a.id,
                "name": a.name,
                "login": a.login,
                "role": a.role,
                "csrf_token": s.csrf,
                "access": self.access(db, a) if audience == "student" else None,
            }

    def logout(self, token):
        with self.sessions.begin() as db:
            db.execute(delete(Session).where(Session.token_hash == sha256(token.encode()).hexdigest()))

    def projects(self, actor):
        with self.sessions() as db:
            a = self.account(db, actor)
            access = self.access(db, a, "read")
            items = db.scalars(
                select(Project).where(Project.account_id == actor).order_by(Project.created_at, Project.id)
            ).all()
            return {
                "items": [project_dto(p) for p in items],
                "quota": {
                    "max_projects": 2,
                    "used_projects": len(items),
                    "can_create": len(items) < 2 and "write" in access["allowed_actions"],
                },
                "next_cursor": None,
            }

    def create_project(self, actor, key, payload):
        def command(db, a):
            count = db.scalar(select(func.count()).select_from(Project).where(Project.account_id == actor))
            if count >= 2:
                raise RuleError("PROJECT_LIMIT_REACHED", "Você já possui os dois projetos permitidos.")
            p = Project(
                account_id=actor,
                name=payload["name"].strip(),
                objective=payload["objective"],
                stack=choose_stack(payload["objective"]),
            )
            db.add(p)
            db.flush()
            from viracase.infrastructure.templates import starter

            self.add_revision(db, p, starter(p.stack), "template", None)
            return project_dto(p)

        return self.mutate(actor, "create-project", key, payload, command, "write")

    def project(self, actor, pid):
        with self.sessions() as db:
            self.access(db, self.account(db, actor), "read")
            return project_dto(self.owned_project(db, actor, pid))

    def edit_project(self, actor, pid, key, payload, version):
        def command(db, a):
            p = self.owned_project(db, actor, pid)
            if p.version != version:
                raise RuleError("VERSION_CONFLICT", "O projeto mudou em outra sessão.", 412)
            p.name, p.objective, p.version = payload["name"].strip(), payload["objective"], p.version + 1
            return project_dto(p)

        return self.mutate(actor, f"edit:{pid}", key, {**payload, "version": version}, command, "write")

    def add_revision(self, db, p, files, origin, base, proposal=False):
        rid = new_id()
        digest = self.sources.write(p.account_id, p.id, rid, files, p.stack)
        manifest = [
            {"id": sha256(name.encode()).hexdigest()[:24], "path": name, "size": len(content.encode())}
            for name, content in sorted(files.items())
        ]
        candidate = proposal or base != p.current_revision_id
        r = Revision(
            id=rid,
            account_id=p.account_id,
            project_id=p.id,
            source_hash=digest,
            manifest=manifest,
            origin=origin,
            candidate=candidate,
        )
        db.add(r)
        if not candidate:
            p.current_revision_id, p.version = rid, p.version + 1
        db.flush()
        return r

    def save_revision(self, actor, pid, key, payload, version):
        def command(db, a):
            p = self.owned_project(db, actor, pid)
            if p.version != version or p.current_revision_id != payload.get("base_revision_id"):
                raise RuleError(
                    "VERSION_CONFLICT", "A revisão mudou. Seu conteúdo local foi preservado.", 412
                )
            if db.scalar(select(func.count()).select_from(Revision).where(Revision.project_id == pid)) >= 100:
                raise RuleError("QUOTA_EXCEEDED", "Limite de 100 revisões por projeto atingido.", 429)
            files = payload.get("files")
            if payload.get("restore_revision_id"):
                files = self.sources.read(
                    self.owned_revision(db, actor, pid, payload["restore_revision_id"]), p.stack
                )
            r = self.add_revision(
                db,
                p,
                files,
                "restore" if payload.get("restore_revision_id") else "edit",
                payload.get("base_revision_id"),
            )
            return {"revision": revision_dto(r), "project": project_dto(p)}

        return self.mutate(actor, f"revision:{pid}", key, {**payload, "version": version}, command, "write")

    def revisions(self, actor, pid, cursor=None):
        with self.sessions() as db:
            self.access(db, self.account(db, actor), "read")
            self.owned_project(db, actor, pid)
            q = select(Revision).where(Revision.account_id == actor, Revision.project_id == pid)
            if cursor:
                q = q.where(Revision.id > cursor)
            rows = db.scalars(q.order_by(Revision.id).limit(21)).all()
            return {
                "items": [revision_dto(r) for r in rows[:20]],
                "next_cursor": rows[19].id if len(rows) > 20 else None,
            }

    def revision(self, actor, pid, rid, file_id=None):
        with self.sessions() as db:
            self.access(db, self.account(db, actor), "read")
            p = self.owned_project(db, actor, pid)
            r = self.owned_revision(db, actor, pid, rid)
            if file_id:
                item = next((f for f in r.manifest if f["id"] == file_id), None)
                if not item:
                    raise RuleError("NOT_FOUND", "Arquivo indisponível.", 404)
                return {**item, "content": self.sources.read(r, p.stack)[item["path"]]}
            return revision_dto(r)

    def source_files(self, actor, pid, rid):
        with self.sessions() as db:
            self.access(db, self.account(db, actor), "read")
            p = self.owned_project(db, actor, pid)
            return self.sources.read(self.owned_revision(db, actor, pid, rid), p.stack)

    def ledger(self, db, g, kind, amount, effect, operation=None):
        db.add(
            Ledger(
                account_id=g.account_id,
                grant_id=g.id,
                kind=kind,
                amount=money(amount),
                effect_key=effect,
                operation_id=operation,
            )
        )

    def create_operation(self, actor, pid, kind, key, payload, policy=None, ceiling=None):
        def command(db, a):
            p = self.owned_project(db, actor, pid)
            if kind in {"preview", "publication"}:
                raise RuleError(
                    "CAPABILITY_UNVALIDATED",
                    "Isolamento, execução e publicação ainda aguardam validação operacional.",
                )
            if kind == "generation":
                if self.config.restore_hold:
                    raise RuleError(
                        "RECOVERY_HOLD", "Operações pausadas para reconciliação após restauração."
                    )
                if payload.get("base_revision_id") != p.current_revision_id:
                    raise RuleError("VERSION_CONFLICT", "A revisão base mudou.", 412)
                choose_stack(payload["brief"])
                if db.scalar(
                    select(Operation.id).where(
                        Operation.project_id == pid,
                        Operation.type == kind,
                        Operation.state.in_(["queued", "running", "reconciling"]),
                    )
                ):
                    raise RuleError("OPERATION_CONFLICT", "Já existe uma geração em andamento neste projeto.")
                if (
                    db.scalar(select(func.count()).select_from(Revision).where(Revision.project_id == pid))
                    >= 100
                ):
                    raise RuleError("QUOTA_EXCEEDED", "Limite de revisões atingido.", 429)
            else:
                self.owned_revision(db, actor, pid, payload["revision_id"])
            op = Operation(account_id=actor, project_id=pid, type=kind, payload=payload, policy=policy or {})
            if kind == "generation" and op.policy.get("generation_contract_version") == 4:
                from viracase.infrastructure.conversation_state import prepare_turn

                prepare_turn(self, db, op, p)
            db.add(op)
            db.flush()
            record_progress(op, "queued")
            if kind == "generation":
                grants = db.scalars(
                    select(Grant)
                    .where(
                        Grant.account_id == actor,
                        Grant.period_id == a.current_period_id,
                        Grant.closed.is_(False),
                    )
                    .order_by(Grant.created_at, Grant.id)
                    .with_for_update()
                ).all()
                reserve(sum((g.available for g in grants), Decimal(0)), ceiling)
                remaining = money(ceiling)
                for g in grants:
                    part = min(remaining, g.available)
                    if part > 0:
                        g.available -= part
                        g.reserved += part
                        db.add(Reservation(account_id=actor, operation_id=op.id, grant_id=g.id, amount=part))
                        self.ledger(db, g, "reserve", part, f"reserve:{op.id}:{g.id}", op.id)
                        remaining -= part
                op.billing_status = "reserved"
            db.add(Job(operation_id=op.id))
            db.flush()
            return operation_dto(op)

        return self.mutate(
            actor, f"{kind}:{pid}", key, payload, command, "export" if kind == "export" else "generate"
        )

    def operations(
        self, actor, project_id=None, state=None, cursor=None, admin=False, latest=False, kind=None
    ):
        with self.sessions() as db:
            q = select(Operation)
            if not admin:
                q = q.where(Operation.account_id == actor)
            if project_id:
                q = q.where(Operation.project_id == project_id)
            if kind:
                q = q.where(Operation.type == kind)
            if state == "active":
                q = q.where(Operation.state.in_(["queued", "running", "reconciling"]))
            elif state:
                q = q.where(Operation.state == state)
            if cursor and latest:
                boundary = db.scalar(q.where(Operation.id == cursor))
                if not boundary:
                    raise RuleError("INVALID_CURSOR", "A página solicitada não está disponível.", 400)
                q = q.where(
                    or_(
                        Operation.created_at < boundary.created_at,
                        and_(Operation.created_at == boundary.created_at, Operation.id < boundary.id),
                    )
                )
            elif cursor:
                q = q.where(Operation.id > cursor)
            order = (Operation.created_at.desc(), Operation.id.desc()) if latest else (Operation.id,)
            rows = db.scalars(q.order_by(*order).limit(51)).all()
            items = []
            for op in rows[:50]:
                item = operation_dto(op, admin=admin)
                if admin:
                    attempt = db.scalar(select(Attempt).where(Attempt.operation_id == op.id))
                    item.update(
                        account_id=op.account_id,
                        policy=op.policy,
                        attempt=None
                        if not attempt
                        else {
                            "id": attempt.id,
                            "state": attempt.state,
                            "external_id": attempt.external_id,
                            "usage": attempt.usage,
                            "actual_brl": str(attempt.actual_brl),
                        },
                    )
                items.append(item)
            return {"items": items, "next_cursor": rows[49].id if len(rows) > 50 else None}

    def operation(self, actor, oid):
        with self.sessions() as db:
            op = db.scalar(select(Operation).where(Operation.id == oid, Operation.account_id == actor))
            if not op:
                raise RuleError("NOT_FOUND", "Operação indisponível.", 404)
            return operation_dto(op)

    def cancel(self, actor, oid, key):
        def command(db, a):
            op = db.scalar(
                select(Operation).where(Operation.id == oid, Operation.account_id == actor).with_for_update()
            )
            if not op:
                raise RuleError("NOT_FOUND", "Operação indisponível.", 404)
            if op.state not in TERMINAL:
                op.cancel_requested = True
                if op.state == "queued":
                    self.settle_operation(db, op, Decimal(0))
                    op.state, op.progress_stage = "cancelled", "cancelled_before_dispatch"
                    record_progress(op, op.progress_stage)
                    job = db.scalar(select(Job).where(Job.operation_id == oid))
                    job.state = "done"
            return operation_dto(op)

        self.mutate(actor, f"cancel:{oid}", key, {}, command)
        return self.operation(actor, oid)

    def settle_operation(self, db, op, actual):
        reservations = db.scalars(
            select(Reservation)
            .where(Reservation.operation_id == op.id, Reservation.state == "reserved")
            .order_by(Reservation.id)
        ).all()
        if not reservations:
            return
        total = sum((r.amount for r in reservations), Decimal(0))
        settle(total, actual)
        remaining = money(actual)
        for r in reservations:
            g = db.get(Grant, r.grant_id)
            debit = min(remaining, r.amount)
            release = r.amount - debit
            g.reserved -= r.amount
            g.consumed += debit
            if g.closed:
                g.expired += release
            else:
                g.available += release
            r.state = "settled"
            self.ledger(db, g, "debit", debit, f"debit:{op.id}:{g.id}", op.id)
            self.ledger(db, g, "expire" if g.closed else "release", release, f"release:{op.id}:{g.id}", op.id)
            remaining -= debit
        op.billing_status = "settled" if actual > 0 else "released"

    def wallet(self, actor, admin=False):
        with self.sessions() as db:
            a = self.account(db, actor)
            grants = db.scalars(select(Grant).where(Grant.account_id == actor)).all()
            current = [g for g in grants if g.period_id == a.current_period_id]
            total = sum((g.total for g in current), Decimal(0))
            available = sum((g.available for g in current), Decimal(0))
            reserved = sum((g.reserved for g in current), Decimal(0))
            consumed = sum((g.consumed for g in current), Decimal(0))
            pending = bool(
                db.scalar(
                    select(Operation.id).where(
                        Operation.account_id == actor, Operation.billing_status == "reconciliation_pending"
                    )
                )
            )
            result = {
                "period_id": a.current_period_id,
                "available_ratio": float(available / total) if total else 0,
                "reserved_ratio": float(reserved / total) if total else 0,
                "consumed_ratio": float(consumed / total) if total else 0,
                "usage_status": "reconciling" if pending else ("available" if available > 0 else "empty"),
                "can_start_operation": "generate" in self.access(db, a)["allowed_actions"]
                and available > 0
                and not self.config.restore_hold,
                "pending_reconciliation": pending,
            }
            if admin:
                result.update(
                    available_brl=str(available),
                    reserved_brl=str(reserved),
                    consumed_brl=str(consumed),
                    grants=[
                        {
                            "id": g.id,
                            "period_id": g.period_id,
                            "kind": g.kind,
                            "total": str(g.total),
                            "available": str(g.available),
                            "reserved": str(g.reserved),
                            "consumed": str(g.consumed),
                            "expired": str(g.expired),
                            "closed": g.closed,
                        }
                        for g in grants
                    ],
                )
            return result

    def entries(self, actor, cursor=None):
        with self.sessions() as db:
            q = select(Ledger).where(Ledger.account_id == actor)
            if cursor:
                q = q.where(Ledger.id > cursor)
            rows = db.scalars(q.order_by(Ledger.id).limit(51)).all()
            return {
                "items": [
                    {
                        "id": r.id,
                        "type": r.kind,
                        "operation_id": r.operation_id,
                        "created_at": iso(r.created_at),
                    }
                    for r in rows[:50]
                ],
                "next_cursor": rows[49].id if len(rows) > 50 else None,
            }

    def download(self, actor, export_id):
        with self.sessions() as db:
            a = self.account(db, actor, True)
            self.access(db, a, "export")
            op = db.scalar(
                select(Operation).where(
                    Operation.id == export_id,
                    Operation.account_id == actor,
                    Operation.type == "export",
                    Operation.state == "succeeded",
                )
            )
            if not op:
                raise RuleError("NOT_FOUND", "Download indisponível.", 404)
            p = self.owned_project(db, actor, op.project_id)
            r = self.owned_revision(db, actor, p.id, op.payload["revision_id"])
            files = self.sources.read(r, p.stack)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for path, content in sorted(files.items()):
                    archive.writestr(path, content)
            self.access(db, a, "export")
            return buffer.getvalue(), f"viroucaseai-{p.id[:8]}-{r.id[:8]}.zip"

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
