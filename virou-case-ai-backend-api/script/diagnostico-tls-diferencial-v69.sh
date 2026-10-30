#!/usr/bin/env bash
set -uo pipefail
umask 077

START_TS=$(date +%s)
REPORT=$(mktemp /tmp/viroucase-tls-diferencial-v69.XXXXXXXX.txt)
PYFILE=$(mktemp /tmp/viroucase-tls-diferencial-v69.XXXXXXXX.py)
cleanup() {
  rm -f "$PYFILE"
}
trap cleanup EXIT

log() {
  local now elapsed
  now=$(date +%s)
  elapsed=$((now - START_TS))
  printf '[%s][+%ss] %s\n' "$(date +%H:%M:%S)" "$elapsed" "$*" | tee -a "$REPORT"
}

log 'INICIO | Comparacao TLS/IPv4 em tres contextos | somente leitura'
log "RELATORIO | $REPORT"
log 'ESCOPO | nenhuma configuracao, regra, container existente ou publicacao sera alterada'

if [[ ! -f compose.yaml && ! -f docker-compose.yml && ! -f docker-compose.yaml ]]; then
  log 'ERRO | Execute a partir da raiz do backend que contem o Compose'
  exit 2
fi
if ! command -v docker >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  log 'ERRO | Requer docker e python3 existentes no host'
  exit 2
fi
if ! docker compose version >/dev/null 2>&1; then
  log 'ERRO | Docker Compose indisponivel'
  exit 2
fi
CID=$(docker compose ps -q worker 2>/dev/null)
if [[ -z "$CID" ]]; then
  log 'ERRO | Container worker nao encontrado em execucao'
  exit 2
fi
IMAGE=$(docker inspect -f '{{.Config.Image}}' "$CID" 2>/dev/null || true)
if [[ -z "$IMAGE" ]]; then
  log 'ERRO | Imagem do worker nao identificada'
  exit 2
fi
log 'OK | Worker e imagem de runtime identificados, sem exibicao de dados de ambiente'

cat > "$PYFILE" <<'PY'
import os
import socket
import ssl
import sys
import time
import urllib.request

TARGETS = (
    ('hono_ip1', 'viroucase-ai-check-256ca8c9727794d0e61629de.felipe-themarkiron.workers.dev', '104.21.44.19', '/api/health'),
    ('hono_ip2', 'viroucase-ai-check-256ca8c9727794d0e61629de.felipe-themarkiron.workers.dev', '172.67.193.242', '/api/health'),
    ('fastapi_ip1', 'viroucase-ai-check-bbba376b77cc7d78321dad8d.felipe-themarkiron.workers.dev', '104.21.44.19', '/api/health'),
    ('cloudflare_control', 'api.cloudflare.com', '104.19.192.174', '/'),
)


def emit(*fields):
    print('|'.join(str(x) for x in fields), flush=True)


def error_name(exc):
    parts = []
    seen = set()
    obj = exc
    while obj is not None and id(obj) not in seen and len(parts) < 4:
        seen.add(id(obj))
        parts.append(type(obj).__name__ + (':errno=' + str(obj.errno) if isinstance(obj, OSError) and obj.errno is not None else ''))
        obj = obj.__cause__ or obj.__context__
    return '>'.join(parts)


def direct_probe(name, host, ip, path):
    emit('DIRECT_START', name, 'ip=' + ip, 'family=AF_INET', 'sni=' + host)
    sock = None
    stage = "TCP"
    started = time.monotonic()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((ip, 443))
        emit('TCP_OK', name, 'source_ip=' + str(sock.getsockname()[0]), 'duration_ms=' + str(round((time.monotonic()-started)*1000)))
        sock.settimeout(6.0)
        stage = 'TLS'
        context = ssl.create_default_context()
        tls = context.wrap_socket(sock, server_hostname=host)
        sock = None
        with tls:
            emit('TLS_OK', name, 'version=' + str(tls.version()), 'cipher=' + str(tls.cipher()[0]))
            stage = 'HTTP'
            request = 'GET ' + path + ' HTTP/1.1\r\nHost: ' + host + '\r\nUser-Agent: VirouCase-Diagnostic/1.0\r\nAccept: application/json\r\nConnection: close\r\n\r\n'
            tls.sendall(request.encode('ascii'))
            response = tls.recv(512)
            first_line = response.split(b'\r\n', 1)[0].decode('ascii', errors='replace')
            emit('HTTP_STATUS', name, first_line)
    except Exception as exc:
        emit('DIRECT_FAIL', name, 'stage=' + stage, 'error=' + error_name(exc), 'duration_ms=' + str(round((time.monotonic()-started)*1000)))
    finally:
        if sock is not None:
            sock.close()
        emit('DIRECT_DONE', name)


