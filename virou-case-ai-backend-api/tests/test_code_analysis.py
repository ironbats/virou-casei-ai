import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_conversation_runtime import fund_generation, login

from viracase.application.code_analysis import analysis_schema, parse_analysis
from viracase.application.generation import GenerationService
from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError, utcnow
from viracase.infrastructure.clarifications import Clarifications
from viracase.infrastructure.code_analysis import CodeAnalysis
from viracase.infrastructure.conversation_state import recent_history
from viracase.infrastructure.drafts import Drafts
from viracase.infrastructure.integrations import Integrations
from viracase.infrastructure.models import Account, Attempt, Grant, Job, Operation, Revision
from viracase.infrastructure.providers import HttpModelGateway, ProviderRejected, ProviderUncertain
from viracase.infrastructure.worker import Worker
from viracase.interfaces.api import create_app


@pytest.fixture
def workspace(system):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor, "analysis-project", {"name": "Análise", "objective": "Aplicação de tarefas"}
    )
    return repo, actor, project, Drafts(repo, None)


def report(**changes):
    return {
        "summary": "A página contém um problema que impede a renderização esperada.",
        "issues": [
            {
                "severity": "error",
                "path": "public/index.html",
                "line": 1,
                "title": "Elemento de resultado ausente",
                "explanation": "A página não contém o resultado esperado.",
                "suggestion": "Adicione o elemento de resultado no corpo da página.",
            }
        ],
        "limitations": [],
        **changes,
    }


class Gateway:
    def __init__(self, mode="complete", on_dispatch=None):
        self.mode, self.on_dispatch, self.calls = mode, on_dispatch, []

    def generate(self, policy, prompt):
        self.calls.append((policy, prompt))
        if self.on_dispatch:
            self.on_dispatch()
        if self.mode == "rejected":
            raise ProviderRejected("synthetic")
        if self.mode == "uncertain":
            raise ProviderUncertain("synthetic")
        usage = {"input": 100, "cached": 0, "output": 100}
        if self.mode == "cost":
            usage["output"] = 100_000_000
        if not policy.get("code_analysis_contract"):
            body = {
                "action": "implement",
                "message": "Adicionei o resultado preservando a página.",
                "files": [],
                "edits": [{"path": "public/index.html", "old": "</body>", "new": "<p>Corrigido</p></body>"}],
                "complete": True,
            }
        elif self.mode == "invalid":
            body = {"files": {"public/index.html": "substituir"}}
        elif self.mode == "no_issues":
            body = report(issues=[])
        elif self.mode == "secret":
            body = report(summary="authorization: Bearer synthetic-analysis-secret-token")
        else:
            body = report()
        return ModelResult(
            json.dumps(body), usage, "synthetic", "GENERATION_TRUNCATED" if self.mode == "truncated" else None
        )


def request(project, **changes):
    return {
        "revision_id": project["current_revision_id"],
        "source": "editor",
        "publication_operation_id": None,
        "runtime_error": None,
        "policy_version": "2026-10-v1",
        **changes,
    }


def start(system, workspace, gateway=None, **changes):
    repo, actor, project, _ = workspace
    service = CodeAnalysis(repo, system["catalog"])
    operation = service.start(actor, project["id"], "analysis", request(project, **changes))
    if gateway:
        assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
        operation = repo.operation(actor, operation["id"])
    return service, operation


def correction(system, workspace, service, operation, key="correction"):
    _, actor, project, _ = workspace
    return service.correct(
        actor,
        project["id"],
        operation["id"],
        key,
        {
            "base_project_revision": project["current_revision_id"],
            "policy_version": "2026-10-v1",
        },
        GenerationService(system["repo"], system["catalog"]),
    )


def test_analysis_is_read_only_billed_once_and_recoverable(system, workspace):
    repo, actor, project, _ = workspace
    gateway = Gateway()
    before = repo.source_files(actor, project["id"], project["current_revision_id"])
    service, operation = start(system, workspace, gateway)
    assert operation["state"] == "succeeded", operation
    assert operation["billing_status"] == "settled"
    result = operation["result_ref"]
    assert result["kind"] == "code_analysis" and result["can_request_correction"]
    assert result["revision_id"] == project["current_revision_id"]
    assert result["summary"] == report()["summary"]
    assert "nenhuma execução" in result["limitations"][-1]
    assert repo.source_files(actor, project["id"], project["current_revision_id"]) == before
    assert repo.project(actor, project["id"])["version"] == project["version"]
    assert service.start(actor, project["id"], "analysis", request(project))["state"] == "succeeded"
    assert not Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    assert len(gateway.calls) == 1
    assert (
        repo.operations(actor, project["id"], latest=True, kind="code_analysis")["items"][0]["id"]
        == operation["id"]
    )
    with repo.sessions() as db:
        assert (
            db.scalar(select(func.count()).select_from(Revision).where(Revision.project_id == project["id"]))
            == 1
        )
        assert db.scalar(select(Attempt).where(Attempt.operation_id == operation["id"])).state == "confirmed"


