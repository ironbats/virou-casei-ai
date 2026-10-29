import json
import os
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from conftest import system as system_fixture
from sqlalchemy import select
from test_developer_recovery_acceptance import developer_case

from viracase.domain.rules import utcnow
from viracase.infrastructure.models import Operation, PublicationResource, Revision
from viracase.infrastructure.publication_recovery import recover_publications
from viracase.interfaces.api import create_app

if os.environ.get("VIRA_E2E_ISOLATED") != "true" or os.environ.get("TEST_DATABASE_URL"):
    raise RuntimeError("Use somente no laboratório E2E isolado, sem banco externo.")


class NoAI:
    def generate(self, *args, **kwargs):
        raise AssertionError("A recuperação determinística não pode chamar IA.")


def main():
    with tempfile.TemporaryDirectory(prefix="viracase-developer-recovery-") as directory:
        root = Path(directory)
        system_lifecycle = system_fixture.__wrapped__(root)
        system = next(system_lifecycle)
        monkeypatch = pytest.MonkeyPatch()
        case_lifecycle = developer_case.__wrapped__(system, monkeypatch)
        case = next(case_lifecycle)
        repo, project = case["repo"], case["project"]
        old_revision = project["current_revision_id"]
        files = repo.source_files(case["actor"], project["id"], old_revision)
        files["src/index.ts"] += "\n"
        current = repo.save_revision(
            case["actor"], project["id"], "e2e-current-revision",
            {"base_revision_id": old_revision, "files": files}, project["version"],
        )["project"]
        with repo.sessions.begin() as db:
            operation = db.get(Operation, case["operation"]["id"])
            operation.result = {
                **operation.result,
                "publication_failure": {
                    **operation.result["publication_failure"],
                    "provider": {
                        "method": "POST", "route": "/accounts/{account}/d1/database/{database}/query",
                        "status": 400, "provider_error_codes": [7500], "effect_outcome": "unknown",
                    },
                },
            }
            resource = db.get(PublicationResource, project["id"])
            previous = db.scalar(select(Revision).where(Revision.project_id == project["id"]).order_by(Revision.created_at))
            resource.state = "published"
            resource.inventory = {
                **resource.inventory,
                "published_revision_id": previous.id,
                "published_at": utcnow().isoformat(),
                "url": "https://previous-recovery-e2e.synthetic-test.workers.dev",
            }
        app = create_app(False, (
            system["config"], system["engine"], repo, system["catalog"], NoAI(),
        ))
        stopped = threading.Event()

        def work():
            while not stopped.wait(0.25):
                recover_publications(repo, operation_id=case["operation"]["id"])

        lifecycle = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application):
            async with lifecycle(application):
                worker = threading.Thread(target=work, daemon=True)
                worker.start()
                try:
                    yield
                finally:
                    stopped.set()
                    worker.join(timeout=10)

        app.router.lifespan_context = lifespan
        print(json.dumps({
            "project_id": project["id"], "operation_id": case["operation"]["id"],
            "original_revision_id": old_revision, "current_revision_id": current["current_revision_id"],
            "login": "student0", "password": "test-student-password",
            "previous_url": "https://previous-recovery-e2e.synthetic-test.workers.dev",
        }), flush=True)
        try:
            uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
        finally:
            stopped.set()
            case_lifecycle.close()
            monkeypatch.undo()
            system_lifecycle.close()


if __name__ == "__main__":
    main()

