#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROBE="$ROOT/script/validar-ca-egress-host-v69.py"
REPORT="$(mktemp /tmp/viroucase-ca-egress-v69.XXXXXXXX.txt)"
T0="$(date +%s)"

log() {
  printf '[%s][+%ss] %s\n' "$(date +%H:%M:%S)" "$(( $(date +%s) - T0 ))" "$*" | tee -a "$REPORT"
}

execute() {
  local rc
  set +e
  "$@" 2>&1 | tee -a "$REPORT"
  rc=${PIPESTATUS[0]}
  set -e
  return "$rc"
}

case_error() {
  printf 'CASE_RESULT|case=%s|status=ERROR|reason=%s\n' "$1" "$2" | tee -a "$REPORT"
}

log "INICIO | Comprovar se a cadeia CA do Ubuntu permite HTTPS validado no Docker host"
log "RELATORIO | $REPORT"
log "POLITICA | diagnostico: nenhuma alteracao em container existente, Docker Compose, firewall ou Cloudflare"

if [[ ! -s "$PROBE" ]]; then
  log "ERRO | helper Python ausente"
  exit 2
fi
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  log "ERRO | Docker indisponivel"
  exit 2
fi

WORKER="$(docker compose -f "$ROOT/compose.yaml" -f "$ROOT/compose.override.yaml" ps -q worker 2>/dev/null || true)"
if [[ -z "$WORKER" ]]; then
  WORKER="$(docker ps -q --filter 'label=com.docker.compose.service=worker' | head -n 1)"
fi
if [[ -z "$WORKER" ]]; then
  log "ERRO | worker nao encontrado"
  exit 3
fi
IMAGE="$(docker inspect -f '{{.Image}}' "$WORKER")"
RUN_USER="$(docker inspect -f '{{.Config.User}}' "$WORKER")"
RUN_USER="${RUN_USER:-65534:65534}"
OUTBOUND="$(docker inspect -f '{{range $name, $data := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$WORKER" | grep 'outbound' | head -n 1 || true)"
if [[ -z "$OUTBOUND" ]]; then
  log "ERRO | worker sem outbound"
  exit 3
fi
CA="$(python3 - <<'PY'
import os, ssl
for candidate in (ssl.get_default_verify_paths().cafile, '/etc/ssl/certs/ca-certificates.crt'):
    if candidate and os.path.isfile(candidate):
        print(os.path.realpath(candidate))
        break
PY
)"
if [[ -z "$CA" || ! -r "$CA" ]]; then
  log "ERRO | bundle de certificados do Ubuntu nao encontrado"
  exit 4
fi

mapfile -t IPS < <(python3 - <<'PY'
import socket
hosts = ('viroucase-ai-check-256ca8c9727794d0e61629de.felipe-themarkiron.workers.dev', 'viroucase-ai-check-bbba376b77cc7d78321dad8d.felipe-themarkiron.workers.dev')
seen = []
for host in hosts:
    for item in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM):
        if item[4][0] not in seen:
            seen.append(item[4][0])
for ip in seen[:2]:
    print(ip)
PY
)
if (( ${#IPS[@]} == 0 )); then
  log "ERRO | DNS nao resolveu IPv4 dos Workers"
  exit 4
fi
IP_ARGS=()
for ip in "${IPS[@]}"; do
  IP_ARGS+=(--ip "$ip")
done

log "BASELINE | imagem=${IMAGE:0:20} | rede=$OUTBOUND | IPv4=$(IFS=,; echo "${IPS[*]}")"
log "BUNDLE | origem Ubuntu | arquivo=$CA | sem transferencia de credenciais"
log "ETAPA 1/5 | Ubuntu nativo, TLS verificado"
execute python3 "$PROBE" --case ubuntu "${IP_ARGS[@]}" || case_error ubuntu python_failed

run_case() {
  local name="$1"
  local network="$2"
  local image="$3"
  local mode="$4"
  local -a cmd=(docker run --pull=never --rm --network "$network" --read-only --cap-drop=ALL --pids-limit 64 --security-opt no-new-privileges --user "$RUN_USER" --mount "type=bind,src=$PROBE,dst=/vca-probe.py,readonly")
  if [[ "$mode" == host_ca ]]; then
    cmd+=(--mount "type=bind,src=$CA,dst=/vca-ca-host.pem,readonly" --env SSL_CERT_FILE=/vca-ca-host.pem)
  fi
  cmd+=(--entrypoint python "$image" /vca-probe.py --case "$name" "${IP_ARGS[@]}")
  log "TESTE | case=$name | network=$network | trust=$mode"
  execute "${cmd[@]}" || case_error "$name" docker_run_failed
}

log "ETAPA 2/5 | Docker host, certificados originais da imagem"
run_case docker_host_image_ca host "$IMAGE" image_ca
log "ETAPA 3/5 | Docker host, certificados do Ubuntu montados somente leitura"
run_case docker_host_ubuntu_ca host "$IMAGE" host_ca
log "ETAPA 4/5 | Docker bridge atual, certificados do Ubuntu montados somente leitura"
run_case docker_bridge_ubuntu_ca "$OUTBOUND" "$IMAGE" host_ca
log "ETAPA 5/5 | Python 3.10 com rede host, bundle Ubuntu (caso imagem disponivel)"
if docker image inspect python:3.10-slim-bookworm >/dev/null 2>&1; then
  run_case python310_host_ubuntu_ca host python:3.10-slim-bookworm host_ca
else
  case_error python310_host_ubuntu_ca image_unavailable
fi

log "RESULTADOS | Consolidacao"
grep '^CASE_RESULT|' "$REPORT" || true
if grep -q '^CASE_RESULT|case=docker_host_ubuntu_ca|status=PASS|' "$REPORT"; then
  log "DECISAO | HOST_CA_VALIDATED | HTTPS com CA do Ubuntu passou no Docker host; bridge nao foi alterada"
  exit 0
fi
log "DECISAO | HOST_CA_NOT_VALIDATED | nao aplicar alteracao de certificados ou arquitetura sem aprofundar evidencia"
exit 21
