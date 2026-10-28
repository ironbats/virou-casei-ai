import io
import json
import logging
import os
import subprocess
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_publication_pipeline import POLICY, publication
from viracase.domain.rules import RuleError
from viracase.infrastructure import build_limits, publication_build, publication_logging, runtime_monitor
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway, PublicationUncertain
from viracase.infrastructure.models import IntegrationConfig, RuntimeEvent
from viracase.infrastructure.observability import heartbeat, runtime_snapshot
from viracase.infrastructure.publication_build import IsolatedBuild
from viracase.infrastructure.publication_diagnostics import classify_failure
from viracase.infrastructure.publication_logging import emit, exception_fields, publication_context, safe_text
from viracase.infrastructure.publication_sandbox import sandbox_command
from viracase.interfaces.integration_routes import mount_integration_routes

CHECKS = {
    "no_network": True,
    "no_host_secrets": True,
    "no_cross_project_files": True,
    "read_only_sources": True,
    "resource_limits": True,
    "no_nested_userns": True,
}


@pytest.fixture
def log_records(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(publication_logging._log, "handlers", [logging.StreamHandler(stream)])
    return lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line]


@pytest.fixture
def toolchain(tmp_path, monkeypatch):
    root = tmp_path / "toolchain"
    (root / "node_modules/esbuild").mkdir(parents=True)
    for name in ("build.mjs", "probe.mjs"):
        (root / name).write_text("")
    monkeypatch.setattr(publication_build, "TOOLCHAIN", root)
    monkeypatch.setattr(publication_build.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    original = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda path: True if str(path) in {"/usr/bin/node", "/usr/bin/unshare"} else original(path))
    monkeypatch.setattr(IsolatedBuild, "fingerprint", lambda self: "f" * 64)
    return root


def diagnostic(available=False, code="SANDBOX_ARGUMENT_INVALID"):
    return {
        "available": available,
        "fingerprint": "f" * 64,
        "stacks": ["hono-typescript-d1"],
        "code": "READY" if available else code,
        "diagnostic_id": str(uuid4()),
        "duration_ms": 2,
        "failed_checks": [],
        "details": {"stage": "sandbox_preflight", "exit_code": 0 if available else 1, "stderr": "" if available else "bwrap: --disable-userns requires --unshare-user"},
    }


def test_preflight_and_build_use_explicit_mandatory_user_namespace(monkeypatch):
    command = sandbox_command()
    assert "--unshare-all" in command and "--unshare-user" in command
    assert command.index("--unshare-user") < command.index("--disable-userns")
    assert command[command.index("--cap-drop") + 1] == "ALL"
    for flag in ("--clearenv", "--new-session", "--die-with-parent"):
        assert flag in command
    assert IsolatedBuild()._sandbox_command()[:len(command)] == command
    calls, limits = [], {}
    monkeypatch.setattr(build_limits.sys, "argv", ["limits", "/tmp/source", "hono-typescript-d1", "revision"])
    monkeypatch.setattr(build_limits.resource, "setrlimit", lambda key, value: limits.update({key: value}))
    monkeypatch.setattr(build_limits.os, "execvpe", lambda name, argv, env: calls.append((name, argv, env)))
    build_limits.main()
    assert calls[0][1][:len(command)] == command
    assert calls[0][2] == {"PATH": "/usr/bin:/bin"}
    assert "--ro-bind" in calls[0][1] and "/project" in calls[0][1]
    assert limits[build_limits.resource.RLIMIT_CORE] == (0, 0)
    assert limits[build_limits.resource.RLIMIT_AS] == (3_000_000_000, 3_000_000_000)
    assert build_limits.resource.RLIMIT_NPROC not in limits
    inner = calls[0][1].index("/usr/bin/prlimit")
    assert calls[0][1][inner + 1] == "--nproc=64:64"
    assert inner > calls[0][1].index("--unshare-user")
    assert calls[0][1][inner + 2] == "--"


