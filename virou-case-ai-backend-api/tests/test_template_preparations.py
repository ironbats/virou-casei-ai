import copy
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import func, select

from viracase.application.ports import ModelResult
from viracase.application.template_preparation_selection import build_selection
from viracase.domain.rules import RuleError, cost
from viracase.domain.template_fields import normalized_fields, render, value_error
from viracase.infrastructure.feature_policies import FeaturePolicies
from viracase.infrastructure.guided_build import GuidedBuild
from viracase.infrastructure.learning import LearningLibrary
from viracase.infrastructure.models import (
    Ledger,
    Operation,
    Reservation,
    Revision,
)
from viracase.infrastructure.providers import ProviderUncertain
from viracase.infrastructure.template_preparations import TemplatePreparations
from viracase.infrastructure.worker import Worker


@pytest.fixture
def preparation(system):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor, "project", {"name": "Preparação", "objective": "Organizar tarefas de uma equipe pequena"}
    )
    learning = LearningLibrary(system["config"].content_root)
    guided = GuidedBuild(repo, learning)
    template = next(item for item in guided.templates.list(actor)["items"] if item["id"] == "sdd")
    data = guided.get(actor, project["id"])["data"]
    data["message_text"] = "Organizar tarefas com critérios claros"
    data["attachments"] = [
        {
            "template_id": template["id"],
            "source_version_id": template["version_id"],
            "edited_content": template["content"],
            "fields": {},
            "enabled": True,
        }
    ]
    guided.save(actor, project["id"], "draft", 0, data)
    service = TemplatePreparations(repo, learning, system["catalog"])
    return service, guided, actor, project, template


def request_for(preparation):
    service, guided, actor, project, _ = preparation
    draft = guided.get(actor, project["id"])
    selected = [item for item in draft["data"]["attachments"] if item["enabled"]]
    return {
        "expected_draft_revision": draft["version"],
        "base_project_revision": project["current_revision_id"],
        "attachment_ids": [item["template_id"] for item in selected],
        "expected_attachment_revisions": {
            item["template_id"]: item["attachment_revision"] for item in selected
        },
    }


def snapshot_contract(repo, identity, version):
    with repo.sessions.begin() as db:
        operation = db.get(Operation, identity)
        payload = {
            key: value
            for key, value in operation.payload.items()
            if key
            not in {
                "template_preparation_contract",
                "proposal_schema",
                "selection_catalog",
                "selection_schema",
            }
        }
        policy = {
            key: value
            for key, value in operation.policy.items()
            if key
            not in {
                "template_preparation_contract",
                "preparation_proposal_schema",
                "preparation_selection_schema",
            }
        }
        if version != 1:
            payload["template_preparation_contract"] = version
        if version == 3:
            selection = build_selection(payload)
            payload.update(selection_catalog=selection["catalog"], selection_schema=selection["schema"])
            policy["preparation_selection_schema"] = selection["schema"]
        operation.payload = payload
        operation.policy = {**policy, "template_preparation_contract": version}


