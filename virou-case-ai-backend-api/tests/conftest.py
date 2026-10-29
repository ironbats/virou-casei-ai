import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from viracase.domain.rules import Access, utcnow
from viracase.infrastructure.config import Settings
from viracase.infrastructure.database import database
from viracase.infrastructure.models import Account, Base, Grant, Period, new_id
from viracase.infrastructure.providers import ModelCatalog
from viracase.infrastructure.repository import HASHER, Repository
from viracase.infrastructure.sources import SourceStore


@pytest.fixture
def system(tmp_path):
    url = os.getenv("TEST_DATABASE_URL")
    schema = "test_" + new_id().replace("-", "")
    parent = None
    if url:
        parent = create_engine(url)
        with parent.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        parsed = make_url(url)
        query = dict(parsed.query)
        query["options"] = "-csearch_path=" + schema
        url = parsed.set(query=query).render_as_string(hide_password=False)
    else:
        url = "sqlite:///" + str(tmp_path / "test.db")
    engine, sessions = database(url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('0007')"))
    policy = {
        "provider": "openai",
        "model": "gpt-6-luna",
        "version": "test-v1",
        "enabled": True,
        "verified_account": True,
        "input_usd_per_million": "0.10",
        "cached_usd_per_million": "0.01",
        "output_usd_per_million": "0.50",
        "fx_brl_per_usd": "5.00",
        "fx_source": "synthetic-test",
        "effective_at": (utcnow() - timedelta(days=1)).isoformat(),
        "valid_until": (utcnow() + timedelta(days=1)).isoformat(),
        "max_output_tokens": 16000,
        "evidence": "synthetic-contract-tests",
    }
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps({"models": [policy]}))
    config = Settings(
        _env_file=None,
        public_origin="http://127.0.0.1:5173",
        admin_origin="http://127.0.0.1:5174",
        database_url=url,
        source_root=tmp_path / "sources",
        credential_key_path=tmp_path / "secrets/credentials.key",
        content_root=Path(__file__).parents[1] / "content",
        catalog_path=catalog_path,
        openai_api_key="synthetic-test-token-not-a-real-key",
    )
    repo = Repository(sessions, SourceStore(config.source_root, (config.openai_api_key,)), config)
    with sessions.begin() as db:
        admin = Account(
            id=new_id(),
            login="operator",
            name="Operador de teste",
            role="admin",
            password_hash=HASHER.hash("test-operator-password"),
        )
        db.add(admin)
        users = []
        for n in range(2):
            a = Account(
                id=new_id(),
                login=f"student{n}",
                name=f"Aluno {n}",
                password_hash=HASHER.hash("test-student-password"),
                entitlement_version=1,
            )
            db.add(a)
            db.flush()
            access = Access.activate(utcnow())
            p = Period(
                id=new_id(),
                account_id=a.id,
                starts_at=access.starts_at,
                expires_at=access.expires_at,
                purge_at=access.purge_at,
                version=1,
            )
            db.add(p)
            db.flush()
            a.current_period_id = p.id
            db.add(
                Grant(
                    id=new_id(),
                    account_id=a.id,
                    period_id=p.id,
                    kind="included",
                    total=Decimal(50),
                    available=Decimal(50),
                )
            )
            users.append(a.id)
    catalog = ModelCatalog(config)
    yield {
        "repo": repo,
        "engine": engine,
        "config": config,
        "catalog": catalog,
        "policy": policy,
        "admin": admin.id,
        "users": users,
    }
    engine.dispose()
    if parent:
        with parent.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        parent.dispose()