@pytest.mark.parametrize("stderr,code", [
    ("bwrap: --disable-userns requires --unshare-user", "SANDBOX_ARGUMENT_INVALID"),
    ("bwrap: Unknown option --disable-userns", "TOOLCHAIN_INCOMPATIBLE"),
    ("bwrap: setting /proc/sys/user/max_user_namespaces: Read-only file system", "USERNS_SYSCTL_READONLY"),
    ("bwrap: Can't mount proc on /newroot/proc: Operation not permitted", "PROC_MOUNT_DENIED"),
    ("bwrap: Creating new namespace failed: Operation not permitted", "NAMESPACE_DENIED"),
    ("bwrap: execvp /usr/bin/node: No such file or directory", "SANDBOX_PATH_MISSING"),
    ("bwrap: Cannot allocate memory", "SANDBOX_RESOURCE_EXHAUSTED"),
    ("bwrap: unexpected failure", "SANDBOX_START_FAILED"),
])
def test_failures_have_distinct_actionable_codes(stderr, code):
    assert classify_failure(stderr, 1) == code


def test_preflight_keeps_real_exit_and_redacted_stderr(toolchain, monkeypatch):
    captured = []
    def run(argv, **kwargs):
        captured.append(kwargs)
        return subprocess.CompletedProcess(argv, 1, b"", b"bwrap: --disable-userns requires --unshare-user\nAuthorization: Bearer test-secret")
    monkeypatch.setattr(publication_build.subprocess, "run", run)
    result = IsolatedBuild().diagnostics()
    assert result["available"] is False
    assert result["code"] == "SANDBOX_ARGUMENT_INVALID"
    assert result["details"]["stage"] == "sandbox_preflight"
    assert result["details"]["exit_code"] == 1
    assert "--unshare-user" in result["details"]["stderr"]
    assert "test-secret" not in json.dumps(result)
    assert result["remediation"] and len(result["diagnostic_id"]) == 36
    assert captured[0]["env"] == {"PATH": "/usr/bin:/bin"}


@pytest.mark.parametrize("error,code", [
    (subprocess.TimeoutExpired(["bwrap"], 5, stderr=b"token=private-value"), "SANDBOX_TIMEOUT"),
    (FileNotFoundError(2, "sensitive path not logged"), "SANDBOX_START_FAILED"),
])
def test_preflight_start_exceptions_are_not_success(toolchain, monkeypatch, error, code):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(publication_build.subprocess, "run", fail)
    result = IsolatedBuild().diagnostics()
    assert result["available"] is False and result["code"] == code
    assert result["details"]["exception_type"] == type(error).__name__
    assert "private-value" not in json.dumps(result)
    assert "sensitive path" not in json.dumps(result)


@pytest.mark.parametrize("checks,available", [(CHECKS, True), ({**CHECKS, "no_network": False}, False), ({}, False), ({**CHECKS, "unexpected": True}, False)])
def test_readiness_requires_all_real_isolation_results(toolchain, monkeypatch, checks, available):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"" if len(calls) == 1 else json.dumps(checks).encode(), b"")
    monkeypatch.setattr(publication_build.subprocess, "run", run)
    result = IsolatedBuild().diagnostics()
    assert result["available"] is available
    assert result["details"]["stage"] == "isolation_probe"
    assert result["code"] == ("READY" if available else "ISOLATION_PROBE_FAILED")
    assert len(calls) == 2


def test_invalid_probe_output_does_not_release_publication(toolchain, monkeypatch):
    responses = iter([subprocess.CompletedProcess([], 0, b"", b""), subprocess.CompletedProcess([], 0, b"not-json", b"")])
    monkeypatch.setattr(publication_build.subprocess, "run", lambda *args, **kwargs: next(responses))
    result = IsolatedBuild().diagnostics()
    assert result["code"] == "PROBE_OUTPUT_INVALID" and not result["available"]


def test_probe_keeps_sysctl_and_namespace_errors_distinct(toolchain, monkeypatch):
    responses = iter([subprocess.CompletedProcess([], 0, b"", b""), subprocess.CompletedProcess([], 1, b"", b"bwrap: setting max_user_namespaces: Read-only file system")])
    monkeypatch.setattr(publication_build.subprocess, "run", lambda *args, **kwargs: next(responses))
    result = IsolatedBuild().diagnostics()
    assert result["code"] == "USERNS_SYSCTL_READONLY" and not result["available"]
    assert result["details"]["exit_code"] == 1


