import json

from viracase.application.ports import ModelResult

OBSOLETE_FILES = {
    "src/domain/user.ts": "",
    "src/application/authenticate-user.ts": (
        'import type { User } from "../domain/user";\n'
        "export const diagnosticFixtureObsoleteAuthentication = true;\n"
    ),
    "src/infrastructure/d1-user-repository.ts": (
        'import type { UserRepository } from "../domain/user";\n'
        "export const diagnosticFixtureObsoleteRepository = true;\n"
    ),
    "src/infrastructure/health-check-service.ts": "export const diagnosticFixtureObsoleteHealthCheck = true;\n",
}


def obsolete_files_result(policy, data):
    files = data["files"]
    if not all(path in files and files[path] == content for path, content in OBSOLETE_FILES.items()):
        return None
    if policy.get("code_analysis_contract"):
        return {
            "summary": "Foram encontrados quatro arquivos obsoletos, incluindo um arquivo de domínio vazio.",
            "issues": [
                {
                    "severity": "error" if path == "src/domain/user.ts" else "warning",
                    "path": path,
                    "line": 0 if not content else 1,
                    "title": "Arquivo de domínio vazio" if not content else "Arquivo redundante",
                    "explanation": "O arquivo pertence ao cenário sintético de código obsoleto e não é usado pelo entrypoint.",
                    "suggestion": "Remova este arquivo e preserve as implementações atuais e os demais arquivos.",
                }
                for path, content in OBSOLETE_FILES.items()
            ],
            "limitations": ["Provedor sintético exclusivo do laboratório E2E; nenhuma compilação foi executada."],
        }
    if "Corrija os problemas de código identificados" not in data.get("request", ""):
        return None
    if not policy.get("generation_full_files"):
        return {
            "action": "implement",
            "message": "Solicitei a remoção dos arquivos obsoletos.",
            "files": [],
            "edits": [{"path": "src/domain/user.ts", "old": "export interface User { id: string; }", "new": ""}],
            "deletes": [],
            "complete": True,
        }
    return {
        "action": "implement",
        "message": "Preparei a remoção dos quatro arquivos obsoletos e preservei os demais arquivos.",
        "files": [],
        "deletes": list(OBSOLETE_FILES),
        "complete": True,
    }


def code_analysis_result(policy, prompt):
    data = json.loads(prompt[prompt.index('{"stack"') :])
    from correction_followup_fixture import correction_followup_result

    followup = correction_followup_result(policy, data)
    if followup is not None:
        return followup
    files = data["files"]
    path, marker = "src/index.ts", "const = 1;"
    source = files.get(path, "")
    obsolete = obsolete_files_result(policy, data)
    if obsolete is not None:
        result = obsolete
    elif policy.get("code_analysis_contract"):
        line = next((index for index, value in enumerate(source.splitlines(), 1) if marker in value), 0)
        result = {
            "summary": "Foi encontrada uma declaração inválida no código."
            if line
            else "Nenhum problema identificado nesta análise de teste.",
            "issues": [
                {
                    "severity": "error",
                    "path": path,
                    "line": line,
                    "title": "Declaração inválida",
                    "explanation": "A declaração const não informa o nome da variável antes do sinal de igualdade.",
                    "suggestion": "Informe um nome de variável válido preservando o restante do arquivo.",
                }
            ]
            if line
            else [],
            "limitations": ["Provedor sintético exclusivo do laboratório E2E."],
        }
    elif marker in source and "Corrija os problemas de código identificados" in data.get("request", ""):
        result = {
            "action": "implement",
            "message": "Corrigi a declaração inválida e preservei os demais arquivos.",
            "files": [{"path": path, "content": source.replace(marker, "const diagnosticValue = 1;")}],
            "edits": [],
            "complete": True,
        }
    else:
        return None
    return ModelResult(
        json.dumps(result, ensure_ascii=False),
        {"input": 100, "cached": 0, "output": 100},
        "synthetic-code-analysis",
    )
