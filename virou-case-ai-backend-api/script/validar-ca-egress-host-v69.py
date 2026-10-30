import argparse
import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import time

TARGETS = {
    "hono": "viroucase-ai-check-256ca8c9727794d0e61629de.felipe-themarkiron.workers.dev",
    "fastapi": "viroucase-ai-check-bbba376b77cc7d78321dad8d.felipe-themarkiron.workers.dev",
}
PATHS = ("/api/health", "/api/__viroucaseai_revision")


def emit(*fields):
    print("|".join(str(value).replace("\n", " ").replace("|", "/") for value in fields), flush=True)


def request(host, address, path, tcp_timeout=3.0, tls_timeout=6.0):
    start = time.monotonic()
    phase = "TCP"
    sock = None
    secure = None
    try:
        sock = socket.create_connection((address, 443), timeout=tcp_timeout)
        emit("TCP_OK", "ip=" + address, "source=" + sock.getsockname()[0], "ms=" + str(int((time.monotonic() - start) * 1000)))
        phase = "TLS"
        sock.settimeout(tls_timeout)
        secure = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        emit("TLS_OK", "ip=" + address, "version=" + str(secure.version()))
        phase = "HTTP"
        secure.sendall(("GET " + path + " HTTP/1.1\r\nHost: " + host + "\r\nAccept: application/json\r\nConnection: close\r\n\r\n").encode("ascii"))
        response = http.client.HTTPResponse(secure)
        response.begin()
        payload = response.read(4097)
        parsed = json.loads(payload.decode("utf-8")) if len(payload) <= 4096 else None
        valid = response.status == 200 and isinstance(parsed, dict) and "json" in response.getheader("Content-Type", "").lower()
        emit("HTTP_RESULT", "ip=" + address, "path=" + path, "status=" + str(response.status), "valid=" + str(valid).lower())
        return valid, "OK" if valid else "HTTP"
    except ssl.SSLCertVerificationError as exc:
        emit("PROBE_FAIL", "ip=" + address, "path=" + path, "stage=TLS", "error=SSLCertVerificationError", "verify_code=" + str(getattr(exc, "verify_code", "unknown")), "verify_message=" + str(getattr(exc, "verify_message", "unknown")), "ms=" + str(int((time.monotonic() - start) * 1000)))
        return False, "CERT"
    except (OSError, ssl.SSLError, ValueError, UnicodeError, json.JSONDecodeError, http.client.HTTPException) as exc:
        emit("PROBE_FAIL", "ip=" + address, "path=" + path, "stage=" + phase, "error=" + type(exc).__name__, "errno=" + str(getattr(exc, "errno", None)), "ms=" + str(int((time.monotonic() - start) * 1000)))
        return False, phase
    finally:
        if secure is not None:
            try:
                secure.close()
            except OSError:
                pass
        elif sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def fingerprint(path):
    try:
        with open(path, "rb") as source:
            content = source.read()
        return str(len(content)) + "B:sha256=" + hashlib.sha256(content).hexdigest()[:16]
    except OSError:
        return "unavailable"


def run(args):
    paths = ssl.get_default_verify_paths()
    cafile = os.environ.get("SSL_CERT_FILE") or paths.cafile or "unavailable"
    emit("RUNTIME", "case=" + args.case, "python=" + sys.version.split()[0], "openssl=" + ssl.OPENSSL_VERSION.replace(" ", "_"), "cafile=" + cafile, "ca_fingerprint=" + fingerprint(cafile), "netns=" + (os.readlink("/proc/self/ns/net") if os.path.exists("/proc/self/ns/net") else "unavailable"))
    total = 0
    passed = 0
    for name, host in TARGETS.items():
        for address in args.ip:
            valid = True
            for path in PATHS:
                emit("PROBE_START", "case=" + args.case, "target=" + name, "ip=" + address, "path=" + path)
                good, stage = request(host, address, path, args.tcp_timeout, args.tls_timeout)
                if not good:
                    valid = False
                    emit("TARGET_FAIL", "case=" + args.case, "target=" + name, "ip=" + address, "stage=" + stage)
                    break
            total += 1
            if valid:
                passed += 1
                emit("TARGET_PASS", "case=" + args.case, "target=" + name, "ip=" + address)
    outcome = "PASS" if total > 0 and total == passed else "PARTIAL" if passed else "FAIL"
    emit("CASE_RESULT", "case=" + args.case, "status=" + outcome, "pairs=" + str(passed) + "/" + str(total))
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--ip", action="append", required=True)
    parser.add_argument("--tcp-timeout", type=float, default=3.0)
    parser.add_argument("--tls-timeout", type=float, default=6.0)
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
