"""Fixture local: compilador real e persistência reais; nenhum serviço externo."""

import json
import os
import secrets
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select
from test_publication_pipeline import POLICY, qualify

from viracase.domain.rules import utcnow
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway
from viracase.infrastructure.credentials import initialize_key
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Account, Job, new_id
from viracase.infrastructure.publication import PublicationService
from viracase.infrastructure.publication_build import IsolatedBuild
from viracase.infrastructure.publication_logging import publication_context
from viracase.infrastructure.publication_worker import execute_publication
from viracase.infrastructure.repository import HASHER
from viracase.infrastructure.worker import Worker

ROOT = Path(__file__).resolve().parents[1]


class LocalCompilerFixture(IsolatedBuild):
    """Usada somente no laboratório; não certifica o sandbox de produção."""

    def fingerprint(self):
        return "f" * 64

    def available(self):
        return True

    def build(self, files, stack, revision, secrets=()):
        node = shutil.which("node")
        if not node or not (ROOT / "build-toolchain/node_modules/esbuild").is_dir():
            raise RuntimeError("Instale a toolchain fixada antes de iniciar a fixture de compilação.")
        original_run = subprocess.run

        def run_local(command, **kwargs):
            if command[1:3] != ["-m", "viracase.infrastructure.build_limits"]:
                raise AssertionError("A fixture só pode executar o compilador local esperado.")
            return original_run(
                [node, str(ROOT / "build-toolchain/build.mjs"), command[4], command[5]],
                cwd=command[3],
                check=False,
                **kwargs,
            )

        with patch("viracase.infrastructure.publication_build.subprocess.run", run_local):
            return super().build(files, stack, revision, secrets)


def seed_publication_build_failure(repo, catalog, actor):
    if os.environ.get("VIRA_E2E_ISOLATED") != "true" or os.environ.get("VIRA_E2E_BUILD_DIAGNOSTIC") != "true":
        raise RuntimeError("A fixture de compilação exige ativação explícita no laboratório isolado.")
    with repo.sessions.begin() as db:
        admin = db.scalar(select(Account).where(Account.role == "admin").limit(1))
        if admin is None:
            admin = Account(
                id=new_id(),
                login="build-diagnostic-e2e-admin",
                name="Operador sintético de diagnóstico",
                role="admin",
                password_hash=HASHER.hash(secrets.token_urlsafe(32)),
            )
            db.add(admin)
            db.flush()
        admin_id = admin.id
    initialize_key(repo.config.credential_key_path, repo.sessions)
    registry = Integrations(repo)
    registry.save(
        admin_id,
        "cloudflare",
        "build-diagnostic-fixture-save",
        0,
        {
            "settings": POLICY,
            "token": "synthetic-cloudflare-token",
            "reason": "Fixture isolada de compilação real",
        },
    )
    validation = {"ready": True, "subdomain": "synthetic-test", "database_ids": [], "workers_count": 0}
    with patch.object(CloudflareGateway, "validate", return_value=validation):
        registry.validate(admin_id, "cloudflare", 1, "Validação sintética sem chamadas externas")
    registry.activate(
        admin_id, "cloudflare", "build-diagnostic-fixture-activate", 1, "Ativação da fixture isolada"
    )
    qualify({"repo": repo, "admin": admin_id}, registry)
    project = repo.create_project(
        actor,
        "build-diagnostic-fixture-project",
        {"name": "Diagnóstico real de compilação", "objective": "Lista de tarefas da equipe"},
    )
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    files["src/index.ts"] = (
        'import { Hono } from "hono";\n'
        "const app = new Hono();\n"
        'const = "synthetic-private-source";\n'
        "export default app;\n"
    )
    saved = repo.save_revision(
        actor,
        project["id"],
        "build-diagnostic-fixture-revision",
        {"base_revision_id": project["current_revision_id"], "files": files},
        project["version"],
    )
    revision = saved["project"]["current_revision_id"]
    builder = LocalCompilerFixture()

    def no_remote(*args, **kwargs):
        raise AssertionError("A falha de compilação deve ocorrer antes de qualquer serviço externo.")

    with patch.object(repo.config, "lifecycle_enabled", True):
        operation = PublicationService(repo, builder).start(
            actor, project["id"], "build-diagnostic-fixture-operation", {"revision_id": revision}
        )
        with repo.sessions.begin() as db:
            job = db.scalar(select(Job).where(Job.operation_id == operation["id"]))
            job.state, job.lease_token = "running", "build-diagnostic-fixture-lease"
            job.lease_until = utcnow() + timedelta(minutes=5)
        with publication_context(operation_id=operation["id"]):
            execute_publication(
                Worker(repo, None, catalog),
                operation["id"],
                "build-diagnostic-fixture-lease",
                no_remote,
                builder,
            )
    result = repo.operation(actor, operation["id"])
    diagnostic = result["result_ref"].get("publication_failure", {}).get("build")
    if result["error_code"] != "BUILD_FAILED" or not diagnostic or diagnostic["tool"] != "esbuild":
        raise AssertionError("A fixture não produziu o diagnóstico real esperado.")
    metadata = {
        "event": "e2e_real_compiler_fixture",
        "project_id": project["id"],
        "operation_id": operation["id"],
        "revision_id": revision,
        "compiler": "esbuild",
        "external_requests": 0,
        "diagnostic": diagnostic,
    }
    print(json.dumps(metadata, ensure_ascii=False))
    return metadata
