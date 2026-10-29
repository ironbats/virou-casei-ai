import io
import secrets
import zipfile
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from viracase.application.progress import record_progress
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
    Draft,
    Grant,
    Idempotency,
    Job,
    Ledger,
    Operation,
    Period,
    Project,
    RateBucket,
    Reservation,
    Revision,
    Session,
    new_id,
)
from viracase.infrastructure.repository_admin import RepositoryAdmin
from viracase.infrastructure.repository_dto import iso, operation_dto, project_dto, revision_dto

HASHER = PasswordHasher()
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))
TERMINAL = {"succeeded", "failed", "cancelled"}


class Repository(RepositoryAdmin):
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
        for candidate in ("read", "export", "write", "generate", "learn", "publish", "recover_own_publication_no_effect"):
            try:
                if candidate == "recover_own_publication_no_effect" and a.role != "student":
                    continue
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
        p = db.scalar(
            select(Project).where(
                Project.id == project_id,
                Project.account_id == account_id,
                Project.state.not_in(["deleting", "deleted"]),
            )
        )
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
                select(Project)
                .where(Project.account_id == actor, Project.state != "deleted")
                .order_by(Project.created_at, Project.id)
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
            count = db.scalar(
                select(func.count())
                .select_from(Project)
                .where(Project.account_id == actor, Project.state != "deleted")
            )
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
        from viracase.domain.rules import validate_file_entries
        from viracase.infrastructure.publication_migration_context import validate_revision_migrations

        if p.current_revision_id and base is None:
            raise RuleError("VERSION_CONFLICT", "Informe a revisão-base consolidada antes de salvar novos arquivos.", 412)
        validate_file_entries(files, self.sources.secrets)
        rid = new_id()
        migration_evidence = validate_revision_migrations(
            self, db, p, files, base_revision_id=base, revision_id=rid,
            operation="proposal" if proposal else origin,
        )
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
        db.add(Draft(account_id=p.account_id, kind="migration_plan", target=rid, data=migration_evidence))
        if not candidate:
            p.current_revision_id, p.version = rid, p.version + 1
        db.flush()
        r.migration_evidence = migration_evidence
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
            return {"revision": revision_dto(r), "project": project_dto(p), **r.migration_evidence}

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
            if kind == "generation" and payload.get("analysis_id"):
                from viracase.infrastructure.code_analysis import validate_correction

                previous = validate_correction(self, db, actor, p, payload)
                if previous:
                    return operation_dto(previous)
            if kind in {"preview", "publication"}:
                raise RuleError(
                    "CAPABILITY_UNVALIDATED",
                    "Isolamento, execução e publicação ainda aguardam validação operacional.",
                )
            if kind in {"generation", "template_preparation", "code_analysis"}:
                if kind == "template_preparation":
                    from viracase.infrastructure.template_preparations import TemplatePreparations

                    TemplatePreparations(self).freeze(db, actor, pid, payload)
                if payload.get("guided_request"):
                    from viracase.infrastructure.guided_build import GuidedBuild

                    GuidedBuild(self, None).freeze(db, actor, pid, payload)
                if self.config.restore_hold:
                    raise RuleError(
                        "RECOVERY_HOLD", "Operações pausadas para reconciliação após restauração."
                    )
                if kind == "code_analysis":
                    revision = self.owned_revision(db, actor, pid, payload["revision_id"])
                    if revision.source_hash != payload["source_hash"]:
                        raise RuleError("ANALYSIS_OUTDATED", "A fonte não corresponde à revisão analisada.", 409)
                elif payload.get("base_revision_id") != p.current_revision_id:
                    raise RuleError("VERSION_CONFLICT", "A revisão base mudou.", 412)
                if kind == "generation" and not payload.get("analysis_id"):
                    choose_stack(payload["brief"])
                if db.scalar(
                    select(Operation.id).where(
                        Operation.project_id == pid,
                        Operation.type == kind,
                        Operation.state.in_(["queued", "running", "reconciling"]),
                    )
                ):
                    raise RuleError("OPERATION_CONFLICT", "Já existe uma operação deste tipo em andamento neste projeto.")
                if kind == "generation" and (
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
            if kind == "code_analysis":
                from viracase.infrastructure.code_analysis import analysis_reference

                op.result = analysis_reference(payload)
            elif kind == "generation" and payload.get("analysis_id"):
                from viracase.infrastructure.code_analysis import owned_analysis

                analysis = owned_analysis(db, actor, pid, payload["analysis_id"])
                analysis.result = {**analysis.result, "correction_operation_id": op.id}
            if kind == "template_preparation" and payload.get("automatic_apply"):
                from viracase.infrastructure.drafts import Drafts
                import copy

                drafts = Drafts(self, None)
                row = drafts.row(db, actor, "build", pid)
                data = copy.deepcopy(row.data)
                selected = {item["attachment_id"]: item for item in payload["items"]}
                for attachment in data["attachments"]:
                    source = selected.get(attachment["template_id"])
                    if source and attachment["enabled"] and attachment["attachment_instance_id"] == source["attachment_instance_id"]:
                        attachment.update(preparation_id=op.id, fill_state="queued")
                drafts.write(db, actor, "build", pid, row.version, data)
            record_progress(op, "queued")
            if kind == "code_analysis":
                record_progress(op, "analysis_queued")
            if kind == "generation" and payload.get("attachment_snapshots"):
                from viracase.infrastructure.integration_traces import TraceStore, trace_context

                fields = [{"attachment_id": item["source_template_id"], "field_id": field_id, "final_value": value, "source_type": item.get("field_values", {}).get(field_id, {}).get("source_type", "user_edit"), "state": "snapshot", "attachment_revision": item.get("attachment_revision"), "snapshot_id": item["id"]} for item in payload["attachment_snapshots"] for field_id, value in item.get("fields", {}).items()]
                TraceStore(self).put(trace_context(self, op, "construction"), "fields", "snapshot", {"fields": {"items": fields}}, db=db)
            if kind in {"generation", "template_preparation", "code_analysis"}:
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
            actor,
            f"guided-send:{pid}" if payload.get("guided_request") else f"{kind}:{pid}",
            key,
            payload.get("analysis_request", payload.get("preparation_request", payload.get("guided_request", payload.get("reply_request", payload)))),
            command,
            "export" if kind == "export" else "generate",
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
            if op.type in {"project_deletion", "publication_check"} and op.state not in TERMINAL:
                raise RuleError(
                    "OPERATION_CONFLICT",
                    "Esta operação precisa concluir a remoção e a verificação dos recursos.",
                )
            if op.state not in TERMINAL:
                op.cancel_requested = True
                if op.state == "queued":
                    self.settle_operation(db, op, Decimal(0))
                    op.state, op.progress_stage = "cancelled", "cancelled_before_dispatch"
                    record_progress(op, op.progress_stage)
                    job = db.scalar(select(Job).where(Job.operation_id == oid))
                    job.state = "done"
                    if op.type == "template_preparation" and op.payload.get("automatic_apply"):
                        from viracase.infrastructure.build_workflow import apply_automatic

                        apply_automatic(self, db, op)
                    elif op.type == "template_autofill":
                        from viracase.infrastructure.build_workflow import cancel_scheduled

                        cancel_scheduled(self, db, op)
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
