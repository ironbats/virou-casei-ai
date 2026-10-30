import importlib.util
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from viracase.infrastructure.config import Settings

ROOT = Path(__file__).resolve().parents[1]
PG_BIN = Path(os.environ["VIRA_TEST_POSTGRES_BIN"]) if os.getenv("VIRA_TEST_POSTGRES_BIN") else None
pytestmark = pytest.mark.skipif(
    PG_BIN is None, reason="Defina VIRA_TEST_POSTGRES_BIN para PostgreSQL real isolado."
)
ADMIN_OLD = "synthetic-admin-old"
APP_OLD = "synthetic-app-old"
ADMIN_NEW = "synthetic-admin-'$@:%\\; SELECT 7; --"
APP_NEW = "synthetic-app-'$@:%\\; SELECT 8; --"


def run(args, *, env=None, cwd=None, timeout=60):
    return subprocess.run(
        args, env=env, cwd=cwd, check=False, capture_output=True, text=True, timeout=timeout
    )


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    if os.geteuid() == 0:
        pytest.skip("Execute os testes nativos de PostgreSQL com usuario nao root.")
    root = tmp_path_factory.mktemp("postgres-startup")
    data = root / "data"
    sockets = root / "socket"
    sockets.mkdir()
    password = root / "password"
    password.write_text(ADMIN_OLD)
    password.chmod(0o600)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    result = run(
        [
            str(PG_BIN / "initdb"),
            "-D",
            str(data),
            "-U",
            "postgres",
            "--locale=C",
            "--encoding=UTF8",
            "--auth-local=trust",
            "--auth-host=scram-sha-256",
            "--pwfile=" + str(password),
        ]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    logfile = root / "postgres.log"
    options = shlex.join(
        [
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
            "-k",
            str(sockets),
            "-c",
            "log_statement=all",
        ]
    )
    result = run([str(PG_BIN / "pg_ctl"), "-D", str(data), "-l", str(logfile), "-o", options, "-w", "start"])
    assert result.returncode == 0, result.stdout + result.stderr
    try:
        yield {"root": root, "data": data, "host": str(sockets), "port": port, "log": logfile}
    finally:
        result = run([str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"])
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def database_cluster(cluster, tmp_path):
    name = "startup_" + uuid4().hex
    with psycopg.connect(
        host=cluster["host"], port=cluster["port"], user="postgres", dbname="postgres", autocommit=True
    ) as conn:
        conn.execute(sql.SQL("ALTER ROLE postgres PASSWORD {} ").format(sql.Literal(ADMIN_OLD)))
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'viracase'").fetchone():
            conn.execute("CREATE ROLE viracase LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        conn.execute(sql.SQL("ALTER ROLE viracase PASSWORD {} ").format(sql.Literal(APP_OLD)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    wrapper = tmp_path / "bin"
    wrapper.mkdir()
    psql = wrapper / "psql"
    psql.write_text(
        "#!"
        + sys.executable
        + "\nimport os, sys\n"
        + "mapping = "
        + repr(
            {
                "--host=postgres": "--host=127.0.0.1",
                "--host=/var/run/postgresql": "--host=" + cluster["host"],
                "--port=5432": "--port=" + str(cluster["port"]),
            }
        )
        + "\n"
        + "binary = "
        + repr(str(PG_BIN / "psql"))
        + "\n"
        + "os.execv(binary, [binary, *[mapping.get(arg, arg) for arg in sys.argv[1:]]])\n"
    )
    psql.chmod(0o755)
    with psycopg.connect(host=cluster["host"], port=cluster["port"], user="postgres", dbname=name) as conn:
        conn.execute("CREATE TABLE startup_sentinel (id integer PRIMARY KEY, content text NOT NULL)")
        conn.execute("INSERT INTO startup_sentinel VALUES (1, 'existing-row-preserved')")
    env = {
        **os.environ,
        "PATH": str(wrapper) + ":" + os.environ["PATH"],
        "POSTGRES_DB": name,
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": ADMIN_NEW,
        "APP_DB_PASSWORD": APP_NEW,
    }
    yield {**cluster, "name": name, "env": env, "work": tmp_path}
    with psycopg.connect(
        host=cluster["host"], port=cluster["port"], user="postgres", dbname="postgres", autocommit=True
    ) as conn:
        conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def url(database, user, password):
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host="127.0.0.1",
        port=database["port"],
        database=database["name"],
    ).render_as_string(hide_password=False)


def reconcile(database, **overrides):
    return run(["sh", str(ROOT / "script/reconcile-db-credentials.sh")], env={**database["env"], **overrides})


def snapshot(database):
    with psycopg.connect(
        host=database["host"], port=database["port"], user="postgres", dbname=database["name"]
    ) as conn:
        return {
            "rows": conn.execute("SELECT * FROM startup_sentinel ORDER BY id").fetchall(),
            "roles": conn.execute(
                "SELECT rolname, rolpassword, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin FROM pg_authid WHERE rolname IN ('postgres', 'viracase') ORDER BY rolname"
            ).fetchall(),
        }


@pytest.mark.parametrize(
    "admin_changed,app_changed", [(True, True), (True, False), (False, True), (False, False)]
)
def test_real_postgres_recovers_passwords_preserves_data_and_is_idempotent(
    database_cluster, admin_changed, app_changed
):
    database = database_cluster
    env = database["env"]
    env["POSTGRES_PASSWORD"] = ADMIN_NEW if admin_changed else ADMIN_OLD
    env["APP_DB_PASSWORD"] = APP_NEW if app_changed else APP_OLD
    before = snapshot(database)
    log_start = database["log"].stat().st_size
    result = reconcile(database)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "DATABASE_CREDENTIALS_RECONCILED" if admin_changed or app_changed else "DATABASE_CREDENTIALS_OK"
    ) in result.stdout
    after = snapshot(database)
    assert before["rows"] == after["rows"]
    assert [row[2:] for row in before["roles"]] == [row[2:] for row in after["roles"]]
    for user, secret in (("postgres", env["POSTGRES_PASSWORD"]), ("viracase", env["APP_DB_PASSWORD"])):
        with psycopg.connect(
            host="127.0.0.1", port=database["port"], user=user, password=secret, dbname=database["name"]
        ) as conn:
            assert conn.execute("SELECT current_user").fetchone()[0] == user
    repeated = reconcile(database)
    assert repeated.returncode == 0 and "DATABASE_CREDENTIALS_OK" in repeated.stdout
    assert snapshot(database) == after
    visible = (
        result.stdout
        + result.stderr
        + repeated.stdout
        + repeated.stderr
        + database["log"].read_text()[log_start:]
    )
    assert ADMIN_NEW not in visible and APP_NEW not in visible


def test_real_postgres_missing_database_does_not_rotate_credentials(database_cluster):
    before = snapshot(database_cluster)
    result = reconcile(database_cluster, POSTGRES_DB="missing_" + uuid4().hex)
    assert result.returncode == 1
    assert "DATABASE_CREDENTIALS_RECONCILIATION_FAILED" in result.stderr
    assert snapshot(database_cluster) == before


def test_real_postgres_recreates_only_missing_application_role(database_cluster):
    database = database_cluster
    with psycopg.connect(
        host=database["host"], port=database["port"], user="postgres", dbname="postgres", autocommit=True
    ) as conn:
        conn.execute("DROP ROLE viracase")
    result = reconcile(database)
    assert result.returncode == 0, result.stdout + result.stderr
    roles = snapshot(database)["roles"]
    assert next(row[2:] for row in roles if row[0] == "viracase") == (False, False, False, True)


def test_real_postgres_initializer_reproduces_error_then_runs_migrations_and_bootstrap(database_cluster):
    database = database_cluster
    spec = importlib.util.spec_from_file_location(
        "initializer_integration", ROOT / "script/container-initialize.py"
    )
    initializer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(initializer)
    config = Settings(
        _env_file=None,
        database_url=url(database, "viracase", APP_NEW),
        migration_database_url=url(database, "postgres", ADMIN_NEW),
    )
    with pytest.raises(ValueError, match="DATABASE_AUTH_FAILED"):
        initializer.check_database_access(config)
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("VIRA_")},
        "VIRA_CONTAINERIZED": "false",
        "VIRA_DATABASE_URL": config.database_url,
        "VIRA_MIGRATION_DATABASE_URL": config.migration_database_url,
        "VIRA_ADMIN_LOGIN": "integration-admin",
        "VIRA_ADMIN_NAME": "Integration Admin",
        "VIRA_ADMIN_PASSWORD": "synthetic-integration-password",
        "PYTHONPATH": str(ROOT / "src"),
        "VIRA_CREDENTIAL_KEY_PATH": str(database["work"] / "secrets/key"),
    }
    result = reconcile(database)
    assert result.returncode == 0, result.stdout + result.stderr
    for _ in range(2):
        result = run(
            [sys.executable, str(ROOT / "script/container-initialize.py")], env=env, cwd=ROOT, timeout=120
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Inicialização do viroucaseai concluída" in result.stdout
    engine = create_engine(config.database_url)
    try:
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
            assert conn.scalar(text("SELECT count(*) FROM accounts WHERE login = 'integration-admin'")) == 1
            assert (
                conn.scalar(text("SELECT content FROM startup_sentinel WHERE id = 1"))
                == "existing-row-preserved"
            )
    finally:
        engine.dispose()
