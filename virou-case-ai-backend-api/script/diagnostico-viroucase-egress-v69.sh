#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd "$SCRIPT_DIRECTORY/.." && pwd)"
cd "$PROJECT_DIRECTORY"

if [[ ! -f compose.yaml ]]; then
  printf '%s\n' 'ERRO: compose.yaml nao encontrado na raiz do projeto. Extraia o ZIP na raiz do backend.' >&2
  exit 2
fi

REPORT="$(mktemp "${TMPDIR:-/tmp}/viroucase-egress-v69-v2.XXXXXXXX.txt")"
STARTED_AT="$(date +%s)"
COLLECTION_FAILED=0
exec > >(tee -a "$REPORT") 2>&1

stamp() {
  local now
  now="$(date +%s)"
  printf '[%s][+%ss] %s\n' "$(date '+%H:%M:%S')" "$((now - STARTED_AT))" "$*"
}

finish() {
  local result="$1"
  stamp "FIM | status=$result | relatorio=$REPORT"
}
trap 'finish "$?"' EXIT

stamp 'INICIO | Diagnostico TCP/TLS/rota v2.0.0.69 - coletor v2 | somente leitura'
stamp "RELATORIO | $REPORT"
stamp 'Nenhum container sera criado, reiniciado ou modificado. Nenhuma publicacao sera executada.'

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  stamp 'ERRO | Docker Compose indisponivel neste terminal'
  exit 3
fi
if ! command -v timeout >/dev/null 2>&1; then
  stamp 'ERRO | comando timeout nao encontrado'
  exit 3
fi

stamp 'ETAPA 1/5 | Identificando os containers corretos'
declare -A IDS=()
for SERVICE in worker admin-api; do
  ID="$(timeout 12 docker compose ps -q "$SERVICE" 2>/dev/null || true)"
  ID="${ID%%$'\n'*}"
  if [[ ! "$ID" =~ ^[a-fA-F0-9]{12,64}$ ]]; then
    stamp "ERRO | container $SERVICE nao encontrado no Compose desta pasta; encerrando sem testes inconclusivos"
    exit 4
  fi
  IDS["$SERVICE"]="$ID"
  stamp "OK | $SERVICE encontrado"
  docker inspect --format 'CONTAINER|name={{.Name}}|state={{.State.Status}}|apparmor={{.AppArmorProfile}}{{range $name, $net := .NetworkSettings.Networks}}|network={{$name}}:ip={{$net.IPAddress}}:gateway={{$net.Gateway}}{{end}}' "$ID" || true
done

stamp 'ETAPA 2/5 | Consultando redes e rotas atuais'
for SERVICE in worker admin-api; do
  while IFS= read -r NETWORK; do
    [[ -n "$NETWORK" ]] || continue
    timeout 5 docker network inspect --format 'DOCKER_NETWORK|name={{.Name}}|driver={{.Driver}}|internal={{.Internal}}|ipv6={{.EnableIPv6}}{{range .IPAM.Config}}|subnet={{.Subnet}}|gateway={{.Gateway}}{{end}}' "$NETWORK" || true
  done < <(docker inspect --format '{{range $name, $net := .NetworkSettings.Networks}}{{println $name}}{{end}}' "${IDS[$SERVICE]}")
done
if command -v ip >/dev/null 2>&1; then
  for TARGET_IP in 104.21.44.19 172.67.193.242; do
    ROUTE="$(timeout 3 ip -4 route get "$TARGET_IP" 2>/dev/null || true)"
    stamp "HOST_ROUTE | target=$TARGET_IP | ${ROUTE:-indisponivel}"
  done
fi
if command -v sysctl >/dev/null 2>&1; then
  FORWARD="$(timeout 3 sysctl -n net.ipv4.ip_forward 2>/dev/null || true)"
  stamp "HOST_IP_FORWARD | ${FORWARD:-indisponivel}"
fi
if command -v iptables >/dev/null 2>&1; then
  if RULES="$(timeout 4 iptables -n -v -L DOCKER-USER 2>/dev/null | head -n 18)" && [[ -n "$RULES" ]]; then
    printf '%s\n' 'FIREWALL_DOCKER_USER_BEGIN' "$RULES" 'FIREWALL_DOCKER_USER_END'
  else
    stamp 'FIREWALL_DOCKER_USER | indisponivel sem permissao elevada; nenhuma permissao foi alterada'
  fi
fi

PYTHON_PROBE="$(cat <<'PY'
import errno
import os
import shutil
import socket
import ssl
import subprocess
import time

