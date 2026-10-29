import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from publication_diagnostic_fixture import seed_publication_build_failure
from sqlalchemy import select
from test_publication_pipeline import Remote, start
from test_publication_pipeline import publication as publication_fixture

from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure import publication_build
from viracase.infrastructure.models import Job
from viracase.infrastructure.publication_build import IsolatedBuild
from viracase.infrastructure.publication_build_diagnostics import (
    CAPTURE_BYTES,
    MAX_ISSUES,
    BuildFailure,
    compiler_diagnostic,
)
from viracase.infrastructure.publication_logging import publication_context
from viracase.infrastructure.publication_worker import execute_publication
from viracase.infrastructure.worker import Worker
from viracase.interfaces.api import create_app

publication = publication_fixture
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def files():
    return {
        "README.md": "Projeto de teste do compilador",
        "migrations/0001.sql": "CREATE TABLE tasks(id INTEGER PRIMARY KEY);",
        "public/index.html": "<!doctype html><html><body>Projeto</body></html>",
        "wrangler.jsonc": "{}",
        "package.json": '{"dependencies":{"hono":"4.12.9"}}',
        "src/index.ts": 'import { Hono } from "hono";\nconst app = new Hono();\napp.get("/api/health", (c) => c.json({ ok: true }));\nexport default app;\n',
    }


def structured(issues, **values):
    return json.dumps(
        {"viracase_build_diagnostic": 1, "tool": "esbuild", "issues": issues, **values}
    ).encode()


def run_toolchain(tmp_path, files):
    node = shutil.which("node")
    if not node or not (ROOT / "build-toolchain/node_modules/esbuild").is_dir():
        pytest.skip("Instale a toolchain fixada com npm ci para executar o compilador real local.")
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return subprocess.run(
        [node, str(ROOT / "build-toolchain/build.mjs"), "hono-typescript-d1", "synthetic-revision"],
        cwd=tmp_path,
        capture_output=True,
        timeout=15,
        check=False,
    )


@pytest.fixture
def compiler_failure(monkeypatch):
    monkeypatch.setattr(IsolatedBuild, "available", lambda self: True)
    stderr = b'Error: Build failed with 1 error:\n/project/src/index.ts:3:7: ERROR: Expected identifier but found "="\n  const secret = "private-source-line"\n'
    monkeypatch.setattr(
        publication_build.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, b"", stderr),
    )


def test_compiler_failure_preserves_actionable_location_without_source(system, compiler_failure):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(actor, "diagnostic-project", {"name": "Build", "objective": "Diagnóstico"})
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    with pytest.raises(RuleError) as caught:
        IsolatedBuild().build(files, "hono-typescript-d1", project["current_revision_id"])
    diagnostic = caught.value.details.get("build")
    assert diagnostic is not None
    assert diagnostic["category"] == "source"
    assert diagnostic["issues"][0]["path"] == "src/index.ts"
    assert diagnostic["issues"][0]["line"] == 3
    assert diagnostic["issues"][0]["column"] == 8
    assert "Expected identifier" in diagnostic["issues"][0]["message"]
    assert "private-source-line" not in json.dumps(diagnostic)


def test_worker_persists_build_diagnostic_in_owned_operation(system, publication, compiler_failure):
    op = start(system, publication)
    repo = system["repo"]
    with repo.sessions.begin() as db:
        job = db.scalar(select(Job).where(Job.operation_id == op["id"]))
        job.state, job.lease_token = "running", "diagnostic-lease"
        job.lease_until = utcnow() + timedelta(minutes=5)
    builder = IsolatedBuild()
    builder.fingerprint = lambda: "f" * 64
    remote = Remote()
    execute_publication(
        Worker(repo, None, system["catalog"]), op["id"], "diagnostic-lease", lambda *_: remote, builder
    )
    result = repo.operation(publication[2], op["id"])
    assert result["state"] == "failed"
    assert result["error_code"] == "BUILD_FAILED"
    diagnostic = result["result_ref"]["publication_failure"].get("build")
    assert diagnostic is not None
    assert diagnostic["operation_id"] == diagnostic["correlation_id"] == op["id"]
    assert diagnostic["revision_id"] == publication[3]["current_revision_id"]
    assert diagnostic["issues"][0]["path"] == "src/index.ts"
    assert not remote.calls
    with pytest.raises(RuleError) as denied:
        repo.operation(system["users"][1], op["id"])
    assert denied.value.code == "NOT_FOUND"


