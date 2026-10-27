from pathlib import Path
import secrets

target = Path(".env")
if not target.exists():
    migration = secrets.token_hex(24)
    application = secrets.token_hex(24)
    data = (
        Path(".env.example")
        .read_text()
        .replace("replace_with_generated_migration_password", migration)
        .replace("replace_with_generated_application_password", application)
    )
    target.write_text(data)
    target.chmod(0o600)
    print("✅ Configuração local criada com credenciais aleatórias. Nenhum segredo foi exibido.")
else:
    print("✅ Configuração existente preservada.")
