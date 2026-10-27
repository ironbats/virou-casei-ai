import os
import time
from pathlib import Path

heartbeat = Path("/tmp/viroucaseai-worker-heartbeat")
maximum = int(os.environ.get("VIRA_PROVIDER_TIMEOUT", "180")) + 180
raise SystemExit(0 if heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < maximum else 1)
