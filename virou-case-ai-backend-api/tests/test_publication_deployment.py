import json
import subprocess
from pathlib import Path

import pytest

from publication_deployment_support import compose_command, make_installation, read_calls

ROOT = Path(__file__).resolve().parents[1]


def test_container_exception_is_scoped_to_nonroot_worker_only():
    yaml = pytest.importorskip("yaml", reason="PyYAML for Compose structure checks")
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    for name, service in compose["services"].items():
        assert not service.get("privileged", False)
        options = service.get("security_opt", [])
        assert "seccomp=unconfined" not in options
        assert "apparmor=unconfined" not in options
        assert ("systempaths=unconfined" in options) == (name == "worker")
    worker = compose["services"]["worker"]
    assert worker["cap_drop"] == ["ALL"]
    assert not worker.get("cap_add")
    assert worker["user"] == "${VIRA_RUNTIME_UID:-10001}:${VIRA_RUNTIME_GID:-10001}"
    assert worker["read_only"] is True
    assert "no-new-privileges:true" in worker["security_opt"]
    assert "seccomp=./config/publication-seccomp.json" in worker["security_opt"]
    assert all("docker.sock" not in json.dumps(mount) for mount in worker["volumes"])


def test_default_and_explicit_overlays_pin_only_the_worker_profile():
    yaml = pytest.importorskip("yaml")
    explicit = (ROOT / "compose.publication-apparmor.yaml").read_text()
    assert (ROOT / "compose.override.yaml").read_text() == explicit
    overlay = yaml.safe_load(explicit)
    assert set(overlay["services"]) == {"worker"}
    options = overlay["services"]["worker"]["security_opt"]
    assert "apparmor=viracase-publication" in options
    assert options == ["apparmor=viracase-publication"]
    assert not any(value.endswith("=unconfined") for value in options if not value.startswith("systempaths="))
    profile = (ROOT / "config/publication-apparmor").read_text()
    assert "profile viracase-publication" in profile
    assert "deny /proc/sysrq-trigger" in profile and "deny /sys/**" in profile


@pytest.fixture
def fake_docker_installation(tmp_path):
    return make_installation(tmp_path)


