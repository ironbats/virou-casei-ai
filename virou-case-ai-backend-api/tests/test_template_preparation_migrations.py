import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url

from viracase.domain.rules import Access, utcnow
from viracase.infrastructure.config import Settings
from viracase.infrastructure.database import database
from viracase.infrastructure.models import (
    Account,
    Draft,
    Grant,
    Ledger,
    Operation,
    Period,
    PurgeInventory,
    new_id,
)
from viracase.infrastructure.repository import Repository
from viracase.infrastructure.sources import SourceStore


def test_postgres_upgrade_and_rollback_preserve_sources_drafts_and_financial_history(tmp_path, monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url or not make_url(url).drivername.startswith("postgresql"):
        pytest.skip("Requer TEST_DATABASE_URL PostgreSQL real isolado.")
    parent = create_engine(url)
    schema = "migration_" + new_id().replace("-", "")
    with parent.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    parsed = make_url(url)
    scoped = parsed.set(query={**dict(parsed.query), "options": "-csearch_path=" + schema}).render_as_string(
        hide_password=False
    )
    config = Settings(
        _env_file=None,
        database_url=scoped,
        migration_database_url=scoped,
        source_root=tmp_path / "sources",
        credential_key_path=tmp_path / "credentials.key",
    )
    monkeypatch.setattr("viracase.infrastructure.config.settings", lambda: config)
    root = Path(__file__).parents[1]
    migration = Config(str(root / "alembic.ini"))
    migration.set_main_option("script_location", str(root / "migrations"))
    engine = None
    try:
        command.upgrade(migration, "0006")
        engine, sessions = database(scoped)
        actor, grant_id, period_id = new_id(), new_id(), new_id()
        with sessions.begin() as db:
            access = Access.activate(utcnow())
            db.add(
                Account(
                    id=actor,
                    login="migration-student",
                    name="Teste de migração",
                    password_hash="synthetic-not-a-password",
                    entitlement_version=1,
                )
            )
            db.flush()
            db.add(
                Period(
                    id=period_id,
                    account_id=actor,
                    starts_at=access.starts_at,
                    expires_at=access.expires_at,
                    purge_at=access.purge_at,
                    version=1,
                )
            )
            db.flush()
            db.get(Account, actor).current_period_id = period_id
            db.add(
                Grant(
                    id=grant_id,
                    account_id=actor,
                    period_id=period_id,
                    kind="included",
                    total=Decimal(50),
                    available=Decimal(49),
                    consumed=Decimal(1),
                )
            )
            db.flush()
            db.add(
                Ledger(
                    effect_key="synthetic-original-consumption",
                    account_id=actor,
                    grant_id=grant_id,
                    kind="consumption",
                    amount=Decimal(1),
                )
            )
        repo = Repository(sessions, SourceStore(config.source_root), config)
        project = repo.create_project(
            actor,
            "original-project",
            {"name": "Preservado", "objective": "Aplicação de teste para preservar dados"},
        )
        original_sources = repo.source_files(actor, project["id"], project["current_revision_id"])
        with sessions.begin() as db:
            db.add(
                Draft(
                    account_id=actor,
                    kind="build",
                    target=project["id"],
                    version=4,
                    data={
                        "stage": "review",
                        "message_text": "Pedido original",
                        "attachments": [
                            {
                                "template_id": "autorizacao",
                                "source_version_id": "original-v1",
                                "fields": {"field_8": "Negar sessão expirada"},
                                "edited_content": "Texto revisado",
                                "enabled": True,
                            }
                        ],
                    },
                )
            )
            db.add(
                Operation(
                    account_id=actor,
                    project_id=project["id"],
                    type="generation",
                    state="succeeded",
                    progress_stage="changes_applied",
                    payload={
                        "attachment_snapshots": [
                            {
                                "id": "original-snapshot",
                                "content": "Texto histórico revisado",
                                "content_hash": "synthetic-hash",
                            }
                        ]
                    },
                    result={"revision_id": project["current_revision_id"]},
                )
            )
        tables = (
            "accounts",
            "access_periods",
            "budget_grants",
            "ledger_entries",
            "projects",
            "revisions",
            "workspace_drafts",
            "operations",
        )

        def snapshot():
            with engine.connect() as connection:
                return json.dumps(
                    {
                        table: [
                            dict(row)
                            for row in connection.execute(
                                text(f'SELECT * FROM "{table}" ORDER BY id')
                            ).mappings()
                        ]
                        for table in tables
                    },
                    sort_keys=True,
                    default=str,
                )

        original = snapshot()
        command.upgrade(migration, "0007")
        inspector = inspect(engine)
        assert (
            next(
                column["type"].length
                for column in inspector.get_columns("purge_inventory")
                if column["name"] == "state"
            )
            == 32
        )
        inventory_id = new_id()
        with sessions.begin() as db:
            db.add(
                PurgeInventory(
                    id=inventory_id,
                    account_id=actor,
                    fence=new_id(),
                    destination="backups_not_managed",
                    state="requires_operator_evidence",
                    evidence={"reference": "synthetic-retained-evidence"},
                )
            )
        assert {"feature_policies", "integration_traces", "integration_trace_payloads"} <= set(
            inspector.get_table_names()
        )
        assert "ix_integration_traces_phase" in {
            index["name"] for index in inspector.get_indexes("integration_traces")
        }
        assert "ix_integration_trace_payloads_expires_at" in {
            index["name"] for index in inspector.get_indexes("integration_trace_payloads")
        }
        assert snapshot() == original
        assert repo.source_files(actor, project["id"], project["current_revision_id"]) == original_sources
        command.downgrade(migration, "0006")
        assert (
            next(
                column["type"].length
                for column in inspect(engine).get_columns("purge_inventory")
                if column["name"] == "state"
            )
            == 32
        )
        with sessions() as db:
            assert db.get(PurgeInventory, inventory_id).state == "requires_operator_evidence"
            assert db.get(PurgeInventory, inventory_id).evidence == {
                "reference": "synthetic-retained-evidence"
            }
        assert "integration_traces" not in inspect(engine).get_table_names()
        assert snapshot() == original
        command.upgrade(migration, "0007")
        assert snapshot() == original
        with sessions() as db:
            assert db.scalar(select(Grant).where(Grant.id == grant_id)).consumed == Decimal(1)
            assert db.get(PurgeInventory, inventory_id).state == "requires_operator_evidence"
        assert repo.source_files(actor, project["id"], project["current_revision_id"]) == original_sources
    finally:
        if engine is not None:
            engine.dispose()
        with parent.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        parent.dispose()
