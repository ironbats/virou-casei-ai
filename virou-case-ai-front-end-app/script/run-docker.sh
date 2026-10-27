#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v docker >/dev/null || { echo "Docker é necessário." >&2; exit 1; }
docker compose version >/dev/null
docker info >/dev/null
docker network inspect viroucaseai-public >/dev/null 2>&1 || { echo "Suba primeiro o backend com script/run-local.sh para criar a rede viroucaseai-public." >&2; exit 1; }
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 180
echo "✅ viroucaseai frontend em container: http://localhost:5173 ou http://127.0.0.1:5173"
docker compose ps
