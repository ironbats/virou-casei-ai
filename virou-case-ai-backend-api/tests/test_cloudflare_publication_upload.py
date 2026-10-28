import base64
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from viracase.infrastructure import publication_build
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway, PublicationUncertain


POLICY = {
    "account_id": "a" * 32,
    "timeout_seconds": 10,
    "compatibility_date": "2026-10-27",
    "spa": True,
}
OLD_ID = "00000000-0000-0000-0000-000000000001"
NEW_ID = "00000000-0000-0000-0000-000000000002"


class GatewayStub(CloudflareGateway):
    def __init__(self, previous=(), next_ids=(NEW_ID,), upload_result=None):
        super().__init__(POLICY, "synthetic-token", sleep=lambda _: None)
        self.previous = list(previous)
        self.next_ids = list(next_ids)
        self.upload_result = upload_result if upload_result is not None else {"id": "test-worker"}
        self.calls = []
        self.metadata = None
        self.listed = False

    def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        if method == "GET" and path.endswith("/deployments"):
            if not self.listed:
                self.listed = True
                return {"deployments": [{"id": identity} for identity in self.previous]}
            return {"deployments": [{"id": identity} for identity in self.next_ids]}
        if method == "POST" and path.endswith("/assets-upload-session"):
            content = "<h1>Test</h1>"
            encoded = base64.b64encode(content.encode()).decode()
            digest = sha256((encoded + "html").encode()).hexdigest()[:32]
            return {"jwt": "synthetic-upload-jwt", "buckets": [[digest]]}
        if method == "POST" and path.endswith("/workers/assets/upload?base64=true"):
            return {"jwt": "synthetic-completion-jwt"}
        if method == "PUT" and "/workers/scripts/" in path:
            self.metadata = json.loads(kwargs["files"]["metadata"][1])
            return self.upload_result
        if method == "GET" and path.endswith("/settings"):
            return {"bindings": self.metadata["bindings"], "tags": self.metadata["tags"]}
        raise AssertionError((method, path))


def artifact():
    return {
        "assets": {"index.html": "<h1>Test</h1>"},
        "modules": {"worker.js": {"content": "export default {}", "mime": "application/javascript+module"}},
        "main_module": "worker.js",
        "compatibility_flags": [],
        "revision_id": "revision-test",
    }


def test_cloudflare_upload_resolves_new_deployment_id_from_documented_endpoint():
    gateway = GatewayStub(previous=[OLD_ID], next_ids=[NEW_ID, OLD_ID])
    result = gateway.upload("test-worker", "synthetic-database-id", artifact())
    assert result == NEW_ID
    assert gateway.metadata["bindings"] == [
        {"type": "d1", "name": "DB", "database_id": "synthetic-database-id"},
        {"type": "assets", "name": "ASSETS"},
    ]
    assert gateway.metadata["assets"]["jwt"] == "synthetic-completion-jwt"
    assert gateway.calls[0][0] == "GET" and gateway.calls[-1][0] == "GET"


def test_cloudflare_upload_fails_closed_if_deployment_does_not_advance():
    gateway = GatewayStub(previous=[OLD_ID], next_ids=[OLD_ID])
    with pytest.raises(PublicationUncertain):
        gateway.upload("test-worker", "synthetic-database-id", artifact())


def test_cloudflare_upload_fails_closed_if_server_returns_another_script():
    gateway = GatewayStub(upload_result={"id": "another-script"})
    with pytest.raises(PublicationUncertain):
        gateway.upload("test-worker", "synthetic-database-id", artifact())


def test_cloudflare_upload_reuses_explicit_documented_deployment_id_if_supplied():
    gateway = GatewayStub(upload_result={"id": "test-worker", "deployment_id": NEW_ID})
    assert gateway.upload("test-worker", "db", artifact()) == NEW_ID
    assert sum(1 for method, path in gateway.calls if method == "GET") == 1


def test_executor_reports_missing_dependency_without_marking_available(monkeypatch, tmp_path):
    monkeypatch.setattr(publication_build, "TOOLCHAIN", tmp_path)
    diagnostics = publication_build.IsolatedBuild().diagnostics()
    assert diagnostics["code"] == "TOOLCHAIN_MISSING"
    assert diagnostics["available"] is False


def test_executor_reports_denied_namespace_with_safe_message(monkeypatch, tmp_path):
    (tmp_path / "build.mjs").touch()
    (tmp_path / "probe.mjs").touch()
    (tmp_path / "node_modules" / "esbuild").mkdir(parents=True)
    monkeypatch.setattr(publication_build, "TOOLCHAIN", tmp_path)
    monkeypatch.setattr(publication_build.shutil, "which", lambda _: "/usr/bin/bwrap")
    real_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self: True if str(self) in {"/usr/bin/node", "/usr/bin/unshare"} else real_is_file(self))
    monkeypatch.setattr(publication_build.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stderr=b"bwrap: unshare: Operation not permitted"))
    diagnostics = publication_build.IsolatedBuild().diagnostics()
    assert diagnostics["code"] == "NAMESPACE_DENIED"
    assert diagnostics["available"] is False
    assert "--privileged" in diagnostics["message"]


def test_executor_reports_failed_isolation_check_without_releasing(monkeypatch):
    builder = publication_build.IsolatedBuild()
    monkeypatch.setattr(builder, "_preflight", lambda: ("READY", "teste"))
    monkeypatch.setattr(builder, "_probe", lambda: {"no_network": False, "resource_limits": True})
    monkeypatch.setattr(builder, "fingerprint", lambda: "f" * 64)
    diagnostics = builder.diagnostics()
    assert diagnostics["code"] == "ISOLATION_PROBE_FAILED"
    assert not diagnostics["available"]
    assert "no_network" in diagnostics["failed_checks"]
