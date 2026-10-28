import re
from hashlib import sha256

from viracase.domain.rules import RuleError

LEDGER = "__viroucaseai_migrations"


def apply_migrations(gateway, database_id, files, inventory, checkpoint):
    migrations = dict(inventory.get("migrations", {}))
    gateway.query(
        database_id, f"CREATE TABLE IF NOT EXISTS {LEDGER} (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL)"
    )
    rows = gateway.query(database_id, f"SELECT name, sha256 FROM {LEDGER}")
    observed = {item["name"]: item["sha256"] for result in rows for item in result.get("results", [])}
    for name, sql in sorted(files.items()):
        digest = sha256(sql.encode()).hexdigest()
        recorded = observed.get(name) or migrations.get(name)
        if recorded:
            if recorded != digest:
                raise RuleError(
                    "MIGRATION_CHECKSUM_CONFLICT",
                    "Uma migration publicada foi alterada. Crie uma nova migration incremental.",
                )
            continue
        pending = inventory.get("migration_in_progress")
        if pending and pending["name"] == name:
            raise RuleError(
                "MIGRATION_RESULT_UNKNOWN",
                "A migration interrompida ainda não tem recibo remoto. Confira o banco antes de qualquer novo envio.",
            )
        stripped = re.sub(r"--[^\n]*|/\*[\s\S]*?\*/", "", sql)
        if (
            re.search(r"\b(DROP|DELETE|TRUNCATE|ATTACH|DETACH|PRAGMA|REPLACE|UPDATE)\b", stripped, re.I)
            or LEDGER.casefold() in sql.casefold()
        ):
            raise RuleError(
                "MIGRATION_REVIEW_REQUIRED",
                "Migration potencialmente destrutiva exige revisão antes de publicação.",
            )
        checkpoint(
            "publication_migrating",
            {"migration_in_progress": {"name": name, "sha256": digest}},
            external=True,
        )
        gateway.query(database_id, sql)
        gateway.query(database_id, f"INSERT INTO {LEDGER} (name, sha256) VALUES (?, ?)", [name, digest])
        migrations[name] = digest
        checkpoint("publication_migrating", {"migrations": dict(migrations), "migration_in_progress": None})
