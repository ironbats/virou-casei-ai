import json

import httpx
import pytest
from fastapi.testclient import TestClient
from realistic_preparation_gateway import prompt_sources, select_phrase
from sqlalchemy import func, select
from test_ai_configuration import (
    KEY,
    connection_data,
    create_connection,
    create_model,
    google_key,
    model_data,
    probe,
)
from test_build_workflow import dispatch, review, save, select_template, workflow
from test_conversation_runtime import login

from viracase.application.template_preparation_selection import build_selection
from viracase.domain.rules import cost
from viracase.infrastructure.ai_registry import AIRegistry
from viracase.infrastructure.credentials import initialize_key
from viracase.infrastructure.models import Ledger, Operation, Reservation, Revision, new_id
from viracase.infrastructure.providers import HttpModelGateway, ModelCatalog
from viracase.infrastructure.recommendations import Recommendations
from viracase.infrastructure.template_preparations import TemplatePreparations
from viracase.infrastructure.worker import Worker
from viracase.interfaces.api import create_app

__all__ = ["legacy_selection", "workflow"]

BAKERY_REQUEST = (
    "Precisamos agora melhorar a home page, e criar um carrousel de produtos da minha "
    "padaria e tambem o preço de cada produto, vamos aprimorar tambem a UX e Ter "
    "opcao de colocar os produtos no carrinho mas para efetivar a compra de fato "
    "o cliente precisa fazer um cadastro e estar logado."
)
OWNER_ANSWER = "ID do usuário autenticado, obtido de uma sessão validada no servidor."
VALUES = {
    "field_1": "cliente",
    "field_2": "efetivar a compra",
    "field_3": "produtos no carrinho",
    "field_4": OWNER_ANSWER,
    "field_5": "para efetivar a compra de fato o cliente precisa fazer um cadastro e estar logado.",
    "field_6": "Proposta: negar a conclusão da compra enquanto o cliente não estiver autenticado.",
    "field_7": "Critério proposto: testar dois clientes e impedir acesso ao carrinho de outro cliente.",
    "field_8": "Proposta: solicitar novo login quando a sessão expirar, preservando o carrinho.",
    "field_9": "Proposta: informar que o recurso não está disponível sem expor dados de outro cliente.",
    "field_10": "Proposta: registrar a decisão de acesso sem senha, token ou conteúdo privado.",
}


@pytest.fixture
def legacy_selection(system, monkeypatch):
    original = TemplatePreparations.start

    def start(service, *args, **kwargs):
        result = original(service, *args, **kwargs)
        with system["repo"].sessions.begin() as db:
            operation = db.get(Operation, result["id"])
            payload = {
                key: value
                for key, value in operation.payload.items()
                if key != "proposal_schema"
            }
            payload["template_preparation_contract"] = 3
            selection = build_selection(payload)
            payload.update(selection_catalog=selection["catalog"], selection_schema=selection["schema"])
            operation.payload = payload
            operation.policy = {
                **{
                    key: value
                    for key, value in operation.policy.items()
                    if key != "preparation_proposal_schema"
                },
                "template_preparation_contract": 3,
                "preparation_selection_schema": selection["schema"],
            }
        return result

    monkeypatch.setattr(TemplatePreparations, "start", start)


def gemini_result(value):
    return httpx.Response(
        200,
        json={
            "responseId": "synthetic-bakery-response",
            "candidates": [
                {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(value)}]}}
            ],
            "usageMetadata": {
                "promptTokenCount": 200,
                "candidatesTokenCount": 100,
                "thoughtsTokenCount": 0,
            },
        },
    )


