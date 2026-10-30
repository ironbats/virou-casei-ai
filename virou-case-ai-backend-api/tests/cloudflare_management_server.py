import json
import os
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from conftest import system as system_fixture
from sqlalchemy import func, select
from test_publication_management_snapshot import live_worker, scenario
from test_publication_pipeline import publication as publication_fixture

from viracase.infrastructure.models import Audit, Job, Operation
from viracase.infrastructure.publication_management import publication_diagnostics
from viracase.interfaces.api import create_app


def main():
    if os.environ.get("VIRA_E2E_ISOLATED") != "true" or os.environ.get("TEST_DATABASE_URL"):
        raise RuntimeError("Use somente no laboratório E2E isolado, sem banco externo.")
    with tempfile.TemporaryDirectory(prefix="viracase-cloudflare-management-") as directory:
        lifecycle = system_fixture.__wrapped__(Path(directory))
        system = next(lifecycle)
        monkeypatch = pytest.MonkeyPatch()
        publication = publication_fixture.__wrapped__(system, monkeypatch)
        config, current, previous = scenario(system, publication)
        system["config"].admin_origin = "http://127.0.0.1:5175"
        repo = system["repo"]
        sample = publication_diagnostics(repo, system["admin"], config["connection_id"])
        output = Path(os.environ.get("VIRA_E2E_OUTPUT", directory))
        output.mkdir(parents=True, exist_ok=True)
        (output / "cloudflare-real-contract.json").write_text(json.dumps(sample, ensure_ascii=False, indent=2))
        (output / "cloudflare-real-connection.json").write_text(json.dumps(publication[0].get("cloudflare"), ensure_ascii=False, indent=2))

        def counters():
            with repo.sessions() as db:
                return {model.__tablename__: db.scalar(select(func.count()).select_from(model)) for model in (Operation, Job, Audit)}

        (output / "cloudflare-live-before.json").write_text(json.dumps(counters(), indent=2))
        app = create_app(True, (system["config"], system["engine"], repo, system["catalog"], None))
        stop = threading.Event()
        original_lifespan = app.router.lifespan_context

        def heartbeat():
            while not stop.wait(20):
                live_worker(repo)

        @asynccontextmanager
        async def lifespan(application):
            async with original_lifespan(application):
                thread = threading.Thread(target=heartbeat, daemon=True)
                thread.start()
                try:
                    yield
                finally:
                    stop.set()
                    thread.join(timeout=5)
                    (output / "cloudflare-live-after.json").write_text(json.dumps(counters(), indent=2))

        app.router.lifespan_context = lifespan
        print(json.dumps({"connection_id": config["connection_id"], "current_run_id": current, "previous_run_id": previous,
                          "login": "operator", "password": "test-operator-password", "synthetic": True}), flush=True)
        try:
            uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
        finally:
            stop.set()
            monkeypatch.undo()
            lifecycle.close()


if __name__ == "__main__":
    main()
