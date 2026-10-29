import json
import re
import shlex
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from publication_deployment_support import compose_command, make_installation, read_calls

SCRIPTS = ("run-docker.sh", "run-local.sh", "restart-publication.sh")
DNS_ERROR = (
    "target worker: failed to solve: python:3.12-slim: failed to resolve source metadata for "
    "docker.io/library/python:3.12-slim: failed to do request: Head "
    '"https://registry-1.docker.io/v2/library/python/manifests/3.12-slim": dial tcp: '
    "lookup registry-1.docker.io on 127.0.0.53:53: server misbehaving"
)


def run_script(root, env, script):
    return subprocess.run(
        ["bash", "script/" + script], cwd=root, env=env,
        capture_output=True, text=True, timeout=20, check=False,
    )


def commands(root):
    return [compose_command(call) for call in read_calls(root) if compose_command(call)]


def assert_no_startup(root):
    forbidden = {"up", "run", "exec", "logs", "down", "rm", "stop", "restart"}
    assert not any(command[0] in forbidden for command in commands(root))


def add_storage_sentinels(root):
    files = {
        root / ".env": (root / ".env").read_bytes(),
        root / "var/sources/project.py": b"existing project source\n",
        root / "var/secrets/credentials.key": b"synthetic preserved credential sentinel\n",
        root / "var/postgres-data/existing-row": b"synthetic existing database sentinel\n",
    }
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return files


def assert_sentinels_preserved(files, result, root):
    assert all(path.read_bytes() == content for path, content in files.items())
    visible = result.stdout + result.stderr + json.dumps(read_calls(root))
    assert "never-include-in-output" not in visible
    assert "synthetic preserved credential sentinel" not in visible
    assert "synthetic existing database sentinel" not in visible


@pytest.mark.parametrize("script", SCRIPTS)
def test_dns_build_failure_does_not_start_services_or_show_old_initializer_logs(tmp_path, script):
    root, env = make_installation(tmp_path)
    sentinels = add_storage_sentinels(root)
    env.update(
        DOCKER_TEST_BUILD_EXIT="7", DOCKER_TEST_BUILD_STDERR=DNS_ERROR,
        DOCKER_TEST_UP_EXIT="7", DOCKER_TEST_UP_STDERR=DNS_ERROR,
        DOCKER_TEST_LOGS_STDOUT="initialize-1 | STALE_INITIALIZER_SUCCESS",
    )
    result = run_script(root, env, script)
    assert result.returncode == 7, result.stdout + result.stderr
    assert DNS_ERROR in result.stdout + result.stderr
    assert "DOCKER_BUILD_DNS_FAILED" in result.stderr
    assert "STALE_INITIALIZER_SUCCESS" not in result.stdout + result.stderr
    assert "Inicializacao incompleta" not in result.stdout + result.stderr
    assert "Executor READY" not in result.stdout
    assert "Executor local READY" not in result.stdout
    assert_no_startup(root)
    assert_sentinels_preserved(sentinels, result, root)


@pytest.mark.parametrize("script", SCRIPTS)
def test_build_success_is_required_before_no_build_startup(tmp_path, script):
    root, env = make_installation(tmp_path)
    result = run_script(root, env, script)
    assert result.returncode == 0, result.stdout + result.stderr
    executed = commands(root)
    build_at = next(i for i, command in enumerate(executed) if command[0] == "build")
    up_at = next(i for i, command in enumerate(executed) if command[0] == "up")
    assert build_at < up_at
    assert "--no-build" in executed[up_at]
    assert "--build" not in executed[up_at]
    assert executed[build_at] == (
        ["build", "worker", "admin-api", "public-api"]
        if script == "restart-publication.sh" else ["build"]
    )
    assert not any(command[0] in {"down", "rm", "stop"} for command in executed)
    if script == "restart-publication.sh":
        assert "--no-deps" in executed[up_at] and "--force-recreate" in executed[up_at]
        assert executed[up_at][-3:] == ["worker", "admin-api", "public-api"]
        assert not any(command[0] == "run" for command in executed)
    else:
        assert "--wait-timeout" in executed[up_at]
        assert executed[up_at][executed[up_at].index("--wait-timeout") + 1] == "300"
        credential_at = next(i for i, command in enumerate(executed) if command[0] == "run")
        assert up_at < credential_at
        assert executed[credential_at] == [
            "run", "--rm", "--no-deps", "initialize", "python", "script/bootstrap-admin.py", "--check"
        ]
    assert any(command[0] == "exec" for command in executed)


