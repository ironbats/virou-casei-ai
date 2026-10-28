#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v apparmor_parser >/dev/null || { echo "AppArmor não está instalado. Use o compose padrão se o host não utiliza AppArmor."; exit 1; }
if [[ "$(id -u)" != 0 ]]; then
  echo "Execute: sudo bash script/setup-publication-host.sh" >&2
  exit 1
fi
install -m 0644 config/publication-apparmor /etc/apparmor.d/viracase-publication
apparmor_parser -r /etc/apparmor.d/viracase-publication
printf '%s\n' "Perfil do worker carregado. Inicie com: docker compose -f compose.yaml -f compose.publication-apparmor.yaml up -d --build"
