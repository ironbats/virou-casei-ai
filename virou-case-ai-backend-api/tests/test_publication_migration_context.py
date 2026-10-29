import json
from hashlib import sha256

import pytest
from migration_history_support import install_legacy_revision
from sqlalchemy import select
from test_conversation_runtime import fund_generation
from test_publication_pipeline import Remote, execute, start
from test_publication_pipeline import publication as publication_fixture

from viracase.application.conversation_turn import turn_prompt
from viracase.application.generation import GenerationService
from viracase.application.ports import ModelResult
from viracase.domain.rules import RuleError
from viracase.infrastructure.code_analysis import CodeAnalysis
from viracase.infrastructure.drafts import Drafts
from viracase.infrastructure.models import Job, PublicationResource
from viracase.infrastructure.publication_migration_context import published_migration_context
from viracase.infrastructure.publication_verification import failure_record, migration_diagnostic
from viracase.infrastructure.worker import Worker


@pytest.fixture
def publication(system, monkeypatch):
    return publication_fixture.__wrapped__(system, monkeypatch)


@pytest.fixture
def published(system, publication):
    repo = system["repo"]
    _, service, actor, project = publication
    operation, remote = start(system, publication), Remote()
    execute(system, operation, remote)
    assert repo.operation(actor, operation["id"])["state"] == "succeeded"
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    path = next(path for path in sorted(files) if path.startswith("migrations/"))
    return repo, service, actor, project, files, path, remote


def save(repo, actor, project, files, key="edit"):
    return repo.save_revision(
        actor,
        project["id"],
        key,
        {"base_revision_id": project["current_revision_id"], "files": files},
        project["version"],
    )["project"]


def context(repo, actor, project, files):
    with repo.sessions() as db:
        owned = repo.owned_project(db, actor, project["id"])
        return published_migration_context(repo, db, owned, files)


def test_confirmed_sources_are_only_added_when_current_files_diverge(published):
    repo, _, actor, project, files, path, _ = published
    original = context(repo, actor, project, files)
    assert original["files"] == [
        {
            "path": path,
            "sha256": sha256(files[path].encode()).hexdigest(),
            "status": "current_matches",
        }
    ]
    changed = {**files, path: files[path] + "\nCREATE TABLE future (id TEXT);\n"}
    current = install_legacy_revision(repo, actor, project, changed)
    historical = context(repo, actor, current, changed)["files"][0]
    assert historical["status"] == "historical_source_verified"
    assert historical["content"] == files[path]
    assert historical["source_revision_id"] == project["current_revision_id"]
    assert repo.source_files(actor, current["id"], current["current_revision_id"]) == changed


def test_receipt_from_unpublished_candidate_can_restore_exact_history(published):
    repo, _, actor, project, files, _path, _ = published
    candidate_files = {**files, "migrations/0002.sql": "CREATE TABLE candidate_data (id TEXT);\n"}
    with repo.sessions.begin() as db:
        owned = repo.owned_project(db, actor, project["id"])
        candidate = repo.add_revision(
            db, owned, candidate_files, "generation", owned.current_revision_id, proposal=True
        )
        resource = db.get(PublicationResource, project["id"])
        resource.inventory = {
            **resource.inventory,
            "candidate_revision_id": candidate.id,
            "migrations": {
                **resource.inventory["migrations"],
                "migrations/0002.sql": sha256(candidate_files["migrations/0002.sql"].encode()).hexdigest(),
            },
        }
        candidate_id = candidate.id
    restored = context(repo, actor, project, files)["files"][1]
    assert restored["source_revision_id"] == candidate_id
    assert restored["content"] == candidate_files["migrations/0002.sql"]
    assert restored["status"] == "historical_source_verified"


@pytest.mark.parametrize("other_account", [False, True])
def test_historical_lookup_never_reads_another_project_or_account(
    system, published, monkeypatch, other_account
):
    repo, _, actor, project, files, path, _ = published
    owner = system["users"][1] if other_account else actor
    other = repo.create_project(owner, "other-project", {"name": "Outro", "objective": "Projeto separado"})
    foreign = repo.source_files(owner, other["id"], other["current_revision_id"])
    foreign[path] = "CREATE TABLE confidential_project_data (id TEXT);\n"
    other = install_legacy_revision(repo, owner, other, foreign)
    with repo.sessions.begin() as db:
        resource = db.get(PublicationResource, project["id"])
        resource.inventory = {
            **resource.inventory,
            "published_revision_id": other["current_revision_id"],
            "migrations": {path: sha256(foreign[path].encode()).hexdigest()},
        }
    read, observed = repo.sources.read, []

    def capture(revision, stack):
        observed.append((revision.account_id, revision.project_id))
        return read(revision, stack)

    monkeypatch.setattr(repo.sources, "read", capture)
    result = context(repo, actor, project, files)
    assert result["files"][0]["status"] == "source_unavailable"
    assert "content" not in result["files"][0]
    assert foreign[path] not in json.dumps(result)
    assert set(observed) == {(actor, project["id"])}