def automatic_probe(client, host):
    url = 'https://' + host + '/api/health'
    started = time.monotonic()
    emit('AUTO_START', client, 'url_path=/api/health')
    if client == 'urllib':
        try:
            with urllib.request.urlopen(url, timeout=7.0) as response:
                response.read(128)
                emit('AUTO_HTTP', client, 'status=' + str(response.status), 'duration_ms=' + str(round((time.monotonic()-started)*1000)))
        except Exception as exc:
            emit('AUTO_FAIL', client, 'error=' + error_name(exc), 'duration_ms=' + str(round((time.monotonic()-started)*1000)))
        return
    try:
        import httpx
    except ImportError:
        emit('AUTO_SKIP', client, 'reason=httpx_not_installed')
        return
    try:
        with httpx.Client(follow_redirects=False, trust_env=False, timeout=7.0) as client_obj:
            response = client_obj.get(url)
            emit('AUTO_HTTP', 'httpx_trust_env_false', 'status=' + str(response.status_code), 'duration_ms=' + str(round((time.monotonic()-started)*1000)))
    except Exception as exc:
        emit('AUTO_FAIL', 'httpx_trust_env_false', 'error=' + error_name(exc), 'duration_ms=' + str(round((time.monotonic()-started)*1000)))


def main():
    context = os.environ.get('VIRACASE_DIAG_CONTEXT', 'unspecified')
    emit('RUNTIME', 'context=' + context, 'python=' + sys.version.split()[0], 'openssl=' + ssl.OPENSSL_VERSION.replace(' ', '_'))
    try:
        emit('RUNTIME', 'netns=' + os.readlink('/proc/self/ns/net'))
    except OSError as exc:
        emit('RUNTIME', 'netns_unavailable=' + error_name(exc))
    emit('RUNTIME', 'proxy_keys_present=' + ','.join(sorted(k for k in os.environ if k.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'))))
    hostname = TARGETS[0][1]
    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        unique = []
        for item in infos:
            token = ('IPv4' if item[0] == socket.AF_INET else 'IPv6' if item[0] == socket.AF_INET6 else str(item[0])) + ':' + item[4][0]
            if token not in unique:
                unique.append(token)
        emit('DNS_AUTO_ORDER', ','.join(unique[:12]))
    except Exception as exc:
        emit('DNS_AUTO_FAIL', error_name(exc))
    for name, host, ip, path in TARGETS:
        direct_probe(name, host, ip, path)
    automatic_probe('urllib', hostname)
    automatic_probe('httpx', hostname)
    emit('CONTEXT_COMPLETE', context)


if __name__ == '__main__':
    main()
PY

run_section() {
  local label rc
  label=$1
  shift
  log "INICIO | $label"
  "$@" 2>&1 | tee -a "$REPORT"
  rc=${PIPESTATUS[0]}
  log "FIM | $label | codigo=$rc"
  return "$rc"
}

FAILED=0
log 'ETAPA 1/3 | Comparacao direta no Ubuntu com o mesmo codigo Python'
VIRACASE_DIAG_CONTEXT=ubuntu run_section ubuntu_python python3 -u "$PYFILE" || FAILED=1
log 'ETAPA 2/3 | Contêiner temporario com rede host e a mesma imagem do worker'
cat "$PYFILE" | docker run --rm -i --network host --read-only --cap-drop=ALL --security-opt=no-new-privileges --entrypoint python -e VIRACASE_DIAG_CONTEXT=docker_host "$IMAGE" -u - 2>&1 | tee -a "$REPORT"
RC=${PIPESTATUS[1]}
log "FIM | docker_host | codigo=$RC"
if (( RC != 0 )); then FAILED=1; fi
log 'ETAPA 3/3 | Worker atual, sem modificar ou reiniciar'
cat "$PYFILE" | docker compose exec -T -e VIRACASE_DIAG_CONTEXT=worker_bridge worker python -u - 2>&1 | tee -a "$REPORT"
RC=${PIPESTATUS[1]}
log "FIM | worker_bridge | codigo=$RC"
if (( RC != 0 )); then FAILED=1; fi
if (( FAILED != 0 )); then
  log 'COLETA_INCOMPLETA | Uma das execucoes falhou; revise o relatorio'
  exit 5
fi
log 'COLETA_COMPLETA | Todos os contextos executados; falhas de rede constam individualmente'
log "RELATORIO_FINAL | $REPORT"
