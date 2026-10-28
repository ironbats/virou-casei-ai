from datetime import timedelta
from hashlib import sha256

from sqlalchemy import func, select

from viracase.domain.rules import RuleError, aware, canonical_hash, utcnow
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.credentials import CredentialVault
from viracase.infrastructure.models import (
    Audit,
    IntegrationConfig,
    IntegrationConnection,
    IntegrationVersionBinding,
    PublicationResource,
    Operation,
    new_id,
)
from viracase.interfaces.feature_schemas import BuildPolicy, CloudflarePolicy


class Integrations:
    def __init__(self, repo):
        self.repo = repo
        self.vault = CredentialVault(repo.config.credential_key_path)

    def connection(self, db, provider, identity=None):
        if identity:
            row = db.get(IntegrationConnection, identity)
            if not row or row.provider != provider:
                raise RuleError("NOT_FOUND", "Conexão indisponível.", 404)
            return row
        return db.scalar(select(IntegrationConnection).where(IntegrationConnection.default_for == provider))

    def connection_for(self, db, row):
        binding = db.get(IntegrationVersionBinding, row.id) if row else None
        return self.connection(db, row.provider, binding.connection_id) if binding else None

    def latest(self, db, provider, active=False, connection_id=None):
        connection = self.connection(db, provider, connection_id)
        if not connection:
            return None
        query = (
            select(IntegrationConfig)
            .join(IntegrationVersionBinding, IntegrationVersionBinding.config_id == IntegrationConfig.id)
            .where(
                IntegrationConfig.provider == provider,
                IntegrationVersionBinding.connection_id == connection.id,
            )
        )
        if active:
            query = query.where(IntegrationConfig.state == "active")
        return db.scalar(query.order_by(IntegrationConfig.version.desc()).limit(1))

    def for_resource(self, db, resource):
        pinned = db.get(IntegrationConfig, resource.config_id)
        connection = self.connection_for(db, pinned)
        current = self.latest(db, "cloudflare", connection_id=connection.id) if connection else pinned
        return current if current and current.settings["account_id"] == resource.remote_account_id else pinned

    def for_project(self, db, pid):
        resource = db.get(PublicationResource, pid)
        return self.for_resource(db, resource) if resource else self.latest(db, "cloudflare")

    def current_rows(self, db, provider):
        rows = db.execute(
            select(IntegrationConfig, IntegrationVersionBinding.connection_id)
            .join(IntegrationVersionBinding, IntegrationVersionBinding.config_id == IntegrationConfig.id)
            .where(IntegrationConfig.provider == provider)
            .order_by(IntegrationConfig.version.desc())
        ).all()
        latest = {}
        for row, identity in rows:
            latest.setdefault(identity, row)
        return latest

    def listing(self, actor, provider):
        with self.repo.sessions() as db:
            self.require_admin(self.repo.account(db, actor))
            rows = self.current_rows(db, provider)
            connections = db.scalars(
                select(IntegrationConnection)
                .where(IntegrationConnection.provider == provider)
                .order_by(IntegrationConnection.name_key, IntegrationConnection.id)
            ).all()
            return {
                "items": [self.describe(rows.get(c.id), c) for c in connections],
                "defaults": BuildPolicy().model_dump() if provider == "jev" else {},
            }

    def describe(self, row, connection):
        return {
            **self.dto(row),
            "connection_id": connection.id if connection else None,
            "name": connection.name if connection else "",
            "is_default": bool(connection and connection.default_for),
        }

    def require_admin(self, operator):
        if operator.role != "admin":
            raise RuleError("FORBIDDEN", "Ação administrativa não permitida.", 403)

    def dto(self, row):
        if not row:
            return {"version": 0, "state": "missing", "settings": {}, "validation": {}, "has_token": False}
        return {
            "id": row.id,
            "version": row.version,
            "state": row.state,
            "settings": row.settings,
            "validation": row.validation,
            "configuration_digest": canonical_hash(row.settings),
            "has_token": bool(row.credential),
            "created_at": aware(row.created_at).isoformat(),
            "validated_at": aware(row.validated_at).isoformat() if row.validated_at else None,
        }

    def get(self, provider, connection_id=None):
        with self.repo.sessions() as db:
            connection = self.connection(db, provider, connection_id)
            latest, active = (
                self.latest(db, provider, connection_id=connection_id),
                self.latest(db, provider, True, connection_id),
            )
            result = self.describe(latest, connection)
            result["active_version"] = active.version if active else None
            result["defaults"] = BuildPolicy().model_dump() if provider == "jev" else {}
            if provider == "cloudflare":
                from viracase.infrastructure.observability import WorkerBuildStatus

                result["executor_digest"] = WorkerBuildStatus(self.repo).fingerprint()
            return result

    def build_policy(self):
        with self.repo.sessions() as db:
            row = self.latest(db, "jev")
            return BuildPolicy(**row.settings).model_dump() if row else BuildPolicy().model_dump()

    def save(self, actor, provider, key, version, payload, connection_id=None, create=False):
        policy = (BuildPolicy if provider == "jev" else CloudflarePolicy).model_validate(payload["settings"])
        settings = policy.model_dump(mode="json")
        token = payload.get("token")
        safe = {
            **payload,
            "token": sha256(token.encode()).hexdigest() if token else None,
            "version": version,
        }

        if connection_id is not None or create:
            safe.update(connection_id=connection_id, create=create)

        def command(db, operator):
            self.require_admin(operator)
            configuration_lock(db, "integration:" + provider)
            connection = None if create else self.connection(db, provider, connection_id)
            if not connection:
                if version:
                    raise RuleError("VERSION_CONFLICT", "Atualize a conexão antes de salvar.", 412)
                name = payload.get(
                    "name", "Cloudflare principal" if provider == "cloudflare" else "Jev principal"
                ).strip()
                connection = IntegrationConnection(
                    provider=provider,
                    name=name,
                    name_key=name.casefold(),
                    created_by=actor,
                    default_for=None if self.connection(db, provider) else provider,
                )
                db.add(connection)
                db.flush()
            current = self.latest(db, provider, connection_id=connection.id)
            if (current.version if current else 0) != version:
                raise RuleError("VERSION_CONFLICT", "A configuração mudou. Atualize antes de salvar.", 412)
            if payload.get("name"):
                connection.name = payload["name"].strip()
                connection.name_key = connection.name.casefold()
            if (
                provider == "cloudflare"
                and current
                and current.settings["account_id"] != settings["account_id"]
            ):
                used = db.scalar(
                    select(PublicationResource.project_id)
                    .join(
                        IntegrationVersionBinding,
                        IntegrationVersionBinding.config_id == PublicationResource.config_id,
                    )
                    .where(IntegrationVersionBinding.connection_id == connection.id)
                    .limit(1)
                )
                pending_check = db.scalar(
                    select(Operation.id)
                    .where(
                        Operation.type == "publication_check",
                        Operation.state.in_(["queued", "running", "reconciling"]),
                        Operation.payload["connection_id"].as_string() == connection.id,
                    )
                    .limit(1)
                )
                if used or pending_check:
                    raise RuleError(
                        "CONNECTION_IN_USE",
                        "Esta conexão possui projetos vinculados. Cadastre outra conexão para outra conta Cloudflare.",
                    )
            next_version = (
                db.scalar(
                    select(func.max(IntegrationConfig.version)).where(IntegrationConfig.provider == provider)
                )
                or 0
            ) + 1
            identity = new_id()
            credential = token or (
                self.vault.decrypt(current.id, current.credential) if current and current.credential else None
            )
            if provider == "cloudflare" and not credential:
                raise RuleError("CONFIGURATION_PENDING", "Informe o token da integração.", 400)
            for active in db.scalars(
                select(IntegrationConfig)
                .join(IntegrationVersionBinding, IntegrationVersionBinding.config_id == IntegrationConfig.id)
                .where(
                    IntegrationVersionBinding.connection_id == connection.id,
                    IntegrationConfig.state == "active",
                )
            ):
                active.state = "disabled"
            row = IntegrationConfig(
                id=identity,
                provider=provider,
                version=next_version,
                state="draft",
                settings=settings,
                credential=self.vault.encrypt(identity, credential) if credential else None,
                validation={},
                created_by=actor,
                created_at=utcnow(),
            )
            db.add(row)
            db.add(
                Audit(
                    actor_id=actor,
                    action=f"integration:{provider}:save",
                    resource_id=identity,
                    reason=payload["reason"],
                )
            )
            db.flush()
            db.add(IntegrationVersionBinding(config_id=row.id, connection_id=connection.id))
            db.flush()
            return self.describe(row, connection)

        return self.repo.mutate(actor, f"integration:{provider}:save", key, safe, command)

    def validate(self, actor, provider, version, reason, connection_id=None):
        with self.repo.sessions() as db:
            self.require_admin(self.repo.account(db, actor))
            row = self.latest(db, provider, connection_id=connection_id)
            if not row or row.version != version:
                raise RuleError("VERSION_CONFLICT", "Atualize a configuração antes de validar.", 412)
            connection_id = self.connection_for(db, row).id
            identity, policy = row.id, dict(row.settings)
            token = self.vault.decrypt(row.id, row.credential) if row.credential else None
        self.repo.rate_limit(f"validate:{provider}:{actor}", 12, 3600)
        if provider == "cloudflare":
            from viracase.infrastructure.cloudflare_gateway import CloudflareGateway

            evidence = CloudflareGateway(policy, token).validate()
        else:
            evidence = {
                "configuration_valid": True,
                "connectivity_verified": False,
                "message": "Configuração local validada. Jev será chamado somente em Preparar construção.",
                "ready": bool(token) or not policy["recommendation_enabled"],
            }
        with self.repo.sessions.begin() as db:
            self.repo.account(db, actor, True)
            configuration_lock(db, "integration:" + provider)
            row = self.latest(db, provider, connection_id=connection_id)
            if not row or row.id != identity:
                raise RuleError("VERSION_CONFLICT", "A configuração mudou durante a validação.", 412)
            was_active = row.state == "active"
            preserved = {
                name: row.validation[name]
                for name in (
                    "qualification", "runtime_verification", "publication_check_blocked",
                    "publication_check_blocked_stacks",
                )
                if name in row.validation and evidence.get("ready")
            }
            row.validation = {**preserved, **evidence, "checked_at": utcnow().isoformat()}
            row.validated_at = utcnow()
            row.state = ("active" if was_active else "validated") if evidence.get("ready") else "invalid"
            db.add(
                Audit(
                    actor_id=actor,
                    action=f"integration:{provider}:validate",
                    resource_id=row.id,
                    reason=reason,
                )
            )
            return self.dto(row)

    def activate(self, actor, provider, key, version, reason, disable=False, connection_id=None):
        def command(db, operator):
            self.require_admin(operator)
            configuration_lock(db, "integration:" + provider)
            row = self.latest(db, provider, connection_id=connection_id)
            if not row or row.version != version:
                raise RuleError("VERSION_CONFLICT", "A configuração mudou. Atualize a página.", 412)
            if not disable:
                if row.state not in {"validated", "active"} or not row.validation.get("ready"):
                    raise RuleError("CONFIGURATION_INVALID", "Valide esta versão antes de ativar.")
                if provider == "cloudflare" and not self.fresh(row):
                    raise RuleError("CONFIGURATION_INVALID", "Validação expirada. Valide novamente.")
            row.state = "disabled" if disable else "active"
            db.add(
                Audit(
                    actor_id=actor,
                    action=f"integration:{provider}:{row.state}",
                    resource_id=row.id,
                    reason=reason,
                )
            )
            return self.dto(row)

        return self.repo.mutate(
            actor,
            f"integration:{provider}:state",
            key,
            {
                "version": version,
                "reason": reason,
                "disable": disable,
                **({"connection_id": connection_id} if connection_id is not None else {}),
            },
            command,
        )

    def set_default(self, actor, provider, identity, key, version, reason):
        def command(db, operator):
            self.require_admin(operator)
            configuration_lock(db, "integration:" + provider)
            connection = self.connection(db, provider, identity)
            row = self.latest(db, provider, connection_id=identity)
            if not row or row.version != version:
                raise RuleError("VERSION_CONFLICT", "Atualize a conexão antes de definir o padrão.", 412)
            if row.state != "active":
                raise RuleError(
                    "CONFIGURATION_INVALID", "Valide e ative a conexão antes de defini-la como padrão."
                )
            if provider == "cloudflare" and not self.fresh(row):
                raise RuleError(
                    "CONFIGURATION_INVALID", "Valide novamente a conexão antes de definir o padrão."
                )
            previous = self.connection(db, provider)
            if previous:
                previous.default_for = None
                db.flush()
            connection.default_for = provider
            db.add(
                Audit(
                    actor_id=actor,
                    action=f"integration:{provider}:default",
                    resource_id=identity,
                    reason=reason,
                )
            )
            return self.describe(row, connection)

        return self.repo.mutate(
            actor,
            f"integration:{provider}:default",
            key,
            {"connection_id": identity, "version": version, "reason": reason},
            command,
        )

    def fresh(self, row):
        return bool(
            row.validated_at
            and aware(row.validated_at) + timedelta(hours=row.settings["validation_hours"]) > utcnow()
        )