@pytest.mark.parametrize(
    "error",
    [
        "ERROR: Could not find a version that satisfies the requirement missing-package==9999",
        "failed to solve: process pip install did not complete successfully: exit code: 1",
        "failed to resolve source metadata: x509: certificate signed by unknown authority",
        "unauthorized: authentication required",
        "toomanyrequests: You have reached your pull rate limit",
    ],
)
def test_other_build_failures_keep_the_real_error_without_dns_misclassification(tmp_path, error):
    root, env = make_installation(tmp_path)
    env.update(DOCKER_TEST_BUILD_EXIT="9", DOCKER_TEST_BUILD_STDERR=error)
    result = run_script(root, env, "run-docker.sh")
    assert result.returncode == 9
    assert error in result.stdout + result.stderr
    assert "DOCKER_BUILD_FAILED" in result.stderr
    assert "DOCKER_BUILD_DNS_FAILED" not in result.stderr
    assert_no_startup(root)


def test_recovered_dns_warning_does_not_replace_a_later_unrelated_build_failure(tmp_path):
    root, env = make_installation(tmp_path)
    warning = "warning: lookup registry-1.docker.io on 127.0.0.53:53: server misbehaving; retry succeeded"
    fatal = "failed to solve: process pip install did not complete successfully: exit code: 1"
    env.update(DOCKER_TEST_BUILD_EXIT="9", DOCKER_TEST_BUILD_STDOUT=warning, DOCKER_TEST_BUILD_STDERR=fatal)
    result = run_script(root, env, "run-docker.sh")
    assert result.returncode == 9
    assert warning in result.stdout + result.stderr and fatal in result.stdout + result.stderr
    assert "DOCKER_BUILD_FAILED" in result.stderr
    assert "DOCKER_BUILD_DNS_FAILED" not in result.stderr
    assert_no_startup(root)


@pytest.mark.parametrize("reason", ["no such host", "i/o timeout"])
def test_fatal_docker_lookup_failure_is_reported_as_dns(tmp_path, reason):
    root, env = make_installation(tmp_path)
    error = DNS_ERROR.replace("server misbehaving", reason)
    env.update(DOCKER_TEST_BUILD_EXIT="7", DOCKER_TEST_BUILD_STDERR=error)
    result = run_script(root, env, "run-docker.sh")
    assert result.returncode == 7
    assert error in result.stdout + result.stderr
    assert "DOCKER_BUILD_DNS_FAILED" in result.stderr
    assert_no_startup(root)


@pytest.mark.parametrize("script", SCRIPTS)
def test_startup_failure_uses_current_attempt_logs_and_preserves_original_exit(tmp_path, script):
    root, env = make_installation(tmp_path)
    env.update(
        DOCKER_TEST_UP_EXIT="17", DOCKER_TEST_UP_STDERR="CURRENT_STARTUP_FAILURE",
        DOCKER_TEST_LOGS_STDOUT="CURRENT_INITIALIZER_DIAGNOSTIC", DOCKER_TEST_LOGS_EXIT="23",
    )
    before = datetime.now(UTC).replace(microsecond=0)
    result = run_script(root, env, script)
    after = datetime.now(UTC)
    assert result.returncode == 17
    assert "CURRENT_STARTUP_FAILURE" in result.stdout + result.stderr
    assert "DOCKER_STARTUP_FAILED" in result.stderr
    assert "DOCKER_BUILD_DNS_FAILED" not in result.stderr
    executed = commands(root)
    log_commands = [command for command in executed if command[0] == "logs"]
    assert len(log_commands) == 1
    log_command = log_commands[0]
    assert "--since" in log_command and "--no-color" in log_command and "--tail=120" in log_command
    since = log_command[log_command.index("--since") + 1]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", since)
    assert before <= datetime.fromisoformat(since.replace("Z", "+00:00")) <= after
    expected_services = (
        {"worker", "admin-api", "public-api"}
        if script == "restart-publication.sh" else {"initialize", "storage-init"}
    )
    assert set(log_command[-len(expected_services):]) == expected_services
    assert not any(command[0] in {"run", "exec", "down", "rm", "stop"} for command in executed)


