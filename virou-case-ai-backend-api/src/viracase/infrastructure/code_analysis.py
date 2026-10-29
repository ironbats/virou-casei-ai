import re
import unicodedata

from sqlalchemy import select

from viracase.application.code_analysis import analysis_prompt
from viracase.application.conversation import INPUT_OVERHEAD, MAX_GENERATION_CONTEXT_BYTES
from viracase.application.progress import progress_message
from viracase.domain.rules import RuleError
from viracase.infrastructure.agent_execution import freeze_execution
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Operation
from viracase.infrastructure.publication_build_diagnostics import clean_message, diagnostic_location
from viracase.infrastructure.publication_logging import safe_text
from viracase.infrastructure.publication_migration_context import published_migration_context
from viracase.infrastructure.publication_verification import migration_diagnostic
from viracase.infrastructure.repository_dto import operation_dto, project_dto


def diagnostic_text(value, secrets=(), limit=4000):
    value = safe_text(value, secrets, limit=20000)
    value = "".join(
        char for char in value if unicodedata.category(char) not in {"Cf", "Cc"} or char in "\n\t"
    )
    value = re.sub(r"(?i)\b(?:https?|postgres(?:ql)?|mysql|redis)://\S+", "[URL]", value)
    return value.strip()[:limit]


def analysis_reference(payload):
    return {
        "kind": "code_analysis",
        "revision_id": payload["revision_id"],
        "source": payload["source"],
        "publication_operation_id": payload.get("publication_operation_id"),
        "can_request_correction": False,
    }


def owned_analysis(db, actor, pid, oid):
    operation = db.scalar(
        select(Operation)
        .where(
            Operation.id == oid,
            Operation.account_id == actor,
            Operation.project_id == pid,
            Operation.type == "code_analysis",
        )
        .with_for_update()
    )
    if not operation:
        raise RuleError("NOT_FOUND", "Análise indisponível.", 404)
    return operation


def correction_operation(db, analysis):
    identity = analysis.result.get("correction_operation_id")
    if not identity:
        return None
    operation = db.scalar(
        select(Operation).where(
            Operation.id == identity,
            Operation.account_id == analysis.account_id,
            Operation.project_id == analysis.project_id,
            Operation.type == "generation",
        )
    )
    if not operation or operation.payload.get("analysis_id") != analysis.id:
        raise RuleError(
            "ANALYSIS_CORRECTION_UNAVAILABLE", "A correção desta análise não está disponível.", 409
        )
    return operation


def correction_retryable(operation):
    return operation.state in {"failed", "cancelled"} and operation.billing_status != "reconciliation_pending"


def correction_context(analysis):
    return {
        "analysis_id": analysis.id,
        "revision_id": analysis.payload["revision_id"],
        "source_hash": analysis.payload["source_hash"],
        "diagnosis": {
            "summary": analysis.result["summary"][:2000],
            "issues": [
                {**item, "explanation": item["explanation"][:250], "suggestion": item["suggestion"][:400]}
                for item in analysis.result["issues"]
            ],
            "limitations": analysis.result["limitations"][:10],
        },
        "evidence": {
            key: value for key, value in analysis.payload.get("evidence", {}).items()
            if key != "published_database_migrations"
        },
    }


def validate_correction(repo, db, actor, project, payload):
    analysis = owned_analysis(db, actor, project.id, payload["analysis_id"])
    previous = correction_operation(db, analysis)
    reply_to = payload.get("reply_to")
    if reply_to:
        parent = db.get(Operation, reply_to)
        if (
            not parent
            or parent.account_id != actor
            or parent.project_id != project.id
            or parent.type != "generation"
            or parent.payload.get("analysis_id") != analysis.id
            or parent.state != "succeeded"
            or parent.result.get("kind") != "clarification"
        ):
            raise RuleError("NOT_FOUND", "Pergunta indisponível nesta conversa.", 404)
        if not previous or not (
            previous.id == parent.id
            or previous.payload.get("reply_to") == parent.id and correction_retryable(previous)
        ):
            raise RuleError(
                "CLARIFICATION_ANSWERED",
                "Esta pergunta já recebeu uma resposta. Acompanhe a continuação no chat.",
                409,
            )
    elif previous and not correction_retryable(previous):
        return previous
    if analysis.state != "succeeded" or not analysis.result.get("can_request_correction"):
        raise RuleError(
            "ANALYSIS_NOT_ACTIONABLE", "A análise não identificou uma correção de código disponível.", 409
        )
    if (
        payload["base_revision_id"] != analysis.payload["revision_id"]
        or project.current_revision_id != analysis.payload["revision_id"]
    ):
        raise RuleError(
            "ANALYSIS_OUTDATED",
            "O código mudou depois da análise. Analise a revisão atual antes de solicitar a correção.",
            409,
        )
    revision = repo.owned_revision(db, actor, project.id, project.current_revision_id)
    if revision.source_hash != analysis.payload["source_hash"]:
        raise RuleError("ANALYSIS_OUTDATED", "A fonte analisada não corresponde mais à revisão atual.", 409)
    return None