def test_correction_requires_review_and_is_deduplicated_after_completion(system, workspace):
    fund_generation(system)
    repo, actor, project, drafts = workspace
    gateway = Gateway()
    service, analysis = start(system, workspace, gateway)
    operation = correction(system, workspace, service, analysis)
    assert correction(system, workspace, service, analysis, "different-click")["id"] == operation["id"]
    assert repo.operation(actor, analysis["id"])["result_ref"]["correction_operation_id"] == operation["id"]
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    complete = repo.operation(actor, operation["id"])
    assert complete["state"] == "succeeded", complete
    assert complete["result_ref"]["candidate"] and "applied_revision_id" not in complete["result_ref"]
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    applied = drafts.apply(actor, project["id"], operation["id"], "apply-fix", project["version"])
    assert "Corrigido" in repo.source_files(actor, project["id"], applied["revision_id"])["public/index.html"]
    assert correction(system, workspace, service, analysis, "after-apply")["id"] == operation["id"]
    assert len(gateway.calls) == 2


@pytest.mark.parametrize(
    "mode,state,code",
    [
        ("invalid", "failed", "INVALID_CODE_ANALYSIS"),
        ("truncated", "failed", "GENERATION_TRUNCATED"),
        ("rejected", "failed", "PROVIDER_REJECTED"),
        ("uncertain", "reconciling", "PROVIDER_RESULT_UNKNOWN"),
        ("cost", "reconciling", "COST_DISCREPANCY"),
    ],
)
def test_provider_failures_never_modify_source_or_replay(system, workspace, mode, state, code):
    repo, actor, project, _ = workspace
    gateway = Gateway(mode)
    _, operation = start(system, workspace, gateway)
    assert (operation["state"], operation["error_code"]) == (state, code), operation
    assert not Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    assert len(gateway.calls) == 1
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    assert not operation["result_ref"]["can_request_correction"]


@pytest.mark.parametrize("change", ["revision", "draft", "no_issues"])
def test_correction_refuses_outdated_unsaved_or_non_actionable_analysis(system, workspace, change):
    repo, actor, project, drafts = workspace
    service, analysis = start(
        system, workspace, Gateway("no_issues" if change == "no_issues" else "complete")
    )
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    files["public/index.html"] += "\n<p>Manual</p>"
    if change == "revision":
        repo.save_revision(
            actor,
            project["id"],
            "manual-revision",
            {"base_revision_id": project["current_revision_id"], "files": files},
            project["version"],
        )
    elif change == "draft":
        drafts.save(
            actor,
            "project",
            project["id"],
            "manual-draft",
            {"base_revision_id": project["current_revision_id"], "files": files, "prompt": ""},
            0,
        )
    with pytest.raises(RuleError) as caught:
        correction(system, workspace, service, analysis)
    assert (
        caught.value.code
        == {
            "revision": "ANALYSIS_OUTDATED",
            "draft": "DRAFT_NOT_SAVED",
            "no_issues": "ANALYSIS_NOT_ACTIONABLE",
        }[change]
    )
    assert repo.operations(actor, project["id"], kind="generation")["items"] == []


def test_publication_evidence_is_owned_sanitized_and_marks_old_revision(system, workspace):
    repo, actor, project, _ = workspace
    with repo.sessions.begin() as db:
        publication = Operation(
            account_id=actor,
            project_id=project["id"],
            type="publication",
            state="failed",
            payload={"revision_id": project["current_revision_id"]},
            error_code="BUILD_FAILED",
            result={
                "publication_failure": {
                    "message": "token=synthetic-private-value",
                    "build": {
                        "category": "source",
                        "issues": [
                            {
                                "code": "SYNTAX_ERROR",
                                "path": "public/index.html",
                                "line": 1,
                                "message": "Unexpected token",
                            }
                        ],
                    },
                }
            },
        )
        db.add(publication)
        db.flush()
        publication_id = publication.id
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    files["public/index.html"] += "\n<p>Nova revisão</p>"
    saved = repo.save_revision(
        actor,
        project["id"],
        "new",
        {"base_revision_id": project["current_revision_id"], "files": files},
        project["version"],
    )
    gateway = Gateway()
    _, operation = start(
        system,
        workspace,
        gateway,
        revision_id=saved["revision"]["id"],
        source="publication",
        publication_operation_id=publication_id,
    )
    assert operation["state"] == "succeeded", operation
    assert "synthetic-private-value" not in gateway.calls[0][1]
    assert '"matches_revision": false' in gateway.calls[0][1]
    assert "outra revisão" in operation["result_ref"]["limitations"][-1]