@pytest.mark.parametrize("script", SCRIPTS)
def test_build_error_output_is_data_and_never_evaluated_as_shell(tmp_path, script):
    root, env = make_installation(tmp_path)
    payload = "build failed: $(touch INJECTED_DOLLAR); `touch INJECTED_BACKTICK`; ${PRIVATE_INSTALL_SECRET}"
    env.update(DOCKER_TEST_BUILD_EXIT="3", DOCKER_TEST_BUILD_STDOUT=payload)
    result = run_script(root, env, script)
    assert result.returncode == 3
    assert payload in result.stdout + result.stderr
    assert not (root / "INJECTED_DOLLAR").exists()
    assert not (root / "INJECTED_BACKTICK").exists()
    assert "never-include-in-output" not in result.stdout + result.stderr
    assert_no_startup(root)


def install_observed_tee(root, env):
    real_tee = shutil.which("tee")
    assert real_tee
    tee = root / "bin/tee"
    tee.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + shlex.quote(real_tee) + ' "$@"\n'
        + 'output="${!#}"\n'
        + 'printf "%s\\n" "$output" >> "$DOCKER_TEST_TEE_OBSERVATION"\n'
        + 'stat -c "%a" "$output" >> "$DOCKER_TEST_TEE_OBSERVATION"\n'
        + 'stat -c "%a" "$(dirname "$output")" >> "$DOCKER_TEST_TEE_OBSERVATION"\n'
        + 'exit "${DOCKER_TEST_TEE_EXIT:-0}"\n'
    )
    tee.chmod(0o755)
    temporary = root / "build-tmp"
    temporary.mkdir()
    observation = root / "tee-observation.log"
    env.update(TMPDIR=str(temporary), DOCKER_TEST_TEE_OBSERVATION=str(observation))
    return temporary, observation


@pytest.mark.parametrize("build_exit", [0, 7])
def test_build_logs_are_private_and_removed_after_success_or_failure(tmp_path, build_exit):
    root, env = make_installation(tmp_path)
    temporary, observation = install_observed_tee(root, env)
    env.update(DOCKER_TEST_BUILD_EXIT=str(build_exit), DOCKER_TEST_BUILD_STDOUT="synthetic build output")
    result = run_script(root, env, "run-docker.sh")
    assert result.returncode == build_exit, result.stdout + result.stderr
    output, file_mode, directory_mode = observation.read_text().splitlines()
    assert file_mode == "600"
    assert directory_mode == "700"
    assert not Path(output).exists()
    assert not Path(output).parent.exists()
    assert not list(temporary.iterdir())


@pytest.mark.parametrize("build_exit,expected_exit", [(0, 1), (7, 7)])
def test_logging_failure_blocks_startup_without_hiding_docker_exit(tmp_path, build_exit, expected_exit):
    root, env = make_installation(tmp_path)
    temporary, observation = install_observed_tee(root, env)
    env.update(DOCKER_TEST_TEE_EXIT="33", DOCKER_TEST_BUILD_EXIT=str(build_exit))
    result = run_script(root, env, "run-docker.sh")
    assert result.returncode == expected_exit
    if not build_exit:
        assert "DOCKER_BUILD_LOG_FAILED" in result.stderr
    assert observation.exists()
    assert not list(temporary.iterdir())
    assert_no_startup(root)


def test_temporary_log_creation_failure_stops_before_build(tmp_path):
    root, env = make_installation(tmp_path)
    mktemp = root / "bin/mktemp"
    mktemp.write_text("#!/usr/bin/env bash\nexit 41\n")
    mktemp.chmod(0o755)
    result = run_script(root, env, "run-docker.sh")
    assert result.returncode == 1
    assert "DOCKER_BUILD_LOG_FAILED" in result.stderr
    assert not any(command[0] == "build" for command in commands(root))
    assert_no_startup(root)


@pytest.mark.parametrize("script", SCRIPTS)
def test_build_and_startup_keep_existing_nonroot_identity_and_custom_overlay(tmp_path, script):
    root, env = make_installation(tmp_path)
    sentinels = add_storage_sentinels(root)
    (root / "custom.yaml").write_text("services: {}\n")
    env.update(
        COMPOSE_FILE="compose.yaml:custom.yaml", DOCKER_TEST_SECURITY='["name=apparmor"]',
        DOCKER_TEST_USER="1839257876:1839257877",
    )
    result = run_script(root, env, script)
    assert result.returncode == 0, result.stdout + result.stderr
    for call in read_calls(root):
        command = compose_command(call)
        if command and command[0] in {"build", "up", "run", "exec"}:
            assert call["uid"] == "1839257876" and call["gid"] == "1839257877"
            assert call["args"][:7] == [
                "compose", "-f", "compose.yaml", "-f", "custom.yaml", "-f", "compose.publication-apparmor.yaml"
            ]
    assert_sentinels_preserved(sentinels, result, root)
