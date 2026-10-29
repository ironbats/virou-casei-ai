import json
from hashlib import sha256

import pytest
from sqlalchemy import func, select

from viracase.application.generation import GenerationService
from viracase.application.ports import ModelResult
from viracase.domain.migration_history import migration_manifest, validate_migration_history
from viracase.domain.rules import RuleError
from viracase.infrastructure.drafts import Drafts
from viracase.infrastructure.models import (
    Draft,
    IntegrationConfig,
    Operation,
    PublicationResource,
    Revision,
    new_id,
)
from viracase.infrastructure.proposal_review import ProposalReviews
from viracase.infrastructure.publication_migration_context import (
    migration_validation_for_revision,
    published_migration_context,
)
from viracase.infrastructure.worker import Worker


@pytest.fixture
def workspace(system):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor, "history-project", {"name": "History", "objective": "Aplicação de teste"}
    )
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    return repo, actor, project, files


def save(workspace, files, key="save", **payload):
    repo, actor, project, _ = workspace
    return repo.save_revision(
        actor,
        project["id"],
        key,
        {"base_revision_id": project["current_revision_id"], "files": files, **payload},
        project["version"],
    )


@pytest.mark.parametrize(
    "change", ["edit", "empty", "delete", "rename", "whitespace", "alias", "backslash", "dot"]
)
def test_pure_policy_rejects_history_mutations(change):
    baseline = {"migrations/0001.sql": "CREATE TABLE items (id INTEGER);\n"}
    files = dict(baseline)
    if change in {"edit", "empty", "whitespace"}:
        files["migrations/0001.sql"] = (
            ""
            if change == "empty"
            else baseline["migrations/0001.sql"]
            + (" " if change == "whitespace" else "CREATE TABLE other (id INTEGER);")
        )
    elif change in {"delete", "rename"}:
        del files["migrations/0001.sql"]
        if change == "rename":
            files["migrations/0002.sql"] = baseline["migrations/0001.sql"]
    else:
        path = {
            "alias": "Migrations/0001.sql",
            "backslash": "migrations\\0001.sql",
            "dot": "migrations/./0001.sql",
        }[change]
        files[path] = baseline["migrations/0001.sql"]
    with pytest.raises(RuleError) as caught:
        validate_migration_history(baseline, files, base_revision_id="base", operation="edit")
    assert caught.value.code == "MIGRATION_HISTORY_IMMUTABLE"
    details = caught.value.details["migration_history"]
    assert details["base_revision_id"] == "base" and details["operation"] == "edit"
    assert "CREATE TABLE" not in json.dumps(details)


def test_incremental_order_matches_existing_lexicographic_publication_order():
    baseline = {"migrations/9999.sql": "CREATE TABLE a (id INTEGER);"}
    with pytest.raises(RuleError) as caught:
        validate_migration_history(
            baseline, {**baseline, "migrations/10000.sql": "CREATE TABLE b (id INTEGER);"}
        )
    assert caught.value.code == "MIGRATION_ORDER_INVALID"
    result = validate_migration_history(
        baseline, {**baseline, "migrations/9999_0001.sql": "CREATE TABLE b (id INTEGER);"}
    )
    assert result["added"][0]["path"] == "migrations/9999_0001.sql"


def test_manual_save_rejected_before_sources_or_revision_change(workspace):
    repo, actor, project, files = workspace
    with pytest.raises(RuleError) as caught:
        save(workspace, {**files, "migrations/0001.sql": files["migrations/0001.sql"] + "\n"})
    assert caught.value.code == "MIGRATION_HISTORY_IMMUTABLE"
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    assert repo.source_files(actor, project["id"], project["current_revision_id"]) == files
    with repo.sessions() as db:
        assert (
            db.scalar(select(func.count()).select_from(Revision).where(Revision.project_id == project["id"]))
            == 1
        )
    assert (
        len(
            list(
                repo.sources.path(actor, project["id"], project["current_revision_id"]).parent.glob("*.json")
            )
        )
        == 1
    )


def test_add_revision_cannot_erase_the_baseline_by_omitting_its_identity(workspace):
    repo, actor, project, files = workspace
    with repo.sessions.begin() as db:
        current = repo.owned_project(db, actor, project["id"])
        with pytest.raises(RuleError) as caught:
            repo.add_revision(
                db,
                current,
                {**files, "migrations/0001.sql": files["migrations/0001.sql"] + "\n"},
                "generation",
                None,
                proposal=True,
            )
        assert caught.value.code == "VERSION_CONFLICT"
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


