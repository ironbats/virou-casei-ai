from datetime import timedelta

from sqlalchemy import delete, event, inspect, select

from viracase.application.progress import STAGES
from viracase.domain.rules import RuleError, aware, utcnow
from viracase.infrastructure.models import Operation, RecommendationRun, RuntimeEvent, RuntimeStatus, new_id

SERVICES = {
    "generation": "agents",
    "publication": "cloudflare",
    "publication_check": "cloudflare",
    "project_deletion": "platform",
}


def install_events(sessions):
    @event.listens_for(sessions, "before_flush")
    def capture(db, context, instances):
        for row in list(db.new) + list(db.dirty):
            if not isinstance(row, (Operation, RecommendationRun)):
                continue
            state = inspect(row)
            fields = ("state", "progress_stage", "error_code") if isinstance(row, Operation) else ("state",)
            if row not in db.new and not any(state.attrs[name].history.has_changes() for name in fields):
                continue
            if not row.id:
                row.id = new_id()
            if isinstance(row, Operation):
                service = SERVICES.get(row.type)
                if not service:
                    continue
                stage, code = row.progress_stage or "queued", row.error_code
            else:
                service, stage = "jev", "recommendation_" + (row.state or "pending")
                code = (row.telemetry or {}).get("error_code") or (
                    row.result.get("fallback_reason") if row.state == "fallback" else None
                )
                if code not in {
                    "RECOMMENDER_DISABLED",
                    "RECOMMENDER_UNAVAILABLE",
                    "RECOMMENDATION_PENDING",
                    "JEV_TIMEOUT",
                    "JEV_ACCESS_DENIED",
                    "JEV_RATE_LIMITED",
                    "JEV_QUOTA_EXCEEDED",
                    "JEV_UNAVAILABLE",
                    "JEV_INVALID_RESPONSE",
                }:
                    code = None
            current = row.state or "queued"
            level = (
                "error"
                if current in {"failed", "reconciling"}
                else "warning"
                if current in {"fallback", "cancelled"}
                else "info"
            )
            duration = None
            if row.created_at and current in {"succeeded", "failed", "cancelled", "ready", "fallback"}:
                duration = max(0, int((utcnow() - aware(row.created_at)).total_seconds() * 1000))
            db.add(
                RuntimeEvent(
                    service=service,
                    level=level,
                    account_id=row.account_id,
                    project_id=row.project_id,
                    correlation_id=row.id,
                    stage=stage,
                    state=current,
                    error_code=code,
                    duration_ms=duration,
                )
            )


def require_admin(repo, db, actor):
    if repo.account(db, actor).role != "admin":
        raise RuleError("FORBIDDEN", "Monitoria disponível apenas no backoffice.", 403)


def events(repo, actor, service=None, level=None, correlation=None, project=None, before=None, limit=100):
    with repo.sessions() as db:
        require_admin(repo, db, actor)
        query = select(RuntimeEvent).where(RuntimeEvent.created_at >= utcnow() - timedelta(days=7))
        for value, column in (
            (service, RuntimeEvent.service),
            (level, RuntimeEvent.level),
            (correlation, RuntimeEvent.correlation_id),
            (project, RuntimeEvent.project_id),
        ):
            if value:
                query = query.where(column == value)
        if before:
            query = query.where(RuntimeEvent.id < before)
        rows = db.scalars(query.order_by(RuntimeEvent.id.desc()).limit(limit + 1)).all()
        return {
            "items": [
                {
                    "id": row.id,
                    "at": aware(row.created_at).isoformat(),
                    "service": row.service,
                    "level": row.level,
                    "account_id": row.account_id,
                    "project_id": row.project_id,
                    "correlation_id": row.correlation_id,
                    "stage": row.stage,
                    "state": row.state,
                    "error_code": row.error_code,
                    "duration_ms": row.duration_ms,
                    "message": STAGES.get(
                        row.stage,
                        {
                            "recommendation_pending": "Jev: preparação iniciada.",
                            "recommendation_ready": "Jev: sugestões recebidas.",
                            "recommendation_fallback": "Jev: seleção manual disponível; consulte o código do motivo.",
                        }.get(row.stage, "Estado da operação atualizado."),
                    ),
                }
                for row in rows[:limit]
            ],
            "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
            "server_time": utcnow().isoformat(),
            "retention_days": 7,
        }


def runtime_snapshot(repo):
    with repo.sessions() as db:
        rows = db.scalars(select(RuntimeStatus).where(RuntimeStatus.id.like("worker:%"))).all()
        current = [r for r in rows if aware(r.checked_at) > utcnow() - timedelta(seconds=60)]
        return [{"id": r.id, "checked_at": aware(r.checked_at).isoformat(), **r.details} for r in current]


class WorkerBuildStatus:
    def __init__(self, repo):
        self.workers = runtime_snapshot(repo)

    def available(self):
        return bool(self.workers) and all(w.get("available") for w in self.workers)

    def fingerprint(self):
        values = {w.get("fingerprint") for w in self.workers}
        return next(iter(values)) if len(values) == 1 and None not in values else None

    def supports(self, stack):
        return bool(self.workers) and all(stack in w.get("stacks", []) for w in self.workers)


def heartbeat(repo, identity, diagnostics):
    with repo.sessions.begin() as db:
        row = db.get(RuntimeStatus, identity)
        if not row:
            row = RuntimeStatus(id=identity)
            db.add(row)
        row.checked_at, row.details = utcnow(), diagnostics
        db.execute(delete(RuntimeStatus).where(RuntimeStatus.checked_at < utcnow() - timedelta(days=1)))


def prune(repo):
    with repo.sessions.begin() as db:
        db.execute(delete(RuntimeEvent).where(RuntimeEvent.created_at < utcnow() - timedelta(days=7)))