class CodeAnalysis:
    def __init__(self, repo, catalog):
        self.repo, self.catalog = repo, catalog

    def start(self, actor, pid, key, request):
        request = {
            **request,
            "runtime_error": diagnostic_text(request.get("runtime_error"), self.repo.sources.secrets) or None,
        }
        previous = self.repo.recover_intention(actor, f"code_analysis:{pid}", key, request)
        if previous:
            return self.repo.operation(actor, previous["id"])
        if request["policy_version"] != "2026-10-v1":
            raise RuleError("POLICY_CHANGED", "Atualize a página para carregar a política vigente.")
        self.repo.rate_limit("code-analysis:" + actor, 20, 3600)
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "generate")
            project = self.repo.owned_project(db, actor, pid)
            revision = self.repo.owned_revision(db, actor, pid, request["revision_id"])
            files = self.repo.sources.read(revision, project.stack)
            evidence = {"source": request["source"], "runtime_error": request.get("runtime_error")}
            publication_id = request.get("publication_operation_id")
            if publication_id:
                publication = db.scalar(
                    select(Operation).where(
                        Operation.id == publication_id,
                        Operation.account_id == actor,
                        Operation.project_id == pid,
                        Operation.type == "publication",
                    )
                )
                if not publication:
                    raise RuleError("NOT_FOUND", "Publicação indisponível.", 404)
                failure = publication.result.get("publication_failure")
                failure = failure if isinstance(failure, dict) else {}
                build = failure.get("build")
                build = build if isinstance(build, dict) else {}
                build_issues = build.get("issues")
                build_issues = build_issues if isinstance(build_issues, list) else []
                matches = publication.payload.get("revision_id") == revision.id
                evidence["publication"] = {
                    "revision_id": publication.payload.get("revision_id"),
                    "matches_revision": matches,
                    "state": publication.state,
                    "stage": clean_message(failure.get("stage") or publication.progress_stage, self.repo.sources.secrets),
                    "code": clean_message(publication.error_code or failure.get("code"), self.repo.sources.secrets),
                    "message": clean_message(failure.get("message") or progress_message(publication), self.repo.sources.secrets),
                    "category": clean_message(build.get("category") or "publication", self.repo.sources.secrets),
                    "issues": [
                        {
                            "code": clean_message(item.get("code"), self.repo.sources.secrets),
                            "message": clean_message(item.get("message"), self.repo.sources.secrets),
                            "hint": clean_message(item.get("hint"), self.repo.sources.secrets),
                            **(
                                diagnostic_location(item, files, self.repo.sources.secrets) if matches else {}
                            ),
                        }
                        for item in build_issues[:12]
                        if isinstance(item, dict)
                    ],
                }
                migration = migration_diagnostic(failure.get("migration"), self.repo.sources.secrets)
                if migration:
                    evidence["publication"]["migration"] = migration
                    evidence["publication"]["category"] = "migration"
            migrations = published_migration_context(self.repo, db, project, files)
            if migrations:
                evidence["published_database_migrations"] = migrations
            project_info, source_hash = project_dto(project), revision.source_hash
        settings = Integrations(self.repo).build_policy()
        input_bound = (
            min(settings["max_generation_context_bytes"], MAX_GENERATION_CONTEXT_BYTES) + INPUT_OVERHEAD
        )
        prompt = analysis_prompt(project_info, files, evidence, input_bound)
        model_id = settings.get("generation_model_id")
        policy, ceiling = (
            self.catalog.resolve(prompt, model_id=model_id) if model_id else self.catalog.resolve(prompt)
        )
        policy = {**freeze_execution(policy, self.repo.config.provider_timeout), "code_analysis_contract": 1}
        payload = {
            **request,
            "analysis_request": request,
            "base_revision_id": request["revision_id"],
            "source_hash": source_hash,
            "project_snapshot": project_info,
            "evidence": evidence,
        }
        return self.repo.create_operation(actor, pid, "code_analysis", key, payload, policy, ceiling)

    def correct(self, actor, pid, oid, key, request, generation):
        intention = {"analysis_id": oid, **request}
        recovered = self.repo.recover_intention(actor, f"generation:{pid}", key, intention)
        if recovered:
            return self.repo.operation(actor, recovered["id"])
        reuse = None
        with self.repo.sessions.begin() as db:
            self.repo.access(db, self.repo.account(db, actor, True), "generate")
            project = self.repo.owned_project(db, actor, pid)
            analysis = owned_analysis(db, actor, pid, oid)
            previous = correction_operation(db, analysis)
            if previous and not correction_retryable(previous):
                reuse = previous.id
            else:
                validate_correction(
                    self.repo,
                    db,
                    actor,
                    project,
                    {
                        "analysis_id": oid,
                        "base_revision_id": request["base_project_revision"],
                    },
                )
            context = correction_context(analysis)
        if reuse:

            def recover(db, account):
                self.repo.owned_project(db, actor, pid)
                operation = db.get(Operation, reuse)
                return operation_dto(operation)

            return self.repo.mutate(actor, f"generation:{pid}", key, intention, recover, "generate")
        brief = (
            "Corrija os problemas de código identificados na análise desta revisão. "
            "Preserve os comportamentos existentes e prepare uma proposta para eu revisar."
        )
        construction = Integrations(self.repo).build_configuration()
        settings = construction["settings"]
        return generation.start(
            actor,
            pid,
            key,
            {
                "brief": brief,
                "base_revision_id": request["base_project_revision"],
                "policy_version": request["policy_version"],
                "analysis_id": oid,
                "analysis_context": context,
                "reply_request": intention,
                "review_required": True,
                "auto_apply": False,
                "context_limit_bytes": settings["max_context_bytes"],
                "generation_context_limit_bytes": settings["max_generation_context_bytes"],
                "construction_configuration": construction,
            },
            contract_version=4,
        )
