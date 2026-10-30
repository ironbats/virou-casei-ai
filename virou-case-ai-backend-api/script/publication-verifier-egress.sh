#!/usr/bin/env bash

publication_enable_verifier_egress() {
  PUBLICATION_VERIFIER_ENABLED=0
  if [[ "${VIRA_VERIFIER_EGRESS:-auto}" != auto && "${VIRA_VERIFIER_EGRESS:-auto}" != on && "${VIRA_VERIFIER_EGRESS:-auto}" != off ]]; then
    publication_fail "VERIFIER_EGRESS_CONFIGURATION_INVALID: use auto, on ou off."
    return 1
  fi
  if [[ "${VIRA_VERIFIER_EGRESS:-auto}" == off ]]; then
    printf '%s\n' 'VERIFIER_EGRESS_DISABLED: verificador HTTPS isolado desativado explicitamente.'
    return 0
  fi
  local daemon_host
  if [[ -n "${DOCKER_CONTEXT:-}" || -z "${DOCKER_HOST:-}" ]]; then
    daemon_host="$(docker context inspect --format '{{.Endpoints.docker.Host}}')" || return 1
  else
    daemon_host="$DOCKER_HOST"
  fi
  if [[ "$daemon_host" != unix://* ]]; then
    publication_fail 'VERIFIER_EGRESS_UNSUPPORTED: o verificador isolado exige Docker local via Unix socket.'
    return 1
  fi
  if [[ ! -s /etc/ssl/certs/ca-certificates.crt ]]; then
    publication_fail 'VERIFIER_EGRESS_CA_MISSING: pacote de certificados confiaveis do Ubuntu indisponivel.'
    return 1
  fi
  if [[ ! -f compose.verifier-egress.yaml ]]; then
    publication_fail 'VERIFIER_EGRESS_COMPOSE_MISSING: compose.verifier-egress.yaml indisponivel.'
    return 1
  fi
  local item
  for item in "${PUBLICATION_COMPOSE_ARGS[@]}"; do
    if [[ "$item" == compose.verifier-egress.yaml ]]; then
      PUBLICATION_VERIFIER_ENABLED=1
      return 0
    fi
  done
  PUBLICATION_COMPOSE_ARGS+=(-f compose.verifier-egress.yaml)
  PUBLICATION_VERIFIER_ENABLED=1
  printf '%s\n' 'VERIFIER_EGRESS_ENABLED: sidecar HTTPS com rede host, CA confiavel e socket Unix privado.'
}
