from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from viracase.domain.rules import RuleError
from viracase.infrastructure.drafts import Drafts
from viracase.infrastructure.learning import LearningLibrary
from viracase.infrastructure.models import Draft, Operation, Revision, new_id
from viracase.infrastructure.proposal_review import ProposalReviews
from viracase.interfaces.api import create_app


@pytest.fixture
def workspace(system):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(
        actor, "project", {"name": "Workspace", "objective": "Aplicação de teste para revisão"}
    )
    return repo, actor, project, Drafts(repo, LearningLibrary(system["config"].content_root))


@pytest.fixture
def proposal(workspace):
    repo, actor, project, drafts = workspace
    original = repo.source_files(actor, project["id"], project["current_revision_id"])
    original["public/obsolete.txt"] = "Conteúdo antigo\n"
    saved = repo.save_revision(
        actor,
        project["id"],
        "setup-base",
        {"base_revision_id": project["current_revision_id"], "files": original},
        project["version"],
    )
    project = saved["project"]
    changed = {**original, "public/modal.js": 'const label = "Entrar";\n'}
    changed["public/index.html"] += "\n<button>Entrar</button>\n"
    del changed["public/obsolete.txt"]
    identity = new_id()
    with repo.sessions.begin() as db:
        p = repo.owned_project(db, actor, project["id"])
        candidate = repo.add_revision(db, p, changed, "generation", p.current_revision_id, proposal=True)
        db.add(
            Operation(
                id=identity,
                account_id=actor,
                project_id=p.id,
                type="generation",
                state="succeeded",
                progress_stage="proposal_ready",
                payload={"base_revision_id": p.current_revision_id},
                result={"candidate": True, "revision_id": candidate.id, "validation": "structure_checked"},
            )
        )
    return repo, actor, project, drafts, identity, original, changed


def guards(review):
    return {
        "expected_proposal_version": review["proposal_version"],
        "expected_project_revision": review["base_project_revision"],
    }


def test_review_reads_authoritative_manifest_and_lazy_files_without_applying(proposal):
    repo, actor, project, _, oid, original, changed = proposal
    review = ProposalReviews(repo).get(actor, project["id"], oid)
    assert review["state"] == "generated"
    assert {f["path"]: f["change"] for f in review["files"]} == {
        "public/index.html": "modified",
        "public/modal.js": "created",
        "public/obsolete.txt": "deleted",
    }
    assert all("before" not in item and "after" not in item for item in review["files"])
    for item in review["files"]:
        content = ProposalReviews(repo).file(
            actor, project["id"], oid, item["id"], review["proposal_version"]
        )
        assert content["before"] == original.get(item["path"])
        assert content["after"] == changed.get(item["path"])
    assert review["verification_results"][1]["state"] == "not_run"
    assert review["verification_results"][2]["state"] == "not_run"
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


def test_explicit_apply_is_atomic_idempotent_and_preserves_next_prompt(proposal):
    repo, actor, project, drafts, oid, _, changed = proposal
    drafts.save(
        actor,
        "project",
        project["id"],
        "draft",
        {
            "base_revision_id": project["current_revision_id"],
            "files": None,
            "prompt": "Próxima ideia preservada",
        },
        0,
    )
    review = ProposalReviews(repo).get(actor, project["id"], oid)
    result = drafts.apply(actor, project["id"], oid, "apply", project["version"], guards(review))
    assert result == drafts.apply(actor, project["id"], oid, "apply", project["version"], guards(review))
    assert ProposalReviews(repo).get(actor, project["id"], oid)["state"] == "applied"
    assert repo.source_files(actor, project["id"], result["revision_id"]) == changed
    draft = drafts.get(actor, "project", project["id"])
    assert draft["data"]["base_revision_id"] == result["revision_id"]
    assert draft["data"]["prompt"] == "Próxima ideia preservada"
    assert draft["version"] == 2
    with repo.sessions() as db:
        assert (
            db.scalar(select(func.count()).select_from(Revision).where(Revision.project_id == project["id"]))
            == 3
        )


