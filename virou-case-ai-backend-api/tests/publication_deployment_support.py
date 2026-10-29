import os
import shlex
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def make_installation(tmp_path):
    root = tmp_path / "backend"
    (root / "script").mkdir(parents=True)
    (root / "bin").mkdir()
    for name in ("restart-publication.sh", "run-docker.sh", "run-local.sh", "publication-runtime.sh"):
        shutil.copy(ROOT / "script" / name, root / "script" / name)
    for name in ("compose.yaml", "compose.override.yaml", "compose.publication-apparmor.yaml"):
        shutil.copy(ROOT / name, root / name)
    (root / ".env").write_text("PRIVATE_INSTALL_SECRET=never-include-in-output\n")
    (root / "script/setup-publication-host.sh").write_text(
        '#!/usr/bin/env bash\nprintf "SETUP\\n" >> "$DOCKER_TEST_LOG"\nexit "${DOCKER_TEST_SETUP_EXIT:-0}"\n'
    )
    docker = root / "bin/docker"
    docker.write_text(r'''#!/usr/bin/env bash
set -eu
{
  printf 'CALL'
  printf ' %q' "$@"
  printf '\nIDENTITY %s %s\n' "${VIRA_RUNTIME_UID:-unset}" "${VIRA_RUNTIME_GID:-unset}"
} >> "$DOCKER_TEST_LOG"
case "$1" in
  info)
    printf '%s\n' "${DOCKER_TEST_SECURITY:-[\"name=seccomp\"]}"
    exit "${DOCKER_TEST_INFO_EXIT:-0}"
    ;;
  context)
    printf '%s\n' "${DOCKER_TEST_HOST:-unix:///var/run/docker.sock}"
    ;;
  inspect)
    if [[ "$3" == *Config.User* ]]; then
      printf '%s\n' "${DOCKER_TEST_USER:-1234:1235}"
    elif [[ "$3" == *AppArmorProfile* ]]; then
      printf '%s\n' "${DOCKER_TEST_PROFILE:-viracase-publication}"
    fi
    ;;
  compose)
    shift
    while [[ "${1:-}" == -f ]]; do shift 2; done
    case "${1:-}" in
      ps) printf 'existing-worker\n' ;;
      config) exit "${DOCKER_TEST_CONFIG_EXIT:-0}" ;;
      build)
        [[ -z "${DOCKER_TEST_BUILD_STDOUT:-}" ]] || printf '%s\n' "$DOCKER_TEST_BUILD_STDOUT"
        [[ -z "${DOCKER_TEST_BUILD_STDERR:-}" ]] || printf '%s\n' "$DOCKER_TEST_BUILD_STDERR" >&2
        exit "${DOCKER_TEST_BUILD_EXIT:-0}"
        ;;
      up)
        [[ -z "${DOCKER_TEST_UP_STDERR:-}" ]] || printf '%s\n' "$DOCKER_TEST_UP_STDERR" >&2
        exit "${DOCKER_TEST_UP_EXIT:-0}"
        ;;
      logs)
        [[ -z "${DOCKER_TEST_LOGS_STDOUT:-}" ]] || printf '%s\n' "$DOCKER_TEST_LOGS_STDOUT"
        exit "${DOCKER_TEST_LOGS_EXIT:-0}"
        ;;
      run) exit "${DOCKER_TEST_CHECK_EXIT:-0}" ;;
      exec)
        code="${DOCKER_TEST_DIAG_EXIT:-0}"
        if [[ "$code" == 0 ]]; then
          printf '{"available":true,"code":"READY"}\n'
        else
          printf '{"available":false,"code":"NAMESPACE_DENIED"}\n'
        fi
        exit "$code"
        ;;
    esac
    ;;
esac
''')
    docker.chmod(0o755)
    sudo = root / "bin/sudo"
    sudo.write_text('#!/usr/bin/env bash\nprintf "SUDO\\n" >> "$DOCKER_TEST_LOG"\nexec "$@"\n')
    sudo.chmod(0o755)
    env = {**os.environ, "PATH": str(root / "bin") + ":" + os.environ["PATH"], "DOCKER_TEST_LOG": str(root / "calls.log")}
    for key in ("VIRA_RUNTIME_UID", "VIRA_RUNTIME_GID", "COMPOSE_FILE", "COMPOSE_PATH_SEPARATOR", "DOCKER_HOST", "DOCKER_CONTEXT"):
        env.pop(key, None)
    return root, env


def read_calls(root):
    path = root / "calls.log"
    calls = []
    if not path.exists():
        return calls
    for line in path.read_text().splitlines():
        if line.startswith("CALL "):
            calls.append({"args": shlex.split(line[5:])})
        elif line.startswith("IDENTITY "):
            _, uid, gid = line.split()
            calls[-1].update(uid=uid, gid=gid)
    return calls


def compose_command(call):
    args = call["args"][:]
    if not args or args.pop(0) != "compose":
        return []
    while args and args[0] == "-f":
        args = args[2:]
    return args
