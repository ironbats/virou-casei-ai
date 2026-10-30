#!/usr/bin/env bash
set -u
set -o pipefail
umask 077

OPERATION_ID="${VIRA_DIAG_OPERATION_ID:-204a0d55-6b7e-4aac-9f33-2e178e80bed8}"
REPORT="$(mktemp /tmp/viroucase-http-json-v69.XXXXXXXX.txt)" || exit 1
exec 3>&1
exec >"$REPORT" 2>&1
exec 4>&1
STARTED_AT="$SECONDS"
PHASE=0
PHASE_COUNT=8

progress() {
  local line
  line="[$(date +%H:%M:%S)] [+$((SECONDS - STARTED_AT))s] $*"
  printf '%s\n' "$line" >&3
  printf '%s\n' "$line" >&4
}

console_result() {
  printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&3
}

live_results() {
  local service="$1" line
  while IFS= read -r line; do
    printf '%s\n' "$line"
    case "$line" in
      DNS\|*|DIRECT_TLS\|*|HTTPX\|*|HTTPX=*|CLOUDFLARE_API\|*) console_result "$service | $line" ;;
    esac
  done
}

phase() {
  PHASE=$((PHASE + 1))
  progress "ETAPA ${PHASE}/${PHASE_COUNT} | $*"
}

watch_command() {
  local label="$1" limit="$2" pid rc started_at last_notice
  shift 2
  progress "INICIO | $label | limite=${limit}"
  timeout -k 3s "$limit" "$@" &
  pid=$!
  started_at="$SECONDS"
  last_notice="$SECONDS"
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    if ((SECONDS - last_notice >= 10)); then
      progress "EM EXECUCAO | $label | decorridos=$((SECONDS - started_at))s"
      last_notice="$SECONDS"
    fi
  done
  wait "$pid"
  rc=$?
  if ((rc == 124 || rc == 137)); then
    progress "TIMEOUT | $label | limite=${limit} | consulte o relatorio"
  elif ((rc != 0)); then
    progress "FALHA DE EXECUCAO | $label | exit=$rc | consulte o relatorio"
  else
    progress "EXECUCAO ENCERRADA | $label | duracao=$((SECONDS - started_at))s | resultados no relatorio"
  fi
  return "$rc"
}

finish() {
  local rc=$?
  trap - EXIT
  if ((rc == 0)); then
    progress "CONCLUIDO | diagnostico finalizado; nenhum recurso foi alterado"
  else
    progress "INTERROMPIDO | exit=$rc | relatorio parcial preservado"
  fi
  printf 'RELATORIO=%s\n' "$REPORT" >&3
}

trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
progress "INICIO | Diagnostico Virou Case AI v2.0.0.69, somente leitura"
progress "ARQUIVO DE EVIDENCIAS | $REPORT"
progress "Pode interromper com Ctrl+C; nenhuma publicacao ou alteracao sera executada"

printf 'VIRACASE_HTTP_JSON_DIAGNOSTIC=read_only\n'
printf 'UTC=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'OPERATION_ID=%s\n' "$OPERATION_ID"
printf 'BASELINE=backend_v2.0.0.69\n'
printf 'ACTIONS=read_only_sql,docker_inspection,dns,tcp_tls,public_https_get\n'

phase "Validando estrutura do projeto e ferramentas locais"
if [[ ! -f compose.yaml || ! -f src/viracase/infrastructure/publication_verification.py ]]; then
  printf 'ERROR=execute este script a partir da raiz da v2.0.0.69 do backend\n'
  progress "ERRO | Execute este script na raiz do backend v2.0.0.69"
  exit 2
fi
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  printf 'ERROR=Docker Compose indisponivel neste usuario\n'
  progress "ERRO | Docker Compose indisponivel"
  exit 2
fi
if ! command -v timeout >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  printf 'ERROR=python3 ou timeout indisponivel neste usuario\n'
  progress "ERRO | python3 ou timeout indisponivel"
  exit 2
fi
progress "OK | Estrutura do projeto e ferramentas disponíveis"

phase "Inspecionando Compose, contêineres e redes Docker"
printf '\n=== COMPOSE_RUNTIME ===\n'
watch_command "Inspecao do Compose e redes" 125s python3 - <<'PY'
import json, subprocess