class PreparationGateway:
    def __init__(self, callback=None, question=False, malformed=False, uncertain=False):
        self.calls, self.callback = [], callback
        self.question, self.malformed, self.uncertain = question, malformed, uncertain

    def generate(self, policy, prompt):
        self.calls.append((policy, prompt))
        if self.callback:
            self.callback()
        if self.uncertain:
            raise ProviderUncertain("synthetic timeout")
        request = json.loads(prompt)
        if policy.get("template_preparation_contract") == 4:
            context = next(
                (entry for entry in request["context"] if entry["type"] == "user_answer"),
                request["context"][0],
            )
            items = {
                item["attachment_id"]: {
                    field["key"]: {
                        "proposal": False
                        if field.get("type") == "boolean"
                        else field["constraints"]["enum"][0]
                        if field.get("type") == "enum"
                        else [context["content"]]
                        if field.get("type") == "string_list"
                        else context["content"]
                    }
                    for field in item["fields"]
                }
                for item in request["templates"]
            }
            value = "not-json" if self.malformed else json.dumps({"items": items})
            return ModelResult(value, {"input": 200, "cached": 0, "output": 80}, "synthetic-response")
        if policy.get("template_preparation_contract", 1) == 3:
            units = request["selection_catalog"]
            unit = next((item for item in units if item["source_type"] == "user_answer"), units[0])
            items = {}
            for item in request["templates"]:
                fields = {}
                for field in item["fields"]:
                    if self.question:
                        fields[field["key"]] = {"missing": "Qual é o resultado esperado?"}
                    elif field["key"] in item.get("protected_field_ids", []) or item["current_values"].get(
                        field["key"]
                    ):
                        fields[field["key"]] = {"missing": "Campo já preenchido."}
                    elif field["allowed_source_types"] == ["policy_suggestion"] or field["semantic_id"] in {
                        "default_denial",
                        "cross_account_test",
                        "expired_session_result",
                        "missing_resource_result",
                        "audit_event",
                    }:
                        fields[field["key"]] = {
                            "proposal": "Critério proposto: validar a regra antes de concluir a construção."
                        }
                    else:
                        fields[field["key"]] = {
                            "extracts": [{"unit": unit["id"], "start": 0, "end": len(unit["tokens"]) - 1}]
                        }
                items[item["attachment_id"]] = fields
            value = "not-json" if self.malformed else json.dumps({"items": items})
            return ModelResult(value, {"input": 200, "cached": 0, "output": 80}, "synthetic-response")
        context = (
            next(item for item in request["context"] if item["type"] == "user_answer")
            if any(item["type"] == "user_answer" for item in request["context"])
            else request["context"][0]
        )
        items = []
        for item in request["templates"]:
            fields = [
                {
                    "field_id": field["key"],
                    **(
                        {
                            "value": context["content"],
                            "citations": [{"source_ref": context["id"], "quote": context["content"]}],
                        }
                        if policy.get("template_preparation_contract", 1) == 2
                        else {
                            "value_json": json.dumps(context["content"]),
                            "source_type": context["type"],
                            "source_refs": [context["id"]],
                            "evidence": context["content"],
                        }
                    ),
                }
                for field in item["fields"]
                if not item["current_values"].get(field["key"])
            ]
            items.append(
                {
                    "attachment_id": item["attachment_id"],
                    "fields": [] if self.question else fields,
                    "questions": [
                        {
                            "question": "Qual é o resultado esperado?",
                            "field_ids": [field["key"] for field in item["fields"]],
                        }
                    ]
                    if self.question
                    else [],
                }
            )
        value = "not-json" if self.malformed else json.dumps({"items": items})
        return ModelResult(value, {"input": 200, "cached": 0, "output": 80}, "synthetic-response")


def run(system, preparation, gateway=None, contract_version=None):
    service, _, actor, project, _ = preparation
    value = service.start(actor, project["id"], "prepare", request_for(preparation))
    if contract_version:
        snapshot_contract(system["repo"], value["id"], contract_version)
    gateway = gateway or PreparationGateway()
    assert Worker(system["repo"], gateway, system["catalog"]).tick(value["id"])
    return service.get(actor, project["id"], value["id"]), gateway


def apply_request(preparation, value):
    _, guided, actor, project, _ = preparation
    draft = guided.get(actor, project["id"])
    return {
        "expected_state_version": value["state_version"],
        "expected_draft_revision": draft["version"],
        "expected_attachment_revisions": {
            item["template_id"]: item["attachment_revision"] for item in draft["data"]["attachments"]
        },
        "accepted_fields": [
            {"attachment_id": item["attachment_id"], "field_id": field["field_id"]}
            for item in value["items"]
            for field in item["suggested_fields"]
        ],
    }


