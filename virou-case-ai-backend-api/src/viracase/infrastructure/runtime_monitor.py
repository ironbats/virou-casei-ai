import threading
import time

from viracase.infrastructure.models import new_id
from viracase.infrastructure.observability import heartbeat, prune
from viracase.infrastructure.publication_build import IsolatedBuild
from viracase.infrastructure.publication_logging import emit, exception_fields
from viracase.infrastructure.publication_maintenance import schedule_automatic_verification


def diagnostic_signature(diagnostics):
    details = diagnostics.get("details", {})
    return (
        diagnostics.get("available"),
        diagnostics.get("fingerprint"),
        diagnostics.get("code"),
        tuple(diagnostics.get("failed_checks", [])),
        details.get("stage"),
        details.get("exit_code"),
        details.get("stderr"),
        details.get("exception_type"),
        diagnostics.get("environment", {}).get("apparmor_profile"),
    )


def start_monitor(repo, stop):
    identity = "worker:" + new_id()

    def run():
        cleaned, last_logged, previous, diagnostic_id = 0, 0, None, None
        while not stop.is_set():
            try:
                diagnostics = IsolatedBuild().diagnostics()
                signature = diagnostic_signature(diagnostics)
                if signature == previous and diagnostic_id:
                    diagnostics["diagnostic_id"] = diagnostic_id
                else:
                    diagnostic_id = diagnostics.get("diagnostic_id")
                heartbeat(repo, identity, diagnostics)
                if repo is not None:
                    schedule_automatic_verification(repo)
                now = time.monotonic()
                if signature != previous or now - last_logged >= 300:
                    emit(
                        "executor_ready" if diagnostics["available"] else "executor_unavailable",
                        "info" if diagnostics["available"] else "error",
                        worker_id=identity,
                        **diagnostics,
                    )
                    previous, last_logged = signature, now
                if now - cleaned > 3600:
                    prune(repo)
                    cleaned = now
            except Exception as exc:
                emit("runtime_monitor_failed", "error", worker_id=identity, **exception_fields(exc))
            stop.wait(15)

    thread = threading.Thread(target=run, name="runtime-monitor", daemon=True)
    thread.start()
    return thread
