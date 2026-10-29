#!/usr/bin/env bash
set -uo pipefail

if [[ $# != 0 ]]; then
  printf '%s\n' 'Uso: bash script/diagnose-docker-build.sh' >&2
  exit 2
fi

printf '%s\n' \
  'Diagnostico somente leitura. Nao inicia builds, containers ou builders e nao altera DNS.' \
  'Cliente, daemon Docker e builder podem usar redes, proxies e resolvedores diferentes.' \
  'Saidas: 0=sondagens concluidas; 1=falha observada; 2=diagnostico incompleto.'

if ! command -v timeout >/dev/null 2>&1; then
  printf '%s\n' 'DIAGNOSTIC_INCOMPLETE: comando timeout ausente; nenhuma sondagem foi executada.' >&2
  exit 2
fi

failed=0
incomplete=0
probe_output=''
probe_status=0

probe() {
  probe_output=''
  probe_status=0
  probe_output="$(timeout --kill-after=2s 12s "$@" 2>/dev/null)" || probe_status=$?
}

report_failure() {
  failed=1
  if [[ "$probe_status" == 124 || "$probe_status" == 137 ]]; then
    printf '%s=TIMEOUT\n' "$1" >&2
  else
    printf '%s=FAILED exit=%s\n' "$1" "$probe_status" >&2
  fi
}

if command -v docker >/dev/null 2>&1; then
  probe docker info --format '{{.ServerVersion}}'
  if [[ "$probe_status" == 0 ]]; then
    printf '%s\n' 'DOCKER_DAEMON=REACHABLE'
  else
    report_failure DOCKER_DAEMON
  fi

  endpoint=''
  if [[ -n "${DOCKER_CONTEXT:-}" || -z "${DOCKER_HOST:-}" ]]; then
    if [[ -n "${DOCKER_CONTEXT:-}" ]]; then
      printf '%s\n' 'DOCKER_SELECTION=DOCKER_CONTEXT'
    else
      printf '%s\n' 'DOCKER_SELECTION=CURRENT_CONTEXT'
    fi
    probe docker context inspect --format '{{.Endpoints.docker.Host}}'
    if [[ "$probe_status" == 0 ]]; then
      endpoint="$probe_output"
    else
      report_failure DOCKER_ENDPOINT
    fi
  else
    printf '%s\n' 'DOCKER_SELECTION=DOCKER_HOST'
    endpoint="$DOCKER_HOST"
  fi
  case "$endpoint" in
    unix://*) printf '%s\n' 'DOCKER_ENDPOINT=LOCAL_SOCKET' ;;
    npipe://*) printf '%s\n' 'DOCKER_ENDPOINT=WINDOWS_PIPE' ;;
    ssh://*|tcp://*|http://*|https://*) printf '%s\n' 'DOCKER_ENDPOINT=NETWORK' ;;
    *) incomplete=1; printf '%s\n' 'DOCKER_ENDPOINT=UNKNOWN' >&2 ;;
  esac
  endpoint=''

  probe docker buildx version
  if [[ "$probe_status" == 124 || "$probe_status" == 137 ]]; then
    report_failure BUILDX
  elif [[ "$probe_status" != 0 ]]; then
    incomplete=1
    printf '%s\n' 'BUILDX=UNAVAILABLE; inspecao do builder nao executada.' >&2
  else
    probe docker buildx inspect
    if [[ "$probe_status" != 0 ]]; then
      report_failure BUILDER_INSPECTION
    else
      driver=''
      while IFS= read -r line; do
        if [[ "$line" =~ ^Driver:[[:space:]]+(docker|docker-container|kubernetes|remote)[[:space:]]*$ ]]; then
          driver="${BASH_REMATCH[1]}"
          break
        fi
      done <<< "$probe_output"
      if [[ -n "$driver" ]]; then
        printf 'BUILDER_DRIVER=%s\n' "$driver"
      else
        incomplete=1
        printf '%s\n' 'BUILDER_DRIVER=UNKNOWN' >&2
      fi
    fi
  fi
else
  incomplete=1
  printf '%s\n' 'DOCKER=UNAVAILABLE' >&2
fi

if command -v getent >/dev/null 2>&1; then
  for hostname in registry-1.docker.io auth.docker.io; do
    probe getent ahosts "$hostname"
    if [[ "$probe_status" == 0 && -n "$probe_output" ]]; then
      printf 'CLIENT_DNS[%s]=RESOLVED\n' "$hostname"
    else
      report_failure "CLIENT_DNS[$hostname]"
    fi
  done
else
  incomplete=1
  printf '%s\n' 'CLIENT_DNS=INCOMPLETE; comando getent ausente.' >&2
fi

if command -v curl >/dev/null 2>&1; then
  probe curl -q --silent --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 5 --max-time 10 --proto '=https' https://registry-1.docker.io/v2/
  if [[ "$probe_status" == 0 ]]; then
    case "$probe_output" in
      401) printf '%s\n' 'CLIENT_HTTPS=REACHABLE HTTP=401; desafio de autenticacao esperado do registry.' ;;
      200) printf '%s\n' 'CLIENT_HTTPS=REACHABLE HTTP=200' ;;
      *) failed=1; printf '%s\n' 'CLIENT_HTTPS=UNEXPECTED_HTTP_RESPONSE' >&2 ;;
    esac
  else
    failed=1
    case "$probe_status" in
      28|124|137) printf '%s\n' 'CLIENT_HTTPS=TIMEOUT' >&2 ;;
      5|6) printf '%s\n' 'CLIENT_HTTPS=DNS_FAILED; pode envolver o proxy configurado no cliente.' >&2 ;;
      35|51|58|60|77|80|82|83|90|91) printf '%s\n' 'CLIENT_HTTPS=TLS_FAILED' >&2 ;;
      *) report_failure CLIENT_HTTPS ;;
    esac
  fi
else
  incomplete=1
  printf '%s\n' 'CLIENT_HTTPS=INCOMPLETE; comando curl ausente.' >&2
fi

probe_output=''
printf '%s\n' \
  'Em hosts com systemd-resolved, consulte: resolvectl status; resolvectl query registry-1.docker.io; resolvectl query auth.docker.io.' \
  'Estas sondagens do cliente nao comprovam acesso pelo builder, download da imagem ou sucesso do build.'
if [[ "$failed" == 1 ]]; then
  printf '%s\n' 'DIAGNOSTIC_FAILED: houve falha observada; confira separadamente eventuais sondagens incompletas.' >&2
  exit 1
fi
if [[ "$incomplete" == 1 ]]; then
  printf '%s\n' 'DIAGNOSTIC_INCOMPLETE: uma ou mais sondagens nao puderam ser concluidas.' >&2
  exit 2
fi
printf '%s\n' 'DIAGNOSTIC_COMPLETED: sondagens concluidas; o build ainda precisa ser executado e validado.'