def test_real_compiler_failure_is_persisted_and_available_only_to_owner_over_http(system, monkeypatch):
    monkeypatch.setenv("VIRA_E2E_ISOLATED", "true")
    monkeypatch.setenv("VIRA_E2E_BUILD_DIAGNOSTIC", "true")
    metadata = seed_publication_build_failure(system["repo"], system["catalog"], system["users"][0])
    dependencies = (system["config"], system["engine"], system["repo"], system["catalog"], None)
    with TestClient(create_app(dependencies=dependencies)) as client:
        denied = client.get("/api/v1/operations/" + metadata["operation_id"])
        assert denied.status_code == 401
        response = client.post(
            "/api/v1/auth/login",
            json={"login": "student0", "password": "test-student-password"},
            headers={"Origin": system["config"].public_origin},
        )
        assert response.status_code == 200
        response = client.get("/api/v1/operations/" + metadata["operation_id"])
        assert response.status_code == 200
        operation = response.json()
        assert operation["state"] == "failed"
        diagnostic = operation["result_ref"]["publication_failure"]["build"]
        assert diagnostic == metadata["diagnostic"]
        assert diagnostic["issues"][0]["path"] == "src/index.ts"
        assert diagnostic["issues"][0]["line"] == 3
        assert diagnostic["issues"][0]["column"] == 7
        assert metadata["external_requests"] == 0
        assert "synthetic-private-source" not in json.dumps(operation)
    with TestClient(create_app(dependencies=dependencies)) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"login": "student1", "password": "test-student-password"},
            headers={"Origin": system["config"].public_origin},
        )
        assert response.status_code == 200
        denied = client.get("/api/v1/operations/" + metadata["operation_id"])
        assert denied.status_code == 404
        assert "Expected identifier" not in denied.text


def test_real_compiler_builds_valid_application_and_preserves_runtime_wrapper(tmp_path, files):
    result = run_toolchain(tmp_path, files)
    assert result.returncode == 0, result.stderr
    artifact = json.loads(result.stdout)
    assert artifact["main_module"] == "worker.js"
    assert artifact["compatibility_flags"] == []
    source = artifact["modules"]["worker.js"]["content"]
    assert "/api/__viroucaseai_revision" in source
    assert "SELECT 1" in source and "ASSETS.fetch" in source
    assert "synthetic-revision" in source and "API_ROUTE_NOT_FOUND" in source
    assert result.stderr == b""


@pytest.mark.parametrize(
    "mode,code,category",
    [
        ("syntax", "SYNTAX_ERROR", "source"),
        ("missing_import", "IMPORT_NOT_FOUND", "source"),
        ("missing_export", "EXPORT_NOT_FOUND", "source"),
        ("forbidden_import", "IMPORT_OUTSIDE_CATALOG", "dependency"),
        ("dependency", "DEPENDENCY_OUTSIDE_CATALOG", "dependency"),
        ("package_json", "INVALID_PACKAGE_JSON", "source"),
    ],
)
def test_real_compiler_returns_useful_diagnostics_without_source_or_stack(
    tmp_path, files, mode, code, category
):
    if mode == "syntax":
        files["src/index.ts"] = (
            'import { Hono } from "hono";\nconst app = new Hono();\nconst = "private-source-marker";\nexport default app;'
        )
    elif mode == "missing_import":
        files["src/index.ts"] = 'import "./missing-module";\n' + files["src/index.ts"]
    elif mode == "missing_export":
        files["src/index.ts"] = files["src/index.ts"].replace("export default app;", "")
    elif mode == "forbidden_import":
        files["src/index.ts"] = 'import "node:fs";\n' + files["src/index.ts"]
    elif mode == "dependency":
        files["package.json"] = '{"dependencies":{"hono":"4.12.9","not-approved":"1.0.0"}}'
    elif mode == "package_json":
        files["package.json"] = '{"dependencies": '
    result = run_toolchain(tmp_path, files)
    assert result.returncode == 1 and result.stdout == b""
    payload = json.loads(result.stderr)
    assert payload["viracase_build_diagnostic"] == 1
    assert "lineText" not in result.stderr.decode() and "at failureError" not in result.stderr.decode()
    assert "private-source-marker" not in result.stderr.decode()
    report = compiler_diagnostic(result.stdout, result.stderr, result.returncode, files)
    assert report["category"] == category
    assert report["issues"][0]["code"] == code
    if mode in {"syntax", "missing_import", "forbidden_import"}:
        assert report["issues"][0]["path"] == "src/index.ts"
        assert report["issues"][0]["line"] >= 1
        assert report["issues"][0]["column"] >= 1
    if mode in {"dependency", "package_json"}:
        assert report["issues"][0]["path"] == "package.json"
    assert str(tmp_path) not in json.dumps(report)


