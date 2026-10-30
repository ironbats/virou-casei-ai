import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from viracase.infrastructure.bootstrap import bootstrap_admin, report_admin_bootstrap
from viracase.infrastructure.config import settings
from viracase.infrastructure.credentials import initialize_key
from viracase.infrastructure.database import database

ROOT = Path(__file__).resolve().parents[1]


def check_database_access(config):
    for role, url in (("postgres", config.migration_url()), ("viracase", config.database_url)):
        options = {"connect_timeout": 5} if url.startswith("postgresql") else {}
        engine = create_engine(url, connect_args=options)
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
        except SQLAlchemyError as error:
            original = getattr(error, "orig", error)
            state = getattr(original, "sqlstate", None)
            if state == "28P01" or "password authentication failed" in str(original).lower():
                code = "DATABASE_AUTH_FAILED"
                reason = "A senha configurada foi rejeitada pelo PostgreSQL."
            elif state == "3D000" or (
                "database" in str(original).lower() and "does not exist" in str(original).lower()
            ):
                code = "DATABASE_NOT_FOUND"
                reason = "O banco configurado nao existe no servidor."
            else:
                code = "DATABASE_CONNECTION_FAILED"
                reason = "Nao foi possivel validar a conexao com o banco."
            raise ValueError(
                f"{code}: usuario={role}. {reason} "
                "No Compose local, execute ./script/run-local.sh para preparar o PostgreSQL. "
                "As migrations nao foram executadas."
            ) from None
        finally:
            engine.dispose()
    print("✅ Conexoes de migrations e da aplicacao verificadas.", flush=True)


def main():
    os.chdir(ROOT)
    print("[1/4] Verificando arquivos e permissões das migrations...", flush=True)
    subprocess.run([sys.executable, str(ROOT / "script/check-migrations.py")], cwd=ROOT, check=True)
    config = settings()
    check_database_access(config)
    print("[2/4] Aplicando migrations pendentes...", flush=True)
    subprocess.run(
        [sys.executable, "-m", "alembic", "--config", str(ROOT / "alembic.ini"), "upgrade", "head"],
        cwd=ROOT,
        check=True,
    )
    print("✅ Migrations concluídas.", flush=True)
    print("[3/4] Aplicando permissões do banco...", flush=True)
    migration_engine = create_engine(config.migration_url())
    try:
        with migration_engine.begin() as connection:
            for statement in (ROOT / "script/db-permissions.sql").read_text().split(";"):
                if statement.strip():
                    connection.exec_driver_sql(statement)
    finally:
        migration_engine.dispose()
    print("✅ Permissões do banco aplicadas.", flush=True)
    print("[4/4] Verificando o administrador e sincronizando a senha configurada...", flush=True)
    engine, sessions = database(config.database_url)
    try:
        initialize_key(config.credential_key_path, sessions)
        print("✅ Proteção das credenciais de IA preparada.", flush=True)
        result = bootstrap_admin(sessions, config, sync_password=True)
        report_admin_bootstrap(sessions, config, result)
    finally:
        engine.dispose()
    print("✅ Inicialização do viroucaseai concluída: schema, permissões e administrador.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            f"❌ Inicialização interrompida na etapa acima (código {error.returncode}). "
            "As etapas seguintes não foram executadas."
        ) from None
    except ValueError as error:
        raise SystemExit(str(error)) from None