HOSTS = [
    ("worker_hono", "viroucase-ai-check-256ca8c9727794d0e61629de.felipe-themarkiron.workers.dev"),
    ("worker_fastapi", "viroucase-ai-check-bbba376b77cc7d78321dad8d.felipe-themarkiron.workers.dev"),
    ("control_api", "api.cloudflare.com"),
    ("control_site", "www.cloudflare.com"),
]


def emit(*items):
    print(" | ".join(str(item) for item in items), flush=True)


def describe_error(error):
    code = getattr(error, "errno", None)
    if isinstance(code, int):
        return f"{type(error).__name__}:errno={code}:{errno.errorcode.get(code, 'UNKNOWN')}"
    return type(error).__name__


def current_routes():
    try:
        with open("/proc/net/route", "r", encoding="ascii") as route_file:
            for row in route_file.read().splitlines()[1:]:
                fields = row.split()
                if len(fields) >= 4 and fields[1] == "00000000":
                    emit("DEFAULT_ROUTE_V4", f"interface={fields[0]}", f"gateway_hex={fields[2]}")
    except OSError as error:
        emit("DEFAULT_ROUTE_V4", describe_error(error))
    try:
        with open("/proc/net/ipv6_route", "r", encoding="ascii") as route_file:
            defaults = [row for row in route_file.read().splitlines() if row.split()[:2] == ["0" * 32, "00000000"]]
            emit("DEFAULT_ROUTE_V6", f"count={len(defaults)}")
    except OSError as error:
        emit("DEFAULT_ROUTE_V6", describe_error(error))


def ip_route(address):
    if not shutil.which("ip"):
        emit("ROUTE_GET", f"dest={address}", "tool=unavailable")
        return
    try:
        result = subprocess.run(["ip", "-4", "route", "get", address], capture_output=True, text=True, timeout=2, check=False)
        if result.returncode == 0:
            line = result.stdout.strip().splitlines()[0][:240]
            emit("ROUTE_GET", f"dest={address}", line)
        else:
            emit("ROUTE_GET", f"dest={address}", f"returncode={result.returncode}")
    except (OSError, subprocess.TimeoutExpired) as error:
        emit("ROUTE_GET", f"dest={address}", describe_error(error))


def check_socket(name, host, family, address):
    started = time.monotonic()
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError as error:
        state = "UNSUPPORTED" if error.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT) else "FAIL"
        emit("TCP", f"target={name}", f"family={family.name}", f"address={address}", f"state={state}", describe_error(error))
        return
    try:
        sock.settimeout(3)
        try:
            sock.connect((address, 443))
        except OSError as error:
            emit("TCP", f"target={name}", f"family={family.name}", f"address={address}", "state=FAIL", describe_error(error), f"duration_ms={int((time.monotonic() - started) * 1000)}")
            return
        emit("TCP", f"target={name}", f"family={family.name}", f"address={address}", "state=OK", f"duration_ms={int((time.monotonic() - started) * 1000)}")
        try:
            sock.settimeout(3)
            with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as secured:
                emit("TLS", f"target={name}", f"family={family.name}", f"address={address}", "state=OK", f"protocol={secured.version()}")
        except OSError as error:
            emit("TLS", f"target={name}", f"family={family.name}", f"address={address}", "state=FAIL", describe_error(error))
    finally:
        sock.close()


def check_http(name, host):
    try:
        import httpx
    except ImportError:
        httpx = None
    started = time.monotonic()
    if httpx is None:
        import urllib.error
        import urllib.request
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request("https://" + host + "/", headers={"Accept": "text/html"}, method="GET")
            with opener.open(request, timeout=5) as response:
                emit("HTTP", f"target={name}", "state=RESPONSE", f"status={response.status}", f"duration_ms={int((time.monotonic() - started) * 1000)}", "client=urllib")
        except urllib.error.HTTPError as error:
            emit("HTTP", f"target={name}", "state=RESPONSE", f"status={error.code}", f"duration_ms={int((time.monotonic() - started) * 1000)}", "client=urllib")
        except OSError as error:
            emit("HTTP", f"target={name}", "state=FAIL", describe_error(error), f"duration_ms={int((time.monotonic() - started) * 1000)}", "client=urllib")
        return
    try:
        with httpx.Client(timeout=httpx.Timeout(5, connect=3), trust_env=False, follow_redirects=False) as client:
            with client.stream("GET", "https://" + host + "/", headers={"Accept": "text/html"}) as response:
                emit("HTTP", f"target={name}", "state=RESPONSE", f"status={response.status_code}", f"duration_ms={int((time.monotonic() - started) * 1000)}")
    except Exception as error:
        chain = [describe_error(error)]
        visited = {id(error)}
        cause = error.__cause__ or error.__context__
        while cause is not None and len(chain) < 5 and id(cause) not in visited:
            visited.add(id(cause))
            chain.append(describe_error(cause))
            cause = cause.__cause__ or cause.__context__
        emit("HTTP", f"target={name}", "state=FAIL", f"chain={'>'.join(chain)}", f"duration_ms={int((time.monotonic() - started) * 1000)}")


