import json
import re
import unicodedata
from pathlib import PurePosixPath

from viracase.domain.rules import RuleError
from viracase.infrastructure.publication_diagnostics import REMEDIATIONS, classify_failure
from viracase.infrastructure.publication_logging import safe_text

CAPTURE_BYTES = 65_536
MAX_ISSUES = 12
MESSAGE_CHARS = 500
MESSAGE_INPUT_CHARS = 4096
ISSUES = {
    "SYNTAX_ERROR": (
        "source",
        "Confira a sintaxe no arquivo e na posição indicados, salve uma revisão e publique novamente.",
    ),
    "IMPORT_NOT_FOUND": (
        "source",
        "Confira o caminho e a capitalização do import; preserve os arquivos importados e use somente as dependências do catálogo.",
    ),
    "EXPORT_NOT_FOUND": (
        "source",
        "Confira os exports do módulo importado. O arquivo de entrada precisa exportar a aplicação esperada pela stack.",
    ),
    "DEPENDENCY_OUTSIDE_CATALOG": (
        "dependency",
        "Use somente as dependências aprovadas para esta stack. Instalação dinâmica não é permitida no executor.",
    ),
    "IMPORT_OUTSIDE_CATALOG": (
        "dependency",
        "Remova imports de rede, data URLs ou módulos Node não permitidos e use o catálogo da stack.",
    ),
    "INVALID_PACKAGE_JSON": (
        "source",
        "Corrija o package.json e mantenha as dependências aprovadas da stack.",
    ),
    "PROJECT_VALIDATION_FAILED": (
        "source",
        "Corrija a estrutura e os arquivos indicados pela validação da revisão antes de publicar.",
    ),
    "PYTHON_ENTRYPOINT_MISSING": (
        "source",
        "Restaure src/entry.py com a aplicação FastAPI antes de publicar.",
    ),
    "PYTHON_VENDOR_NOT_QUALIFIED": (
        "executor",
        "Recrie o worker com o perfil Python aprovado e execute a verificação de publicação no backoffice.",
    ),
    "TOOLCHAIN_DEPENDENCY_MISSING": (
        "executor",
        "Recrie a imagem do worker com a toolchain fixada e execute a verificação de publicação no backoffice.",
    ),
    "EXECUTOR_UNAVAILABLE": (
        "executor",
        "Confira a disponibilidade do worker e execute o diagnóstico da publicação no backoffice.",
    ),
    "BUILD_RUNTIME_FAILED": (
        "executor",
        "O processo de preparação falhou internamente. Confira a versão da toolchain e os logs correlacionados do worker.",
    ),
    "EXECUTOR_TERMINATED": (
        "executor",
        "Confira o sinal de encerramento e os limites de recursos do worker nos logs da operação; não remova a contenção.",
    ),
    "BUILD_TIMEOUT": (
        "executor",
        "A preparação excedeu o prazo. Confira recursos do executor e o tamanho do projeto antes de tentar novamente.",
    ),
    "BUILD_OUTPUT_LIMIT": (
        "output",
        "O artefato excedeu o limite do executor. Reduza dependências e conteúdo incluído no bundle.",
    ),
    "BUILD_OUTPUT_INVALID": (
        "output",
        "O executor não devolveu o contrato esperado. Confira a versão do worker e reexecute a verificação de publicação no backoffice.",
    ),
    "BUILD_PROCESS_FAILED": (
        "unknown",
        "Consulte os logs correlacionados desta operação. O processo não informou um diagnóstico reconhecido; não há arquivo ou linha comprovados.",
    ),
}
ALIASES = {
    "dependency_outside_catalog": "DEPENDENCY_OUTSIDE_CATALOG",
    "python_vendor_not_qualified": "PYTHON_VENDOR_NOT_QUALIFIED",
    "python_entrypoint_missing": "PYTHON_ENTRYPOINT_MISSING",
}


def clean_message(value, secrets=()):
    text = value if isinstance(value, str) else ""
    if len(text) > MESSAGE_INPUT_CHARS:
        return "O diagnóstico excedeu o limite de mensagem; consulte a orientação e os identificadores desta operação."
    text = re.sub(r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c|$)", "", text)
    text = re.sub(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]", "", text)
    text = safe_text(text, secrets, limit=CAPTURE_BYTES)
    text = "".join(char for char in text if unicodedata.category(char) not in {"Cf", "Cc"} or char in "\n\t")
    text = safe_text(text, secrets, limit=CAPTURE_BYTES)
    text = re.sub(r"(?i)\b(?:https?|postgres(?:ql)?|mysql|redis)://\S+", "[REDACTED_URL]", text)
    text = re.sub(r"(?i)\b[a-z]:[\\/][^\s\"'<>]+", "[path]", text)
    text = re.sub(r"(?<![\w.])/(?:[^\s\"'<>]+)", "[path]", text)
    text = next((line for line in text.splitlines() if line.strip()), "")
    return " ".join(text.split())[:MESSAGE_CHARS]


