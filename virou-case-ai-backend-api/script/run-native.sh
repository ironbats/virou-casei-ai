#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
for executable in python3 docker; do
  command -v "$executable" >/dev/null || { echo "Requisito ausente: $executable"; exit 1; }
done
docker compose version >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12 ou superior é necessário"'
export VIRA_SOURCE_ROOT="$(python3 script/source-storage.py --root .)"
python3 script/bootstrap-env.py
docker compose up -d --wait --wait-timeout 120 postgres
echo "✅ PostgreSQL disponível em Docker; volumes preservados."
if [[ ! -x .venv/bin/python ]]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/alembic upgrade head
docker compose exec -T postgres sh -c 'exec psql -U postgres -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' < script/db-permissions.sql
.venv/bin/python script/init-credentials.py
.venv/bin/python script/bootstrap-admin.py
echo "✅ Schema atualizado. API e worker serão executados na máquina local."
pids=()
cleanup() {
  trap - EXIT INT TERM
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM
.venv/bin/uvicorn viracase.interfaces.public:app --host 127.0.0.1 --port 8000 --no-proxy-headers &
pids+=("$!")
.venv/bin/uvicorn viracase.interfaces.admin:app --host 127.0.0.1 --port 8001 --no-proxy-headers &
pids+=("$!")
.venv/bin/python -m viracase.infrastructure.worker &
pids+=("$!")
.venv/bin/python script/wait-ready.py
echo "✅ API pública: http://127.0.0.1:8000 | API administrativa privada: http://127.0.0.1:8001"
echo "Frontend e backoffice possuem seus próprios script/run-local.sh. Ctrl+C encerra os serviços locais, preservando o banco."
wait -n "${pids[@]}"
echo "Um processo encerrou. Os demais serão finalizados; verifique o erro acima."
exit 1
