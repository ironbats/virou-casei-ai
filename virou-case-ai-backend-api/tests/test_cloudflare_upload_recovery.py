from copy import deepcopy

import httpx
import pytest

from viracase.domain.rules import RuleError
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway, PublicationUncertain
from viracase.infrastructure.publication_verification import failure_record

POLICY = {
    "account_id": "a" * 32,
    "timeout_seconds": 15,
    "compatibility_date": "2026-10-27",
    "spa": False,
}
OLD_ID = "00000000-0000-0000-0000-000000000001"
NEW_ID = "00000000-0000-0000-0000-000000000002"
OTHER_ID = "00000000-0000-0000-0000-000000000003"
ARTIFACT = {
    "assets": {"index.html": "<h1>Probe</h1>"},
    "modules": {"entry.py": {"content": "from workers import WorkerEntrypoint\n", "mime": "text/x-python"}},
    "main_module": "entry.py",
    "compatibility_flags": ["python_workers"],
    "revision_id": "expected-revision",
}


class Clock:
    def __init__(self):
        self.elapsed = 0

    def now(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


class UploadServer:
    def __init__(self, mode="success", visible_after=1, settings=None, deployments=None):
        self.mode, self.visible_after = mode, visible_after
        self.calls, self.timeouts = [], []
        self.puts = self.confirmations = 0
        self.settings = settings if settings is not None else {
            "tags": ["viroucaseai", ARTIFACT["revision_id"]],
            "bindings": [
                {"type": "d1", "name": "DB", "database_id": "test-database"},
                {"type": "assets", "name": "ASSETS"},
            ],
        }
        self.deployments = deployments if deployments is not None else [NEW_ID, OLD_ID]

    @staticmethod
    def response(result):
        return httpx.Response(200, json={"success": True, "result": result})

    def handler(self, request):
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        if method == "GET" and path.endswith("/deployments"):
            if not self.puts:
                return self.response({"deployments": [{"id": OLD_ID}]})
            self.confirmations += 1
            if self.mode == "read_error" and self.confirmations == 1:
                return httpx.Response(503, json={"success": False})
            identities = self.deployments if self.confirmations >= self.visible_after else [OLD_ID]
            return self.response({"deployments": [{"id": identity} for identity in identities]})
        if method == "POST" and path.endswith("/assets-upload-session"):
            return self.response({"jwt": "synthetic-completion-jwt", "buckets": []})
        if method == "PUT" and path.endswith("/workers/scripts/test-worker"):
            self.puts += 1
            self.timeouts.append(request.extensions["timeout"])
            if self.mode == "timeout":
                raise httpx.ReadTimeout("Authorization: Bearer never-expose-this", request=request)
            if self.mode == "disconnected":
                raise httpx.RemoteProtocolError("private-upstream-response", request=request)
            if self.mode == "server_error":
                return httpx.Response(503, headers={"cf-ray": "0123456789abcdef-GRU"}, json={"success": False})
            if self.mode == "forbidden":
                return httpx.Response(403, json={"success": False, "errors": [{"code": 10000}]})
            if self.mode == "empty_response":
                return httpx.Response(200, content=b"")
            if self.mode == "invalid_json":
                return httpx.Response(200, content=b"private-upstream-message", headers={"content-type": "application/json"})
            if self.mode == "invalid_envelope":
                return httpx.Response(200, json=[])
            return self.response({"id": "test-worker"})
        if method == "GET" and path.endswith("/settings"):
            return self.response(self.settings)
        raise AssertionError((method, path))


def gateway_for(server):
    clock = Clock()
    gateway = CloudflareGateway(
        POLICY, "synthetic-test-token", httpx.MockTransport(server.handler),
        clock=clock.now, sleep=clock.sleep,
    )
    return gateway, clock


def test_python_upload_allows_snapshot_time_while_preserving_connection_timeout():
    server = UploadServer()
    gateway, clock = gateway_for(server)
    assert gateway.upload("test-worker", "test-database", ARTIFACT) == NEW_ID
    assert server.timeouts == [{"connect": 15, "read": 120, "write": 15, "pool": 15}]
    assert clock.elapsed == 0


@pytest.mark.parametrize("mode", ["timeout", "server_error", "disconnected", "empty_response", "invalid_json", "invalid_envelope"])
def test_uncertain_upload_is_confirmed_by_reads_without_repeating_any_write(mode):
    server = UploadServer(mode, visible_after=3)
    gateway, clock = gateway_for(server)
    assert gateway.upload("test-worker", "test-database", ARTIFACT) == NEW_ID
    assert server.puts == 1 and server.confirmations == 3
    assert clock.elapsed == 3
    writes = [(method, path) for method, path in server.calls if method != "GET"]
    assert [method for method, path in writes] == ["POST", "PUT"]


def test_successful_upload_waits_for_deployment_inventory_propagation():
    server = UploadServer(visible_after=4)
    gateway, clock = gateway_for(server)
    assert gateway.upload("test-worker", "test-database", ARTIFACT) == NEW_ID
    assert server.puts == 1 and server.confirmations == 4
    assert clock.elapsed == 7


def test_transient_read_failure_can_recover_without_reupload():
    server = UploadServer("read_error")
    gateway, _clock = gateway_for(server)
    assert gateway.upload("test-worker", "test-database", ARTIFACT) == NEW_ID
    assert server.puts == 1 and server.confirmations == 2


def test_unknown_upload_never_accepts_a_deployment_with_another_revision():
    settings = deepcopy(UploadServer().settings)
    settings["tags"] = ["viroucaseai", "another-revision"]
    server = UploadServer("timeout", settings=settings)
    gateway, clock = gateway_for(server)
    with pytest.raises(PublicationUncertain) as caught:
        gateway.upload("test-worker", "test-database", ARTIFACT)
    assert server.puts == 1 and server.confirmations > 1
    assert clock.elapsed < 45
    diagnostic = failure_record(caught.value, "verification_uploading", "fastapi-python-d1")
    assert diagnostic["provider"]["transport_error"] == "ReadTimeout"
    assert diagnostic["provider"]["upload_confirmation"]["confirmed"] is False
    assert "never-expose-this" not in str(diagnostic)


def test_unknown_upload_never_accepts_another_database_binding():
    settings = deepcopy(UploadServer().settings)
    settings["bindings"][0]["database_id"] = "another-project-database"
    server = UploadServer("timeout", settings=settings)
    gateway, _clock = gateway_for(server)
    with pytest.raises(PublicationUncertain) as caught:
        gateway.upload("test-worker", "test-database", ARTIFACT)
    assert server.puts == server.confirmations == 1
    assert caught.value.provider_diagnostic["upload_confirmation"]["attempts"][-1]["code"] == "BINDINGS_MISMATCH"


def test_multiple_new_deployments_remain_uncertain():
    server = UploadServer("timeout", deployments=[NEW_ID, OTHER_ID, OLD_ID])
    gateway, _clock = gateway_for(server)
    with pytest.raises(PublicationUncertain) as caught:
        gateway.upload("test-worker", "test-database", ARTIFACT)
    assert server.puts == server.confirmations == 1
    assert caught.value.provider_diagnostic["upload_confirmation"]["attempts"][-1]["code"] == "DEPLOYMENTS_AMBIGUOUS"


def test_unchanged_deployment_never_confirms_upload_and_has_a_bounded_deadline():
    server = UploadServer("timeout", deployments=[OLD_ID])
    gateway, clock = gateway_for(server)
    with pytest.raises(PublicationUncertain):
        gateway.upload("test-worker", "test-database", ARTIFACT)
    assert server.puts == 1
    assert 1 < server.confirmations <= 12
    assert clock.elapsed < 45


def test_rejected_upload_does_not_retry_or_reconcile_as_success():
    server = UploadServer("forbidden")
    gateway, _clock = gateway_for(server)
    with pytest.raises(RuleError) as caught:
        gateway.upload("test-worker", "test-database", ARTIFACT)
    assert caught.value.code == "CONFIGURATION_INVALID"
    assert server.puts == 1 and server.confirmations == 0


def test_invalid_read_response_stays_a_dependency_failure():
    gateway = CloudflareGateway(POLICY, "synthetic-test-token", httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"private-invalid-response")
    ))
    with pytest.raises(RuleError) as caught:
        gateway.request("GET", gateway.account_path)
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE"
    assert "private-invalid-response" not in str(caught.value.provider_diagnostic)
