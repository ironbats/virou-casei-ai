from datetime import timedelta

from sqlalchemy import select

from viracase.application.progress import record_progress
from viracase.domain.rules import RuleError, aware, utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway, PublicationUncertain
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Audit, Job, Operation, PublicationResource, new_id
from viracase.infrastructure.publication_migrations import LEDGER, confirmed_migrations, preflight_migrations, verify_remote_migration_history
from viracase.infrastructure.publication_outcome import migration_identity, publication_diagnostic
from viracase.infrastructure.publication_verification import binding_observation
from viracase.infrastructure.repository import operation_dto


def observe_publication(gateway, name, revision, inventory, database_id=None, files=None):
    inventory = dict(inventory)
    migrations = confirmed_migrations(inventory)
    database = gateway.database(name)
    known_database = database_id or inventory.get("database_id")
    if database and (not isinstance(database, dict) or not isinstance(database.get("uuid"), str) or not database["uuid"]):
        raise RuleError("RESOURCE_CONFLICT", "A identidade do banco remoto não foi confirmada.")
    if database and not inventory.get("database_creation_started") and not known_database:
        raise RuleError("RESOURCE_CONFLICT", "Banco remoto sem criação registrada para este projeto.")
    if database and known_database and database.get("uuid") != known_database:
        raise RuleError("RESOURCE_CONFLICT", "O banco remoto não corresponde ao inventário do projeto.")
    if known_database and not database:
        raise RuleError("PUBLICATION_BINDINGS_NOT_READY", "O banco autorizado ainda não foi localizado na Cloudflare.")
    pending = inventory.get("migration_in_progress")
    if pending:
        if not isinstance(pending, dict) or not isinstance(pending.get("name"), str) or not isinstance(pending.get("sha256"), str):
            raise RuleError("MIGRATION_RESULT_UNKNOWN", "O recibo pendente da migration precisa de revisão.")
        confirmed_migrations({"migrations": {pending["name"]: pending["sha256"]}})
        recorded = migrations.get(pending["name"])
        if recorded and recorded != pending["sha256"]:
            raise RuleError("MIGRATION_CHECKSUM_CONFLICT", "O recibo pendente diverge do histórico confirmado da migration.")
        protocol = pending.get("protocol_version")
        phase, effect = pending.get("phase"), pending.get("effect_outcome")
        not_sent = protocol == 2 and phase == "prepared" and effect == "not_sent"
        rejected = protocol == 2 and phase == "sql_rejected" and effect in ("none", "not_sent")
        if not_sent or rejected:
            inventory.update(
                migration_in_progress=None,
                migration_last_attempt={
                    **pending, "reconciled_at": utcnow().isoformat(),
                    "resolution": "not_sent" if not_sent else "rejected_without_effect",
                },
            )
        elif not database:
            raise RuleError("MIGRATION_RESULT_UNKNOWN", "Banco ausente durante reconciliação da migration.")
        else:
            receipts = gateway.query(
                database["uuid"], f"SELECT name, sha256 FROM {LEDGER} WHERE name = ?", [pending["name"]]
            )
            matches = [
                row for item in receipts if isinstance(item, dict) and isinstance(item.get("results"), list)
                for row in item["results"] if isinstance(row, dict) and row.get("name") == pending["name"]
            ] if isinstance(receipts, list) else []
            if matches and (len(matches) != 1 or matches[0].get("sha256") != pending["sha256"]):
                raise RuleError(
                    "MIGRATION_CHECKSUM_CONFLICT",
                    "O recibo remoto da migration possui um hash diferente da execução pendente.",
                    details={"migration_execution": migration_identity(pending)},
                )
            if not matches and protocol == 2 and effect == "confirmed" and isinstance(phase, str) and phase in {
                "sql_confirmed", "receipt_sending",
            }:
                from viracase.infrastructure.publication_migrations import confirm_migration_receipt

                confirm_migration_receipt(gateway, database["uuid"], pending)
                matches = [{"name": pending["name"], "sha256": pending["sha256"]}]
            if not matches:
                raise RuleError(
                    "MIGRATION_RECEIPT_PENDING" if protocol == 2 and effect == "confirmed" else "MIGRATION_RESULT_UNKNOWN",
                    "Não há recibo remoto da migration. É preciso confirmar sua execução; o SQL não será repetido.",
                    details={"migration_execution": migration_identity(pending)},
                )
            inventory.update(
                migrations={**migrations, pending["name"]: pending["sha256"]},
                migration_in_progress=None,
                migration_last_attempt={
                    **pending, "phase": "receipt_confirmed", "effect_outcome": "confirmed",
                    "reconciled_at": utcnow().isoformat(), "resolution": "remote_receipt_confirmed",
                },
            )
    if database and files is not None:
        inventory["migrations"] = verify_remote_migration_history(gateway, database["uuid"], files, inventory)
    remote = gateway.settings(name)
    matches_revision, url = False, None
    if remote:
        if not isinstance(remote, dict) or not isinstance(remote.get("tags"), list) or not isinstance(remote.get("bindings"), list):
            raise RuleError("RESOURCE_CONFLICT", "O inventário remoto do Worker não foi confirmado.")
        tags = remote.get("tags", [])
        if (
            "viroucaseai" not in tags
            or not database
        ):
            raise RuleError(
                "RESOURCE_CONFLICT", "Worker ou binding remoto não corresponde ao inventário autorizado."
            )
        bindings = binding_observation(remote, database["uuid"], revision)
        if bindings["code"] in {"BINDINGS_MISMATCH", "BINDINGS_RESPONSE_INVALID"}:
            raise RuleError("PUBLICATION_BINDINGS_MISMATCH", "Os bindings remotos não correspondem aos recursos autorizados.")
        if revision in tags:
            if not bindings["passed"]:
                raise RuleError("PUBLICATION_BINDINGS_NOT_READY", "Os bindings da revisão ainda não foram confirmados.")
            if files is not None:
                verify_remote_migration_history(gateway, database["uuid"], files, inventory, require_complete=True)
            subdomain = gateway.request("GET", gateway.account_path + "/workers/subdomain")
            label = subdomain.get("subdomain", "") if subdomain else ""
            import re

            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", label):
                raise RuleError("CONFIGURATION_INVALID", "Subdomínio remoto inválido.")
            url = f"https://{name}.{label}.workers.dev"
            matches_revision = True
    if database:
        inventory["database_id"] = database["uuid"]
    return {
        "inventory": inventory, "database_id": database["uuid"] if database else None,
        "matches_revision": matches_revision, "url": url,
    }


