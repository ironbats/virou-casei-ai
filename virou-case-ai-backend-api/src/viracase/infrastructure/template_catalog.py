from sqlalchemy import select

from viracase.domain.rules import RuleError, canonical_hash, utcnow
from viracase.infrastructure.configuration_lock import configuration_lock
from viracase.infrastructure.learning import CATEGORIES
from viracase.infrastructure.models import Audit, TemplateVersion


class TemplateCatalog:
    def __init__(self, repo, learning):
        self.repo, self.learning = repo, learning

    def ensure_seed(self, db):
        if db.scalar(select(TemplateVersion.id).limit(1)):
            return
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
        for guide in self.learning.guides.values():
            identity = canonical_hash([guide["slug"], guide["template_version"]])[:32]
            db.execute(
                insert(TemplateVersion)
                .values(
                    id=identity,
                    slug=guide["slug"],
                    version=1,
                    owner_id=None,
                    available=True,
                    content={
                        "title": guide["title"],
                        "category": guide["category"],
                        "summary": guide["objective"],
                        "content": guide["application_template"],
                        "fields": guide["fields"],
                        "source_label": guide["template_version"],
                    },
                    created_at=utcnow(),
                )
                .on_conflict_do_nothing()
            )

    def list(self, actor, search="", category=None):
        if category and category not in CATEGORIES:
            raise RuleError("VALIDATION_ERROR", "Categoria inválida.", 400)
        with self.repo.sessions.begin() as db:
            self.repo.access(db, self.repo.account(db, actor), "learn")
            self.ensure_seed(db)
            rows = db.scalars(
                select(TemplateVersion)
                .where((TemplateVersion.owner_id.is_(None)) | (TemplateVersion.owner_id == actor))
                .order_by(TemplateVersion.slug, TemplateVersion.version.desc())
            ).all()
            latest = {}
            for row in rows:
                latest.setdefault(row.slug, row)
            return {
                "items": [
                    self.dto(row)
                    for row in latest.values()
                    if row.available
                    and (not category or row.content["category"] == category)
                    and (not search or search.casefold() in str(row.content).casefold())
                ]
            }

    def dto(self, row):
        return {
            "id": row.slug,
            "version_id": row.id,
            "version": row.version,
            "visibility": "private" if row.owner_id else "official",
            "available": row.available,
            **row.content,
        }

    def version(self, actor, identity):
        with self.repo.sessions() as db:
            self.repo.access(db, self.repo.account(db, actor), "learn")
            return self.dto(self.authorized(db, actor, identity))

    def guide(self, db, actor, slug):
        self.ensure_seed(db)
        row = db.scalar(
            select(TemplateVersion)
            .where(TemplateVersion.slug == slug)
            .order_by(TemplateVersion.version.desc())
            .limit(1)
        )
        if not row:
            raise RuleError("NOT_FOUND", "Guia indisponível.", 404)
        self.authorized(db, actor, row.id)
        original = self.learning.guides.get(slug, {})
        return {
            **original,
            "slug": slug,
            "title": row.content["title"],
            "category": row.content["category"],
            "objective": row.content["summary"],
            "template": row.content["content"],
            "application_template": row.content["content"],
            "fields": row.content["fields"],
            "template_version": row.content.get("source_label", f"v{row.version}"),
            "template_source_version_id": row.id,
            "version": str(row.version),
            "concept": original.get("concept", row.content["summary"]),
            "when_to_use": original.get("when_to_use", row.content["summary"]),
            "steps": original.get("steps", []),
            "checklist": original.get("checklist", []),
            "security_notes": original.get("security_notes", []),
            "sources": original.get("sources", []),
            "security_summary": original.get("security_summary", ""),
            "application": original.get("application", row.content["summary"]),
            "limitations": original.get("limitations", ""),
            "reviewed_at": original.get("reviewed_at", ""),
        }

    def guides(self, actor, category=None):
        catalog = self.list(actor, category=category)
        return {
            "items": [
                {
                    "slug": item["id"],
                    "title": item["title"],
                    "category": item["category"],
                    "objective": item["summary"],
                    "version": str(item["version"]),
                    "security_summary": self.learning.guides.get(item["id"], {}).get("security_summary", ""),
                    "search_terms": " ".join(
                        [
                            item["summary"],
                            self.learning.guides.get(item["id"], {}).get("concept", ""),
                            self.learning.guides.get(item["id"], {}).get("when_to_use", ""),
                        ]
                    ),
                }
                for item in catalog["items"]
            ]
        }

    def get_guide(self, actor, slug):
        with self.repo.sessions.begin() as db:
            self.repo.access(db, self.repo.account(db, actor), "learn")
            return self.guide(db, actor, slug)

    def authorized(self, db, actor, identity):
        row = db.get(TemplateVersion, identity)
        if not row or row.owner_id not in {None, actor} or not row.available:
            raise RuleError(
                "TEMPLATE_UNAVAILABLE", "Um anexo está indisponível. Remova-o ou escolha outra versão.", 409
            )
        latest = db.scalar(
            select(TemplateVersion)
            .where(TemplateVersion.slug == row.slug)
            .order_by(TemplateVersion.version.desc())
            .limit(1)
        )
        if latest and not latest.available:
            raise RuleError(
                "TEMPLATE_UNAVAILABLE", "Um template foi revogado. Remova-o antes de enviar.", 409
            )
        return row

    def save(self, actor, key, payload):
        def command(db, operator):
            if operator.role != "admin":
                raise RuleError("FORBIDDEN", "Ação administrativa não permitida.", 403)
            self.ensure_seed(db)
            configuration_lock(db, "template:" + payload["slug"])
            previous = db.scalar(
                select(TemplateVersion)
                .where(TemplateVersion.slug == payload["slug"])
                .order_by(TemplateVersion.version.desc())
                .limit(1)
            )
            if (previous.version if previous else 0) != payload["expected_version"]:
                raise RuleError("VERSION_CONFLICT", "O template mudou. Atualize antes de salvar.", 412)
            row = TemplateVersion(
                slug=payload["slug"],
                version=payload["expected_version"] + 1,
                available=payload["available"],
                content={k: payload[k] for k in ("title", "category", "summary", "content", "fields")},
            )
            db.add(row)
            db.flush()
            db.add(
                Audit(actor_id=actor, action="template:version", resource_id=row.id, reason=payload["reason"])
            )
            return self.dto(row)

        return self.repo.mutate(actor, "template:save", key, payload, command)

    def admin_list(self, actor):
        with self.repo.sessions.begin() as db:
            if self.repo.account(db, actor).role != "admin":
                raise RuleError("FORBIDDEN", "Ação administrativa não permitida.", 403)
            self.ensure_seed(db)
            rows = db.scalars(
                select(TemplateVersion)
                .where(TemplateVersion.owner_id.is_(None))
                .order_by(TemplateVersion.slug, TemplateVersion.version.desc())
            ).all()
            latest = {}
            for row in rows:
                latest.setdefault(row.slug, self.dto(row))
            return {"items": list(latest.values())}