def test_runtime_errors_and_provider_messages_do_not_expose_secrets(system, workspace):
    _, operation = start(
        system,
        workspace,
        Gateway("secret"),
        source="preview",
        runtime_error="Authorization: Bearer synthetic-runtime-secret\nTypeError at https://localhost/private?token=value",
    )
    assert operation["state"] == "succeeded", operation
    with system["repo"].sessions() as db:
        record = db.get(Operation, operation["id"])
        stored = json.dumps({"payload": record.payload, "result": record.result})
    assert "synthetic-runtime-secret" not in stored and "synthetic-analysis-secret-token" not in stored
    assert "localhost/private" not in stored


@pytest.mark.parametrize("when", ["before", "during"])
def test_cancellation_settles_reservation_and_never_stores_diagnosis(system, workspace, when):
    repo, actor, _, _ = workspace
    _, operation = start(system, workspace)
    gateway = Gateway(on_dispatch=lambda: repo.cancel(actor, operation["id"], "cancel-during"))
    if when == "before":
        repo.cancel(actor, operation["id"], "cancel-before")
    Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    final = repo.operation(actor, operation["id"])
    assert final["state"] == "cancelled" and "summary" not in final["result_ref"]
    assert len(gateway.calls) == (0 if when == "before" else 1)
    assert repo.wallet(actor)["reserved_ratio"] == 0


@pytest.mark.parametrize("blocked", ["quota", "suspended", "restore"])
def test_analysis_respects_budget_access_and_restore_hold(system, workspace, blocked):
    repo, actor, _, _ = workspace
    with repo.sessions.begin() as db:
        if blocked == "quota":
            grant = db.scalar(select(Grant).where(Grant.account_id == actor))
            grant.total, grant.available = Decimal(0), Decimal(0)
        elif blocked == "suspended":
            db.get(Account, actor).suspended = True
    if blocked == "restore":
        system["config"].restore_hold = True
    with pytest.raises(RuleError):
        start(system, workspace)
    assert repo.operations(actor, kind="code_analysis")["items"] == []


def test_recovery_does_not_repeat_uncertain_provider_dispatch(system, workspace):
    repo, actor, _, _ = workspace
    _, operation = start(system, workspace)
    with repo.sessions.begin() as db:
        job = db.scalar(select(Job).where(Job.operation_id == operation["id"]))
        job.state, job.lease_until = "running", utcnow() - timedelta(seconds=1)
        db.add(Attempt(operation_id=operation["id"], provider="openai", model="gpt-6-luna", state="sent"))
    gateway = Gateway()
    Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    assert repo.operation(actor, operation["id"])["state"] == "reconciling"
    assert gateway.calls == []


@pytest.mark.parametrize(
    "bad",
    [
        {"files": {}},
        {"issues": [{**report()["issues"][0], "path": "../escape"}]},
        {"issues": [{**report()["issues"][0], "line": 999}]},
        {"issues": [{**report()["issues"][0], "line": True}]},
        {"issues": [{**report()["issues"][0], "path": "", "line": 1}]},
        {"issues": [{**report()["issues"][0], "severity": "fatal"}]},
    ],
)
def test_analysis_contract_rejects_generated_code_and_unverified_locations(bad):
    with pytest.raises(RuleError, match="diagnóstico válido"):
        parse_analysis(json.dumps({**report(), **bad}), {"public/index.html": "<html></html>"})


