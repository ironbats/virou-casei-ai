#!/usr/bin/env bash
set -uo pipefail
umask 077

START=$(date +%s)
REPORT=$(mktemp /tmp/viroucase-clienthello-v69.XXXXXXXX.txt)
PROBE=$(mktemp /tmp/viroucase-clienthello-v69.XXXXXXXX.py)
trap 'rm -f "$PROBE"' EXIT

say() {
  local now
  now=$(date +%s)
  printf '[%s][+%ss] %s\n' "$(date +%H:%M:%S)" "$((now-START))" "$*" | tee -a "$REPORT"
}

say 'INICIO | Comparacao controlada TLS 1.3, X25519 e TLS 1.2 | somente leitura'
say "RELATORIO | $REPORT"
say 'SEM ALTERACOES | Nao edita backend, Docker Compose, firewall, AppArmor, roteador ou Cloudflare'

if [[ ! -f compose.yaml && ! -f docker-compose.yml && ! -f docker-compose.yaml ]]; then
  say 'BLOQUEADO | Execute na raiz do backend com Docker Compose'
  exit 2
fi
if ! command -v docker >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  say 'BLOQUEADO | Requer docker e python3 presentes no host'
  exit 2
fi
if ! docker compose version >/dev/null 2>&1; then
  say 'BLOQUEADO | docker compose indisponivel'
  exit 2
fi
CID=$(docker compose ps -q worker 2>/dev/null)
if [[ -z "$CID" ]]; then
  say 'BLOQUEADO | worker nao encontrado em execucao'
  exit 2
fi
IMAGE=$(docker inspect -f '{{.Config.Image}}' "$CID" 2>/dev/null || true)
if [[ -z "$IMAGE" ]]; then
  say 'BLOQUEADO | imagem do worker nao identificada'
  exit 2
fi

cat > "$PROBE" <<'PY'
import os
import socket
import ssl
import sys
import time

H = 'viroucase-ai-check-256ca8c9727794d0e61629de.felipe-themarkiron.workers.dev'
F = 'viroucase-ai-check-bbba376b77cc7d78321dad8d.felipe-themarkiron.workers.dev'
A = 'api.cloudflare.com'
TARGETS = (("hono", H, "104.21.44.19", "/api/health"), ("fastapi", F, "104.21.44.19", "/api/health"), ("control", A, "104.19.192.174", "/"))


def emit(*fields):
    print('|'.join(str(v) for v in fields), flush=True)


def make_context(mode):
    ctx = ssl.create_default_context()
    if mode == 'tls13_default':
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    elif mode == 'tls13_x25519':
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.maximum_version = ssl.TLSVersion.TLSv1_3
        ctx.set_ecdh_curve('X25519')
    elif mode == 'tls12_only':
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    else:
        raise ValueError('invalid mode')
    return ctx


def client_hello_size(ctx, host):
    inbound = ssl.MemoryBIO()
    outbound = ssl.MemoryBIO()
    instance = ctx.wrap_bio(inbound, outbound, server_side=False, server_hostname=host)
    try:
        instance.do_handshake()
    except ssl.SSLWantReadError:
        pass
    payload = outbound.read()
    return len(payload)


