#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ "${1:-}" == --native ]]; then
  shift
  exec bash script/run-native.sh "$@"
fi
exec bash script/run-docker.sh "$@"
