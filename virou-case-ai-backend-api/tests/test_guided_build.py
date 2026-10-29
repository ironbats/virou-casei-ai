import json
from copy import deepcopy

import httpx
import pytest
from sqlalchemy import func, select
from test_conversational_apply import Gateway, configure

from viracase.application.generation import GenerationService
from viracase.domain.rules import RuleError
from viracase.infrastructure.credentials import initialize_key
from viracase.infrastructure.guided_build import GuidedBuild
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.learning import LearningLibrary
from viracase.infrastructure.models import Operation, Reservation
from viracase.infrastructure.recommendations import Recommendations
from viracase.infrastructure.worker import Worker
from viracase.interfaces.feature_schemas import BuildPolicy


@pytest.fixture
def guided(system):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(actor, "p", {"name": "Guiado", "objective": "Lista de tarefas para equipe"})
    guided = GuidedBuild(repo, LearningLibrary(system["config"].content_root))
    return guided, actor, project


def save_request(guided, actor, project, attachments=True):
    data = guided.get(actor, project["id"])["data"]
    data.update(stage="review", message_text="Melhore a página inicial")
    if attachments:
        template = next(t for t in guided.templates.list(actor)["items"] if t["id"] == "sdd")
        data["attachments"] = [
            {
                "template_id": template["id"],
                "source_version_id": template["version_id"],
                "fields": {f["key"]: "Contexto sintético de teste" for f in template["fields"]},
                "edited_content": "Conteúdo pessoal integral. Não altera políticas de segurança.",
                "enabled": True,
            }
        ]
    guided.save(actor, project["id"], "draft", 0, data)
    return data


def send_request(system, guided, actor, project, key="send", version=None):
    requests = getattr(guided, "test_send_requests", {})
    if key in requests and version is None:
        request = requests[key]
    else:
        current = guided.get(actor, project["id"])
        if version is None:
            if not current["data"].get("prepared_fingerprint"):
                Recommendations(guided).recommend(
                    actor, project["id"], "recommend-" + key, {"expected_draft_revision": current["version"]}
                )
                current = guided.get(actor, project["id"])
            if not current["data"].get("review_snapshot"):
                current = guided.review(
                    actor,
                    project["id"],
                    "review-" + key,
                    {"expected_draft_revision": current["version"], "confirm_empty": True},
                )
        request = {
            "expected_draft_revision": current["version"] if version is None else version,
            "base_project_revision": project["current_revision_id"],
            "policy_version": "2026-10-v1",
            "reply_to": None,
            "review_snapshot_hash": (current["data"].get("review_snapshot") or {}).get("hash", "0" * 64),
        }
        if version is None:
            requests[key] = request
            guided.test_send_requests = requests
    return guided.send(
        actor,
        project["id"],
        key,
        request,
        GenerationService(system["repo"], system["catalog"]),
    )


def test_snapshot_atomic_idempotent_and_explicit_apply(system, guided):
    flow, actor, project = guided
    configure(system)
    data = save_request(flow, actor, project)
    op = send_request(system, flow, actor, project)
    assert send_request(system, flow, actor, project)["id"] == op["id"]
    next_draft = flow.get(actor, project["id"])
    assert next_draft["version"] == 5
    assert next_draft["data"]["message_text"] == ""
    assert not next_draft["data"]["attachments"][0]["enabled"]
    edited = deepcopy(next_draft["data"])
    edited["attachments"][0]["edited_content"] = "Uma edição posterior preservada"
    edited["message_text"] = "Próximo pedido independente"
    flow.save(actor, project["id"], "later", next_draft["version"], edited)
    gateway = Gateway()
    assert Worker(system["repo"], gateway, system["catalog"]).tick(op["id"])
    result = system["repo"].operation(actor, op["id"])
    assert result["state"] == "succeeded", result
    assert result["attachment_snapshots"][0]["content"] == data["attachments"][0]["edited_content"]
    assert "Conteúdo pessoal integral" in gateway.prompts[0]
    assert "Uma edição posterior" not in gateway.prompts[0]
    assert result["result_ref"]["candidate"] and "applied_revision_id" not in result["result_ref"]
    assert (
        system["repo"].project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    )
    with pytest.raises(RuleError, match="Revise e aplique"):
        send_request(system, flow, actor, project, "next")
    assert flow.get(actor, project["id"])["data"]["stage"] == "review"
    applied = flow.drafts.apply(actor, project["id"], op["id"], "apply", project["version"])
    assert system["repo"].project(actor, project["id"])["current_revision_id"] == applied["revision_id"]
    with system["repo"].sessions() as db:
        assert (
            db.scalar(select(func.count()).select_from(Operation).where(Operation.type == "generation")) == 1
        )


