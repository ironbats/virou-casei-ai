import argparse
import os
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from viracase.infrastructure.bootstrap import (
    bootstrap_admin,
    check_admin_credentials,
    report_admin_bootstrap,
    reset_admin_password,
)
from viracase.infrastructure.config import settings
from viracase.infrastructure.database import database


def main():
    parser = argparse.ArgumentParser(
        description="Cria o primeiro administrador ou sincroniza sua senha com o .env; inclui diagnóstico."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="Verifica login e senha configurados sem gravar dados."
    )
    mode.add_argument(
        "--reset-password",
        action="store_true",
        help="Aplica VIRA_ADMIN_PASSWORD ao administrador existente de VIRA_ADMIN_LOGIN e encerra suas sessões.",
    )
    args = parser.parse_args()
    os.chdir(Path(__file__).resolve().parents[1])
    config = settings()
    engine, sessions = database(config.database_url)
    try:
        if args.check:
            check_admin_credentials(sessions, config)
            print(
                "✅ Login e senha administrativos configurados conferem com o banco. Nenhum dado foi alterado."
            )
        elif args.reset_password:
            reset_admin_password(sessions, config)
            print(
                "✅ Senha do administrador configurado atualizada; sessões anteriores dessa conta encerradas."
            )
        else:
            result = bootstrap_admin(sessions, config, sync_password=True)
            report_admin_bootstrap(sessions, config, result)
            return
        print(
            "Acesso administrativo pelo backoffice: " + ", ".join(sorted(config.allowed_origins(admin=True)))
        )
        print("Rota de login: /admin-api/v1/auth/login. A plataforma de alunos usa outro perfil.")
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except SQLAlchemyError:
        raise SystemExit(
            "Não foi possível concluir a operação no banco. Verifique a inicialização e a conexão do backend."
        ) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