def test_preparation_reserves_once_never_generates_code_and_requires_explicit_apply(system, preparation):
    service, guided, actor, project, template = preparation
    before = system["repo"].wallet(actor, True)
    request = request_for(preparation)
    value = service.start(actor, project["id"], "prepare", request)
    assert service.start(actor, project["id"], "prepare", request)["id"] == value["id"]
    gateway = PreparationGateway()
    Worker(system["repo"], gateway, system["catalog"]).tick(value["id"])
    value = service.get(actor, project["id"], value["id"])
    assert value["state"] == "ready", value
    assert value["can_apply"] and len(gateway.calls) == 1
    assert "generation_contract_version" not in gateway.calls[0][0]
    assert guided.get(actor, project["id"])["data"]["attachments"][0]["fields"] == {}
    assert (
        system["repo"].project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    )
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(Revision)) == 1
        assert db.scalar(select(func.count()).select_from(Operation)) == 1
        assert db.scalar(select(func.count()).select_from(Reservation)) == 1
    request = apply_request(preparation, value)
    result = service.apply(actor, project["id"], value["id"], "apply", request)
    assert len(result["applied_fields"]) == len(template["fields"])
    assert not result["conflicts"] and not result["preparation"]["can_apply"]
    attachment = result["draft"]["data"]["attachments"][0]
    assert "{{" not in attachment["edited_content"]
    assert all(field["state"] == "applied" for field in attachment["field_values"].values())
    assert service.apply(actor, project["id"], value["id"], "apply", request) == result
    wallet = system["repo"].wallet(actor, True)
    assert wallet["reserved_brl"] == "0E-8"
    assert Decimal(wallet["consumed_brl"]) > Decimal(before["consumed_brl"])


def test_late_response_preserves_manual_fields_and_applies_other_compatible_fields(system, preparation):
    service, guided, actor, project, template = preparation
    field_id = template["fields"][0]["key"]

    def edit():
        draft = guided.get(actor, project["id"])
        data = copy.deepcopy(draft["data"])
        data["attachments"][0]["fields"][field_id] = "Minha decisão manual"
        data["attachments"][0]["edited_content"] = render(
            template["content"], template["fields"], data["attachments"][0]["fields"]
        )
        guided.save(actor, project["id"], "manual", draft["version"], data)

    value, _ = run(system, preparation, PreparationGateway(callback=edit))
    assert value["state"] == "ready"
    changed = next(field for field in value["items"][0]["suggested_fields"] if field["field_id"] == field_id)
    assert not changed["compatible"]
    result = service.apply(actor, project["id"], value["id"], "apply", apply_request(preparation, value))
    assert result["draft"]["data"]["attachments"][0]["fields"][field_id] == "Minha decisão manual"
    assert result["conflicts"][0]["field_id"] == field_id
    assert len(result["applied_fields"]) == len(template["fields"]) - 1


def test_removed_and_reattached_template_rejects_old_suggestions(system, preparation):
    service, guided, actor, project, _ = preparation
    value, _ = run(system, preparation)
    draft = guided.get(actor, project["id"])
    old = copy.deepcopy(draft["data"])
    removed = copy.deepcopy(old)
    removed["attachments"] = []
    guided.save(actor, project["id"], "remove", draft["version"], removed)
    guided.save(actor, project["id"], "reattach", draft["version"] + 1, old)
    current = service.get(actor, project["id"], value["id"])
    assert not current["can_apply"]
    assert all(not field["compatible"] for field in current["items"][0]["suggested_fields"])
    result = service.apply(actor, project["id"], value["id"], "old", apply_request(preparation, current))
    assert not result["applied_fields"] and result["conflicts"]


def test_context_changed_before_dispatch_does_not_call_or_charge(system, preparation):
    service, guided, actor, project, _ = preparation
    value = service.start(actor, project["id"], "prepare", request_for(preparation))
    draft = guided.get(actor, project["id"])
    draft["data"]["message_text"] = "Outra solicitação com contexto diferente"
    guided.save(actor, project["id"], "changed", draft["version"], draft["data"])
    gateway = PreparationGateway()
    Worker(system["repo"], gateway, system["catalog"]).tick(value["id"])
    assert not gateway.calls
    assert service.get(actor, project["id"], value["id"])["error_code"] == "PREPARATION_STALE"
    assert Decimal(system["repo"].wallet(actor, True)["consumed_brl"]) == 0


