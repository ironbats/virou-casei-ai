#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  printf '%s\n' \
    'Uso: bash script/recover-publication.sh UUID [--inspect] [--login LOGIN] [--reason JUSTIFICATIVA]' \
    'Executa a recuperação administrativa no serviço admin-api já instalado.' \
    'A senha é solicitada no terminal; não informe senha ou token nos argumentos.' \
    '--inspect: confere e exibe o plano sem atestar ou resolver.' \
    'Para encerrar, revise o plano e digite a frase exata ENCERRAR UUID solicitada.' \
    'Saídas: 0=recuperado e pronto; 4=recuperado com outro impedimento; 2=inspeção, cancelamento ou entrada inválida; 3=falha técnica.'
}

invalid() {
  printf '%s\n' 'ARGUMENTS_INVALID: informe um UUID válido e somente --inspect, --login ou --reason. Consulte --help.' >&2
  exit 2
}

for argument in "$@"; do
  if [[ "$argument" == --help || "$argument" == -h ]]; then
    usage
    exit 0
  fi
done

operation_id=''
expect_value=0
literal=0
for argument in "$@"; do
  if [[ "$expect_value" == 1 ]]; then
    [[ -n "$argument" && "$argument" != --* ]] || invalid
    expect_value=0
    continue
  fi
  if [[ "$literal" == 0 ]]; then
    case "$argument" in
      --inspect) continue ;;
      --login|--reason) expect_value=1; continue ;;
      --login=*|--reason=*) [[ -n "${argument#*=}" ]] || invalid; continue ;;
      --) literal=1; continue ;;
      -*) invalid ;;
    esac
  fi
  [[ -z "$operation_id" ]] || invalid
  operation_id="$argument"
done

[[ "$expect_value" == 0 && "$operation_id" =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]] || invalid
if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' 'DOCKER_UNAVAILABLE: execute este comando no host da instalação com o serviço admin-api em funcionamento.' >&2
  exit 3
fi

cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec docker compose exec admin-api python script/recover-publication-local.py "$@"
