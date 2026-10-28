#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ -f .env ]] || { echo 'Instalacao existente sem .env. Nao foram alterados containers ou credenciais.' >&2; exit 1; }
source script/publication-runtime.sh
publication_prepare_compose
publication_preserve_runtime_user
publication_compose config --quiet
printf '%s\n' 'Reconstruindo worker e APIs sem remover banco, volumes ou credenciais.'
publication_compose build worker admin-api public-api
publication_compose up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 worker admin-api public-api
publication_assert_worker_profile
printf '%s\n' 'Executando o preflight e os testes de isolamento dentro do worker.'
if publication_compose exec -T worker python script/diagnose-publication.py --pretty; then
  printf '%s\n' 'Executor READY. No Backoffice, atualize o diagnostico e execute Verificar publicacao na conexao ativa.'
else
  printf '%s\n' 'Executor ainda bloqueado. O JSON acima contem a etapa e o erro real. Nao houve publicacao externa nem remocao de controles.' >&2
  exit 2
fi
