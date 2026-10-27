import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select

from viracase.infrastructure.bootstrap import (
    bootstrap_admin,
    check_admin_credentials,
    report_admin_bootstrap,
    reset_admin_password,
)
from viracase.infrastructure.config import Settings
from viracase.infrastructure.models import Account, Audit, Session
from viracase.infrastructure.repository import HASHER
from viracase.interfaces.api import create_app

ROOT = Path(__file__).resolve().parents[1]
OLD_PASSWORD = "test-operator-password"
NEW_PASSWORD = "synthetic-F#-$-new-password"


def admin_config(**changes):
    values = {"admin_login": "operator", "admin_password": OLD_PASSWORD}
    values.update(changes)
    return Settings(_env_file=None, **values)


def client_for(system, admin=False):
    return TestClient(
        create_app(
            admin=admin,
            dependencies=(system["config"], system["engine"], system["repo"], system["catalog"], None),
        )
    )


def login(client, password=OLD_PASSWORD, admin=True, login="operator"):
    prefix = "/admin-api/v1" if admin else "/api/v1"
    return client.post(
        prefix + "/auth/login",
        json={"login": login, "password": password},
        headers={"Origin": f"http://127.0.0.1:{5174 if admin else 5173}", "X-CSRF-Token": ""},
    )


def rows(system, model):
    with system["engine"].connect() as connection:
        return [dict(row) for row in connection.execute(select(model.__table__)).mappings()]


def snapshot(system):
    return {model.__tablename__: rows(system, model) for model in (Account, Session, Audit)}


def test_valid_admin_at_student_login_is_generic_without_session(system):
    public, private = client_for(system), client_for(system, True)
    before = snapshot(system)
    response = login(public, admin=False)
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "UNAUTHENTICATED" and error["retryable"] is False
    assert error["details"] == {} and error["request_id"]
    assert error["message"] == "Credenciais inválidas."
    assert "set-cookie" not in response.headers and snapshot(system) == before
    assert login(private).status_code == 200
    assert private.get("/admin-api/v1/me").json()["role"] == "admin"


@pytest.mark.parametrize("login_name", ["operator", "unknown"])
@pytest.mark.parametrize("admin", [False, True])
def test_wrong_password_never_reveals_admin_role(system, login_name, admin):
    response = login(client_for(system, admin), "wrong-password", admin, login_name)
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Credenciais inválidas."
    assert "set-cookie" not in response.headers and rows(system, Session) == []


def test_student_still_cannot_authenticate_in_backoffice(system):
    response = login(client_for(system, True), "test-student-password", True, "student0")
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Credenciais inválidas."
    assert "set-cookie" not in response.headers


def test_check_credentials_is_read_only_and_normalizes_login(system):
    before = snapshot(system)
    check_admin_credentials(system["repo"].sessions, admin_config(admin_login="  OpErAtOr "))
    assert snapshot(system) == before


@pytest.mark.parametrize(
    "changes, fragment",
    [
        ({"admin_password": NEW_PASSWORD}, "não corresponde à senha gravada"),
        ({"admin_login": "absent-admin"}, "não corresponde a uma conta existente"),
        ({"admin_login": "student0"}, "conta de aluno"),
        ({"admin_password": ""}, "Configure VIRA_ADMIN_LOGIN"),
        ({"admin_password": "short"}, "senha de 12 a 128"),
    ],
)
def test_check_explains_local_config_errors_without_writing_or_exposing_password(system, changes, fragment):
    before = snapshot(system)
    with pytest.raises(ValueError, match=fragment) as error:
        check_admin_credentials(system["repo"].sessions, admin_config(**changes))
    assert OLD_PASSWORD not in str(error.value) and NEW_PASSWORD not in str(error.value)
    assert snapshot(system) == before


