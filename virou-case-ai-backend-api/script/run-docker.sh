#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v docker >/dev/null || { echo "Docker é necessário." >&2; exit 1; }
docker compose version >/dev/null
docker info >/dev/null
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "Configure as senhas do banco e VIRA_ADMIN_LOGIN, VIRA_ADMIN_NAME e VIRA_ADMIN_PASSWORD no .env; depois execute novamente." >&2
  exit 1
fi
if [[ -z "${VIRA_RUNTIME_UID:-}" && "$(id -u)" != 0 ]]; then export VIRA_RUNTIME_UID="$(id -u)"; fi
if [[ -z "${VIRA_RUNTIME_GID:-}" && "$(id -g)" != 0 ]]; then export VIRA_RUNTIME_GID="$(id -g)"; fi
mkdir -p var/sources var/secrets
docker compose config --quiet
if ! docker compose up -d --build --wait --wait-timeout 300; then
  echo "Diagnóstico dos inicializadores (últimas 120 linhas):" >&2
  docker compose logs --no-color --tail=120 initialize storage-init || true
  echo "Inicialização incompleta. Consulte: docker compose logs --tail=100 initialize storage-init public-api admin-api worker" >&2
  exit 1
fi
if ! docker compose run --rm --no-deps initialize python script/bootstrap-admin.py --check; then
  echo "Falha na verificação das credenciais administrativas. Consulte: docker compose logs --tail=120 initialize" >&2
  exit 1
fi
echo "✅ Containers do viroucaseai iniciados e healthchecks aprovados."
echo "Execute o script/run-local.sh do frontend e do backoffice."
echo "Os processos Python rodam nos containers. O Python do Ubuntu não é utilizado."
docker compose ps