@pytest.mark.parametrize("during", [False, True])
def test_cancel_releases_or_settles_real_usage_without_applying(system, preparation, during):
    service, guided, actor, project, _ = preparation
    value = service.start(actor, project["id"], "prepare", request_for(preparation))
    callback = lambda: service.cancel(actor, project["id"], value["id"], "cancel")
    if not during:
        callback()
    gateway = PreparationGateway(callback=callback if during else None)
    Worker(system["repo"], gateway, system["catalog"]).tick(value["id"])
    assert len(gateway.calls) == int(during)
    assert service.get(actor, project["id"], value["id"])["state"] == "cancelled"
    assert not guided.get(actor, project["id"])["data"]["attachments"][0]["fields"]
    assert (Decimal(system["repo"].wallet(actor, True)["consumed_brl"]) > 0) is during
    assert Decimal(system["repo"].wallet(actor, True)["reserved_brl"]) == 0


def test_timeout_never_redispatches_and_keeps_reservation_until_reconciliation(system, preparation):
    service, _, actor, project, _ = preparation
    value, gateway = run(system, preparation, PreparationGateway(uncertain=True))
    assert value["state"] == "reconciling"
    worker = Worker(system["repo"], gateway, system["catalog"])
    assert not worker.tick(value["id"])
    assert len(gateway.calls) == 1
    assert Decimal(system["repo"].wallet(actor, True)["reserved_brl"]) > 0
    with pytest.raises(RuleError) as caught:
        service.start(actor, project["id"], "duplicate", request_for(preparation))
    assert caught.value.code == "OPERATION_CONFLICT"


def test_questions_are_durable_and_answers_create_one_bounded_followup(system, preparation):
    service, guided, actor, project, _ = preparation
    value, _ = run(system, preparation, PreparationGateway(question=True), contract_version=3)
    assert value["state"] == "needs_input"
    request = {
        "expected_state_version": value["state_version"],
        "expected_draft_revision": guided.get(actor, project["id"])["version"],
        "answers": [
            {
                "question_id": value["items"][0]["questions"][0]["id"],
                "value": "Equipe autorizada com tarefas claras",
            }
        ],
    }
    followup = service.answer(actor, project["id"], value["id"], "answer", request)
    assert service.answer(actor, project["id"], value["id"], "answer", request)["id"] == followup["id"]
    assert followup["id"] != value["id"] and followup["correlation_id"] == value["correlation_id"]
    Worker(system["repo"], PreparationGateway(), system["catalog"]).tick(followup["id"])
    result = service.get(actor, project["id"], followup["id"])
    assert result["state"] == "ready"
    assert all(
        field["source_type"] == "policy_suggestion" for field in result["items"][0]["suggested_fields"]
    )
    assert all(
        field["value"] == "Equipe autorizada com tarefas claras"
        for field in result["items"][0]["suggested_fields"]
    )


def test_invalid_response_charges_actual_usage_and_bounded_repair(system, preparation):
    service, _, actor, project, _ = preparation
    policies = FeaturePolicies(system["repo"], system["catalog"])
    settings = policies.current("template_preparation")["settings"]
    policies.save(
        system["admin"],
        "template_preparation",
        "repairs",
        0,
        {"settings": {**settings, "max_repairs": 1}, "reason": "Permitir uma reparação limitada"},
    )
    value, gateway = run(system, preparation, PreparationGateway(malformed=True))
    assert value["state"] == "failed" and value["error_code"] == "PREPARATION_INVALID_RESPONSE"
    assert len(gateway.calls) == 2
    assert Decimal(system["repo"].wallet(actor, True)["consumed_brl"]) == cost(
        gateway.calls[0][0], {"input": 400, "cached": 0, "output": 160}
    )
    assert not service.get(actor, project["id"], value["id"])["can_apply"]


def test_schema_legacy_ids_distinguish_results_and_validate_types():
    content = {
        "content": "Sessão expirada: {{field_8}}\nRecurso inexistente: {{field_9}}",
        "fields": [
            {"key": "field_8", "label": "Resultado", "required": True, "max_length": 1000},
            {"key": "field_9", "label": "Resultado", "required": True, "max_length": 1000},
        ],
    }
    fields = normalized_fields(content, "autorizacao")
    assert [field["id"] for field in fields] == ["field_8", "field_9"]
    assert fields[0]["label"] != fields[1]["label"]
    assert fields[0]["semantic_id"] == "expired_session_result"
    boolean = {**fields[0], "type": "boolean"}
    assert value_error(boolean, False, True) is None
    assert value_error(boolean, "false", True) == "FIELD_TYPE"
    assert value_error(boolean, None, True) == "FIELD_REQUIRED"


