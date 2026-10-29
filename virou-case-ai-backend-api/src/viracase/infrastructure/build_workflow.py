import copy
from datetime import timedelta

from sqlalchemy import select

from viracase.application.template_preparation_proposals import explicit_empty_target
from viracase.domain.rules import RuleError, canonical_hash, utcnow
from viracase.domain.template_fields import attachment_errors, empty, normalized_fields, render
from viracase.infrastructure.models import Job, Operation, TemplateVersion, new_id
from viracase.infrastructure.workflow_logging import current_context, emit


ACTIVE_FILL = {"queued", "filling", "reconciling"}
SERVER_FIELDS = {
    "request_id",
    "workflow_version",
    "context_revision",
    "context_hash",
    "context_empty",
    "prepared_fingerprint",
    "recommendation_status",
    "templates_completed",
    "templates_completed_at",
    "empty_selection_confirmed",
    "review_snapshot",
    "selection_revision",
    "event_sequence",
    "correlation_id",
    "preparation_id",
}
FILL_FIELDS = {
    "fill_state",
    "fill_message",
    "fill_error_code",
    "fill_questions",
    "fill_context",
    "fill_request_id",
    "preparation_id",
    "filled_at",
    "field_generations",
}


def field_generation(item, key, state, operation=None, error_code=None, message=None):
    value = {
        **item.get("field_generations", {}).get(key, {}),
        "state": state,
        "error_code": error_code,
        "message": message
        or {
            "queued": "Sugestão solicitada para este campo.",
            "filling": "Gerando sugestão para este campo.",
            "reconciling": "Aguarde a conciliação antes de gerar novamente.",
            "succeeded": "Sugestão da IA pronta para revisão.",
            "failed": "Não foi possível gerar este campo. Você pode tentar novamente.",
            "stale": "O campo ou o contexto mudou. Sua edição foi preservada.",
            "cancelled": "A geração deste campo foi cancelada.",
        }[state],
    }
    if operation:
        value["operation_id"] = operation.id
        value["request_id"] = operation.payload.get("http_request_id")
    item["field_generations"] = {**item.get("field_generations", {}), key: value}


def idea_fingerprint(data):
    return canonical_hash(
        {key: data.get(key) for key in ("objective", "message_text", "context_revision", "context_hash")}
    )


def selection_fingerprint(data):
    return canonical_hash(
        [
            {key: item.get(key) for key in ("template_id", "source_version_id", "fields", "edited_content")}
            for item in data.get("attachments", [])
            if item.get("enabled")
        ]
    )


def invalidate_review(data):
    data.update(
        templates_completed=False,
        templates_completed_at=None,
        empty_selection_confirmed=False,
        review_snapshot=None,
    )


def record_transition(repo, db, actor, pid, data, revision, event):
    from viracase.infrastructure.integration_traces import TraceStore

    snapshot = data.get("review_snapshot") or {}
    selected = [item for item in data["attachments"] if item["enabled"]]
    TraceStore(repo).put(
        {
            "trace_id": data.get("correlation_id") or data.get("recommendation_id") or data["request_id"],
            "account_id": actor,
            "project_id": pid,
            "stage": "review" if event == "review_ready" else "preparation",
            "draft_revision": revision,
            "project_revision": data.get("context_revision"),
            "snapshot_id": snapshot.get("hash"),
            "template_versions": [
                {
                    "attachment_id": item["template_id"],
                    "source_version_id": item["source_version_id"],
                    "attachment_revision": item["attachment_revision"],
                }
                for item in selected
            ],
        },
        "normalized",
        event,
        details={
            "request_id": data["request_id"],
            "selection_revision": data.get("selection_revision", 0),
            "selected_count": len(selected),
            "empty_selection_confirmed": data.get("empty_selection_confirmed", False),
        },
        db=db,
    )


def context_snapshot(repo, db, actor, project):
    revision = project.current_revision_id
    source = repo.owned_revision(db, actor, project.id, revision) if revision else None
    return {
        "context_revision": revision,
        "context_hash": source.source_hash if source else canonical_hash({}),
        "context_empty": source is None,
    }


