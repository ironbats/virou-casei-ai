import os
import platform
from pathlib import Path

from viracase.infrastructure.publication_logging import safe_text


REMEDIATIONS = {
    "CGROUP_PIDS_EXHAUSTED": "O cgroup registrou bloqueio de criacao de tarefas ou atingiu pids.max. Confira resources_before/resources_after e os limites do worker e ancestrais. Nao remova os limites nem altere o token Cloudflare.",
    "WORKLOAD_LIMIT_FAILED": "O prlimit nao aplicou os limites dentro do sandbox. Confira a imagem e os rlimits herdados; nao execute o build sem contencao.",
    "NAMESPACE_QUOTA_EXCEEDED": "A criacao do namespace excedeu uma quota ou profundidade do kernel. Confira as quotas e processos do executor; nao altere sysctls globais automaticamente.",
    "APPARMOR_PROFILE_MISMATCH": "O worker esta com docker-default, que bloqueia mount. Na raiz do backend, execute bash script/restart-publication.sh; confirme viracase-publication (enforce) no novo diagnostico. Alterar o token Cloudflare nao corrige esta falha local.",
    "MOUNT_PROPAGATION_DENIED": "O Bubblewrap falhou na propagacao de montagem de /. Confira o perfil AppArmor efetivo e os registros DENIED do kernel. Use o perfil dedicado do worker; nao use --privileged nem altere sysctls globais.",
    "APPARMOR_NOT_ENFORCING": "O perfil AppArmor nao esta em enforce. Reaplique o perfil dedicado com script/restart-publication.sh; modo complain nao comprova contencao.",
    "SANDBOX_ARGUMENT_INVALID": "A imagem usa argumentos incompatíveis do Bubblewrap. Recrie o worker com esta correção; --disable-userns exige --unshare-user explícito.",
    "TOOLCHAIN_INCOMPATIBLE": "Recrie a imagem: o Bubblewrap instalado não suporta uma opção obrigatória. Não remova controles de isolamento.",
    "USERNS_SYSCTL_READONLY": "O /proc/sys do container está somente leitura. Use a configuração de publicação fornecida, restrita ao worker, e recrie o container.",
    "PROC_MOUNT_DENIED": "A montagem de /proc foi recusada. Confira a política de system paths do worker e o perfil AppArmor; não use --privileged.",
    "NAMESPACE_DENIED": "Namespaces ou montagens foram recusados. Confira AppArmor, seccomp e a política userns do host; não use --privileged.",
    "SANDBOX_PATH_MISSING": "Um caminho obrigatório não existe na imagem. Recrie o worker e confira a arquitetura e a toolchain.",
    "SANDBOX_TIMEOUT": "O teste do executor excedeu o prazo. Confira pressão de CPU, memória e processos do worker.",
    "SANDBOX_RESOURCE_EXHAUSTED": "A criacao do processo falhou por recursos. Confira rlimits, pids.current/max/events e memoria em resources_before/resources_after. EAGAIN sozinho nao identifica o limite atingido; nao desative a contencao.",
    "SANDBOX_START_FAILED": "Confira a etapa, o código de saída e o stderr sanitizado, usando o identificador de diagnóstico nos logs do worker.",
    "ISOLATION_PROBE_FAILED": "Um controle do teste de isolamento falhou. O executor continua bloqueado; confira os controles reprovados e os logs correlacionados.",
    "PROBE_OUTPUT_INVALID": "O teste de isolamento não retornou JSON válido. Recrie a imagem e confira a toolchain.",
    "TOOLCHAIN_MISSING": "Recrie a imagem do worker; há arquivos ou executáveis obrigatórios ausentes.",
}


def classify_failure(stderr, returncode=None, environment=None, details=None):
    value = safe_text(stderr).lower()
    profile = str((environment or {}).get("apparmor_profile", "")).strip()
    if "failed to make / slave" in value and any(text in value for text in ("permission denied", "operation not permitted")):
        if profile == "docker-default" or profile.startswith("docker-default ("):
            return "APPARMOR_PROFILE_MISMATCH"
        return "MOUNT_PROPAGATION_DENIED"
    if "creating new namespace failed" in value and any(text in value for text in ("enospc", "no space left on device", "nesting depth")):
        return "NAMESPACE_QUOTA_EXCEEDED"
    if value.startswith("prlimit:"):
        return "WORKLOAD_LIMIT_FAILED"
    if "resource temporarily unavailable" in value and any(item.get("code") == "CGROUP_PIDS_LIMIT_REACHED" for item in (details or {}).get("resource_evidence", [])):
        return "CGROUP_PIDS_EXHAUSTED"
    if "--disable-userns requires --unshare-user" in value:
        return "SANDBOX_ARGUMENT_INVALID"
    if any(text in value for text in ("unknown option", "unrecognized option", "invalid option")):
        return "TOOLCHAIN_INCOMPATIBLE"
    if "max_user_namespaces" in value and "read-only" in value:
        return "USERNS_SYSCTL_READONLY"
    if "proc" in value and any(text in value for text in ("mount", "permission")):
        return "PROC_MOUNT_DENIED"
    if any(text in value for text in ("operation not permitted", "no permissions", "permission denied", "user namespaces")):
        return "NAMESPACE_DENIED"
    if any(text in value for text in ("no such file or directory", "can't find source")):
        return "SANDBOX_PATH_MISSING"
    if any(text in value for text in ("out of memory", "cannot allocate memory", "resource temporarily unavailable")) or returncode in (-9, -24, -25):
        return "SANDBOX_RESOURCE_EXHAUSTED"
    return "SANDBOX_START_FAILED"


def runtime_environment():
    result = {"uid": os.getuid(), "gid": os.getgid(), "kernel": platform.release(), "architecture": platform.machine()}
    paths = {
        "apparmor_profile": "/proc/self/attr/current",
        "max_user_namespaces": "/proc/sys/user/max_user_namespaces",
        "unprivileged_userns_clone": "/proc/sys/kernel/unprivileged_userns_clone",
        "apparmor_restrict_unprivileged_userns": "/proc/sys/kernel/apparmor_restrict_unprivileged_userns",
    }
    for name, path in paths.items():
        try:
            result[name] = safe_text(Path(path).read_text().strip(), limit=160)
        except OSError:
            result[name] = "not_exposed"
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"NoNewPrivs", "Seccomp", "CapEff", "CapBnd"}:
                result[key] = value.strip()
    except OSError:
        pass
    return result
