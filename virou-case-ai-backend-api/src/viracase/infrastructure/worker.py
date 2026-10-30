import json
import logging
import signal
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import and_, delete, or_, select, update

from viracase.application.generation import generated_files, generation_prompt
from viracase.application.progress import record_progress
from viracase.application.verification_progress import queue_verification_attempt
from viracase.domain.rules import RuleError, aware, cost, utcnow, validate_files
from viracase.infrastructure.agent_execution import execution_timeouts
from viracase.infrastructure.models import (
    Account,
    Attempt,
    Draft,
    Idempotency,
    Job,
    Operation,
    Period,
    Project,
    PublicationResource,
    PurgeInventory,
    RecommendationRun,
    Revision,
    new_id,
)
from viracase.infrastructure.provider_errors import report_rejection
from viracase.infrastructure.providers import ProviderRejected, ProviderUncertain
from viracase.infrastructure.publication_logging import emit, exception_fields, publication_context
from viracase.infrastructure.repository import project_dto
from viracase.infrastructure.workflow_logging import emit as emit_workflow

log = logging.getLogger("viracase.worker")


class Worker:
    def __init__(self, repository, gateway, catalog):
        self.repo, self.gateway, self.catalog = repository, gateway, catalog

    def recover(self):
        with self.repo.sessions() as db:
            candidates = db.execute(
                select(Job.id, Operation.account_id)
                .join(Operation, Operation.id == Job.operation_id)
                .where(Job.state == "running", Job.lease_until < utcnow())
                .limit(100)
            ).all()
        for jid, aid in candidates:
            with self.repo.sessions.begin() as db:
                self.repo.account(db, aid, True)
                job = db.scalar(select(Job).where(Job.id == jid).with_for_update())
                if job.state != "running" or aware(job.lease_until) >= utcnow():
                    continue
                op = db.get(Operation, job.operation_id)
                attempt = db.scalar(select(Attempt).where(Attempt.operation_id == op.id))
                if op.type == "publication_check" and op.result.get("creation_started"):
                    op.state, job.state, op.error_code = (
                        "reconciling",
                        "reconciling",
                        "VERIFICATION_RESUME_REQUIRED",
                    )
                    op.progress_stage = "verification_retained"
                elif op.type == "publication" and (
                    op.result.get("external_effect_pending")
                    or (
                        (publication_resource := db.get(PublicationResource, op.project_id))
                        and publication_resource.inventory.get("migration_in_progress")
                    )
                ):
                    from viracase.infrastructure.publication_recovery import recovery_status

                    op.state, job.state, op.progress_stage = (
                        "reconciling",
                        "reconciling",
                        "publication_reconciling",
                    )
                    op.result = {
                        **op.result,
                        "publication_recovery": recovery_status(
                            "scheduled", previous=op.result.get("publication_recovery"),
                            next_attempt_at=utcnow().isoformat(),
                        ),
                    }
                    job.available_at = utcnow()
                elif attempt and attempt.state == "confirmed":
                    self.repo.settle_operation(db, op, attempt.actual_brl or Decimal(0))
                    op.state, job.state, op.error_code = "failed", "done", "PROCESSING_FAILED"
                    op.progress_stage = "failed"
                elif attempt:
                    op.state, op.billing_status, op.progress_stage = (
                        "reconciling",
                        "reconciliation_pending",
                        "provider_result_unknown",
                    )
                    attempt.state, job.state = "uncertain", "reconciling"
                else:
                    if op.type == "publication_check":
                        queue_verification_attempt(op)
                    op.state, job.state = "queued", "queued"
                    op.progress_stage = "queued"
                record_progress(op, op.progress_stage)
                if op.type in {"template_preparation", "template_autofill"}:
                    emit_workflow("preparation_worker_recovered", "warning", operation=op,
                                  phase="recovery", state=op.state, error_code=op.error_code)
                elif op.type == "generation":
                    emit_workflow(
                        "generation_worker_recovered", "warning", operation=op,
                        phase="recovery", state=op.state, error_code=op.error_code,
                        transaction_state="pending_commit", secrets=self.repo.sources.secrets,
                    )
                if op.type == "template_preparation" and op.payload.get("automatic_apply") and op.state != "queued":
                    from viracase.infrastructure.build_workflow import apply_automatic

                    apply_automatic(self.repo, db, op)
                job.lease_token = None

    def tick(self, operation_id=None):
        self.recover()
        with self.repo.sessions.begin() as db:
            job = db.scalar(
                select(Job)
                .where(Job.state == "queued", Job.available_at <= utcnow())
                .where(Job.operation_id == operation_id if operation_id else True)
                .order_by(Job.available_at, Job.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not job:
                return False
            oid, token = job.operation_id, new_id()
            operation = db.get(Operation, oid)
            request_timeout = self.repo.config.provider_timeout
            if operation.type in {"generation", "template_preparation", "code_analysis"}:
                request_timeout, _ = execution_timeouts(operation.policy, request_timeout)
            claimed = db.execute(
                update(Job)
                .where(Job.id == job.id, Job.state == "queued")
                .values(
                    state="running",
                    lease_token=token,
                    lease_until=utcnow() + timedelta(seconds=request_timeout + 120),
                )
            )
            if claimed.rowcount != 1:
                return False
        try:
            with publication_context(operation_id=oid):
                self.execute(oid, token)
        except Exception as exc:
            emit("worker_failure", "error", operation_id=oid, **exception_fields(exc))
            with self.repo.sessions.begin() as db:
                op = db.get(Operation, oid)
                self.repo.account(db, op.account_id, True)
                job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
                if job.lease_token != token:
                    return True
                attempt = db.scalar(select(Attempt).where(Attempt.operation_id == oid))
                if attempt and attempt.state != "confirmed":
                    op.state, op.billing_status, job.state = (
                        "reconciling",
                        "reconciliation_pending",
                        "reconciling",
                    )
                else:
                    self.repo.settle_operation(db, op, attempt.actual_brl if attempt else Decimal(0))
                    op.state, job.state = "failed", "done"
                op.error_code = exc.code if isinstance(exc, RuleError) else "PROCESSING_FAILED"
                record_progress(op, op.state)
                if op.type in {"template_preparation", "template_autofill"}:
                    emit_workflow("preparation_worker_failed", "error", operation=op, error=exc,
                                  phase="worker", state=op.state, error_code=op.error_code)
                elif op.type == "generation":
                    emit_workflow(
                        "generation_worker_failed", "error", operation=op, error=exc,
                        phase="worker", state=op.state, error_code=op.error_code,
                        transaction_state="pending_commit", secrets=self.repo.sources.secrets,
                    )
                if op.type == "template_autofill" and op.state == "failed":
                    from viracase.infrastructure.build_workflow import fail_scheduled

                    fail_scheduled(self.repo, db, op, op.error_code)
                if op.type == "template_preparation" and op.payload.get("automatic_apply"):
                    from viracase.infrastructure.build_workflow import apply_automatic

                    apply_automatic(self.repo, db, op)
        return True

    def execute(self, oid, token):
        with self.repo.sessions() as db:
            current = db.get(Operation, oid)
            if current.type == "template_autofill":
                from viracase.infrastructure.build_workflow import execute_autofill

                return execute_autofill(self, oid, token)
            preparation = current.type == "template_preparation"
            if current.type == "project_deletion":
                from viracase.infrastructure.project_deletion import execute_deletion

                return execute_deletion(self, oid, token)
            if current.type == "publication_check":
                from viracase.infrastructure.publication_check import execute_check

                return execute_check(self, oid, token)
            if current.type == "publication":
                from viracase.infrastructure.publication_worker import execute_publication

                return execute_publication(self, oid, token)
            if current.type == "code_analysis":
                from viracase.infrastructure.code_analysis_worker import execute_code_analysis

                return execute_code_analysis(self, oid, token)
            if current.type == "generation" and current.policy.get("generation_contract_version") in {3, 4}:
                from viracase.infrastructure.conversation_generation import execute_conversation

                return execute_conversation(self, oid, token)
        if preparation:
            from viracase.infrastructure.preparation_worker import execute_preparation

            return execute_preparation(self, oid, token)
        with self.repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            a = self.repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return
            if op.cancel_requested:
                self.repo.settle_operation(db, op, Decimal(0))
                op.state, op.progress_stage, job.state = "cancelled", "cancelled_before_dispatch", "done"
                record_progress(op, op.progress_stage)
                return
            op.state, op.progress_stage = "running", "validating"
            record_progress(op, op.progress_stage)
        with self.repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            a = self.repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return
            db.refresh(op)
            if op.cancel_requested:
                self.repo.settle_operation(db, op, Decimal(0))
                op.state, job.state = "cancelled", "done"
                record_progress(op, "cancelled_before_dispatch")
                return
            self.repo.access(db, a, "export" if op.type == "export" else "generate")
            p = self.repo.owned_project(db, a.id, op.project_id)
            if op.type == "export":
                r = self.repo.owned_revision(db, a.id, p.id, op.payload["revision_id"])
                self.repo.sources.read(r, p.stack)
                op.result = {
                    **op.result,
                    "export_id": oid,
                    "revision_id": r.id,
                    "download_path": f"/api/v1/exports/{oid}/download",
                }
                op.state, op.progress_stage, job.state = "succeeded", "export_ready", "done"
                record_progress(op, op.progress_stage)
                return
            if self.repo.config.restore_hold:
                raise RuleError("RECOVERY_HOLD", "Aguardando reconciliação de restauração.")
            eligible = self.catalog.eligible()
            if not any(
                x["version"] == op.policy["version"] and x["model"] == op.policy["model"] for x in eligible
            ):
                raise RuleError(
                    "COMMERCIAL_CONFIGURATION_PENDING", "Catálogo desabilitado ou vencido antes do envio."
                )
            base = op.payload.get("base_revision_id")
            files = (
                self.repo.sources.read(self.repo.owned_revision(db, a.id, p.id, base), p.stack)
                if base
                else {}
            )
            prompt = generation_prompt(
                project_dto(p),
                op.payload["brief"],
                files,
                contract_version=op.policy.get("generation_contract_version", 1),
            )
            if len(prompt.encode()) + 4096 > op.policy["reserved_input_bound"]:
                raise RuleError("CONTEXT_CHANGED", "O contexto excede a reserva aceita.")
            policy = dict(op.policy)
            db.add(
                Attempt(operation_id=oid, provider=policy["provider"], model=policy["model"], state="sent")
            )
            record_progress(op, "provider_sent")
            request_timeout, _ = execution_timeouts(policy, self.repo.config.provider_timeout)
            job.lease_until = utcnow() + timedelta(seconds=request_timeout * 2 + 120)
        try:
            from viracase.infrastructure.integration_traces import observe_integration, trace_context

            with observe_integration(self.repo, trace_context(self.repo, op, "construction")):
                result = self.gateway.generate(policy, prompt)
        except ProviderRejected as exc:
            report_rejection(oid, exc)
            self.finish_error(oid, token, uncertain=False, error_code=exc.error_code)
            return
        except ProviderUncertain:
            self.finish_error(oid, token, uncertain=True)
            return
        with self.repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            a = self.repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return
            db.refresh(op)
            attempt = db.scalar(select(Attempt).where(Attempt.operation_id == oid))
            attempt.external_id, attempt.usage = result.external_id, result.usage
            actual = cost(policy, result.usage)
            attempt.actual_brl, attempt.state = actual, "confirmed"
            try:
                self.repo.settle_operation(db, op, actual)
            except RuleError:
                op.state, op.billing_status, op.error_code = (
                    "reconciling",
                    "reconciliation_pending",
                    "COST_DISCREPANCY",
                )
                job.state = "reconciling"
                record_progress(op, "reconciling")
                return
            if (
                job.lease_token != token
                or a.lifecycle in {"purging", "reconciling", "purged"}
                or "read" not in self.repo.access(db, a)["allowed_actions"]
            ):
                op.state, op.progress_stage, job.state = "failed", "result_discarded_after_retention", "done"
                op.payload = {}
                op.result = {}
                return
            if op.cancel_requested:
                op.state, job.state = "cancelled", "done"
                record_progress(op, "cancelled_after_dispatch")
                return
            record_progress(op, "validating_result")
        try:
            if result.output_error:
                raise RuleError(result.output_error, "Resposta incompleta.")
            next_files, changed_files = generated_files(result.text, files)
            validate_files(next_files, p.stack, self.repo.sources.secrets)
        except RuleError as exc:
            with self.repo.sessions.begin() as db:
                op = db.get(Operation, oid)
                self.repo.account(db, op.account_id, True)
                job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
                if job.lease_token != token:
                    return
                db.refresh(op)
                if op.cancel_requested:
                    op.state, job.state = "cancelled", "done"
                    record_progress(op, "cancelled_after_dispatch")
                    return
                op.state, op.error_code, job.state = "failed", exc.code, "done"
                record_progress(op, "invalid_generated_files")
                log.warning(
                    json.dumps(
                        {"operation_id": oid, "event": "generation_validation_failed", "error_code": exc.code}
                    )
                )
            return
        with self.repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            self.repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return
            record_progress(op, "saving_revision")
        with self.repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            a = self.repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return
            db.refresh(op)
            if op.cancel_requested:
                op.state, job.state = "cancelled", "done"
                record_progress(op, "cancelled_after_dispatch")
                return
            if (
                a.lifecycle in {"purging", "reconciling", "purged"}
                or "read" not in self.repo.access(db, a)["allowed_actions"]
            ):
                op.state, job.state = "failed", "done"
                op.payload, op.result = {}, {}
                op.progress_stage = "result_discarded_after_retention"
                return
            try:
                p = self.repo.owned_project(db, a.id, op.project_id)
                revision = self.repo.add_revision(
                    db, p, next_files, "generation", op.payload.get("base_revision_id")
                )
                op.result = {
                    **op.result,
                    "revision_id": revision.id,
                    "candidate": revision.candidate,
                    "changed_files": changed_files,
                    "validation": "structure_checked",
                    **revision.migration_evidence,
                }
                op.state, op.progress_stage = "succeeded", "revision_ready"
            except (ValueError, KeyError, TypeError, RuleError) as exc:
                op.state, op.progress_stage = "failed", "invalid_generated_files"
                op.error_code = exc.code if isinstance(exc, RuleError) else "INVALID_GENERATION"
            record_progress(op, op.progress_stage)
            job.state = "done"

    def finish_error(self, oid, token, uncertain, error_code="PROVIDER_REJECTED"):
        with self.repo.sessions.begin() as db:
            op = db.get(Operation, oid)
            self.repo.account(db, op.account_id, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job.lease_token != token:
                return
            attempt = db.scalar(select(Attempt).where(Attempt.operation_id == oid))
            if uncertain:
                op.state, op.billing_status, op.progress_stage, job.state, attempt.state = (
                    "reconciling",
                    "reconciliation_pending",
                    "provider_result_unknown",
                    "reconciling",
                    "uncertain",
                )
            else:
                self.repo.settle_operation(db, op, Decimal(0))
                op.state, op.progress_stage, job.state, attempt.state = (
                    "failed",
                    "provider_rejected",
                    "done",
                    "rejected",
                )
            op.error_code = "PROVIDER_RESULT_UNKNOWN" if uncertain else error_code
            record_progress(op, op.progress_stage)

    def lifecycle_tick(self):
        if not self.repo.config.lifecycle_enabled:
            return 0
        from viracase.infrastructure.publication_lifecycle import synchronize_publication_visibility
        from viracase.infrastructure.publication_recovery import recover_publications

        recover_publications(self.repo)
        synchronize_publication_visibility(self.repo)
        with self.repo.sessions() as db:
            ids = list(
                db.scalars(
                    select(Account.id)
                    .join(Period, Period.id == Account.current_period_id)
                    .where(Period.purge_at <= utcnow(), Account.lifecycle.in_(["active", "purging"]))
                )
            )
        for aid in ids:
            self.purge(aid)
        return len(ids)

    def purge(self, aid):
        with self.repo.sessions.begin() as db:
            a = self.repo.account(db, aid, True)
            period = self.repo.period(db, a)
            if not period or aware(period.purge_at) > utcnow() or a.lifecycle == "purged":
                return
            if a.lifecycle == "active":
                a.lifecycle, a.fence = "purging", new_id()
                for destination in (
                    "source_volume",
                    "operational_database",
                    "exports_memory_only",
                    "cloudflare_resources",
                    "backups_not_managed",
                ):
                    db.add(PurgeInventory(account_id=aid, fence=a.fence, destination=destination))
            fence = a.fence
            for op in db.scalars(
                select(Operation).where(Operation.account_id == aid, Operation.state == "queued")
            ):
                self.repo.settle_operation(db, op, Decimal(0))
                op.state, op.progress_stage = "cancelled", "retention_expired"
                db.scalar(select(Job).where(Job.operation_id == op.id)).state = "done"
        from viracase.infrastructure.publication_lifecycle import purge_publications

        with self.repo.sessions() as db:
            in_flight = db.scalar(
                select(Job.id)
                .join(Operation, Operation.id == Job.operation_id)
                .where(
                    Operation.account_id == aid,
                    Operation.type.in_(["publication", "project_deletion"]),
                    or_(
                        Job.state == "running",
                        and_(Job.state == "reconciling", Job.lease_token.is_not(None), Job.lease_until > utcnow()),
                    ),
                )
                .limit(1)
            )
            if in_flight:
                return
        cloudflare_evidence = purge_publications(self.repo, aid, fence)
        evidence = self.repo.sources.remove_account(aid)
        with self.repo.sessions.begin() as db:
            a = self.repo.account(db, aid, True)
            if a.fence != fence:
                raise RuleError("LIFECYCLE_FENCE_CONFLICT", "Lifecycle em reconciliação.")
            for op in db.scalars(select(Operation).where(Operation.account_id == aid)):
                op.payload, op.result, op.project_id = {}, {}, None
            db.flush()
            db.execute(delete(RecommendationRun).where(RecommendationRun.account_id == aid))
            from viracase.infrastructure.integration_traces import TraceStore

            TraceStore(self.repo).purge_project(db, account_id=aid)
            db.execute(delete(PublicationResource).where(PublicationResource.account_id == aid))
            db.execute(delete(Draft).where(Draft.account_id == aid))
            db.execute(delete(Revision).where(Revision.account_id == aid))
            db.execute(delete(Project).where(Project.account_id == aid))
            for intent in db.scalars(select(Idempotency).where(Idempotency.actor_id == aid)):
                intent.result = {"expired": True}
            records = db.scalars(
                select(PurgeInventory).where(PurgeInventory.account_id == aid, PurgeInventory.fence == fence)
            ).all()
            for record in records:
                if record.destination == "source_volume":
                    record.state, record.evidence = "confirmed", evidence
                elif record.destination == "cloudflare_resources":
                    record.state, record.evidence = "confirmed", cloudflare_evidence
                elif record.destination == "backups_not_managed":
                    record.state, record.evidence = (
                        "requires_operator_evidence",
                        {"reason": "External host backups must be inventoried and verified."},
                    )
                else:
                    record.state, record.evidence = (
                        "confirmed",
                        {"checked_at": utcnow().isoformat(), "local_cleanup_confirmed": True},
                    )
            a.lifecycle = "reconciling"


def main():
    from viracase.infrastructure.composition import compose

    config, engine, repo, catalog, gateway = compose()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    worker = Worker(repo, gateway, catalog)
    from viracase.infrastructure.runtime_monitor import start_monitor

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    monitor = start_monitor(repo, stop)
    while not stop.is_set():
        try:
            active = worker.tick()
            worker.lifecycle_tick()
            Path("/tmp/viroucaseai-worker-heartbeat").touch()
            stop.wait(0.2 if active else 2)
        except Exception as exc:
            emit("worker_loop_failure", "error", **exception_fields(exc))
            stop.wait(5)
    monitor.join(timeout=10)
    engine.dispose()


if __name__ == "__main__":
    main()
