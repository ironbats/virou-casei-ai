import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from publication_deployment_support import compose_command, make_installation, read_calls
from sqlalchemy.exc import OperationalError

from viracase.infrastructure.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def load_initializer():
    spec = importlib.util.spec_from_file_location(
        "database_initializer", ROOT / "script/container-initialize.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_launcher(tmp_path, **values):
    root, env = make_installation(tmp_path)
    env.update(values)
    result = subprocess.run(
        ["bash", "script/run-local.sh"],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    commands = [compose_command(call) for call in read_calls(root) if compose_command(call)]
    return result, commands


def test_local_startup_reconciles_database_before_starting_initializers_and_apis(tmp_path):
    result, commands = run_launcher(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    start = commands.index(["up", "-d", "--no-build", "--wait", "--wait-timeout", "300", "postgres"])
    sync = commands.index(["exec", "-T", "--user", "postgres", "postgres", "sh", "-s"])
    services = commands.index(["up", "-d", "--no-build", "--wait", "--wait-timeout", "300"])
    assert commands.index(["build"]) < start < sync < services
    assert not any(command[0] in {"down", "rm", "stop"} for command in commands)


@pytest.mark.parametrize("field,code", [("DOCKER_TEST_POSTGRES_EXIT", 13), ("DOCKER_TEST_DATABASE_EXIT", 19)])
def test_database_failure_stops_before_initializers_and_api_recreation(tmp_path, field, code):
    result, commands = run_launcher(tmp_path, **{field: str(code)})
    assert result.returncode == code
    assert [command for command in commands if command[0] == "up"] == [
        ["up", "-d", "--no-build", "--wait", "--wait-timeout", "300", "postgres"]
    ]
    assert not any(
        command[0] == "run" or "worker" in command and command[0] == "exec" for command in commands
    )
    if field == "DOCKER_TEST_POSTGRES_EXIT":
        assert not any(command[0] == "exec" for command in commands)
        assert next(command for command in commands if command[0] == "logs")[-1] == "postgres"
    else:
        assert "DATABASE_STARTUP_BLOCKED" in result.stderr


@pytest.mark.parametrize("missing", ["POSTGRES_PASSWORD", "APP_DB_PASSWORD"])
def test_missing_password_stops_without_invoking_psql(tmp_path, missing):
    binary = tmp_path / "psql"
    binary.write_text('#!/bin/sh\ntouch "$PSQL_WAS_CALLED"\n')
    binary.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path),
        "POSTGRES_PASSWORD": "synthetic-admin",
        "APP_DB_PASSWORD": "synthetic-app",
        "PSQL_WAS_CALLED": str(tmp_path / "called"),
    }
    env.pop(missing)
    result = subprocess.run(
        [shutil.which("sh"), str(ROOT / "script/reconcile-db-credentials.sh")],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert f"DATABASE_CONFIG_INVALID: {missing}" in result.stderr
    assert not (tmp_path / "called").exists()


@pytest.mark.parametrize(
    "message,code",
    [
        ('password authentication failed for user "postgres"', "DATABASE_AUTH_FAILED"),
        ('database "missing" does not exist', "DATABASE_NOT_FOUND"),
        ("connection refused", "DATABASE_CONNECTION_FAILED"),
    ],
)
@pytest.mark.parametrize("failing_role", ["postgres", "viracase"])
def test_initializer_stops_before_migrations_and_masks_connection_errors(
    monkeypatch, message, code, failing_role
):
    module = load_initializer()
    checked = []
    disposed = []
    commands = []
    secret = "synthetic-secret-must-remain-private"
    config = Settings(_env_file=None, containerized=True, APP_DB_PASSWORD=secret, POSTGRES_PASSWORD=secret)
    config.validate_runtime()

    class Engine:
        def __init__(self, role):
            self.role = role

        def connect(self):
            checked.append(self.role)
            if self.role == failing_role:
                raise OperationalError("SELECT 1", None, Exception(message + " " + secret))
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def exec_driver_sql(self, statement):
            assert statement == "SELECT 1"

        def dispose(self):
            disposed.append(self.role)

    def create_engine(url, **kwargs):
        assert kwargs == {"connect_args": {"connect_timeout": 5}}
        return Engine("postgres" if "//postgres:" in url else "viracase")

    monkeypatch.setattr(module, "settings", lambda: config)
    monkeypatch.setattr(module, "create_engine", create_engine)
    monkeypatch.setattr(module.subprocess, "run", lambda args, **kwargs: commands.append(args))
    monkeypatch.setattr(module.os, "chdir", lambda path: None)
    with pytest.raises(ValueError) as caught:
        module.main()
    assert code in str(caught.value)
    assert f"usuario={failing_role}" in str(caught.value)
    assert secret not in str(caught.value)
    assert disposed == checked
    assert len(commands) == 1 and commands[0][1].endswith("check-migrations.py")


def test_reconciliation_script_has_valid_posix_shell_syntax():
    result = subprocess.run(
        ["sh", "-n", str(ROOT / "script/reconcile-db-credentials.sh")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
