import os
from pathlib import Path
import subprocess
import sys
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine
from viracase.infrastructure.models import Base


def test_initial_migration_repeat_and_model_consistency(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    url = "sqlite:///" + str(tmp_path / "migration.db")
    env = {
        **os.environ,
        "VIRA_MIGRATION_DATABASE_URL": url,
        "VIRA_DATABASE_URL": url,
        "PYTHONPATH": str(backend / "src"),
    }
    for _ in range(2):
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=backend,
            env=env,
            check=True,
            capture_output=True,
        )
    engine = create_engine(url)
    with engine.connect() as connection:
        assert not compare_metadata(MigrationContext.configure(connection), Base.metadata)
    engine.dispose()