def test_changed_dotenv_does_not_silently_reset_existing_admin(system, capsys):
    cfg = admin_config(admin_password=NEW_PASSWORD)
    before = snapshot(system)
    result = bootstrap_admin(system["repo"].sessions, cfg)
    assert result == "preserved"
    report_admin_bootstrap(system["repo"].sessions, cfg, result)
    output = capsys.readouterr().out
    assert "⚠️" in output and "--reset-password" in output
    assert "http://127.0.0.1:5174" in output and "/admin-api/v1/auth/login" in output
    assert "✅ Login e senha" not in output
    assert OLD_PASSWORD not in output and NEW_PASSWORD not in output
    assert snapshot(system) == before


def test_first_admin_from_dotenv_can_login_in_backoffice(system, tmp_path, capsys):
    with system["repo"].sessions.begin() as db:
        db.execute(delete(Account).where(Account.id == system["admin"]))
    env = tmp_path / "admin.env"
    env.write_text(
        "VIRA_ADMIN_LOGIN='  New.Admin  '\nVIRA_ADMIN_NAME='Administrador de teste'\n"
        f"VIRA_ADMIN_PASSWORD='{NEW_PASSWORD}'\n"
    )
    cfg = Settings(_env_file=env)
    result = bootstrap_admin(system["repo"].sessions, cfg)
    report_admin_bootstrap(system["repo"].sessions, cfg, result)
    output = capsys.readouterr().out
    assert "✅ Login e senha" in output and "⚠️" not in output and NEW_PASSWORD not in output
    response = login(client_for(system, True), NEW_PASSWORD, True, "new.admin")
    assert response.status_code == 200 and response.json()["role"] == "admin"


def test_explicit_reset_changes_only_target_password_revokes_its_sessions_and_audits(system):
    private, public = client_for(system, True), client_for(system)
    assert login(private).status_code == 200
    assert login(public, "test-student-password", False, "student0").status_code == 200
    with system["repo"].sessions.begin() as db:
        other = Account(
            login="other-admin", name="Outro", role="admin", password_hash=HASHER.hash(OLD_PASSWORD)
        )
        db.add(other)
        db.flush()
        other_id = other.id
    other_token, _ = system["repo"].login("other-admin", OLD_PASSWORD, "admin", "other-test-client")
    before = {row["id"]: row for row in rows(system, Account)}
    reset_admin_password(system["repo"].sessions, admin_config(admin_password=NEW_PASSWORD))
    for account in rows(system, Account):
        previous = before[account["id"]]
        if account["id"] == system["admin"]:
            assert HASHER.verify(account["password_hash"], NEW_PASSWORD)
            assert account["password_hash"] != previous["password_hash"]
            assert {k: v for k, v in account.items() if k != "password_hash"} == {
                k: v for k, v in previous.items() if k != "password_hash"
            }
        else:
            assert account == previous
    assert private.get("/admin-api/v1/me").status_code == 401
    assert public.get("/api/v1/me").status_code == 200
    assert system["repo"].authenticate(other_token, "admin")["id"] == other_id
    assert login(private).status_code == 401
    response = login(private, NEW_PASSWORD)
    assert response.status_code == 200
    assert (
        private.post("/admin-api/v1/auth/logout", headers={"Origin": "http://127.0.0.1:5174"}).status_code
        == 403
    )
    assert (
        private.post(
            "/admin-api/v1/auth/logout",
            headers={"Origin": "http://127.0.0.1:5174", "X-CSRF-Token": response.json()["csrf_token"]},
        ).status_code
        == 204
    )
    audit = rows(system, Audit)
    assert len(audit) == 1 and audit[0]["action"] == "reset-admin-password-cli"
    assert audit[0]["resource_id"] == system["admin"]
    assert NEW_PASSWORD not in str(audit) and OLD_PASSWORD not in str(audit)


@pytest.mark.parametrize("login_name", ["student0", "missing-admin"])
def test_reset_does_not_promote_create_or_select_another_admin(system, login_name):
    before = snapshot(system)
    with pytest.raises(ValueError):
        reset_admin_password(
            system["repo"].sessions, admin_config(admin_login=login_name, admin_password=NEW_PASSWORD)
        )
    assert snapshot(system) == before


