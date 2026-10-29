import importlib.util
import json
import os
import stat
import warnings
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from viracase.domain.rules import canonical_hash

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("recover_publication_cli", ROOT / "script" / "recover-publication.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)
OID = "14526578-249d-4d54-a6ef-2f6c8a201ad9"
PID = "e73d1736-fc65-47cf-a8d6-6b44724dc55f"
RID = "69282827-d18b-4253-9cd8-2fe7cc45eea3"
ADMIN = "2e3c3dbb-50f5-47dc-8d86-d4c73f8e0176"
PASSWORD = "private-cli-password-123"
COOKIE = "session-value-private-cli"
CSRF = "csrf-value-private-cli"
LOGIN = "operator@example.invalid"
BASE_ARGS = ["--base-url", "https://api.example.invalid/admin-api/v1", "--origin", "https://admin.example.invalid", "--login", LOGIN]
REASON = "Conferir e resolver a evidência desta operação"


@pytest.fixture(autouse=True)
def password_input(monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: PASSWORD)


def make_plan(classification="schema_applied", *, expired=False):
    plan = {
        "format_version": 1, "plan_id": PID, "operation_id": OID, "revision_id": RID, "prepared_by": ADMIN,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=-1 if expired else 15)).isoformat(),
        "assessment": {
            "classification": classification, "statement_count": 2,
            "statement_manifest": [{"index": 1, "sha256": "a" * 64}, {"index": 2, "sha256": "b" * 64}],
            "required_action": "Confira as evidências específicas deste plano.", "missing_evidence_messages": [],
        },
        "action": cli.PLAN_ACTIONS.get(classification), "can_resolve": classification in cli.PLAN_ACTIONS,
    }
    plan["plan_sha256"] = canonical_hash(plan)
    return plan


def write_plan(tmp_path, plan):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def transport_for(action, calls=None, *, login_result=None):
    calls = calls if calls is not None else []

    def handler(request):
        calls.append(request)
        assert request.url.host == "api.example.invalid"
        assert request.headers["Origin"] == "https://admin.example.invalid"
        if request.url.path == "/admin-api/v1/auth/login":
            assert json.loads(request.content) == {"login": LOGIN, "password": PASSWORD}
            return httpx.Response(
                200, json=login_result or {"id": ADMIN, "role": "admin", "csrf_token": CSRF},
                headers={"Set-Cookie": f"vira_admin={COOKIE}; Path=/admin-api/v1; Secure; HttpOnly"},
            )
        assert request.headers["X-CSRF-Token"] == CSRF
        assert f"vira_admin={COOKIE}" in request.headers["Cookie"]
        if request.url.path == "/admin-api/v1/auth/logout":
            return httpx.Response(204)
        return action(request)

    return httpx.MockTransport(handler)


def resolved():
    return httpx.Response(200, json={
        "id": OID, "state": "failed", "error_code": "PUBLICATION_REJECTED",
        "result_ref": {"publication_recovery": {"state": "resolved"}},
    })


def test_inspect_uses_admin_login_cookie_origin_and_csrf_without_reconciliation(capsys):
    calls = []

    def action(request):
        assert request.method == "GET" and request.url.path.endswith(f"/{OID}/recovery")
        return httpx.Response(200, json={
            "operation_id": OID, "revision_id": RID, "classification": "unknown", "operator_required": True,
            "summary": f"password={PASSWORD}", "secret": "never-print-provider-body",
            "migration": {"name": "migrations/0002.sql", "sha256": "a" * 64, "phase": "sql_sending"},
            "diagnostic": {
                "initial_error": {"code": "PUBLICATION_REJECTED", "message": "O provedor recusou a solicitação.",
                    "provider": {"status": 400, "cf_ray": "0123456789abcdef-DFW", "provider_error_codes": [7500], "body": PASSWORD}},
                "current_error": {"code": "MIGRATION_RESULT_UNKNOWN", "message": "SELECT * FROM private_customer;"},
            },
        })

    assert cli.main([*BASE_ARGS, "inspect", OID], transport=transport_for(action, calls)) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["operation_id"] == OID and report["migration"]["sha256"] == "a" * 64
    assert report["diagnostic"]["initial_error"]["provider"]["cf_ray"] == "0123456789abcdef-DFW"
    assert all(value not in output.out + output.err for value in (PASSWORD, COOKIE, CSRF, "never-print-provider-body", "private_customer"))
    assert [request.method for request in calls] == ["POST", "GET", "POST"]