def test_missing_historical_source_is_explicit_and_never_reconstructed(published):
    repo, _, actor, project, files, path, _ = published
    changed = {**files, path: files[path] + "\nCREATE TABLE later_data (id TEXT);\n"}
    current = install_legacy_revision(repo, actor, project, changed)
    repo.sources.path(actor, project["id"], project["current_revision_id"]).unlink()
    result = context(repo, actor, current, changed)["files"][0]
    assert result["status"] == "source_unavailable"
    assert "content" not in result and "source_revision_id" not in result


class MigrationGateway:
    def __init__(self, path, original, mode="restore", on_dispatch=None):
        self.path, self.original, self.mode, self.on_dispatch = path, original, mode, on_dispatch
        self.contexts, self.prompts = [], []

    def generate(self, policy, prompt):
        current = json.loads(prompt[prompt.index('{"stack"') :])
        self.contexts.append(current)
        self.prompts.append(prompt)
        if self.on_dispatch:
            self.on_dispatch()
        if policy.get("code_analysis_contract"):
            body = {
                "summary": "Uma migration aplicada foi modificada.",
                "issues": [
                    {
                        "severity": "error",
                        "path": self.path if self.path in current["files"] else "",
                        "line": 0,
                        "title": "Histórico aplicado divergente",
                        "explanation": "O conteúdo atual difere do recibo confirmado.",
                        "suggestion": "Restaure a fonte histórica exata e crie uma migration incremental.",
                    }
                ],
                "limitations": ["Diagnóstico sintético para teste de integração."],
            }
        elif self.mode == "clarify":
            body = {
                "action": "clarify",
                "message": "Forneça o SQL histórico original desta migration.",
                "files": [],
                "edits": [],
                "complete": True,
            }
        else:
            wrong = self.mode == "persistent" or self.mode == "retry" and len(self.prompts) == 1
            body = {
                "action": "implement",
                "message": "Preservei o histórico e separei a evolução do banco.",
                "files": [
                    {
                        "path": self.path,
                        "content": self.original + "\nCREATE TABLE wrong (id TEXT);\n"
                        if wrong
                        else self.original,
                    },
                    {
                        "path": "migrations/0003.sql"
                        if self.path == "migrations/0002.sql"
                        else "migrations/0002.sql",
                        "content": "CREATE TABLE additional_data (id TEXT);\n",
                    },
                ],
                "edits": [],
                "complete": True,
            }
        return ModelResult(
            json.dumps(body), {"input": 100, "cached": 0, "output": 100}, "synthetic-migrations"
        )


def configure_generation(system):
    fund_generation(system)
    policy = {
        **system["policy"],
        "input_usd_per_million": "0",
        "cached_usd_per_million": "0",
        "output_usd_per_million": "125",
    }
    system["config"].catalog_path.write_text(json.dumps({"models": [policy]}))


def generate(system, actor, project, gateway, **payload):
    configure_generation(system)
    repo = system["repo"]
    operation = GenerationService(repo, system["catalog"]).start(
        actor,
        project["id"],
        "migration-repair",
        {
            "brief": "Corrija a publicação preservando os dados existentes.",
            "base_revision_id": project["current_revision_id"],
            "policy_version": "2026-10-v1",
            "auto_apply": False,
            "review_required": True,
            **payload,
        },
        contract_version=4,
    )
    assert Worker(repo, gateway, system["catalog"]).tick(operation["id"])
    return repo.operation(actor, operation["id"])