@pytest.mark.parametrize("evidence_state", ["missing", "incompatible", "compatible"])
def test_apply_persists_reusable_server_migration_evidence_atomically(proposal, monkeypatch, evidence_state):
    from viracase.infrastructure import publication_migration_validation

    repo, actor, project, drafts, oid, _, changed = proposal
    review = ProposalReviews(repo).get(actor, project["id"], oid)
    revision_id = review["candidate_revision_id"]
    query = select(Draft).where(
        Draft.account_id == actor, Draft.kind == "migration_plan", Draft.target == revision_id
    )
    with repo.sessions.begin() as db:
        row = db.scalar(query)
        if evidence_state == "missing":
            db.delete(row)
        elif evidence_state == "incompatible":
            row.data = {
                **row.data,
                "migration_validation": {
                    **row.data["migration_validation"], "policy_version": "obsolete-policy"
                },
            }

    def stored_evidence():
        with repo.sessions() as db:
            rows = list(db.scalars(query))
            assert len(rows) <= 1
            return (rows[0].id, rows[0].version, rows[0].data) if rows else None

    previous = stored_evidence()
    with pytest.raises(RuleError) as caught:
        drafts.apply(actor, project["id"], oid, "wrong-project-version", project["version"] + 1, guards(review))
    assert caught.value.code == "VERSION_CONFLICT"
    assert stored_evidence() == previous
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]

    applied = drafts.apply(actor, project["id"], oid, "apply-evidence", project["version"], guards(review))
    assert applied == {"revision_id": revision_id}
    current = stored_evidence()
    assert current is not None
    evidence = current[2]["migration_validation"]
    assert evidence["revision_id"] == revision_id and evidence["passed"] is True
    assert evidence["policy_version"] == publication_migration_validation.POLICY_VERSION
    assert current[1] == (2 if evidence_state == "incompatible" else 1)
    if evidence_state == "compatible":
        assert current == previous

    def unexpected_validation(*args, **kwargs):
        raise AssertionError("A publicação deve reutilizar a evidência persistida na aplicação.")

    monkeypatch.setattr(publication_migration_validation, "_validate_cached", unexpected_validation)
    reused = publication_migration_validation.validate_migration_plan(
        changed, revision_id=revision_id, previous_evidence=evidence
    )
    assert reused == evidence
    assert drafts.apply(actor, project["id"], oid, "apply-evidence-again", project["version"], guards(review)) == applied
    assert stored_evidence() == current


@pytest.mark.parametrize("target", ["read", "file", "apply"])
def test_proposals_deny_cross_account(system, proposal, target):
    repo, actor, project, drafts, oid, _, _ = proposal
    review = ProposalReviews(repo).get(actor, project["id"], oid)
    other = system["users"][1]
    with pytest.raises(RuleError) as caught:
        if target == "read":
            ProposalReviews(repo).get(other, project["id"], oid)
        elif target == "file":
            ProposalReviews(repo).file(
                other, project["id"], oid, review["files"][0]["id"], review["proposal_version"]
            )
        else:
            drafts.apply(other, project["id"], oid, "cross", project["version"], guards(review))
    assert caught.value.code == "NOT_FOUND"


@pytest.mark.parametrize("target", ["draft", "revision"])
def test_concurrent_edits_preserve_both_versions_and_report_paths(proposal, target):
    repo, actor, project, drafts, oid, original, changed = proposal
    review = ProposalReviews(repo).get(actor, project["id"], oid)
    manual = {**original, "public/index.html": original["public/index.html"] + "\n<p>Manual</p>"}
    if target == "draft":
        drafts.save(
            actor,
            "project",
            project["id"],
            "manual",
            {
                "base_revision_id": project["current_revision_id"],
                "files": manual,
                "prompt": "Não perder",
            },
            0,
        )
    else:
        repo.save_revision(
            actor,
            project["id"],
            "manual",
            {
                "base_revision_id": project["current_revision_id"],
                "files": manual,
            },
            project["version"],
        )
    current = ProposalReviews(repo).get(actor, project["id"], oid)
    assert current["state"] == "conflict"
    assert "public/index.html" in current["conflict_paths"]
    assert current["proposal_version"] == review["proposal_version"]
    with pytest.raises(RuleError) as caught:
        drafts.apply(actor, project["id"], oid, "stale", project["version"], guards(review))
    assert caught.value.code == "VERSION_CONFLICT"
    assert repo.source_files(actor, project["id"], review["candidate_revision_id"]) == changed
    if target == "draft":
        assert drafts.get(actor, "project", project["id"])["data"]["files"] == manual
    else:
        latest = repo.project(actor, project["id"])
        assert repo.source_files(actor, project["id"], latest["current_revision_id"]) == manual


def test_wrong_version_and_unknown_file_never_change_project(proposal):
    repo, actor, project, drafts, oid, _, _ = proposal
    review = ProposalReviews(repo).get(actor, project["id"], oid)
    with pytest.raises(RuleError, match="versão"):
        ProposalReviews(repo).file(actor, project["id"], oid, review["files"][0]["id"], "wrong")
    with pytest.raises(RuleError, match="indisponível"):
        ProposalReviews(repo).file(actor, project["id"], oid, "../escape", review["proposal_version"])
    with pytest.raises(RuleError) as caught:
        drafts.apply(
            actor,
            project["id"],
            oid,
            "wrong",
            project["version"],
            {**guards(review), "expected_proposal_version": "wrong"},
        )
    assert caught.value.code == "PROPOSAL_CONFLICT"
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


