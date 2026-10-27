from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


def normalize_origin(value):
    if not isinstance(value, str) or any(c.isspace() for c in value) or "," in value:
        raise ValueError("Origem inválida: informe esquema, host e porta, sem caminhos ou curingas.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Porta ou host inválido na origem configurada.") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "*" in parsed.netloc
        or "\\" in value
    ):
        raise ValueError("Origem inválida: informe esquema, host e porta, sem caminhos ou curingas.")
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    suffix = "" if port is None or port == (443 if parsed.scheme == "https" else 80) else f":{port}"
    return f"{parsed.scheme}://{host}{suffix}"


def origin_set(value):
    items = value.split(",")
    if not items or any(not item.strip() for item in items):
        raise ValueError("Configure ao menos uma origem válida, separando as origens por vírgula.")
    return frozenset(normalize_origin(item.strip()) for item in items)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_prefix="VIRA_")
    database_url: str = "postgresql+psycopg://viracase:configure@127.0.0.1:55432/viracase"
    migration_database_url: str = ""
    source_root: Path = Path("var/sources")
    content_root: Path = Path("content")
    catalog_path: Path = Path("config/models.json")
    credential_key_path: Path = Path("var/secrets/credentials.key")
    public_origin: str = "http://127.0.0.1:5173,http://localhost:5173"
    admin_origin: str = "http://127.0.0.1:5174,http://localhost:5174"
    public_hosts: str = "127.0.0.1,localhost,testserver"
    cookie_secure: bool = False
    openai_api_key: str = ""
    gemini_api_key: str = ""
    provider_timeout: int = 180
    session_hours: int = 12
    lifecycle_enabled: bool = True
    restore_hold: bool = False
    containerized: bool = False
    db_host: str = "postgres"
    postgres_db: str = Field(default="viracase", validation_alias="POSTGRES_DB")
    app_db_password: SecretStr = Field(default=SecretStr(""), validation_alias="APP_DB_PASSWORD")
    postgres_password: SecretStr = Field(default=SecretStr(""), validation_alias="POSTGRES_PASSWORD")
    admin_login: str = ""
    admin_name: str = ""
    admin_password: SecretStr = SecretStr("")

    def allowed_origins(self, admin=False):
        return origin_set(self.admin_origin if admin else self.public_origin)

    def migration_url(self):
        if self.containerized:
            password = self.postgres_password.get_secret_value()
            if not password:
                raise ValueError("POSTGRES_PASSWORD é necessário para inicializar o banco.")
            return self._database_url("postgres", password)
        return self.migration_database_url or self.database_url

    def _database_url(self, user, password):
        return URL.create(
            "postgresql+psycopg",
            username=user,
            password=password,
            host=self.db_host,
            port=5432,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)

    def validate_runtime(self):
        for origin in self.allowed_origins():
            if urlsplit(origin).hostname not in {"127.0.0.1", "localhost", "::1"} and not self.cookie_secure:
                raise ValueError("VIRA_COOKIE_SECURE deve estar ativo para todas as origens não locais.")
        for origin in self.allowed_origins(admin=True):
            if urlsplit(origin).hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("As origens administrativas devem usar localhost ou endereço de loopback.")
        if self.containerized:
            password = self.app_db_password.get_secret_value()
            if not password:
                raise ValueError("APP_DB_PASSWORD é necessário para iniciar os serviços.")
            self.database_url = self._database_url("viracase", password)


@lru_cache
def settings():
    value = Settings()
    value.validate_runtime()
    return value