def test_real_compiler_reports_character_column_for_utf8_source(tmp_path, files):
    line = 'const café = "ação"; const = 2;'
    files["src/index.ts"] = 'import { Hono } from "hono";\n' + line + "\nexport default new Hono();"
    result = run_toolchain(tmp_path, files)
    assert result.returncode == 1
    report = compiler_diagnostic(result.stdout, result.stderr, result.returncode, files)
    issue = report["issues"][0]
    assert issue["line"] == 2
    assert issue["column"] == line.rfind("=") + 1


def test_real_compiler_truncates_many_unicode_errors_inside_json_byte_budget(tmp_path, files):
    imports = [
        "import " + json.dumps("./" + ("欠🚧" * 20 + "/") * 9 + str(index), ensure_ascii=False) + ";"
        for index in range(40)
    ]
    files["src/index.ts"] = "\n".join(imports) + "\n" + files["src/index.ts"]
    result = run_toolchain(tmp_path, files)
    assert result.returncode == 1 and result.stdout == b""
    assert len(result.stderr) <= 8192
    payload = json.loads(result.stderr.decode("utf-8", errors="strict"))
    assert 1 <= len(payload["issues"]) < MAX_ISSUES
    assert payload["output_truncated"] is True
    report = compiler_diagnostic(result.stdout, result.stderr, result.returncode, files)
    assert report["output_truncated"] is True
    assert report["issues"][0]["code"] == "IMPORT_NOT_FOUND"
    assert report["stderr_bytes"] == len(result.stderr)


@pytest.mark.parametrize(
    "module", ["python_vendor_not_qualified", "dependency_outside_catalog", "import outside catalog"]
)
def test_missing_import_names_cannot_impersonate_executor_or_catalog_errors(tmp_path, files, module):
    files["src/index.ts"] = f'import "./{module}";\n' + files["src/index.ts"]
    result = run_toolchain(tmp_path, files)
    assert result.returncode == 1
    report = compiler_diagnostic(result.stdout, result.stderr, result.returncode, files)
    assert report["category"] == "source"
    assert report["issues"][0]["code"] == "IMPORT_NOT_FOUND"
    assert report["issues"][0]["path"] == "src/index.ts"


def test_export_error_is_not_classified_from_the_module_name(files):
    report = compiler_diagnostic(
        b"",
        structured([{"message": 'No matching export in "could not resolve" for import "default"'}]),
        1,
        files,
    )
    assert report["category"] == "source"
    assert report["issues"][0]["code"] == "EXPORT_NOT_FOUND"


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_real_compiler_preserves_error_position_at_empty_final_line(tmp_path, files, ending):
    files["src/index.ts"] = "export default function(" + ending
    result = run_toolchain(tmp_path, files)
    assert result.returncode == 1
    report = compiler_diagnostic(result.stdout, result.stderr, result.returncode, files)
    issue = report["issues"][0]
    assert issue["code"] == "SYNTAX_ERROR"
    assert {key: issue[key] for key in ("path", "line", "column")} == {
        "path": "src/index.ts", "line": 2, "column": 1,
    }


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
def test_diagnostic_accepts_both_output_streams_without_copying_extra_fields(files, channel):
    payload = structured(
        [
            {
                "code": "SYNTAX_ERROR",
                "message": "Expected an expression",
                "path": "src/index.ts",
                "line": 2,
                "column": 5,
                "lineText": "private-source-line",
                "stack": "private-stack",
            }
        ]
    )
    report = compiler_diagnostic(
        payload if channel == "stdout" else b"", payload if channel == "stderr" else b"", 1, files
    )
    assert report["issues"][0]["path"] == "src/index.ts"
    assert report[channel + "_bytes"] == len(payload)
    assert "private-" not in json.dumps(report)


@pytest.mark.parametrize(
    "name",
    [
        "/etc/passwd",
        "../src/index.ts",
        "/project/../src/index.ts",
        "src/../src/index.ts",
        "src\\index.ts",
        "src/unknown.ts",
        "src/\u202eindex.ts",
    ],
)
def test_paths_outside_the_revision_are_never_exposed_as_file_locations(files, name):
    report = compiler_diagnostic(
        b"",
        structured([{"message": "Expected an expression", "path": name, "line": 2, "column": 1}]),
        1,
        files,
    )
    assert "path" not in report["issues"][0]
    assert "line" not in report["issues"][0] and "column" not in report["issues"][0]


