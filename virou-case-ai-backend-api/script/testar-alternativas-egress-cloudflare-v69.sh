#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROBE="$PROJECT_ROOT/script/alternativas-egress-cloudflare-v69.py"
REPORT="$(mktemp /tmp/viroucase-egress-alternativas-v69.XXXXXXXX.txt)"
WORKDIR="$(mktemp -d /tmp/viroucase-egress-matriz-v69.XXXXXXXX)"
RUN_ID="$(basename "$WORKDIR" | tr -cd 'a-zA-Z0-9')"
START_TS="$(date +%s)"
CREATED_NETWORKS=()
CREATED_CONTAINERS=()
RELAY_PID=""
PULL_PYTHON310=0
QUICK=0
WORKER=""
ADMIN=""
IMAGE=""
RUN_USER=""
OUTBOUND=""
GATEWAY=""
WORKER_IP=""
ADMIN_IP=""
IP_ARGS=()

cleanup() {
  local net label
  set +e
  if [[ -n "$RELAY_PID" ]]; then
    kill "$RELAY_PID" >/dev/null 2>&1 || true
    wait "$RELAY_PID" >/dev/null 2>&1 || true
  fi
  local container
  for container in "${CREATED_CONTAINERS[@]}"; do
    label="$(docker container inspect -f '{{index .Config.Labels "viroucase.diagnostics"}}' "$container" 2>/dev/null || true)"
    if [[ "$label" == "$RUN_ID" ]]; then
      docker rm -f "$container" >>"$REPORT" 2>&1 || printf 'CLEANUP_WARN|container=%s|remove_failed\n' "$container" | tee -a "$REPORT"
    fi
  done
  for net in "${CREATED_NETWORKS[@]}"; do
    label="$(docker network inspect -f '{{index .Labels "viroucase.diagnostics"}}' "$net" 2>/dev/null || true)"
    if [[ "$label" == "$RUN_ID" ]]; then
      docker network rm "$net" >>"$REPORT" 2>&1 || printf 'CLEANUP_WARN|network=%s|remove_failed\n' "$net" | tee -a "$REPORT"
    fi
  done
  rm -rf -- "$WORKDIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

log() {
  printf '[%s][+%ss] %s\n' "$(date +%H:%M:%S)" "$(( $(date +%s) - START_TS ))" "$*" | tee -a "$REPORT"
}

execute() {
  local rc
  set +e
  "$@" 2>&1 | tee -a "$REPORT"
  rc=${PIPESTATUS[0]}
  set -e
  return "$rc"
}

result_error() {
  printf 'CASE_RESULT|case=%s|status=%s|reason=%s\n' "$1" "$2" "$3" | tee -a "$REPORT"
}

find_service() {
  local service="$1"
  local result=""
  result="$(docker compose -f "$PROJECT_ROOT/compose.yaml" -f "$PROJECT_ROOT/compose.override.yaml" ps -q "$service" 2>/dev/null || true)"
  if [[ -z "$result" ]]; then
    result="$(docker ps -q --filter "label=com.docker.compose.service=$service" | head -n 1)"
  fi
  if [[ -z "$result" ]] || [[ "$(docker inspect -f '{{.State.Running}}' "$result" 2>/dev/null || true)" != "true" ]]; then
    return 1
  fi
  printf '%s\n' "$result"
}

run_temp() {
  local case_name="$1"
  local network="$2"
  local image="$3"
  shift 3
  local name="vca-diag-${RUN_ID: -9}-${case_name//_/-}"
  CREATED_CONTAINERS+=("$name")
  local -a options=(
    run --pull=never --rm --name "$name" --label "viroucase.diagnostics=$RUN_ID" --network "$network"
    --read-only --cap-drop=ALL --pids-limit 64 --security-opt no-new-privileges
    --user "$RUN_USER" --mount "type=bind,src=$PROBE,dst=/vca-probe.py,readonly"
    --entrypoint python
  )
  while [[ $# -gt 0 ]]; do
    options+=("$1")
    shift
  done
  options+=("$image" /vca-probe.py probe --case "$case_name" "${IP_ARGS[@]}")
  log "TESTE | $case_name | rede=$network | imagem=$image"
  if ! execute docker "${options[@]}"; then
    result_error "$case_name" "ERROR" "docker_run_failed"
  fi
}

make_network() {
  local mtu="$1"
  local name="vca-diag-${RUN_ID: -9}-mtu${mtu}"
  log "CRIANDO REDE TEMPORARIA | mtu=$mtu | nome=$name"
  if docker network create -d bridge --label "viroucase.diagnostics=$RUN_ID" -o "com.docker.network.driver.mtu=$mtu" "$name" >>"$REPORT" 2>&1; then
    CREATED_NETWORKS+=("$name")
    run_temp "bridge_mtu_${mtu}" "$name" "$IMAGE"
  else
    result_error "bridge_mtu_${mtu}" "SKIP" "docker_network_create_failed"
  fi
}

start_relay() {
  local ready="$WORKDIR/relay-port"
  log "TESTE | relay_ubuntu | relay HTTPS restrito, somente GET de health/revision"
  python3 "$PROBE" relay-server --bind "$GATEWAY" --allow-ip "$WORKER_IP" --allow-ip "$ADMIN_IP" --ready "$ready" --lifetime 100 >"$WORKDIR/relay.log" 2>&1 &
  RELAY_PID=$!
  local attempt
  for attempt in $(seq 1 30); do
    if [[ -s "$ready" ]]; then
      break
    fi
    if ! kill -0 "$RELAY_PID" 2>/dev/null; then
      break
    fi
    sleep 0.15
  done
  if [[ ! -s "$ready" ]]; then
    result_error "relay_worker" "SKIP" "host_relay_unavailable"
    result_error "relay_admin" "SKIP" "host_relay_unavailable"
    log "INFO | relay nao iniciado no gateway; nenhuma regra foi alterada"
    return 0
  fi
  local port
  port="$(cat "$ready")"
  if [[ ! "$port" =~ ^[0-9]+$ ]]; then
    result_error "relay_worker" "ERROR" "invalid_relay_port"
    return 0
  fi
  if [[ -n "$WORKER" ]]; then
    log "TESTE | relay_worker | worker atual -> gateway -> HTTPS nativo Ubuntu"
    if ! execute docker exec -i "$WORKER" python - relay-probe --case relay_worker --relay-gateway "$GATEWAY" --relay-port "$port" < "$PROBE"; then
      result_error "relay_worker" "ERROR" "docker_exec_failed"
    fi
  fi
  if [[ -n "$ADMIN" ]]; then
    log "TESTE | relay_admin | admin-api atual -> gateway -> HTTPS nativo Ubuntu"
    if ! execute docker exec -i "$ADMIN" python - relay-probe --case relay_admin --relay-gateway "$GATEWAY" --relay-port "$port" < "$PROBE"; then
      result_error "relay_admin" "ERROR" "docker_exec_failed"
    fi
  fi
  if [[ -s "$WORKDIR/relay.log" ]]; then
    log "RELAY_HOST_LOG | pacotes e HTTP upstream do verificador nativo"
    sed -n '1,100p' "$WORKDIR/relay.log" | sed 's/^/RELAY_HOST|/' | tee -a "$REPORT"
  fi
  kill "$RELAY_PID" >/dev/null 2>&1 || true
  wait "$RELAY_PID" >/dev/null 2>&1 || true
  RELAY_PID=""
}

for arg in "$@"; do
  case "$arg" in
    --pull-python310) PULL_PYTHON310=1 ;;
    --quick) QUICK=1 ;;
    *) printf 'Opcao nao reconhecida: %s\nUso: bash script/testar-alternativas-egress-cloudflare-v69.sh [--quick] [--pull-python310]\n' "$arg" >&2; exit 2 ;;
  esac