def test_unapplied_candidate_is_never_a_new_consolidated_baseline(workspace):
    repo, actor, project, files = workspace
    with repo.sessions.begin() as db:
        current = repo.owned_project(db, actor, project["id"])
        proposal = repo.add_revision(
            db,
            current,
            {**files, "migrations/0002.sql": "CREATE TABLE a (id INTEGER);"},
            "generation",
            project["current_revision_id"],
            proposal=True,
        )
        with pytest.raises(RuleError) as caught:
            repo.add_revision(db, current, files, "generation", proposal.id, proposal=True)
        assert caught.value.code == "VERSION_CONFLICT"
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


def test_new_migration_can_be_drafted_then_is_protected_at_consolidation(workspace):
    repo, actor, project, files = workspace
    drafts = Drafts(repo, None)
    draft = drafts.save(
        actor,
        "project",
        project["id"],
        "first-draft",
        {
            "base_revision_id": project["current_revision_id"],
            "files": {**files, "migrations/0002.sql": "INVALID DRAFT"},
        },
        0,
    )
    valid = {**files, "migrations/0002.sql": "CREATE TABLE additional_data (id INTEGER);"}
    drafts.save(
        actor,
        "project",
        project["id"],
        "second-draft",
        {"base_revision_id": project["current_revision_id"], "files": valid},
        draft["version"],
    )
    saved = save(workspace, valid)
    assert saved["migration_history"]["added"][0]["path"] == "migrations/0002.sql"
    with repo.sessions() as db:
        evidence = migration_validation_for_revision(db, actor, saved["revision"]["id"])
        assert evidence and evidence["revision_id"] == saved["revision"]["id"]
        assert migration_validation_for_revision(db, "another-account", saved["revision"]["id"]) is None
    with pytest.raises(RuleError) as caught:
        save(
            (repo, actor, saved["project"], valid),
            {**valid, "migrations/0002.sql": valid["migrations/0002.sql"] + "\n"},
            "change-history",
        )
    assert caught.value.code == "MIGRATION_HISTORY_IMMUTABLE"


def test_restore_cannot_remove_later_migrations(workspace):
    repo, actor, project, files = workspace
    expanded = {**files, "migrations/0002.sql": "CREATE TABLE additional_data (id INTEGER);"}
    saved = save(workspace, expanded)
    with pytest.raises(RuleError) as caught:
        save(
            (repo, actor, saved["project"], expanded),
            None,
            "restore",
            restore_revision_id=project["current_revision_id"],
        )
    assert caught.value.code == "MIGRATION_HISTORY_IMMUTABLE"
    assert repo.project(actor, project["id"])["current_revision_id"] == saved["revision"]["id"]


def test_code_only_revision_reuses_base_validation_and_keeps_approved_backfill(workspace, monkeypatch):
    from viracase.infrastructure import publication_migration_validation

    repo, actor, project, files = workspace
    expanded = {
        **files,
        "migrations/0002.sql": (
            "ALTER TABLE project_settings ADD COLUMN display_title TEXT;\n"
            "UPDATE project_settings SET display_title = 'Projeto' WHERE display_title IS NULL;"
        ),
    }
    saved = save(workspace, expanded)
    before = saved["migration_validation"]
    assert before["approved_backfills"][0]["path"] == "migrations/0002.sql"

    def unexpected_validation(*args, **kwargs):
        raise AssertionError("Uma edição de código não deve reconstruir o schema validado.")

    monkeypatch.setattr(publication_migration_validation, "_validate_cached", unexpected_validation)
    updated = save(
        (repo, actor, saved["project"], expanded),
        {**expanded, "public/revision.txt": "Código atualizado."},
        "code-only",
    )
    after = updated["migration_validation"]
    assert after["approved_backfills"] == before["approved_backfills"]
    assert after["schema_sha256"] == before["schema_sha256"]
    assert after["manifest"] == before["manifest"]
    assert after["baseline_manifest"] == after["manifest"]
    assert after["artifact_sha256"] != before["artifact_sha256"]
    assert after["revision_id"] == updated["revision"]["id"]
    with repo.sessions() as db:
        assert migration_validation_for_revision(db, actor, updated["revision"]["id"]) == after


def test_unpublished_context_freezes_saved_migrations_and_does_not_enroll_draft(workspace):
    repo, actor, project, files = workspace
    with repo.sessions() as db:
        p = repo.owned_project(db, actor, project["id"])
        context = published_migration_context(repo, db, p, {**files, "migrations/0002.sql": "draft"})
    assert context["base_revision_id"] == project["current_revision_id"]
    assert context["protected_files"] == migration_manifest(files)
    assert context["files"] == []


class BatchGateway:
    def __init__(self, responses):
        self.responses, self.contexts = responses, []

    def generate(self, policy, prompt):
        self.contexts.append(json.loads(prompt[prompt.index('{"stack"') :]))
        body = self.responses[min(len(self.contexts) - 1, len(self.responses) - 1)]
        return ModelResult(
            json.dumps(body), {"input": 100, "cached": 0, "output": 100}, f"batch-{len(self.contexts)}"
        )


