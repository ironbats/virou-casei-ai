from pathlib import Path
import os
import signal
import subprocess
import sys
import time
import urllib.request

backend = Path(__file__).resolve().parents[1]
root = backend.parent
environment = {**os.environ, "PYTHONPATH": str(backend / "src")}
commands = [
    ([sys.executable, str(backend / "script/e2e-server.py")], backend),
    (["npm", "run", "dev"], root / "frontend"),
    (["npm", "run", "dev"], root / "backoffice"),
    (["npm", "run", "dev", "--", "--port", "5175"], root / "backoffice"),
]
processes = []
try:
    for command, cwd in commands:
        processes.append(subprocess.Popen(command, cwd=cwd, env=environment, start_new_session=True))
    for port, path in [(8000, "/health/ready"), (8001, "/health/ready"), (5173, "/"), (5174, "/"), (5175, "/")]:
        for attempt in range(60):
            if any(p.poll() is not None for p in processes):
                raise RuntimeError("Um serviço encerrou antes da validação")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError(f"Serviço {port} não ficou pronto")
    result = subprocess.run(
        ["npx", "playwright", "test", *sys.argv[1:]], cwd=root / "frontend", env=environment
    )
    if result.returncode:
        raise SystemExit(result.returncode)
    result = subprocess.run(["npx", "playwright", "test"], cwd=root / "backoffice", env=environment)
    raise SystemExit(result.returncode)
finally:
    for process in processes:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
    for process in processes:
        try:
            process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
