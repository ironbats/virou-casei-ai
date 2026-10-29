from viracase.application.progress import STAGES, progress_message
from viracase.domain.rules import aware


def iso(value):
    return aware(value).isoformat() if value else None


def project_dto(p):
    return {
        "id": p.id,
        "name": p.name,
        "objective": p.objective,
        "stack": p.stack,
        "version": p.version,
        "current_revision_id": p.current_revision_id,
        "state": p.state,
        "created_at": iso(p.created_at),
    }


def revision_dto(r):
    return {
        "id": r.id,
        "project_id": r.project_id,
        "source_hash": r.source_hash,
        "files": r.manifest,
        "origin": r.origin,
        "candidate": r.candidate,
        "created_at": iso(r.created_at),
    }


def operation_request_text(o):
    if o.type != "generation":
        return None
    if o.payload.get("analysis_id") and not o.payload.get("analysis_context"):
        return "Corrija os problemas de código identificados na análise desta revisão."
    return o.payload.get("brief")


def operation_dto(o, admin=False):
    result_ref = {k: v for k, v in o.result.items() if k != "activity"}
    if o.type == "publication":
        result_ref = {"revision_id": o.payload.get("revision_id"), **result_ref}
        if not admin:
            result_ref.pop("publication_resolution", None)
            result_ref.pop("publication_recovery_assessment", None)
            result_ref.pop("publication_developer_recovery", None)
            result_ref.pop("publication_developer_recovery_history", None)
        if "publication_diagnostic" not in result_ref and o.result.get("publication_failure"):
            from viracase.infrastructure.publication_outcome import publication_diagnostic

            result_ref["publication_diagnostic"] = publication_diagnostic(
                o.result, stage=o.result["publication_failure"].get("stage"),
                operation_id=o.id, revision_id=o.payload.get("revision_id"),
            )
    return {
        "id": o.id,
        "type": o.type,
        "project_id": o.project_id,
        "state": o.state,
        "progress_stage": o.progress_stage,
        "billing_status": o.billing_status,
        "pending_reconciliation": o.billing_status == "reconciliation_pending" or o.state == "reconciling",
        "cancel_requested": o.cancel_requested,
        "result_ref": {"preparation_state": o.result.get("preparation", {}).get("state"), "attachment_count": len(o.payload.get("items", []))} if o.type == "template_preparation" else result_ref,
        "request_text": operation_request_text(o),
        **(
            {
                "analysis_id": o.payload["analysis_id"],
                "analysis_revision_id": o.payload.get("base_revision_id"),
            }
            if o.type == "generation" and o.payload.get("analysis_id")
            else {}
        ),
        "reply_to": o.payload.get("reply_to") if o.type == "generation" else None,
        "attachment_snapshots": o.payload.get("attachment_snapshots", []),
        "progress_message": progress_message(o),
        "activity": [
            {**event, "message": STAGES.get(event["stage"], progress_message(o))}
            for event in o.result.get("activity", [])
        ],
        "error_code": o.error_code,
        "model_label": o.policy.get("model") if admin else None,
        "created_at": iso(o.created_at),
        "updated_at": iso(o.updated_at),
    }