@pytest.mark.parametrize(
    "mode,expected,calls",
    [("restore", "succeeded", 1), ("retry", "succeeded", 2), ("persistent", "failed", 3)],
)
def test_generation_cannot_commit_changed_applied_migration(system, published, mode, expected, calls):
    repo, _, actor, project, files, path, _ = published
    gateway = MigrationGateway(path, files[path], mode)
    operation = generate(system, actor, project, gateway)
    assert operation["state"] == expected, operation
    assert len(gateway.prompts) == calls
    evidence = gateway.contexts[0]["published_database_migrations"]["files"][0]
    assert "content" not in evidence
    assert evidence["status"] == "current_matches"
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    if expected == "succeeded":
        candidate = repo.source_files(actor, project["id"], operation["result_ref"]["revision_id"])
        assert candidate[path] == files[path]
        assert "migrations/0002.sql" in candidate
        assert {key: value for key, value in candidate.items() if not key.startswith("migrations/")} == {
            key: value for key, value in files.items() if not key.startswith("migrations/")
        }
    else:
        assert operation["error_code"] == "MIGRATION_HISTORY_IMMUTABLE"
        assert "revision_id" not in operation["result_ref"]
    if calls > 1:
        assert "MIGRATION_HISTORY_IMMUTABLE" in gateway.prompts[1]


def test_receipt_confirmed_during_provider_call_is_checked_before_commit(system, published):
    repo, _, actor, project, files, path, _ = published

    def confirm_concurrent_migration():
        with repo.sessions.begin() as db:
            resource = db.get(PublicationResource, project["id"])
            resource.inventory = {
                **resource.inventory,
                "migrations": {
                    **resource.inventory["migrations"],
                    "migrations/0002.sql": "a" * 64,
                },
            }

    gateway = MigrationGateway(path, files[path], on_dispatch=confirm_concurrent_migration)
    operation = generate(system, actor, project, gateway)
    assert operation["state"] == "failed" and operation["error_code"] == "MIGRATION_BASELINE_INCONSISTENT"
    assert "revision_id" not in operation["result_ref"]
    assert len(gateway.contexts) == 1
    assert operation["result_ref"]["migration_failure"]["migration"]["issues"][0]["path"] == "migrations/0002.sql"


def test_proposal_is_rechecked_if_receipts_change_before_apply(system, published):
    repo, _, actor, project, files, path, _ = published
    operation = generate(system, actor, project, MigrationGateway(path, files[path]))
    assert operation["state"] == "succeeded"
    with repo.sessions.begin() as db:
        resource = db.get(PublicationResource, project["id"])
        resource.inventory = {
            **resource.inventory,
            "migrations": {
                **resource.inventory["migrations"],
                "migrations/0002.sql": "a" * 64,
            },
        }
    with pytest.raises(RuleError) as caught:
        Drafts(repo, None).apply(actor, project["id"], operation["id"], "apply-stale", project["version"])
    assert caught.value.code == "MIGRATION_BASELINE_INCONSISTENT"
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]


def test_legacy_divergence_can_be_analyzed_but_never_redefines_the_baseline(system, published):
    configure_generation(system)
    repo, service, actor, project, files, path, remote = published
    altered = {**files, path: files[path] + "\nCREATE TABLE additional_data (id TEXT);\n"}
    project = install_legacy_revision(repo, actor, project, altered)
    blocked = service.start(
        actor, project["id"], "publish-conflict", {"revision_id": project["current_revision_id"]}
    )
    before_calls = list(remote.calls)
    execute(system, blocked, remote)
    failure = repo.operation(actor, blocked["id"])
    assert failure["state"] == "failed" and failure["error_code"] == "MIGRATION_CHECKSUM_CONFLICT"
    assert failure["result_ref"]["revision_id"] == project["current_revision_id"]
    assert remote.calls == before_calls
    assert failure["result_ref"]["publication_failure"]["migration"]["issues"][0]["path"] == path
    assert service.readiness(actor, project["id"])["can_publish"]
    analyses = CodeAnalysis(repo, system["catalog"])
    analysis = analyses.start(
        actor,
        project["id"],
        "migration-analysis",
        {
            "revision_id": project["current_revision_id"],
            "source": "publication",
            "publication_operation_id": blocked["id"],
            "runtime_error": None,
            "policy_version": "2026-10-v1",
        },
    )
    gateway = MigrationGateway(path, files[path])
    assert Worker(repo, gateway, system["catalog"]).tick(analysis["id"])
    evidence = gateway.contexts[0]["evidence"]
    assert evidence["publication"]["migration"]["issues"][0]["reason"] == "checksum_changed"
    assert evidence["published_database_migrations"]["files"][0]["content"] == files[path]
    correction = analyses.correct(
        actor,
        project["id"],
        analysis["id"],
        "correct-migration",
        {
            "base_project_revision": project["current_revision_id"],
            "policy_version": "2026-10-v1",
        },
        GenerationService(repo, system["catalog"]),
    )
    assert correction["type"] == "generation"
    assert Worker(repo, gateway, system["catalog"]).tick(correction["id"])
    corrected = repo.operation(actor, correction["id"])
    assert corrected["state"] == "failed" and corrected["error_code"] == "MIGRATION_BASELINE_INCONSISTENT"
    assert "revision_id" not in corrected["result_ref"]
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    assert remote.receipts[path] == sha256(files[path].encode()).hexdigest()
    assert "migrations/0002.sql" not in remote.receipts
    assert remote.calls.count("upload") == 1
    with repo.sessions() as db:
        resource = db.get(PublicationResource, project["id"])
        assert resource.inventory["published_revision_id"] != project["current_revision_id"]
        assert db.scalar(select(Job).where(Job.operation_id == blocked["id"])).state == "done"


