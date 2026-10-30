from copy import deepcopy

from viracase.domain.rules import utcnow


def queue_verification_attempt(operation):
    result = dict(operation.result or {})
    attempt = result.get("verification_attempt", 0)
    legacy_attempt = not attempt and (
        operation.state in {"succeeded", "failed", "cancelled", "reconciling"}
        or bool(
            result.get("verification_failure")
            or result.get("stack_results")
            or result.get("verification_steps")
        )
        or any(
            event.get("stage") == "verification_isolation"
            for event in result.get("activity", [])
            if isinstance(event, dict)
        )
    )
    if legacy_attempt:
        attempt = 1
        result["verification_attempt"] = attempt
    if attempt:
        history = list(result.get("verification_attempt_history", []))
        history.append(
            {
                "attempt": attempt,
                "state": operation.state
                if operation.state in {"succeeded", "failed", "cancelled"}
                else "unknown",
                "interrupted_state": operation.state
                if operation.state not in {"succeeded", "failed", "cancelled"}
                else None,
                "error_code": operation.error_code,
                "progress_stage": operation.progress_stage,
                "updated_at": operation.updated_at.isoformat() if operation.updated_at else None,
                "result": deepcopy(
                    {key: value for key, value in result.items() if key != "verification_attempt_history"}
                ),
            }
        )
        result["verification_attempt_history"] = history
    result.update(
        verification_attempt=attempt + 1,
        verification_started_at=None,
        verification_finished_at=None,
        verification_steps=[],
        verification_events=[],
        verification_failure=None,
        remote_verification=None,
        stack_results=[],
        verification_stack=None,
    )
    operation.result = result


def begin_verification_attempt(operation):
    if not (operation.result or {}).get("verification_attempt"):
        queue_verification_attempt(operation)
    result = dict(operation.result or {})
    result["verification_started_at"] = result.get("verification_started_at") or utcnow().isoformat()
    operation.result = result


def record_verification_step(operation, stage, profile=None, state="running", error_code=None):
    result = dict(operation.result or {})
    steps = [dict(step) for step in result.get("verification_steps", [])]
    identity = f"{profile or 'connection'}:{stage}"
    step = next((item for item in steps if item["id"] == identity), None)
    if step and (step["state"] == state or state == "running" and step["state"] != "running"):
        return
    now = utcnow().isoformat()
    if step is None:
        step = {
            "id": identity,
            "stage": stage,
            "profile_id": profile,
            "order": len(steps),
            "started_at": now if state == "running" else None,
        }
        steps.append(step)
    step.update(
        state=state,
        error_code=error_code,
        finished_at=now if state in {"succeeded", "failed", "skipped", "cancelled"} else None,
    )
    events = list(result.get("verification_events", []))
    events.append(
        {
            "id": f"{operation.id}:{result.get('verification_attempt', 1)}:{len(events) + 1}",
            "order": len(events) + 1,
            "stage": stage,
            "profile_id": profile,
            "state": state,
            "at": now,
            "error_code": error_code,
        }
    )
    operation.result = {**result, "verification_steps": steps, "verification_events": events}


def finish_verification_attempt(operation):
    operation.result = {**(operation.result or {}), "verification_finished_at": utcnow().isoformat()}
