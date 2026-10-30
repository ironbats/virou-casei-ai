#!/usr/bin/env bash
set -Eeuo pipefail
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'ERRO: python3 nao encontrado no host.' >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' 'ERRO: docker nao encontrado no host.' >&2
  exit 2
fi
if [[ "$(id -u)" -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    printf '%s\n' 'ERRO: sudo necessario apenas para captura passiva dos pacotes e leitura das regras de firewall.' >&2
    exit 2
  fi
  printf '%s\n' 'Solicitando sudo somente para observacao de pacotes e regras. Nenhuma regra ou container sera alterado.'
  sudo -v
  runner=(sudo)
else
  runner=()
fi
"${runner[@]}" python3 -u - "$@" <<'PY'
import collections
import concurrent.futures
import datetime
import errno
import http.client
import ipaddress
import json
import os
import pathlib
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback

HOSTS = (
    ("hono", "viroucase-ai-check-256ca8c9727794d0e61629de.felipe-themarkiron.workers.dev"),
    ("fastapi", "viroucase-ai-check-bbba376b77cc7d78321dad8d.felipe-themarkiron.workers.dev"),
    ("control_api", "api.cloudflare.com"),
    ("control_site", "www.cloudflare.com"),
)
PROJECT = "viracaseai"
SERVICES = ("worker", "admin-api")
CLOCK = time.monotonic()
LOCK = threading.Lock()
REPORT = None
TARGETS = {}
CONTAINERS = {}
IFACES = {}
OBSERVATIONS = collections.defaultdict(list)
STOP_CAPTURE = threading.Event()
CAPTURE_PHASE = "preflight"
CAPTURE_PHASE = "preflight"
CAPTURE_READY = threading.Event()
CAPTURE_ERROR = []
CAPTURE_COUNTER = collections.Counter()
CAPTURE_EXAMPLES = collections.defaultdict(list)
MAX_EXAMPLES = 30


def emit(value):
    now = datetime.datetime.now().astimezone().strftime("%H:%M:%S")
    line = f"[{now}][+{int(time.monotonic()-CLOCK)}s] {value}"
    with LOCK:
        print(line, flush=True)
        if REPORT:
            REPORT.write(line + "\n")
            REPORT.flush()


def cmd(args, timeout=12, maxchars=30000):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, (result.stdout or "")[:maxchars], (result.stderr or "")[:1500]
    except FileNotFoundError:
        return 127, "", "command-not-found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:
        return 125, "", type(exc).__name__


def run_observation(title, args, timeout=12, maxchars=30000, maxlines=100):
    rc, out, err = cmd(args, timeout=timeout, maxchars=maxchars)
    emit(f"COMANDO|{title}|rc={rc}|args={args[0]} {args[1] if len(args)>1 else ''}")
    if out:
        for line in out.splitlines()[:maxlines]:
            emit(f"{title}|{line[:380]}")
    if err:
        emit(f"{title}|stderr={err[:300]}")
    if out.count("\n") > maxlines:
        emit(f"{title}|linhas_adicionais={out.count(chr(10))-maxlines}")
    return rc, out


def docker_json(args, timeout=12):
    rc, data, err = cmd(["docker", *args], timeout, maxchars=250000)
    if rc != 0:
        raise RuntimeError(f"docker {args[0]}: rc={rc} stderr={err[:130]}")
    return json.loads(data)


def discover():
    emit("ETAPA 1/6 | Conferindo Docker, interfaces e destinos")
    for name in SERVICES:
        rc, out, err = cmd(["docker", "ps", "--filter", f"label=com.docker.compose.project={PROJECT}", "--filter", f"label=com.docker.compose.service={name}", "--format", "{{.ID}}"], timeout=10)
        ids = [x.strip() for x in out.splitlines() if x.strip()]
        if rc != 0 or len(ids) != 1:
            raise RuntimeError(f"Servico {name} nao encontrado de forma unica. Docker: rc={rc}; encontrados={len(ids)}. {err[:80]}")
        cid = ids[0]
        info = docker_json(["inspect", cid])[0]
        if not info.get("State", {}).get("Running"):
            raise RuntimeError(f"Container {name} nao esta running")
        nets = info.get("NetworkSettings", {}).get("Networks", {})
        candidates = [(key, val) for key, val in nets.items() if key.endswith("_outbound")]
        if len(candidates) != 1:
            raise RuntimeError(f"Servico {name}: esperado exatamente uma rede _outbound; observado={len(candidates)}")
        network_name, net = candidates[0]
        ip = net.get("IPAddress", "")
        ipaddress.IPv4Address(ip)
        CONTAINERS[name] = (cid, network_name, ip)
        emit(f"CONTAINER|service={name}|id={cid[:12]}|outbound_network={network_name}|source_ipv4={ip}|gateway={net.get('Gateway', '')}|apparmor={info.get('AppArmorProfile', '')}")
    netname = CONTAINERS["worker"][1]
    if CONTAINERS["admin-api"][1] != netname:
        raise RuntimeError("Worker e admin-api nao usam a mesma rede outbound")
    net = docker_json(["network", "inspect", netname])[0]
    if net.get("Driver") != "bridge" or net.get("Internal") is not False:
        raise RuntimeError("A rede outbound nao e uma bridge externa padrao")
    nid = net.get("Id", "")
    explicit = net.get("Options", {}).get("com.docker.network.bridge.name", "")
    bridge = explicit or "br-" + nid[:12]
    if bridge not in dict(socket.if_nameindex()).values():
        raise RuntimeError(f"Interface da bridge nao encontrada: {bridge}")
    IFACES["bridge"] = bridge
    emit(f"DOCKER_BRIDGE|interface={bridge}|network={netname}|id={nid[:14]}")
    for key, host in HOSTS:
        try:
            ips = sorted({entry[4][0] for entry in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)})[:2]
        except OSError as exc:
            raise RuntimeError(f"DNS do host {key} nao respondeu: {type(exc).__name__}") from exc
        if not ips or not all(ipaddress.IPv4Address(ip).is_global for ip in ips):
            raise RuntimeError(f"DNS nao retornou IPv4 publico valido para {key}")
        TARGETS[key] = (host, ips)
        emit(f"DNS_FIXADO|target={key}|host={host}|ipv4={','.join(ips)}")
    rc, route, err = cmd(["ip", "-o", "route", "get", TARGETS["hono"][1][0]], timeout=7)
    if rc != 0 or " dev " not in route:
        raise RuntimeError(f"Rota fisica nao identificada: {err[:100]}")
    physical = route.split(" dev ", 1)[1].split()[0]
    if physical not in dict(socket.if_nameindex()).values():
        raise RuntimeError("Interface fisica selecionada nao existe")
    IFACES["physical"] = physical
    emit(f"HOST_UPLINK|interface={physical}|route={route.strip()[:220]}")
    emit("PRECONDICOES|nenhuma interface, conteiner, firewall ou rota foi modificada")


def parse_packet(packet, ifname, pkttype, ipset):
    if len(packet) < 54:
        return None
    etype = struct.unpack_from("!H", packet, 12)[0]
    pos = 14
    while etype in (0x8100, 0x88A8, 0x9100):
        if len(packet) < pos + 4:
            return None
        etype = struct.unpack_from("!H", packet, pos + 2)[0]
        pos += 4
    if etype != 0x0800 or len(packet) < pos + 20:
        return None
    version_ihl = packet[pos]
    if version_ihl >> 4 != 4:
        return None
    iplen = (version_ihl & 15) * 4
    if iplen < 20 or len(packet) < pos + iplen + 20:
        return None
    if packet[pos + 9] != 6:
        return None
    src = socket.inet_ntoa(packet[pos + 12:pos + 16])
    dst = socket.inet_ntoa(packet[pos + 16:pos + 20])
    if src not in ipset and dst not in ipset:
        return None
    off = pos + iplen
    sport, dport = struct.unpack_from("!HH", packet, off)
    if sport != 443 and dport != 443:
        return None
    flags = packet[off + 13]
    if flags & 4:
        flag = "RST"
    elif flags & 2 and flags & 16:
        flag = "SYN_ACK"
    elif flags & 2:
        flag = "SYN"
    elif flags & 1:
        flag = "FIN"
    else:
        return None
    if pkttype == socket.PACKET_OUTGOING:
        direction = "OUTGOING"
    else:
        direction = "INGRESS"
    return ifname, direction, src, sport, dst, dport, flag


def sniff():
    sock = None
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        sock.settimeout(0.35)
        ipset = {ip for _, ips in TARGETS.values() for ip in ips}
        CAPTURE_READY.set()
        while not STOP_CAPTURE.is_set():
            try:
                packet, metadata = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError as exc:
                CAPTURE_ERROR.append(f"RECV:{type(exc).__name__}:{getattr(exc, 'errno', 'unknown')}")
                break
            decoded = parse_packet(packet, metadata[0], metadata[2], ipset)
            if decoded is None:
                continue
            iface, direction, src, sport, dst, dport, flag = decoded
            key = (CAPTURE_PHASE, iface, direction, src, dst, flag)
            CAPTURE_COUNTER[key] += 1
            item = (iface, direction, src, sport, dst, dport, flag)
            if len(CAPTURE_EXAMPLES[(CAPTURE_PHASE, dst if dst in ipset else src, iface, flag)]) < MAX_EXAMPLES:
                CAPTURE_EXAMPLES[(CAPTURE_PHASE, dst if dst in ipset else src, iface, flag)].append(item)
    except Exception as exc:
        CAPTURE_ERROR.append(f"CAPTURE:{type(exc).__name__}:{getattr(exc,'errno','unknown')}")
        CAPTURE_READY.set()
    finally:
        if sock is not None:
            sock.close()


PROBE_CODE = r'''import json,socket,ssl,sys,time,errno,http.client
spec=json.loads(sys.argv[1])
context=spec['context']
def out(event, **kwargs):
 print('PROBE|context='+context+'|'+event+'|'+ '|'.join(k+'='+str(v).replace('|','_').replace('\\n',' ') for k,v in kwargs.items()),flush=True)
for label,host,ips in spec['targets']:
 out('START',target=label,hostname=host)
 for ip in ips:
  start=time.monotonic()
  s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
  s.settimeout(3.5)
  try:
   s.connect((ip,443))
   local=s.getsockname()[0]
   out('TCP_OK',target=label,dest=ip,source=local,duration_ms=int((time.monotonic()-start)*1000))
   if ip==ips[0]:
    try:
     ss=ssl.create_default_context().wrap_socket(s,server_hostname=host)
     s=None
     out('TLS_OK',target=label,dest=ip,version=ss.version())
     for path in (['/','/api/health','/api/__viroucaseai_revision'] if label in ('hono','fastapi') else ['/']):
      try:
       with socket.create_connection((ip,443),3.5) as raw:
        raw.settimeout(4)
        with ssl.create_default_context().wrap_socket(raw,server_hostname=host) as tls:
         tls.settimeout(4)
         tls.sendall(('GET '+path+' HTTP/1.1\r\nHost: '+host+'\r\nAccept: application/json,text/html\r\nConnection: close\r\n\r\n').encode('ascii'))
         response=http.client.HTTPResponse(tls)
         response.begin()
         payload=response.read(65536)
         mime=response.getheader('Content-Type','unknown')[:100]
         fields={'target':label,'path':path,'status':response.status,'content_type':mime,'bytes_read':len(payload)}
         if path.startswith('/api/') and response.status==200:
          try:
           decoded=json.loads(payload)
           fields['json_valid']=True
           fields['json_type']=type(decoded).__name__
           if isinstance(decoded,dict):
            fields['json_key_count']=len(decoded)
            if isinstance(decoded.get('database'),bool):fields['database']=decoded['database']
          except Exception as err:
           fields['json_valid']=False
           fields['json_error']=type(err).__name__
         out('HTTP',**fields)
      except Exception as exc:
       out('HTTP_ERROR',target=label,path=path,error=type(exc).__name__,errno=getattr(exc,'errno',None))
    except Exception as exc:
     out('TLS_ERROR',target=label,dest=ip,error=type(exc).__name__,errno=getattr(exc,'errno',None))
    finally:
     if s is not None:s.close()
     else:ss.close()
   else:s.close()
  except Exception as exc:
   out('TCP_FAIL',target=label,dest=ip,error=type(exc).__name__,errno=getattr(exc,'errno',None),duration_ms=int((time.monotonic()-start)*1000))
   s.close()
 out('DONE',target=label)
'''


def execute_probe(context, container=None):
    spec = json.dumps({"context": context, "targets": [(key, host, ips) for key, (host, ips) in TARGETS.items()]})
    argv = ["docker", "exec", container, "python", "-u", "-c", PROBE_CODE, spec] if container else [sys.executable, "-u", "-c", PROBE_CODE, spec]
    try:
        process = subprocess.Popen(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1)
    except Exception as exc:
        emit(f"ERRO_EXECUCAO|context={context}|type={type(exc).__name__}")
        OBSERVATIONS[context].append("EXECUTION_FAILED")
        return
    started = time.monotonic()
    timer = threading.Timer(100, process.kill)
    timer.daemon = True
    timer.start()
    try:
        for line in process.stdout:
            line = line.rstrip("\n")
            if line.startswith("PROBE|"):
                emit(line)
                OBSERVATIONS[context].append(line)
            else:
                emit(f"RAW_PROBE|context={context}|{line[:150]}")
            if time.monotonic() - started > 100:
                process.kill()
                raise TimeoutError("Probe excedeu limite de 100 segundos")
        process.wait(timeout=8)
    except Exception as exc:
        process.kill()
        process.wait(timeout=5)
        emit(f"PROBE_TIMEOUT|context={context}|tipo={type(exc).__name__}")
        OBSERVATIONS[context].append("PROCESS_FAILED")
    timer.cancel()
    error = process.stderr.read()[:200].replace("\n", " ")
    if process.returncode != 0:
        emit(f"PROBE_PROCESS_FAILED|context={context}|rc={process.returncode}|stderr={error}")
        OBSERVATIONS[context].append("PROCESS_FAILED")
    emit(f"PROBE_CONTEXT_FINISHED|context={context}|rc={process.returncode}")


def read_firewall(stage):
    emit(f"ETAPA {stage} | Capturando estado de encaminhamento, NAT e firewall (sem alteracoes)")
    run_observation("IP_RULES", ["ip", "rule", "show"], maxlines=35)
    run_observation("HOST_ROUTE", ["ip", "route", "get", TARGETS["hono"][1][0]], maxlines=8)
    for path in ("/proc/sys/net/ipv4/ip_forward", "/proc/sys/net/ipv4/conf/all/rp_filter", "/proc/sys/net/ipv4/conf/default/rp_filter", f"/proc/sys/net/ipv4/conf/{IFACES['bridge']}/rp_filter", f"/proc/sys/net/ipv4/conf/{IFACES['physical']}/rp_filter", "/proc/sys/net/bridge/bridge-nf-call-iptables"):
        try:
            value = pathlib.Path(path).read_text().strip()
        except OSError:
            value = "unavailable"
        emit(f"SYSCTL_READ|{path}={value}")
    for family, chain, table in (("FILTER", "FORWARD", "filter"),("FILTER", "DOCKER-USER", "filter"),("FILTER", "DOCKER-FORWARD", "filter"),("NAT", "POSTROUTING", "nat"),("NAT", "DOCKER", "nat")):
        run_observation(f"IPTABLES_{stage}_{family}_{chain}", ["iptables", "-w", "2", "-t", table, "-nvxL", chain, "--line-numbers"], timeout=8, maxlines=70)
    if stage == "2A/6":
        run_observation("IPTABLES_VERSION", ["iptables", "-V"], maxlines=2)
        rc, out, err = cmd(["nft", "-a", "list", "ruleset"], timeout=9, maxchars=180000)
        emit(f"NFT_RULESET|rc={rc}|bytes_lidos={len(out)}")
        if rc == 0:
            lines = out.splitlines()
            tokens = ("hook forward", "hook postrouting", "chain DOCKER", "chain docker", "drop", "reject", "masquerade", "dnat", "snat", "iifname", "oifname", "ufw", "firewalld", *(ip for _, ips in TARGETS.values() for ip in ips))
            emitted = 0
            for idx, line in enumerate(lines):
                if any(token.lower() in line.lower() for token in tokens):
                    emit(f"NFT_RELEVANTE|line={idx+1}|{line[:400]}")
                    emitted += 1
                    if emitted >= 160:
                        emit("NFT_RELEVANTE|truncado_em_160_linhas")
                        break
        elif err:
            emit(f"NFT_RULESET|stderr={err[:120]}")
        run_observation("UFW", ["ufw", "status", "verbose"], maxlines=40)


def print_captured():
    emit("ETAPA 5/6 | Evidencias dos pacotes TCP observados no kernel do host")
    if CAPTURE_ERROR:
        for err in CAPTURE_ERROR:
            emit(f"CAPTURA_FALHOU|{err}")
        return
    emit(f"CAPTURE_OK|events={sum(CAPTURE_COUNTER.values())}|interfaces={len(set(k[1] for k in CAPTURE_COUNTER))}")
    for key, count in sorted(CAPTURE_COUNTER.items(), key=lambda item: (item[0][0],item[0][4],item[0][1],item[0][5])):
        phase, iface, direction, src, dst, flag = key
        emit(f"PACOTE_CONTAGEM|phase={phase}|if={iface}|direction={direction}|src={src}|dst={dst}|flags={flag}|count={count}")
    emitted = 0
    for key, items in sorted(CAPTURE_EXAMPLES.items()):
        for iface, direction, src, sport, dst, dport, flag in items[:4]:
            emit(f"PACOTE_EXEMPLO|phase={key[0]}|if={iface}|direction={direction}|{src}:{sport}->{dst}:{dport}|flags={flag}")
            emitted += 1
            if emitted >= 110:
                emit("PACOTE_EXEMPLO|truncado_em_110")
                return


def summarize():
    emit("ETAPA 6/6 | Matriz de resultados e criterios para localizar a causa raiz")
    for label, (host, ips) in TARGETS.items():
        for ip in ips:
            for phase in ("host", "containers"):
                uplink_syn = sum(c for (p, iface, direction, src, dst, flags), c in CAPTURE_COUNTER.items() if p == phase and dst == ip and flags == "SYN" and iface == IFACES["physical"])
                bridge_syn = sum(c for (p, iface, direction, src, dst, flags), c in CAPTURE_COUNTER.items() if p == phase and dst == ip and flags == "SYN" and iface == IFACES["bridge"])
                uplink_reply = sum(c for (p, iface, direction, src, dst, flags), c in CAPTURE_COUNTER.items() if p == phase and src == ip and flags in ("SYN_ACK", "RST") and iface == IFACES["physical"])
                bridge_reply = sum(c for (p, iface, direction, src, dst, flags), c in CAPTURE_COUNTER.items() if p == phase and src == ip and flags in ("SYN_ACK", "RST") and iface == IFACES["bridge"])
                emit(f"CAMINHO_PACOTES|phase={phase}|target={label}|ip={ip}|bridge_syn={bridge_syn}|uplink_syn={uplink_syn}|uplink_reply={uplink_reply}|bridge_reply={bridge_reply}")
    emit("INTERPRETACAO|bridge SYN e uplink sem SYN durante probes => verificar FORWARD/NAT/politicas do host")
    emit("INTERPRETACAO|uplink SYN presente e nenhuma resposta => verificar firewall externo, roteador ou bloqueio upstream")
    emit("INTERPRETACAO|uplink SYN-ACK presente e bridge sem retorno => investigar conntrack/NAT/filtro de retorno")
    emit("INTERPRETACAO|TCP OK e TLS/HTTP falham => investigar SNI/TLS/HTTP da aplicacao")
    emit("ALERTA|captura no host pode conter duplicatas de interfaces; comparar horario e tuple TCP dos pacotes antes de concluir")
    emit("POLITICA|nenhum arquivo do backend, container, interface, firewall, sysctl ou recurso Cloudflare foi alterado")


def main():
    global REPORT, CAPTURE_PHASE
    if os.geteuid() != 0:
        raise RuntimeError("Captura de pacotes exige sudo")
    if sys.platform != "linux" or not hasattr(socket, "AF_PACKET"):
        raise RuntimeError("Requer host Linux com AF_PACKET")
    fd, name = tempfile.mkstemp(prefix="viroucase-egress-forense-v69.", suffix=".txt", dir="/tmp")
    os.fchmod(fd, 0o600)
    if os.environ.get("SUDO_UID", "").isdigit() and os.environ.get("SUDO_GID", "").isdigit():
        owner = int(os.environ["SUDO_UID"])
        group = int(os.environ["SUDO_GID"])
        if owner > 0:
            os.fchown(fd, owner, group)
    REPORT = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
    emit("INICIO|analise forense e reproducao TCP/443, somente leitura")
    emit(f"RELATORIO|{name}")
    emit("ATENCAO|o coletor solicita sudo para captura passiva de headers TCP; nao escreve regras ou configuracoes")
    try:
        discover()
        read_firewall("2A/6")
        emit("ETAPA 3/6 | Captura de SYN/SYN-ACK nas interfaces Docker e uplink; simulacao concorrente")
        capture = threading.Thread(target=sniff, daemon=True)
        capture.start()
        CAPTURE_READY.wait(timeout=4)
        if CAPTURE_ERROR or not CAPTURE_READY.is_set():
            raise RuntimeError("Captura AF_PACKET nao pode ser inicializada: " + str(CAPTURE_ERROR))
        time.sleep(0.5)
        CAPTURE_PHASE = "host"
        emit("PROBE_FASE|host|baseline isolada: conexoes do Ubuntu")
        execute_probe("host")
        time.sleep(0.4)
        CAPTURE_PHASE = "containers"
        emit("PROBE_FASE|containers|worker e admin-api; sem trafego de teste concorrente do host")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(execute_probe, service, CONTAINERS[service][0]) for service in SERVICES]
            for job in concurrent.futures.as_completed(jobs, timeout=115):
                job.result()
        STOP_CAPTURE.set()
        capture.join(timeout=3)
        print_captured()
        emit("CONNTRACK | Observacoes opcionais de conexoes TCP (consulta somente leitura)")
        if shutil.which("conntrack"):
            for key, (_, ips) in TARGETS.items():
                for ip in ips:
                    run_observation(f"CONNTRACK_{key}_{ip}", ["conntrack", "-L", "-p", "tcp", "--dport", "443", "-d", ip], timeout=7, maxlines=15, maxchars=6000)
        else:
            emit("CONNTRACK|comando_nao_instalado|captura_TCP_e_firewall_disponiveis")
        read_firewall("5A/6")
        summarize()
        if CAPTURE_ERROR or not CAPTURE_COUNTER:
            emit("COLETA_INCOMPLETA|nenhuma captura valida; diagnostico sem evidencias de pacote")
            return 5
        for service in SERVICES:
            if not OBSERVATIONS[service] or "PROCESS_FAILED" in OBSERVATIONS[service] or "EXECUTION_FAILED" in OBSERVATIONS[service]:
                emit(f"COLETA_INCOMPLETA|probe de {service} nao executou corretamente")
                return 5
        emit("COLETA_CONCLUIDA|pacotes observados e probes executados; revisar o caminho de cada destino")
        return 0
    finally:
        STOP_CAPTURE.set()
        emit(f"RELATORIO_FINAL|{name}")
        REPORT.close()


try:
    status = main()
except KeyboardInterrupt:
    emit("INTERRUPCAO|Ctrl+C; nenhuma configuracao alterada")
    status = 130
except Exception as exc:
    emit(f"ERRO_COLETA|tipo={type(exc).__name__}|descricao={str(exc)[:240]}")
    status = 5
sys.exit(status)
PY