def test_diagnostic_whitelist_keeps_valid_hashes_and_excludes_sql_secrets_and_unknown_fields():
    diagnostic = {
        "format_version": 1,
        "category": "migration",
        "code": "MIGRATION_CHECKSUM_CONFLICT",
        "issues": [
            {
                "code": "MIGRATION_CHECKSUM_CONFLICT",
                "reason": "checksum_changed",
                "receipt_source": "inventory",
                "path": "migrations/0001.sql",
                "expected_sha256": "a" * 64,
                "actual_sha256": "b" * 64,
                "message": "Bearer synthetic-secret",
                "hint": "SELECT secret FROM data",
                "sql": "SELECT secret FROM data",
            }
        ],
        "issue_count": 1,
        "token": "synthetic-secret",
        "database_id": "private-identifier",
    }
    error = RuleError(
        "MIGRATION_CHECKSUM_CONFLICT", "Migration aplicada divergente.", details={"migration": diagnostic}
    )
    result = failure_record(error, "publication_provisioning", secrets=("synthetic-secret",))
    serialized = json.dumps(result)
    assert (
        "synthetic-secret" not in serialized
        and "SELECT" not in serialized
        and "private-identifier" not in serialized
    )
    issue = result["migration"]["issues"][0]
    assert issue["expected_sha256"] == "a" * 64 and issue["actual_sha256"] == "b" * 64
    assert issue["path"] == "migrations/0001.sql"


@pytest.mark.parametrize(
    "path",
    [
        "../secrets.sql",
        "migrations/../secrets.sql",
        "migrations/\u202esecrets.sql",
        "migrations//0001.sql",
        "/migrations/0001.sql",
    ],
)
def test_diagnostic_rejects_untrusted_paths(path):
    result = migration_diagnostic(
        {
            "code": "MIGRATION_CHECKSUM_CONFLICT",
            "issues": [
                {
                    "code": "MIGRATION_CHECKSUM_CONFLICT",
                    "reason": "missing_file",
                    "receipt_source": "remote",
                    "path": path,
                }
            ],
        }
    )
    assert "path" not in result["issues"][0]


@pytest.mark.parametrize("field", ["code", "reason", "receipt_source"])
@pytest.mark.parametrize("value", [[], {}, None, True])
def test_diagnostic_discards_malformed_enumerations(field, value):
    issue = {
        "code": "MIGRATION_CHECKSUM_CONFLICT",
        "reason": "missing_file",
        "receipt_source": "remote",
        field: value,
    }
    assert migration_diagnostic({"code": "MIGRATION_CHECKSUM_CONFLICT", "issues": [issue]}) is None
    if field == "code":
        assert migration_diagnostic({"code": value, "issues": []}) is None


def test_unpublished_project_already_protects_its_consolidated_migrations(system):
    repo, actor = system["repo"], system["users"][0]
    project = repo.create_project(actor, "unpublished", {"name": "Novo", "objective": "Aplicação nova"})
    files = repo.source_files(actor, project["id"], project["current_revision_id"])
    evidence = context(repo, actor, project, files)
    assert evidence["files"] == []
    assert evidence["protected_files"] == [{"path": "migrations/0001.sql", "sha256": sha256(files["migrations/0001.sql"].encode()).hexdigest()}]


def test_unknown_history_can_clarify_without_creating_a_revision(system, published):
    repo, _, actor, project, files, path, _ = published
    with repo.sessions.begin() as db:
        resource = db.get(PublicationResource, project["id"])
        resource.inventory = {**resource.inventory, "migrations": {path: "a" * 64}}
    gateway = MigrationGateway(path, files[path], "clarify")
    operation = generate(system, actor, project, gateway)
    assert operation["state"] == "succeeded" and operation["result_ref"]["kind"] == "clarification"
    assert "revision_id" not in operation["result_ref"]
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    assert gateway.contexts[0]["published_database_migrations"]["files"][0]["status"] == "source_unavailable"