def initialize(data):
    result = copy.deepcopy(data)
    if result.get("workflow_version") != 1:
        result.update(
            request_id=new_id(),
            workflow_version=1,
            stage="idea",
            context_revision=None,
            context_hash=None,
            context_empty=False,
            prepared_fingerprint=None,
            recommendation_status=None,
            selection_revision=0,
            event_sequence=0,
        )
        invalidate_review(result)
        result["recommendation_id"] = None
    return result


def decorate(repo, db, actor, project, data, revision):
    value = initialize(data)
    reasons = []
    if not value.get("message_text", "").strip():
        reasons.append({"code": "IDEA_REQUIRED", "message": "Descreva o que vamos fazer na etapa Ideia."})
    prepared = bool(value.get("prepared_fingerprint"))
    if not prepared or value["prepared_fingerprint"] != idea_fingerprint(value):
        reasons.append(
            {"code": "TEMPLATES_STAGE_INCOMPLETE", "message": "Prepare a construção na etapa Ideia."}
        )
    elif project.current_revision_id != value.get("context_revision"):
        reasons.append(
            {
                "code": "CONTEXT_STALE",
                "message": "O projeto possui uma nova revisão. Volte à Ideia e atualize a preparação.",
            }
        )
    if value.get("recommendation_status") == "pending":
        reasons.append({"code": "RECOMMENDATION_PENDING", "message": "Aguarde a busca de templates."})
    preparation_ids = (
        {item.get("preparation_id") for item in value.get("attachments", [])}
        | {
            generation.get("operation_id")
            for item in value.get("attachments", [])
            for generation in item.get("field_generations", {}).values()
        }
    ) - {None}
    operations = (
        {
            operation.id: operation
            for operation in db.scalars(
                select(Operation).where(
                    Operation.account_id == actor,
                    Operation.project_id == project.id,
                    Operation.id.in_(preparation_ids),
                )
            ).all()
        }
        if preparation_ids
        else {}
    )
    for item in value.get("attachments", []):
        if not item.get("enabled"):
            continue
        state = item.get("fill_state")
        operation = operations.get(item.get("preparation_id"))
        for key, generation in item.get("field_generations", {}).items():
            field_operation = operations.get(generation.get("operation_id"))
            if field_operation and field_operation.type == "template_autofill" and operation:
                target = next(
                    (
                        source.get("explicit_empty_target")
                        for source in operation.payload.get("items", [])
                        if source["attachment_id"] == item["template_id"]
                    ),
                    None,
                )
                if target and target["field_id"] == key:
                    field_operation = operation
            if field_operation and field_operation.state in {"queued", "running", "reconciling"}:
                field_generation(
                    item,
                    key,
                    "filling" if field_operation.state == "running" else field_operation.state,
                    field_operation,
                    field_operation.error_code,
                )
        if operation and operation.state in {"queued", "running", "reconciling"}:
            state = item["fill_state"] = "filling" if operation.state == "running" else operation.state
        if prepared and item.get("fill_context") and item["fill_context"] != value["prepared_fingerprint"]:
            state = item["fill_state"] = "stale"
        if state in ACTIVE_FILL:
            reasons.append(
                {
                    "code": "FILL_PENDING",
                    "attachment_id": item["template_id"],
                    "message": "Aguarde o preenchimento de " + item["title"] + ".",
                }
            )
        if state == "stale":
            reasons.append(
                {
                    "code": "CONTEXT_STALE",
                    "attachment_id": item["template_id"],
                    "message": "Atualize a preparação de " + item["title"] + ".",
                }
            )
        reasons.extend(item.get("validation_errors", []))
        if not item.get("edited_content", "").strip():
            reasons.append(
                {
                    "code": "ATTACHMENT_EMPTY",
                    "attachment_id": item["template_id"],
                    "message": "Revise o conteúdo de " + item["title"] + ".",
                }
            )
    snapshot = value.get("review_snapshot")
    valid_review = bool(
        snapshot
        and value.get("templates_completed")
        and snapshot.get("selection_hash") == selection_fingerprint(value)
        and snapshot.get("prepared_fingerprint") == value.get("prepared_fingerprint")
    )
    value["can_review"] = not reasons
    value["can_build"] = not reasons and valid_review and value["stage"] == "review"
    value["blocking_reasons"] = reasons
    value["event_sequence"] = revision
    return value


