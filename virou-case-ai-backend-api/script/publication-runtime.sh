#!/usr/bin/env bash

publication_fail() {
  printf '%s\n' "$1" >&2
  return 1
}

publication_compose() {
  docker compose "${PUBLICATION_COMPOSE_ARGS[@]}" "$@"
}

publication_prepare_compose() {
  command -v docker >/dev/null || { publication_fail 'Docker nao encontrado.'; return 1; }
  docker compose version >/dev/null || return 1
  local security files separator item default_override daemon_host
  security="$(docker info --format '{{json .SecurityOptions}}')" || return 1
  PUBLICATION_APPARMOR=0
  [[ "$security" != *name=apparmor* ]] || PUBLICATION_APPARMOR=1
  PUBLICATION_COMPOSE_ARGS=()
  if [[ -z "${COMPOSE_FILE:-}" ]] && grep -Eq '^[[:space:]]*(export[[:space:]]+)?COMPOSE_FILE[[:space:]]*=' .env; then
    publication_fail 'COMPOSE_FILE esta no .env. Exporte a mesma lista no terminal antes de executar este script; nenhum arquivo de configuracao foi alterado.'
    return 1
  fi
  if [[ -n "${COMPOSE_FILE:-}" ]]; then
    separator="${COMPOSE_PATH_SEPARATOR:-:}"
    [[ ${#separator} == 1 ]] || { publication_fail 'COMPOSE_PATH_SEPARATOR deve ter um caractere.'; return 1; }
    IFS="$separator" read -r -a files <<< "$COMPOSE_FILE"
  else
    files=(compose.yaml)
    for default_override in compose.override.yaml compose.override.yml; do
      [[ -f "$default_override" ]] || continue
      if [[ "$PUBLICATION_APPARMOR" == 1 ]] || ! cmp -s "$default_override" compose.publication-apparmor.yaml; then
        files+=("$default_override")
      fi
      break
    done
  fi
  for item in "${files[@]}"; do
    [[ -n "$item" && -f "$item" ]] || { publication_fail 'Arquivo Compose ausente ou lista de arquivos invalida.'; return 1; }
    if [[ "$PUBLICATION_APPARMOR" == 0 ]] && cmp -s "$item" compose.publication-apparmor.yaml; then
      publication_fail 'O Compose solicitado exige AppArmor, mas o daemon nao anuncia suporte. Use o script sem selecionar o overlay AppArmor neste host.'
      return 1
    fi
    if [[ "$PUBLICATION_APPARMOR" == 1 ]] && cmp -s "$item" compose.publication-apparmor.yaml; then
      continue
    fi
    PUBLICATION_COMPOSE_ARGS+=(-f "$item")
  done
  if [[ "$PUBLICATION_APPARMOR" == 1 ]]; then
    PUBLICATION_COMPOSE_ARGS+=(-f compose.publication-apparmor.yaml)
    if [[ -n "${DOCKER_CONTEXT:-}" || -z "${DOCKER_HOST:-}" ]]; then
      daemon_host="$(docker context inspect --format '{{.Endpoints.docker.Host}}')" || return 1
    else
      daemon_host="$DOCKER_HOST"
    fi
    if [[ "$daemon_host" == unix://* ]]; then
      printf '%s\n' 'Validando o perfil AppArmor dedicado no host. Nenhuma politica global sera desativada.'
      if [[ "$(id -u)" == 0 ]]; then
        bash script/setup-publication-host.sh || return 1
      else
        command -v sudo >/dev/null || { publication_fail 'Instale o perfil como administrador: sudo bash script/setup-publication-host.sh'; return 1; }
        sudo bash script/setup-publication-host.sh || return 1
      fi
    else
      printf '%s\n' 'Docker remoto: o perfil viracase-publication deve estar carregado no host do daemon. O container nao sera criado sem esse perfil.' >&2
    fi
  fi
}

publication_preserve_runtime_user() {
  local container_id runtime_user
  container_id="$(publication_compose ps -aq worker)" || return 1
  container_id="${container_id%%$'\n'*}"
  if [[ -n "$container_id" ]]; then
    runtime_user="$(docker inspect --format '{{.Config.User}}' "$container_id")" || return 1
    if [[ "$runtime_user" =~ ^([0-9]+):([0-9]+)$ ]]; then
      export VIRA_RUNTIME_UID="${VIRA_RUNTIME_UID:-${BASH_REMATCH[1]}}"
      export VIRA_RUNTIME_GID="${VIRA_RUNTIME_GID:-${BASH_REMATCH[2]}}"
    else
      publication_fail 'O worker existente nao usa UID:GID numericos. Nao foi alterado; revise a identidade antes da recriacao.'
      return 1
    fi
  fi
  if [[ -z "${VIRA_RUNTIME_UID:-}" && "$(id -u)" != 0 ]]; then export VIRA_RUNTIME_UID="$(id -u)"; fi
  if [[ -z "${VIRA_RUNTIME_GID:-}" && "$(id -g)" != 0 ]]; then export VIRA_RUNTIME_GID="$(id -g)"; fi
  local value
  for value in "${VIRA_RUNTIME_UID:-10001}" "${VIRA_RUNTIME_GID:-10001}"; do
    if [[ ! "$value" =~ ^[0-9]+$ || "$value" =~ ^0+$ ]]; then
      publication_fail 'O executor deve usar UID e GID numericos e nao root. Nenhum container foi recriado.'
      return 1
    fi
  done
}

publication_assert_worker_profile() {
  local container_ids container_id actual
  container_ids="$(publication_compose ps -aq worker)" || return 1
  [[ -n "$container_ids" ]] || { publication_fail 'WORKER_NOT_FOUND: nenhum container worker foi localizado.'; return 1; }
  while IFS= read -r container_id; do
    actual="$(docker inspect --format '{{.AppArmorProfile}}' "$container_id")" || return 1
    if [[ "$PUBLICATION_APPARMOR" == 1 && "$actual" != viracase-publication ]]; then
      printf '%s\n' '{"event":"publication_deployment_blocked","code":"APPARMOR_PROFILE_MISMATCH","expected":"viracase-publication"}' >&2
      printf 'Perfil AppArmor recebido pelo worker: %q\n' "$actual" >&2
      publication_fail 'O worker nao recebeu o perfil dedicado. Nao prossiga para a Cloudflare; confira os arquivos Compose efetivos.'
      return 1
    fi
  done <<< "$container_ids"
  printf '%s\n' '{"event":"publication_container_policy_checked"}'
}
