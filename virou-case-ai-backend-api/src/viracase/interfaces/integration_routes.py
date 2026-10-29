from typing import Literal

from fastapi import Depends
from pydantic import ValidationError

from viracase.domain.rules import RuleError
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.learning import LearningLibrary
from viracase.infrastructure.publication_qualification import (
    REQUIRED_CHECKS,
    QualificationReport,
    record_qualification,
)
from viracase.infrastructure.template_catalog import TemplateCatalog
from viracase.interfaces.feature_schemas import ConfigAction, ConfigInput, ConnectionInput, TemplateInput


def mount_integration_routes(app, prefix, repo, actor, key, version):
    registry = Integrations(repo)
    templates = TemplateCatalog(repo, LearningLibrary(repo.config.content_root))

    @app.get(prefix + "/integrations/{provider}/connections")
    def connections(provider: Literal["cloudflare", "jev"], who=Depends(actor)):
        return registry.listing(who["id"], provider)

    @app.post(prefix + "/integrations/{provider}/connections", status_code=201)
    def create_connection(
        provider: Literal["cloudflare", "jev"],
        data: ConnectionInput,
        who=Depends(actor),
        intention=Depends(key),
    ):
        try:
            return registry.save(who["id"], provider, intention, 0, data.model_dump(), create=True)
        except ValidationError:
            raise RuleError("VALIDATION_ERROR", "Confira os campos e limites da conexão.", 400) from None

    @app.get(prefix + "/integrations/{provider}/connections/{identity}")
    def connection(provider: Literal["cloudflare", "jev"], identity: str, who=Depends(actor)):
        return registry.get(provider, identity)

    @app.put(prefix + "/integrations/{provider}/connections/{identity}")
    def edit_connection(
        provider: Literal["cloudflare", "jev"],
        identity: str,
        data: ConnectionInput,
        who=Depends(actor),
        intention=Depends(key),
        current=Depends(version),
    ):
        try:
            return registry.save(who["id"], provider, intention, current, data.model_dump(), identity)
        except ValidationError:
            raise RuleError("VALIDATION_ERROR", "Confira os campos e limites da conexão.", 400) from None

    @app.post(prefix + "/integrations/{provider}/connections/{identity}/validate")
    def validate_connection(
        provider: Literal["cloudflare", "jev"],
        identity: str,
        data: ConfigAction,
        who=Depends(actor),
        current=Depends(version),
    ):
        return registry.validate(who["id"], provider, current, data.reason, identity)

    @app.post(prefix + "/integrations/{provider}/connections/{identity}/{action}")
    def change_connection(
        provider: Literal["cloudflare", "jev"],
        identity: str,
        action: Literal["activate", "disable", "default"],
        data: ConfigAction,
        who=Depends(actor),
        intention=Depends(key),
        current=Depends(version),
    ):
        if action == "default":
            return registry.set_default(who["id"], provider, identity, intention, current, data.reason)
        return registry.activate(
            who["id"],
            provider,
            intention,
            current,
            data.reason,
            disable=action == "disable",
            connection_id=identity,
        )

    @app.get(prefix + "/integration-connections/{identity}/publication-diagnostics")
    def publication_diagnostics(identity: str, who=Depends(actor)):
        from viracase.infrastructure.publication import PublicationService, MESSAGES
        from viracase.infrastructure.observability import WorkerBuildStatus
        from viracase.infrastructure.models import Operation
        from viracase.infrastructure.publication_verification import verification_summary
        from viracase.infrastructure.publication_maintenance import automatic_status
        from sqlalchemy import select

        with repo.sessions() as db:
            registry.require_admin(repo.account(db, who["id"]))
            executor = WorkerBuildStatus(repo)
            service = PublicationService(repo, executor)
            row = registry.latest(db, "cloudflare", connection_id=identity)
            codes = service.configuration_reasons(row)
            verification_codes = []
            if not row or row.state not in {"validated", "active"} or not registry.fresh(row) or not row.validation.get("ready"):
                verification_codes.append("CONFIGURATION_INVALID")
            if not executor.available() or not executor.fingerprint():
                verification_codes.append("EXECUTOR_UNAVAILABLE")
            if repo.config.restore_hold or not repo.config.lifecycle_enabled:
                verification_codes.append("LIFECYCLE_LOCKED")
            latest = db.scalar(
                select(Operation)
                .where(
                    Operation.type == "publication_check",
                    Operation.payload["connection_id"].as_string() == identity,
                )
                .order_by(Operation.created_at.desc())
                .limit(1)
            )
            stack_readiness = []
            for stack in row.settings["allowed_stacks"] if row else []:
                stack_codes = service.configuration_reasons(row, stack)
                stack_readiness.append({
                    "stack": stack,
                    "can_publish": not stack_codes,
                    "reasons": [{"code": code, "message": service.configuration_message(row, code)} for code in stack_codes],
                })
            return {
                "can_publish": not codes,
                "stack_readiness": stack_readiness,
                "can_verify": not verification_codes,
                "verification_reasons": [{"code": c, "message": MESSAGES[c]} for c in verification_codes],
                "workers": executor.workers,
                "reasons": [{"code": c, "message": service.configuration_message(row, c)} for c in codes],
                "verification": verification_summary(latest),
                "automatic_verification": automatic_status(repo, row, executor),
            }

    @app.post(prefix + "/integration-connections/{identity}/verify-publication", status_code=202)
    def verify_publication(
        identity: str,
        data: ConfigAction,
        who=Depends(actor),
        intention=Depends(key),
        current=Depends(version),
    ):
        from viracase.infrastructure.publication_check import request_check

        return request_check(repo, who["id"], identity, intention, current, data.reason)

    @app.post(prefix + "/publication-checks/{oid}/resume", status_code=202)
    def resume_publication_check(oid: str, data: ConfigAction, who=Depends(actor), intention=Depends(key)):
        from viracase.infrastructure.publication_check import resume_check

        return resume_check(repo, who["id"], oid, intention, data.reason)

    @app.post(prefix + "/integration-connections/{identity}/qualification")
    def connection_qualification(
        identity: str,
        data: QualificationReport,
        who=Depends(actor),
        intention=Depends(key),
        current=Depends(version),
    ):
        return record_qualification(
            repo, who["id"], intention, current, data.model_dump(mode="json"), identity
        )

    @app.get(prefix + "/publication-qualification-requirements")
    def qualification_requirements(who=Depends(actor)):
        return {"format_version": 1, "checks": sorted(REQUIRED_CHECKS)}

    @app.post(prefix + "/integrations/cloudflare/qualification")
    def qualification(
        data: QualificationReport, who=Depends(actor), intention=Depends(key), current=Depends(version)
    ):
        return record_qualification(repo, who["id"], intention, current, data.model_dump(mode="json"))

    @app.post(prefix + "/publications/{oid}/reconcile")
    def reconcile(oid: str, data: ConfigAction, who=Depends(actor), intention=Depends(key)):
        from viracase.infrastructure.publication_reconcile import reconcile_publication

        return reconcile_publication(repo, who["id"], oid, intention, data.reason)

    @app.get(prefix + "/integrations/{provider}")
    def config(provider: Literal["cloudflare", "jev"], who=Depends(actor)):
        return registry.get(provider)

    @app.put(prefix + "/integrations/{provider}")
    def save(
        provider: Literal["cloudflare", "jev"],
        data: ConfigInput,
        who=Depends(actor),
        intention=Depends(key),
        current=Depends(version),
    ):
        try:
            return registry.save(who["id"], provider, intention, current, data.model_dump())
        except ValidationError:
            raise RuleError("VALIDATION_ERROR", "Confira os campos e limites da configuração.", 400) from None

    @app.post(prefix + "/integrations/{provider}/validate")
    def validate(
        provider: Literal["cloudflare", "jev"],
        data: ConfigAction,
        who=Depends(actor),
        current=Depends(version),
    ):
        return registry.validate(who["id"], provider, current, data.reason)

    @app.post(prefix + "/integrations/{provider}/activate")
    def activate(
        provider: Literal["cloudflare", "jev"],
        data: ConfigAction,
        who=Depends(actor),
        intention=Depends(key),
        current=Depends(version),
    ):
        return registry.activate(who["id"], provider, intention, current, data.reason)

    @app.post(prefix + "/integrations/{provider}/disable")
    def disable(
        provider: Literal["cloudflare", "jev"],
        data: ConfigAction,
        who=Depends(actor),
        intention=Depends(key),
        current=Depends(version),
    ):
        return registry.activate(who["id"], provider, intention, current, data.reason, disable=True)

    @app.get(prefix + "/templates")
    def list_templates(who=Depends(actor)):
        return templates.admin_list(who["id"])

    @app.post(prefix + "/templates")
    def save_template(data: TemplateInput, who=Depends(actor), intention=Depends(key)):
        return templates.save(who["id"], intention, data.model_dump())
