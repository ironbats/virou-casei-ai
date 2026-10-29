import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
OPERATION_ID = "ce81e74c-8604-477b-95ff-ff2f703bf730"
pytestmark = pytest.mark.skipif(BASH is None, reason="Bash is required for the launcher contract")


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "backend installation with spaces"
    scripts = root / "script"
    binaries = root / "bin"
    scripts.mkdir(parents=True)
    binaries.mkdir()
    shutil.copy(ROOT / "script/recover-publication.sh", scripts)
    os.symlink(shutil.which("dirname"), binaries / "dirname")
    docker = binaries / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "entry = {'args': sys.argv[1:], 'cwd': os.getcwd(), 'environment': "
        "{name: os.environ.get(name) for name in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'COMPOSE_FILE', 'COMPOSE_PROJECT_NAME')}}\n"
        "with Path(os.environ['RECOVERY_LAUNCHER_TEST_LOG']).open('a') as stream:\n"
        "    stream.write(json.dumps(entry) + '\\n')\n"
        "print('ADMIN_RECOVERY_RESULT')\n"
        "raise SystemExit(int(os.environ.get('RECOVERY_LAUNCHER_TEST_EXIT', '0')))\n"
    )
    docker.chmod(0o755)
    log = root / "docker-calls.jsonl"
    env = {**os.environ, "PATH": str(binaries), "RECOVERY_LAUNCHER_TEST_LOG": str(log)}
    for name in ("BASH_ENV", "ENV", "DOCKER_HOST", "DOCKER_CONTEXT", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME"):
        env.pop(name, None)
    return root, env


def invoke(installation, *arguments):
    root, env = installation
    return subprocess.run(
        [BASH, str(root / "script/recover-publication.sh"), *arguments],
        cwd=root.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def calls(installation):
    path = Path(installation[1]["RECOVERY_LAUNCHER_TEST_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_launches_only_the_installed_admin_command_and_preserves_interactive_exec(installation):
    result = invoke(installation, OPERATION_ID)
    assert result.returncode == 0, result.stderr
    assert calls(installation) == [{
        "args": ["compose", "exec", "admin-api", "python", "script/recover-publication-local.py", OPERATION_ID],
        "cwd": str(installation[0]),
        "environment": {name: None for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME")},
    }]
    assert "ADMIN_RECOVERY_RESULT" in result.stdout


@pytest.mark.parametrize("arguments", [
    [OPERATION_ID, "--inspect"],
    ["--inspect", OPERATION_ID],
    [OPERATION_ID, "--login", "admin local", "--reason", "Conferência estrutural autorizada."],
    ["--login=admin", "--reason=Conferência estrutural autorizada.", OPERATION_ID],
    ["--", OPERATION_ID],
    [OPERATION_ID.upper()],
])
def test_preserves_argument_boundaries_without_shell_evaluation(installation, arguments):
    result = invoke(installation, *arguments)
    assert result.returncode == 0, result.stderr
    assert calls(installation)[0]["args"][5:] == arguments


@pytest.mark.parametrize("arguments", [
    [],
    ["not-a-uuid"],
    ["ce81e74c8604477b95ffff2f703bf730"],
    [OPERATION_ID + "; exit 0"],
    [OPERATION_ID + "\n"],
    ["../" + OPERATION_ID],
    [OPERATION_ID, OPERATION_ID],
    [OPERATION_ID, "--force"],
    [OPERATION_ID, "--yes"],
    [OPERATION_ID, "--password", "do-not-echo-this"],
    [OPERATION_ID, "--inspect=true"],
    [OPERATION_ID, "--login"],
    [OPERATION_ID, "--login", ""],
    [OPERATION_ID, "--login="],
    [OPERATION_ID, "--reason"],
    [OPERATION_ID, "--reason", "--inspect"],
    [OPERATION_ID, "--reason="],
])
def test_rejects_invalid_input_before_any_docker_call_without_echoing_it(installation, arguments):
    result = invoke(installation, *arguments)
    assert result.returncode == 2
    assert "ARGUMENTS_INVALID" in result.stderr
    assert "do-not-echo-this" not in result.stdout + result.stderr
    assert calls(installation) == []


@pytest.mark.parametrize("argument", ["--help", "-h"])
def test_help_works_without_docker_or_an_existing_env(installation, argument):
    (installation[0] / "bin/docker").unlink()
    result = invoke(installation, argument)
    assert result.returncode == 0
    assert "ENCERRAR UUID" in result.stdout
    assert "--inspect" in result.stdout
    assert calls(installation) == []
    assert not (installation[0] / ".env").exists()


def test_missing_docker_is_reported_without_fallback_or_mutation(installation):
    (installation[0] / "bin/docker").unlink()
    result = invoke(installation, OPERATION_ID)
    assert result.returncode == 3
    assert "DOCKER_UNAVAILABLE" in result.stderr
    assert calls(installation) == []


@pytest.mark.parametrize("exit_code", [0, 2, 3, 4, 42, 130])
def test_propagates_admin_command_exit_status(installation, exit_code):
    installation[1]["RECOVERY_LAUNCHER_TEST_EXIT"] = str(exit_code)
    result = invoke(installation, OPERATION_ID)
    assert result.returncode == exit_code
    assert len(calls(installation)) == 1


def test_preserves_selected_compose_context_and_host_without_printing_them(installation):
    selected = {
        "DOCKER_CONTEXT": "selected-context",
        "DOCKER_HOST": "ssh://selected-host.invalid",
        "COMPOSE_FILE": "compose.yaml:override with spaces.yaml",
        "COMPOSE_PROJECT_NAME": "selected-project",
    }
    installation[1].update(selected)
    result = invoke(installation, OPERATION_ID)
    assert result.returncode == 0
    assert calls(installation)[0]["environment"] == selected
    assert all(value not in result.stdout + result.stderr for value in selected.values())


def test_does_not_source_env_prepare_apparmor_or_mutate_installation(installation):
    root, _ = installation
    marker = root / "env-was-executed"
    env_bytes = f"PRIVATE_SECRET=must-not-be-printed\n$(touch '{marker}')\n".encode()
    (root / ".env").write_bytes(env_bytes)
    (root / "script/publication-runtime.sh").write_text("exit 98\n")
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    result = invoke(installation, OPERATION_ID)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert (root / ".env").read_bytes() == env_bytes
    assert "must-not-be-printed" not in result.stdout + result.stderr
    assert all((root / name).read_bytes() == value for name, value in before.items())
    assert len(calls(installation)) == 1


def test_reason_is_transferred_as_one_literal_argument(installation):
    marker = installation[0] / "argument-was-executed"
    reason = f"Conferência $(touch '{marker}') `touch '{marker}'`; exit 99"
    result = invoke(installation, OPERATION_ID, "--reason", reason)
    assert result.returncode == 0, result.stderr
    assert calls(installation)[0]["args"][-1] == reason
    assert not marker.exists()
