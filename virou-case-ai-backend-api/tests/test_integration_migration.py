import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select, text

from viracase.infrastructure.credentials import CredentialVault, initialize_key
from viracase.infrastructure.database import database
from viracase.infrastructure.models import (
    Account,
    IntegrationConfig,
    IntegrationConnection,
    IntegrationVersionBinding,
    Project,
    PublicationResource,
    TemplateVersion,
)

ROOT = Path(__file__).resolve().parents[1]


def migrate(url, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("VIRA_")}
    env.update(VIRA_DATABASE_URL=url, VIRA_MIGRATION_DATABASE_URL=url, PYTHONPATH=str(ROOT / "src"))
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_upgrade_preserves_all_legacy_rows_ciphertexts_and_publication_bindings(tmp_path):
    path = tmp_path / "legacy.db"
    url = "sqlite:///" + str(path)
    migrate(url, "upgrade", "0004")
    engine, sessions = database(url)
    key = tmp_path / "credential.key"
    initialize_key(key, sessions)
    vault = CredentialVault(key)
    with sessions.begin() as db:
        db.add(
            Account(
                id="admin",
                login="migration-admin",
                name="Existing admin",
                password_hash="unchanged-hash",
                role="admin",
            )
        )
        db.flush()
        db.add(
            Project(
                id="project",
                account_id="admin",
                name="Existing project",
                objective="Preserve all source records",
                stack="hono-typescript-d1",
            )
        )
        for provider in ("cloudflare", "jev"):
            for version in (1, 2):
                identity = provider + str(version)
                db.add(
                    IntegrationConfig(
                        id=identity,
                        provider=provider,
                        version=version,
                        state="active" if version == 2 else "disabled",
                        settings={"account_id": "a" * 32},
                        credential=vault.encrypt(identity, "synthetic-existing-secret-" + provider),
                        validation={"ready": True, "existing": "evidence"},
                        created_by="admin",
                    )
                )
        db.add(
            TemplateVersion(
                id="template",
                slug="preserve-template",
                version=1,
                available=True,
                content={"content": "existing template"},
            )
        )
        db.flush()
        db.add(
            PublicationResource(
                project_id="project",
                account_id="admin",
                config_id="cloudflare1",
                remote_account_id="a" * 32,
                worker_name="existing-worker",
                database_id="existing-database",
                inventory={"url": "https://synthetic.invalid"},
                state="published",
            )
        )
    with sqlite3.connect(path) as db:
        tables = [
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if r[0] not in {"alembic_version", "sqlite_sequence"}
        ]
        before = {name: db.execute('SELECT * FROM "' + name + '" ORDER BY 1').fetchall() for name in tables}
    migrate(url, "upgrade", "head")
    migrate(url, "upgrade", "head")
    with sqlite3.connect(path) as db:
        assert before == {
            name: db.execute('SELECT * FROM "' + name + '" ORDER BY 1').fetchall() for name in tables
        }
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    with sessions() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
        connections = db.scalars(select(IntegrationConnection)).all()
        assert len(connections) == 2
        assert {c.default_for for c in connections} == {"cloudflare", "jev"}
        for connection in connections:
            bindings = db.scalars(
                select(IntegrationVersionBinding).where(
                    IntegrationVersionBinding.connection_id == connection.id
                )
            ).all()
            assert len(bindings) == 2
            for binding in bindings:
                config = db.get(IntegrationConfig, binding.config_id)
                assert config.provider == connection.provider
                assert (
                    vault.decrypt(config.id, config.credential)
                    == "synthetic-existing-secret-" + connection.provider
                )
        assert db.get(PublicationResource, "project").config_id == "cloudflare1"
    engine.dispose()


def test_fresh_migration_does_not_create_unconfigured_connections(tmp_path):
    url = "sqlite:///" + str(tmp_path / "fresh.db")
    migrate(url, "upgrade", "head")
    engine, sessions = database(url)
    with sessions() as db:
        assert db.scalars(select(IntegrationConnection)).all() == []
        assert db.scalars(select(IntegrationVersionBinding)).all() == []
    engine.dispose()


def test_postgres_offline_migration_renders_backfill_values_without_parameters():
    sql = migrate("postgresql+psycopg://unused:unused@localhost/unused", "upgrade", "0004:0005", "--sql")
    assert "CREATE TABLE integration_connections" in sql
    assert "INSERT INTO integration_version_bindings" in sql
    assert "'Cloudflare principal'" in sql and "'Jev principal'" in sql
    assert ":provider" not in sql and ":identity" not in sql