def transition(repo, db, actor, project, previous, saved, requested_stage, version):
    previous = initialize(previous)
    result = {**saved, **{key: previous[key] for key in SERVER_FIELDS if key in previous}}
    changed_idea = any(saved.get(key) != previous.get(key) for key in ("objective", "message_text"))
    changed_selection = selection_fingerprint(saved) != selection_fingerprint(previous)
    if changed_idea:
        result.update(prepared_fingerprint=None, recommendation_id=None, recommendation_status=None)
        invalidate_review(result)
    if changed_selection:
        result["selection_revision"] = previous.get("selection_revision", 0) + 1
        invalidate_review(result)
    old_items = {item["template_id"]: item for item in previous.get("attachments", [])}
    for item in result["attachments"]:
        old = old_items.get(item["template_id"], {})
        if old.get("source_version_id") == item["source_version_id"]:
            item.update({key: old[key] for key in FILL_FIELDS if key in old})
        if changed_idea:
            item["fill_state"] = "stale"
            for key in item.get("field_generations", {}):
                field_generation(item, key, "stale")
        elif item["enabled"] and not old.get("enabled"):
            reusable = old.get("fill_state") == "ready" and old.get("fill_context") == result.get(
                "prepared_fingerprint"
            )
            if not reusable:
                item["attachment_instance_id"] = new_id()
                item.pop("preparation_id", None)
                item.pop("fill_request_id", None)
                item.pop("field_generations", None)
                item["fill_state"] = "needs_input" if item.get("validation_errors") else "ready"
        elif item["enabled"] and item.get("fill_state") not in ACTIVE_FILL | {"stale"}:
            item["fill_state"] = "needs_input" if item.get("validation_errors") else "ready"
    prepared = result.get("prepared_fingerprint") == idea_fingerprint(result)
    if changed_idea or not prepared:
        result["stage"] = "idea"
    elif requested_stage == "review" and result.get("review_snapshot"):
        result["stage"] = "review"
    else:
        result["stage"] = "idea" if requested_stage == "idea" else "templates"
    if prepared and project.current_revision_id == result.get("context_revision"):
        additions = [
            item
            for item in result["attachments"]
            if item["enabled"]
            and (
                not old_items.get(item["template_id"], {}).get("enabled")
                or old_items.get(item["template_id"], {}).get("source_version_id")
                != item["source_version_id"]
            )
        ]
        schedule_fill(repo, db, actor, project.id, result, additions)
    result["event_sequence"] = version
    return result