def test_large_historical_sql_uses_code_budget_without_weakening_request_or_total_limits():
    project = {"stack": "hono-typescript-d1", "objective": "Evoluir o banco publicado"}
    content = "".join(f"CREATE TABLE historical_{index} (id INTEGER);\n" for index in range(1800))
    history = {
        "format_version": 1,
        "files": [
            {
                "path": "migrations/0001.sql",
                "sha256": sha256(content.encode()).hexdigest(),
                "status": "historical_source_verified",
                "content": content,
            }
        ],
    }
    files = {"migrations/0001.sql": "CREATE TABLE changed (id INTEGER);\n"}
    prompt = turn_prompt(
        project,
        "Corrija a migration aplicada.",
        files,
        input_bound=256384,
        request_limit_bytes=60000,
        published_migrations=history,
    )
    assert len(prompt.encode()) > 60000
    assert json.loads(prompt[prompt.index('{"stack"') :])["published_database_migrations"] == history
    with pytest.raises(RuleError) as caught:
        turn_prompt(
            project,
            "x" * 60001,
            files,
            input_bound=256384,
            request_limit_bytes=60000,
            published_migrations=history,
        )
    assert caught.value.details["reason_code"] == "REQUEST_CONTEXT_LIMIT"
    with pytest.raises(RuleError) as caught:
        turn_prompt(
            project,
            "Corrija a migration aplicada.",
            files,
            input_bound=64096,
            request_limit_bytes=60000,
            published_migrations=history,
        )
    assert caught.value.code == "CONTEXT_LIMIT_EXCEEDED"
    assert caught.value.details["context_bytes"] > 64096


def test_removed_legacy_migration_is_analyzed_without_automatic_source_replacement(system, published):
    configure_generation(system)
    repo, service, actor, project, files, _path, remote = published
    path, content = "migrations/0002.sql", "CREATE TABLE retained_history (id TEXT);\n"
    project = save(repo, actor, project, {**files, path: content}, "add-migration")
    publication = service.start(
        actor, project["id"], "publish-second", {"revision_id": project["current_revision_id"]}
    )
    execute(system, publication, remote)
    assert repo.operation(actor, publication["id"])["state"] == "succeeded"
    project = install_legacy_revision(repo, actor, project, files)
    blocked = service.start(
        actor, project["id"], "publish-removed", {"revision_id": project["current_revision_id"]}
    )
    execute(system, blocked, remote)
    result = repo.operation(actor, blocked["id"])
    assert result["state"] == "failed"
    assert result["result_ref"]["publication_failure"]["migration"]["issues"][0]["reason"] == "missing_file"
    analysis_service = CodeAnalysis(repo, system["catalog"])
    analysis = analysis_service.start(
        actor,
        project["id"],
        "analyze-removed",
        {
            "revision_id": project["current_revision_id"],
            "source": "publication",
            "publication_operation_id": blocked["id"],
            "runtime_error": None,
            "policy_version": "2026-10-v1",
        },
    )
    gateway = MigrationGateway(path, content)
    assert Worker(repo, gateway, system["catalog"]).tick(analysis["id"])
    report = repo.operation(actor, analysis["id"])
    assert report["state"] == "succeeded"
    assert report["result_ref"]["issues"][0]["path"] is None
    assert report["result_ref"]["can_request_correction"] is True
    correction = analysis_service.correct(
        actor,
        project["id"],
        analysis["id"],
        "restore-removed",
        {
            "base_project_revision": project["current_revision_id"],
            "policy_version": "2026-10-v1",
        },
        GenerationService(repo, system["catalog"]),
    )
    assert Worker(repo, gateway, system["catalog"]).tick(correction["id"])
    corrected = repo.operation(actor, correction["id"])
    assert corrected["state"] == "failed" and corrected["error_code"] == "MIGRATION_BASELINE_INCONSISTENT", corrected
    assert "revision_id" not in corrected["result_ref"]
    request_context = gateway.contexts[1]["request"]
    assert "published_database_migrations" not in request_context
    verified_context = gateway.contexts[1]["published_database_migrations"]
    assert next(item for item in verified_context["files"] if item["path"] == path)["content"] == content
    assert repo.project(actor, project["id"])["current_revision_id"] == project["current_revision_id"]
    assert path not in repo.source_files(actor, project["id"], project["current_revision_id"])
    assert remote.receipts[path] == sha256(content.encode()).hexdigest()
    assert remote.calls.count("upload") == 2