def managed_bakery_gateway(system, auth_mode, transform=None):
    requests = []

    def respond(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-bakery-oauth-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        assert "gemini-2.5-flash:generateContent" in request.url.path
        body = json.loads(request.content)
        try:
            prompt = json.loads(body["contents"][0]["parts"][0]["text"])
        except ValueError:
            return gemini_result({"ok": True})
        if not isinstance(prompt, dict) or "templates" not in prompt:
            return gemini_result({"ok": True})
        requests.append(prompt)
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
        schema = body["generationConfig"]["responseSchema"]
        version = prompt["template_preparation_contract"]
        assert version in {3, 4}
        assert "schema" not in prompt
        fields_schema = schema["properties"]["items"]["properties"]["autorizacao"]
        assert set(fields_schema["required"]) == set(fields_schema["properties"])
        for key, field_schema in fields_schema["properties"].items():
            if version == 4:
                assert set(field_schema["properties"]) == {"proposal"}
                assert field_schema["required"] == ["proposal"]
            else:
                branches = {next(iter(branch["properties"])) for branch in field_schema["anyOf"]}
                assert branches == ({"extracts", "missing"} if int(key.split("_")[1]) <= 5 else {"proposal", "missing"})
        sources = prompt_sources(prompt)
        request_source = next(item for item in sources if item["id"] == "request:message")
        owner_source = next(
            (
                item
                for item in sources
                if item["type"] in {"request", "user_answer"}
                and "".join(OWNER_ANSWER.split()) in "".join(item["content"].split())
            ),
            None,
        )
        items = {}
        for template in prompt["templates"]:
            assert template["attachment_id"] == "autorizacao"
            fields = {}
            for field in template["fields"]:
                key = field["key"]
                if version == 4:
                    fields[key] = {"proposal": VALUES[key]}
                    continue
                if key == "field_4" and owner_source is None:
                    fields[key] = {"missing": "Qual identidade validada no servidor define o dono do carrinho?"}
                    continue
                source = owner_source if key == "field_4" else request_source
                fields[key] = (
                    {"extracts": [select_phrase(prompt, source["id"], VALUES[key])]}
                    if int(key.split("_")[1]) <= 5
                    else {"proposal": VALUES[key]}
                )
            items[template["attachment_id"]] = fields
        result = {"items": items}
        if transform is not None:
            result = transform(result, prompt, body)
        return gemini_result(result)

    transport = httpx.MockTransport(respond)
    initialize_key(system["config"].credential_key_path, system["repo"].sessions)
    registry = AIRegistry(system["repo"], transport=transport)
    connection = create_connection(
        system,
        registry,
        {
            **connection_data(),
            "provider": "gemini",
            "auth_mode": auth_mode,
            "project_id": "synthetic-bakery-project",
            "credential": json.dumps(google_key()) if auth_mode == "service_account" else KEY,
        },
    )
    pricing = {
        "model": "gemini-2.5-flash",
        "input_usd_per_million": "0",
        "cached_usd_per_million": "0",
        "output_usd_per_million": "125",
    }
    model = create_model(system, registry, connection, **pricing)
    assert probe(system, registry, connection, model)["state"] == "verified"
    registry.save_model(
        system["admin"],
        connection["id"],
        model["id"],
        new_id(),
        model_data(**pricing, enabled=True, expected_version=model["version"]),
    )
    system["config"].catalog_path.write_text('{"models":[]}')
    system["catalog"] = ModelCatalog(system["config"], registry)
    return HttpModelGateway(system["config"], transport=transport, registry=registry), requests


@pytest.mark.parametrize("auth_mode", ["api_key", "service_account"])
@pytest.mark.parametrize("owner_known", [False, True])
def test_bakery_request_uses_managed_gemini_and_only_asks_for_missing_owner(
    system, workflow, auth_mode, owner_known, legacy_selection
):
    flow, actor, project = workflow
    gateway, requests = managed_bakery_gateway(system, auth_mode)
    dependencies = (
        system["config"], system["engine"], system["repo"], system["catalog"], gateway
    )
    path = f"/api/v1/projects/{project['id']}"
    with TestClient(create_app(False, dependencies)) as client:
        headers = login(client, system)
        current = client.get(path + "/build-draft").json()
        message = BAKERY_REQUEST + (" " + OWNER_ANSWER if owner_known else "")
        current["data"].update(objective="Melhorar a loja da padaria", message_text=message)
        response = client.put(
            path + "/build-draft",
            headers={**headers, "Idempotency-Key": "bakery-idea", "If-Match": str(current["version"])},
            json=current["data"],
        )
        assert response.status_code == 200, response.text
        response = client.post(
            path + "/recommendations",
            headers={**headers, "Idempotency-Key": "bakery-prepare"},
            json={"expected_draft_revision": response.json()["version"]},
        )
        assert response.status_code == 200, response.text
        template = next(item for item in client.get("/api/v1/templates").json()["items"] if item["id"] == "autorizacao")
        current = client.get(path + "/build-draft").json()
        current["data"]["attachments"] = [{
            "template_id": template["id"],
            "source_version_id": template["version_id"],
            "fields": {},
            "edited_content": template["content"],
            "enabled": True,
        }]
        response = client.put(
            path + "/build-draft",
            headers={**headers, "Idempotency-Key": "bakery-select", "If-Match": str(current["version"])},
            json=current["data"],
        )
        assert response.status_code == 200, response.text
        current, _ = dispatch(system, flow, actor, project, gateway)
        attachment = current["data"]["attachments"][0]
        assert attachment["fill_error_code"] is None
        assert len(requests) == 1
        if not owner_known:
            assert attachment["fill_state"] == "needs_input", attachment
            assert attachment["fields"] == {key: value for key, value in VALUES.items() if key != "field_4"}
            assert {item["field_id"] for item in attachment["validation_errors"]} == {"field_4"}
            preparation = client.get(path + "/template-preparations/" + attachment["preparation_id"]).json()
            assert preparation["state"] == "needs_input"
            assert preparation["items"][0]["errors"] == []
            question = preparation["items"][0]["questions"][0]
            assert question["field_ids"] == ["field_4"]
            answer = {
                "expected_state_version": preparation["state_version"],
                "expected_draft_revision": current["version"],
                "answers": [{"question_id": question["id"], "value": OWNER_ANSWER}],
            }
            endpoint = path + "/template-preparations/" + preparation["id"] + "/answers"
            answer_headers = {**headers, "Idempotency-Key": "bakery-owner-answer"}
            response = client.post(endpoint, headers=answer_headers, json=answer)
            assert response.status_code == 202, response.text
            followup = response.json()
            assert client.post(endpoint, headers=answer_headers, json=answer).json()["id"] == followup["id"]
            assert Worker(system["repo"], gateway, system["catalog"]).tick(followup["id"])
            assert len(requests) == 2
            assert [field["key"] for field in requests[1]["templates"][0]["fields"]] == ["field_4"]
            current = client.get(path + "/build-draft").json()
            attachment = current["data"]["attachments"][0]
            assert attachment["field_values"]["field_4"]["source_type"] == "user_answer"
        assert attachment["fill_state"] == "ready", attachment
        assert attachment["fields"] == VALUES
        assert attachment["validation_errors"] == []
        assert "{{" not in attachment["edited_content"]
        assert client.get(path + "/build-draft").json()["data"]["attachments"][0]["fields"] == VALUES
        response = client.post(
            path + "/build-review",
            headers={**headers, "Idempotency-Key": "bakery-review"},
            json={"expected_draft_revision": current["version"], "confirm_empty": False},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["can_build"]
    with system["repo"].sessions() as db:
        operations = db.scalars(select(Operation).where(Operation.type == "template_preparation")).all()
        assert len(operations) == (1 if owner_known else 2)
        assert all(operation.policy["model"] == "gemini-2.5-flash" for operation in operations)
        assert all(operation.policy["template_preparation_contract"] == 3 for operation in operations)
        assert all(operation.payload["template_preparation_contract"] == 3 for operation in operations)
        assert all(operation.payload["selection_catalog"] for operation in operations)
        assert all(operation.payload["selection_schema"] == operation.policy["preparation_selection_schema"] for operation in operations)
        assert all(operation.billing_status == "settled" for operation in operations)
        assert db.scalar(select(func.count()).select_from(Reservation)) == len(operations)
        assert db.scalar(select(func.count()).select_from(Ledger).where(Ledger.kind == "debit")) == len(operations)
        assert db.scalar(select(func.count()).select_from(Operation).where(Operation.type == "generation")) == 0
        assert db.scalar(select(func.count()).select_from(Revision)) == 1


@pytest.mark.parametrize("auth_mode", ["api_key", "service_account"])
@pytest.mark.parametrize("owner_known", [False, True])
def test_bakery_request_proposes_all_ten_fields_for_developer_review(
    system, workflow, auth_mode, owner_known
):
    flow, actor, project = workflow
    gateway, requests = managed_bakery_gateway(system, auth_mode)
    message = BAKERY_REQUEST + (" " + OWNER_ANSWER if owner_known else "")
    current = save(
        flow,
        actor,
        project,
        lambda data: data.update(objective="Melhorar a loja da padaria", message_text=message),
    )
    Recommendations(flow).recommend(
        actor,
        project["id"],
        "bakery-review-proposals",
        {"expected_draft_revision": current["version"]},
    )
    select_template(flow, actor, project, "autorizacao")
    current, _ = dispatch(system, flow, actor, project, gateway)
    attachment = current["data"]["attachments"][0]
    assert attachment["fill_state"] == "ready", attachment
    assert attachment["fill_error_code"] is None
    assert attachment["fill_questions"] == []
    assert attachment["validation_errors"] == []
    assert attachment["fields"] == VALUES
    assert len(requests) == 1
    assert requests[0]["template_preparation_contract"] == 4
    assert "selection_catalog" not in requests[0]
    assert next(item for item in requests[0]["context"] if item["id"] == "request:message")["content"] == message
    assert all(
        value["source_type"] == "policy_suggestion" and value["source_refs"] == []
        for value in attachment["field_values"].values()
    )
    assert not current["data"]["can_build"]
    assert review(flow, actor, project)["data"]["can_build"]
    with system["repo"].sessions() as db:
        operation = db.scalar(select(Operation).where(Operation.type == "template_preparation"))
        assert operation.payload["template_preparation_contract"] == 4
        assert operation.policy["template_preparation_contract"] == 4
        assert operation.payload["proposal_schema"] == operation.policy["preparation_proposal_schema"]
        assert "selection_catalog" not in operation.payload
        assert operation.state == "succeeded" and operation.billing_status == "settled"
        assert db.scalar(select(func.count()).select_from(Reservation)) == 1
        debits = db.scalars(select(Ledger).where(Ledger.kind == "debit")).all()
        assert len(debits) == 1
        assert debits[0].amount == cost(operation.policy, {"input": 200, "cached": 0, "output": 100})
        assert db.scalar(select(func.count()).select_from(Operation).where(Operation.type == "generation")) == 0
        assert db.scalar(select(func.count()).select_from(Revision)) == 1