def test_secret_redaction_preserves_diagnostic_information():
    secret = "known-short-secret"
    source = "\x1b[31mbwrap: --disable-userns requires --unshare-user\x1b[0m\n" + "\n".join([
        "Authorization: Bearer short-bearer",
        '"api_token": "test-token-secret"',
        "password='hidden password'",
        "postgresql://admin:db-password@db/private",
        "cookie: session=hidden-cookie",
        "-----BEGIN PRIVATE KEY-----\nprivate-key-content\n-----END PRIVATE KEY-----",
        "A" * 48,
        secret,
    ])
    result = safe_text(source, (secret,))
    for value in ("short-bearer", "test-token-secret", "hidden password", "db-password", "hidden-cookie", "private-key-content", "A" * 48, secret, "\x1b"):
        assert value not in result
    assert "--disable-userns requires --unshare-user" in result
    assert len(safe_text("x " * 5000)) <= 2048


def test_json_log_correlates_operations_and_does_not_leak_exception_text(log_records):
    with publication_context(request_id="request-one"):
        with publication_context(operation_id="operation-two"):
            try:
                raise ValueError("sensitive-token-in-exception")
            except ValueError as exc:
                emit("failed", "error", **exception_fields(exc))
        emit("request_completed")
    emit("outside_request")
    records = log_records()
    assert records[0]["correlation_id"] == "operation-two"
    assert records[0]["request_id"] == "request-one"
    assert records[0]["frames"] and records[0]["exception_type"] == "ValueError"
    assert "sensitive-token" not in json.dumps(records)
    assert records[1]["correlation_id"] == "request-one"
    assert "correlation_id" not in records[2]


def test_cloudflare_failure_has_ray_status_and_numeric_code_without_secrets(log_records):
    secret = "short-token-never-print"
    def handler(request):
        assert request.headers["authorization"] == "Bearer " + secret
        return httpx.Response(403, headers={"cf-ray": "0123456789abcdef-GRU"}, json={"success": False, "errors": [{"code": 10000, "message": secret}]})
    gateway = CloudflareGateway(POLICY, secret, httpx.MockTransport(handler))
    with publication_context(operation_id="my-operation"):
        with pytest.raises(RuleError) as error:
            gateway.request("GET", gateway.account_path + "/d1/database?token=" + secret)
    assert error.value.code == "CONFIGURATION_INVALID"
    records = log_records()
    assert records[-1]["status"] == 403
    assert records[-1]["provider_error_codes"] == [10000]
    assert records[-1]["cf_ray"] == "0123456789abcdef-GRU"
    assert records[-1]["correlation_id"] == "my-operation"
    assert records[-1]["route"] == "/accounts/{account}/d1/database"
    assert secret not in json.dumps(records) and POLICY["account_id"] not in json.dumps(records)
    assert all(r["event"] != "cloudflare_request_completed" for r in records)


def test_cloudflare_application_error_on_http_200_is_not_logged_as_success(log_records):
    gateway = CloudflareGateway(POLICY, "fake", httpx.MockTransport(lambda r: httpx.Response(200, json={"success": False, "errors": [{"code": 1001}]})))
    with pytest.raises(RuleError):
        gateway.request("GET", gateway.account_path)
    assert [r["event"] for r in log_records()] == ["cloudflare_request_failed"]


@pytest.mark.parametrize("method,error_code", [("GET", "DEPENDENCY_UNAVAILABLE"), ("POST", "PUBLICATION_RESULT_UNKNOWN")])
def test_network_failures_keep_read_vs_write_reconciliation_contract(method, error_code, log_records):
    def timeout(request):
        raise httpx.ReadTimeout("do-not-log-provider-token", request=request)
    gateway = CloudflareGateway(POLICY, "fake", httpx.MockTransport(timeout))
    with pytest.raises(RuleError) as error:
        gateway.request(method, gateway.account_path + "/d1/database")
    assert error.value.code == error_code
    assert "do-not-log-provider-token" not in json.dumps(log_records())
    assert log_records()[0]["event"] == "cloudflare_transport_failed"


