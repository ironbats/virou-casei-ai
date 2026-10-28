import json
from email.parser import BytesParser
from email.policy import default
from types import MethodType, SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from test_publication_diagnostics import diagnostics_client
from test_publication_pipeline import Remote, publication as publication_fixture, start, execute
from test_publication_remote_verification import Clock
from test_publication_runtime import ProbeBuilder
from viracase.domain.rules import RuleError
from viracase.infrastructure import publication_verification as verification_module
from viracase.infrastructure.cloudflare_gateway import CloudflareGateway
from viracase.infrastructure.models import Operation
from viracase.infrastructure.observability import heartbeat
from viracase.infrastructure.publication import PublicationService
from viracase.infrastructure.publication_check import execute_check, request_check
from viracase.infrastructure.publication_reconcile import reconcile_publication
from viracase.infrastructure.worker import Worker

REAL_VALIDATE = CloudflareGateway.validate


@pytest.fixture
def publication(system, monkeypatch):
    return publication_fixture.__wrapped__(system, monkeypatch)


class ArtifactBuilder(ProbeBuilder):
    def __init__(self):
        self.built = []

    def build(self, files, stack, revision, secrets=()):
        self.built.append((stack, revision))
        return {
            **super().build(files, stack, revision, secrets),
            "assets": {"index.html": "<h1>Qualification fixture</h1>"},
            "modules": {"worker.js": {"content": "export default {}", "mime": "application/javascript+module"}},
        }


class CloudflareFixture:
    def __init__(self, mode="transient", cleanup_failure=False):
        self.mode, self.cleanup_failure = mode, cleanup_failure
        self.databases = {"existing-db": {"uuid": "existing-db", "name": "customer-project"}}
        self.scripts = {"customer-project": {"tags": ["customer"], "bindings": []}}
        self.public_attempts = {}
        self.calls = []
        self.prior_revision = None
        self.upload_count = 0
        self.create_count = 0
        self.api_token = "synthetic-cloudflare-token"

    @staticmethod
    def response(result, status=200):
        return httpx.Response(status, json={"success": True, "result": result})

    def handler(self, request):
        path, method = request.url.path, request.method
        self.calls.append((method, path, request.url.host))
        if request.url.host != "api.cloudflare.com":
            assert method == "GET" and "authorization" not in request.headers
            name = request.url.host.split(".")[0]
            assert name in self.scripts
            revision = self.scripts[name]["tags"][1]
            if path == "/":
                self.public_attempts[revision] = self.public_attempts.get(revision, 0) + 1
                if self.mode == "transient" and self.public_attempts[revision] == 1:
                    return httpx.Response(523, headers={"cf-ray": "abcdef0123456789-GRU"})
                if self.mode == "frontend_failure":
                    return httpx.Response(404)
                return httpx.Response(200, text="<h1>Fixture</h1>", headers={"content-type": "text/html"})
            if path.endswith("__viroucaseai_revision"):
                seen_revision = revision
                if self.mode == "stale_python" and self.upload_count == 2 and self.public_attempts[revision] < 3:
                    seen_revision = self.prior_revision
                return httpx.Response(200, json={"revision_id": seen_revision, "database": True})
            if path == "/api/health":
                if (
                    self.mode == "api_failure"
                    or self.mode == "python_api_failure" and self.upload_count == 2
                    or self.mode == "hono_api_failure" and self.upload_count == 1
                ):
                    return httpx.Response(200, text="private body", headers={"content-type": "text/html"})
                return httpx.Response(200, json={"ok": True, "database": True})
            raise AssertionError(path)
        assert request.headers.get("authorization") == "Bearer " + self.api_token
        if path == "/client/v4/user/tokens/verify":
            return self.response({"status": "active"})
        suffix = path.split("/accounts/", 1)[1].split("/", 1)
        if len(suffix) == 1:
            return self.response({"id": suffix[0]})
        route = "/" + suffix[1]
        if route == "/workers/subdomain":
            return self.response({"subdomain": "fixture"})
        if route == "/workers/scripts":
            return self.response([{"id": key} for key in self.scripts])
        if route == "/d1/database":
            if method == "GET":
                name = request.url.params.get("name")
                return self.response([d for d in self.databases.values() if name is None or name == d["name"]])
            self.create_count += 1
            row = {"uuid": str(uuid4()), "name": json.loads(request.content)["name"]}
            self.databases[row["uuid"]] = row
            return self.response(row)
        if route.startswith("/d1/database/"):
            database_id = route.split("/")[3]
            if route.endswith("/query"):
                sql = json.loads(request.content)["sql"]
                rows = [{"value": "verified"}] if sql.startswith("SELECT value") and self.mode != "d1_failure" else []
                return self.response([{"success": True, "results": rows}])
            if method == "GET":
                return self.response(self.databases[database_id]) if database_id in self.databases else httpx.Response(404)
            if method == "DELETE":
                self.databases.pop(database_id, None)
                return self.response({})
        if route.startswith("/workers/scripts/"):
            name = route.split("/")[3]
            if route.endswith("/assets-upload-session"):
                return self.response({"jwt": "private-upload-token", "buckets": []})
            if route.endswith("/deployments"):
                return self.response({"deployments": []})
            if route.endswith("/settings"):
                row = self.scripts.get(name)
                if row is None:
                    return httpx.Response(404)
                if self.mode == "bindings_failure" and name != "customer-project":
                    row = {**row, "bindings": [{"type": "d1", "name": "DB", "database_id": "foreign-db"}, {"type": "assets", "name": "ASSETS"}]}
                return self.response(row)
            if route.endswith("/subdomain"):
                return self.response(json.loads(request.content))
            if method == "PUT":
                message = BytesParser(policy=default).parsebytes(
                    b"Content-Type: " + request.headers["content-type"].encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + request.content
                )
                metadata = next(p for p in message.iter_parts() if p.get_param("name", header="content-disposition") == "metadata")
                settings = json.loads(metadata.get_payload(decode=True))
                if name in self.scripts:
                    self.prior_revision = self.scripts[name]["tags"][1]
                elif self.upload_count:
                    self.prior_revision = next(row["tags"][1] for key, row in self.scripts.items() if key != "customer-project")
                self.scripts[name] = settings
                self.upload_count += 1
                return self.response({"id": name, "deployment_id": str(uuid4())})
            if method == "DELETE":
                if self.cleanup_failure:
                    return httpx.Response(503)
                self.scripts.pop(name, None)
                return self.response({})
        raise AssertionError((method, route))


