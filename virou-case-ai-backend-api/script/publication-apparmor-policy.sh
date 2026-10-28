#!/usr/bin/env bash

publication_apparmor_restriction() {
  local path=/proc/sys/kernel/apparmor_restrict_unprivileged_userns value
  if [[ ! -e "$path" && ! -L "$path" ]]; then
    printf '%s\n' absent
  elif [[ -r "$path" ]] && IFS= read -r value < "$path"; then
    case "$value" in
      0|1) printf '%s\n' "$value" ;;
      *) printf '%s\n' unknown ;;
    esac
  else
    printf '%s\n' unknown
  fi
}

publication_apparmor_render() {
  local source="$1" workspace="$2" restriction rule_count
  PUBLICATION_APPARMOR_POLICY="$workspace/publication-apparmor"
  PUBLICATION_APPARMOR_VARIANT=explicit-userns
  PUBLICATION_APPARMOR_RESTRICTION="$(publication_apparmor_restriction)"
  [[ -r "$source" ]] || { printf 'APPARMOR_SOURCE_MISSING: %s\n' "$source" >&2; return 1; }
  rule_count="$(grep -Ec '^[[:space:]]*userns[[:space:]]*,[[:space:]]*$' "$source" || true)"
  if [[ "$rule_count" != 1 ]]; then
    printf '%s\n' 'APPARMOR_SOURCE_INVALID: esperado exatamente um userns, no perfil original. Nenhuma regra foi alterada.' >&2
    return 1
  fi
  printf 'profile viracase-userns-syntax-probe {\n  userns,\n}\n' > "$workspace/userns-probe"
  if LC_ALL=C apparmor_parser -Q -T -K "$workspace/userns-probe" > "$workspace/userns-probe.log" 2>&1; then
    cat "$source" > "$PUBLICATION_APPARMOR_POLICY" || return 1
  else
    printf 'profile viracase-base-syntax-probe {\n}\n' > "$workspace/base-probe"
    if ! LC_ALL=C apparmor_parser -Q -T -K "$workspace/base-probe" > "$workspace/base-probe.log" 2>&1; then
      printf '%s\n' 'APPARMOR_PARSER_FAILED: o analisador recusou ate um perfil minimo. Nenhum perfil foi instalado.' >&2
      cat "$workspace/base-probe.log" >&2
      return 1
    fi
    if ! grep -Fq 'syntax error' "$workspace/userns-probe.log"; then
      printf '%s\n' 'APPARMOR_USERNS_PROBE_FAILED: a falha nao e de sintaxe. Nenhuma compatibilidade foi aplicada.' >&2
      cat "$workspace/userns-probe.log" >&2
      return 1
    fi
    restriction="$PUBLICATION_APPARMOR_RESTRICTION"
    case "$restriction" in
      0|absent) ;;
      *)
        printf 'APPARMOR_PARSER_USERNS_REQUIRED: parser sem suporte a userns e restricao do host=%s.\n' "$restriction" >&2
        printf '%s\n' 'Atualize o parser com pacotes suportados pela distribuicao. Nao desative a restricao do host. Nenhum perfil foi instalado.' >&2
        return 1
        ;;
    esac
    PUBLICATION_APPARMOR_VARIANT=legacy-no-userns
    sed '/^[[:space:]]*userns[[:space:]]*,[[:space:]]*$/d' "$source" > "$PUBLICATION_APPARMOR_POLICY" || return 1
    printf 'APPARMOR_LEGACY_COMPATIBILITY: parser sem a regra userns; restricao do host=%s. Apenas essa regra sera omitida na copia instalada. Fonte e sysctl preservados.\n' "$restriction" >&2
  fi
  if ! apparmor_parser -Q -T -K "$PUBLICATION_APPARMOR_POLICY" > "$workspace/profile-compile.log" 2>&1; then
    printf '%s\n' 'APPARMOR_PROFILE_INVALID: o perfil completo nao compilou. Nenhum perfil foi instalado.' >&2
    cat "$workspace/profile-compile.log" >&2
    return 1
  fi
  printf 'APPARMOR_POLICY_COMPILED: variant=%s restriction=%s\n' "$PUBLICATION_APPARMOR_VARIANT" "$PUBLICATION_APPARMOR_RESTRICTION"
}
