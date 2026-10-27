import time
import urllib.request

for port in (8000, 8001):
    for attempt in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=1) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.5)
    else:
        raise SystemExit(f"Serviço na porta {port} não ficou pronto. Verifique configuração e logs.")
