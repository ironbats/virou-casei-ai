import argparse
import http.client
import ipaddress
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TARGETS = {
    "hono": "viroucase-ai-check-256ca8c9727794d0e61629de.felipe-themarkiron.workers.dev",
    "fastapi": "viroucase-ai-check-bbba376b77cc7d78321dad8d.felipe-themarkiron.workers.dev",
}
PATHS = ("/api/health", "/api/__viroucaseai_revision")


def emit(*parts):
    print("|".join(str(part).replace("\n", " ").replace("|", "/") for part in parts), flush=True)


def failure(exc):
    current = exc
    chain = []
    for _ in range(3):
        if current is None:
            break
        chain.append(type(current).__name__)
        current = getattr(current, "__cause__", None)
    return ">".join(chain)


def resolve_ipv4(host):
    try:
        return list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)))
    except OSError as exc:
        emit("DNS_FAIL", "host=" + host, "error=" + failure(exc))
        return []


def direct_exchange(host, address, path, timeout_tcp, timeout_tls):
    start = time.monotonic()
    phase = "TCP"
    sock = None
    secured = None
    try:
        sock = socket.create_connection((address, 443), timeout=timeout_tcp)
        tcp_ms = int((time.monotonic() - start) * 1000)
        source = sock.getsockname()[0]
        emit("TCP_OK", "dest=" + address, "source=" + source, "ms=" + str(tcp_ms))
        phase = "TLS"
        sock.settimeout(timeout_tls)
        secured = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        emit("TLS_OK", "dest=" + address, "version=" + str(secured.version()))
        phase = "HTTP"
        request = "GET {} HTTP/1.1\r\nHost: {}\r\nAccept: application/json\r\nConnection: close\r\n\r\n".format(path, host)
        secured.sendall(request.encode("ascii"))
        response = http.client.HTTPResponse(secured)
        response.begin()
        body = response.read(4097)
        content_type = response.getheader("Content-Type", "")
        payload = json.loads(body.decode("utf-8")) if len(body) <= 4096 else None
        valid = response.status == 200 and isinstance(payload, dict) and "json" in content_type.lower()
        emit("HTTP_RESULT", "dest=" + address, "path=" + path, "status=" + str(response.status), "json=" + str(valid).lower(), "ms=" + str(int((time.monotonic() - start) * 1000)))
        return valid, "HTTP" if not valid else "OK"
    except (OSError, ssl.SSLError, ValueError, UnicodeError, json.JSONDecodeError, http.client.HTTPException) as exc:
        emit("PROBE_FAIL", "dest=" + address, "stage=" + phase, "error=" + failure(exc), "ms=" + str(int((time.monotonic() - start) * 1000)))
        return False, phase
    finally:
        if secured is not None:
            try:
                secured.close()
            except OSError:
                pass
        elif sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def probe(args):
    emit("RUNTIME", "case=" + args.case, "python=" + sys.version.split()[0], "openssl=" + ssl.OPENSSL_VERSION.replace(" ", "_"), "netns=" + (os.readlink("/proc/self/ns/net") if os.path.exists("/proc/self/ns/net") else "unknown"))
    target_pass = {}
    count_ok = 0
    count_total = 0
    for name, host in TARGETS.items():
        resolved = resolve_ipv4(host)
        emit("DNS", "target=" + name, "resolved=" + ",".join(resolved or ["none"]))
        available = list(dict.fromkeys(args.ip)) if args.ip else resolved[:2]
        passes = 0
        for address in available:
            emit("TARGET_START", "case=" + args.case, "target=" + name, "ip=" + address)
            good = True
            for path in PATHS:
                valid, stage = direct_exchange(host, address, path, args.tcp_timeout, args.tls_timeout)
                if not valid:
                    good = False
                    emit("TARGET_FAIL", "case=" + args.case, "target=" + name, "ip=" + address, "stage=" + stage)
                    break
            count_total += 1
            if good:
                passes += 1
                count_ok += 1
                emit("TARGET_PASS", "case=" + args.case, "target=" + name, "ip=" + address)
        target_pass[name] = passes
    control_ip = resolve_ipv4("api.cloudflare.com")
    if control_ip:
        ok, stage = direct_exchange_control(control_ip[0], args.tcp_timeout, args.tls_timeout)
        emit("CONTROL_RESULT", "case=" + args.case, "result=" + ("OK" if ok else "FAIL"), "stage=" + stage)
    else:
        emit("CONTROL_RESULT", "case=" + args.case, "result=DNS_FAIL")
    if count_total == 0 or any(v == 0 for v in target_pass.values()):
        verdict = "FAIL"
    elif count_ok == count_total:
        verdict = "PASS"
    else:
        verdict = "PARTIAL"
    emit("CASE_RESULT", "case=" + args.case, "status=" + verdict, "pairs=" + str(count_ok) + "/" + str(count_total), "hono=" + str(target_pass.get("hono", 0)), "fastapi=" + str(target_pass.get("fastapi", 0)))
    return 0


