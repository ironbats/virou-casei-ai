import hmac
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select, text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from viracase.application.generation import GenerationService
from viracase.domain.rules import RuleError, cost
from viracase.infrastructure.config import normalize_origin
from viracase.infrastructure.drafts import Drafts
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.learning import LearningLibrary
from viracase.infrastructure.models import Attempt, Audit, Job, Operation, PurgeInventory
from viracase.infrastructure.publication import PublicationService
from viracase.infrastructure.workflow_logging import emit, workflow_context
from viracase.interfaces.preparation_schemas import ProposalApplyInput
from viracase.interfaces.schemas import (
    AccessAdjustmentInput,
    AccessInput,
    AccountInput,
    AdjustmentInput,
    BackupEvidence,
    ConversationInput,
    GenerateInput,
    LearningDraftInput,
    Login,
    PaymentInput,
    ProjectDraftInput,
    ProjectInput,
    ProjectDeletionInput,
    ReasonInput,
    ReconcileInput,
    ResetInput,
    RevisionInput,
    RevisionReference,
    TransferInput,
)

OFFER = {
    "offer_id": "experimentation",
    "version": "2026-10-v1",
    "currency": "BRL",
    "amount_minor": "7500",
    "payment_mode": "manual_one_time",
    "access_days": 30,
    "hosting_grace_days": 2,
    "retention_days": 32,
    "max_projects": 2,
    "automatic_renewal": False,
    "rollover": False,
    "whatsapp_url": "https://wa.me/5511941810735",
}


