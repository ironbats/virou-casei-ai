from sqlalchemy import func, select

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Job, Operation, PublicationResource
from viracase.infrastructure.observability import WorkerBuildStatus
from viracase.infrastructure.publication_qualification import qualified
from viracase.infrastructure.repository import operation_dto

MESSAGES = {
    "CONFIGURATION_PENDING": "Configuração da hospedagem pendente. Fale com o responsável.",
    "CONFIGURATION_INVALID": "A configuração da hospedagem precisa ser validada no backoffice.",
    "CAPABILITY_UNVALIDATED": "A publicação aguarda a verificação operacional na conexão Cloudflare do backoffice.",
    "EXECUTOR_UNAVAILABLE": "O executor isolado de publicação está indisponível. Fale com o responsável.",
    "UNSUPPORTED_STACK": "Este perfil ainda não foi habilitado para publicação.",
    "ARTIFACT_NOT_READY": "Salve uma revisão válida antes de publicar.",
    "ACCESS_EXPIRED": "Seu acesso não permite novas publicações. Fale com o responsável.",
    "OPERATION_CONFLICT": "Há uma publicação em andamento ou em verificação neste projeto.",
    "QUOTA_EXCEEDED": "A capacidade de hospedagem está esgotada. Fale com o responsável.",
    "LIFECYCLE_LOCKED": "A hospedagem está em manutenção ou encerramento de acesso.",
}


