import os
from pathlib import Path

uid = int(os.environ.get("VIRA_RUNTIME_UID", "10001"))
gid = int(os.environ.get("VIRA_RUNTIME_GID", "10001"))
if uid < 1 or gid < 1:
    raise SystemExit("O usuário de execução deve ser não root.")
for root in (Path("/app/var/sources"), Path("/app/var/secrets")):
    root.mkdir(parents=True, exist_ok=True)
    for directory, directories, files in os.walk(root, followlinks=False):
        for path in [Path(directory), *(Path(directory) / name for name in directories + files)]:
            if path.is_symlink():
                raise SystemExit("Link simbólico inesperado no volume de fontes. Inicialização interrompida.")
            os.chown(path, uid, gid, follow_symlinks=False)
            path.chmod(0o700 if path.is_dir() else 0o600)
print("✅ Permissões dos volumes de fontes e credenciais preparadas; conteúdo preservado.")