def test_cross_account_unknown_fields_and_revision_conflicts_are_closed(system, preparation):
    service, _, actor, project, _ = preparation
    value, _ = run(system, preparation)
    with pytest.raises(RuleError) as caught:
        service.get(system["users"][1], project["id"], value["id"])
    assert caught.value.code == "NOT_FOUND"
    request = apply_request(preparation, value)
    with pytest.raises(RuleError) as caught:
        service.apply(
            actor,
            project["id"],
            value["id"],
            "unknown",
            {**request, "accepted_fields": [{"attachment_id": "sdd", "field_id": "invented"}]},
        )
    assert caught.value.code == "PREPARATION_UNKNOWN_FIELD"
    with pytest.raises(RuleError) as caught:
        service.apply(actor, project["id"], value["id"], "stale", {**request, "expected_draft_revision": 99})
    assert caught.value.status == 412


def test_preparation_command_concurrency_reserves_once(system, preparation):
    if system["engine"].dialect.name != "postgresql":
        pytest.skip("Concorrência entre conexões exige PostgreSQL real.")
    service, _, actor, project, _ = preparation
    request, barrier = request_for(preparation), Barrier(2)

    def start():
        barrier.wait(timeout=10)
        return service.start(actor, project["id"], "same-intention", request)["id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(), range(2)))
    assert len(set(results)) == 1
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reservation)) == 1
        assert db.scalar(select(func.count()).select_from(Ledger).where(Ledger.kind == "reserve")) == 1


def test_preparation_network_boundary_has_no_open_database_transaction(system, preparation):
    connections = []
    gateway = PreparationGateway(callback=lambda: connections.append(system["engine"].pool.checkedout()))
    value, _ = run(system, preparation, gateway)
    assert value["state"] == "ready"
    assert connections == [0]


def test_context_schema_text_is_sanitized_and_partial_context_is_disclosed(system, preparation):
    from viracase.infrastructure.models import TemplateVersion

    service, _, actor, project, template = preparation
    secret = system["config"].openai_api_key
    with system["repo"].sessions.begin() as db:
        row = db.get(TemplateVersion, template["version_id"])
        content = copy.deepcopy(row.content)
        content["title"] = "Template " + secret
        content["fields"][0]["description"] = "Credencial " + secret
        row.content = content
        db.add(
            Operation(
                account_id=actor,
                project_id=project["id"],
                type="generation",
                state="succeeded",
                payload={
                    "brief": "Decisão anterior da mesma conta",
                    "base_revision_id": project["current_revision_id"],
                },
                result={"message": "Proposta anterior ainda não aplicada"},
            )
        )
    policies = FeaturePolicies(system["repo"], system["catalog"])
    settings = policies.current("template_preparation")["settings"]
    policies.save(
        system["admin"],
        "template_preparation",
        "context-limit",
        0,
        {
            "settings": {**settings, "max_context_bytes": 7000},
            "reason": "Contexto limitado para validar transparência",
        },
    )
    value, gateway = run(system, preparation)
    assert value["state"] == "ready", value
    assert secret not in gateway.calls[0][1]
    assert "[SEGREDO REMOVIDO]" in gateway.calls[0][1]
    prompt = json.loads(gateway.calls[0][1])
    source_text = " ".join(entry["content"] for entry in prompt["context"])
    assert "Decisão anterior da mesma conta" in source_text
    assert value["context_summary"]["included_conversation_messages"] == 2
    assert value["context_summary"]["included_files"] + value["context_summary"]["omitted_files"] > 0
    with system["repo"].sessions() as db:
        assert secret in db.get(TemplateVersion, template["version_id"]).content["title"]


