import json
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from viracase.domain.rules import RuleError
from viracase.infrastructure.models import AIConnection


def initialize_key(path, sessions):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("O diretório de credenciais não pode conter links simbólicos.")
    if path.exists():
        Fernet(path.read_bytes())
        return
    with sessions() as db:
        if db.scalar(select(AIConnection.id).limit(1)):
            raise ValueError(
                "Restaure var/secrets/credentials.key: existem credenciais criptografadas no banco."
            )
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".credential-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(Fernet.generate_key())
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            Fernet(path.read_bytes())
    finally:
        os.unlink(temporary)


class CredentialVault:
    def __init__(self, path):
        self.path = path

    def cipher(self):
        try:
            return Fernet(self.path.read_bytes())
        except (OSError, ValueError):
            raise RuleError(
                "CREDENTIAL_STORE_UNAVAILABLE", "Armazenamento de credenciais indisponível.", 503
            ) from None

    def encrypt(self, identity, value):
        return self.cipher().encrypt(json.dumps({"id": identity, "value": value}).encode()).decode()

    def decrypt(self, identity, value):
        try:
            data = json.loads(self.cipher().decrypt(value.encode()))
            if data["id"] != identity:
                raise ValueError
            return data["value"]
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise RuleError(
                "CREDENTIAL_STORE_UNAVAILABLE", "Não foi possível ler a credencial protegida.", 503
            ) from None