@pytest.mark.parametrize(
    "line,column,expected",
    [
        (999, 1, {"path"}),
        (True, 1, {"path"}),
        (2, 9999, {"path", "line"}),
        (2, True, {"path", "line"}),
        (2, 1, {"path", "line", "column"}),
    ],
)
def test_positions_are_checked_against_the_saved_file(files, line, column, expected):
    report = compiler_diagnostic(
        b"",
        structured(
            [{"message": "Expected an expression", "path": "src/index.ts", "line": line, "column": column}]
        ),
        1,
        files,
    )
    assert set(report["issues"][0]) & {"path", "line", "column"} == expected


def test_redaction_removes_credentials_osc_controls_host_paths_and_source_excerpt(files):
    secret = "short-provider-credential"
    message = (
        'Expected expression; Authorization: Bearer hidden-token password="hidden value" https://host.test/?token=url-secret /tmp/private-user/cache '
        + secret
        + '\x1b]8;;https://hidden-link.test\x07link\x1b]8;;\x07\u202e\nconst private_source = "never-expose";'
    )
    files["src/" + secret + ".ts"] = "const app = 1;"
    report = compiler_diagnostic(
        b"",
        structured([{"message": message, "path": "src/" + secret + ".ts", "line": 1, "column": 1}]),
        1,
        files,
        secrets=(secret,),
    )
    encoded = json.dumps(report, ensure_ascii=False)
    for value in [
        secret,
        "hidden-token",
        "hidden value",
        "url-secret",
        "/tmp/private-user",
        "hidden-link",
        "never-expose",
        "private_source",
        "\u202e",
        "\x1b",
    ]:
        assert value not in encoded
    assert "Expected expression" in report["issues"][0]["message"]
    assert "path" not in report["issues"][0]


@pytest.mark.parametrize(
    "message",
    [
        'Unexpected token to\u202eken="hidden-value"',
        'Unexpected token to\x00ken="hidden-value"',
        'Unexpected token \x9b31mtoken="hidden-value"\x9b0m',
        "Unexpected token \x9d8;;https://hidden-value.test\x9cvisible\x9d8;;\x9c",
        "Unexpected token C:\\private-user\\hidden-value\\file.ts",
        "Unexpected token /hidden-value",
    ],
)
def test_terminal_controls_cannot_hide_sensitive_assignments_or_paths(files, message):
    report = compiler_diagnostic(b"", structured([{"message": message}]), 1, files)
    assert "hidden-value" not in json.dumps(report)
    assert "Unexpected token" in report["issues"][0]["message"]


@pytest.mark.parametrize(
    "payload",
    [
        b'{"viracase_build_diagnostic":1,"issues":"private-data"}',
        b'{"viracase_build_diagnostic":true,"issues":[]}',
        b'{"viracase_build_diagnostic":1,"tool":[],"issues":[{"code":{},"message":"Unexpected token"}]}',
        b'{"viracase_build_diagnostic":1,"issues":[null,[],42]}',
        b"not-json private-data",
    ],
)
def test_malformed_diagnostic_output_is_bounded_and_never_crashes(files, payload):
    report = compiler_diagnostic(b"", payload, 1, files)
    assert report["issues"] and report["exit_code"] == 1
    assert "private-data" not in json.dumps(report)


def test_large_output_and_error_lists_are_explicitly_marked_truncated(files):
    issues = [
        {"message": "Unexpected token " + str(index), "path": "src/index.ts", "line": 2}
        for index in range(MAX_ISSUES + 10)
    ]
    report = compiler_diagnostic(b"", structured(issues), 1, files)
    assert len(report["issues"]) == MAX_ISSUES and report["output_truncated"]
    report = compiler_diagnostic(
        b"private-artifact" * CAPTURE_BYTES, b"private-source" * CAPTURE_BYTES, 1, files
    )
    assert report["output_truncated"]
    assert len(json.dumps(report)) < 3000
    assert "private-" not in json.dumps(report)