def schedule_fill(repo, db, actor, pid, data, items, target_field_id=None):
    needed = []
    for item in items:
        template = db.get(TemplateVersion, item["source_version_id"])
        fields = normalized_fields(template.content, template.slug)
        if not any(
            empty(item["fields"].get(field["key"])) for field in fields if field["sensitivity"] != "secret"
        ):
            item["fill_state"] = "needs_input" if item.get("validation_errors") else "ready"
            item["fill_context"] = data["prepared_fingerprint"]
            continue
        if item.get("fill_state") in ACTIVE_FILL:
            continue
        item.update(
            fill_state="queued",
            fill_message="Aguardando preenchimento automático.",
            fill_error_code=None,
            fill_questions=[
                question
                for question in item.get("fill_questions", [])
                if target_field_id and target_field_id not in question["field_ids"]
            ],
            fill_context=data["prepared_fingerprint"],
            fill_request_id=new_id(),
        )
        needed.append(item)
    if not needed:
        return
    active = (
        db.scalar(
            select(Operation).where(
                Operation.account_id == actor,
                Operation.project_id == pid,
                Operation.type == "template_autofill",
                Operation.state == "queued",
                Operation.payload["request_id"].as_string() == data["request_id"],
                Operation.payload["target"]["field_id"].as_string().is_(None),
            )
        )
        if not target_field_id
        else None
    )
    from viracase.infrastructure.integrations import Integrations

    available = utcnow() + timedelta(
        milliseconds=Integrations(repo).build_policy().get("selection_debounce_ms", 500)
    )
    if active:
        job = db.scalar(select(Job).where(Job.operation_id == active.id))
        if job and job.state == "queued":
            job.available_at = available
            emit("autofill_coalesced", operation=active, phase="selection", selected_count=len(needed))
            return
    active = Operation(
        account_id=actor,
        project_id=pid,
        type="template_autofill",
        payload={
            "request_id": data["request_id"],
            "http_request_id": current_context().get("request_id"),
            "correlation_id": data.get("correlation_id") or data["request_id"],
            **(
                {
                    "target": {
                        "attachment_id": needed[0]["template_id"],
                        "field_id": target_field_id,
                        "field_revision": needed[0]
                        .get("field_values", {})
                        .get(target_field_id, {})
                        .get("field_revision", 1),
                        "source_version_id": needed[0]["source_version_id"],
                        "attachment_instance_id": needed[0]["attachment_instance_id"],
                        "fill_request_id": needed[0]["fill_request_id"],
                        "prepared_fingerprint": data["prepared_fingerprint"],
                        "context_revision": data.get("context_revision"),
                    }
                }
                if target_field_id
                else {}
            ),
        },
    )
    db.add(active)
    db.flush()
    if target_field_id:
        field_generation(needed[0], target_field_id, "queued", active)
    db.add(Job(operation_id=active.id, available_at=available))
    emit(
        "autofill_scheduled",
        operation=active,
        phase="selection",
        selected_count=len(needed),
        **(
            {"attachment_id": needed[0]["template_id"], "field_id": target_field_id}
            if target_field_id
            else {}
        ),
    )


def reset_ai_fields(db, item):
    template = db.get(TemplateVersion, item["source_version_id"])
    definitions = normalized_fields(template.content, template.slug)
    before = copy.deepcopy(item["fields"])
    automatic = item["edited_content"] == render(template.content["content"], definitions, before)
    for key, meta in item.get("field_values", {}).items():
        if meta.get("edited_by_user") or meta.get("source_type") == "user_edit":
            continue
        item["fields"].pop(key, None)
        item["field_values"][key] = {
            **meta,
            "value": None,
            "state": "empty",
            "field_revision": meta.get("field_revision", 1) + 1,
            "updated_at": utcnow().isoformat(),
        }
    if automatic:
        item["edited_content"] = render(template.content["content"], definitions, item["fields"])
    item["validation_errors"] = attachment_errors(item, definitions)
    item.update(fill_state="needs_input", fill_questions=[], fill_error_code=None)
    item.pop("fill_request_id", None)
    item.pop("preparation_id", None)