def run_check(system, publication, monkeypatch, mode="transient", cleanup_failure=False, both_stacks=False):
    repo, registry = system["repo"], publication[0]
    if both_stacks:
        cfg = registry.get("cloudflare")
        registry.save(system["admin"], "cloudflare", "enable-python", cfg["version"], {
            "settings": {**cfg["settings"], "allowed_stacks": ["hono-typescript-d1", "fastapi-python-d1"]},
            "reason": "Qualificacao dos dois perfis de teste",
        })
        registry.validate(system["admin"], "cloudflare", 2, "Validacao dos perfis")
        registry.activate(system["admin"], "cloudflare", "activate-python", 2, "Ativacao dos perfis")
    heartbeat(repo, "worker:test", {"available": True, "fingerprint": "f" * 64, "stacks": ["hono-typescript-d1", "fastapi-python-d1"]})
    cfg = registry.get("cloudflare")
    operation = request_check(repo, system["admin"], cfg["connection_id"], "qualification-check", cfg["version"], "Teste controlado da verificacao remota")
    remote, builder, clock = CloudflareFixture(mode, cleanup_failure), ArtifactBuilder(), Clock()
    monkeypatch.setattr(verification_module, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))

    def gateway_factory(policy, token):
        gateway = CloudflareGateway(policy, token, httpx.MockTransport(remote.handler))
        gateway.validate = MethodType(REAL_VALIDATE, gateway)
        return gateway

    worker = Worker(repo, None, None)
    worker.execute = lambda oid, token: execute_check(worker, oid, token, gateway_factory, builder)
    assert worker.tick(operation["id"])
    result = repo.operation(system["admin"], operation["id"])
    with diagnostics_client(system, system["admin"]) as client:
        response = client.get("/admin/integration-connections/" + cfg["connection_id"] + "/publication-diagnostics")
    assert response.status_code == 200
    return result, response.json(), remote, builder, clock


def assert_resources_preserved(remote):
    assert remote.scripts["customer-project"] == {"tags": ["customer"], "bindings": []}
    assert remote.databases["existing-db"] == {"uuid": "existing-db", "name": "customer-project"}
    assert not any(method == "DELETE" for method, path, host in remote.calls)
    assert len(remote.databases) == 1 + remote.create_count
    assert len(remote.scripts) == 1 + remote.upload_count


