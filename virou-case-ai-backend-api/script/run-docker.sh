#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo 'Configure as senhas do banco e VIRA_ADMIN_LOGIN, VIRA_ADMIN_NAME e VIRA_ADMIN_PASSWORD no .env; depois execute novamente.' >&2
  exit 1
fi
source script/publication-runtime.sh
publication_prepare_compose
publication_preserve_runtime_user
mkdir -p var/sources var/secrets
publication_compose config --quiet
if ! publication_compose up -d --build --wait --wait-timeout 300; then
  echo 'Diagnostico dos inicializadores (ultimas 120 linhas):' >&2
  publication_compose logs --no-color --tail=120 initialize storage-init || true
  echo 'Inicializacao incompleta. Consulte os logs de initialize, storage-init, public-api, admin-api e worker.' >&2
  exit 1
fi
if ! publication_compose run --rm --no-deps initialize python script/bootstrap-admin.py --check; then
  echo 'Falha na verificacao das credenciais administrativas. Consulte os logs de initialize.' >&2
  exit 1
fi
publication_assert_worker_profile
printf '%s\n' 'Containers iniciados e credenciais administrativas verificadas. Conferindo o executor de publicacao.'
if ! publication_compose exec -T worker python script/diagnose-publication.py --pretty; then
  echo 'A plataforma esta iniciada, mas a publicacao continua bloqueada pelo diagnostico acima.' >&2
  exit 2
fi
printf '%s\n' 'Executor local READY. A publicacao externa ainda exige a verificacao operacional da conexao no Backoffice.'
echo 'Execute o script/run-local.sh do frontend e do backoffice.'
publication_compose ps
