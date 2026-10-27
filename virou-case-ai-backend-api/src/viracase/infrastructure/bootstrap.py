from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select, text

from viracase.infrastructure.models import Account, Audit, Session
from viracase.infrastructure.repository import HASHER


def bootstrap_admin(sessions, config, *, sync_password=False):
    with sessions.begin() as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(73210430)"))
        if db.scalar(select(Account.id).where(Account.role == "admin").limit(1)):
            if sync_password:
                login, password = _configured_admin_credentials(config)
                account = _existing_admin(db, login, lock=True)
                try:
                    HASHER.verify(account.password_hash, password)
                except (VerificationError, InvalidHashError):
                    _replace_admin_password(
                        db,
                        account,
                        password,
                        action="sync-admin-password-env",
                        reason="Senha administrativa sincronizada com o ambiente durante a inicialização.",
                    )
                    return "updated"
            return "preserved"
        login = config.admin_login.strip().lower()
        name = config.admin_name.strip()
        password = config.admin_password.get_secret_value()
        if not login or not name or not password:
            raise ValueError(
                "Configure VIRA_ADMIN_LOGIN, VIRA_ADMIN_NAME e VIRA_ADMIN_PASSWORD no .env do backend."
            )
        if len(login) > 160 or len(name) > 100 or not 12 <= len(password) <= 128:
            raise ValueError(
                "Administrador inválido: login até 160 caracteres, nome até 100 e senha de 12 a 128."
            )
        if db.scalar(select(Account.id).where(Account.login == login)):
            raise ValueError(
                "O login configurado já pertence a outra conta. Escolha um login administrativo distinto."
            )
        db.add(Account(login=login, name=name, role="admin", password_hash=HASHER.hash(password)))
        return "created"


def _configured_admin_credentials(config):
    login = config.admin_login.strip().lower()
    password = config.admin_password.get_secret_value()
    if not login or not password:
        raise ValueError("Configure VIRA_ADMIN_LOGIN e VIRA_ADMIN_PASSWORD no .env do backend.")
    if len(login) > 160 or not 12 <= len(password) <= 128:
        raise ValueError("Administrador inválido: login até 160 caracteres e senha de 12 a 128.")
    return login, password


def _existing_admin(db, login, lock=False):
    query = select(Account).where(Account.login == login)
    account = db.scalar(query.with_for_update() if lock else query)
    if not account:
        raise ValueError(
            "VIRA_ADMIN_LOGIN não corresponde a uma conta existente. "
            "O .env só cria o primeiro administrador; não renomeia contas já gravadas. "
            "Informe o login administrativo original."
        )
    if account.role != "admin":
        raise ValueError(
            "VIRA_ADMIN_LOGIN corresponde a uma conta de aluno. "
            "Informe o login de uma conta administrativa existente; nenhuma conta foi promovida."
        )
    return account


def check_admin_credentials(sessions, config):
    login, password = _configured_admin_credentials(config)
    with sessions() as db:
        account = _existing_admin(db, login)
        try:
            HASHER.verify(account.password_hash, password)
        except (VerificationError, InvalidHashError):
            raise ValueError(
                "VIRA_ADMIN_PASSWORD não corresponde à senha gravada para VIRA_ADMIN_LOGIN. "
                "Execute script/run-local.sh para sincronizar a senha na inicialização, "
                "ou script/bootstrap-admin.py --reset-password no ambiente do backend."
            ) from None


def reset_admin_password(sessions, config):
    login, password = _configured_admin_credentials(config)
    with sessions.begin() as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(73210430)"))
        account = _existing_admin(db, login, lock=True)
        _replace_admin_password(
            db,
            account,
            password,
            action="reset-admin-password-cli",
            reason="Recuperação administrativa explícita via CLI local; senha fornecida pelo ambiente.",
        )


def _replace_admin_password(db, account, password, *, action, reason):
    account.password_hash = HASHER.hash(password)
    db.execute(delete(Session).where(Session.account_id == account.id))
    db.add(Audit(actor_id=account.id, action=action, resource_id=account.id, reason=reason))


def report_admin_bootstrap(sessions, config, result):
    if result == "updated":
        print("✅ Senha administrativa sincronizada com o .env; sessões anteriores dessa conta encerradas.")
    elif result == "preserved":
        print("✅ Administrador existente preservado; login e senha não foram alterados.")
    else:
        print("✅ Administrador criado com os dados do .env.")
    try:
        check_admin_credentials(sessions, config)
    except ValueError as error:
        print(f"⚠️ Verificação das credenciais administrativas: {error}")
    else:
        print("✅ Login e senha administrativos configurados conferem com o banco.")
    print("Acesso administrativo pelo backoffice: " + ", ".join(sorted(config.allowed_origins(admin=True))))
    print("Rota de login: /admin-api/v1/auth/login. A plataforma de alunos usa outro perfil.")