def apply_automatic(repo, db, operation):
    if not operation.payload.get("automatic_apply"):
        return
    from viracase.infrastructure.guided_build import GuidedBuild
    from viracase.infrastructure.template_preparations import preparation_error_message

    guided = GuidedBuild(repo, None)
    row = guided.drafts.row(db, operation.account_id, "build", operation.project_id)
    if not row:
        return
    data = copy.deepcopy(row.data)
    project = repo.owned_project(db, operation.account_id, operation.project_id)
    current = {item["template_id"]: item for item in data["attachments"]}
    snapshots = {item["attachment_id"]: item for item in operation.payload["items"]}
    compatible_context = project.current_revision_id == operation.payload.get(
        "base_revision_id"
    ) and canonical_hash(
        {key: data.get(key, "") for key in ("objective", "message_text")}
    ) == operation.payload.get("request_context_hash")
    changed = False
    trace_fields = []
    for result in operation.result.get("preparation", {}).get(
        "items", operation.result.get("preparation_partial", operation.payload["public_items"])
    ):
        item = current.get(result["attachment_id"])
        source = snapshots.get(result["attachment_id"])
        if (
            not source
            or not item
            or not item["enabled"]
            or item["attachment_instance_id"] != source["attachment_instance_id"]
        ):
            emit(
                "preparation_apply_skipped",
                operation=operation,
                phase="apply",
                attachment_id=result["attachment_id"],
                reason_code="ATTACHMENT_CHANGED",
                skipped_count=len(result.get("suggested_fields", [])),
            )
            continue
        if item.get("preparation_id") not in {None, operation.id}:
            continue
        if not compatible_context:
            item.update(
                fill_state="stale",
                fill_message="O contexto mudou. Prepare a construção novamente.",
                fill_error_code="PREPARATION_STALE",
            )
            emit(
                "preparation_apply_stale",
                "warning",
                operation=operation,
                phase="apply",
                attachment_id=item["template_id"],
                error_code="PREPARATION_STALE",
            )
            changed = True
            if source.get("explicit_empty_target"):
                field_generation(
                    item, source["explicit_empty_target"]["field_id"], "stale", operation, "PREPARATION_STALE"
                )
            continue
        try:
            template = guided.templates.authorized(db, operation.account_id, item["source_version_id"])
        except RuleError as exc:
            if exc.code != "TEMPLATE_UNAVAILABLE":
                raise
            item.update(
                fill_state="failed",
                fill_message=exc.message,
                fill_error_code=exc.code,
                validation_errors=[
                    {
                        "attachment_id": item["template_id"],
                        "code": exc.code,
                        "message": exc.message,
                        "action": "Remova o anexo ou escolha outra versão",
                    }
                ],
            )
            changed = True
            if source.get("explicit_empty_target"):
                field_generation(
                    item,
                    source["explicit_empty_target"]["field_id"],
                    "failed",
                    operation,
                    exc.code,
                    exc.message,
                )
            emit(
                "preparation_apply_unavailable",
                "warning",
                operation=operation,
                error=exc,
                phase="apply",
                attachment_id=item["template_id"],
            )
            continue
        definitions = normalized_fields(template.content, template.slug)
        automatic = item["edited_content"] == render(template.content["content"], definitions, item["fields"])
        for field in result.get("suggested_fields", []):
            key = field["field_id"]
            meta = item.get("field_values", {}).get(key, {})
            if (
                field.get("state") != "suggested"
                or item["fields"].get(key) != field["original_value"]
                or meta.get("field_revision", 1) != source.get("original_field_revisions", {}).get(key, 1)
                or meta.get("edited_by_user")
                and not explicit_empty_target(source, key, item["fields"].get(key), meta)
            ):
                emit(
                    "preparation_field_preserved",
                    operation=operation,
                    phase="apply",
                    attachment_id=item["template_id"],
                    field_id=key,
                    reason_code="FIELD_CHANGED",
                    skipped_count=1,
                )
                continue
            if (
                not automatic
                and "{{" + key + "}}" not in item["edited_content"]
                and not explicit_empty_target(source, key, item["fields"].get(key), meta)
            ):
                emit(
                    "preparation_field_preserved",
                    operation=operation,
                    phase="apply",
                    attachment_id=item["template_id"],
                    field_id=key,
                    reason_code="CONTENT_CHANGED",
                    skipped_count=1,
                )
                continue
            item["fields"][key] = field["value"]
            item["field_values"][key] = {
                "field_id": key,
                "value": field["value"],
                "state": "applied",
                "source_type": field["source_type"],
                "source_refs": field["source_refs"],
                "suggestion_version": field["suggestion_version"],
                "edited_by_user": False,
                "updated_at": utcnow().isoformat(),
                "preparation_id": operation.id,
                "field_revision": meta.get("field_revision", 1) + 1,
            }
            field["state"] = "applied"
            trace_fields.append(
                {
                    "attachment_id": item["template_id"],
                    "field_id": key,
                    "final_value": field["value"],
                    "source_type": field["source_type"],
                    "state": "applied",
                }
            )
        item["edited_content"] = render(
            template.content["content"] if automatic else item["edited_content"], definitions, item["fields"]
        )
        item["validation_errors"] = attachment_errors(item, definitions)
        item["attachment_revision"] = row.version + 1
        state = operation.state
        errors = [
            error
            for error in result.get("errors", [])
            if not error.get("field_id") or empty(item["fields"].get(error["field_id"]))
        ]
        if state == "succeeded":
            state = "failed" if errors else "needs_input" if item["validation_errors"] else "ready"
        elif state == "cancelled":
            state = "needs_input" if item["validation_errors"] else "ready"
        elif state == "failed" and not item["validation_errors"]:
            state = "ready"
        error_code = operation.error_code or (errors[0]["code"] if errors else None)
        item.update(
            fill_state=state,
            fill_questions=[
                *[
                    question
                    for question in item.get("fill_questions", [])
                    if source.get("explicit_empty_target")
                    and source["explicit_empty_target"]["field_id"] not in question["field_ids"]
                    and any(empty(item["fields"].get(key)) for key in question["field_ids"])
                ],
                *[
                    question
                    for question in result.get("questions", [])
                    if any(empty(item["fields"].get(key)) for key in question["field_ids"])
                ],
            ],
            preparation_id=operation.id,
            filled_at=utcnow().isoformat(),
            fill_error_code=error_code if state in {"failed", "reconciling"} else None,
            fill_message="Conteúdo pronto para revisão."
            if state == "ready"
            else "Complete as informações indicadas."
            if state == "needs_input"
            else preparation_error_message(
                error_code or "PROVIDER_RESULT_UNKNOWN" if state == "reconciling" else error_code
            ),
        )
        if source.get("explicit_empty_target"):
            key = source["explicit_empty_target"]["field_id"]
            metadata = item.get("field_values", {}).get(key, {})
            target_state = (
                "filling"
                if operation.state == "running"
                else operation.state
                if operation.state in {"queued", "reconciling"}
                else "succeeded"
                if metadata.get("preparation_id") == operation.id and not empty(item["fields"].get(key))
                else "cancelled"
                if operation.state == "cancelled"
                else "stale"
                if not explicit_empty_target(source, key, item["fields"].get(key), metadata)
                else "failed"
            )
            field_generation(
                item,
                key,
                target_state,
                operation,
                error_code if target_state in {"failed", "reconciling"} else None,
                preparation_error_message(error_code)
                if error_code and target_state in {"failed", "reconciling"}
                else None,
            )
        emit(
            "preparation_attachment_updated",
            "warning" if errors else "info",
            operation=operation,
            phase="apply",
            attachment_id=item["template_id"],
            state=state,
            error_code=item["fill_error_code"],
            applied_count=sum(field["attachment_id"] == item["template_id"] for field in trace_fields),
            pending_count=len(item["validation_errors"]),
        )
        changed = True
    if changed:
        invalidate_review(data)
        data["preparation_id"] = operation.id
        guided.drafts.write(db, operation.account_id, "build", operation.project_id, row.version, data)
        if trace_fields:
            from viracase.infrastructure.integration_traces import TraceStore, trace_context

            TraceStore(repo).put(
                trace_context(repo, operation, "review"),
                "fields",
                "applied",
                {"fields": {"items": trace_fields}},
                db=db,
            )