def test_authenticated_routes_enforce_isolation_csrf_and_contract(system, workspace):
    repo, _, project, _ = workspace
    gateway = Gateway()
    with TestClient(
        create_app(dependencies=(system["config"], system["engine"], repo, system["catalog"], gateway))
    ) as client:
        headers = login(client, system)
        path = f"/api/v1/projects/{project['id']}/code-analyses"
        denied = client.post(
            path, headers={"Origin": headers["Origin"], "Idempotency-Key": "csrf"}, json=request(project)
        )
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "CSRF_DENIED"
        invalid = client.post(
            path, headers={**headers, "Idempotency-Key": "invalid"}, json={**request(project), "files": {}}
        )
        assert invalid.status_code == 400
        other = repo.create_project(
            system["users"][1], "other", {"name": "Outro", "objective": "Outro projeto"}
        )
        denied = client.post(
            f"/api/v1/projects/{other['id']}/code-analyses",
            headers={**headers, "Idempotency-Key": "other"},
            json=request(other),
        )
        assert denied.status_code == 404
        accepted = client.post(
            path, headers={**headers, "Idempotency-Key": "accepted"}, json=request(project)
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["result_ref"]["revision_id"] == project["current_revision_id"]
        assert Worker(repo, gateway, system["catalog"]).tick(accepted.json()["id"])
        result = client.get("/api/v1/operations/" + accepted.json()["id"]).json()
        assert result["state"] == "succeeded"
        assert client.get("/api/v1/capabilities").json()["code_analysis"]["enabled"]


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_provider_wire_contract_uses_diagnostic_schema(system, provider):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if provider == "openai":
            return httpx.Response(
                200,
                json={
                    "id": "synthetic",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": json.dumps(report())}],
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 10},
                },
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps(report())}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 10},
            },
        )

    policy = {
        **system["policy"],
        "provider": provider,
        "model": "gpt-6-luna" if provider == "openai" else "gemini-3.8-flash",
        "code_analysis_contract": 1,
    }
    result = HttpModelGateway(system["config"], transport=httpx.MockTransport(handler)).generate(
        policy, "Diagnosticar"
    )
    assert json.loads(result.text) == report()
    schema = (
        calls[0]["text"]["format"]["schema"]
        if provider == "openai"
        else calls[0]["generationConfig"]["responseSchema"]
    )
    assert set(schema["properties"]) == set(analysis_schema()["properties"])
    assert "files" not in schema["properties"]


def test_simultaneous_correction_requests_create_one_generation(system, workspace):
    if system["engine"].dialect.name != "postgresql":
        pytest.skip("A concorrência usa o lock de conta do PostgreSQL.")
    fund_generation(system)
    service, analysis = start(system, workspace, Gateway())
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(correction, system, workspace, service, analysis, "parallel-" + str(index))
            for index in range(2)
        ]
        results = [future.result(timeout=20) for future in futures]
    assert results[0]["id"] == results[1]["id"]
    assert len(system["repo"].operations(workspace[1], workspace[2]["id"], kind="generation")["items"]) == 1


@pytest.mark.parametrize("mode", ["rejected", "cancelled"])
def test_failed_correction_can_retry_without_charging_a_new_analysis(system, workspace, mode):
    fund_generation(system)
    repo, actor, _, _ = workspace
    service, analysis = start(system, workspace, Gateway())
    first = correction(system, workspace, service, analysis, "first-correction")
    same = correction(system, workspace, service, analysis, "double-click")
    assert same["id"] == first["id"]
    if mode == "cancelled":
        repo.cancel(actor, first["id"], "cancel-correction")
    else:
        Worker(repo, Gateway("rejected"), system["catalog"]).tick(first["id"])
    assert correction(system, workspace, service, analysis, "first-correction")["id"] == first["id"]
    assert correction(system, workspace, service, analysis, "double-click")["id"] == first["id"]
    second = correction(system, workspace, service, analysis, "retry-correction")
    assert second["id"] != first["id"]
    assert repo.operation(actor, analysis["id"])["result_ref"]["correction_operation_id"] == second["id"]
    assert len(repo.operations(actor, kind="code_analysis")["items"]) == 1


def test_correction_preserves_backoffice_context_and_call_limits(system, workspace):
    service, analysis = start(system, workspace, Gateway())
    saved = Integrations(system["repo"]).save(
        system["admin"],
        "jev",
        "analysis-limits",
        0,
        {
            "settings": {
                "max_context_bytes": 12000,
                "max_generation_context_bytes": 18000,
                "max_generation_calls": 2,
            },
            "reason": "Limites específicos para correção",
        },
    )
    result = correction(system, workspace, service, analysis)
    with system["repo"].sessions() as db:
        operation = db.get(Operation, result["id"])
        assert operation.payload["context_limit_bytes"] == 12000
        assert operation.payload["generation_context_limit_bytes"] == 18000
        assert operation.policy["construction_policy_version"] == saved["version"]
        assert operation.policy["max_batches"] == 2