done

log "INICIO | 11+ cenarios de alternativas de egress | v2.0.0.69"
log "RELATORIO | $REPORT"
log "SEGURANCA | nao altera containers existentes, politicas manuais de firewall, AppArmor, sysctl, roteador ou Cloudflare"
log "TEMPORARIOS | docker run --rm; redes MTU criadas com label exclusivo e removidas ao fim; o Docker cria NAT transitorio para elas"
log "ESCOPOS | nenhum token, segredo, senha ou arquivo .env sera compartilhado com testes"
if [[ ! -s "$PROBE" ]]; then
  log "ERRO | helper Python nao encontrado em $PROBE"
  exit 2
fi
if ! command -v docker >/dev/null 2>&1 || ! docker info --format '{{.ServerVersion}}' >/dev/null 2>&1; then
  log "ERRO | Docker Engine indisponivel para este usuario"
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  log "ERRO | Python 3 no Ubuntu indisponivel"
  exit 2
fi
WORKER="$(find_service worker || true)"
ADMIN="$(find_service admin-api || true)"
if [[ -z "$WORKER" ]]; then
  log "ERRO | worker do projeto nao encontrado como container em execucao"
  exit 3
fi
IMAGE="$(docker inspect -f '{{.Image}}' "$WORKER")"
RUN_USER="$(docker inspect -f '{{.Config.User}}' "$WORKER")"
RUN_USER="${RUN_USER:-65534:65534}"
PROFILE="$(docker inspect -f '{{.AppArmorProfile}}' "$WORKER" 2>/dev/null || true)"
OUTBOUND="$(docker inspect -f '{{range $name, $data := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$WORKER" | grep 'outbound' | head -n 1 || true)"
if [[ -z "$OUTBOUND" ]]; then
  log "ERRO | rede outbound do worker nao encontrada"
  exit 3