def test_unknown_markers_block_after_preparation_without_starting_construction(system, preparation):
    from viracase.application.generation import GenerationService

    service, guided, actor, project, _ = preparation
    value, _ = run(system, preparation)
    applied = service.apply(actor, project["id"], value["id"], "apply", apply_request(preparation, value))
    draft = applied["draft"]
    draft["data"]["attachments"][0]["edited_content"] += "\nExpressão literal {{not_a_template_field}}"
    saved = guided.save(actor, project["id"], "literal", draft["version"], draft["data"])
    before = system["repo"].wallet(actor, True)
    with pytest.raises(RuleError) as caught:
        guided.send(
            actor,
            project["id"],
            "construct",
            {
                "expected_draft_revision": saved["version"],
                "base_project_revision": project["current_revision_id"],
                "policy_version": "2026-10-v1",
                "reply_to": None,
                "review_snapshot_hash": "0" * 64,
            },
            GenerationService(system["repo"], system["catalog"]),
        )
    assert caught.value.code == "ATTACHMENT_INCOMPLETE"
    assert system["repo"].wallet(actor, True) == before


def test_required_fields_return_precise_server_errors_without_reserving(system, preparation):
    from viracase.application.generation import GenerationService

    _, guided, actor, project, template = preparation
    before = system["repo"].wallet(actor, True)
    with pytest.raises(RuleError) as caught:
        guided.send(
            actor,
            project["id"],
            "incomplete",
            {
                "expected_draft_revision": 1,
                "base_project_revision": project["current_revision_id"],
                "policy_version": "2026-10-v1",
                "reply_to": None,
            },
            GenerationService(system["repo"], system["catalog"]),
        )
    assert caught.value.code == "ATTACHMENT_INCOMPLETE"
    assert {field["field_id"] for field in caught.value.details["fields"]} == {
        field["key"] for field in template["fields"]
    }
    assert all(field["attachment_id"] == "sdd" for field in caught.value.details["fields"])
    assert system["repo"].wallet(actor, True) == before


def test_public_preparation_http_contract_auth_csrf_and_review_apply(system, preparation):
    from fastapi.testclient import TestClient

    from viracase.interfaces.api import create_app

    service, _, actor, project, _ = preparation
    client = TestClient(
        create_app(False, (system["config"], system["engine"], system["repo"], system["catalog"], None))
    )
    path = f"/api/v1/projects/{project['id']}/template-preparations"
    assert client.get(path).status_code == 401
    login = client.post(
        "/api/v1/auth/login",
        json={"login": "student0", "password": "test-student-password"},
        headers={"Origin": system["config"].public_origin},
    )
    headers = {
        "Origin": system["config"].public_origin,
        "X-CSRF-Token": login.json()["csrf_token"],
        "Idempotency-Key": "prepare-http",
    }
    request = request_for(preparation)
    assert (
        client.post(
            path, json=request, headers={"Origin": headers["Origin"], "Idempotency-Key": "denied"}
        ).status_code
        == 403
    )
    response = client.post(path, json=request, headers=headers)
    assert response.status_code == 202, response.text
    value = response.json()
    assert client.get("/api/v1/capabilities").json()["template_preparation"]["enabled"]
    assert client.get(path).json()["items"][0]["id"] == value["id"]
    assert client.post(path, json=request, headers=headers).json()["id"] == value["id"]
    Worker(system["repo"], PreparationGateway(), system["catalog"]).tick(value["id"])
    value = service.get(actor, project["id"], value["id"])
    applied = client.post(
        path + "/" + value["id"] + "/apply",
        json=apply_request(preparation, value),
        headers={**headers, "Idempotency-Key": "apply-http"},
    )
    assert applied.status_code == 200 and applied.json()["applied_fields"]
    assert all(
        field["state"] == "applied" for field in applied.json()["preparation"]["items"][0]["suggested_fields"]
    )


