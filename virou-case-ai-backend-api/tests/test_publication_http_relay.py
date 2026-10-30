import json
import os
import tempfile
import threading
from pathlib import Path

import httpx
import pytest

from viracase.infrastructure.cloudflare_gateway import CloudflareGateway
from viracase.infrastructure.publication_http_relay import (
    UnixRelayTransport,
    authorized_target,
    configured_transport,
    make_server,
)
from viracase.infrastructure.publication_verification import RemoteVerifier, VerificationFailure

URL = "https://probe.fixture.workers.dev"
REVISION = "b8434aba-415e-4edf-b18e-bcb2511e02d4"


@pytest.fixture
def socket_server():
    with tempfile.TemporaryDirectory(prefix="vca-verifier-") as directory:
        socket_path = str(Path(directory) / "verify.sock")
        calls = []

        def fetch(url, timeout, ca_file):
            calls.append((url, timeout, ca_file))
            path = httpx.URL(url).path
            if path == "/":
                return 200, {"content-type": "text/html"}, b"<h1>Success</h1>"
            if path == "/api/__viroucaseai_revision":
                return 200, {"content-type": "application/json"}, json.dumps({"revision_id": REVISION, "database": True}).encode()
            return 200, {"content-type": "application/json"}, b'{"ok":true,"database":true}'

        server = make_server(socket_path, "/tmp/ca-file-test.pem", fetch=fetch)
        runner = threading.Thread(target=server.serve_forever, daemon=True)
        runner.start()
        try:
            yield socket_path, calls, server
        finally:
            server.shutdown()
            runner.join(timeout=5)
            server.server_close()


def test_private_socket_verifies_all_existing_probes_without_changing_contract(socket_server, monkeypatch):
    path, calls, server = socket_server
    monkeypatch.setenv("VIRA_REMOTE_VERIFIER_SOCKET", path)
    verified = RemoteVerifier(5).verify(URL, REVISION)
    assert verified["passed"] is True
    assert [part["name"] for part in verified["attempts"][0]["checks"]] == ["frontend", "revision", "api"]
    assert len(calls) == 3
    assert all("__viroucaseai_probe=" in url for url, _, _ in calls)
    assert all(ca_file == "/tmp/ca-file-test.pem" for _, _, ca_file in calls)
    assert isinstance(configured_transport(), UnixRelayTransport)
    assert server.server_address == path


def test_direct_verification_unchanged_when_sidecar_not_configured(monkeypatch):
    monkeypatch.delenv("VIRA_REMOTE_VERIFIER_SOCKET", raising=False)
    assert configured_transport() is None
    assert RemoteVerifier(5, httpx.MockTransport(lambda request: httpx.Response(200))).transport is not None


@pytest.mark.parametrize("url", [
    "http://probe.fixture.workers.dev/api/health?__viroucaseai_probe=" + "a" * 32,
    "https://127.0.0.1/api/health?__viroucaseai_probe=" + "a" * 32,
    "https://probe.fixture.workers.dev:443/api/health?__viroucaseai_probe=" + "a" * 32,
    "https://probe.fixture.workers.dev.evil.test/api/health?__viroucaseai_probe=" + "a" * 32,
    "https://user:password@probe.fixture.workers.dev/api/health?__viroucaseai_probe=" + "a" * 32,
    "https://probe.fixture.workers.dev/admin?__viroucaseai_probe=" + "a" * 32,
    "https://probe.fixture.workers.dev/api/health?wrong=" + "a" * 32,
    "https://probe.fixture.workers.dev/api/health?__viroucaseai_probe=" + "a" * 33,
    "https://probe.fixture.workers.dev/api/health?__viroucaseai_probe=" + "a" * 32 + "&next=1",
])
def test_relay_refuses_unapproved_targets(socket_server, url):
    path, calls, _ = socket_server
    assert not authorized_target(url)
    with httpx.Client(transport=httpx.HTTPTransport(uds=path), timeout=2) as client:
        response = client.post("http://verifier.local/fetch", json={"url": url, "timeout": 3.0})
    assert response.status_code == 403
    assert calls == []


def test_relay_ignores_worker_supplied_auth_headers(socket_server):
    path, calls, _ = socket_server
    request = httpx.Request(
        "GET", URL + "/api/health?__viroucaseai_probe=" + "a" * 32,
        headers={"Authorization": "Bearer secret", "Cookie": "token=secret"},
    )
    with httpx.Client(transport=UnixRelayTransport(path), timeout=5) as client:
        response = client.send(request)
    assert response.status_code == 200
    assert len(calls) == 1
    assert "secret" not in calls[0][0]


def test_inaccessible_socket_fails_closed(tmp_path):
    transport = UnixRelayTransport(str(tmp_path / "missing.sock"))
    with httpx.Client(transport=transport, timeout=1) as client:
        with pytest.raises(httpx.ConnectError):
            client.get(URL + "/api/health?__viroucaseai_probe=" + "a" * 32)


def test_upstream_http_error_remains_verification_failure(socket_server, monkeypatch):
    path, calls, server = socket_server

    def denied(url, timeout, ca_file):
        calls.append((url, timeout, ca_file))
        if httpx.URL(url).path == "/api/health":
            return 403, {"content-type": "text/plain"}, b"rejected"
        if httpx.URL(url).path == "/":
            return 200, {"content-type": "text/html"}, b"ok"
        return 200, {"content-type": "application/json"}, json.dumps({"revision_id": REVISION, "database": True}).encode()

    server.fetch = denied
    monkeypatch.setenv("VIRA_REMOTE_VERIFIER_SOCKET", path)
    with pytest.raises(VerificationFailure) as exc:
        RemoteVerifier(5).verify(URL, REVISION)
    assert exc.value.code == "PUBLICATION_ACCESS_DENIED"
    assert exc.value.diagnostic["failed_probe"] == "api"
    assert len(calls) == 3


def test_bad_request_and_upstream_outage_do_not_expose_payload(socket_server):
    path, calls, server = socket_server
    with httpx.Client(transport=httpx.HTTPTransport(uds=path), timeout=2) as client:
        invalid = client.post("http://verifier.local/fetch", json={"url": "secret", "timeout": 4})
        assert invalid.status_code == 403

        def unavailable(url, timeout, ca_file):
            raise httpx.ConnectTimeout("private value must not leak")

        server.fetch = unavailable
        response = client.post("http://verifier.local/fetch", json={
            "url": URL + "/api/health?__viroucaseai_probe=" + "b" * 32,
            "timeout": 5,
        })
    assert response.status_code == 502
    assert b"private value" not in response.content
    assert calls == []


def test_health_endpoint_is_unix_only(socket_server):
    path, _, _ = socket_server
    with httpx.Client(transport=httpx.HTTPTransport(uds=path), timeout=2) as client:
        response = client.get("http://verifier.local/health")
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert client.get("http://verifier.local/").status_code == 404


def test_socket_file_is_owner_only(socket_server):
    path, _, _ = socket_server
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_cloudflare_gateway_uses_existing_verification_contract_with_socket(socket_server, monkeypatch):
    path, calls, _ = socket_server
    monkeypatch.setenv("VIRA_REMOTE_VERIFIER_SOCKET", path)
    gateway = CloudflareGateway({"account_id": "safe-account", "timeout_seconds": 5}, "private-token")
    verbs = []
    gateway.before_request = verbs.append
    output = gateway.verify(URL, REVISION)
    assert output["passed"] is True
    assert verbs == ["GET", "GET", "GET"]
    assert len(calls) == 3
    assert "private-token" not in str(calls)
