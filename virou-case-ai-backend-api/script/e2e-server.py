from decimal import Decimal
from pathlib import Path
import os
import signal
import subprocess
import sys
import tempfile
import time
from viracase.domain.rules import Access, utcnow
from viracase.infrastructure.database import database
from viracase.infrastructure.models import Account, Grant, Period, new_id
from viracase.infrastructure.repository import HASHER

base = Path(__file__).resolve().parents[1]
temporary = tempfile.TemporaryDirectory(prefix="viracase-e2e-")
root = Path(temporary.name)
url = "sqlite:///" + str(root / "test.db")
os.environ.update(
    VIRA_DATABASE_URL=url,
    VIRA_E2E_ISOLATED="true",
    VIRA_MIGRATION_DATABASE_URL=url,
    VIRA_SOURCE_ROOT=str(root / "sources"),
    VIRA_CREDENTIAL_KEY_PATH=str(root / "secrets/credentials.key"),
    VIRA_CONTENT_ROOT=str(base / "content"),
    VIRA_CATALOG_PATH=str(base / "config/models.json"),
    VIRA_PUBLIC_HOSTS="127.0.0.1,localhost",
    VIRA_PUBLIC_ORIGIN="http://127.0.0.1:5173,http://localhost:5173",
    VIRA_ADMIN_ORIGIN="http://127.0.0.1:5174,http://localhost:5174,http://127.0.0.1:5175,http://localhost:5175",
    VIRA_COOKIE_SECURE="false",
    VIRA_LIFECYCLE_ENABLED="false",
)
subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=base, check=True)
engine, sessions = database(url)
from viracase.infrastructure.credentials import initialize_key
initialize_key(root / "secrets/credentials.key", sessions)
os.environ.update(VIRA_OPENAI_API_KEY="", VIRA_GEMINI_API_KEY="")
with sessions.begin() as db:
    admin = Account(
        id=new_id(),
        login="operator-e2e",
        name="Operador E2E",
        role="admin",
        password_hash=HASHER.hash("e2e-operator-only-password"),
    )
    student = Account(
        id=new_id(),
        login="student-e2e",
        name="Aluno E2E",
        password_hash=HASHER.hash("e2e-student-only-password"),
        entitlement_version=1,
    )
    db.add_all([admin, student])
    db.flush()
    access = Access.activate(utcnow())
    period = Period(
        id=new_id(),
        account_id=student.id,
        starts_at=access.starts_at,
        expires_at=access.expires_at,
        purge_at=access.purge_at,
        version=1,
    )
    db.add(period)
    db.flush()
    student.current_period_id = period.id
    db.add(
        Grant(
            account_id=student.id,
            period_id=period.id,
            kind="included",
            total=Decimal(50),
            available=Decimal(50),
        )
    )
engine.dispose()
commands = [
    [
        sys.executable,
        "-m",
        "uvicorn",
        "viracase.interfaces.public:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--log-level",
        "warning",
    ],
    [
        sys.executable,
        "-m",
        "uvicorn",
        "e2e_ai_app:app",
        "--app-dir",
        str(base / "tests"),
        "--host",
        "127.0.0.1",
        "--port",
        "8001",
        "--log-level",
        "warning",
    ],
    [sys.executable, "-m", "viracase.infrastructure.worker"],
]
processes = [subprocess.Popen(command, cwd=base) for command in commands]
stopped = False


def stop(*args):
    global stopped
    stopped = True


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
print("Ambiente E2E isolado iniciado; contas sintéticas exclusivas de teste.", flush=True)
try:
    while not stopped:
        if any(p.poll() is not None for p in processes):
            raise SystemExit("Um serviço E2E encerrou.")
        time.sleep(0.5)
finally:
    for process in processes:
        process.terminate()
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    temporary.cleanup()
