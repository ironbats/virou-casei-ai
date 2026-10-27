from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from viracase.infrastructure.bootstrap import bootstrap_admin
from viracase.infrastructure.config import Settings, normalize_origin
from viracase.infrastructure.models import Account
from viracase.infrastructure.repository import HASHER
from viracase.interfaces.api import create_app


def client_for(system, admin=False):
    config = system["config"]
    config.public_origin = "http://127.0.0.1:5173, http://localhost:5173/"
    config.admin_origin = "http://127.0.0.1:5174, http://localhost:5174"
    return TestClient(
        create_app(
            admin=admin, dependencies=(config, system["engine"], system["repo"], system["catalog"], None)
        )
    )


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
@pytest.mark.parametrize("admin", [False, True])
def test_comma_separated_origins_login_session_csrf_and_logout(system, host, admin):
    client = client_for(system, admin)
    prefix = "/admin-api/v1" if admin else "/api/v1"
    origin = f"http://{host}:{5174 if admin else 5173}"
    credentials = {
        "login": "operator" if admin else "student0",
        "password": "test-operator-password" if admin else "test-student-password",
    }
    response = client.post(prefix + "/auth/login", json=credentials, headers={"Origin": origin})
    assert response.status_code == 200
    assert (
        "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
    )
    assert client.get(prefix + "/me").status_code == 200
    assert client.post(prefix + "/auth/logout", headers={"Origin": origin}).status_code == 403
    headers = {"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]}
    assert client.post(prefix + "/auth/logout", headers=headers).status_code == 204
    assert client.get(prefix + "/me").status_code == 401


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "http://localhost.evil:5173",
        "http://127.0.0.1:9999",
        "https://evil.example",
        "http://localhost:5174",
        "http://localhost:5173,http://127.0.0.1:5173",
        "http://localhost:5173/path",
    ],
)
def test_untrusted_origin_stays_denied(system, origin):
    response = client_for(system).post(
        "/api/v1/auth/login",
        json={"login": "student0", "password": "test-student-password"},
        headers={} if origin is None else {"Origin": origin},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORIGIN_DENIED"
    assert "set-cookie" not in response.headers


def test_admin_and_student_audiences_remain_separate(system):
    public = client_for(system)
    private = client_for(system, True)
    assert public.get("/admin-api/v1/accounts").status_code == 404
    assert private.get("/api/v1/projects").status_code == 404
    response = public.post(
        "/api/v1/auth/login",
        json={"login": "operator", "password": "test-operator-password"},
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 401
    response = private.post(
        "/admin-api/v1/auth/login",
        json={"login": "operator", "password": "test-operator-password"},
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "null",
        "https://*.example.com",
        "https://example.com/path",
        "https://user:pass@example.com",
        "http://localhost:not-a-port",
        "http://localhost:5173,",
        "http://localhost:5173,https://example.com",
    ],
)
def test_invalid_or_insecure_origin_configuration_fails_closed(value):
    config = Settings(_env_file=None, public_origin=value, cookie_secure=False)
    with pytest.raises(ValueError):
        config.validate_runtime()


def test_origin_configuration_validates_every_item():
    config = Settings(
        _env_file=None, public_origin="http://localhost:5173,https://app.example.com", cookie_secure=True
    )
    config.validate_runtime()
    assert normalize_origin("https://APP.example.com:443/") == "https://app.example.com"
    config.admin_origin = "http://localhost:5174,https://admin.example.com"
    with pytest.raises(ValueError):
        config.validate_runtime()


def test_container_urls_override_host_urls_and_encode_passwords():
    config = Settings(
        _env_file=None,
        containerized=True,
        database_url="postgresql+psycopg://wrong@127.0.0.1:55432/wrong",
        APP_DB_PASSWORD="app:/@#$ synthetic",
        POSTGRES_PASSWORD="root:/@#$ synthetic",
    )
    config.validate_runtime()
    from sqlalchemy.engine import make_url

    app, migration = make_url(config.database_url), make_url(config.migration_url())
    assert (app.host, app.port, app.username, app.password) == (
        "postgres",
        5432,
        "viracase",
        "app:/@#$ synthetic",
    )
    assert (
        migration.host == "postgres"
        and migration.username == "postgres"
        and migration.password == "root:/@#$ synthetic"
    )


def remove_admin(system):
    with system["repo"].sessions.begin() as db:
        db.execute(delete(Account).where(Account.id == system["admin"]))


def test_first_admin_from_dotenv_is_idempotent_and_does_not_reset_password(system, tmp_path):
    remove_admin(system)
    env = tmp_path / "bootstrap.env"
    env.write_text(
        "VIRA_ADMIN_LOGIN=' New.Operator '\nVIRA_ADMIN_NAME='Nome de Homologação'\nVIRA_ADMIN_PASSWORD='test-$#-password-012345'\n"
    )
    config = Settings(_env_file=env)
    sessions = system["repo"].sessions
    assert bootstrap_admin(sessions, config) == "created"
    assert bootstrap_admin(sessions, Settings(_env_file=None)) == "preserved"
    changed = Settings(
        _env_file=None, admin_login="another", admin_name="Outro", admin_password="different-password-test"
    )
    assert bootstrap_admin(sessions, changed) == "preserved"
    with sessions() as db:
        accounts = db.scalars(select(Account).where(Account.role == "admin")).all()
        assert len(accounts) == 1
        assert accounts[0].login == "new.operator" and accounts[0].name == "Nome de Homologação"
        assert HASHER.verify(accounts[0].password_hash, "test-$#-password-012345")
        assert accounts[0].password_hash != "test-$#-password-012345"


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"admin_login": "operator", "admin_name": "Operador", "admin_password": "short"},
        {"admin_login": "student0", "admin_name": "Operador", "admin_password": "valid-test-password"},
    ],
)
def test_invalid_bootstrap_never_creates_or_promotes_account(system, changes):
    remove_admin(system)
    with pytest.raises(ValueError):
        bootstrap_admin(system["repo"].sessions, Settings(_env_file=None, **changes))
    with system["repo"].sessions() as db:
        assert not db.scalar(select(Account.id).where(Account.role == "admin"))
        assert db.get(Account, system["users"][0]).role == "student"


def test_concurrent_first_admin_postgres(system):
    if system["engine"].dialect.name != "postgresql":
        pytest.skip("Bootstrap concorrente exige PostgreSQL real")
    remove_admin(system)
    config = Settings(
        _env_file=None, admin_login="first-admin", admin_name="Admin", admin_password="synthetic-password-123"
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: bootstrap_admin(system["repo"].sessions, config), range(4)))
    assert results.count("created") == 1 and results.count("preserved") == 3