def reconcile_publication(repo, actor, oid, key, reason, gateway_factory=CloudflareGateway):
    registry = Integrations(repo)
    with repo.sessions() as db:
        registry.require_admin(repo.account(db, actor))
    recovered = repo.recover_intention(actor, "publication:reconcile:" + oid, key, {"reason": reason})
    if recovered is not None:
        return recovered
    with repo.sessions.begin() as db:
        registry.require_admin(repo.account(db, actor))
        op = db.get(Operation, oid)
        if not op or op.type != "publication" or op.state != "reconciling":
            raise RuleError("NOT_FOUND", "Publicação em reconciliação indisponível.", 404)
        owner = op.account_id
        repo.account(db, owner, True)
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        if job.state != "reconciling" or job.lease_token and job.lease_until and aware(job.lease_until) > utcnow():
            raise RuleError("OPERATION_CONFLICT", "A publicação já está sendo verificada.")
        lease_token = new_id()
        job.lease_token, job.lease_until = lease_token, utcnow() + timedelta(minutes=5)
        from viracase.infrastructure.publication_recovery import recovery_status

        previous = op.result.get("publication_recovery") or {}
        op.result = {
            **op.result,
            "publication_recovery": recovery_status(
                "running", previous=previous, attempts=previous.get("attempts", 0) + 1,
                last_attempt_at=utcnow().isoformat(), next_attempt_at=None,
            ),
        }
    try:
        return _reconcile_publication(repo, actor, oid, key, reason, lease_token, gateway_factory)
    except Exception as exc:
        if not isinstance(exc, RuleError) or exc.code not in {"LEASE_LOST", "OPERATION_CONFLICT"}:
            from viracase.infrastructure.publication_recovery import _retry_recovery

            _retry_recovery(repo, oid, lease_token, exc, secrets=repo.sources.secrets, manual=True)
        raise
    finally:
        with repo.sessions.begin() as db:
            repo.account(db, owner, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if job and job.lease_token == lease_token:
                job.lease_token, job.lease_until = None, None


def _reconcile_publication(repo, actor, oid, key, reason, lease_token, gateway_factory):
    from viracase.infrastructure.publication_recovery import (
        deterministic_pre_upload_failure,
        pre_upload_migration_failure,
        recovery_status,
        upload_may_have_started,
    )

    registry = Integrations(repo)
    with repo.sessions() as db:
        registry.require_admin(repo.account(db, actor))
        op = db.get(Operation, oid)
        if not op or op.type != "publication" or op.state != "reconciling":
            raise RuleError("NOT_FOUND", "Publicação em reconciliação indisponível.", 404)
        job = db.scalar(select(Job).where(Job.operation_id == oid))
        if job.lease_token != lease_token:
            raise RuleError("OPERATION_CONFLICT", "A conferência foi assumida por outra execução.")
        resource = db.get(PublicationResource, op.project_id)
        config = registry.for_resource(db, resource)
        gateway = gateway_factory(config.settings, registry.vault.decrypt(config.id, config.credential))
        name, revision = resource.worker_name, op.payload["revision_id"]
        inventory, result = dict(resource.inventory), dict(op.result)
        database_id = resource.database_id
        target, owner = op.project_id, op.account_id
        config_id, error_code = config.id, op.error_code

    def guard(method="GET"):
        with repo.sessions.begin() as db:
            row = db.get(Operation, oid)
            account = repo.account(db, owner, True)
            job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
            if row.state != "reconciling" or job.state != "reconciling" or job.lease_token != lease_token:
                raise RuleError("OPERATION_CONFLICT", "A operação mudou durante a consulta remota.")
            if repo.config.restore_hold or not repo.config.lifecycle_enabled or account.lifecycle != "active":
                raise RuleError("LIFECYCLE_LOCKED", "A publicação aguarda o término da manutenção.")
            repo.access(db, account, "publish")
            resource = db.get(PublicationResource, target)
            current = registry.for_resource(db, resource)
            project = repo.owned_project(db, owner, target)
            if (
                not current or current.id != config_id or current.state != "active"
                or not current.validation.get("ready")
                or current.settings["account_id"] != resource.remote_account_id
                or current.validation.get("publication_check_blocked")
                or project.stack in current.validation.get("publication_check_blocked_stacks", [])
            ):
                raise RuleError("CONFIGURATION_INVALID", "A conexão mudou durante a conferência da publicação.")
            job.lease_until = utcnow() + timedelta(minutes=5)

    guard()
    gateway.before_request = guard
    files = repo.source_files(owner, target, revision)
    if error_code == "MIGRATION_RESULT_UNKNOWN" and not inventory.get("migration_in_progress"):
        verify_remote_migration_history(gateway, database_id, files, inventory, require_complete=True)
    observed = observe_publication(gateway, name, revision, inventory, database_id, files)
    from viracase.infrastructure.publication_resolution import apply_resolution_result, assisted_receipt_observed

    assisted = assisted_receipt_observed(result, oid, revision, database_id, inventory, observed["inventory"])
    inventory, url = observed["inventory"], observed["url"]
    verified, verification = False, None
    terminal_failure = deterministic_pre_upload_failure(result, error_code, inventory, revision, oid)
    if pre_upload_migration_failure(result, error_code) and not terminal_failure:
        raise RuleError(error_code, "O histórico de migrations precisa ser conferido antes de confirmar a publicação.")
    if terminal_failure or assisted:
        pass
    elif observed["matches_revision"]:
        guard()
        preflight_migrations(repo.source_files(owner, target, revision), inventory)
        verification = gateway.verify(url, revision)
        if isinstance(verification, dict) and verification.get("passed") is not True:
            raise RuleError("PUBLICATION_VERIFICATION_FAILED", "O funcionamento da publicação não foi confirmado.")
        verified = True
    elif upload_may_have_started(result, inventory, revision) or (
        inventory.get("database_creation_started") and not observed["database_id"]
    ):
        raise PublicationUncertain()

    def command(db, operator):
        registry.require_admin(operator)
        account = repo.account(db, owner, True)
        configuration_lock(db, "integration:cloudflare")
        if repo.config.restore_hold or not repo.config.lifecycle_enabled or account.lifecycle != "active":
            raise RuleError("LIFECYCLE_LOCKED", "O projeto está em encerramento de acesso.")
        row = db.get(Operation, oid)
        job = db.scalar(select(Job).where(Job.operation_id == oid).with_for_update())
        if row.state != "reconciling" or job.state != "reconciling" or job.lease_token != lease_token:
            raise RuleError("OPERATION_CONFLICT", "A operação mudou durante a consulta remota.")
        resource = db.get(PublicationResource, target)
        repo.access(db, account, "publish")
        current = registry.for_resource(db, resource)
        project = repo.owned_project(db, owner, target)
        if (
            not current or current.id != config_id or current.state != "active"
            or not current.validation.get("ready")
            or current.settings["account_id"] != resource.remote_account_id
            or current.validation.get("publication_check_blocked")
            or project.stack in current.validation.get("publication_check_blocked_stacks", [])
        ):
            raise RuleError("CONFIGURATION_INVALID", "A conexão mudou durante a conferência da publicação.")
        resource.inventory = inventory
        if observed["database_id"]:
            resource.database_id = observed["database_id"]
        if assisted:
            apply_resolution_result(row, job, resource, assisted)
            db.add(Audit(actor_id=assisted["actor_id"], action="publication:recovery:resolved", resource_id=oid, reason="Recibo da resolução iniciada confirmado na reconciliação, sem repetir SQL ou publicar revisão antiga."))
        elif terminal_failure:
            row.state, job.state = "failed", "done"
            row.result = {
                **row.result, "external_effect_pending": False,
                "publication_recovery": recovery_status(
                    "resolved", previous=row.result.get("publication_recovery"),
                    last_checked_at=utcnow().isoformat(), next_attempt_at=None, last_error_code=error_code,
                    resolution="pre_upload_failure",
                ),
            }
            record_progress(row, "publication_failed")
        elif verified:
            resource.state = "published"
            resource.inventory = {
                **resource.inventory,
                "url": url,
                "published_revision_id": revision,
                "published_at": utcnow().isoformat(),
            }
            row.state, job.state = "succeeded", "done"
            row.result = {
                **row.result,
                "url": url,
                "revision_id": revision,
                "verified_at": utcnow().isoformat(),
                "external_effect_pending": False,
                "remote_verification": verification,
                "publication_recovery": recovery_status(
                    "resolved", previous=row.result.get("publication_recovery"),
                    last_checked_at=utcnow().isoformat(), next_attempt_at=None, last_error_code=None,
                    resolution="published",
                ),
            }
            record_progress(row, "publication_verified")
        else:
            repo.access(db, account, "publish")
            from viracase.infrastructure.publication import PublicationService

            current = registry.for_resource(db, resource)
            if (
                not current
                or current.settings["account_id"] != resource.remote_account_id
                or PublicationService(repo).configuration_reasons(current, project.stack)
            ):
                raise RuleError(
                    "CONFIGURATION_INVALID", "Valide e qualifique a configuração atual antes de retomar."
                )
            row.policy = {
                **row.policy,
                "execution_config_id": current.id,
                "execution_version": current.version,
            }
            row.payload = {**row.payload, "entitlement_version": account.entitlement_version}
            row.state, job.state, job.lease_token = "queued", "queued", None
            job.available_at = utcnow()
            row.result = {
                **row.result, "external_effect_pending": False,
                "publication_recovery": recovery_status(
                    "resolved", previous=row.result.get("publication_recovery"),
                    last_checked_at=utcnow().isoformat(), next_attempt_at=None, last_error_code=None,
                    resolution="resume_safe",
                ),
            }
            record_progress(row, "queued")
        job.lease_token, job.lease_until = None, None
        if not terminal_failure and not assisted:
            row.error_code = None
        row.result = {
            **row.result,
            "publication_diagnostic": publication_diagnostic(
                row.result, resource.inventory, stage=row.progress_stage, operation_id=oid, revision_id=revision,
            ),
        }
        db.add(Audit(actor_id=actor, action="publication:reconcile", resource_id=oid, reason=reason))
        return operation_dto(row, admin=True)

    return repo.mutate(actor, "publication:reconcile:" + oid, key, {"reason": reason}, command)
