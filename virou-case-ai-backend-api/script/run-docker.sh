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
publication_build_images
startup_started_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
if publication_compose up -d --no-build --wait --wait-timeout 300; then
  :
else
  startup_status=$?
  echo 'DOCKER_STARTUP_FAILED: o build concluiu, mas a inicializacao ou a verificacao de saude falhou.' >&2
  printf 'Diagnostico dos inicializadores desde %s (ate 120 linhas por servico):\n' "$startup_started_at" >&2
  publication_compose logs --no-color --timestamps --since "$startup_started_at" --tail=120 initialize storage-init || true
  echo 'Inicializacao incompleta. Consulte os logs de initialize, storage-init, public-api, admin-api e worker.' >&2
  exit "$startup_status"
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
printf '%s\n' 'Executor local READY. O worker verifica automaticamente as conexoes Cloudflare validadas ou ativas. Acompanhe o estado no Backoffice.'
echo 'Execute o script/run-local.sh do frontend e do backoffice.'
publication_compose ps