def test_correction_rejects_source_exceeding_backoffice_context_limit(system, workspace):
    repo, actor, project, _ = workspace
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    files["public/notes.txt"] = "Detalhe do projeto.\n" * 1000
    saved = repo.save_revision(
        actor,
        project["id"],
        "large-source",
        {"base_revision_id": project["current_revision_id"], "files": files},
        project["version"],
    )
    workspace = (repo, actor, saved["project"], workspace[3])
    service, analysis = start(system, workspace, Gateway())
    Integrations(repo).save(
        system["admin"],
        "jev",
        "small-limit",
        0,
        {
            "settings": {"max_generation_context_bytes": 10000},
            "reason": "Limite reduzido para novas operações",
        },
    )
    with pytest.raises(RuleError) as caught:
        correction(system, workspace, service, analysis)
    assert caught.value.code == "CONTEXT_LIMIT_EXCEEDED"
    assert repo.operations(actor, project["id"], kind="generation")["items"] == []


def test_correction_is_a_readable_conversation_with_immutable_analysis_context(system, workspace):
    fund_generation(system)
    repo, actor, project, _ = workspace
    gateway = Gateway()
    service, analysis = start(system, workspace, gateway, source="preview", runtime_error="Elemento ausente")
    operation = correction(system, workspace, service, analysis)
    assert operation["analysis_id"] == analysis["id"]
    assert operation["analysis_revision_id"] == project["current_revision_id"]
    assert "Corrija os problemas de código" in operation["request_text"]
    assert len(operation["request_text"]) < 250
    assert report()["summary"] not in operation["request_text"]
    assert "analysis_context" not in operation
    with repo.sessions() as db:
        record = db.get(Operation, operation["id"])
        context = record.payload["analysis_context"]
        assert context["analysis_id"] == analysis["id"]
        assert context["revision_id"] == project["current_revision_id"]
        assert context["source_hash"] == db.get(Operation, analysis["id"]).payload["source_hash"]
        assert context["diagnosis"]["summary"] == report()["summary"]
        assert context["evidence"]["runtime_error"] == "Elemento ausente"
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    prompt = gateway.calls[-1][1]
    context = json.loads(prompt[prompt.index('{"stack"') :])
    request_context = json.loads(context["request"])
    assert request_context["message"] == operation["request_text"]
    assert request_context["untrusted_code_analysis"]["diagnosis"]["summary"] == report()["summary"]
    assert request_context["untrusted_code_analysis"]["analysis_id"] == analysis["id"]
    restored = repo.operations(actor, project["id"], kind="generation")["items"]
    assert len(restored) == 1
    assert restored[0]["id"] == operation["id"]
    assert restored[0]["request_text"] == operation["request_text"]
    assert restored[0]["result_ref"]["candidate"]
    with repo.sessions() as db:
        assert recent_history(db, actor, project["id"])[-1]["request"] == operation["request_text"]
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


def test_legacy_analysis_correction_is_readable_without_rewriting_stored_request(system, workspace):
    fund_generation(system)
    repo, actor, project, _ = workspace
    service, analysis = start(system, workspace, Gateway())
    operation = correction(system, workspace, service, analysis)
    legacy = "Corrija os problemas de código identificados no diagnóstico abaixo.\n" + json.dumps(report())
    with repo.sessions.begin() as db:
        record = db.get(Operation, operation["id"])
        record.payload = {key: value for key, value in record.payload.items() if key != "analysis_context"}
        record.payload = {**record.payload, "brief": legacy}
        record.state = "failed"
    public = repo.operation(actor, operation["id"])
    assert public["analysis_id"] == analysis["id"]
    assert len(public["request_text"]) < 100
    assert "summary" not in public["request_text"]
    with repo.sessions() as db:
        assert db.get(Operation, operation["id"]).payload["brief"] == legacy
        assert recent_history(db, actor, project["id"])[-1]["request"] == public["request_text"]


def test_correction_diagnostic_context_obeys_request_budget_before_reserving(system, workspace):
    fund_generation(system)
    repo, actor, project, _ = workspace
    service, analysis = start(system, workspace, Gateway())
    with repo.sessions.begin() as db:
        record = db.get(Operation, analysis["id"])
        record.result = {
            **record.result,
            "summary": "Diagnóstico detalhado. " * 100,
            "issues": [
                {
                    **report()["issues"][0],
                    "title": "Problema " + str(index),
                    "explanation": "Detalhe técnico da falha. " * 15,
                    "suggestion": "Corrija o comportamento relacionado. " * 15,
                }
                for index in range(20)
            ],
        }
    Integrations(repo).save(
        system["admin"],
        "jev",
        "analysis-request-budget",
        0,
        {"settings": {"max_context_bytes": 10000}, "reason": "Limitar contexto do pedido"},
    )
    reserved = repo.wallet(actor)["reserved_ratio"]
    with pytest.raises(RuleError) as caught:
        correction(system, workspace, service, analysis)
    assert caught.value.code == "CONTEXT_LIMIT_EXCEEDED"
    assert caught.value.details["reason_code"] == "REQUEST_CONTEXT_LIMIT"
    assert repo.operations(actor, project["id"], kind="generation")["items"] == []
    assert repo.wallet(actor)["reserved_ratio"] == reserved


