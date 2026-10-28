#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mode=install
case "${1:-}" in
  '') [[ $# == 0 ]] || exit 2 ;;
  --check) [[ $# == 1 ]] || exit 2; mode=check ;;
  *) printf '%s\n' 'Uso: bash script/setup-publication-host.sh [--check]' >&2; exit 2 ;;
esac
if [[ "$mode" == install && "$(id -u)" != 0 ]]; then
  printf '%s\n' 'Execute: sudo bash script/setup-publication-host.sh. Para validar sem sudo: bash script/setup-publication-host.sh --check' >&2
  exit 1
fi
command -v apparmor_parser >/dev/null || { printf '%s\n' 'apparmor_parser ausente. Instale o pacote apparmor suportado pela distribuicao; nao desative AppArmor.' >&2; exit 1; }
source script/publication-apparmor-policy.sh
printf '%s\n' 'Verificando a sintaxe suportada pelo AppArmor local, sem carregar perfis de teste.'
apparmor_parser --version
workspace="$(mktemp -d)"
staged=''
cleanup() {
  [[ -z "$staged" ]] || rm -f -- "$staged"
  rm -rf -- "$workspace"
}
trap cleanup EXIT
publication_apparmor_render config/publication-apparmor "$workspace"
if [[ "$mode" == check ]]; then
  printf '%s\n' 'APPARMOR_CHECK_OK: compilacao concluida. Nenhum perfil carregado, sysctl alterado ou arquivo instalado em /etc.'
  exit 0
fi
profiles=/sys/kernel/security/apparmor/profiles
[[ -r "$profiles" ]] || { printf '%s\n' 'Nao foi possivel consultar AppArmor no host. Execute no Linux que hospeda o daemon Docker.' >&2; exit 1; }
profile=/etc/apparmor.d/viracase-publication
if cmp -s "$PUBLICATION_APPARMOR_POLICY" "$profile" && grep -Fxq 'viracase-publication (enforce)' "$profiles"; then
  printf 'Perfil viracase-publication atualizado e em enforce. Variante: %s.\n' "$PUBLICATION_APPARMOR_VARIANT"
  exit 0
fi
staged="$(mktemp /etc/apparmor.d/.viracase-publication.XXXXXX)"
install -m 0644 "$PUBLICATION_APPARMOR_POLICY" "$staged"
apparmor_parser -r -T -K "$staged"
grep -Fxq 'viracase-publication (enforce)' "$profiles" || { printf '%s\n' 'O perfil nao foi confirmado em enforce. Nao prossiga com a recriacao.' >&2; exit 1; }
mv -f "$staged" "$profile"
staged=''
printf '%s\n' 'Perfil viracase-publication carregado e persistido. O worker existente ainda precisa ser recriado pelo script/restart-publication.sh.'
