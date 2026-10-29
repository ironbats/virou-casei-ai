import json
import os
from hashlib import sha256

from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError
from viracase.infrastructure.models import IntegrationConfig, Operation, PublicationResource, new_id
from viracase.infrastructure.publication_verification import failure_record

from test_publication_pipeline import POLICY

OBJECTIVE = "Validar a continuidade da correção após uma pergunta no acompanhamento de publicação."
QUESTION = "Podemos manter os dados atuais e concluir a correção do cadastro?"
ANSWER = "Sim, mantenha os dados atuais e conclua a correção."
MIGRATION = "migrations/0002.sql"
ORIGINAL_SQL = "CREATE TABLE correction_requests (id TEXT PRIMARY KEY, title TEXT NOT NULL);\n"
INCREMENTAL_SQL = "ALTER TABLE correction_requests ADD COLUMN note TEXT;\n"
OBSOLETE = {
    "src/domain/followup-obsolete.ts": "",
    "src/application/followup-obsolete.ts": "export const obsoleteApplication = true;\n",
    "src/infrastructure/followup-obsolete.ts": "export const obsoleteInfrastructure = true;\n",
    "public/followup-obsolete.js": "export const obsoleteFrontend = true;\n",
}


def seed_correction_followup(repo, actor):
    if (
        os.environ.get("VIRA_E2E_ISOLATED") != "true"
        or os.environ.get("VIRA_E2E_CORRECTION_FOLLOWUP") != "true"
    ):
        raise RuntimeError("A fixture exige o laboratório isolado de continuação da correção.")
    project = repo.create_project(
        actor, "correction-followup-project", {"name": "Correção após pergunta", "objective": OBJECTIVE}
    )
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    files.update(OBSOLETE)
    files[MIGRATION] = ORIGINAL_SQL
    while len(files) < 40:
        index = len(files)
        files[f"public/followup-preserved-{index}.js"] = f"export const preserved{index} = true;\n"
    original = repo.save_revision(
        actor,
        project["id"],
        "correction-followup-historical",
        {"base_revision_id": project["current_revision_id"], "files": files},
        project["version"],
    )["project"]
    inventory = {
        "published_revision_id": original["current_revision_id"],
        "migrations": {
            path: sha256(content.encode()).hexdigest()
            for path, content in files.items()
            if path.startswith("migrations/")
        },
    }
    current = original
    diagnostic = failure_record(
        RuleError("BUILD_FAILED", "Falha sintética de compilação: arquivos obsoletos precisam ser corrigidos."),
        "publication_building",
        current["stack"],
    )
    with repo.sessions.begin() as db:
        config = IntegrationConfig(
            provider="cloudflare",
            version=1,
            state="draft",
            settings={**POLICY, "account_id": "c" * 32},
            created_by=actor,
        )
        db.add(config)
        db.flush()
        db.add(PublicationResource(
            project_id=project["id"],
            account_id=actor,
            config_id=config.id,
            remote_account_id="c" * 32,
            worker_name="synthetic-followup-e2e",
            database_id=new_id(),
            inventory=inventory,
            state="active",
        ))
        publication = Operation(
            account_id=actor,
            project_id=project["id"],
            type="publication",
            state="failed",
            progress_stage="publication_failed",
            payload={"revision_id": current["current_revision_id"]},
            result={"publication_failure": diagnostic},
            error_code="BUILD_FAILED",
        )
        db.add(publication)
        db.flush()
        return {"project_id": project["id"], "operation_id": publication.id}


def correction_followup_result(policy, data):
    if data.get("objective") != OBJECTIVE:
        return None
    files = data["files"]
    paths = list(OBSOLETE)
    if policy.get("code_analysis_contract"):
        assert data["evidence"]["publication"]["code"] == "BUILD_FAILED"
        historical = data["evidence"]["published_database_migrations"]["files"]
        assert any(item.get("path") == MIGRATION and item.get("status") == "current_matches" and item.get("sha256") == sha256(ORIGINAL_SQL.encode()).hexdigest() for item in historical)
        result = {
            "summary": "A compilação falhou; quatro arquivos obsoletos precisam ser removidos e o banco evoluído por migration nova.",
            "issues": [
                {
                    "severity": "error",
                    "path": MIGRATION,
                    "line": 1,
                    "title": "Evolução incremental do banco",
                    "explanation": "A nova coluna deve ser criada preservando o histórico confirmado do banco.",
                    "suggestion": "Mantenha o SQL original e crie uma migration incremental.",
                },
                *[
                    {
                        "severity": "warning",
                        "path": path,
                        "line": 0,
                        "title": "Arquivo obsoleto",
                        "explanation": "Arquivo sintético sem uso na aplicação.",
                        "suggestion": "Remova este arquivo preservando os demais.",
                    }
                    for path in paths
                ],
            ],
            "limitations": ["Provedor e publicação simulados; API, worker e persistência reais."],
        }
    else:
        request = json.loads(data["request"])
        assert request["untrusted_code_analysis"]["evidence"]["publication"]["matches_revision"]
        message = request["message"]
        result = {
            "action": "implement",
            "message": "Preparei a correção com os arquivos já processados preservados.",
            "files": [],
            "edits": [],
            "deletes": [],
            "complete": False,
        }
        if message == ANSWER:
            assert any(item["response"] == QUESTION for item in data["history"])
            assert files[MIGRATION] == ORIGINAL_SQL
            assert all(path not in files for path in paths[:2])
            if paths[2] in files:
                result["deletes"] = paths
            else:
                result.update(
                    files=[
                        {"path": "migrations/0003.sql", "content": INCREMENTAL_SQL},
                        {
                            "path": "public/index.html",
                            "content": files["public/index.html"].replace(
                                "</body>", "<p>Correção após resposta concluída</p></body>"
                            ),
                        },
                    ],
                    deletes=paths,
                    complete=True,
                    message="Preservei o histórico, preparei a migration incremental e removi os arquivos obsoletos.",
                )
        elif paths[0] in files:
            result.update(files=[{"path": MIGRATION, "content": ORIGINAL_SQL}], deletes=paths[:2])
        else:
            result.update(action="clarify", message=QUESTION, complete=True)
    return ModelResult(
        json.dumps(result, ensure_ascii=False),
        {"input": 100, "cached": 0, "output": 100},
        "synthetic-correction-followup",
    )