def direct_exchange_control(address, tcp_timeout, tls_timeout):
    start = time.monotonic()
    sock = None
    secured = None
    stage = "TCP"
    try:
        sock = socket.create_connection((address, 443), timeout=tcp_timeout)
        stage = "TLS"
        sock.settimeout(tls_timeout)
        secured = ssl.create_default_context().wrap_socket(sock, server_hostname="api.cloudflare.com")
        stage = "HTTP"
        secured.sendall(b"GET / HTTP/1.1\r\nHost: api.cloudflare.com\r\nConnection: close\r\n\r\n")
        response = http.client.HTTPResponse(secured)
        response.begin()
        response.read(512)
        return 200 <= response.status < 500, stage
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        return False, stage + ":" + failure(exc)
    finally:
        if secured:
            secured.close()
        elif sock:
            sock.close()


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        remote = self.client_address[0]
        if remote not in self.server.allowed_ips:
            self.send_error(403)
            return
        permitted = {
            "/hono/health": ("hono", "/api/health"),
            "/fastapi/health": ("fastapi", "/api/health"),
            "/hono/revision": ("hono", "/api/__viroucaseai_revision"),
            "/fastapi/revision": ("fastapi", "/api/__viroucaseai_revision"),
        }
        if self.path not in permitted:
            self.send_error(404)
            return
        name, path = permitted[self.path]
        host = TARGETS[name]
        valid = False
        for address in resolve_ipv4(host)[:2]:
            valid, stage = direct_exchange(host, address, path, 2.5, 4.5)
            if valid:
                break
        encoded = json.dumps({"upstream_status": 200 if valid else 0, "json_valid": valid}, separators=(",", ":")).encode("ascii")
        self.send_response(200 if valid else 502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except OSError:
            pass


def relay_server(args):
    allowed = set()
    for raw in args.allow_ip:
        allowed.add(str(ipaddress.IPv4Address(raw)))
    server = RelayServer((args.bind, 0), RelayHandler)
    server.allowed_ips = allowed
    Path(args.ready).write_text(str(server.server_address[1]), encoding="ascii")
    deadline = time.monotonic() + args.lifetime
    server.timeout = 0.5
    try:
        while time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    return 0


def relay_probe(args):
    passed = 0
    for name in TARGETS:
        for path in ("health", "revision"):
            url = "http://" + args.relay_gateway + ":" + str(args.relay_port) + "/" + name + "/" + path
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                response = opener.open(url, timeout=10)
                try:
                    body = json.loads(response.read(2048).decode("utf-8"))
                    valid = response.status == 200 and body.get("upstream_status") == 200 and body.get("json_valid") is True
                    emit("RELAY_RESPONSE", "case=" + args.case, "target=" + name, "path=" + path, "status=" + str(response.status), "valid=" + str(valid).lower())
                finally:
                    response.close()
                if valid:
                    passed += 1
            except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
                emit("RELAY_FAIL", "case=" + args.case, "target=" + name, "path=" + path, "error=" + failure(exc))
    emit("CASE_RESULT", "case=" + args.case, "status=" + ("PASS_INDIRECT" if passed == 4 else "FAIL"), "checks=" + str(passed) + "/4")
    return 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--case", required=True)
    p.add_argument("--ip", action="append", default=[])
    p.add_argument("--tcp-timeout", type=float, default=2.5)
    p.add_argument("--tls-timeout", type=float, default=4.5)
    r = sub.add_parser("relay-server")
    r.add_argument("--bind", required=True)
    r.add_argument("--allow-ip", action="append", required=True)
    r.add_argument("--ready", required=True)
    r.add_argument("--lifetime", type=int, default=45)
    t = sub.add_parser("relay-probe")
    t.add_argument("--case", required=True)
    t.add_argument("--relay-gateway", required=True)
    t.add_argument("--relay-port", type=int, required=True)
    args = parser.parse_args()
    if args.command == "probe":
        return probe(args)
    if args.command == "relay-server":
        return relay_server(args)
    return relay_probe(args)


if __name__ == "__main__":
    sys.exit(main())