def check(name, host, address, path, mode):
    started = time.monotonic()
    sock = None
    stage = 'CTX'
    emit('PROBE_START', 'target=' + name, 'mode=' + mode, 'ip=' + address)
    try:
        context = make_context(mode)
        hello_size = client_hello_size(context, host)
        emit('CLIENT_HELLO', 'target=' + name, 'mode=' + mode, 'bytes=' + str(hello_size))
        stage = 'TCP'
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(4.0)
        sock.connect((address, 443))
        emit('TCP_OK', 'target=' + name, 'mode=' + mode, 'source=' + sock.getsockname()[0], 'elapsed_ms=' + str(round((time.monotonic()-started)*1000)))
        stage = 'TLS'
        sock.settimeout(5.0)
        tls = context.wrap_socket(sock, server_hostname=host)
        sock = None
        with tls:
            emit('TLS_OK', 'target=' + name, 'mode=' + mode, 'version=' + str(tls.version()))
            stage = 'HTTP'
            request = f'GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nAccept: application/json\r\nUser-Agent: VirouCase-ClientHello-Diagnostic/1\r\n\r\n'
            tls.sendall(request.encode('ascii'))
            first = tls.recv(512).split(b'\r\n', 1)[0].decode('ascii', errors='replace')
            emit('HTTP_RESULT', 'target=' + name, 'mode=' + mode, 'status=' + first)
    except Exception as exc:
        emit('PROBE_FAIL', 'target=' + name, 'mode=' + mode, 'stage=' + stage, 'class=' + type(exc).__name__, 'errno=' + str(getattr(exc, 'errno', None)), 'elapsed_ms=' + str(round((time.monotonic()-started)*1000)))
    finally:
        if sock is not None:
            sock.close()
    emit('PROBE_END', 'target=' + name, 'mode=' + mode)


def main():
    label = os.environ.get('VIRACASE_DIAG_CONTEXT', 'unknown')
    emit('RUNTIME', 'context=' + label, 'python=' + sys.version.split()[0], 'openssl=' + ssl.OPENSSL_VERSION.replace(' ', '_'))
    emit('RUNTIME', 'netns=' + os.readlink('/proc/self/ns/net'))
    if label == 'ubuntu':
        modes = ('tls13_default', 'tls13_x25519')
    elif label == 'docker_host':
        modes = ('tls13_default', 'tls13_x25519', 'tls12_only')
    else:
        modes = ('tls13_x25519',)
    for name, host, ip, path in TARGETS:
        for mode in modes if name != 'control' else (modes[0],):
            check(name, host, ip, path, mode)
    emit('CONTEXT_COMPLETE', label)


if __name__ == '__main__':
    main()
PY

FAILED=0
say 'ETAPA 1/3 | Ubuntu: TLS 1.3 padrao versus X25519, mesmos IPs IPv4'
VIRACASE_DIAG_CONTEXT=ubuntu python3 -u "$PROBE" 2>&1 | tee -a "$REPORT"
RC=${PIPESTATUS[0]}
say "ETAPA_1_RESULTADO | rc=$RC"
if (( RC != 0 )); then FAILED=1; fi

say 'ETAPA 2/3 | Docker com rede host: TLS 1.3 padrao, TLS 1.3 X25519 e TLS 1.2'
docker run --rm -i --network host --read-only --cap-drop=ALL --security-opt=no-new-privileges --entrypoint python -e VIRACASE_DIAG_CONTEXT=docker_host "$IMAGE" -u - < "$PROBE" 2>&1 | tee -a "$REPORT"
RC=${PIPESTATUS[0]}
say "ETAPA_2_RESULTADO | rc=$RC"
if (( RC != 0 )); then FAILED=1; fi

say 'ETAPA 3/3 | Worker atual: testar se X25519 consegue sequer abrir TCP'
docker compose exec -T -e VIRACASE_DIAG_CONTEXT=worker_bridge worker python -u - < "$PROBE" 2>&1 | tee -a "$REPORT"
RC=${PIPESTATUS[0]}
say "ETAPA_3_RESULTADO | rc=$RC"
if (( RC != 0 )); then FAILED=1; fi

say 'RESULTADO | Compare TLS13_PADRAO versus TLS13_X25519 no docker_host: se o segundo funcionar, a diferenca de ClientHello interfere no caminho TLS'
say 'RESULTADO | Se worker_bridge falhar em TCP mesmo com X25519, essa falha nao pode ser corrigida alterando o TLS'
say "RELATORIO_FINAL | $REPORT"
if (( FAILED != 0 )); then
  say 'COLETA_INCOMPLETA | Pelo menos um contexto nao executou corretamente'
  exit 5
fi
say 'COLETA_CONCLUIDA | Todas as etapas executadas, resultados individuais no relatorio'