def test_plan_saves_integral_hash_verified_private_file_and_supplies_intention(tmp_path, capsys):
    plan, target, calls = make_plan(), tmp_path / "saved.json", []

    def action(request):
        assert request.method == "POST" and request.url.path.endswith("/recovery-plan")
        assert request.headers["Idempotency-Key"] == "review-plan-2026"
        assert json.loads(request.content) == {"reason": REASON}
        return httpx.Response(200, json=plan)

    assert cli.main(
        [*BASE_ARGS, "plan", OID, "--reason", REASON, "--output", str(target), "--idempotency-key", "review-plan-2026"],
        transport=transport_for(action, calls),
    ) == 0
    assert json.loads(target.read_text()) == plan
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    output = capsys.readouterr()
    assert json.loads(output.out)["plan_sha256"] == plan["plan_sha256"]
    assert json.loads(output.err)["idempotency_key"] == "review-plan-2026"


@pytest.mark.parametrize("classification,action", [("confirmed", "record_receipt"), ("no_effect", "close_no_effect")])
def test_technical_resolution_never_adds_an_operator_attestation(tmp_path, capsys, classification, action):
    plan = make_plan(classification)
    path = write_plan(tmp_path, plan)

    def request_handler(request):
        assert request.url.path.endswith("/resolve-recovery")
        assert json.loads(request.content) == {"plan_id": PID, "plan_sha256": plan["plan_sha256"], "action": action, "reason": REASON}
        return resolved()

    assert cli.main([*BASE_ARGS, "resolve", "--plan", str(path), "--reason", REASON], transport=transport_for(request_handler)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["operation_state"] == "failed" and report["recovery_state"] == "resolved"
    assert "publicada" not in report["message"]


@pytest.mark.parametrize("classification", ["schema_applied", "schema_unchanged"])
def test_structural_resolution_hashes_evidence_locally_and_attests_all_indices_without_upload(tmp_path, capsys, classification):
    plan = make_plan(classification)
    path = write_plan(tmp_path, plan)
    evidence = tmp_path / "evidence.json"
    content = b'{"verified_record":"private-evidence-content"}'
    evidence.write_bytes(content)

    def action(request):
        body = json.loads(request.content)
        assert body["action"] == plan["action"]
        assert body["attestation"] == {
            "reference": "INC-2026 reviewed", "evidence_sha256": sha256(content).hexdigest(),
            "statement_indices": [1, 2], "observed_outcome": classification,
        }
        assert b"private-evidence-content" not in request.content
        assert request.headers["Idempotency-Key"] == "resume-reviewed-intention"
        return resolved()

    assert cli.main(
        [*BASE_ARGS, "resolve", "--plan", str(path), "--reason", REASON, "--evidence", str(evidence), "--reference", "INC-2026 reviewed", "--idempotency-key", "resume-reviewed-intention"],
        transport=transport_for(action),
    ) == 0
    assert "private-evidence-content" not in capsys.readouterr().out


def test_default_resolution_intention_is_stable_for_identical_plan_and_payload(tmp_path):
    path = write_plan(tmp_path, make_plan("confirmed"))
    keys = []

    def action(request):
        keys.append(request.headers["Idempotency-Key"])
        return resolved()

    args = [*BASE_ARGS, "resolve", "--plan", str(path), "--reason", REASON]
    assert cli.main(args, transport=transport_for(action)) == 0
    assert cli.main(args, transport=transport_for(action)) == 0
    assert len(keys) == 2 and keys[0] == keys[1]


def test_read_timeout_does_not_repeat_mutation_and_keeps_intention_visible(tmp_path, capsys):
    path, calls = write_plan(tmp_path, make_plan("confirmed")), []

    def timeout(request):
        raise httpx.ReadTimeout("private transport details " + PASSWORD, request=request)

    assert cli.main(
        [*BASE_ARGS, "resolve", "--plan", str(path), "--reason", REASON, "--idempotency-key", "same-intention-after-timeout"],
        transport=transport_for(timeout, calls),
    ) == 3
    output = capsys.readouterr()
    assert sum(request.url.path.endswith("/resolve-recovery") for request in calls) == 1
    assert "same-intention-after-timeout" in output.err and "HTTP_RESULT_UNKNOWN" in output.err
    assert PASSWORD not in output.out + output.err and "private transport details" not in output.err


def test_expired_plan_requires_explicit_original_intention_to_attempt_resumption(tmp_path, capsys):
    path = write_plan(tmp_path, make_plan("confirmed", expired=True))
    calls = []
    args = [*BASE_ARGS, "resolve", "--plan", str(path), "--reason", REASON]
    assert cli.main(args, transport=transport_for(lambda request: resolved(), calls)) == 2
    assert calls == [] and "PLAN_EXPIRED" in capsys.readouterr().err
    assert cli.main([*args, "--idempotency-key", "original-expired-intention"], transport=transport_for(lambda request: resolved(), calls)) == 0


@pytest.mark.parametrize("change", ["hash", "classification", "action", "indices", "count", "can_resolve"])
def test_invalid_or_incomplete_saved_plan_is_rejected_before_login(tmp_path, capsys, change):
    plan = make_plan()
    if change == "hash":
        plan["action"] = "close_no_effect"
    else:
        if change == "classification":
            plan["assessment"]["classification"] = []
        elif change == "action":
            plan["action"] = "force_success"
        elif change == "indices":
            plan["assessment"]["statement_manifest"][1]["index"] = 1
        elif change == "count":
            plan["assessment"]["statement_count"] = True
        else:
            plan["can_resolve"] = False
        plan["plan_sha256"] = canonical_hash({key: value for key, value in plan.items() if key != "plan_sha256"})
    path = write_plan(tmp_path, plan)
    evidence = tmp_path / "proof.json"
    evidence.write_text("reviewed record")
    calls = []
    assert cli.main(
        [*BASE_ARGS, "resolve", "--plan", str(path), "--reason", REASON, "--evidence", str(evidence), "--reference", "INC-123 reviewed"],
        transport=transport_for(lambda request: resolved(), calls),
    ) == 2
    assert calls == [] and "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("url", [
    "http://api.example.invalid", "https://user:password@api.example.invalid", "https://api.example.invalid?token=secret",
    "https://api.example.invalid/private/path", "https://api.example.invalid/#token", "https://api.example.invalid:bad", "file:///tmp/private",
])
def test_invalid_or_insecure_service_urls_are_rejected_before_credentials(url):
    with pytest.raises(cli.CliError):
        cli.normalize_url(url, allow_local_http=True)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8081", "http://localhost:8081", "http://[::1]:8081"])