def diagnostic_location(item, files, secrets=(), *, byte_column=False):
    name = item.get("path")
    if not isinstance(name, str) or len(name) > 200:
        return {}
    if name.startswith("/project/"):
        name = name[len("/project/") :]
    elif name.startswith("./"):
        name = name[2:]
    if (
        name not in files
        or not isinstance(files.get(name), str)
        or name.startswith("/")
        or "\\" in name
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or "/".join(PurePosixPath(name).parts) != name
        or any(unicodedata.category(char) in {"Cf", "Cc"} for char in name)
        or safe_text(name, secrets, limit=201) != name
    ):
        return {}
    result = {"path": name}
    line = item.get("line")
    lines = files[name].splitlines() or [""]
    if files[name].endswith(("\n", "\r")):
        lines.append("")
    if type(line) is not int or not 1 <= line <= len(lines):
        return result
    result["line"] = line
    column = item.get("column")
    if type(column) is not int:
        return result
    if byte_column and 0 <= column <= len(lines[line - 1].encode("utf-8")):
        column = len(lines[line - 1].encode("utf-8")[:column].decode("utf-8", errors="ignore")) + 1
    if 1 <= column <= len(lines[line - 1]) + 1:
        result["column"] = column
    return result


def issue_code(message, supplied=None):
    if isinstance(supplied, str) and (supplied in ISSUES or supplied in REMEDIATIONS):
        return supplied
    lower = message.strip().lower()
    sentinel = lower.removeprefix("error: ")
    for alias, code in ALIASES.items():
        if sentinel == alias:
            return code
    if sentinel == "import outside catalog":
        return "IMPORT_OUTSIDE_CATALOG"
    if lower.startswith("could not resolve "):
        return "IMPORT_NOT_FOUND"
    if lower.startswith(("no matching export", "no default export")):
        return "EXPORT_NOT_FOUND"
    if "cannot find module" in lower or "err_module_not_found" in lower:
        return "TOOLCHAIN_DEPENDENCY_MISSING"
    if any(
        value in lower
        for value in (
            "expected ",
            "unexpected ",
            "unterminated ",
            "invalid syntax",
            "syntaxerror",
            "has already been declared",
        )
    ):
        return "SYNTAX_ERROR"
    return "BUILD_PROCESS_FAILED"


def normalized_issue(item, files, secrets=(), *, byte_column=False):
    raw = item.get("message", "")
    message = clean_message(raw, secrets)
    code = issue_code(message, item.get("code"))
    if not message:
        message = "A preparação não foi concluída; consulte a orientação deste diagnóstico."
    hint = ISSUES[code][1] if code in ISSUES else REMEDIATIONS[code]
    return {
        "code": code,
        "message": message,
        "hint": hint,
        **diagnostic_location(item, files, secrets, byte_column=byte_column),
    }


def _read_output(value):
    raw = value if isinstance(value, bytes) else str(value or "").encode("utf-8", errors="replace")
    return raw[:CAPTURE_BYTES].decode("utf-8", errors="replace"), len(raw), len(raw) > CAPTURE_BYTES