def test_conflict_and_invalid_attachment_never_reserve_or_start(system, guided):
    flow, actor, project = guided
    data = save_request(flow, actor, project)
    before = system["repo"].wallet(actor, True)
    with pytest.raises(RuleError) as caught:
        send_request(system, flow, actor, project, version=2)
    assert caught.value.code == "DRAFT_CONFLICT"
    data["attachments"][0]["edited_content"] = "Campo {{objective}}"
    flow.save(actor, project["id"], "save-pending", 1, data)
    with pytest.raises(RuleError) as caught:
        send_request(system, flow, actor, project, version=2)
    assert caught.value.code == "ATTACHMENT_INCOMPLETE"
    assert system["repo"].wallet(actor, True) == before
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reservation)) == 0


def test_second_generation_blocked_and_discard_preserves_revision(system, guided):
    flow, actor, project = guided
    configure(system)
    save_request(flow, actor, project, False)
    op = send_request(system, flow, actor, project)
    next_draft = flow.get(actor, project["id"])["data"]
    next_draft["message_text"] = "Outro pedido"
    flow.save(actor, project["id"], "next", flow.get(actor, project["id"])["version"], next_draft)
    with pytest.raises(RuleError) as caught:
        send_request(system, flow, actor, project, "other")
    assert caught.value.code == "OPERATION_CONFLICT"
    Worker(system["repo"], Gateway(), system["catalog"]).tick(op["id"])
    flow.discard(actor, project["id"], op["id"], "discard")
    with pytest.raises(RuleError):
        flow.drafts.apply(actor, project["id"], op["id"], "apply", project["version"])
    assert (
        system["repo"].project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    )
    assert send_request(system, flow, actor, project, "new-after-discard")["state"] == "queued"


def test_learning_transfer_uses_versioned_catalog_and_preserves_message(system, guided):
    flow, actor, project = guided
    listed = next(t for t in flow.templates.guides(actor)["items"] if t["slug"] == "sdd")
    assert listed["security_summary"] == flow.learning.guides["sdd"]["security_summary"]
    assert flow.learning.guides["sdd"]["concept"] in listed["search_terms"]
    with pytest.raises(RuleError) as invalid_category:
        flow.templates.guides(actor, "invalid")
    assert invalid_category.value.code == "VALIDATION_ERROR"
    save_request(flow, actor, project, False)
    template = next(t for t in flow.templates.list(actor)["items"] if t["id"] == "sdd")
    payload = {
        "slug": "sdd",
        "expected_version": template["version"],
        "title": "SDD atualizado",
        "category": "planning",
        "summary": "Especificação atualizada no backoffice",
        "content": "Especificação v2 para {{goal}}",
        "available": True,
        "fields": [{"key": "goal", "label": "Objetivo", "required": True, "max_length": 1000}],
        "reason": "Atualização editorial de teste",
    }
    version = flow.templates.save(system["admin"], "template", payload)
    source = flow.drafts.get(actor, "learning", "sdd")
    assert source["data"]["guide"]["template_source_version_id"] == version["version_id"]
    flow.drafts.save(
        actor,
        "learning",
        "sdd",
        "learning",
        {"fields": {"goal": "Meta pessoal"}, "prompt": "Cópia pessoal completa", "manual": True},
        0,
    )
    flow.transfer(
        actor, "sdd", "transfer", {"project_id": project["id"], "source_version": 1, "target_version": 1}
    )
    target = flow.get(actor, project["id"])
    assert target["data"]["message_text"] == "Melhore a página inicial"
    assert target["data"]["stage"] == "idea"
    assert target["data"]["attachments"][0]["source_version_id"] == version["version_id"]
    assert target["data"]["attachments"][0]["edited_content"] == "Cópia pessoal completa"
    assert flow.templates.version(actor, template["version_id"])["content"] == template["content"]