def test_http_loopback_requires_explicit_development_option(url):
    with pytest.raises(cli.CliError):
        cli.normalize_url(url)
    assert cli.normalize_url(url, allow_local_http=True) == url


def test_login_redirect_is_never_followed_to_another_host(capsys):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://other.example.invalid/capture"})

    assert cli.main([*BASE_ARGS, "inspect", OID], transport=httpx.MockTransport(handler)) == 3
    assert len(calls) == 1 and calls[0].url.host == "api.example.invalid"
    assert PASSWORD not in capsys.readouterr().err


def test_existing_plan_file_is_never_overwritten_or_followed_as_symlink(tmp_path, capsys):
    target = tmp_path / "original.json"
    target.write_text("preserve this content")
    link = tmp_path / "linked.json"
    link.symlink_to(target)
    for destination in (target, link):
        calls = []
        assert cli.main(
            [*BASE_ARGS, "plan", OID, "--reason", REASON, "--output", str(destination)],
            transport=transport_for(lambda request: resolved(), calls),
        ) == 2
        assert calls == []
    assert target.read_text() == "preserve this content" and "PLAN_FILE_EXISTS" in capsys.readouterr().err


def test_unknown_password_flag_never_echoes_its_value(capsys):
    assert cli.main([*BASE_ARGS, "inspect", OID, "--password", PASSWORD]) == 2
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err and "ARGUMENTS_INVALID" in output.err


def test_non_secure_password_prompt_is_rejected_without_network(monkeypatch, capsys):
    def unsafe(prompt):
        warnings.warn("Terminal cannot hide password", cli.getpass.GetPassWarning, stacklevel=2)
        return PASSWORD

    monkeypatch.setattr(cli.getpass, "getpass", unsafe)
    calls = []
    assert cli.main([*BASE_ARGS, "inspect", OID], transport=transport_for(lambda request: resolved(), calls)) == 2
    assert calls == [] and "SECURE_INPUT_UNAVAILABLE" in capsys.readouterr().err


