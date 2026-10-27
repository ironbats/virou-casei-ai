import os
import sys
from configparser import Error as ConfigError
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util import CommandError


def validate(root):
    migration_root = root / "migrations"
    versions = migration_root / "versions"
    if not versions.is_dir():
        raise CommandError("O diretório migrations/versions está ausente ou inacessível.")
    with (migration_root / "env.py").open("rb") as stream:
        stream.read(1)

    def unreadable(error):
        raise error

    for directory, _, files in os.walk(versions, onerror=unreadable):
        for name in files:
            if name.endswith(".py"):
                with (Path(directory) / name).open("rb") as stream:
                    stream.read(1)
    scripts = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    revision = scripts.get_revision("0001")
    if revision is None:
        raise CommandError("A revisão obrigatória 0001 não foi localizada.")
    heads = scripts.get_heads()
    if len(heads) != 1:
        raise CommandError("O pacote deve conter um único head de migrations.")
    tuple(scripts.walk_revisions())
    return heads[0]


if __name__ == "__main__":
    try:
        head = validate(Path(__file__).resolve().parents[1])
    except (
        OSError,
        CommandError,
        RevisionError,
        KeyError,
        SyntaxError,
        ImportError,
        ConfigError,
        ValueError,
    ) as error:
        print(
            f"❌ Pacote de migrations inválido ({type(error).__name__}): {error}\n"
            "Confira os arquivos e as permissões da imagem. Nenhuma alteração no banco foi executada.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(f"✅ Revisão 0001 localizada e legível; head={head}; usuário de execução={os.geteuid()}.")