@pytest.mark.parametrize(
    "stderr,code,tool",
    [
        (b"bwrap: Creating new namespace failed: Operation not permitted", "NAMESPACE_DENIED", "sandbox"),
        (b"prlimit: failed to set NPROC limit: Operation not permitted", "WORKLOAD_LIMIT_FAILED", "sandbox"),
        (
            b"Error [ERR_MODULE_NOT_FOUND]: Cannot find module 'esbuild' imported from /tmp/private-user/build.mjs",
            "TOOLCHAIN_DEPENDENCY_MISSING",
            "executor",
        ),
        (
            b"FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory",
            "SANDBOX_RESOURCE_EXHAUSTED",
            "executor",
        ),
        (
            b"TypeError: Cannot read properties of undefined ('private-name')",
            "BUILD_RUNTIME_FAILED",
            "executor",
        ),
    ],
)
def test_runtime_and_isolation_failures_are_not_reported_as_generated_code_errors(files, stderr, code, tool):
    report = compiler_diagnostic(b"", stderr, 1, files)
    assert report["category"] == "executor" and report["tool"] == tool
    assert report["issues"][0]["code"] == code
    assert "path" not in report["issues"][0]
    assert "private-" not in json.dumps(report)


@pytest.mark.parametrize(
    "returncode,fallback,expected",
    [
        (-9, None, "EXECUTOR_TERMINATED"),
        (None, "BUILD_TIMEOUT", "BUILD_TIMEOUT"),
        (None, "EXECUTOR_UNAVAILABLE", "EXECUTOR_UNAVAILABLE"),
    ],
)
def test_process_termination_takes_precedence_over_partial_compiler_errors(
    files, returncode, fallback, expected
):
    report = compiler_diagnostic(
        b"", structured([{"message": "Unexpected token"}]), returncode, files, fallback_code=fallback
    )
    assert report["category"] == "executor"
    assert report["issues"][0]["code"] == expected


@pytest.mark.parametrize(
    "payload",
    [
        b"invalid-artifact-private",
        b"[]",
        b'{"modules":{},"main_module":[]}',
        b'{"modules":{},"main_module":"worker.js"}',
    ],
)
def test_invalid_success_output_becomes_actionable_build_failure_without_artifact_leak(
    files, monkeypatch, payload
):
    monkeypatch.setattr(IsolatedBuild, "available", lambda self: True)
    monkeypatch.setattr(
        publication_build.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, payload, b""),
    )
    with pytest.raises(BuildFailure) as caught:
        IsolatedBuild().build(files, "hono-typescript-d1", "revision")
    assert caught.value.code == "BUILD_FAILED"
    assert caught.value.diagnostic["category"] == "output"
    assert caught.value.diagnostic["issues"][0]["code"] == "BUILD_OUTPUT_INVALID"
    assert "invalid-artifact-private" not in json.dumps(caught.value.diagnostic)


@pytest.mark.parametrize(
    "error,expected",
    [
        (subprocess.TimeoutExpired(["build"], 45, stderr=b"private-timeout-output"), "BUILD_TIMEOUT"),
        (FileNotFoundError(2, "private-host-path"), "EXECUTOR_UNAVAILABLE"),
    ],
)
def test_build_start_and_timeout_errors_retain_diagnostic_contract(files, monkeypatch, error, expected):
    monkeypatch.setattr(IsolatedBuild, "available", lambda self: True)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(publication_build.subprocess, "run", fail)
    with pytest.raises(BuildFailure) as caught:
        IsolatedBuild().build(files, "hono-typescript-d1", "revision")
    assert caught.value.code == expected
    assert caught.value.diagnostic["category"] == "executor"
    assert "private-" not in json.dumps(caught.value.diagnostic)


def test_sanitized_diagnostic_reaches_correlated_logs(files, monkeypatch, caplog):
    monkeypatch.setattr(IsolatedBuild, "available", lambda self: True)
    payload = structured(
        [
            {
                "message": 'Expected identifier; token="private-value"',
                "path": "src/index.ts",
                "line": 2,
                "column": 2,
                "lineText": "private-source",
            }
        ]
    )
    monkeypatch.setattr(
        publication_build.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, b"", payload),
    )
    from viracase.infrastructure import publication_logging

    monkeypatch.setattr(publication_logging._log, "handlers", [caplog.handler])
    with publication_context(operation_id="diagnostic-operation"), pytest.raises(BuildFailure):
        IsolatedBuild().build(files, "hono-typescript-d1", "revision")
    messages = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "viracase.publication" and record.message.startswith("{")
    ]
    event = next(record for record in messages if record["event"] == "build_failed")
    assert event["correlation_id"] == "diagnostic-operation"
    assert event["build"]["issues"][0]["path"] == "src/index.ts"
    assert "private-" not in json.dumps(event)