def cancel_scheduled(repo, db, operation):
    fail_scheduled(
        repo,
        db,
        operation,
        "PREPARATION_CANCELLED",
        "Preenchimento cancelado. Suas edições foram preservadas.",
    )


def fail_scheduled(repo, db, operation, error_code, message=None):
    from viracase.infrastructure.drafts import Drafts
    from viracase.infrastructure.template_preparations import preparation_error_message

    drafts = Drafts(repo, None)
    row = drafts.row(db, operation.account_id, "build", operation.project_id)
    if not row or row.data.get("request_id") != operation.payload.get("request_id"):
        return
    data = copy.deepcopy(row.data)
    changed = False
    for item in data["attachments"]:
        target = operation.payload.get("target")
        if target and item["template_id"] != target["attachment_id"]:
            continue
        if item["enabled"] and item.get("fill_state") == "queued" and not item.get("preparation_id"):
            item.update(
                fill_state="failed",
                fill_error_code=error_code,
                fill_message=message or preparation_error_message(error_code),
            )
            changed = True
            if target:
                field_generation(
                    item,
                    target["field_id"],
                    "cancelled"
                    if error_code == "PREPARATION_CANCELLED"
                    else "stale"
                    if error_code in {"FIELD_CHANGED", "PREPARATION_STALE"}
                    else "failed",
                    operation,
                    error_code,
                    message or preparation_error_message(error_code),
                )
    if changed:
        invalidate_review(data)
        drafts.write(db, operation.account_id, "build", operation.project_id, row.version, data)
        emit("autofill_failed", "warning", operation=operation, phase="scheduling", error_code=error_code)