def turn(files, complete=True, message="Alterações propostas."):
    return {
        "action": "implement",
        "message": message,
        "files": [{"path": path, "content": content} for path, content in files.items()],
        "edits": [],
        "complete": complete,
    }


def generate(system, workspace, gateway, contract=4):
    from test_conversation_runtime import fund_generation

    fund_generation(system)
    repo, actor, project, _ = workspace
    op = GenerationService(repo, system["catalog"]).start(
        actor,
        project["id"],
        new_id(),
        {
            "brief": "Evolua a aplicação e preserve as migrations.",
            "base_revision_id": project["current_revision_id"],
            "policy_version": "2026-10-v1",
            "review_required": True,
        },
        contract_version=contract,
    )
    assert Worker(repo, gateway, system["catalog"]).tick(op["id"])
    return repo.operation(actor, op["id"])


def test_each_batch_preserves_frozen_baseline_and_keeps_accepted_work(system, workspace):
    repo, actor, project, files = workspace
    bad = files["migrations/0001.sql"] + "CREATE TABLE forbidden (id INTEGER);"
    gateway = BatchGateway(
        [
            turn({"public/accepted.txt": "accepted"}, complete=False),
            turn({"migrations/0001.sql": bad}, complete=False),
            turn({"migrations/0002.sql": "CREATE TABLE additional_data (id INTEGER);"}),
        ]
    )
    result = generate(system, workspace, gateway)
    assert result["state"] == "succeeded", result
    assert len(gateway.contexts) == 3
    assert gateway.contexts[2]["files"]["migrations/0001.sql"] == files["migrations/0001.sql"]
    assert gateway.contexts[2]["files"]["public/accepted.txt"] == "accepted"
    assert "MIGRATION_HISTORY_IMMUTABLE" in gateway.contexts[2]["validation_feedback"]
    candidate = repo.source_files(actor, project["id"], result["result_ref"]["revision_id"])
    assert candidate["migrations/0001.sql"] == files["migrations/0001.sql"]
    review = ProposalReviews(repo).get(actor, project["id"], result["id"])
    assert review["migration_history"]["preserved"] == migration_manifest(files)
    assert review["migration_history"]["added"][0]["path"] == "migrations/0002.sql"
    applied = Drafts(repo, None).apply(actor, project["id"], result["id"], "apply", project["version"])
    again = Drafts(repo, None).apply(actor, project["id"], result["id"], "apply-again", project["version"])
    assert applied == again
    assert (
        repo.source_files(actor, project["id"], applied["revision_id"])["public/accepted.txt"] == "accepted"
    )


def test_persistent_violation_fails_without_a_candidate_or_new_baseline(system, workspace):
    repo, actor, project, files = workspace
    gateway = BatchGateway(
        [turn({"migrations/0001.sql": files["migrations/0001.sql"] + "\n"}, complete=False)]
    )
    result = generate(system, workspace, gateway)
    assert result["state"] == "failed" and result["error_code"] == "MIGRATION_HISTORY_IMMUTABLE"
    assert len(gateway.contexts) == 3
    assert "revision_id" not in result["result_ref"]
    assert (
        result["result_ref"]["migration_failure"]["migration_history"]["base_revision_id"]
        == project["current_revision_id"]
    )
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


def test_new_migration_remains_editable_between_batches_until_proposal_is_applied(system, workspace):
    repo, actor, project, _ = workspace
    gateway = BatchGateway(
        [
            turn({"migrations/0002.sql": "CREATE TABLE draft ("}, complete=False),
            turn({"migrations/0002.sql": "CREATE TABLE additional_data (id INTEGER);"}),
        ]
    )
    result = generate(system, workspace, gateway)
    assert result["state"] == "succeeded", result
    assert "migrations/0002.sql" not in {
        item["path"] for item in gateway.contexts[1]["published_database_migrations"]["protected_files"]
    }
    review = ProposalReviews(repo).get(actor, project["id"], result["id"])
    assert (
        review["migration_history"]["added"][0]["sha256"]
        == sha256(b"CREATE TABLE additional_data (id INTEGER);").hexdigest()
    )


