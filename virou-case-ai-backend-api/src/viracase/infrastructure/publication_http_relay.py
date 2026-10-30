import json
import os
import re
import socketserver
import stat
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

import httpx

SOCKET_PATH = "/run/viracase-verifier/http.sock"
CA_PATH = "/etc/ssl/certs/ca-certificates.crt"
MAX_RESPONSE = 65537
MAX_REQUEST = 2048
ALLOWED_PATHS = frozenset(("/", "/api/health", "/api/__viroucaseai_revision"))
HOST_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.workers\.dev")
PROBE_PATTERN = re.compile(r"__viroucaseai_probe=[a-f0-9]{32}")
RESPONSE_HEADERS = ("content-type", "retry-after", "cf-ray")


def authorized_target(url):
    if not isinstance(url, str) or len(url) > 512 or any(ord(char) < 33 or ord(char) > 126 for char in url):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and HOST_PATTERN.fullmatch(parsed.netloc)
        and parsed.path in ALLOWED_PATHS
        and PROBE_PATTERN.fullmatch(parsed.query)
        and not parsed.fragment
    )


def fetch_public_probe(url, timeout, ca_file):
    with httpx.Client(verify=ca_file, trust_env=False, follow_redirects=False, timeout=timeout) as client:
        with client.stream(
            "GET", url,
            headers={"Cache-Control": "no-cache", "Accept": "text/html" if urlsplit(url).path == "/" else "application/json"},
        ) as upstream:
            body = bytearray()
            for chunk in upstream.iter_bytes(chunk_size=4096):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE:
                    del body[MAX_RESPONSE:]
                    break
            return upstream.status_code, {
                key: upstream.headers[key]
                for key in RESPONSE_HEADERS
                if key in upstream.headers
            }, bytes(body)


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def respond(self, status, body=b"", headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.respond(200, b'{"ok":true}', {"content-type": "application/json"})
        else:
            self.respond(404)

    def do_POST(self):
        if self.path != "/fetch" or self.headers.get("Transfer-Encoding"):
            self.respond(404)
            return
        raw_length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,5}", raw_length):
            self.respond(400)
            return
        length = int(raw_length)
        if length < 1 or length > MAX_REQUEST:
            self.respond(413)
            return
        try:
            request = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            self.respond(400)
            return
        if not isinstance(request, dict) or set(request) != {"url", "timeout"}:
            self.respond(400)
            return
        url, timeout = request["url"], request["timeout"]
        if not authorized_target(url) or type(timeout) not in (int, float) or not 0.1 <= timeout <= 120:
            self.respond(403)
            return
        try:
            status, headers, content = self.server.fetch(url, timeout, self.server.ca_file)
        except (httpx.HTTPError, OSError, ValueError):
            self.respond(502)
            return
        self.respond(status, content, headers)


class UnixRelayServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False


def make_server(path, ca_file, fetch=fetch_public_probe):
    socket_path = Path(path)
    if socket_path.is_symlink():
        raise RuntimeError("verification_socket_invalid")
    if socket_path.exists():
        if not stat.S_ISSOCK(socket_path.lstat().st_mode):
            raise RuntimeError("verification_socket_invalid")
        socket_path.unlink()
    old_umask = os.umask(0o077)
    try:
        server = UnixRelayServer(str(socket_path), RelayHandler)
    finally:
        os.umask(old_umask)
    server.ca_file = str(ca_file)
    server.fetch = fetch
    os.chmod(socket_path, 0o600)
    return server


class UnixRelayTransport(httpx.BaseTransport):
    def __init__(self, socket_path):
        self.socket_path = str(socket_path)

    def handle_request(self, request):
        if request.method != "GET" or not authorized_target(str(request.url)):
            raise httpx.ConnectError("verification_target_rejected", request=request)
        values = request.extensions.get("timeout") or {}
        timeout = values.get("read") or values.get("connect") or 15.0
        timeout = min(120.0, max(0.1, float(timeout)))
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=self.socket_path),
                trust_env=False, follow_redirects=False, timeout=timeout + 3.0,
            ) as client:
                relay = client.post(
                    "http://verifier.local/fetch",
                    json={"url": str(request.url), "timeout": timeout},
                )
                return httpx.Response(
                    status_code=relay.status_code,
                    headers={key: value for key, value in relay.headers.items() if key in RESPONSE_HEADERS},
                    content=relay.content,
                    request=request,
                )
        except httpx.HTTPError:
            raise httpx.ConnectError("verification_relay_unavailable", request=request) from None


def configured_transport():
    socket_path = os.environ.get("VIRA_REMOTE_VERIFIER_SOCKET", "")
    return UnixRelayTransport(socket_path) if socket_path else None


def initialize_volume():
    uid = int(os.environ["VIRA_RUNTIME_UID"])
    gid = int(os.environ["VIRA_RUNTIME_GID"])
    if uid <= 0 or gid <= 0:
        raise ValueError("invalid_runtime_identity")
    folder = Path(SOCKET_PATH).parent
    if folder.is_symlink() or not folder.is_dir():
        raise RuntimeError("verification_volume_missing")
    os.chown(folder, uid, gid)
    os.chmod(folder, 0o700)


def run_server():
    folder = Path(SOCKET_PATH).parent
    if not folder.is_dir() or folder.stat().st_uid != os.geteuid():
        raise RuntimeError("verification_volume_permissions_invalid")
    if not Path(CA_PATH).is_file():
        raise RuntimeError("verification_host_ca_missing")
    import ssl
    ssl.create_default_context(cafile=CA_PATH)
    server = make_server(SOCKET_PATH, CA_PATH)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        Path(SOCKET_PATH).unlink(missing_ok=True)


def health():
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=SOCKET_PATH), timeout=2.0, trust_env=False) as client:
            response = client.get("http://verifier.local/health")
            return response.status_code == 200 and response.json() == {"ok": True}
    except (httpx.HTTPError, OSError, ValueError):
        return False


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "init":
        initialize_volume()
    elif command == "run":
        run_server()
    elif command == "health":
        sys.exit(0 if health() else 1)
    else:
        raise ValueError("invalid_verification_command")


if __name__ == "__main__":
    main()