@pytest.mark.parametrize("password", ["", "short", "x" * 129])
def test_reset_rejects_invalid_password_before_writing(system, password):
    before = snapshot(system)
    with pytest.raises(ValueError):
        reset_admin_password(system["repo"].sessions, admin_config(admin_password=password))
    assert snapshot(system) == before


def test_reset_rolls_back_password_and_sessions_if_audit_write_fails(system):
    token, _ = system["repo"].login("operator", OLD_PASSWORD, "admin", "rollback-test")
    before = snapshot(system)

    def fail_audit(mapper, connection, target):
        raise RuntimeError("synthetic audit failure")

    event.listen(Audit, "before_insert", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="synthetic audit failure"):
            reset_admin_password(system["repo"].sessions, admin_config(admin_password=NEW_PASSWORD))
    finally:
        event.remove(Audit, "before_insert", fail_audit)
    assert snapshot(system) == before
    assert system["repo"].authenticate(token, "admin")["id"] == system["admin"]


def run_cli(system, tmp_path, *args, password=OLD_PASSWORD):
    env = {k: v for k, v in os.environ.items() if not k.startswith("VIRA_")}
    env.update(
        PYTHONPATH=str(ROOT / "src"),
        VIRA_DATABASE_URL=system["config"].database_url,
        VIRA_CONTAINERIZED="false",
        VIRA_ADMIN_LOGIN="operator",
        VIRA_ADMIN_NAME="Nome não deve sobrescrever o cadastro",
        VIRA_ADMIN_PASSWORD=password,
    )
    return subprocess.run(
        [sys.executable, str(ROOT / "script/bootstrap-admin.py"), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_cli_check_and_recovery_work_outside_project_directory(system, tmp_path):
    before = snapshot(system)
    success = run_cli(system, tmp_path, "--check")
    assert success.returncode == 0 and "✅" in success.stdout
    mismatch = run_cli(system, tmp_path, "--check", password=NEW_PASSWORD)
    assert mismatch.returncode == 1 and "não corresponde à senha gravada" in mismatch.stderr
    assert snapshot(system) == before
    recovered = run_cli(system, tmp_path, "--reset-password", password=NEW_PASSWORD)
    assert recovered.returncode == 0 and "atualizada" in recovered.stdout
    assert run_cli(system, tmp_path, "--check", password=NEW_PASSWORD).returncode == 0
    for result in (success, mismatch, recovered):
        assert OLD_PASSWORD not in result.stdout + result.stderr
        assert NEW_PASSWORD not in result.stdout + result.stderr


def test_cli_default_synchronizes_configured_admin_password(system, tmp_path):
    before = {row["id"]: row for row in rows(system, Account)}
    result = run_cli(system, tmp_path, password=NEW_PASSWORD)
    assert result.returncode == 0 and "sincronizada" in result.stdout and "⚠️" not in result.stdout
    assert OLD_PASSWORD not in result.stdout + result.stderr
    assert NEW_PASSWORD not in result.stdout + result.stderr
    check_admin_credentials(system["repo"].sessions, admin_config(admin_password=NEW_PASSWORD))
    for account in rows(system, Account):
        previous = before[account["id"]]
        if account["id"] == system["admin"]:
            assert HASHER.verify(account["password_hash"], NEW_PASSWORD)
            assert {k: v for k, v in account.items() if k != "password_hash"} == {
                k: v for k, v in previous.items() if k != "password_hash"
            }
        else:
            assert account == previous


def test_admin_session_cannot_be_reused_in_student_api(system):
    private, public = client_for(system, True), client_for(system)
    assert login(private).status_code == 200
    token = private.cookies.get("vira_admin")
    response = public.get("/api/v1/me", headers={"Cookie": "vira_student=" + token})
    assert response.status_code == 401
    with system["repo"].sessions() as db:
        assert db.get(Session, sha256(token.encode()).hexdigest()).audience == "admin"