def test_real_gateway_and_job_qualify_after_523_then_retain_and_release_project(system, publication, monkeypatch):
    result, diagnostic, remote, builder, clock = run_check(system, publication, monkeypatch)
    assert result["state"] == "succeeded"
    assert diagnostic["can_publish"]
    assert diagnostic["verification"]["cleanup"]["state"] == "retained"
    assert diagnostic["verification"]["cleanup"]["intentional"] is False
    assert diagnostic["verification"]["temporary_resources"] is False
    assert len(diagnostic["verification"]["resources"]) == 1
    assert_resources_preserved(remote)
    assert remote.upload_count == remote.create_count == len(builder.built) == 1
    assert clock.delays == [1]
    assert diagnostic["verification"]["remote_verification"]["attempts"][0]["checks"][0]["status"] == 523
    assert PublicationService(system["repo"]).readiness(publication[2], publication[3]["id"])["can_publish"]
    assert "synthetic-cloudflare-token" not in json.dumps(diagnostic)
    assert "private-upload-token" not in json.dumps(diagnostic)


def test_two_profiles_have_distinct_revisions_and_old_hono_cannot_approve_python(system, publication, monkeypatch):
    result, diagnostic, remote, builder, clock = run_check(system, publication, monkeypatch, mode="stale_python", both_stacks=True)
    assert result["state"] == "succeeded" and diagnostic["can_publish"]
    assert len({revision for stack, revision in builder.built}) == 2
    reports = diagnostic["verification"]["stack_results"]
    assert len(reports) == 2
    assert reports[1]["stack"] == "fastapi-python-d1"
    assert len(reports[1]["remote"]["attempts"]) == 3
    assert reports[1]["remote"]["attempts"][0]["checks"][1]["code"] == "REVISION_MISMATCH"
    assert remote.upload_count == 2 and remote.create_count == 2
    resources = diagnostic["verification"]["resources"]
    assert len({item["worker_name"] for item in resources}) == 2
    assert len({item["database_id"] for item in resources}) == 2
    assert_resources_preserved(remote)


@pytest.mark.parametrize("mode,approved,failed", [
    ("python_api_failure", "hono-typescript-d1", "fastapi-python-d1"),
    ("hono_api_failure", "fastapi-python-d1", "hono-typescript-d1"),
])
def test_each_profile_is_verified_and_retained_independently(system, publication, monkeypatch, mode, approved, failed):
    from viracase.infrastructure.models import IntegrationConfig
    from viracase.infrastructure.publication_check import runtime_verified

    result, diagnostic, remote, builder, _clock = run_check(system, publication, monkeypatch, mode=mode, both_stacks=True)
    assert result["state"] == "failed"
    assert diagnostic["verification"]["failure"]["stack"] == failed
    assert diagnostic["verification"]["cleanup"]["state"] == "retained"
    reports = {item["stack"]: item for item in diagnostic["verification"]["stack_results"]}
    assert reports[approved]["passed"] is True
    assert reports[failed]["passed"] is False
    assert len(builder.built) == remote.create_count == remote.upload_count == 2
    config = publication[0].get("cloudflare")
    with system["repo"].sessions() as db:
        row = db.get(IntegrationConfig, config["id"])
        assert runtime_verified(row, approved, "f" * 64)
        assert not runtime_verified(row, failed, "f" * 64)
        assert row.validation["publication_check_blocked"] is False
        assert row.validation["publication_check_blocked_stacks"] == [failed]
    resources = diagnostic["verification"]["resources"]
    assert len({item["worker_name"] for item in resources}) == 2
    assert len({item["database_id"] for item in resources}) == 2
    assert_resources_preserved(remote)


@pytest.mark.parametrize("mode,code,stage", [
    ("frontend_failure", "PUBLICATION_FRONTEND_NOT_READY", "verification_remote"),
    ("api_failure", "PUBLICATION_API_NOT_READY", "verification_remote"),
    ("bindings_failure", "PUBLICATION_BINDINGS_MISMATCH", "verification_bindings"),
    ("d1_failure", "PUBLICATION_D1_PROBE_FAILED", "verification_d1"),
])
def test_failures_keep_cause_and_resources_without_unlocking_button(system, publication, monkeypatch, mode, code, stage):
    result, diagnostic, remote, builder, clock = run_check(system, publication, monkeypatch, mode=mode)
    verification = diagnostic["verification"]
    assert result["state"] == "failed" and verification["error_code"] == code
    assert verification["failed_stage"] == stage
    assert verification["failure"]["code"] == code
    assert verification["cleanup"]["state"] == "retained"
    assert verification["resources"][0]["worker_confirmed"] is (mode != "d1_failure")
    assert not diagnostic["can_publish"] and diagnostic["can_verify"]
    assert_resources_preserved(remote)
    assert not PublicationService(system["repo"]).readiness(publication[2], publication[3]["id"])["can_publish"]
    assert "private body" not in json.dumps(diagnostic)


