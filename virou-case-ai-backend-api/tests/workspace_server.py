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
    connection.execute(text("INSERT INTO alembic_version VALUES ('0007')"))
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
            total=Decimal(250),
            available=Decimal(250),
        )
    )


class WorkspaceGateway:
    def __init__(self):
        from realistic_preparation_gateway import RealisticPreparationGateway

        self.realistic_preparation = RealisticPreparationGateway(config)

    def generate(self, policy, prompt):
        if policy.get("template_preparation_contract"):
            from test_template_preparations import PreparationGateway

            threading.Event().wait(1)
            request = json.loads(prompt)
            from realistic_preparation_gateway import prompt_sources

            message = next(item["content"] for item in prompt_sources(request) if item["id"] == "request:message")
            if "cadastro e login de clientes na padaria" in message or message.startswith(
                ("Gerar campo pendente no pedido atual.", "Falha ao gerar campo pendente.")
            ):
                return self.realistic_preparation.generate(policy, prompt)
            return PreparationGateway().generate(policy, prompt)
        data = json.loads(prompt[prompt.index('{"stack"') :])
        ten_calls = "CONSTRUIR_DEZ_ETAPAS" in data["request"]
        threading.Event().wait(0.9 if ten_calls else 3)
        if "SIMULAR_INCERTO" in data["request"]:
            raise ProviderUncertain("synthetic-only")
        usage = {"input": 100, "cached": 0, "output": 100}
        turn_message = data["request"]
        try:
            context = json.loads(turn_message)
            if isinstance(context, dict):
                turn_message = context.get("message", turn_message)
        except ValueError:
            pass
        history = data.get("history", [])
        partial_question = "Como confirmar a sessão para concluir o carrinho?"
        retention_question = "Posso manter a sessão confirmada por 30 minutos?"
        initial_partial_request = "ESCLARECER_PARCIAL" in turn_message
        answering_partial = bool(
            history and history[-1].get("response") in (partial_question, retention_question)
        )
        if initial_partial_request or answering_partial:
            partial_path = "public/carrinho-parcial.js"
            if initial_partial_request and data.get("batch", 1) == 1:
                result = {
                    "action": "implement",
                    "message": "Preparei o carrinho antes de confirmar a sessão.",
                    "files": [
                        {
                            "path": partial_path,
                            "content": "export const estado = 'preparado antes da pergunta';\n"
                            + f"export const referencia = '{new_id()}';\n",
                        }
                    ],
                    "edits": [],
                    "complete": False,
                }
            elif initial_partial_request:
                result = {
                    "action": "clarify",
                    "message": partial_question,
                    "files": [],
                    "edits": [],
                    "complete": True,
                }
            else:
                assert partial_path in data["files"], "O checkpoint anterior à pergunta foi perdido"
                assert any("ESCLARECER_PARCIAL" in item["request"] for item in history)
                serialized = json.dumps({"request": data["request"], "history": history}, ensure_ascii=False)
                assert "ANEXO_ESCLARECIMENTO_INTEGRAL" in serialized, "O anexo original foi perdido"
                message = turn_message
                if message == "Opção 2":
                    result = {
                        "action": "clarify",
                        "message": retention_question,
                        "files": [],
                        "edits": [],
                        "complete": True,
                    }
                else:
                    assert message == "sim", "A resposta curta não foi preservada"
                    assert any("Opção 2" in item["request"] for item in history)
                    if "RECUPERAR_DOIS_CORTES" in serialized and data.get("batch", 1) <= 2:
                        return ModelResult(
                            '{"files":',
                            usage,
                            f"synthetic-clarification-truncated-{data.get('batch', 1)}",
                            "GENERATION_TRUNCATED",
                            {"finish_reason": "MAX_TOKENS", "visible_output_tokens": 100},
                        )
                    current = data["files"]["public/index.html"]
                    result = {
                        "action": "implement",
                        "message": "Concluí o carrinho usando as decisões e os arquivos já preparados.",
                        "files": [
                            {
                                "path": "public/index.html",
                                "content": current.replace("</body>", "<p>Carrinho confirmado</p></body>"),
                            },
                            {
                                "path": "public/confirmacao-contexto.json",
                                "content": json.dumps(
                                    {
                                        "request": data["request"],
                                        "history": history,
                                        "partial": data["files"][partial_path],
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        ],
                        "edits": [],
                        "complete": True,
                    }
            return ModelResult(json.dumps(result, ensure_ascii=False), usage, "synthetic-clarification")
        if ten_calls:
            batch = data.get("batch", 1)
            path = f"public/construcao-etapa-{batch:02d}.js"
            changes = [
                {
                    "path": path,
                    "content": f"export const etapa = {batch};\n"
                    + "export const referencia = " + json.dumps(data["request"], ensure_ascii=False) + ";\n",
                }
            ]
            complete = batch == 10 and "MANTER_INCOMPLETO" not in data["request"]
            if complete:
                current = data["files"]["public/index.html"]
                changes.append(
                    {
                        "path": "public/index.html",
                        "content": current.replace(
                            "</body>",
                            '<p data-etapas="10">Construção concluída em dez etapas</p></body>',
                        ),
                    }
                )
            return ModelResult(
                json.dumps(
                    {
                        "action": "implement",
                        "message": f"Etapa {batch} preparada com os arquivos anteriores preservados.",
                        "files": changes,
                        "edits": [],
                        "complete": complete,
                    }
                ),
                usage,
                f"synthetic-ten-calls-{batch}",
            )
        if "SIMULAR_TRUNCADO" in data["request"] or (
            "RECUPERAR_CORTE" in data["request"] and data.get("batch", 1) == 1
        ):
            return ModelResult('{"files":', usage, "synthetic", "GENERATION_TRUNCATED")
        if "SIMULAR_INVALIDO" in data["request"] or (
            "RECUPERAR_FORMATO" in data["request"] and data.get("batch", 1) == 1
        ):
            return ModelResult('{"files":', usage, "synthetic")
        if "CONTEXTO_CRESCENTE" in data["request"] and data.get("batch", 1) == 1:
            products = [
                {"id": index, "name": f"Produto da padaria {index}", "price_cents": 500 + index}
                for index in range(400)
            ]
            return ModelResult(
                json.dumps(
                    {
                        "action": "implement",
                        "message": "Preparei a lista de produtos e vou concluir a página inicial no próximo lote.",
                        "files": [
                            {
                                "path": "public/carrossel.js",
                                "content": "export const products = "
                                + json.dumps(products, ensure_ascii=False)
                                + ";\n",
                            }
                        ],
                        "edits": [],
                        "complete": False,
                    }
                ),
                usage,
                "synthetic-context-first-batch",
            )
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
if os.environ.get("VIRA_E2E_BUILD_DIAGNOSTIC") == "true":
    from publication_diagnostic_fixture import seed_publication_build_failure

    seed_publication_build_failure(repo, catalog, account.id)
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
