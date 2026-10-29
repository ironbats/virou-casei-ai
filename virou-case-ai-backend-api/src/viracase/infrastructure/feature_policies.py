from sqlalchemy import select

from viracase.domain.rules import RuleError
from viracase.infrastructure.agent_execution import validate_model_execution
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.models import Account, Audit, FeaturePolicy
from viracase.infrastructure.repository_dto import iso
from viracase.interfaces.preparation_schemas import CapturePolicy, PreparationPolicy

DEFAULTS = {
    "template_preparation": PreparationPolicy().model_dump(),
    "integration_capture": CapturePolicy().model_dump(),
    "payload_access": {"admin_ids": []},
}


class FeaturePolicies:
    def __init__(self, repo, catalog=None):
        self.repo, self.catalog = repo, catalog

    def read(self, db, name):
        row = db.scalar(
            select(FeaturePolicy)
            .where(FeaturePolicy.name == name)
            .order_by(FeaturePolicy.version.desc())
            .limit(1)
        )
        return {
            "version": row.version if row else 0,
            "settings": {**DEFAULTS[name], **row.settings} if row else dict(DEFAULTS[name]),
            "updated_at": iso(row.created_at) if row else None,
        }

    def current(self, name):
        with self.repo.sessions() as db:
            return self.read(db, name)

    def admin(self, db, actor):
        if self.repo.account(db, actor).role != "admin":
            raise RuleError("FORBIDDEN", "Ação administrativa não permitida.", 403)

    def can_inspect(self, db, actor):
        return (
            self.repo.account(db, actor).role == "admin"
            and actor in self.read(db, "payload_access")["settings"]["admin_ids"]
        )

    def model_options(self):
        if not self.catalog:
            return []
        return [
            {
                "id": policy.get("model_id", policy["provider"] + ":" + policy["model"]),
                "connection_id": policy.get("connection_id"),
                "provider": policy["provider"],
                "model": policy["model"],
                "name": policy.get("name", policy["model"]),
                "ready": True,
                "max_output_tokens": policy["max_output_tokens"],
            }
            for policy in self.catalog.eligible()
        ]

    def get(self, actor, name):
        with self.repo.sessions() as db:
            self.admin(db, actor)
            value = self.read(db, name)
            if name == "template_preparation":
                return {**value, "available_models": self.model_options()}
            if name == "payload_access":
                admins = db.scalars(
                    select(Account).where(Account.role == "admin").order_by(Account.name, Account.id)
                ).all()
                return {
                    "version": value["version"],
                    "admins": [
                        {
                            "id": admin.id,
                            "name": admin.name,
                            "login": admin.login,
                            "allowed": admin.id in value["settings"]["admin_ids"],
                        }
                        for admin in admins
                    ],
                    "can_inspect_payloads": self.can_inspect(db, actor),
                }
            return {**value, "can_inspect_payloads": self.can_inspect(db, actor)}

    def save(self, actor, name, key, version, payload):
        def command(db, account):
            self.admin(db, actor)
            configuration_lock(db, "feature-policy:" + name)
            if self.read(db, name)["version"] != version:
                raise RuleError("VERSION_CONFLICT", "A política mudou. Atualize antes de salvar.", 412)
            settings = payload.get("settings", {"admin_ids": payload.get("admin_ids", [])})
            if name == "payload_access":
                ids = settings["admin_ids"]
                known = set(
                    db.scalars(select(Account.id).where(Account.role == "admin", Account.id.in_(ids)))
                )
                if len(set(ids)) != len(ids) or known != set(ids):
                    raise RuleError("VALIDATION_ERROR", "Selecione administradores existentes.", 400)
            elif name == "template_preparation":
                settings = PreparationPolicy.model_validate(settings).model_dump()
                if settings["model_id"] and settings["model_id"] not in {
                    item["id"] for item in self.model_options()
                }:
                    raise RuleError("MODEL_UNAVAILABLE", "Selecione um modelo habilitado e validado.", 400)
                if settings["model_id"] and self.catalog:
                    for policy in self.catalog.eligible():
                        if settings["model_id"] == policy.get("model_id", policy["provider"] + ":" + policy["model"]):
                            validate_model_execution(
                                policy["provider"], policy["model"],
                                {**policy, "max_output_tokens": min(policy["max_output_tokens"], settings["max_output_tokens"])},
                            )
            else:
                settings = CapturePolicy.model_validate(settings).model_dump()
            db.add(FeaturePolicy(name=name, version=version + 1, settings=settings, created_by=actor))
            db.add(Audit(actor_id=actor, action="policy:" + name, resource_id=name, reason=payload["reason"]))
            return {"version": version + 1}

        self.repo.mutate(actor, "policy:" + name, key, {"version": version, **payload}, command)
        if name == "integration_capture":
            from viracase.infrastructure.integration_traces import TraceStore

            TraceStore(self.repo).prune()
        return self.get(actor, name)
