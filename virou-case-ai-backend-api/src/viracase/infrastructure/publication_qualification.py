from datetime import datetime
from typing import Literal

from pydantic import Field

from viracase.domain.rules import RuleError, aware, canonical_hash, utcnow
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Audit
from viracase.interfaces.schemas import Input, Reason

REQUIRED_CHECKS = frozenset(
    {
        "executor_no_network",
        "executor_no_host_secrets",
        "executor_no_cross_project_files",
        "executor_resource_limits",
        "worker_no_cross_tenant_binding",
        "worker_no_platform_credentials",
        "free_plan_confirmed",
        "capacity_and_abuse_limits",
        "frontend_api_d1_read_write",
        "d1_migration_timeout_recovery",
        "restart_without_duplicate_resources",
        "suspension_verified",
        "purge_verified",
        "renewal_race_fenced",
        "backup_retention_reviewed",
    }
)


class QualificationCheck(Input):
    code: str = Field(max_length=80)
    passed: bool
    expected: str = Field(min_length=10, max_length=1000)
    observed: str = Field(min_length=10, max_length=1000)
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class QualificationReport(Input):
    format_version: Literal[1]
    configuration_id: str = Field(max_length=36)
    configuration_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    account_id: str = Field(pattern=r"^[a-fA-F0-9]{32}$")
    tested_at: datetime
    expires_at: datetime
    runner_version: str = Field(min_length=5, max_length=100)
    runner_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    executor_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    stacks: list[Literal["hono-typescript-d1", "fastapi-python-d1"]] = Field(min_length=1, max_length=2)
    checks: list[QualificationCheck] = Field(min_length=len(REQUIRED_CHECKS), max_length=30)
    report_reference: str = Field(min_length=10, max_length=300)
    reason: Reason


def qualified(row, stack=None, executor_digest=None):
    from viracase.infrastructure.publication_check import runtime_verified

    if row.validation.get("publication_check_blocked"):
        return False
    blocked_stacks = row.validation.get("publication_check_blocked_stacks", [])
    if stack and stack in blocked_stacks:
        return False
    if not stack and blocked_stacks:
        return any(
            qualified(row, candidate, executor_digest)
            for candidate in row.settings["allowed_stacks"]
            if candidate not in blocked_stacks
        )
    if runtime_verified(row, stack, executor_digest):
        return True
    evidence = row.validation.get("qualification", {})
    try:
        report = QualificationReport.model_validate(evidence["report"])
        checks = {check.code for check in report.checks if check.passed}
        return (
            evidence["digest"] == canonical_hash(report.model_dump(mode="json"))
            and report.configuration_id == row.id
            and report.configuration_digest == canonical_hash(row.settings)
            and report.account_id == row.settings["account_id"]
            and (executor_digest is None or report.executor_digest == executor_digest)
            and aware(report.tested_at) <= utcnow() < aware(report.expires_at)
            and REQUIRED_CHECKS <= checks
            and (not stack or stack in report.stacks)
        )
    except (ValueError, KeyError, TypeError):
        return False


def record_qualification(repo, actor, key, version, payload, connection_id=None):
    registry = Integrations(repo)

    def command(db, operator):
        registry.require_admin(operator)
        configuration_lock(db, "integration:cloudflare")
        row = registry.latest(db, "cloudflare", connection_id=connection_id)
        if not row or row.version != version:
            raise RuleError("VERSION_CONFLICT", "Atualize a configuração antes de registrar evidências.", 412)
        report = QualificationReport.model_validate(payload)
        if (
            row.state not in {"validated", "active"}
            or not row.validation.get("ready")
            or report.configuration_id != row.id
            or report.configuration_digest != canonical_hash(row.settings)
            or report.account_id != row.settings["account_id"]
        ):
            raise RuleError(
                "QUALIFICATION_INVALID", "O relatório deve identificar esta versão validada e sua conta."
            )
        if (
            aware(report.tested_at) > utcnow()
            or aware(report.expires_at) <= utcnow()
            or aware(report.tested_at) < aware(row.created_at)
        ):
            raise RuleError("QUALIFICATION_INVALID", "Confira a data dos testes e a validade do relatório.")
        if len({check.code for check in report.checks}) != len(report.checks) or not REQUIRED_CHECKS <= {
            check.code for check in report.checks
        }:
            raise RuleError(
                "QUALIFICATION_INVALID",
                "O relatório não cobre os cenários obrigatórios, ou contém duplicatas.",
            )
        data = report.model_dump(mode="json")
        row.validation = {
            **row.validation,
            "qualification": {
                "report": data,
                "digest": canonical_hash(data),
                "reviewed_by": actor,
                "reviewed_at": utcnow().isoformat(),
            },
        }
        db.add(
            Audit(
                actor_id=actor, action="publication:qualification", resource_id=row.id, reason=report.reason
            )
        )
        return {"qualified": qualified(row), "digest": canonical_hash(data)}

    return repo.mutate(actor, "publication:qualification", key, {"version": version, **payload}, command)
