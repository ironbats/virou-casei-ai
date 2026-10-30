#!/bin/sh
set +x
set -eu

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

[ -n "${POSTGRES_PASSWORD:-}" ] || fail 'DATABASE_CONFIG_INVALID: POSTGRES_PASSWORD ausente.'
[ -n "${APP_DB_PASSWORD:-}" ] || fail 'DATABASE_CONFIG_INVALID: APP_DB_PASSWORD ausente.'
[ "${POSTGRES_USER:-postgres}" = postgres ] || fail 'DATABASE_CONFIG_INVALID: o Compose local exige o usuario postgres.'

unset PGHOSTADDR PGSERVICE PGSERVICEFILE PGPASSWORD
export PGDATABASE="${POSTGRES_DB:-viracase}"
export PGCONNECT_TIMEOUT=5
export PGOPTIONS='-c statement_timeout=15000 -c lock_timeout=10000'
export VIRA_SYNC_POSTGRES_PASSWORD=false
export VIRA_SYNC_APP_PASSWORD=false

check_admin() {
  PGPASSWORD="$POSTGRES_PASSWORD" psql -X -w -qAt --host=postgres --port=5432 --username=postgres \
    --set=ON_ERROR_STOP=1 --command='SELECT 1' >/dev/null 2>&1
}

check_app() {
  PGPASSWORD="$APP_DB_PASSWORD" psql -X -w -qAt --host=postgres --port=5432 --username=viracase \
    --set=ON_ERROR_STOP=1 --command='SELECT 1' >/dev/null 2>&1
}

check_admin || VIRA_SYNC_POSTGRES_PASSWORD=true
check_app || VIRA_SYNC_APP_PASSWORD=true

if [ "$VIRA_SYNC_POSTGRES_PASSWORD" = false ] && [ "$VIRA_SYNC_APP_PASSWORD" = false ]; then
  printf '%s\n' 'DATABASE_CREDENTIALS_OK: postgres e viracase autenticaram; nenhuma credencial foi alterada.'
  exit 0
fi

printf '%s\n' 'DATABASE_CREDENTIALS_RECONCILING: alinhando as credenciais do PostgreSQL local com a configuracao efetiva do Compose; dados preservados.'
if psql -X -w -qAt --host=/var/run/postgresql --port=5432 --username=postgres \
  --set=ON_ERROR_STOP=1 >/dev/null 2>&1 <<'SQL'
BEGIN;
SET LOCAL log_statement = 'none';
SET LOCAL log_min_duration_statement = -1;
SET LOCAL log_min_duration_sample = -1;
SET LOCAL log_min_error_statement = 'panic';
SET LOCAL log_transaction_sample_rate = 0;
SELECT pg_advisory_xact_lock(73210430);
\getenv admin_password POSTGRES_PASSWORD
\getenv app_password APP_DB_PASSWORD
\getenv sync_admin VIRA_SYNC_POSTGRES_PASSWORD
\getenv sync_app VIRA_SYNC_APP_PASSWORD
\if :sync_admin
ALTER ROLE postgres PASSWORD :'admin_password';
\endif
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'viracase') AS app_exists \gset
\if :app_exists
\if :sync_app
ALTER ROLE viracase PASSWORD :'app_password';
\endif
\else
CREATE ROLE viracase LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
SELECT current_database() AS database_name \gset
GRANT CONNECT ON DATABASE :"database_name" TO viracase;
GRANT USAGE ON SCHEMA public TO viracase;
\endif
COMMIT;
SQL
then
  :
else
  fail 'DATABASE_CREDENTIALS_RECONCILIATION_FAILED: nao foi possivel concluir a transacao pelo socket local. Verifique o banco configurado, o acesso local do usuario postgres e os logs do PostgreSQL. Nenhum volume ou regra de autenticacao foi removido.'
fi

check_admin || fail 'DATABASE_CREDENTIALS_CHECK_FAILED: postgres nao autenticou via TCP apos a reconciliacao; inicializacao bloqueada.'
check_app || fail 'DATABASE_CREDENTIALS_CHECK_FAILED: viracase nao autenticou via TCP apos a reconciliacao; inicializacao bloqueada.'
printf '%s\n' 'DATABASE_CREDENTIALS_RECONCILED: postgres e viracase autenticaram via TCP; migrations liberadas.'