class PublicationService:
    def __init__(self, repo, builder=None):
        self.repo, self.registry = repo, Integrations(repo)
        self.builder = builder or WorkerBuildStatus(repo)

    def configuration_reasons(self, row, stack=None):
        reasons = []
        if not row:
            return ["CONFIGURATION_PENDING"]
        if row.state != "active" or not self.registry.fresh(row) or not row.validation.get("ready"):
            reasons.append("CONFIGURATION_INVALID")
        settings = row.settings
        if not self.builder.fingerprint() or not qualified(row, stack, self.builder.fingerprint()):
            reasons.append("CAPABILITY_UNVALIDATED")
        if stack and stack not in settings["allowed_stacks"]:
            reasons.append("UNSUPPORTED_STACK")
        if not self.builder.available():
            reasons.append("EXECUTOR_UNAVAILABLE")
        if stack and hasattr(self.builder, "supports") and not self.builder.supports(stack):
            reasons.append("UNSUPPORTED_STACK")
        if self.repo.config.restore_hold or not self.repo.config.lifecycle_enabled:
            reasons.append("LIFECYCLE_LOCKED")
        return list(dict.fromkeys(reasons))

    def capability(self):
        with self.repo.sessions() as db:
            rows = self.registry.current_rows(db, "cloudflare").values()
            reasons = (
                []
                if any(not self.configuration_reasons(row) for row in rows)
                else self.configuration_reasons(self.registry.latest(db, "cloudflare"))
            )
            return {
                "enabled": not reasons,
                "reason_code": reasons[0] if reasons else None,
                "message": MESSAGES[reasons[0]] if reasons else "Publicação disponível.",
            }

    def readiness(self, actor, pid, revision=None):
        with self.repo.sessions() as db:
            a = self.repo.account(db, actor)
            self.repo.access(db, a, "read")
            p = self.repo.owned_project(db, actor, pid)
            return self.inspect(db, a, p, revision or p.current_revision_id)

    def inspect(self, db, account, project, revision, exclude_operation=None):
        row = self.registry.for_project(db, project.id)
        reasons = self.configuration_reasons(row, project.stack)
        if "publish" not in self.repo.access(db, account)["allowed_actions"]:
            reasons.append("ACCESS_EXPIRED")
        if account.lifecycle != "active":
            reasons.append("LIFECYCLE_LOCKED")
        if not revision:
            reasons.append("ARTIFACT_NOT_READY")
        else:
            r = self.repo.owned_revision(db, account.id, project.id, revision)
            if r.candidate:
                reasons.append("ARTIFACT_NOT_READY")
        query = select(Operation.id).where(
            Operation.project_id == project.id,
            Operation.type == "publication",
            Operation.state.in_(["queued", "running", "reconciling"]),
        )
        if exclude_operation:
            query = query.where(Operation.id != exclude_operation)
        if db.scalar(query):
            reasons.append("OPERATION_CONFLICT")
        resource = db.get(PublicationResource, project.id)
        if row:
            count = db.scalar(
                select(func.count())
                .select_from(PublicationResource)
                .where(
                    PublicationResource.remote_account_id == row.settings["account_id"],
                    PublicationResource.state != "purged",
                )
            )
            observed_ids = set(row.validation.get("database_ids", []))
            unobserved = db.scalar(
                select(func.count())
                .select_from(PublicationResource)
                .where(
                    PublicationResource.remote_account_id == row.settings["account_id"],
                    PublicationResource.state != "purged",
                    (PublicationResource.database_id.is_(None))
                    | (PublicationResource.database_id.not_in(observed_ids)),
                )
            )
            if not resource and (
                count >= row.settings["max_projects"]
                or len(observed_ids) + unobserved >= 10
                or row.validation.get("workers_count", 100) + unobserved >= 100
            ):
                reasons.append("QUOTA_EXCEEDED")
            if resource and resource.remote_account_id != row.settings["account_id"]:
                reasons.append("CONFIGURATION_INVALID")
        return {
            "can_publish": not reasons,
            "revision_id": revision,
            "configuration_version": row.version if row else None,
            "reasons": [{"code": code, "message": MESSAGES[code]} for code in dict.fromkeys(reasons)],
            "publication": {
                "url": resource.inventory.get("url"),
                "revision_id": resource.inventory.get("published_revision_id"),
                "published_at": resource.inventory.get("published_at"),
            }
            if resource
            else None,
        }

    def start(self, actor, pid, key, payload):
        def command(db, a):
            project = self.repo.owned_project(db, actor, pid)
            configuration_lock(db, "integration:cloudflare")
            row = self.registry.for_project(db, pid)
            status = self.inspect(db, a, project, payload["revision_id"])
            if not status["can_publish"]:
                reason = status["reasons"][0]
                code = (
                    "CAPABILITY_UNVALIDATED"
                    if reason["code"]
                    in {"CONFIGURATION_PENDING", "CONFIGURATION_INVALID", "EXECUTOR_UNAVAILABLE"}
                    else reason["code"]
                )
                raise RuleError(code, reason["message"])
            concurrent = db.scalar(
                select(func.count())
                .select_from(Operation)
                .join(PublicationResource, PublicationResource.project_id == Operation.project_id)
                .where(
                    Operation.type == "publication",
                    Operation.state.in_(["queued", "running", "reconciling"]),
                    PublicationResource.remote_account_id == row.settings["account_id"],
                )
            )
            if concurrent >= row.settings["max_concurrent"]:
                raise RuleError("QUOTA_EXCEEDED", "Aguarde a publicação em andamento antes de tentar.", 429)
            revision = self.repo.owned_revision(db, actor, pid, payload["revision_id"])
            resource = db.get(PublicationResource, pid)
            if not resource:
                resource = PublicationResource(
                    project_id=pid,
                    account_id=actor,
                    config_id=row.id,
                    remote_account_id=row.settings["account_id"],
                    worker_name=row.settings["worker_prefix"] + "-" + pid.replace("-", ""),
                    inventory={},
                    state="reserved",
                )
                db.add(resource)
            op = Operation(
                account_id=actor,
                project_id=pid,
                type="publication",
                policy={"config_id": row.id, "version": row.version},
                payload={
                    **payload,
                    "source_hash": revision.source_hash,
                    "entitlement_version": a.entitlement_version,
                },
            )
            db.add(op)
            db.flush()
            record_progress(op, "queued")
            db.add(Job(operation_id=op.id))
            db.flush()
            return operation_dto(op)

        result = self.repo.mutate(actor, f"publication:{pid}", key, payload, command, "publish")
        return self.repo.operation(actor, result["id"])
