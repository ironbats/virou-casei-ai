import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "script" / "alternativas-egress-cloudflare-v69.py"
SPEC = importlib.util.spec_from_file_location("vca_egress_probe", SCRIPT)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


class FakeSocket:
    def __init__(self):
        self.closed = False
        self.sent = b""

    def getsockname(self):
        return ("192.168.0.21", 42000)

    def settimeout(self, seconds):
        self.timeout = seconds

    def sendall(self, data):
        self.sent += data

    def close(self):
        self.closed = True

    def version(self):
        return "TLSv1.3"


class FakeContext:
    def __init__(self, socket_value, exc=None):
        self.sock = socket_value
        self.exc = exc

    def wrap_socket(self, value, server_hostname):
        if self.exc:
            raise self.exc
        return self.sock


class FakeResponse:
    status = 200

    def __init__(self, stream):
        self.stream = stream

    def begin(self):
        return None

    def getheader(self, name, default=""):
        return "application/json" if name == "Content-Type" else default

    def read(self, amount):
        return b'{"database":true}'


class ProbeTests(unittest.TestCase):
    def test_fixed_targets_and_paths(self):
        self.assertEqual(set(MOD.TARGETS), {"hono", "fastapi"})
        self.assertEqual(MOD.PATHS, ("/api/health", "/api/__viroucaseai_revision"))
        self.assertTrue(all(value.endswith(".workers.dev") for value in MOD.TARGETS.values()))

    def test_resolve_ipv4_only(self):
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.21.44.19", 443)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.21.44.19", 443))]
        with patch.object(MOD.socket, "getaddrinfo", return_value=fake) as getaddr:
            values = MOD.resolve_ipv4("example.com")
        self.assertEqual(values, ["104.21.44.19"])
        self.assertEqual(getaddr.call_args.args[2], socket.AF_INET)

    def test_connect_timeout_distinguishes_tcp(self):
        with patch.object(MOD.socket, "create_connection", side_effect=TimeoutError("refused")):
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                valid, stage = MOD.direct_exchange("host", "104.21.44.19", "/api/health", 1, 1)
        self.assertFalse(valid)
        self.assertEqual(stage, "TCP")
        self.assertIn("stage=TCP", captured.getvalue())

    def test_tls_timeout_distinguishes_tls(self):
        sock = FakeSocket()
        with patch.object(MOD.socket, "create_connection", return_value=sock), patch.object(MOD.ssl, "create_default_context", return_value=FakeContext(sock, TimeoutError("tls"))):
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                valid, stage = MOD.direct_exchange("host", "104.21.44.19", "/api/health", 1, 1)
        self.assertFalse(valid)
        self.assertEqual(stage, "TLS")
        self.assertIn("stage=TLS", captured.getvalue())

    def test_http_json_success(self):
        sock = FakeSocket()
        with patch.object(MOD.socket, "create_connection", return_value=sock), patch.object(MOD.ssl, "create_default_context", return_value=FakeContext(sock)), patch.object(MOD.http.client, "HTTPResponse", FakeResponse):
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                valid, stage = MOD.direct_exchange("host", "104.21.44.19", "/api/health", 1, 1)
        self.assertTrue(valid)
        self.assertEqual(stage, "OK")
        self.assertIn("HTTP_RESULT", captured.getvalue())
        self.assertIn(b"Host: host", sock.sent)
        self.assertIn(b"/api/health", sock.sent)
        self.assertTrue(sock.closed)

    def test_invalid_http_json_is_not_success(self):
        sock = FakeSocket()
        class WrongResponse(FakeResponse):
            status = 503
        with patch.object(MOD.socket, "create_connection", return_value=sock), patch.object(MOD.ssl, "create_default_context", return_value=FakeContext(sock)), patch.object(MOD.http.client, "HTTPResponse", WrongResponse):
            with contextlib.redirect_stdout(io.StringIO()):
                valid, stage = MOD.direct_exchange("host", "104.21.44.19", "/api/health", 1, 1)
        self.assertFalse(valid)
        self.assertEqual(stage, "HTTP")

    def test_probe_pass_requires_two_targets_and_paths(self):
        from argparse import Namespace
        args = Namespace(case="test", ip=["104.21.44.19"], tcp_timeout=1, tls_timeout=1)
        with patch.object(MOD, "resolve_ipv4", return_value=["104.21.44.19"]), patch.object(MOD, "direct_exchange", return_value=(True, "OK")) as check, patch.object(MOD, "direct_exchange_control", return_value=(True, "HTTP")):
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                rc = MOD.probe(args)
        self.assertEqual(rc, 0)
        self.assertEqual(check.call_count, 4)
        self.assertIn("status=PASS", captured.getvalue())

    def test_probe_partial_when_only_one_ip_is_healthy(self):
        from argparse import Namespace
        args = Namespace(case="test", ip=["104.21.44.19", "172.67.193.242"], tcp_timeout=1, tls_timeout=1)
        def check(host, ip, path, tcp, tls):
            return (ip == "104.21.44.19", "OK" if ip == "104.21.44.19" else "TCP")
        with patch.object(MOD, "resolve_ipv4", return_value=["104.21.44.19"]), patch.object(MOD, "direct_exchange", side_effect=check), patch.object(MOD, "direct_exchange_control", return_value=(True, "HTTP")):
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                MOD.probe(args)
        self.assertIn("status=PARTIAL", captured.getvalue())

    def test_probe_fails_when_both_workers_unreachable(self):
        from argparse import Namespace
        args = Namespace(case="test", ip=["104.21.44.19"], tcp_timeout=1, tls_timeout=1)
        with patch.object(MOD, "resolve_ipv4", return_value=["104.21.44.19"]), patch.object(MOD, "direct_exchange", return_value=(False, "TCP")), patch.object(MOD, "direct_exchange_control", return_value=(True, "HTTP")):
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                MOD.probe(args)
        self.assertIn("status=FAIL", captured.getvalue())

    def test_relay_rejects_unauthorized_source(self):
        srv = MOD.RelayServer(("127.0.0.1", 0), MOD.RelayHandler)
        srv.allowed_ips = {"192.168.176.2"}
        task = threading.Thread(target=srv.serve_forever, daemon=True)
        task.start()
        try:
            opener = MOD.urllib.request.build_opener(MOD.urllib.request.ProxyHandler({}))
            with self.assertRaises(MOD.urllib.error.HTTPError) as caught:
                opener.open("http://127.0.0.1:{}/hono/health".format(srv.server_address[1]), timeout=2)
            self.assertEqual(caught.exception.code, 403)
        finally:
            srv.shutdown()
            srv.server_close()
            task.join(2)

    def test_relay_allows_only_fixed_public_health_endpoints(self):
        srv = MOD.RelayServer(("127.0.0.1", 0), MOD.RelayHandler)
        srv.allowed_ips = {"127.0.0.1"}
        task = threading.Thread(target=srv.serve_forever, daemon=True)
        task.start()
        try:
            opener = MOD.urllib.request.build_opener(MOD.urllib.request.ProxyHandler({}))
            with self.assertRaises(MOD.urllib.error.HTTPError) as caught:
                opener.open("http://127.0.0.1:{}/any.other.domain".format(srv.server_address[1]), timeout=2)
            self.assertEqual(caught.exception.code, 404)
            with patch.object(MOD, "resolve_ipv4", return_value=["104.21.44.19"]), patch.object(MOD, "direct_exchange", return_value=(True, "OK")) as called:
                response = opener.open("http://127.0.0.1:{}/hono/health".format(srv.server_address[1]), timeout=2)
                with response:
                    payload = json.loads(response.read())
            self.assertTrue(payload["json_valid"])
            self.assertEqual(called.call_args.args[0], MOD.TARGETS["hono"])
            self.assertEqual(called.call_args.args[2], "/api/health")
        finally:
            srv.shutdown()
            srv.server_close()
            task.join(2)

    def test_relay_probe_reports_indirect_not_direct(self):
        srv = MOD.RelayServer(("127.0.0.1", 0), MOD.RelayHandler)
        srv.allowed_ips = {"127.0.0.1"}
        task = threading.Thread(target=srv.serve_forever, daemon=True)
        task.start()
        try:
            from argparse import Namespace
            args = Namespace(case="test_relay", relay_gateway="127.0.0.1", relay_port=srv.server_address[1])
            with patch.object(MOD, "resolve_ipv4", return_value=["104.21.44.19"]), patch.object(MOD, "direct_exchange", return_value=(True, "OK")):
                with contextlib.redirect_stdout(io.StringIO()) as captured:
                    MOD.relay_probe(args)
            self.assertIn("status=PASS_INDIRECT", captured.getvalue())
        finally:
            srv.shutdown()
            srv.server_close()
            task.join(2)


if __name__ == "__main__":
    unittest.main()