@pytest.mark.parametrize(
    "invalid", [{"type": {}}, {"sensitivity": []}, {"allowed_source_types": [{}]}, {"label": " \t\n "}]
)
def test_template_schema_invalid_nested_types_are_validation_errors(invalid, system):
    from fastapi.testclient import TestClient
    from pydantic import ValidationError

    from viracase.interfaces.api import create_app
    from viracase.interfaces.feature_schemas import TemplateInput

    data = {
        "slug": "strict",
        "expected_version": 0,
        "title": "Template estrito",
        "category": "planning",
        "summary": "Template para validação de tipos",
        "content": "Campo {{goal}}",
        "fields": [{"key": "goal", "label": "Objetivo", "required": True, "max_length": 1000, **invalid}],
        "reason": "Validação de tipos desconhecidos",
    }
    with pytest.raises(ValidationError):
        TemplateInput.model_validate(data)
    client = TestClient(
        create_app(True, (system["config"], system["engine"], system["repo"], system["catalog"], None))
    )
    login = client.post(
        "/admin-api/v1/auth/login",
        json={"login": "operator", "password": "test-operator-password"},
        headers={"Origin": system["config"].admin_origin},
    )
    response = client.post(
        "/admin-api/v1/templates",
        json=data,
        headers={
            "Origin": system["config"].admin_origin,
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": "invalid-schema",
        },
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("with_attachment", [False, True])
def test_build_draft_http_roundtrip_preserves_server_metadata(system, preparation, with_attachment):
    from fastapi.testclient import TestClient

    from viracase.interfaces.api import create_app

    service, guided, actor, project, _ = preparation
    value, _ = run(system, preparation)
    applied = service.apply(actor, project["id"], value["id"], "apply", apply_request(preparation, value))
    assert "correlation_id" not in applied["draft"]["data"]
    assert "preparation_id" not in applied["draft"]["data"]
    if not with_attachment:
        guided.save(
            actor,
            project["id"],
            "remove",
            applied["draft"]["version"],
            {**applied["draft"]["data"], "attachments": []},
        )
    client = TestClient(
        create_app(
            False,
            (
                system["config"],
                system["engine"],
                system["repo"],
                system["catalog"],
                None,
            ),
        )
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"login": "student0", "password": "test-student-password"},
        headers={"Origin": system["config"].public_origin},
    )
    headers = {"Origin": system["config"].public_origin, "X-CSRF-Token": login.json()["csrf_token"]}
    path = f"/api/v1/projects/{project['id']}/build-draft"
    draft = client.get(path).json()
    assert "correlation_id" not in draft["data"] and "preparation_id" not in draft["data"]
    data = {**draft["data"], "message_text": "Próximo pedido preservado"}
    response = client.put(
        path,
        json=data,
        headers={
            **headers,
            "If-Match": str(draft["version"]),
            "Idempotency-Key": "roundtrip",
        },
    )
    assert response.status_code == 200, response.text
    current = client.get(path).json()
    assert current["data"]["message_text"] == data["message_text"]
    if with_attachment:
        assert current["data"]["attachments"] == draft["data"]["attachments"]
        forged = copy.deepcopy(current["data"])
        forged["attachments"][0].update(
            title="Outro título",
            attachment_revision=99999,
            attachment_instance_id="forged",
            schema_version=99999,
            field_values={"forged": {"source_type": "project_context"}},
            validation_errors=[],
        )
        response = client.put(
            path,
            json=forged,
            headers={
                **headers,
                "If-Match": str(current["version"]),
                "Idempotency-Key": "readonly",
            },
        )
        assert response.status_code == 200, response.text
        current = client.get(path).json()
        assert current["data"]["attachments"] == draft["data"]["attachments"]
        forged["attachments"][0]["unknown_metadata"] = "rejected"
        response = client.put(
            path,
            json=forged,
            headers={
                **headers,
                "If-Match": str(current["version"]),
                "Idempotency-Key": "unknown-attachment",
            },
        )
        assert response.status_code == 400 and response.json()["error"]["code"] == "VALIDATION_ERROR"
    response = client.put(
        path,
        json={**current["data"], "correlation_id": "forged"},
        headers={
            **headers,
            "If-Match": str(current["version"]),
            "Idempotency-Key": "unknown-draft",
        },
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == "VALIDATION_ERROR"
    with system["repo"].sessions() as db:
        persisted = guided.drafts.row(db, actor, "build", project["id"])
        assert persisted.data["correlation_id"] == value["correlation_id"]
        assert persisted.data["preparation_id"] == value["id"]