def test_monitor_logs_transitions_and_keeps_id_linked_to_visible_heartbeat(monkeypatch):
    first, repeated, ready = diagnostic(), diagnostic(), diagnostic(True)
    records, beats = [], []
    items = iter([first, repeated, ready])
    class Stop:
        count = 0
        def is_set(self):
            return self.count == 3
        def wait(self, seconds):
            self.count += 1
    class Build:
        def diagnostics(self):
            return deepcopy(next(items))
    monkeypatch.setattr(runtime_monitor, "IsolatedBuild", Build)
    monkeypatch.setattr(runtime_monitor, "heartbeat", lambda repo, identity, value: beats.append(deepcopy(value)))
    monkeypatch.setattr(runtime_monitor, "prune", lambda repo: None)
    monkeypatch.setattr(runtime_monitor, "emit", lambda event, level="info", **fields: records.append({"event": event, **fields}))
    thread = runtime_monitor.start_monitor(None, Stop())
    thread.join(2)
    assert not thread.is_alive()
    assert len(beats) == 3 and len(records) == 2
    assert beats[0]["diagnostic_id"] == beats[1]["diagnostic_id"] == records[0]["diagnostic_id"]
    assert beats[2]["diagnostic_id"] == records[1]["diagnostic_id"]
    assert [r["event"] for r in records] == ["executor_unavailable", "executor_ready"]


def test_database_records_state_changes_without_heartbeat_event_flood(system):
    repo = system["repo"]
    failed, ready = diagnostic(), diagnostic(True)
    for value in (failed, failed, ready, ready):
        heartbeat(repo, "worker:diagnostic-test", deepcopy(value))
    with repo.sessions() as db:
        events = db.scalars(select(RuntimeEvent).order_by(RuntimeEvent.id)).all()
    assert len(events) == 2
    assert [event.stage for event in events] == ["executor_unavailable", "executor_ready"]
    assert events[0].correlation_id == failed["diagnostic_id"]
    assert runtime_snapshot(repo)[0]["available"] is True


def diagnostics_client(system, actor):
    app = FastAPI()
    @app.exception_handler(RuleError)
    async def handle(request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status)
    mount_integration_routes(app, "/admin", system["repo"], lambda: {"id": actor}, lambda: "key", lambda: 1)
    return TestClient(app)


@pytest.mark.parametrize("available", [False, True])
def test_admin_diagnostic_separates_local_verification_from_qualification(system, publication, available):
    registry = publication[0]
    config = registry.get("cloudflare")
    heartbeat(system["repo"], "worker:test", diagnostic(available))
    with diagnostics_client(system, system["admin"]) as client:
        response = client.get("/admin/integration-connections/" + config["connection_id"] + "/publication-diagnostics")
    assert response.status_code == 200
    data = response.json()
    assert data["can_verify"] is available
    assert data["can_publish"] is False
    assert "CAPABILITY_UNVALIDATED" in [r["code"] for r in data["reasons"]]
    assert data["workers"][0]["diagnostic_id"]
    assert "synthetic-cloudflare-token" not in response.text


def test_diagnostic_never_exposes_host_details_to_student(system, publication):
    config = publication[0].get("cloudflare")
    heartbeat(system["repo"], "worker:test", diagnostic())
    with diagnostics_client(system, system["users"][0]) as client:
        response = client.get("/admin/integration-connections/" + config["connection_id"] + "/publication-diagnostics")
    assert response.status_code == 403
    assert "workers" not in response.text and "stderr" not in response.text


def test_validation_expiry_blocks_operational_check_even_with_ready_worker(system, publication):
    config = publication[0].get("cloudflare")
    heartbeat(system["repo"], "worker:test", diagnostic(True))
    with system["repo"].sessions.begin() as db:
        row = db.get(IntegrationConfig, config["id"])
        row.validation = {**row.validation, "ready": False}
    with diagnostics_client(system, system["admin"]) as client:
        response = client.get("/admin/integration-connections/" + config["connection_id"] + "/publication-diagnostics")
    assert not response.json()["can_verify"]
    assert "CONFIGURATION_INVALID" in [r["code"] for r in response.json()["verification_reasons"]]


