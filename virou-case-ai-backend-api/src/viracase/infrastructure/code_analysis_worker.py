import json
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from viracase.application.code_analysis import analysis_prompt, parse_analysis
from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, cost, utcnow, validate_file_entries
from viracase.infrastructure.agent_execution import execution_timeouts
from viracase.infrastructure.code_analysis import diagnostic_text
from viracase.infrastructure.integration_traces import observe_integration, trace_context
from viracase.infrastructure.models import Attempt, Job, Operation
from viracase.infrastructure.providers import ProviderRejected, ProviderUncertain
from viracase.infrastructure.publication_migration_context import has_restorable_missing_migration
from viracase.infrastructure.workflow_logging import emit


def execute_code_analysis(worker, oid, token):
    repo = worker.repo
    with repo.sessions.begin() as db:
        op = db.get(Operation, oid)
        account = repo.account(db, op.account_id, True)
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        if job.lease_token != token:
            return
        if op.cancel_requested:
            repo.settle_operation(db, op, Decimal(0))
            op.state, job.state = "cancelled", "done"
            record_progress(op, "cancelled_before_dispatch")
            return
        repo.access(db, account, "generate")
        if repo.config.restore_hold:
            raise RuleError("RECOVERY_HOLD", "Aguardando reconciliação de restauração.")
        project = repo.owned_project(db, account.id, op.project_id)
        revision = repo.owned_revision(db, account.id, project.id, op.payload["revision_id"])
        if revision.source_hash != op.payload["source_hash"]:
            raise RuleError("ANALYSIS_OUTDATED", "A fonte não corresponde à revisão analisada.")
        if not any(
            item["version"] == op.policy["version"]
            and item["model"] == op.policy["model"]
            and item.get("connection_id") == op.policy.get("connection_id")
            for item in worker.catalog.eligible()
        ):
            raise RuleError("COMMERCIAL_CONFIGURATION_PENDING", "A integração de IA mudou antes do envio.")
        files = repo.sources.read(revision, project.stack)
        policy = dict(op.policy)
        prompt = analysis_prompt(
            op.payload["project_snapshot"], files, op.payload["evidence"], policy["reserved_input_bound"]
        )
        op.state = "running"
        record_progress(op, "analysis_running")
        db.add(Attempt(operation_id=oid, provider=policy["provider"], model=policy["model"], state="sent"))
        timeout, _ = execution_timeouts(policy, repo.config.provider_timeout)
        job.lease_until = utcnow() + timedelta(seconds=timeout * 2 + 120)
        emit("code_analysis_dispatched", operation=op, phase="provider", secrets=repo.sources.secrets)
    try:
        with observe_integration(repo, trace_context(repo, op, "review")):
            result = worker.gateway.generate(policy, prompt)
    except ProviderRejected as exc:
        worker.finish_error(oid, token, uncertain=False, error_code=exc.error_code)
        return
    except ProviderUncertain:
        worker.finish_error(oid, token, uncertain=True)
        return
    with repo.sessions.begin() as db:
        op = db.get(Operation, oid)
        account = repo.account(db, op.account_id, True)
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        if job.lease_token != token:
            return
        attempt = db.scalar(select(Attempt).where(Attempt.operation_id == oid))
        actual = cost(policy, result.usage)
        attempt.external_id, attempt.usage = result.external_id, result.usage
        attempt.actual_brl, attempt.state = actual, "confirmed"
        try:
            repo.settle_operation(db, op, actual)
        except RuleError:
            op.state, op.billing_status, op.error_code, job.state = (
                "reconciling",
                "reconciliation_pending",
                "COST_DISCREPANCY",
                "reconciling",
            )
            record_progress(op, "reconciling")
            return
        if (
            account.lifecycle in {"purging", "reconciling", "purged"}
            or "read" not in repo.access(db, account)["allowed_actions"]
        ):
            op.state, job.state, op.payload, op.result = "failed", "done", {}, {}
            record_progress(op, "result_discarded_after_retention")
            return
        if op.cancel_requested:
            op.state, job.state = "cancelled", "done"
            record_progress(op, "cancelled_after_dispatch")
            return
        try:
            if result.output_error:
                raise RuleError(result.output_error, "Resposta da análise incompleta.")
            report = parse_analysis(result.text, files)
            report["summary"] = diagnostic_text(report["summary"], repo.sources.secrets)
            for issue in report["issues"]:
                for name in ("title", "explanation", "suggestion"):
                    issue[name] = diagnostic_text(
                        issue[name], repo.sources.secrets, 160 if name == "title" else 2000
                    )
            report["limitations"] = [
                diagnostic_text(value, repo.sources.secrets, 500) for value in report["limitations"]
            ]
            report["limitations"].append(
                "Análise estática por IA; nenhuma execução, teste ou publicação foi realizada nesta análise."
            )
            if op.payload["evidence"].get("publication", {}).get("matches_revision") is False:
                report["limitations"].append(
                    "A publicação informada usou outra revisão; seu erro é contexto histórico, não evidência sobre o código atual."
                )
            validate_file_entries(
                {"analysis.json": json.dumps(report, ensure_ascii=False)}, repo.sources.secrets
            )
            missing_migration = has_restorable_missing_migration(op.payload["evidence"], files)
            op.result = {
                **op.result,
                **report,
                "can_request_correction": any(
                    (issue["path"] or missing_migration) and issue["severity"] in {"error", "warning"}
                    for issue in report["issues"]
                ),
            }
            op.state, job.state = "succeeded", "done"
            record_progress(op, "analysis_ready")
            emit("code_analysis_finished", operation=op, phase="validation", secrets=repo.sources.secrets)
        except RuleError as exc:
            op.state, op.error_code, job.state = "failed", exc.code, "done"
            record_progress(op, "analysis_failed")
            emit(
                "code_analysis_failed",
                "error",
                operation=op,
                error=exc,
                phase="validation",
                secrets=repo.sources.secrets,
            )
