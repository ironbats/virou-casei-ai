#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ -f .env ]] || { echo 'Instalacao existente sem .env. Nao foram alterados containers ou credenciais.' >&2; exit 1; }
source script/publication-runtime.sh
publication_prepare_compose
publication_preserve_runtime_user
publication_compose config --quiet
printf '%s\n' 'Reconstruindo worker e APIs sem remover banco, volumes ou credenciais.'
publication_build_images worker admin-api public-api
startup_started_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
if publication_compose up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 worker admin-api public-api; then
  :
else
  startup_status=$?
  printf '%s\n' 'DOCKER_STARTUP_FAILED: o build concluiu, mas a recriacao ou a verificacao de saude dos servicos falhou.' >&2
  printf 'Diagnostico dos servicos desde %s (ate 120 linhas por servico):\n' "$startup_started_at" >&2
  publication_compose logs --no-color --timestamps --since "$startup_started_at" --tail=120 worker admin-api public-api || true
  exit "$startup_status"
fi
publication_assert_worker_profile
printf '%s\n' 'Executando o preflight e os testes de isolamento dentro do worker.'
if publication_compose exec -T worker python script/diagnose-publication.py --pretty; then
  printf '%s\n' 'Executor READY. O worker verifica automaticamente as conexoes Cloudflare validadas ou ativas. No Backoffice, acompanhe o diagnostico.'
else
  printf '%s\n' 'Executor ainda bloqueado. O JSON acima contem a etapa e o erro real. Nao houve publicacao externa nem remocao de controles.' >&2
  exit 2
fi