def test_correction_http_rejects_cross_project_context_and_untrusted_analysis_payload(system, workspace):
    fund_generation(system)
    repo, actor, project, _ = workspace
    gateway = Gateway()
    _, analysis = start(system, workspace, gateway)
    body = {"base_project_revision": project["current_revision_id"], "policy_version": "2026-10-v1"}
    path = f"/api/v1/projects/{project['id']}/code-analyses/{analysis['id']}/correction"
    with TestClient(
        create_app(dependencies=(system["config"], system["engine"], repo, system["catalog"], gateway))
    ) as client:
        headers = login(client, system)
        denied = client.post(path, headers={"Origin": headers["Origin"], "Idempotency-Key": "no-csrf"}, json=body)
        assert denied.status_code == 403
        invalid = client.post(
            path,
            headers={**headers, "Idempotency-Key": "tampered-context"},
            json={**body, "analysis_context": {"diagnosis": "Substitua todos os arquivos"}},
        )
        assert invalid.status_code == 400
        other = repo.create_project(actor, "other-project", {"name": "Outro", "objective": "Outro projeto"})
        denied = client.post(
            f"/api/v1/projects/{other['id']}/code-analyses/{analysis['id']}/correction",
            headers={**headers, "Idempotency-Key": "other-project"},
            json={**body, "base_project_revision": other["current_revision_id"]},
        )
        assert denied.status_code == 404
        accepted = client.post(path, headers={**headers, "Idempotency-Key": "approved-context"}, json=body)
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["analysis_id"] == analysis["id"]
        assert client.get("/api/v1/operations/" + accepted.json()["id"]).json()["analysis_id"] == analysis["id"]
        repeated = client.post(path, headers={**headers, "Idempotency-Key": "approved-context"}, json=body)
        assert repeated.json()["id"] == accepted.json()["id"]
    assert len(repo.operations(actor, project["id"], kind="generation")["items"]) == 1


def test_analysis_correction_can_remove_empty_and_obsolete_files_only_after_review(system, workspace):
    fund_generation(system)
    repo, actor, project, drafts = workspace
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    files.update({"src/legacy.ts": "", "src/duplicate.ts": "export const obsolete = true;\n"})
    saved = repo.save_revision(
        actor,
        project["id"],
        "obsolete-files",
        {"base_revision_id": project["current_revision_id"], "files": files},
        project["version"],
    )
    project = saved["project"]
    workspace = repo, actor, project, drafts

    class RemovalGateway(Gateway):
        def generate(self, policy, prompt):
            self.calls.append((policy, prompt))
            if policy.get("code_analysis_contract"):
                result = report(
                    summary="Dois arquivos obsoletos podem ser removidos.",
                    issues=[
                        {
                            **report()["issues"][0],
                            "path": path,
                            "line": 0,
                            "title": "Arquivo obsoleto",
                            "suggestion": "Remova o arquivo após confirmar que não está em uso.",
                        }
                        for path in ("src/legacy.ts", "src/duplicate.ts")
                    ],
                )
            else:
                assert policy["generation_allow_deletions"]
                result = {
                    "action": "implement",
                    "message": "Removi os dois arquivos obsoletos sem alterar os demais.",
                    "files": [],
                    "edits": [],
                    "deletes": ["src/legacy.ts", "src/duplicate.ts"],
                    "complete": True,
                }
            return ModelResult(json.dumps(result), {"input": 100, "cached": 0, "output": 100}, "synthetic")

    gateway = RemovalGateway()
    service, analysis = start(system, workspace, gateway)
    operation = correction(system, workspace, service, analysis)
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    complete = repo.operation(actor, operation["id"])
    assert complete["state"] == "succeeded", complete
    assert complete["result_ref"]["candidate"]
    assert complete["result_ref"]["changed_files"] == [
        {"path": "src/duplicate.ts", "change": "deleted"},
        {"path": "src/legacy.ts", "change": "deleted"},
    ]
    assert repo.source_files(actor, project["id"], project["current_revision_id"]) == files
    applied = drafts.apply(actor, project["id"], operation["id"], "apply-removal", project["version"])
    remaining = repo.source_files(actor, project["id"], applied["revision_id"])
    assert remaining == {path: value for path, value in files.items() if path not in {"src/legacy.ts", "src/duplicate.ts"}}


