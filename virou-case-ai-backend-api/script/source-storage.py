import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID


def canonical_uuid(value):
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError):
        return False


def env_source(root):
    setting = os.environ.get("VIRA_SOURCE_ROOT", "").strip()
    if not setting and (root / ".env").is_file():
        for line in (root / ".env").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip().removeprefix("export ").strip() == "VIRA_SOURCE_ROOT":
                setting = value.strip().strip("\"'")
                break
    if not setting or setting in {"var/sources", "./var/sources"}:
        return None
    if "${" in setting:
        raise ValueError("VIRA_SOURCE_ROOT invalido: interpolacao nao suportada neste campo")
    return (root / setting).resolve()


def previous_worker_mount(container):
    if not container:
        return None
    result = subprocess.run(
        ["docker", "inspect", "--format", '{{range .Mounts}}{{if eq .Destination "/app/var/sources"}}{{.Source}}{{end}}{{end}}', container],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def candidates(root, existing=None):
    paths = [existing, root / "var" / "sources"]
    for candidate in sorted(root.parent.glob("virou-case-ai-backend-api*/var/sources")):
        paths.append(candidate)
    for value in os.environ.get("VIRA_SOURCE_IMPORT_ROOTS", "").split(os.pathsep):
        if value.strip():
            paths.append(Path(value).expanduser())
    unique = []
    for value in paths:
        if value is None:
            continue
        value = Path(value)
        if value.is_symlink():
            raise ValueError(f"Origem nao segura: {value}")
        resolved = value.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def revisions(source):
    if not source.exists():
        return
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"Origem invalida: {source}")
    for account in sorted(source.iterdir()):
        if account.is_symlink():
            raise ValueError(f"Link simbolico rejeitado: {account}")
        if not account.is_dir() or not canonical_uuid(account.name):
            continue
        for project in sorted(account.iterdir()):
            if project.is_symlink():
                raise ValueError(f"Link simbolico rejeitado: {project}")
            if not project.is_dir() or not canonical_uuid(project.name):
                continue
            for path in sorted(project.iterdir()):
                if path.is_symlink():
                    raise ValueError(f"Link simbolico rejeitado: {path}")
                if path.is_file() and path.suffix == ".json" and canonical_uuid(path.stem):
                    yield path.relative_to(source), path


def copy_sources(target, origins):
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError("Armazenamento de destino nao pode ser link simbolico")
    count = 0
    for source in origins:
        if source == target or not source.exists():
            continue
        for relative, original in revisions(source):
            if (target / ".deleted-projects" / relative.parts[0] / relative.parts[1]).exists():
                continue
            destination = target / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            data = original.read_bytes()
            if not isinstance(json.loads(data), dict):
                raise ValueError(f"Revisao invalida na origem: {relative}")
            if destination.exists():
                if destination.is_symlink() or hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(data).digest():
                    raise ValueError(f"SOURCE_STORAGE_CONFLICT: {relative}")
                continue
            fd, temporary = tempfile.mkstemp(dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o600)
                try:
                    os.link(temporary, destination)
                    count += 1
                except FileExistsError:
                    if destination.read_bytes() != data:
                        raise ValueError(f"SOURCE_STORAGE_CONFLICT: {relative}")
            finally:
                os.unlink(temporary)
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-container", default="")
    parser.add_argument("--root", default=".")
    parser.add_argument("--rollback-to", default="")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    configured = env_source(root)
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    target = configured or (Path(base) / "viroucaseai" / "sources").resolve()
    target = target.expanduser().resolve()
    if args.rollback_to:
        destination = (root / args.rollback_to).resolve()
        count = copy_sources(destination, [target])
        print(f"SOURCE_STORAGE_ROLLBACK_READY: destino={destination} revisoes_exportadas={count}", file=sys.stderr)
        print(destination)
        return
    existing = previous_worker_mount(args.worker_container)
    count = copy_sources(target, candidates(root, existing))
    print(f"SOURCE_STORAGE_READY: destino_estavel={target} revisoes_importadas={count}", file=sys.stderr)
    print(target)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"SOURCE_STORAGE_BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(1)