def test_real_sandbox_when_explicitly_enabled():
    if os.environ.get("VIRA_TEST_SANDBOX") != "1":
        pytest.skip("Set VIRA_TEST_SANDBOX=1 in the deployed worker to test its real isolation boundary")
    result = IsolatedBuild().diagnostics()
    assert result["available"], json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("profile,code", [
    ("docker-default (enforce)", "APPARMOR_PROFILE_MISMATCH"),
    ("docker-default", "APPARMOR_PROFILE_MISMATCH"),
    ("viracase-publication (enforce)", "MOUNT_PROPAGATION_DENIED"),
    ("another-profile (enforce)", "MOUNT_PROPAGATION_DENIED"),
    ("not_exposed", "MOUNT_PROPAGATION_DENIED"),
])
def test_actual_mount_failure_uses_runtime_profile_as_evidence(profile, code):
    stderr = "bwrap: Failed to make / slave: Permission denied\n"
    assert classify_failure(stderr, 1, {"apparmor_profile": profile}) == code


def test_reported_production_failure_is_reproduced_with_exact_stderr(toolchain, monkeypatch):
    environment = {
        "uid": 1829257876,
        "gid": 1255800513,
        "apparmor_profile": "docker-default (enforce)",
        "max_user_namespaces": "125869",
        "unprivileged_userns_clone": "1",
        "apparmor_restrict_unprivileged_userns": "0",
        "CapEff": "0000000000000000",
        "CapBnd": "0000000000000000",
        "NoNewPrivs": "1",
        "Seccomp": "2",
    }
    calls = []
    def denied(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, b"", b"bwrap: Failed to make / slave: Permission denied\n")
    monkeypatch.setattr(publication_build, "runtime_environment", lambda: deepcopy(environment))
    monkeypatch.setattr(publication_build.subprocess, "run", denied)
    result = IsolatedBuild().diagnostics()
    assert result["available"] is False and result["code"] == "APPARMOR_PROFILE_MISMATCH"
    assert result["details"]["stage"] == "sandbox_preflight" and result["details"]["exit_code"] == 1
    assert result["details"]["expected_apparmor_profile"] == "viracase-publication (enforce)"
    assert result["details"]["observed_apparmor_profile"] == environment["apparmor_profile"]
    assert "Failed to make / slave" in result["details"]["stderr"]
    assert "restart-publication.sh" in result["remediation"]
    assert len(calls) == 1 and result["failed_checks"] == []


def test_apparmor_complain_does_not_qualify_as_ready(toolchain, monkeypatch):
    monkeypatch.setattr(publication_build, "runtime_environment", lambda: {"apparmor_profile": "viracase-publication (complain)"})
    monkeypatch.setattr(publication_build.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, b"", b""))
    result = IsolatedBuild().diagnostics()
    assert result["available"] is False and result["code"] == "APPARMOR_NOT_ENFORCING"


def test_changed_runtime_profile_changes_log_signature():
    first = diagnostic()
    first["environment"] = {"apparmor_profile": "docker-default (enforce)"}
    second = deepcopy(first)
    second["environment"]["apparmor_profile"] = "viracase-publication (enforce)"
    assert runtime_monitor.diagnostic_signature(first) != runtime_monitor.diagnostic_signature(second)


def test_missing_fingerprint_artifact_is_explained_without_absolute_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(publication_build, "TOOLCHAIN", tmp_path / "private-not-for-logs")
    builder = IsolatedBuild()
    assert builder.fingerprint() is None
    assert builder._fingerprint_error == {"artifact": "build.mjs", "exception_type": "FileNotFoundError", "errno": 2}
    assert "private-not-for-logs" not in json.dumps(builder._fingerprint_error)


def test_missing_fingerprint_prevents_readiness_and_is_visible(monkeypatch):
    builder = IsolatedBuild()
    def missing():
        builder._fingerprint_error = {"artifact": "pylock.toml", "exception_type": "FileNotFoundError", "errno": 2}
        return None
    monkeypatch.setattr(builder, "fingerprint", missing)
    monkeypatch.setattr(builder, "_preflight", lambda: ("READY", "ready"))
    monkeypatch.setattr(builder, "_probe", lambda: CHECKS)
    result = builder.diagnostics()
    assert result["available"] is False and result["code"] == "TOOLCHAIN_MISSING"
    assert result["fingerprint"] is None and result["fingerprint_error"]["artifact"] == "pylock.toml"