def run_script(installation, name="restart-publication.sh"):
    root, env = installation
    return subprocess.run(["bash", "script/" + name], cwd=root, env=env, capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize("diagnostic_exit", [0, 2])
def test_restart_preserves_storage_identity_and_real_probe_exit(fake_docker_installation, diagnostic_exit):
    root, env = fake_docker_installation
    env["DOCKER_TEST_DIAG_EXIT"] = str(diagnostic_exit)
    result = run_script((root, env))
    assert result.returncode == diagnostic_exit, result.stdout + result.stderr
    calls = read_calls(root)
    up = next(call for call in calls if compose_command(call)[:1] == ["up"])
    assert up["uid"] == "1234" and up["gid"] == "1235"
    assert "--no-deps" in up["args"] and "--force-recreate" in up["args"] and "--no-build" in up["args"]
    assert up["args"][-3:] == ["worker", "admin-api", "public-api"]
    assert any(compose_command(call)[:1] == ["build"] for call in calls)
    assert not any("down" in call["args"] or "rm" in call["args"] or "initialize" in call["args"] for call in calls)
    assert "never-include-in-output" not in result.stdout + result.stderr + json.dumps(calls)
    assert (root / ".env").read_text() == "PRIVATE_INSTALL_SECRET=never-include-in-output\n"


@pytest.mark.parametrize("script", ["restart-publication.sh", "run-local.sh"])
def test_apparmor_is_selected_from_daemon_not_unreadable_host_profiles(fake_docker_installation, script):
    root, env = fake_docker_installation
    env["DOCKER_TEST_SECURITY"] = '["name=apparmor","name=seccomp,profile=builtin"]'
    result = run_script((root, env), script)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = read_calls(root)
    up = next(call for call in calls if compose_command(call)[:1] == ["up"])
    assert "compose.publication-apparmor.yaml" in up["args"]
    assert "SETUP" in (root / "calls.log").read_text()
    assert any(call["args"][:2] == ["inspect", "--format"] and "AppArmorProfile" in call["args"][2] for call in calls)


def test_no_apparmor_host_does_not_receive_profile_or_require_sudo(fake_docker_installation):
    root, env = fake_docker_installation
    result = run_script((root, env))
    assert result.returncode == 0, result.stderr
    for call in read_calls(root):
        assert "compose.override.yaml" not in call["args"]
        assert "compose.publication-apparmor.yaml" not in call["args"]
    assert "SETUP" not in (root / "calls.log").read_text()
    assert "SUDO" not in (root / "calls.log").read_text()


def test_custom_compose_files_keep_policy_as_last_override(fake_docker_installation):
    root, env = fake_docker_installation
    (root / "custom.yaml").write_text("services: {}\n")
    env.update(COMPOSE_FILE="compose.yaml:custom.yaml", DOCKER_TEST_SECURITY='["name=apparmor"]')
    result = run_script((root, env))
    assert result.returncode == 0, result.stderr
    up = next(call for call in read_calls(root) if compose_command(call)[:1] == ["up"])
    assert up["args"][:7] == ["compose", "-f", "compose.yaml", "-f", "custom.yaml", "-f", "compose.publication-apparmor.yaml"]


@pytest.mark.parametrize("script", ["restart-publication.sh", "run-local.sh"])
def test_wrong_actual_profile_cannot_report_success(fake_docker_installation, script):
    root, env = fake_docker_installation
    env.update(DOCKER_TEST_SECURITY='["name=apparmor"]', DOCKER_TEST_PROFILE="docker-default")
    result = run_script((root, env), script)
    assert result.returncode == 1
    assert "APPARMOR_PROFILE_MISMATCH" in result.stderr
    assert "Executor READY" not in result.stdout
    assert not any(compose_command(call)[:1] == ["exec"] for call in read_calls(root))


def test_remote_daemon_never_loads_profile_on_client_host(fake_docker_installation):
    root, env = fake_docker_installation
    env.update(DOCKER_TEST_SECURITY='["name=apparmor"]', DOCKER_HOST="ssh://remote-server")
    result = run_script((root, env))
    assert result.returncode == 0, result.stderr
    assert "SETUP" not in (root / "calls.log").read_text()
    assert "Docker remoto" in result.stderr


def test_context_takes_priority_over_docker_host(fake_docker_installation):
    root, env = fake_docker_installation
    env.update(DOCKER_TEST_SECURITY='["name=apparmor"]', DOCKER_HOST="unix:///var/run/docker.sock", DOCKER_CONTEXT="remote", DOCKER_TEST_HOST="ssh://remote-server")
    result = run_script((root, env))
    assert result.returncode == 0, result.stderr
    assert "SETUP" not in (root / "calls.log").read_text()
    assert any(call["args"][:2] == ["context", "inspect"] for call in read_calls(root))


@pytest.mark.parametrize("variable,value", [("DOCKER_TEST_SETUP_EXIT", "1"), ("DOCKER_TEST_CONFIG_EXIT", "1"), ("DOCKER_TEST_BUILD_EXIT", "1")])
def test_deployment_failures_stop_before_recreating_services(fake_docker_installation, variable, value):
    root, env = fake_docker_installation
    env.update(DOCKER_TEST_SECURITY='["name=apparmor"]')
    env[variable] = value
    result = run_script((root, env))
    assert result.returncode != 0
    assert not any(compose_command(call)[:1] == ["up"] for call in read_calls(root))


def test_missing_env_stops_without_docker_mutations(fake_docker_installation):
    root, env = fake_docker_installation
    (root / ".env").unlink()
    result = run_script((root, env))
    assert result.returncode == 1 and not read_calls(root)


def test_env_compose_file_is_not_silently_ignored(fake_docker_installation):
    root, env = fake_docker_installation
    with (root / ".env").open("a") as stream:
        stream.write("COMPOSE_FILE=compose.yaml:custom.yaml\n")
    result = run_script((root, env))
    assert result.returncode == 1 and "COMPOSE_FILE" in result.stderr
    assert not any(compose_command(call)[:1] == ["up"] for call in read_calls(root))


@pytest.mark.parametrize("user", ["0:0", "00:00", "named-user"])
def test_restart_does_not_replace_identity_with_root_or_guess_a_named_user(fake_docker_installation, user):
    root, env = fake_docker_installation
    env["DOCKER_TEST_USER"] = user
    result = run_script((root, env))
    assert result.returncode == 1
    assert not any(compose_command(call)[:1] == ["up"] for call in read_calls(root))


@pytest.mark.parametrize("script", ["publication-runtime.sh", "restart-publication.sh", "run-docker.sh", "setup-publication-host.sh"])
def test_scripts_have_valid_bash_syntax(script):
    result = subprocess.run(["bash", "-n", str(ROOT / "script" / script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_profile_installer_validates_and_loads_before_persisting():
    source = (ROOT / "script/setup-publication-host.sh").read_text()
    assert source.index("publication_apparmor_render config/publication-apparmor") < source.index('apparmor_parser -r -T -K "$staged"') < source.index('mv -f "$staged" "$profile"')
    helper = (ROOT / "script/publication-apparmor-policy.sh").read_text()
    assert 'apparmor_parser -Q -T -K "$PUBLICATION_APPARMOR_POLICY"' in helper
    assert "(enforce)" in source
    assert "systemctl" not in source and "sysctl -w" not in source