def create_app(admin=False, dependencies=None):
    if dependencies is None:
        from viracase.infrastructure.composition import compose

        dependencies = compose()
    config, engine, repo, catalog, gateway = dependencies
    audience = "admin" if admin else "student"
    prefix = "/admin-api/v1" if admin else "/api/v1"
    cookie = "vira_admin" if admin else "vira_student"
    config.validate_runtime()
    origins = config.allowed_origins(admin)
    learning = LearningLibrary(config.content_root) if not admin else None
    generation = GenerationService(repo, catalog)
    drafts = Drafts(repo, learning)

    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.dispose()

    app = FastAPI(
        title="viroucaseai " + audience,
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"]
        if admin
        else [host.strip() for host in config.public_hosts.split(",") if host.strip()],
    )

    def request_fields(request):
        route = request.scope.get("route")
        fields = {
            "request_id": getattr(request.state, "request_id", ""),
            "phase": "api",
            "audience": audience,
            "method": request.method,
            "route": getattr(route, "path", None),
        }
        for parameter, name in (("pid", "project_id"), ("oid", "operation_id")):
            try:
                value = request.path_params.get(parameter)
                fields[name] = str(UUID(value)) if value else None
            except (ValueError, TypeError, AttributeError):
                pass
        return fields

    def error_response(request, code, message, status, details=None, cause=None):
        request.state.error_code = code
        emit(
            "http_request_failed" if status >= 500 else "http_request_rejected",
            "error" if status >= 500 else "warning",
            error=cause,
            **request_fields(request),
            error_code=code,
            http_status=status,
        )
        return JSONResponse(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": getattr(request.state, "request_id", ""),
                    "retryable": False,
                    "details": details or {},
                }
            },
            status_code=status,
        )

    def complete_response(request, response, started):
        response.headers.update(
            {
                "X-Request-ID": request.state.request_id,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            }
        )
        emit(
            "http_request_completed",
            **request_fields(request),
            http_status=response.status_code,
            duration_ms=round((time.monotonic() - started) * 1000),
            error_code=getattr(request.state, "error_code", None),
        )
        return response

    @app.middleware("http")
    async def boundary(request, call_next):
        request.state.request_id = str(uuid4())
        start = time.monotonic()
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            try:
                origin_allowed = normalize_origin(request.headers.get("origin")) in origins
            except ValueError:
                origin_allowed = False
            if not origin_allowed:
                return complete_response(
                    request, error_response(request, "ORIGIN_DENIED", "Origem não autorizada.", 403), start
                )
            try:
                size = int(request.headers.get("content-length", "0"))
            except ValueError:
                size = 9_000_000
            if size > 3_000_000:
                return complete_response(
                    request, error_response(request, "PAYLOAD_TOO_LARGE", "Conteúdo acima do limite.", 413), start
                )
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 3_000_000:
                    return complete_response(
                        request, error_response(request, "PAYLOAD_TOO_LARGE", "Conteúdo acima do limite.", 413), start
                    )
            request._body = bytes(body)
        try:
            from viracase.infrastructure.publication_logging import publication_context

            with publication_context(request_id=request.state.request_id), workflow_context(
                request_id=request.state.request_id, phase="api", audience=audience
            ):
                response = await call_next(request)
        except Exception as exc:
            response = error_response(
                request,
                "DEPENDENCY_UNAVAILABLE",
                "Não foi possível concluir. Consulte o estado antes de repetir.",
                503,
                cause=exc,
            )
        return complete_response(request, response, start)

    @app.exception_handler(RuleError)
    async def rule_error(request, exc):
        return error_response(request, exc.code, exc.message, exc.status, exc.details, cause=exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error_response(
            request, "VALIDATION_ERROR", "Confira os campos obrigatórios e seus limites.", 400, cause=exc
        )

    def actor(request: Request):
        who = repo.authenticate(request.cookies.get(cookie), audience)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not hmac.compare_digest(
            request.headers.get("x-csrf-token", ""), who["csrf_token"]
        ):
            raise RuleError("CSRF_DENIED", "Atualize a sessão antes de continuar.", 403)
        return who

    def key(idempotency_key: str = Header(...)):
        return idempotency_key

    def version(if_match: str = Header(...)):
        try:
            return int(if_match.strip('"'))
        except ValueError:
            raise RuleError("VALIDATION_ERROR", "ETag inválido.", 400) from None

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        with engine.connect() as connection:
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            if version != "0007":
                raise RuleError("SCHEMA_PENDING", "Migrações pendentes.", 503)
        return {"status": "ready"}

    @app.post(prefix + "/auth/login")
    def login(data: Login, request: Request):
        token, info = repo.login(
            data.login, data.password, audience, request.client.host if request.client else "unknown"
        )
        response = JSONResponse(info)
        response.set_cookie(
            cookie,
            token,
            httponly=True,
            secure=config.cookie_secure,
            samesite="strict",
            path=prefix,
            max_age=config.session_hours * 3600,
        )
        return response

    @app.post(prefix + "/auth/logout", status_code=204)
    def logout(request: Request, who=Depends(actor)):
        repo.logout(request.cookies[cookie])
        response = Response(status_code=204)
        response.delete_cookie(cookie, path=prefix)
        return response

    @app.get(prefix + "/me")
    def me(who=Depends(actor)):
        return who

    if not admin:
        from viracase.interfaces.guided_routes import mount_guided_routes
        from viracase.interfaces.preparation_routes import mount_preparation_routes

        mount_guided_routes(app, prefix, repo, learning, generation, actor, key, version)
        mount_preparation_routes(app, prefix, repo, learning, catalog, actor, key)

        @app.get(prefix + "/offer")
        def offer():
            return OFFER

        @app.get(prefix + "/capabilities")
        def capabilities(who=Depends(actor)):
            from viracase.infrastructure.template_preparations import TemplatePreparations

            enabled = bool(catalog.eligible()) and not config.restore_hold
            blocked = {"enabled": False, "reason_code": "CAPABILITY_UNVALIDATED"}
            return {
                "policy_version": "2026-10-v1",
                "stack_labels": ["Hono · TypeScript · D1", "FastAPI · Python · D1"],
                "routing_mode": "automatic",
                "limits": {
                    "max_projects": 2,
                    "max_files": 80,
                    "max_file_bytes": 200000,
                    "max_path_length": 180,
                    "max_prompt_chars": 12000,
                    "max_project_bytes": 2000000,
                    "max_revisions": 100,
                },
                "preview": blocked,
                "visual_preview": {"enabled": True, "mode": "browser_static", "backend_execution": False},
                "publication": PublicationService(repo).capability(),
                "guided_build": {
                    k: v
                    for k, v in Integrations(repo).build_policy().items()
                    if k.startswith("max_") or k in {"autosave_ms", "selection_debounce_ms"}
                },
                "generation": {
                    "enabled": enabled,
                    "reason_code": None if enabled else "COMMERCIAL_CONFIGURATION_PENDING",
                },
                "template_preparation": TemplatePreparations(repo, learning, catalog).capability(),
            }

        def authorize_learning(who):
            with repo.sessions() as db:
                repo.access(db, repo.account(db, who["id"]), "learn")

        @app.get(prefix + "/learning/guides")
        def guides(category: str | None = None, who=Depends(actor)):
            authorize_learning(who)
            from viracase.infrastructure.template_catalog import TemplateCatalog

            return TemplateCatalog(repo, learning).guides(who["id"], category)

        @app.get(prefix + "/learning/guides/{slug}")
        def guide(slug: str, who=Depends(actor)):
            authorize_learning(who)
            from viracase.infrastructure.template_catalog import TemplateCatalog

            return TemplateCatalog(repo, learning).get_guide(who["id"], slug)

        @app.get(prefix + "/learning/guides/{slug}/draft")
        def learning_draft(slug: str, who=Depends(actor)):
            return drafts.get(who["id"], "learning", slug)

        @app.put(prefix + "/learning/guides/{slug}/draft")
        def save_learning_draft(
            slug: str,
            data: LearningDraftInput,
            who=Depends(actor),
            intention=Depends(key),
            current=Depends(version),
        ):
            return drafts.save(who["id"], "learning", slug, intention, data.model_dump(), current)

        @app.post(prefix + "/learning/guides/{slug}/transfer")
        def transfer(slug: str, data: TransferInput, who=Depends(actor), intention=Depends(key)):
            return drafts.transfer(who["id"], slug, intention, data.model_dump())

        @app.get(prefix + "/projects/{pid}/draft")
        def project_draft(pid: str, who=Depends(actor)):
            return drafts.get(who["id"], "project", pid)

        @app.put(prefix + "/projects/{pid}/draft")
        def save_project_draft(
            pid: str,
            data: ProjectDraftInput,
            who=Depends(actor),
            intention=Depends(key),
            current=Depends(version),
        ):
            return drafts.save(who["id"], "project", pid, intention, data.model_dump(), current)

        @app.post(prefix + "/projects/{pid}/proposals/{oid}/apply")
        def apply_proposal(
            pid: str, oid: str, data: ProposalApplyInput | None = None, who=Depends(actor), intention=Depends(key), current=Depends(version)
        ):
            return drafts.apply(who["id"], pid, oid, intention, current, data.model_dump(exclude_none=True) if data else None)

        @app.get(prefix + "/projects")
        def projects(who=Depends(actor)):
            return repo.projects(who["id"])

        @app.post(prefix + "/projects", status_code=201)
        def create_project(data: ProjectInput, who=Depends(actor), intention=Depends(key)):
            return repo.create_project(who["id"], intention, data.model_dump())

        @app.get(prefix + "/projects/{pid}")
        def project(pid: str, who=Depends(actor)):
            item = repo.project(who["id"], pid)
            return JSONResponse(item, headers={"ETag": f'"{item["version"]}"'})

        @app.delete(prefix + "/projects/{pid}", status_code=202)
        def delete_project(
            pid: str,
            data: ProjectDeletionInput,
            who=Depends(actor),
            intention=Depends(key),
            current=Depends(version),
        ):
            from viracase.infrastructure.project_deletion import request_deletion

            return request_deletion(repo, who["id"], pid, intention, current, data.confirmation)

        @app.patch(prefix + "/projects/{pid}")
        def patch_project(
            pid: str, data: ProjectInput, who=Depends(actor), intention=Depends(key), current=Depends(version)
        ):
            item = repo.edit_project(who["id"], pid, intention, data.model_dump(), current)
            return JSONResponse(item, headers={"ETag": f'"{item["version"]}"'})

        @app.get(prefix + "/projects/{pid}/revisions")
        def revisions(pid: str, cursor: str | None = None, who=Depends(actor)):
            return repo.revisions(who["id"], pid, cursor)

        @app.get(prefix + "/projects/{pid}/revisions/{rid}")
        def revision(pid: str, rid: str, who=Depends(actor)):
            return repo.revision(who["id"], pid, rid)

        @app.get(prefix + "/projects/{pid}/revisions/{rid}/files/{fid}")
        def file(pid: str, rid: str, fid: str, who=Depends(actor)):
            return repo.revision(who["id"], pid, rid, fid)

        @app.post(prefix + "/projects/{pid}/revisions", status_code=201)
        def save_revision(
            pid: str,
            data: RevisionInput,
            who=Depends(actor),
            intention=Depends(key),
            current=Depends(version),
        ):
            return repo.save_revision(who["id"], pid, intention, data.model_dump(), current)

        @app.post(prefix + "/projects/{pid}/generations", status_code=202)
        def generate(pid: str, data: GenerateInput, who=Depends(actor), intention=Depends(key)):
            repo.project(who["id"], pid)
            raise RuleError("WORKFLOW_REQUIRED", "Prepare a construção em Ideia, escolha Templates e revise antes de enviar.", 409)

        @app.post(prefix + "/projects/{pid}/conversation", status_code=202)
        def conversation(
            pid: str,
            data: ConversationInput,
            background: BackgroundTasks,
            who=Depends(actor),
            intention=Depends(key),
        ):
            repo.project(who["id"], pid)
            raise RuleError("WORKFLOW_REQUIRED", "Todo novo pedido começa em Ideia e passa por Templates e Revisão.", 409)

        @app.post(prefix + "/projects/{pid}/exports", status_code=202)
        def export(pid: str, data: RevisionReference, who=Depends(actor), intention=Depends(key)):
            return repo.create_operation(who["id"], pid, "export", intention, data.model_dump())

        @app.post(prefix + "/projects/{pid}/previews", status_code=202)
        def preview(pid: str, data: RevisionReference, who=Depends(actor), intention=Depends(key)):
            return repo.create_operation(who["id"], pid, "preview", intention, data.model_dump())

        @app.get(prefix + "/projects/{pid}/publication-readiness")
        def publication_readiness(pid: str, revision_id: str | None = None, who=Depends(actor)):
            return PublicationService(repo).readiness(who["id"], pid, revision_id)

        @app.post(prefix + "/projects/{pid}/publications", status_code=202)
        def publish(pid: str, data: RevisionReference, who=Depends(actor), intention=Depends(key)):
            return PublicationService(repo).start(who["id"], pid, intention, data.model_dump())

        @app.get(prefix + "/exports/{eid}/download")
        def download(eid: str, who=Depends(actor)):
            raw, name = repo.download(who["id"], eid)
            return Response(
                raw,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{name}"'},
            )

        @app.get(prefix + "/operations/{oid}")
        def operation(oid: str, who=Depends(actor)):
            return repo.operation(who["id"], oid)

        @app.get(prefix + "/operations")
        @app.get(prefix + "/usage")
        def operations(
            project_id: str | None = None,
            state: str | None = None,
            cursor: str | None = None,
            latest: bool = False,
            kind: str | None = None,
            who=Depends(actor),
        ):
            return repo.operations(who["id"], project_id, state, cursor, latest=latest, kind=kind)

        @app.post(prefix + "/operations/{oid}/cancel", status_code=202)
        def cancel(oid: str, who=Depends(actor), intention=Depends(key)):
            return repo.cancel(who["id"], oid, intention)

        @app.get(prefix + "/wallet")
        def wallet(who=Depends(actor)):
            value = repo.wallet(who["id"])
            value["can_start_operation"] = value["can_start_operation"] and bool(catalog.eligible())
            return value

        @app.get(prefix + "/wallet/entries")
        def entries(cursor: str | None = None, who=Depends(actor)):
            return repo.entries(who["id"], cursor)
    else:
        from viracase.interfaces.ai_routes import mount_ai_routes

        mount_ai_routes(app, prefix, repo, catalog, actor, key)
        from viracase.interfaces.integration_routes import mount_integration_routes
        from viracase.interfaces.preparation_routes import mount_trace_routes

        mount_integration_routes(app, prefix, repo, actor, key, version)
        mount_trace_routes(app, prefix, repo, catalog, actor, key, version)

        @app.get(prefix + "/accounts")
        def accounts(cursor: str | None = None, who=Depends(actor)):
            return repo.admin_accounts(cursor)

        @app.post(prefix + "/accounts", status_code=201)
        def provision(data: AccountInput, who=Depends(actor), intention=Depends(key)):
            return repo.admin_command(who["id"], "create-account", None, intention, data.model_dump())

        @app.post(prefix + "/accounts/{aid}/credentials/reset")
        def reset(aid: str, data: ResetInput, who=Depends(actor), intention=Depends(key)):
            return repo.admin_command(who["id"], "reset", aid, intention, data.model_dump())

        @app.post(prefix + "/accounts/{aid}/access-grants")
        def access_grant(aid: str, data: AccessInput, who=Depends(actor), intention=Depends(key)):
            return repo.admin_command(who["id"], "grant", aid, intention, data.model_dump(mode="json"))

        @app.post(prefix + "/accounts/{aid}/suspend")
        def suspend(aid: str, data: ReasonInput, who=Depends(actor), intention=Depends(key)):
            return repo.admin_command(who["id"], "suspend", aid, intention, data.model_dump())

        @app.post(prefix + "/accounts/{aid}/access-adjustments")
        def access_adjustment(
            aid: str, data: AccessAdjustmentInput, who=Depends(actor), intention=Depends(key)
        ):
            return repo.admin_command(
                who["id"], "access-adjust", aid, intention, data.model_dump(mode="json")
            )

        @app.post(prefix + "/accounts/{aid}/topups")
        def topup(aid: str, data: PaymentInput, who=Depends(actor), intention=Depends(key)):
            return repo.admin_command(who["id"], "topup", aid, intention, data.model_dump())

        @app.post(prefix + "/accounts/{aid}/wallet-adjustments")
        def adjustment(aid: str, data: AdjustmentInput, who=Depends(actor), intention=Depends(key)):
            return repo.admin_command(who["id"], "adjust", aid, intention, data.model_dump())

        @app.get(prefix + "/accounts/{aid}/wallet")
        def wallet_admin(aid: str, who=Depends(actor)):
            return repo.wallet(aid, True)

        @app.get(prefix + "/operations")
        def operations_admin(cursor: str | None = None, who=Depends(actor)):
            return repo.operations(who["id"], cursor=cursor, admin=True)

        @app.get(prefix + "/audit-events")
        def audit(cursor: str | None = None, who=Depends(actor)):
            return repo.audit_events(cursor)

        @app.get(prefix + "/observability/events")
        def runtime_events(
            service: str | None = Query(None, pattern="^(jev|agents|cloudflare|platform)$"),
            level: str | None = Query(None, pattern="^(info|warning|error)$"),
            correlation: str | None = Query(None, max_length=36),
            project: str | None = Query(None, max_length=36),
            before: int | None = Query(None, ge=1),
            limit: int = Query(100, ge=1, le=200),
            phase: str | None = Query(None, pattern="^(recommendation|preparation|review|construction)$"),
            state: str | None = Query(None, max_length=24),
            since: datetime | None = None,
            until: datetime | None = None,
            who=Depends(actor),
        ):
            from viracase.infrastructure.observability import events

            return events(repo, who["id"], service, level, correlation, project, before, limit, phase, state, since, until)

        @app.get(prefix + "/observability/runtime")
        def runtime_status(who=Depends(actor)):
            from viracase.infrastructure.observability import runtime_snapshot

            return {"workers": runtime_snapshot(repo)}

        @app.get(prefix + "/operational-status")
        def operational_status(who=Depends(actor)):
            with repo.sessions() as db:
                purges = db.scalars(select(PurgeInventory).order_by(PurgeInventory.id).limit(100)).all()
                return {
                    "ai_enabled": bool(catalog.eligible()),
                    "restore_hold": config.restore_hold,
                    "lifecycle_enabled": config.lifecycle_enabled,
                    "publication_enabled": PublicationService(repo).capability()["enabled"],
                    "purges": [
                        {
                            "account_id": r.account_id,
                            "fence": r.fence,
                            "destination": r.destination,
                            "state": r.state,
                            "evidence": r.evidence,
                        }
                        for r in purges
                    ],
                }

        @app.post(prefix + "/operations/{oid}/reconcile")
        def reconcile(oid: str, data: ReconcileInput, who=Depends(actor), intention=Depends(key)):
            def command(db, operator):
                op = db.get(Operation, oid)
                if not op:
                    raise RuleError("NOT_FOUND", "Operação indisponível.", 404)
                repo.account(db, op.account_id, True)
                if op.billing_status != "reconciliation_pending":
                    raise RuleError("OPERATION_CONFLICT", "A operação não aguarda apuração.")
                if data.confirmed_no_effect == (data.usage is not None):
                    raise RuleError(
                        "VALIDATION_ERROR", "Informe ausência de efeito comprovada ou uso confirmado.", 400
                    )
                actual = Decimal(0) if data.confirmed_no_effect else cost(op.policy, data.usage)
                repo.settle_operation(db, op, actual)
                op.state, op.progress_stage = "failed", "reconciled_by_operator"
                attempt = db.scalar(select(Attempt).where(Attempt.operation_id == oid))
                if attempt:
                    attempt.state, attempt.actual_brl, attempt.usage = "reconciled", actual, data.usage or {}
                db.scalar(select(Job).where(Job.operation_id == oid)).state = "done"
                if op.type == "template_preparation" and op.payload.get("automatic_apply"):
                    from viracase.infrastructure.build_workflow import apply_automatic

                    apply_automatic(repo, db, op)
                db.add(
                    Audit(
                        actor_id=who["id"],
                        action="reconcile",
                        resource_id=oid,
                        reason=data.reason + " | " + data.evidence_reference,
                    )
                )
                return {"id": oid, "state": op.state, "billing_status": op.billing_status}

            return repo.mutate(who["id"], f"reconcile:{oid}", intention, data.model_dump(), command)

        @app.post(prefix + "/accounts/{aid}/purge-evidence")
        def purge_evidence(aid: str, data: BackupEvidence, who=Depends(actor), intention=Depends(key)):
            def command(db, operator):
                a = repo.account(db, aid, True)
                if a.fence != data.fence or a.lifecycle != "reconciling":
                    raise RuleError("LIFECYCLE_FENCE_CONFLICT", "Inventário de expurgo desatualizado.")
                rows = db.scalars(
                    select(PurgeInventory).where(
                        PurgeInventory.account_id == aid, PurgeInventory.fence == data.fence
                    )
                ).all()
                for row in rows:
                    if row.destination == "backups_not_managed":
                        row.state, row.evidence = (
                            "confirmed_by_operator",
                            {"reference": data.evidence_reference, "operator_id": who["id"]},
                        )
                    elif row.state != "confirmed":
                        raise RuleError("PURGE_INCOMPLETE", "Há destinos de expurgo pendentes.")
                if not rows:
                    raise RuleError("PURGE_INCOMPLETE", "Inventário ausente.")
                a.lifecycle = "purged"
                db.add(
                    Audit(actor_id=who["id"], action="purge_evidence", resource_id=aid, reason=data.reason)
                )
                return {"id": aid, "lifecycle": a.lifecycle}

            return repo.mutate(who["id"], f"purge-evidence:{aid}", intention, data.model_dump(), command)

    return app
