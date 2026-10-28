import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select

from viracase.infrastructure.bootstrap import bootstrap_admin, check_admin_credentials, report_admin_bootstrap
from viracase.infrastructure.config import Settings
from viracase.infrastructure.models import Account, Audit, Session
from viracase.infrastructure.repository import HASHER
from viracase.interfaces.api import create_app

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "synthetic-F#-$-env-password"


def config(**changes):
    values = {
        "admin_login": "operator",
        "admin_name": "Nome configurado não renomeia conta existente",
        "admin_password": PASSWORD,
        "admin_origin": "http://127.0.0.1:5175,http://localhost:5175",
    }
    values.update(changes)
    return Settings(_env_file=None, **values)


def snapshot(system):
    with system["engine"].connect() as connection:
        return {
            model.__tablename__: [dict(row) for row in connection.execute(select(model.__table__)).mappings()]
            for model in (Account, Session, Audit)
        }


def private_client(system):
    system["config"].admin_origin = config().admin_origin
    return TestClient(
        create_app(
            admin=True,
            dependencies=(system["config"], system["engine"], system["repo"], system["catalog"], None),
        )
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_changed_env_password_authenticates_on_configured_port_and_is_idempotent(system, host, capsys):
    client = private_client(system)
    origin = f"http://{host}:5175"
    payload = {"login": "operator", "password": PASSWORD}
    assert (
        client.post("/admin-api/v1/auth/login", json=payload, headers={"Origin": origin}).status_code == 401
    )
    cfg = config(admin_login="  OpErAtOr ")
    sessions = system["repo"].sessions
    result = bootstrap_admin(sessions, cfg, sync_password=True)
    assert result == "updated"
    check_admin_credentials(sessions, cfg)
    report_admin_bootstrap(sessions, cfg, result)
    output = capsys.readouterr().out
    assert "sincronizada" in output and "5175" in output and "5174" not in output
    assert "⚠️" not in output and PASSWORD not in output
    response = client.post("/admin-api/v1/auth/login", json=payload, headers={"Origin": origin})
    assert response.status_code == 200 and response.json()["role"] == "admin"
    assert (
        "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
    )
    before_repeat = snapshot(system)
    assert bootstrap_admin(sessions, cfg, sync_password=True) == "preserved"
    assert snapshot(system) == before_repeat
    assert client.get("/admin-api/v1/me").status_code == 200
    assert (
        client.post(
            "/admin-api/v1/auth/login",
            json={"login": "operator", "password": "test-operator-password"},
            headers={"Origin": origin},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/admin-api/v1/auth/login", json=payload, headers={"Origin": "http://127.0.0.1:5174"}
        ).status_code
        == 403
    )
    assert client.post("/admin-api/v1/auth/logout", headers={"Origin": origin}).status_code == 403
    assert (
        client.post(
            "/admin-api/v1/auth/logout",
            headers={"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]},
        ).status_code
        == 204
    )


def test_sync_preserves_identity_other_accounts_and_only_revokes_changed_admin_sessions(system):
    repo = system["repo"]
    with repo.sessions.begin() as db:
        db.add(Account(login="other-admin", name="Outro", role="admin", password_hash=HASHER.hash(PASSWORD)))
    old_admin_token, _ = repo.login("operator", "test-operator-password", "admin", "sync-admin-test")
    other_token, _ = repo.login("other-admin", PASSWORD, "admin", "sync-other-test")
    student_token, _ = repo.login("student0", "test-student-password", "student", "sync-student-test")
    before = snapshot(system)
    assert bootstrap_admin(repo.sessions, config(), sync_password=True) == "updated"
    after = snapshot(system)
    original = {row["id"]: row for row in before["accounts"]}
    for row in after["accounts"]:
        old = original[row["id"]]
        if row["id"] == system["admin"]:
            assert HASHER.verify(row["password_hash"], PASSWORD)
            assert {k: v for k, v in row.items() if k != "password_hash"} == {
                k: v for k, v in old.items() if k != "password_hash"
            }
        else:
            assert row == old
    assert len(after["sessions"]) == len(before["sessions"]) - 1
    assert not any(row["account_id"] == system["admin"] for row in after["sessions"])
    from viracase.domain.rules import RuleError

    with pytest.raises(RuleError):
        repo.authenticate(old_admin_token, "admin")
    assert repo.authenticate(other_token, "admin")["role"] == "admin"
    assert repo.authenticate(student_token, "student")["role"] == "student"
    audit = after["audit_events"]
    assert len(audit) == 1 and audit[0]["action"] == "sync-admin-password-env"
    assert audit[0]["resource_id"] == system["admin"]
    assert PASSWORD not in str(audit)


@pytest.mark.parametrize("password", ["", "short", "x" * 129])
def test_invalid_configured_password_fails_without_changing_existing_accounts(system, password):
    before = snapshot(system)
    with pytest.raises(ValueError):
        bootstrap_admin(system["repo"].sessions, config(admin_password=password), sync_password=True)
    assert snapshot(system) == before


@pytest.mark.parametrize("login_name", ["", "missing-admin", "student0"])
def test_sync_never_renames_promotes_or_selects_an_unrelated_admin(system, login_name):
    before = snapshot(system)
    with pytest.raises(ValueError):
        bootstrap_admin(system["repo"].sessions, config(admin_login=login_name), sync_password=True)
    assert snapshot(system) == before


def test_sync_repairs_invalid_stored_hash_with_configured_password(system):
    with system["repo"].sessions.begin() as db:
        db.get(Account, system["admin"]).password_hash = "synthetic-invalid-old-hash"
    assert bootstrap_admin(system["repo"].sessions, config(), sync_password=True) == "updated"
    check_admin_credentials(system["repo"].sessions, config())


def test_sync_rolls_back_credentials_and_sessions_when_audit_fails(system):
    system["repo"].login("operator", "test-operator-password", "admin", "sync-rollback-test")
    before = snapshot(system)

    def fail_audit(mapper, connection, target):
        raise RuntimeError("synthetic sync audit failure")

    event.listen(Audit, "before_insert", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="synthetic sync audit failure"):
            bootstrap_admin(system["repo"].sessions, config(), sync_password=True)
    finally:
        event.remove(Audit, "before_insert", fail_audit)
    assert snapshot(system) == before


def test_first_admin_is_created_then_preserved_in_sync_mode(system):
    with system["repo"].sessions.begin() as db:
        db.execute(delete(Account).where(Account.id == system["admin"]))
    cfg = config()
    assert bootstrap_admin(system["repo"].sessions, cfg, sync_password=True) == "created"
    check_admin_credentials(system["repo"].sessions, cfg)
    before = snapshot(system)
    assert bootstrap_admin(system["repo"].sessions, cfg, sync_password=True) == "preserved"
    assert snapshot(system) == before


@pytest.mark.parametrize("up_exit, check_exit", [(0, 0), (0, 1), (1, 0)])
def test_docker_launcher_checks_credentials_before_reporting_success(tmp_path, up_exit, check_exit):
    from publication_deployment_support import compose_command, make_installation, read_calls

    project, env = make_installation(tmp_path)
    original = (project / ".env").read_bytes()
    env.update(DOCKER_TEST_UP_EXIT=str(up_exit), DOCKER_TEST_CHECK_EXIT=str(check_exit))
    result = subprocess.run(
        ["bash", "script/run-local.sh"], cwd=project, env=env,
        capture_output=True, text=True, timeout=20, check=False,
    )
    commands = [compose_command(call) for call in read_calls(project)]
    check_command = ["run", "--rm", "--no-deps", "initialize", "python", "script/bootstrap-admin.py", "--check"]
    assert (project / ".env").read_bytes() == original
    assert "never-include-in-output" not in result.stdout + result.stderr
    if up_exit:
        assert result.returncode == 1 and check_command not in commands
    elif check_exit:
        assert result.returncode == 1 and check_command in commands
        assert "Falha na verificacao das credenciais" in result.stderr
    else:
        assert result.returncode == 0 and check_command in commands
        assert next(i for i, cmd in enumerate(commands) if cmd[:1] == ["up"]) < commands.index(check_command)
    assert ("Executor local READY" in result.stdout) == (up_exit == check_exit == 0)