def test_delete_unavailability_does_not_hide_original_error_or_remove_resources(system, publication, monkeypatch):
    result, diagnostic, remote, builder, clock = run_check(system, publication, monkeypatch, mode="api_failure", cleanup_failure=True)
    report = diagnostic["verification"]
    assert result["state"] == "failed"
    assert report["error_code"] == "PUBLICATION_API_NOT_READY"
    assert report["failure"]["code"] == "PUBLICATION_API_NOT_READY"
    assert report["cleanup"]["state"] == "retained"
    assert not diagnostic["can_publish"]
    assert_resources_preserved(remote)


def test_legacy_failed_operation_remains_readable_without_invented_cause(system, publication):
    config = publication[0].get("cloudflare")
    with system["repo"].sessions.begin() as db:
        db.add(Operation(account_id=system["admin"], type="publication_check", state="failed", progress_stage="failed", error_code="PUBLICATION_VERIFICATION_FAILED", payload={"connection_id": config["connection_id"]}))
    with diagnostics_client(system, system["admin"]) as client:
        response = client.get("/admin/integration-connections/" + config["connection_id"] + "/publication-diagnostics")
    assert response.status_code == 200
    assert response.json()["verification"]["failure"] is None
    assert response.json()["verification"]["failed_stage"] is None


def test_diagnostics_are_admin_only_even_when_they_include_remote_failure(system, publication, monkeypatch):
    run_check(system, publication, monkeypatch, mode="api_failure")
    config = publication[0].get("cloudflare")
    with diagnostics_client(system, publication[2]) as client:
        response = client.get("/admin/integration-connections/" + config["connection_id"] + "/publication-diagnostics")
    assert response.status_code == 403
    assert "verification" not in response.text


def test_reconciliation_accepts_current_database_id_and_keeps_project_worker(system, publication):
    operation = start(system, publication)
    remote = Remote("verify_failed")
    execute(system, operation, remote)
    assert system["repo"].operation(publication[2], operation["id"])["state"] == "reconciling"
    remote.remote["bindings"][0]["database_id"] = remote.remote["bindings"][0].pop("id")
    remote.mode = "success"
    result = reconcile_publication(system["repo"], system["admin"], operation["id"], "reconcile-current-binding", "Confirmar a revisao publicada", lambda *_: remote)
    assert result["state"] == "succeeded"
    assert remote.remote is not None and remote.db is not None
    assert "delete" not in remote.calls


def test_provider_permission_failure_contains_only_safe_http_metadata():
    def handler(request):
        return httpx.Response(403, headers={"cf-ray": "abcdef0123456789-GRU"}, json={"success": False, "errors": [{"code": 10000, "message": "Bearer secret-not-for-logs"}]})
    gateway = CloudflareGateway({"account_id": "a" * 32, "timeout_seconds": 10}, "secret-not-for-logs", httpx.MockTransport(handler))
    with pytest.raises(RuleError) as caught:
        gateway.settings("temporary-worker")
    record = verification_module.failure_record(caught.value, "verification_bindings")
    assert record["provider"]["status"] == 403
    assert record["provider"]["provider_error_codes"] == [10000]
    assert "secret-not-for-logs" not in json.dumps(record)


def test_automatic_evidence_from_single_attempt_format_requires_requalification(system, publication, monkeypatch):
    from viracase.domain.rules import canonical_hash
    from viracase.infrastructure.models import IntegrationConfig
    from viracase.infrastructure.publication_check import runtime_verified

    result, diagnostic, remote, builder, clock = run_check(system, publication, monkeypatch)
    config = publication[0].get("cloudflare")
    with system["repo"].sessions.begin() as db:
        row = db.get(IntegrationConfig, config["id"])
        assert runtime_verified(row, "hono-typescript-d1", "f" * 64)
        evidence = row.validation["runtime_verification"]
        legacy = dict(evidence["report"])
        del legacy["verification_format_version"]
        row.validation = {**row.validation, "runtime_verification": {"report": legacy, "digest": canonical_hash(legacy)}}
        assert not runtime_verified(row, "hono-typescript-d1", "f" * 64)