def compiler_diagnostic(
    stdout,
    stderr,
    returncode,
    files,
    *,
    duration_ms=0,
    secrets=(),
    environment=None,
    resources=None,
    fallback_code=None,
):
    streams = [_read_output(stderr), _read_output(stdout)]
    report = {
        "format_version": 1,
        "category": "unknown",
        "tool": "executor",
        "exit_code": returncode if type(returncode) is int else None,
        "duration_ms": max(0, int(duration_ms)),
        "stdout_bytes": streams[1][1],
        "stderr_bytes": streams[0][1],
        "output_truncated": any(stream[2] for stream in streams),
        "issues": [],
    }
    issues = []
    for output, _, _ in streams:
        for line in output.splitlines():
            try:
                payload = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if (
                not isinstance(payload, dict)
                or type(payload.get("viracase_build_diagnostic")) is not int
                or payload["viracase_build_diagnostic"] != 1
            ):
                continue
            if isinstance(payload.get("tool"), str) and payload["tool"] in {
                "esbuild",
                "python",
                "sandbox",
                "executor",
            }:
                report["tool"] = payload["tool"]
            items = payload.get("issues")
            if not isinstance(items, list):
                continue
            report["output_truncated"] |= payload.get("output_truncated") is True or len(items) > MAX_ISSUES
            for item in items[:MAX_ISSUES]:
                if isinstance(item, dict):
                    report["output_truncated"] |= (
                        isinstance(item.get("message"), str) and len(item["message"]) > MESSAGE_CHARS
                    )
                    issues.append(normalized_issue(item, files, secrets))
    if not issues:
        for output, _, _ in streams:
            for line in output.splitlines():
                if len(line) > MESSAGE_INPUT_CHARS:
                    report["output_truncated"] = True
                    continue
                line = safe_text(line, secrets, limit=CAPTURE_BYTES).strip()
                found = re.match(r"^(.+?):(\d+):(\d+):\s*(?:ERROR|error):\s*(.+)$", line)
                if found:
                    report["tool"] = "esbuild"
                    name, number, column, message = found.groups()
                    issues.append(
                        normalized_issue(
                            {"message": message, "path": name, "line": int(number), "column": int(column)},
                            files,
                            secrets,
                            byte_column=True,
                        )
                    )
                elif line.startswith(("bwrap:", "prlimit:")):
                    report["tool"] = "sandbox"
                    code = classify_failure(line, returncode, environment, resources)
                    issues.append(normalized_issue({"code": code, "message": line}, files, secrets))
                elif re.match(
                    r"^(?:Error|SyntaxError|TypeError|FATAL ERROR|error)(?:\s+\[[A-Z_]+\])?:\s", line
                ):
                    code = issue_code(line)
                    if code != "BUILD_PROCESS_FAILED":
                        issues.append(normalized_issue({"code": code, "message": line}, files, secrets))
                    elif "out of memory" in line.lower() or "heap limit" in line.lower():
                        issues.append(
                            normalized_issue(
                                {
                                    "code": "SANDBOX_RESOURCE_EXHAUSTED",
                                    "message": "O processo informou esgotamento de memória durante a preparação.",
                                },
                                files,
                            )
                        )
                    elif line.startswith("TypeError:"):
                        issues.append(
                            normalized_issue(
                                {
                                    "code": "BUILD_RUNTIME_FAILED",
                                    "message": "O processo de preparação terminou com TypeError.",
                                },
                                files,
                            )
                        )
                if len(issues) > MAX_ISSUES:
                    report["output_truncated"] = True
                    break
    terminal_code = fallback_code or (
        "EXECUTOR_TERMINATED" if type(returncode) is int and returncode < 0 else None
    )
    if terminal_code or not issues:
        code = terminal_code or "BUILD_PROCESS_FAILED"
        messages = {
            "BUILD_TIMEOUT": "O processo de preparação excedeu o prazo do executor.",
            "BUILD_OUTPUT_LIMIT": "O artefato produzido excedeu o limite de saída do executor.",
            "BUILD_OUTPUT_INVALID": "O executor não retornou um artefato válido.",
            "EXECUTOR_UNAVAILABLE": "O processo de preparação não pôde iniciar ou comprovar o isolamento.",
        }
        if type(returncode) is int and returncode < 0:
            messages["EXECUTOR_TERMINATED"] = f"O processo foi encerrado pelo sinal {-returncode}."
        message = messages.get(
            code,
            f"O processo terminou com código {returncode if returncode is not None else 'indisponível'} sem um diagnóstico reconhecido.",
        )
        issues.insert(0, normalized_issue({"code": code, "message": message}, files, secrets))
    unique = {}
    for issue in issues:
        unique.setdefault(json.dumps(issue, sort_keys=True), issue)
    report["output_truncated"] |= len(unique) > MAX_ISSUES
    report["issues"] = list(unique.values())[:MAX_ISSUES]
    categories = {
        ISSUES[item["code"]][0] if item["code"] in ISSUES else "executor" for item in report["issues"]
    }
    report["category"] = next(
        category
        for category in ("executor", "dependency", "source", "output", "unknown")
        if category in categories
    )
    return report


class BuildFailure(RuleError):
    def __init__(self, code, message, diagnostic, status=409):
        super().__init__(code, message, status=status, details={"build": diagnostic})
        self.diagnostic = diagnostic