def test_delete_then_recreate_cannot_remove_protection_across_correction_batches(system):
    from test_correction_recovery import prepared_correction, response

    repo, actor, project, files, _, operation, _ = prepared_correction(system)
    gateway = BatchGateway(
        [
            response(deletes=["migrations/0001.sql"], complete=False),
            response(
                files=[{"path": "migrations/0001.sql", "content": files["migrations/0001.sql"] + "\n"}],
                complete=False,
            ),
            response(
                files=[{"path": "migrations/0002.sql", "content": "CREATE TABLE appended (id INTEGER);"}]
            ),
        ]
    )
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    result = repo.operation(actor, operation["id"])
    assert result["state"] == "succeeded", result
    assert all(
        context["files"]["migrations/0001.sql"] == files["migrations/0001.sql"]
        for context in gateway.contexts
    )
    assert all("migrations/0001.sql" not in context["accepted_deletions"] for context in gateway.contexts)
    assert all(
        "MIGRATION_HISTORY_IMMUTABLE" in context["validation_feedback"] for context in gateway.contexts[1:]
    )
    candidate = repo.source_files(actor, project["id"], result["result_ref"]["revision_id"])
    assert candidate["migrations/0001.sql"] == files["migrations/0001.sql"]


def test_contract_three_batches_cannot_modify_migrations_before_completion(system, workspace):
    repo, actor, project, files = workspace
    gateway = BatchGateway(
        [
            {
                "files": [{"path": "migrations/0001.sql", "content": files["migrations/0001.sql"] + "\n"}],
                "edits": [],
                "complete": False,
            },
            {
                "files": [{"path": "migrations/0002.sql", "content": "CREATE TABLE appended (id INTEGER);"}],
                "edits": [],
                "complete": True,
            },
        ]
    )
    result = generate(system, workspace, gateway, contract=3)
    assert result["state"] == "succeeded", result
    assert gateway.contexts[1]["files"]["migrations/0001.sql"] == files["migrations/0001.sql"]
    assert "MIGRATION_HISTORY_IMMUTABLE" in gateway.contexts[1]["validation_feedback"]
    assert gateway.contexts[1]["protected_migrations"] == migration_manifest(files)
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


def test_concurrent_revision_blocks_apply_even_when_migrations_are_valid(system, workspace):
    repo, actor, project, files = workspace
    result = generate(
        system, workspace, BatchGateway([turn({"migrations/0002.sql": "CREATE TABLE a (id INTEGER);"})])
    )
    assert result["state"] == "succeeded"
    saved = save(workspace, {**files, "public/manual.txt": "newer"})
    with pytest.raises(RuleError) as caught:
        Drafts(repo, None).apply(actor, project["id"], result["id"], "stale", project["version"])
    assert caught.value.code == "VERSION_CONFLICT"
    assert repo.project(actor, project["id"])["current_revision_id"] == saved["revision"]["id"]


def test_legacy_candidate_cannot_bypass_append_only_on_apply(workspace):
    repo, actor, project, files = workspace
    tampered = {**files, "migrations/0001.sql": files["migrations/0001.sql"] + "\n"}
    rid, oid = new_id(), new_id()
    digest = repo.sources.write(actor, project["id"], rid, tampered, project["stack"])
    with repo.sessions.begin() as db:
        db.add(
            Revision(
                id=rid,
                account_id=actor,
                project_id=project["id"],
                source_hash=digest,
                manifest=[
                    {
                        "id": sha256(path.encode()).hexdigest()[:24],
                        "path": path,
                        "size": len(content.encode()),
                    }
                    for path, content in sorted(tampered.items())
                ],
                origin="generation",
                candidate=True,
            )
        )
        db.add(
            Operation(
                id=oid,
                account_id=actor,
                project_id=project["id"],
                type="generation",
                state="succeeded",
                payload={"base_revision_id": project["current_revision_id"]},
                result={"candidate": True, "revision_id": rid},
            )
        )
    with pytest.raises(RuleError) as caught:
        Drafts(repo, None).apply(actor, project["id"], oid, "apply-legacy", project["version"])
    assert caught.value.code == "MIGRATION_HISTORY_IMMUTABLE"
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


def test_baseline_receipt_divergence_has_distinct_diagnosis_without_rewriting_history(workspace):
    repo, actor, project, files = workspace
    with repo.sessions.begin() as db:
        configuration = IntegrationConfig(
            id=new_id(), provider="cloudflare", version=1, settings={}, created_by=actor
        )
        db.add(configuration)
        db.flush()
        db.add(
            PublicationResource(
                account_id=actor,
                project_id=project["id"],
                config_id=configuration.id,
                remote_account_id="synthetic",
                worker_name="synthetic-worker",
                inventory={"migrations": {"migrations/0001.sql": "a" * 64}},
            )
        )
    with pytest.raises(RuleError) as caught:
        save(workspace, {**files, "public/new.txt": "new"})
    assert caught.value.code == "MIGRATION_BASELINE_INCONSISTENT"
    assert caught.value.details["base_revision_id"] == project["current_revision_id"]
    with repo.sessions() as db:
        resource = db.get(PublicationResource, project["id"])
        assert resource.inventory["migrations"]["migrations/0001.sql"] == "a" * 64
        row = db.scalar(
            select(Draft).where(
                Draft.kind == "migration_plan", Draft.target == project["current_revision_id"]
            )
        )
        assert row is not None
