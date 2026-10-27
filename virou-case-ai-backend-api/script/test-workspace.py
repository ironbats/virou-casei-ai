import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser(
    description="Valida a construção com IA em laboratório isolado, sem chamadas externas."
)
parser.add_argument("--frontend", type=Path, default=Path(__file__).resolve().parents[2] / "frontend")
args, extra = parser.parse_known_args()
backend = Path(__file__).resolve().parents[1]
frontend = args.frontend.resolve()
if not (frontend / "package.json").is_file():
    raise SystemExit("Informe --frontend com o diretório do frontend e instale suas dependências.")
environment = {
    **os.environ,
    "PYTHONPATH": str(backend / "src"),
    "VIRA_E2E_ISOLATED": "true",
    "VIRA_E2E_WORKSPACE": "true",
}
processes = []
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
subprocess.run(["npm", "run", "build"], cwd=frontend, env=environment, check=True)
try:
    for command, cwd in [
        ([sys.executable, str(backend / "tests/workspace_server.py")], backend),
        (
            [
                "node",
                "--input-type=module",
                "--eval",
                "import { preview } from 'vite'; await preview({preview:{proxy:{'/api':{target:'http://127.0.0.1:8000',changeOrigin:false}}}});",
            ],
            frontend,
        ),
    ]:
        processes.append(subprocess.Popen(command, cwd=cwd, env=environment, start_new_session=True))
    for port, path in [(8000, "/health/ready"), (5173, "/")]:
        for _ in range(60):
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("Um serviço de teste encerrou antes da validação.")
            try:
                with opener.open(f"http://127.0.0.1:{port}{path}", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError(f"Serviço de teste {port} não ficou pronto.")
    result = subprocess.run(
        ["npx", "playwright", "test", "tests/workspace.spec.ts", "tests/preview.spec.ts", *extra], cwd=frontend, env=environment
    )
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