@pytest.mark.parametrize("target", ["source", "manifest"])
def test_integrity_failure_blocks_read_and_apply(proposal, target):
    repo, actor, project, drafts, oid, _, _ = proposal
    review = ProposalReviews(repo).get(actor, project["id"], oid)
    with repo.sessions.begin() as db:
        candidate = db.get(Revision, review["candidate_revision_id"])
        if target == "source":
            path = repo.sources.path(actor, project["id"], candidate.id)
            path.write_text(path.read_text().replace("Entrar", "Alterado"))
        else:
            manifest = deepcopy(candidate.manifest)
            manifest[0]["size"] += 1
            candidate.manifest = manifest
    with pytest.raises(RuleError) as caught:
        ProposalReviews(repo).get(actor, project["id"], oid)
    assert caught.value.code == "SOURCE_INTEGRITY_FAILED"
    with pytest.raises(RuleError):
        drafts.apply(actor, project["id"], oid, "tampered", project["version"], guards(review))
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


def test_failed_transaction_keeps_entire_original_revision(proposal, monkeypatch):
    repo, actor, project, drafts, oid, original, _ = proposal
    review = ProposalReviews(repo).get(actor, project["id"], oid)

    def fail(*args):
        raise RuntimeError("synthetic transaction failure")

    monkeypatch.setattr("viracase.infrastructure.drafts.record_progress", fail)
    with pytest.raises(RuntimeError):
        drafts.apply(actor, project["id"], oid, "failed-apply", project["version"], guards(review))
    current = repo.project(actor, project["id"])
    assert current["current_revision_id"] == project["current_revision_id"]
    assert repo.source_files(actor, project["id"], current["current_revision_id"]) == original
    assert ProposalReviews(repo).get(actor, project["id"], oid)["state"] == "generated"


def test_postgres_concurrent_apply_creates_one_effect(proposal):
    repo, actor, project, drafts, oid, _, changed = proposal
    if repo.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Requer PostgreSQL real para bloqueio concorrente.")
    review = ProposalReviews(repo).get(actor, project["id"], oid)
    barrier = Barrier(2)

    def apply():
        barrier.wait(timeout=10)
        return drafts.apply(actor, project["id"], oid, "same-intent", project["version"], guards(review))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(apply) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0] == results[1]
    assert repo.project(actor, project["id"])["version"] == project["version"] + 1
    assert repo.source_files(actor, project["id"], results[0]["revision_id"]) == changed


@pytest.mark.parametrize("legacy", [False, True])
def test_authenticated_http_review_and_apply_preserve_csrf_and_legacy_contract(system, proposal, legacy):
    repo, actor, project, _, oid, _, changed = proposal
    dependencies = (system["config"], system["engine"], repo, system["catalog"], object())
    with TestClient(create_app(dependencies=dependencies)) as client:
        prefix = f"/api/v1/projects/{project['id']}/proposals/{oid}"
        assert client.get(prefix).status_code == 401
        login = client.post(
            "/api/v1/auth/login",
            headers={"Origin": system["config"].public_origin},
            json={"login": "student0", "password": "test-student-password"},
        )
        assert login.status_code == 200
        headers = {
            "Origin": system["config"].public_origin,
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": "http-apply",
            "If-Match": str(project["version"]),
        }
        review = client.get(prefix)
        assert review.status_code == 200
        body = review.json()
        content = client.get(
            prefix + "/files/" + body["files"][0]["id"], params={"proposal_version": body["proposal_version"]}
        )
        assert content.status_code == 200
        assert content.json()["proposal_version"] == body["proposal_version"]
        assert (
            client.post(
                prefix + "/apply",
                json=guards(body),
                headers={key: value for key, value in headers.items() if key != "X-CSRF-Token"},
            ).status_code
            == 403
        )
        applied = client.post(prefix + "/apply", json={} if legacy else guards(body), headers=headers)
        assert applied.status_code == 200, applied.text
        assert (
            client.post(prefix + "/apply", json={} if legacy else guards(body), headers=headers).json()
            == applied.json()
        )
        assert client.get(prefix).json()["state"] == "applied"
        assert repo.source_files(actor, project["id"], applied.json()["revision_id"]) == changed