def run(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None

p=run(['docker','compose','config','--format','json'])
if p and p.returncode == 0:
    try:
        config=json.loads(p.stdout)
        for name in ('postgres','initialize','worker','admin-api','public-api'):
            service=config.get('services',{}).get(name,{})
            networks=service.get('networks',{})
            print('SERVICE_NETWORKS|%s|%s'%(name,','.join(sorted(networks)) if isinstance(networks,dict) else ','.join(networks)))
        for name, config_network in sorted(config.get('networks',{}).items()):
            print('NETWORK|%s|internal=%s'%(name,bool(config_network.get('internal',False))))
    except (ValueError,TypeError,KeyError):
        print('COMPOSE_CONFIG=not_parseable')
else:
    print('COMPOSE_CONFIG=unavailable')

p=run(['docker','compose','ps','--all','--format','json'])
if p and p.returncode == 0:
    try:
        raw=p.stdout.strip()
        rows=json.loads(raw) if raw.startswith('[') else [json.loads(line) for line in raw.splitlines() if line.startswith('{')]
        for item in rows:
            if isinstance(item,dict) and item.get('Service') in ('postgres','initialize','worker','admin-api','public-api'):
                print('CONTAINER|%s|state=%s|health=%s'%(item.get('Service'),item.get('State','unknown'),item.get('Health','unknown')))
    except (ValueError,TypeError):
        print('CONTAINERS=not_parseable')
else:
    print('CONTAINERS=unavailable')

for service in ('worker','admin-api'):
    p=run(['docker','compose','ps','-q',service])
    if not p or p.returncode or not p.stdout.strip():
        print('CONTAINER_NETWORKS|%s|unavailable'%service)
        continue
    ident=p.stdout.strip().splitlines()[0]
    p=run(['docker','inspect','--format','{{json .NetworkSettings.Networks}}',ident])
    if not p or p.returncode:
        print('CONTAINER_NETWORKS|%s|unavailable'%service)
        continue
    try:
        networks=json.loads(p.stdout)
        for name,details in sorted(networks.items()):
            print('CONTAINER_NETWORK|%s|%s|ipv4=%s|gateway=%s'%(service,name,details.get('IPAddress','unknown'),details.get('Gateway','unknown')))
    except (ValueError,TypeError,AttributeError):
        print('CONTAINER_NETWORKS|%s|not_parseable'%service)
PY

phase "Lendo registros da verificacao no PostgreSQL (somente leitura)"
printf '\n=== STORED_VERIFICATION_RESOURCES_READ_ONLY ===\n'
lookup_resources() {
  local service="$1"
  watch_command "Consulta SQL via $service" 35s docker compose exec -T -e "VIRA_DIAG_OPERATION_ID=$OPERATION_ID" "$service" python - 2>/dev/null <<'PY'
import json, os, re
from sqlalchemy import create_engine, text
from viracase.infrastructure.config import settings

try:
    engine=create_engine(settings().database_url,connect_args={'connect_timeout':5})
    with engine.connect() as conn:
        conn.execute(text('SET TRANSACTION READ ONLY'))
        row=conn.execute(text("SELECT id,state,error_code,result FROM operations WHERE type='publication_check' AND id=:operation_id"),{'operation_id':os.environ.get('VIRA_DIAG_OPERATION_ID','')}).mappings().first()
        if row is None:
            row=conn.execute(text("SELECT id,state,error_code,result FROM operations WHERE type='publication_check' ORDER BY created_at DESC LIMIT 1")).mappings().first()
    engine.dispose()
    if row is None:
        print('DB_OPERATION=not_found')
    else:
        print('DB_OPERATION|id=%s|state=%s|error_code=%s'%(row['id'],row['state'],row['error_code']))
        result=row['result']
        if isinstance(result,str):
            result=json.loads(result)
        if not isinstance(result,dict):
            result={}
        for resource in result.get('verification_resources',[]):
            if not isinstance(resource,dict):
                continue
            stack=resource.get('stack','')
            url=resource.get('url','')
            if stack in ('hono-typescript-d1','fastapi-python-d1') and isinstance(url,str) and re.fullmatch(r'https://[a-z0-9][a-z0-9-]{0,62}\.[a-z0-9][a-z0-9-]{0,62}\.workers\.dev',url):
                print('RESOURCE|%s|%s|%s'%(stack,url,resource.get('revision_id','') if isinstance(resource.get('revision_id'),str) else ''))
        failure=result.get('verification_failure')
        if isinstance(failure,dict):
            print('DB_FAILURE|code=%s|stage=%s'%(failure.get('code','unknown'),failure.get('stage','unknown')))
except Exception as exc:
    print('DB_READ_ERROR|class=%s'%type(exc).__name__)
PY
}

progress "INICIO | Busca dos enderecos publicados via worker"
lookup="$(lookup_resources worker)" || lookup=""
if [[ "$lookup" != *'RESOURCE|'* ]]; then
  progress "AVISO | Worker nao retornou enderecos; buscando via admin-api"
  alternate="$(lookup_resources admin-api)" || alternate=""
  if [[ -n "$alternate" ]]; then lookup="$alternate"; fi
fi
printf '%s\n' "${lookup:-DB_OPERATION=unavailable}"

urls=()
stacks=()
revisions=()
while IFS='|' read -r kind stack url revision; do
  if [[ "$kind" == RESOURCE && "$url" =~ ^https://[a-z0-9][a-z0-9-]{0,62}\.[a-z0-9][a-z0-9-]{0,62}\.workers\.dev$ ]]; then
    urls+=("$url")
    stacks+=("$stack")
    revisions+=("$revision")
  fi
done <<< "$lookup"
for value in "$@"; do
  if [[ "$value" =~ ^https://[a-z0-9][a-z0-9-]{0,62}\.[a-z0-9][a-z0-9-]{0,62}\.workers\.dev$ ]]; then
    urls+=("$value")
    stacks+=("manual")
    revisions+=("")
  else
    printf 'IGNORED_MANUAL_URL=invalid_or_not_workers_dev\n'
  fi
done

if [[ "${#urls[@]}" -eq 0 ]]; then
  printf 'NO_URLS=nao foi possivel obter URLs do banco; opcionalmente informe https://worker.conta.workers.dev como argumento\n'
  progress "AVISO | Nenhuma URL publica localizada: os testes dos perfis serao ignorados"
else
  progress "OK | ${#urls[@]} endereco(s) publico(s) localizado(s) para verificacao de rede"
fi

printf '\n=== NETWORK_HTTPX_FROM_CONTAINERS ===\n'
probe_container() {
  local service="$1" target="$2" scope="$3" expected_revision="$4"
  printf '\nTARGET|service=%s|scope=%s|url=%s\n' "$service" "$scope" "$target"
  local limit="45s"
  if [[ "$scope" == full ]]; then limit="75s"; fi
  watch_command "Conteiner $service: DNS/TLS/HTTPX ($scope) em $target" "$limit" docker compose exec -T -e "VIRA_DIAG_URL=$target" -e "VIRA_DIAG_SCOPE=$scope" -e "VIRA_DIAG_EXPECTED_REVISION=$expected_revision" "$service" python -u - 2>/dev/null <<'PY' | live_results "$service"
import json, os, re, socket, ssl, time
from uuid import uuid4
from urllib.parse import urlsplit

try:
    import httpx
except ImportError:
    print('HTTPX=unavailable')
    raise SystemExit(0)

url=os.environ.get('VIRA_DIAG_URL','')
scope=os.environ.get('VIRA_DIAG_SCOPE','short')
expected_revision=os.environ.get('VIRA_DIAG_EXPECTED_REVISION','')
p=urlsplit(url)
valid=(p.scheme=='https' and bool(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}\.[a-z0-9][a-z0-9-]{0,62}\.workers\.dev',p.hostname or '')) and p.port is None and not p.username and not p.password and p.path in ('','/') and not p.query and not p.fragment)
if not valid:
    print('TARGET_REJECTED=invalid')
    raise SystemExit(0)

host=p.hostname
print('PROXY_KEYS_PRESENT='+','.join(k for k in ('HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','NO_PROXY','https_proxy','http_proxy','all_proxy','no_proxy') if os.environ.get(k)))
try:
    resolved=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    ips=sorted(set(a[4][0] for a in resolved))
    print('DNS|ok=true|address_count=%d|addresses=%s'%(len(ips),','.join(ips[:6])))
except Exception as exc:
    print('DNS|ok=false|class=%s'%type(exc).__name__)

try:
    start=time.monotonic()
    sock=socket.create_connection((host,443),timeout=5)
    sock.settimeout(5)
    with ssl.create_default_context().wrap_socket(sock,server_hostname=host) as secure:
        print('DIRECT_TLS|ok=true|protocol=%s|duration_ms=%d'%(secure.version(),round((time.monotonic()-start)*1000)))
except Exception as exc:
    print('DIRECT_TLS|ok=false|class=%s'%type(exc).__name__)

paths=('/','/api/__viroucaseai_revision','/api/health') if scope=='full' else ('/',)
for trust_env in (True,False):
    selected_paths=paths if trust_env else ('/',)
    for path in selected_paths:
        started=time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(8,connect=5),trust_env=trust_env,follow_redirects=False) as client:
                with client.stream('GET',url+path.lstrip('/') if url.endswith('/') else url+path,params={'__viroucaseai_probe':uuid4().hex},headers={'Cache-Control':'no-cache','Accept':'text/html' if path=='/' else 'application/json'}) as response:
                    media=response.headers.get('content-type','').split(';',1)[0].strip().lower()
                    ray=response.headers.get('cf-ray','')
                    if not re.fullmatch(r'[A-Fa-f0-9]{8,32}(?:-[A-Z]{3})?',ray):
                        ray='none'
                    shape='not_checked'
                    if path!='/' and response.status_code==200 and media=='application/json':
                        data=bytearray()
                        try:
                            for chunk in response.iter_bytes(chunk_size=4096):
                                data.extend(chunk)
                                if len(data)>65536:
                                    break
                            if len(data)>65536:
                                shape='response_too_large'
                            else:
                                payload=json.loads(data)
                                if not isinstance(payload,dict):
                                    shape='not_object'
                                elif path.endswith('__viroucaseai_revision'):
                                    shape='revision_match_%s_db_true_%s'%((payload.get('revision_id')==expected_revision) if expected_revision else 'not_supplied',payload.get('database') is True)
                                else:
                                    shape='ok_false_%s_db_false_%s'%(payload.get('ok') is False,payload.get('database') is False)
                        except Exception:
                            shape='json_unreadable'
                    print('HTTPX|trust_env=%s|path=%s|status=%s|mime=%s|cf_ray=%s|shape=%s|duration_ms=%d'%(str(trust_env).lower(),path,response.status_code,media if media in ('text/html','text/plain','application/json') else 'other',ray,shape,round((time.monotonic()-started)*1000)))
        except Exception as exc:
            print('HTTPX|trust_env=%s|path=%s|error=%s|duration_ms=%d'%(str(trust_env).lower(),path,type(exc).__name__,round((time.monotonic()-started)*1000)))
PY
}

phase "Testando Cloudflare API e HTTPS pelo host"
printf '\n=== CONTROL_PLANE_BASELINE ===\n'
if command -v curl >/dev/null 2>&1; then
  for control in 'https://api.cloudflare.com/client/v4/' 'https://www.cloudflare.com/'; do
    for mode in proxy direct; do
      opts=()
      if [[ "$mode" == direct ]]; then opts+=(--noproxy '*'); fi
      progress "INICIO | Host HTTPS $mode: $control (limite 8s)"
      measure="$(curl -s -o /dev/null --connect-timeout 4 --max-time 8 "${opts[@]}" -w '%{http_code}|%{time_namelookup}|%{time_connect}|%{time_appconnect}|%{time_total}' "$control" 2>/dev/null)"
      printf 'HOST_CONTROL|url=%s|mode=%s|http_dns_connect_tls_total=%s\n' "$control" "$mode" "${measure:-unavailable}"
      progress "RESULTADO | Host HTTPS $mode: codigo=${measure%%|*} | detalhes no relatorio"
    done
  done
else
  printf 'HOST_CURL=not_installed\n'
  progress "AVISO | curl ausente; testes HTTPS do host foram ignorados"
fi

phase "Comparando acesso Cloudflare API nos contêineres worker e admin-api"
printf '\n=== CONTROL_PLANE_FROM_CONTAINERS ===\n'
for service in worker admin-api; do
  printf 'CONTROL_SERVICE=%s\n' "$service"
  watch_command "Cloudflare API via $service (proxy ligado/desligado)" 40s docker compose exec -T "$service" python -u - 2>/dev/null <<'PYCONTROL' | live_results "$service"
import time
import httpx
for trust in (True,False):
    start=time.monotonic()
    try:
        with httpx.Client(trust_env=trust,timeout=httpx.Timeout(8,connect=5),follow_redirects=False) as client:
            with client.stream('GET','https://api.cloudflare.com/client/v4/') as response:
                print('CLOUDFLARE_API|trust_env=%s|status=%d|duration_ms=%d'%(str(trust).lower(),response.status_code,round((time.monotonic()-start)*1000)))
    except Exception as exc:
        print('CLOUDFLARE_API|trust_env=%s|error=%s|duration_ms=%d'%(str(trust).lower(),type(exc).__name__,round((time.monotonic()-start)*1000)))
PYCONTROL
  if [[ "$?" -ne 0 ]]; then printf 'CONTROL_SERVICE_RESULT=unavailable\n'; fi
done

phase "Testando os Workers publicados (host, worker e admin-api)"
if [[ "${#urls[@]}" -gt 0 ]]; then
  for i in "${!urls[@]}"; do
    progress "PERFIL $((i + 1))/${#urls[@]} | ${stacks[$i]} | ${urls[$i]}"
    printf '\n=== PROFILE_%s ===\n' "${stacks[$i]}"
    target="${urls[$i]}"
    if command -v curl >/dev/null 2>&1; then
      for mode in proxy direct; do
        opts=()
        if [[ "$mode" == direct ]]; then opts+=(--noproxy '*'); fi
        progress "INICIO | Host -> ${stacks[$i]} | modo=$mode | limite=8s"
        measure="$(curl -s -o /dev/null --connect-timeout 4 --max-time 8 "${opts[@]}" -w '%{http_code}|%{time_namelookup}|%{time_connect}|%{time_appconnect}|%{time_total}|%{content_type}' "${target}/" 2>/dev/null)"
        printf 'HOST_PUBLIC|stack=%s|mode=%s|http_dns_connect_tls_total_mime=%s\n' "${stacks[$i]}" "$mode" "${measure:-unavailable}"
        progress "RESULTADO | Host -> ${stacks[$i]} | modo=$mode | HTTP=${measure%%|*}"
      done
    fi
    probe_container worker "$target" full "${revisions[$i]}" || printf 'WORKER_PROBE=unavailable\n'
    probe_container admin-api "$target" short "${revisions[$i]}" || printf 'ADMIN_API_PROBE=unavailable\n'
  done
fi

phase "Contabilizando eventos recentes do worker (sem dados sensiveis)"
printf '\n=== RECENT_SAFE_EVENT_COUNTERS ===\n'
watch_command "Contadores de eventos no Docker" 30s python3 - <<'PY'
import collections,json,subprocess
try:
    p=subprocess.run(['docker','compose','logs','--no-color','--tail','1000','worker'],capture_output=True,text=True,timeout=20)
except (OSError,subprocess.TimeoutExpired):
    p=None
if not p or p.returncode:
    print('EVENTS=unavailable')
else:
    counts=collections.Counter()
    codes=collections.Counter()
    recognized={'publication_http_probe','publication_check_failed','publication_check_stage','publication_check_step_outcome','cloudflare_transport_failed','cloudflare_request_completed','publication_check_resources_retained'}
    for line in p.stdout.splitlines():
        start=line.find('{"timestamp"')
        if start<0:
            continue
        try:
            item=json.loads(line[start:])
        except ValueError:
            continue
        event=item.get('event')
        if event not in recognized:
            continue
        counts[event]+=1
        if event=='publication_http_probe':
            for check in item.get('checks',[]):
                code=check.get('code')
                if isinstance(code,str) and len(code)<50 and code.replace('_','').isalnum():
                    codes[code]+=1
    for name,n in counts.most_common():
        print('EVENT|%s|count=%d'%(name,n))
    for name,n in codes.most_common(12):
        print('PROBE_CODE|%s|count=%d'%(name,n))
PY

phase "Consolidando e finalizando evidencias"
printf '\n=== SUMMARY ===\n'
printf 'No Docker services foram reiniciados, nenhuma verificacao Cloudflare foi disparada, nenhum arquivo de aplicacao foi editado, e nenhuma senha ou token foi consultado para exibicao.\n'
printf 'Retorne somente este relatorio para cruzar DNS, TCP/TLS, host, worker e admin-api.\n'
