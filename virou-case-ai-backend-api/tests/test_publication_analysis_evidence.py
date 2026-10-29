import json

import pytest
from test_code_analysis import Gateway, request, start
from test_code_analysis import workspace as workspace_fixture

from viracase.domain.rules import RuleError
from viracase.infrastructure.code_analysis import CodeAnalysis
from viracase.infrastructure.models import Operation
from viracase.infrastructure.publication_migrations import LEDGER
from viracase.infrastructure.publication_sql_review import ANALYSIS_METHOD, sql_review_findings


@pytest.fixture
def workspace(system):
    return workspace_fixture.__wrapped__(system)


@pytest.mark.parametrize(
    "state,stage,code",
    [
        ("failed", "publication_failed", "CONFIGURATION_INVALID"),
        ("failed", "publication_failed", "PUBLICATION_FRONTEND_NOT_READY"),
        ("failed", "publication_failed", "MIGRATION_RESULT_UNKNOWN"),
        ("running", "publication_verifying", None),
        ("reconciling", "publication_reconciling", "PUBLICATION_RESULT_UNKNOWN"),
    ],
)
def test_any_publication_state_can_supply_analysis_evidence_without_a_build_failure(
    system, workspace, state, stage, code
):
    repo, actor, project, _ = workspace
    before = repo.source_files(actor, project["id"], project["current_revision_id"])
    with repo.sessions.begin() as db:
        operation = Operation(
            account_id=actor,
            project_id=project["id"],
            type="publication",
            state=state,
            progress_stage=stage,
            error_code=code,
            payload={"revision_id": project["current_revision_id"]},
            result={},
        )
        db.add(operation)
        db.flush()
        publication_id = operation.id
    gateway = Gateway()
    _, analysis = start(
        system, workspace, gateway, source="publication", publication_operation_id=publication_id
    )
    assert analysis["state"] == "succeeded"
    with repo.sessions() as db:
        evidence = db.get(Operation, analysis["id"]).payload["evidence"]["publication"]
        assert evidence["state"] == state and evidence["stage"] == stage
        assert evidence["category"] == "publication" and evidence["message"]
        assert evidence["matches_revision"] is True
        assert evidence["code"] == (code or "")
        assert db.get(Operation, publication_id).state == state
    assert repo.source_files(actor, project["id"], project["current_revision_id"]) == before


def test_sql_review_location_and_rules_reach_ai_without_untrusted_sql_diagnostics(system, workspace):
    repo, actor, project, _ = workspace
    finding = sql_review_findings("-- change\nDELETE FROM items;", LEDGER)[0]
    with repo.sessions.begin() as db:
        publication = Operation(
            account_id=actor,
            project_id=project["id"],
            type="publication",
            state="failed",
            progress_stage="publication_failed",
            error_code="MIGRATION_REVIEW_REQUIRED",
            payload={"revision_id": project["current_revision_id"]},
            result={
                "publication_failure": {
                    "stage": "publication_migrating",
                    "message": "Migration exige revisão.",
                    "migration": {
                        "code": "MIGRATION_REVIEW_REQUIRED",
                        "analysis_method": ANALYSIS_METHOD,
                        "issues": [
                            {
                                "code": "MIGRATION_REVIEW_REQUIRED",
                                "reason": "unsafe_sql",
                                "receipt_source": "candidate",
                                "path": "migrations/0002.sql",
                                **finding,
                                "sql": "private-diagnostic-sql",
                                "explanation": "private-diagnostic-secret",
                            }
                        ],
                    },
                }
            },
        )
        db.add(publication)
        db.flush()
        publication_id = publication.id
    gateway = Gateway()
    _, analysis = start(
        system, workspace, gateway, source="publication", publication_operation_id=publication_id
    )
    assert analysis["state"] == "succeeded"
    prompt = gateway.calls[0][1]
    assert "private-diagnostic" not in prompt
    context = json.loads(prompt[prompt.index('{"stack"') :])
    publication = context["evidence"]["publication"]
    assert publication["category"] == "migration" and publication["stage"] == "publication_migrating"
    diagnostic = publication["migration"]
    assert diagnostic["analysis_method"] == ANALYSIS_METHOD
    issue = diagnostic["issues"][0]
    assert issue["line"] == 2 and issue["operation"] == "DELETE" and issue["rule"] == "delete_rows"
    assert "ON DELETE e ON UPDATE" in prompt and "não uma conclusão de IA" in prompt


@pytest.mark.parametrize("different_owner", [False, True])
def test_publication_evidence_cannot_cross_project_or_account(system, workspace, different_owner):
    repo, actor, project, _ = workspace
    owner = system["users"][1] if different_owner else actor
    other = repo.create_project(owner, "other-analysis", {"name": "Outro", "objective": "Projeto separado"})
    with repo.sessions.begin() as db:
        operation = Operation(
            account_id=owner,
            project_id=other["id"],
            type="publication",
            state="failed",
            payload={"revision_id": other["current_revision_id"]},
            result={"publication_failure": {"message": "private-other-project"}},
        )
        db.add(operation)
        db.flush()
        identity = operation.id
    with pytest.raises(RuleError) as caught:
        CodeAnalysis(repo, system["catalog"]).start(
            actor,
            project["id"],
            "isolation",
            request(project, source="publication", publication_operation_id=identity),
        )
    assert caught.value.code == "NOT_FOUND"
    assert "private-other-project" not in caught.value.message
    assert repo.operations(actor, project["id"], kind="code_analysis")["items"] == []


def test_readiness_problem_without_publication_operation_can_be_analyzed(system, workspace):
    repo, _, _, _ = workspace
    gateway = Gateway()
    _, analysis = start(
        system,
        workspace,
        gateway,
        source="publication",
        runtime_error="CONFIGURATION_INVALID: Publicação indisponível. Authorization: Bearer private-token",
    )
    assert analysis["state"] == "succeeded"
    with repo.sessions() as db:
        evidence = db.get(Operation, analysis["id"]).payload["evidence"]
        assert "publication" not in evidence and evidence["source"] == "publication"
        assert "CONFIGURATION_INVALID" in evidence["runtime_error"]
        assert "private-token" not in evidence["runtime_error"]
    assert "private-token" not in gateway.calls[0][1]