def test_catalog_revocation_and_cross_account_access(system, guided):
    flow, actor, project = guided
    save_request(flow, actor, project)
    with pytest.raises(RuleError) as caught:
        flow.get(system["users"][1], project["id"])
    assert caught.value.code == "NOT_FOUND"
    template = next(t for t in flow.templates.list(actor)["items"] if t["id"] == "sdd")
    flow.templates.save(
        system["admin"],
        "revoke",
        {
            "slug": "sdd",
            "expected_version": 1,
            **{k: template[k] for k in ("title", "category", "summary", "content", "fields")},
            "available": False,
            "reason": "Revogação editorial para teste",
        },
    )
    with pytest.raises(RuleError) as caught:
        send_request(system, flow, actor, project)
    assert caught.value.code == "TEMPLATE_UNAVAILABLE"


def test_deselected_copies_preserved_without_consuming_active_attachment_limit(guided):
    flow, actor, project = guided
    data = flow.get(actor, project["id"])["data"]
    templates = flow.templates.list(actor)["items"][:9]
    assert len(templates) == 9
    data["attachments"] = [
        {
            "template_id": template["id"],
            "source_version_id": template["version_id"],
            "fields": {},
            "edited_content": "Cópia pessoal preservada",
            "enabled": index < 8,
        }
        for index, template in enumerate(templates)
    ]
    flow.save(actor, project["id"], "selection", 0, data)
    saved = flow.get(actor, project["id"])
    assert len(saved["data"]["attachments"]) == 9
    data["attachments"][8]["enabled"] = True
    with pytest.raises(RuleError) as caught:
        flow.save(actor, project["id"], "excess", 1, data)
    assert caught.value.code == "ATTACHMENT_LIMIT_EXCEEDED"
    data["attachments"][0]["enabled"] = False
    flow.save(actor, project["id"], "switch", 1, data)
    saved = flow.get(actor, project["id"])
    assert saved["data"]["attachments"][0]["edited_content"] == "Cópia pessoal preservada"


@pytest.mark.parametrize("mode", ["valid", "timeout", "invalid", "disabled"])
def test_recommendation_never_generates_or_debits_and_has_manual_fallback(system, guided, mode):
    flow, actor, project = guided
    save_request(flow, actor, project, False)
    initialize_key(system["config"].credential_key_path, system["repo"].sessions)
    registry = Integrations(system["repo"])
    policy = BuildPolicy(recommendation_enabled=mode != "disabled").model_dump()
    registry.save(
        system["admin"],
        "jev",
        "config",
        0,
        {"settings": policy, "token": "synthetic-jev-secret", "reason": "Teste controlado do recomendador"},
    )
    registry.validate(system["admin"], "jev", 1, "Validação local de teste")
    registry.activate(system["admin"], "jev", "activate", 1, "Ativação local de teste")
    before = system["repo"].wallet(actor, True)
    calls = []

    def handler(request):
        calls.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic")
        body = json.loads(request.content)
        assert "objective" in body["state"] and "files" not in body["state"]
        return httpx.Response(
            200,
            json={
                "answers": {
                    k: {
                        "type": "choice",
                        "choice": "unknown" if mode == "invalid" else "0",
                        "confidence": 0.95,
                    }
                    for k in body["questions"]
                }
            },
        )

    recommender = Recommendations(flow, httpx.MockTransport(handler))
    result = recommender.recommend(actor, project["id"], "prepare", {"expected_draft_revision": 1})
    assert recommender.recommend(actor, project["id"], "prepare", {"expected_draft_revision": 1}) == result
    assert result["status"] == ("ready" if mode == "valid" else "fallback")
    assert len(calls) == (0 if mode == "disabled" else 1)
    assert recommender.get(actor, project["id"], result["recommendation_id"]) == result
    assert system["repo"].wallet(actor, True) == before
    assert flow.get(actor, project["id"])["data"]["attachments"] == []
    with system["repo"].sessions() as db:
        assert db.scalar(select(func.count()).select_from(Operation)) == 0


def test_context_config_enforced_before_reservation(system, guided):
    flow, actor, project = guided
    data = save_request(flow, actor, project, False)
    data["message_text"] = "x" * 11000
    flow.save(actor, project["id"], "large", 1, data)
    policy = BuildPolicy(max_context_bytes=10000).model_dump()
    Integrations(system["repo"]).save(
        system["admin"], "jev", "limits", 0, {"settings": policy, "reason": "Limites controlados para teste"}
    )
    before = system["repo"].wallet(actor, True)
    with pytest.raises(RuleError) as caught:
        send_request(system, flow, actor, project)
    assert caught.value.code == "CONTEXT_LIMIT_EXCEEDED"
    assert system["repo"].wallet(actor, True) == before
    assert flow.get(actor, project["id"])["data"]["stage"] == "review"