emit("PYTHON_RUNTIME", f"context={os.getenv('VIRA_DIAG_CONTEXT', 'unknown')}")
current_routes()
for name, host in HOSTS:
    emit("TARGET_START", f"name={name}", f"hostname={host}")
    ipv4 = []
    ipv6 = []
    try:
        ipv4 = sorted({entry[4][0] for entry in socket.getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)})
        emit("DNS_A", f"target={name}", f"count={len(ipv4)}", ",".join(ipv4[:4]))
    except OSError as error:
        emit("DNS_A", f"target={name}", "state=FAIL", describe_error(error))
    try:
        ipv6 = sorted({entry[4][0] for entry in socket.getaddrinfo(host, 443, family=socket.AF_INET6, type=socket.SOCK_STREAM)})
        emit("DNS_AAAA", f"target={name}", f"count={len(ipv6)}")
    except OSError as error:
        emit("DNS_AAAA", f"target={name}", "state=FAIL", describe_error(error))
    if ipv4:
        ip_route(ipv4[0])
    for address in ipv4[:2]:
        check_socket(name, host, socket.AF_INET, address)
    if ipv6:
        if os.path.exists("/proc/net/ipv6_route"):
            check_socket(name, host, socket.AF_INET6, ipv6[0])
        else:
            emit("IPV6", f"target={name}", "state=SKIPPED", "reason=ipv6_route_unavailable")
    check_http(name, host)
    emit("TARGET_DONE", f"name={name}")
PY
)"

stamp 'ETAPA 3/5 | Testando primeiro os Workers publicados no host Ubuntu (IPv4 prioritario)'
if command -v python3 >/dev/null 2>&1; then
  if VIRA_DIAG_CONTEXT=host timeout 90 python3 -u - <<< "$PYTHON_PROBE"; then
    stamp 'OK | Testes de host encerrados'
  else
    stamp 'AVISO | Host: coleta terminou com falha ou timeout, sem alterar o ambiente'
    COLLECTION_FAILED=1
  fi
else
  stamp 'AVISO | Python3 nao encontrado no host; a coleta esta incompleta'
  COLLECTION_FAILED=1
fi

stamp 'ETAPA 4/5 | Testando os Workers publicados no worker e admin-api (IPv4 prioritario)'
for SERVICE in worker admin-api; do
  stamp "INICIO | $SERVICE | DNS, TCP, TLS e HTTP em IPv4/IPv6 | limite=90s"
  if printf '%s\n' "$PYTHON_PROBE" | timeout 90 docker exec -i -e "VIRA_DIAG_CONTEXT=$SERVICE" "${IDS[$SERVICE]}" python -u -; then
    stamp "OK | $SERVICE: teste concluido"
  else
    RC="$?"
    stamp "AVISO | $SERVICE: falha na coleta ou timeout | exit_code=$RC"
    COLLECTION_FAILED=1
  fi
done

stamp 'ETAPA 5/5 | Consolidacao'
stamp 'INTERPRETACAO | TCP falha nos IPs dos Workers e funciona nos controles => investigar filtragem, NAT e roteamento para os IPs de destino.'
stamp 'INTERPRETACAO | TCP funciona e TLS falha => investigar SNI, handshake TLS ou bloqueio seletivo.'
stamp 'INTERPRETACAO | TCP e TLS funcionam e HTTP falha => investigar cliente HTTP ou protocolo.'
stamp 'INTERPRETACAO | Host funciona e ambos containers falham => investigar infraestrutura compartilhada dos containers, nao JSON.'
if (( COLLECTION_FAILED )); then
  stamp 'COLETA_INCOMPLETA | Um ou mais testes falharam por erro do coletor ou timeout; nao concluir causa-raiz com estes resultados isolados.'
  exit 5
fi
stamp 'COLETA_COMPLETA | Todos os contextos foram inspecionados. Falhas de TCP/TLS/HTTP observadas sao resultados diagnosticos.'
stamp 'CONCLUIDO | Entregue somente o relatorio indicado; nenhuma verificacao Cloudflare foi disparada.' 