fi
GATEWAY="$(docker network inspect -f '{{range .IPAM.Config}}{{println .Gateway}}{{end}}' "$OUTBOUND" | head -n 1)"
WORKER_IP="$(docker inspect -f "{{(index .NetworkSettings.Networks \"$OUTBOUND\").IPAddress}}" "$WORKER")"
if [[ -n "$ADMIN" ]]; then
  ADMIN_IP="$(docker inspect -f "{{(index .NetworkSettings.Networks \"$OUTBOUND\").IPAddress}}" "$ADMIN" 2>/dev/null || true)"
fi
ADMIN_IP="${ADMIN_IP:-$WORKER_IP}"

mapfile -t IPS < <(python3 - <<'PY'
import socket
hosts = (
    "viroucase-ai-check-256ca8c9727794d0e61629de.felipe-themarkiron.workers.dev",
    "viroucase-ai-check-bbba376b77cc7d78321dad8d.felipe-themarkiron.workers.dev",
)
addresses = []
for host in hosts:
    try:
        current = [x[4][0] for x in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)]
    except OSError:
        current = []
    for ip in current[:2]:
        if ip not in addresses:
            addresses.append(ip)
for address in addresses:
    print(address)
PY
)
if [[ ${#IPS[@]} -eq 0 ]]; then
  log "ERRO | resolucao DNS IPv4 dos Workers falhou no host"
  exit 4
fi
if (( QUICK == 1 )); then
  IPS=("${IPS[0]}")
fi
for ip in "${IPS[@]}"; do
  IP_ARGS+=(--ip "$ip")
done
log "AMBIENTE | worker=$WORKER | admin=${ADMIN:-indisponivel} | image_id=${IMAGE:0:20} | apparmor=$PROFILE"
log "REDE | outbound=$OUTBOUND | gateway=$GATEWAY | origem_worker=$WORKER_IP | origem_admin=$ADMIN_IP"
log "DNS_FIXADO | workers_ipv4=$(IFS=,; echo "${IPS[*]}")"

log "ETAPA 1/10 | Ubuntu nativo, controle HTTP/JSON publico"
if ! execute python3 "$PROBE" probe --case ubuntu "${IP_ARGS[@]}"; then
  result_error ubuntu ERROR host_python_failed
fi

log "ETAPA 2/10 | Worker de producao, sem reiniciar ou modificar"
if ! execute docker exec -i "$WORKER" python - probe --case worker_atual "${IP_ARGS[@]}" < "$PROBE"; then
  result_error worker_atual ERROR docker_exec_failed
fi

log "ETAPA 3/10 | Container sem credenciais na mesma rede outbound"
run_temp "outbound_nova_instancia" "$OUTBOUND" "$IMAGE"

log "ETAPA 4/10 | Container com as mesmas restricoes AppArmor e seccomp declaradas no projeto"
if [[ -n "$PROFILE" && "$PROFILE" != "unconfined" && -r "$PROJECT_ROOT/config/publication-seccomp.json" ]]; then
  run_temp "outbound_perfil_publicacao" "$OUTBOUND" "$IMAGE" --security-opt "apparmor=$PROFILE" --security-opt "seccomp=$PROJECT_ROOT/config/publication-seccomp.json"
else
  result_error outbound_perfil_publicacao SKIP policy_file_or_apparmor_missing
fi

log "ETAPA 5/10 | Docker bridge padrao docker0"
run_temp "docker0" "bridge" "$IMAGE"

log "ETAPA 6/10 | Rede host, sem NAT Docker"
run_temp "docker_host" "host" "$IMAGE"

log "ETAPA 7/10 | Bridges temporarias com MTUs diferentes"
make_network 1500
make_network 1400
make_network 1200

log "ETAPA 8/10 | Runtime Python 3.10 em container separado"
if (( PULL_PYTHON310 == 1 )) && ! docker image inspect python:3.10-slim-bookworm >/dev/null 2>&1; then
  log "DOWNLOAD EXPLICITO | imagem python:3.10-slim-bookworm ausente; solicitado pull pelo parametro --pull-python310"
  if ! execute docker pull python:3.10-slim-bookworm; then
    log "AVISO | docker pull falhou; tentativa com cache local"
  fi
fi
if docker image inspect python:3.10-slim-bookworm >/dev/null 2>&1; then
  run_temp "python310_host" "host" "python:3.10-slim-bookworm"
  run_temp "python310_outbound" "$OUTBOUND" "python:3.10-slim-bookworm"
else
  result_error python310_host SKIP image_not_cached_use_pull_python310
  result_error python310_outbound SKIP image_not_cached_use_pull_python310
fi

log "ETAPA 9/10 | Relay experimental de validacao pelo Python nativo no Ubuntu"
if [[ -n "$GATEWAY" && -n "$WORKER_IP" ]]; then
  start_relay
else
  result_error relay_worker SKIP gateway_or_source_missing
  result_error relay_admin SKIP gateway_or_source_missing
fi

log "ETAPA 10/10 | Consolidando alternativas e recomendacoes"
log "CRITERIO | PASS exige HTTP 200 com JSON valido em health e revision para ambos os Workers e todos os IPs testados"
log "CRITERIO | PASS_INDIRECT significa relay via Ubuntu funcionou; nao equivale a HTTPS direto pelo Docker"
log "RESULTADOS | Inicio"
grep '^CASE_RESULT|' "$REPORT" || true
FINAL_CODE=21
if grep -Eq '^CASE_RESULT\|case=(worker_atual|outbound_nova_instancia|outbound_perfil_publicacao|docker0|docker_host|bridge_mtu_(1500|1400|1200)|python310_(host|outbound))\|status=PASS\|' "$REPORT"; then
  FINAL_CODE=0
  log "DESFECHO | existe alternativa de egress DIRETO aprovada em teste; nao foi aplicada a producao"
elif grep -q '^CASE_RESULT|case=.*|status=PASS_INDIRECT|' "$REPORT"; then
  FINAL_CODE=20
  log "DESFECHO | somente relay INDIRETO pode ter funcionado; nao declarar egress direto restaurado"
else
  log "DESFECHO | nenhum caminho alternativo comprovado neste host; nenhuma alteracao permanente aplicada"
fi
log "OBSERVACAO | resultado Ubuntu e relay sao referencias, nao solucao Docker direta"
log "SKIP | macvlan/ipvlan exigem IP livre e configuracao de LAN; nao criar automaticamente em rede corporativa/domestica"
log "SKIP | docker daemon.json, DOCKER-USER, UFW, seccomp=unconfined e privileged nao serao alterados"
log "RELATORIO_FINAL | $REPORT"
log "FIM | codigo=$FINAL_CODE | 0=alternativa_direta_aprovada; 20=apenas_indireta; 21=nenhuma_aprovada"
log "LIMPEZA | redes e containers temporarios serao removidos no encerramento"
exit "$FINAL_CODE"
