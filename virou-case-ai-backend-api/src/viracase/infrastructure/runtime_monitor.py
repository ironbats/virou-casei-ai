import logging
import threading
import time

from viracase.infrastructure.models import new_id
from viracase.infrastructure.observability import heartbeat, prune
from viracase.infrastructure.publication_build import IsolatedBuild


def start_monitor(repo, stop):
    identity = "worker:" + new_id()

    def run():
        cleaned = 0
        while not stop.is_set():
            try:
                heartbeat(repo, identity, IsolatedBuild().diagnostics())
                if time.monotonic() - cleaned > 3600:
                    prune(repo)
                    cleaned = time.monotonic()
            except Exception as exc:
                logging.getLogger("viracase.worker").warning("runtime_monitor_failed:%s", type(exc).__name__)
            stop.wait(15)

    thread = threading.Thread(target=run, name="runtime-monitor", daemon=True)
    thread.start()
    return thread
