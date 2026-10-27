from viracase.infrastructure.config import settings
from viracase.infrastructure.credentials import initialize_key
from viracase.infrastructure.database import database


def main():
    config = settings()
    engine, sessions = database(config.database_url)
    try:
        initialize_key(config.credential_key_path, sessions)
    finally:
        engine.dispose()
    print("✅ Proteção das credenciais de IA preparada; chave existente preservada.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(
            f"Falha ao preparar a proteção das credenciais: {type(error).__name__}. Confira o volume var/secrets e sua chave."
        ) from None