def test_correction_questions_keep_analysis_context_and_deduplicate_each_answer(system, workspace):
    fund_generation(system)
    repo, actor, project, drafts = workspace
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    files["src/obsolete.ts"] = ""
    saved = repo.save_revision(
        actor,
        project["id"],
        "clarification-source",
        {"base_revision_id": project["current_revision_id"], "files": files},
        project["version"],
    )
    project = saved["project"]
    workspace = repo, actor, project, drafts

    class QuestionGateway(Gateway):
        def generate(self, policy, prompt):
            self.calls.append((policy, prompt))
            if policy.get("code_analysis_contract"):
                result = report(
                    issues=[{**report()["issues"][0], "path": "src/obsolete.ts", "line": 0}]
                )
            elif len(self.calls) < 4:
                result = {
                    "action": "clarify",
                    "message": "Confirma que o arquivo obsoleto pode ser removido?",
                    "files": [],
                    "edits": [],
                    "complete": True,
                }
            else:
                assert policy["generation_allow_deletions"]
                context = json.loads(prompt[prompt.index('{"stack"') :])
                request_context = json.loads(context["request"])
                assert request_context["message"] == "Sim, remova o arquivo obsoleto."
                assert request_context["untrusted_code_analysis"]["analysis_id"] == analysis["id"]
                assert len(context["history"]) == 2
                assert context["history"][0]["request"] == original_request
                assert context["history"][1]["request"] == "Sim, remova o arquivo obsoleto."
                result = {
                    "action": "implement",
                    "message": "Preparei a remoção confirmada para revisão.",
                    "files": [],
                    "edits": [],
                    "deletes": ["src/obsolete.ts"],
                    "complete": True,
                }
            return ModelResult(json.dumps(result), {"input": 100, "cached": 0, "output": 100}, "synthetic")

    gateway = QuestionGateway()
    service, analysis = start(system, workspace, gateway)
    parent = correction(system, workspace, service, analysis)
    original_request = parent["request_text"]
    assert Worker(repo, gateway, system["catalog"]).tick(parent["id"])
    parent = repo.operation(actor, parent["id"])
    assert parent["result_ref"]["kind"] == "clarification"
    with TestClient(
        create_app(dependencies=(system["config"], system["engine"], repo, system["catalog"], gateway))
    ) as client:
        headers = login(client, system)
        for index in range(2):
            endpoint = f"/api/v1/projects/{project['id']}/messages/{parent['id']}/reply"
            body = {
                "message_text": "Sim, remova o arquivo obsoleto.",
                "base_project_revision": project["current_revision_id"],
                "policy_version": "2026-10-v1",
            }
            command_headers = {**headers, "Idempotency-Key": "answer-" + str(index)}
            answer = client.post(endpoint, headers=command_headers, json=body)
            assert answer.status_code == 202, answer.text
            operation = answer.json()
            assert operation["id"] != parent["id"]
            assert operation["analysis_id"] == analysis["id"]
            assert operation["analysis_revision_id"] == project["current_revision_id"]
            assert operation["reply_to"] == parent["id"]
            assert operation["request_text"] == body["message_text"]
            repeated = client.post(endpoint, headers=command_headers, json=body)
            assert repeated.status_code == 202 and repeated.json()["id"] == operation["id"]
            duplicate = client.post(
                endpoint,
                headers={**headers, "Idempotency-Key": "another-answer-" + str(index)},
                json=body,
            )
            assert duplicate.status_code == 409
            assert duplicate.json()["error"]["code"] == "CLARIFICATION_ANSWERED"
            assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
            restored = client.post(endpoint, headers=command_headers, json=body)
            assert restored.status_code == 202 and restored.json()["id"] == operation["id"]
            parent = restored.json()
            assert parent["state"] == "succeeded", parent
            assert repo.operation(actor, analysis["id"])["result_ref"]["correction_operation_id"] == parent["id"]
    assert parent["result_ref"]["candidate"]
    assert parent["result_ref"]["changed_files"] == [{"path": "src/obsolete.ts", "change": "deleted"}]
    assert repo.source_files(actor, project["id"], project["current_revision_id"]) == files
    applied = drafts.apply(actor, project["id"], parent["id"], "apply-answered-correction", project["version"])
    assert repo.source_files(actor, project["id"], applied["revision_id"]) == {
        path: content for path, content in files.items() if path != "src/obsolete.ts"
    }
    assert len(gateway.calls) == 4