def test_error_response_keeps_code_and_request_id_without_provider_body(tmp_path, capsys):
    path = write_plan(tmp_path, make_plan("confirmed"))

    def action(request):
        return httpx.Response(412, json={"error": {
            "code": "RECOVERY_PLAN_STALE", "request_id": RID,
            "message": "SELECT * FROM private_customer " + PASSWORD, "details": {"authorization": COOKIE},
        }})

    assert cli.main([*BASE_ARGS, "resolve", "--plan", str(path), "--reason", REASON], transport=transport_for(action)) == 3
    output = capsys.readouterr()
    assert "RECOVERY_PLAN_STALE" in output.err and RID in output.err
    assert all(value not in output.out + output.err for value in (PASSWORD, COOKIE, CSRF, "private_customer"))


def test_sensitive_plan_response_is_not_persisted(tmp_path, capsys):
    plan = make_plan()
    plan["unexpected"] = {"csrf_token": CSRF}
    plan["plan_sha256"] = canonical_hash({key: value for key, value in plan.items() if key != "plan_sha256"})
    target = tmp_path / "unsafe-plan.json"
    assert cli.main(
        [*BASE_ARGS, "plan", OID, "--reason", REASON, "--output", str(target)],
        transport=transport_for(lambda request: httpx.Response(200, json=plan)),
    ) == 2
    assert not target.exists() and CSRF not in capsys.readouterr().err


def test_operator_login_does_not_collide_with_structural_plan_fields_and_credentials_stay_protected(tmp_path):
    def handler(request):
        if request.url.path.endswith("/auth/login"):
            assert json.loads(request.content) == {"login": "operator", "password": PASSWORD}
            return httpx.Response(
                200, json={"id": ADMIN, "role": "admin", "csrf_token": CSRF},
                headers={"Set-Cookie": f"vira_admin={COOKIE}; Path=/admin-api/v1; Secure; HttpOnly"},
            )
        assert request.url.path.endswith("/auth/logout")
        return httpx.Response(204)

    client = cli.AdministrativeClient(
        "https://api.example.invalid", "https://admin.example.invalid", transport=httpx.MockTransport(handler),
    )
    try:
        client.login("operator", PASSWORD)
        assert "operator" not in client.secrets
        assert all(value in client.secrets for value in (PASSWORD, COOKIE, CSRF))
        plan = make_plan()
        plan["assessment"].update(requires_operator=True, origin="operator_attested_schema_postconditions")
        plan["plan_sha256"] = canonical_hash({key: value for key, value in plan.items() if key != "plan_sha256"})
        path = tmp_path / "operator-plan.json"
        cli._save_plan(plan, path, client.secrets)
        assert json.loads(path.read_text()) == plan
        for number, secret in enumerate((PASSWORD, COOKIE, CSRF)):
            unsafe = {**plan, "unexpected": secret}
            destination = tmp_path / f"unsafe-{number}.json"
            with pytest.raises(cli.CliError) as caught:
                cli._save_plan(unsafe, destination, client.secrets)
            assert caught.value.code == "PLAN_SENSITIVE_CONTENT"
            assert not destination.exists()
    finally:
        client.close()


def test_fifo_and_empty_evidence_are_rejected_without_blocking(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(cli.CliError):
        cli._file_digest(empty)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(cli.CliError):
        cli._file_digest(fifo)
    with pytest.raises(cli.CliError):
        cli._read_plan(fifo)


def test_plan_hash_is_compatible_with_the_backend_algorithm():
    payload = {"reason": "Revisão íntegra", "assessment": {"indices": [1, 2]}}
    assert cli.canonical_hash(payload) == canonical_hash(payload)


def test_missing_and_inapplicable_attestations_are_rejected(tmp_path):
    with pytest.raises(cli.CliError) as missing:
        cli.resolution_payload(make_plan(), REASON)
    assert missing.value.code == "ATTESTATION_REQUIRED"
    with pytest.raises(cli.CliError) as unexpected:
        cli.resolution_payload(make_plan("confirmed"), REASON, evidence=tmp_path / "proof.json", reference="INC-123")
    assert unexpected.value.code == "ATTESTATION_NOT_ALLOWED"


def test_response_for_another_operation_never_creates_a_plan_file(tmp_path):
    plan = deepcopy(make_plan())
    plan["operation_id"] = RID
    plan["plan_sha256"] = canonical_hash({key: value for key, value in plan.items() if key != "plan_sha256"})
    target = tmp_path / "other-operation.json"
    assert cli.main(
        [*BASE_ARGS, "plan", OID, "--reason", REASON, "--output", str(target)],
        transport=transport_for(lambda request: httpx.Response(200, json=plan)),
    ) == 2
    assert not target.exists()
