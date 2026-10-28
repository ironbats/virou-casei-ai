import json
import os
import tempfile
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import uvicorn
from sqlalchemy import text

from viracase.application.ports import ModelResult
from viracase.domain.rules import Access, utcnow
from viracase.infrastructure.config import Settings
from viracase.infrastructure.database import database
from viracase.infrastructure.models import Account, Base, Grant, Period, new_id
from viracase.infrastructure.providers import ModelCatalog, ProviderUncertain
from viracase.infrastructure.repository import HASHER, Repository
from viracase.infrastructure.sources import SourceStore
from viracase.infrastructure.worker import Worker
from viracase.interfaces.api import create_app

if os.environ.get("VIRA_E2E_ISOLATED") != "true":
    raise RuntimeError("Use somente no laboratório E2E isolado.")

temporary = tempfile.TemporaryDirectory(prefix="viracase-workspace-")
root = Path(temporary.name)
base = Path(__file__).resolve().parents[1]
config = Settings(
    _env_file=None,
    database_url="sqlite:///" + str(root / "test.db"),
    source_root=root / "sources",
    credential_key_path=root / "credentials.key",
    content_root=base / "content",
    catalog_path=root / "catalog.json",
    public_origin="http://127.0.0.1:5173,http://localhost:5173",
    admin_origin="http://127.0.0.1:5174,http://localhost:5174",
    public_hosts="127.0.0.1,localhost",
    cookie_secure=False,
    lifecycle_enabled=False,
    openai_api_key="synthetic-workspace-test-only",
)
policy = {
    "provider": "openai",
    "model": "gpt-6-luna",
    "version": "workspace-test",
    "enabled": True,
    "verified_account": True,
    # Binary-exact synthetic amounts avoid SQLite floating-point CHECK artefacts.
    # PostgreSQL production uses NUMERIC; financial contract tests exercise its pricing independently.
    "input_usd_per_million": "0",
    "cached_usd_per_million": "0",
    "output_usd_per_million": "125",
    "fx_brl_per_usd": "5",
    "fx_source": "synthetic-test",
    "evidence": "synthetic-only",
    "max_output_tokens": 16000,
    "effective_at": (utcnow() - timedelta(days=1)).isoformat(),
    "valid_until": (utcnow() + timedelta(days=1)).isoformat(),
}
config.catalog_path.write_text(json.dumps({"models": [policy]}))
engine, sessions = database(config.database_url)
Base.metadata.create_all(engine)
with engine.begin() as connection:
    connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
    connection.execute(text("INSERT INTO alembic_version VALUES ('0006')"))
repo = Repository(sessions, SourceStore(config.source_root), config)
with sessions.begin() as db:
    account = Account(
        id=new_id(),
        login="student-e2e",
        name="Aluno E2E",
        password_hash=HASHER.hash("e2e-student-only-password"),
        entitlement_version=1,
    )
    db.add(account)
    db.flush()
    access = Access.activate(utcnow())
    period = Period(
        id=new_id(),
        account_id=account.id,
        starts_at=access.starts_at,
        expires_at=access.expires_at,
        purge_at=access.purge_at,
        version=1,
    )
    db.add(period)
    db.flush()
    account.current_period_id = period.id
    db.add(
        Grant(
            account_id=account.id,
            period_id=period.id,
            kind="included",
            total=Decimal(50),
            available=Decimal(50),
        )
    )


class WorkspaceGateway:
    def generate(self, policy, prompt):
        data = json.loads(prompt[prompt.index('{"stack"') :])
        threading.Event().wait(3)
        if "SIMULAR_INCERTO" in data["request"]:
            raise ProviderUncertain("synthetic-only")
        usage = {"input": 100, "cached": 0, "output": 100}
        if "SIMULAR_TRUNCADO" in data["request"] or (
            "RECUPERAR_CORTE" in data["request"] and data.get("batch", 1) == 1
        ):
            return ModelResult('{"files":', usage, "synthetic", "GENERATION_TRUNCATED")
        if "SIMULAR_INVALIDO" in data["request"] or (
            "RECUPERAR_FORMATO" in data["request"] and data.get("batch", 1) == 1
        ):
            return ModelResult('{"files":', usage, "synthetic")
        current = data["files"]["public/index.html"]
        content = current.replace("</body>", "<p>Alteração E2E " + new_id() + "</p></body>")
        if policy.get("generation_contract_version") == 4:
            if "CONFIRMAR_OBJETIVO" in data["request"]:
                result = {
                    "action": "clarify",
                    "message": "Posso priorizar a apresentação e os botões da homepage?",
                    "files": [],
                    "edits": [],
                    "complete": True,
                }
            else:
                result = {
                    "action": "implement",
                    "message": "Melhorei a página inicial com uma apresentação mais clara e preservei os demais arquivos.",
                    "files": [{"path": "public/index.html", "content": content}],
                    "edits": [],
                    "complete": True,
                }
            return ModelResult(json.dumps(result), usage, "synthetic")
        if policy.get("generation_contract_version") == 3:
            return ModelResult(
                json.dumps(
                    {
                        "files": [{"path": "public/index.html", "content": content}],
                        "edits": [],
                        "complete": True,
                    }
                ),
                usage,
                "synthetic",
            )
        return ModelResult(json.dumps({"files": {"public/index.html": content}}), usage, "synthetic")


gateway = WorkspaceGateway()
catalog = ModelCatalog(config)
worker = Worker(repo, gateway, catalog)
stop = threading.Event()


def work():
    while not stop.is_set():
        worker.tick()
        stop.wait(0.1)


thread = threading.Thread(target=work, daemon=True)
thread.start()
try:
    uvicorn.run(
        create_app(False, (config, engine, repo, catalog, gateway)),
        host="127.0.0.1",
        port=8000,
        log_level="warning",
    )
finally:
    stop.set()
    thread.join(timeout=10)
    engine.dispose()
    temporary.cleanup()