def execute_autofill(worker, identity, token):
    from viracase.infrastructure.guided_build import GuidedBuild
    from viracase.infrastructure.template_preparations import TemplatePreparations

    repo = worker.repo
    guided = GuidedBuild(repo, None)
    with repo.sessions.begin() as db:
        operation = db.get(Operation, identity)
        repo.account(db, operation.account_id, True)
        job = db.scalar(select(Job).where(Job.operation_id == identity).with_for_update())
        if job.lease_token != token:
            return
        emit("autofill_started", operation=operation, phase="scheduling")
        if operation.cancel_requested:
            operation.state, job.state = "cancelled", "done"
            cancel_scheduled(repo, db, operation)
            return
        operation.state = "running"
        row = guided.drafts.row(db, operation.account_id, "build", operation.project_id)
        if not row or row.data.get("request_id") != operation.payload["request_id"]:
            operation.state, job.state = "cancelled", "done"
            return
        data = copy.deepcopy(row.data)
        target = operation.payload.get("target")
        if target:
            project = repo.owned_project(db, operation.account_id, operation.project_id)
            same_context = (
                data.get("prepared_fingerprint") == target["prepared_fingerprint"]
                and data.get("prepared_fingerprint") == idea_fingerprint(data)
                and data.get("context_revision") == target["context_revision"]
                and project.current_revision_id == target["context_revision"]
            )
            item = next(
                (item for item in data["attachments"] if item["template_id"] == target["attachment_id"]),
                None,
            )
            unchanged = bool(
                item
                and item["enabled"]
                and item["source_version_id"] == target["source_version_id"]
                and item["attachment_instance_id"] == target["attachment_instance_id"]
                and item.get("fill_request_id") == target["fill_request_id"]
                and item.get("field_values", {}).get(target["field_id"], {}).get("field_revision", 1)
                == target["field_revision"]
                and empty(item["fields"].get(target["field_id"]))
                and same_context
            )
            if not unchanged:
                error_code = "FIELD_CHANGED" if same_context else "PREPARATION_STALE"
                if item and item.get("fill_request_id") == target["fill_request_id"]:
                    field_generation(item, target["field_id"], "stale", operation, error_code)
                    item["fill_state"] = (
                        "stale"
                        if not same_context
                        else "needs_input"
                        if item.get("validation_errors")
                        else "ready"
                    )
                    invalidate_review(data)
                    guided.drafts.write(
                        db, operation.account_id, "build", operation.project_id, row.version, data
                    )
                operation.state, operation.error_code, job.state = "cancelled", error_code, "done"
                emit(
                    "autofill_target_stale",
                    "warning",
                    operation=operation,
                    phase="scheduling",
                    error_code=error_code,
                    attachment_id=target["attachment_id"],
                    field_id=target["field_id"],
                )
                return
        items = [
            item
            for item in data["attachments"]
            if item["enabled"]
            and item.get("fill_state") == "queued"
            and not item.get("preparation_id")
            and (
                item["template_id"] == target["attachment_id"]
                if target
                else not any(
                    generation.get("state") in ACTIVE_FILL
                    for generation in item.get("field_generations", {}).values()
                )
            )
        ]
        if not items or data.get("prepared_fingerprint") != idea_fingerprint(data):
            operation.state, job.state = "cancelled", "done"
            return
        if db.scalar(
            select(Operation.id).where(
                Operation.project_id == operation.project_id,
                Operation.type == "template_preparation",
                Operation.state.in_(["queued", "running", "reconciling"]),
            )
        ):
            operation.state, job.state = "queued", "queued"
            job.available_at = utcnow() + timedelta(seconds=2)
            job.lease_token = None
            return
        if operation.payload.get("dispatch_request"):
            request = operation.payload["dispatch_request"]
        else:
            request = {
                "expected_draft_revision": row.version,
                "base_project_revision": data["context_revision"],
                "attachment_ids": [item["template_id"] for item in items],
                "expected_attachment_revisions": {
                    item["template_id"]: item["attachment_revision"] for item in items
                },
                **(
                    {"target_field_id": target["field_id"], "target_field_revision": target["field_revision"]}
                    if target
                    else {}
                ),
            }
            operation.payload = {**operation.payload, "dispatch_request": request}
        actor, pid = operation.account_id, operation.project_id
    try:
        prepared = TemplatePreparations(repo, None, worker.catalog).start(
            actor, pid, "autofill:" + identity, request, automatic=True, schedule_id=identity
        )
        failure = None
    except RuleError as exc:
        prepared, failure = None, exc
        emit("autofill_preparation_failed", "warning", operation=operation, error=exc, phase="configuration")
    with repo.sessions.begin() as db:
        repo.account(db, actor, True)
        operation = db.get(Operation, identity)
        job = db.scalar(select(Job).where(Job.operation_id == identity).with_for_update())
        if job.lease_token != token:
            return
        if operation.cancel_requested:
            if prepared:
                child = db.get(Operation, prepared["id"])
                child.cancel_requested = True
                if child.state == "queued":
                    repo.settle_operation(db, child, 0)
                    child.state = "cancelled"
                    db.scalar(select(Job).where(Job.operation_id == child.id)).state = "done"
                    apply_automatic(repo, db, child)
            operation.state, job.state = "cancelled", "done"
            cancel_scheduled(repo, db, operation)
            return
        if failure and failure.code in {"DRAFT_CONFLICT", "ATTACHMENT_CONFLICT", "OPERATION_CONFLICT"}:
            operation.payload = {
                key: value for key, value in operation.payload.items() if key != "dispatch_request"
            }
            operation.state, job.state = "queued", "queued"
            job.available_at = utcnow() + timedelta(milliseconds=500)
            job.lease_token = None
            return
        row = guided.drafts.row(db, actor, "build", pid)
        if row:
            data = copy.deepcopy(row.data)
            changed = False
            scheduled = {item["template_id"]: item for item in items}
            for item in data["attachments"]:
                source = scheduled.get(item["template_id"])
                if (
                    not source
                    or item["template_id"] not in request["attachment_ids"]
                    or not item["enabled"]
                    or item.get("fill_state") != "queued"
                    or item["attachment_instance_id"] != source["attachment_instance_id"]
                    or item["source_version_id"] != source["source_version_id"]
                    or item.get("fill_request_id") != source.get("fill_request_id")
                ):
                    continue
                if failure:
                    item.update(
                        fill_state="failed", fill_message=failure.message, fill_error_code=failure.code
                    )
                    if target:
                        field_generation(
                            item, target["field_id"], "failed", operation, failure.code, failure.message
                        )
                else:
                    item.update(
                        preparation_id=prepared["id"], fill_message="Aguardando preenchimento com IA."
                    )
                    if target:
                        child = db.get(Operation, prepared["id"])
                        field_generation(item, target["field_id"], "queued", child)
                changed = True
            if changed:
                guided.drafts.write(db, actor, "build", pid, row.version, data)
        operation.state, job.state = ("failed" if failure else "succeeded"), "done"
        operation.error_code = failure.code if failure else None
        operation.result = {"preparation_id": prepared["id"]} if prepared else {}
        emit(
            "autofill_completed",
            "warning" if failure else "info",
            operation=operation,
            phase="scheduling",
            state=operation.state,
            error_code=operation.error_code,
        )
