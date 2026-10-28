import hashlib
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from viracase.infrastructure.models import Account

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture
def image_tree(tmp_path):
    image = tmp_path / "image"
    image.mkdir()
    for name in ("src", "migrations", "script", "content", "config", "build-toolchain"):
        shutil.copytree(
            BACKEND / name,
            image / name,
            ignore=shutil.ignore_patterns(
                "__pycache__",
                "*.pyc",
                "*.egg-info",
                "node_modules",
                ".venv-workers",
                "python_modules",
                "python-vendor.json",
            ),
        )
    for name in ("Dockerfile", "alembic.ini", "pyproject.toml", "requirements.lock", ".dockerignore"):
        shutil.copy2(BACKEND / name, image / name)
    for path in image.rglob("*"):
        path.chmod(0o750 if path.is_dir() else 0o640)
    (image / "script/run-local.sh").chmod(0o750)
    return image


def environment(image, url):
    return {
        **{key: value for key, value in os.environ.items() if not key.startswith("VIRA_")},
        "PYTHONPATH": str(image / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "VIRA_CONTAINERIZED": "false",
        "VIRA_DATABASE_URL": url,
        "VIRA_MIGRATION_DATABASE_URL": url,
    }


def run(image, args, url, cwd=None):
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd or image,
        env=environment(image, url),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def normalize_image_permissions(image):
    commands = [
        line.removeprefix("RUN ")
        for line in (image / "Dockerfile").read_text().splitlines()
        if line.startswith("RUN chmod -R u=rwX,go=rX ")
    ]
    assert len(commands) == 1
    command = commands[0].replace("/app", shlex.quote(str(image)))
    subprocess.run(["bash", "-c", command], check=True, capture_output=True, text=True)


def seed_database(image, url):
    result = run(image, ["-m", "alembic", "upgrade", "0001"], url)
    assert result.returncode == 0, result.stdout + result.stderr
    engine = create_engine(url)
    try:
        with Session(engine) as session, session.begin():
            session.add(
                Account(
                    id="existing-admin",
                    login="keep-existing-admin",
                    name="Administrador existente",
                    role="admin",
                    password_hash="synthetic-existing-hash-preserved",
                )
            )
    finally:
        engine.dispose()


def test_docker_permission_step_handles_750_without_changing_data_or_secrets(image_tree):
    secret = image_tree / ".env"
    secret.write_text("SYNTHETIC_SECRET=not-a-real-credential\n")
    secret.chmod(0o600)
    sources = image_tree / "var/sources"
    sources.mkdir(parents=True, mode=0o700)
    sources.chmod(0o700)
    stored = sources / "existing.json"
    stored.write_text('{"preserve": true}')
    stored.chmod(0o600)
    before = {
        p.relative_to(image_tree): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in image_tree.rglob("*")
        if p.is_file()
    }
    normalize_image_permissions(image_tree)
    for name in ("src", "migrations", "content", "config", "script"):
        for path in [image_tree / name, *(image_tree / name).rglob("*")]:
            mode = stat.S_IMODE(path.stat().st_mode)
            if path.is_dir():
                assert mode == 0o755
            else:
                assert mode in (0o644, 0o755)
            assert not mode & 0o022
    for relative, digest in before.items():
        assert hashlib.sha256((image_tree / relative).read_bytes()).hexdigest() == digest
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE(sources.stat().st_mode) == 0o700
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600


def test_migration_check_is_offline_and_independent_of_working_directory(image_tree, tmp_path):
    result = run(
        image_tree,
        [str(image_tree / "script/check-migrations.py")],
        "postgresql+psycopg://invalid:invalid@127.0.0.1:1/unreachable",
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0001 localizada e legível" in result.stdout


@pytest.mark.parametrize("failure", ["missing", "unreadable"])
def test_invalid_migration_package_stops_before_touching_database(image_tree, tmp_path, failure):
    db_file = tmp_path / "existing.db"
    url = "sqlite:///" + str(db_file)
    seed_database(image_tree, url)
    before = db_file.read_bytes()
    versions = image_tree / "migrations/versions"
    if failure == "missing":
        (versions / "0001_initial.py").unlink()
    else:
        versions.chmod(0)
        try:
            list(versions.iterdir())
        except PermissionError:
            pass
        else:
            versions.chmod(0o750)
            pytest.skip(
                "O processo tem privilégio para ignorar permissões Unix; validar com usuário não root."
            )
    try:
        result = run(image_tree, ["script/container-initialize.py"], url)
    finally:
        versions.chmod(0o750)
    assert result.returncode == 1
    assert "Pacote de migrations inválido" in result.stderr
    assert "[2/4]" not in result.stdout
    assert "Nenhuma alteração no banco foi executada" in result.stderr
    assert db_file.read_bytes() == before


def test_existing_revision_and_admin_survive_permission_fix_and_upgrade(image_tree, tmp_path):
    url = "sqlite:///" + str(tmp_path / "existing.db")
    seed_database(image_tree, url)
    normalize_image_permissions(image_tree)
    for _ in range(2):
        result = run(
            image_tree,
            ["-m", "alembic", "--config", str(image_tree / "alembic.ini"), "upgrade", "head"],
            url,
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            accounts = session.scalars(select(Account)).all()
            assert len(accounts) == 1
            assert accounts[0].id == "existing-admin"
            assert accounts[0].password_hash == "synthetic-existing-hash-preserved"
            assert accounts[0].login == "keep-existing-admin"
            assert session.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
    finally:
        engine.dispose()


def test_migration_graph_with_missing_parent_is_rejected(image_tree):
    (image_tree / "migrations/versions/broken.py").write_text(
        "revision = '0003'\ndown_revision = 'missing-parent'\n"
    )
    result = run(image_tree, ["script/check-migrations.py"], "sqlite://")
    assert result.returncode == 1
    assert "Pacote de migrations inválido" in result.stderr


@pytest.mark.skipif(
    os.getenv("VIRA_TEST_DOCKER") != "1", reason="Defina VIRA_TEST_DOCKER=1 para testar a imagem Docker real."
)
def test_image_with_750_sources_is_readable_by_arbitrary_non_root_users(image_tree):
    assert shutil.which("docker"), "Docker é necessário para VIRA_TEST_DOCKER=1"
    tag = "viroucaseai-migrations-test:" + uuid4().hex
    try:
        subprocess.run(["docker", "build", "-t", tag, str(image_tree)], check=True, timeout=600)
        for uid in (10001, 1000, 34567):
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--user",
                    f"{uid}:{uid}",
                    tag,
                    "python",
                    "script/check-migrations.py",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            assert f"usuário de execução={uid}" in result.stdout
    finally:
        subprocess.run(["docker", "image", "rm", tag], capture_output=True, check=False, timeout=60)