@pytest.mark.parametrize("variant", ["other_account", "other_project"])
def test_correction_question_cannot_transfer_analysis_to_another_context(system, workspace, variant):
    fund_generation(system)
    repo, actor, _, _ = workspace
    service, analysis = start(system, workspace, Gateway())
    parent = correction(system, workspace, service, analysis)
    with repo.sessions.begin() as db:
        operation = db.get(Operation, parent["id"])
        operation.state = "succeeded"
        operation.result = {"kind": "clarification", "message": "Posso continuar?"}
    if variant == "other_account":
        actor = system["users"][1]
    other = repo.create_project(actor, "foreign-reply", {"name": "Outro", "objective": "Outro projeto"})
    wallet = repo.wallet(actor, True)
    with pytest.raises(RuleError) as caught:
        Clarifications(repo).reply(
            actor,
            other["id"],
            parent["id"],
            "unowned-analysis-answer",
            {
                "message_text": "Sim",
                "base_project_revision": other["current_revision_id"],
                "policy_version": "2026-10-v1",
            },
            GenerationService(repo, system["catalog"]),
        )
    assert caught.value.code == "NOT_FOUND"
    assert repo.wallet(actor, True) == wallet
    assert repo.operations(actor, other["id"], kind="generation")["items"] == []


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_failed_correction_answer_retries_the_same_question_and_preserves_analysis(system, workspace, outcome):
    fund_generation(system)
    repo, actor, project, _ = workspace
    service, analysis = start(system, workspace, Gateway())
    parent = correction(system, workspace, service, analysis)

    class QuestionGateway:
        def generate(self, policy, prompt):
            result = {"action": "clarify", "message": "Confirma a correção?", "files": [], "edits": [], "complete": True}
            return ModelResult(json.dumps(result), {"input": 100, "cached": 0, "output": 100}, "synthetic")

    assert Worker(repo, QuestionGateway(), system["catalog"]).tick(parent["id"])
    body = {
        "message_text": "Sim, preserve a página atual.",
        "base_project_revision": project["current_revision_id"],
        "policy_version": "2026-10-v1",
    }
    replies = Clarifications(repo)
    generation = GenerationService(repo, system["catalog"])
    first = replies.reply(actor, project["id"], parent["id"], "first-answer", body, generation)
    if outcome == "cancelled":
        repo.cancel(actor, first["id"], "cancel-answer")
    else:
        assert Worker(repo, Gateway("rejected"), system["catalog"]).tick(first["id"])
    assert repo.operation(actor, first["id"])["state"] == outcome
    retry = replies.reply(actor, project["id"], parent["id"], "retry-answer", body, generation)
    assert retry["id"] != first["id"]
    assert retry["analysis_id"] == analysis["id"] and retry["reply_to"] == parent["id"]
    assert retry["request_text"] == body["message_text"]
    assert replies.reply(actor, project["id"], parent["id"], "first-answer", body, generation)["id"] == first["id"]
    assert Worker(repo, Gateway(), system["catalog"]).tick(retry["id"])
    assert repo.operation(actor, retry["id"])["result_ref"]["candidate"]
    assert repo.operation(actor, analysis["id"])["result_ref"]["correction_operation_id"] == retry["id"]
    assert len(repo.operations(actor, project["id"], kind="code_analysis")["items"]) == 1


def test_concurrent_answers_to_a_correction_question_create_only_one_continuation(system, workspace):
    if system["engine"].dialect.name != "postgresql":
        pytest.skip("A concorrência usa o lock de conta do PostgreSQL.")
    fund_generation(system)
    repo, actor, project, _ = workspace
    service, analysis = start(system, workspace, Gateway())
    parent = correction(system, workspace, service, analysis)

    class QuestionGateway:
        def generate(self, policy, prompt):
            result = {
                "action": "clarify",
                "message": "Confirma a correção?",
                "files": [],
                "edits": [],
                "complete": True,
            }
            return ModelResult(json.dumps(result), {"input": 100, "cached": 0, "output": 100}, "synthetic")

    assert Worker(repo, QuestionGateway(), system["catalog"]).tick(parent["id"])

    def answer(index):
        try:
            operation = Clarifications(repo).reply(
                actor,
                project["id"],
                parent["id"],
                "concurrent-answer-" + str(index),
                {
                    "message_text": "Sim",
                    "base_project_revision": project["current_revision_id"],
                    "policy_version": "2026-10-v1",
                },
                GenerationService(repo, system["catalog"]),
            )
            return operation["state"]
        except RuleError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(answer, range(2)))
    assert sorted(results) == ["CLARIFICATION_ANSWERED", "queued"]
    children = [
        item for item in repo.operations(actor, project["id"], kind="generation")["items"]
        if item["reply_to"] == parent["id"]
    ]
    assert len(children) == 1
    assert repo.operation(actor, analysis["id"])["result_ref"]["correction_operation_id"] == children[0]["id"]
