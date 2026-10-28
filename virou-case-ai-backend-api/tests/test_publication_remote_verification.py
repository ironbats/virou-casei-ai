import json

import httpx
import pytest

from viracase.domain.rules import RuleError
from viracase.infrastructure.publication_verification import (
    RemoteVerifier, VerificationFailure, binding_observation, confirm_bindings,
    database_binding_matches,
)

URL = "https://probe.fixture.workers.dev"
REVISION = "b8434aba-415e-4edf-b18e-bcb2511e02d4"


class Clock:
    def __init__(self):
        self.now = 0.0
        self.delays = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += seconds


def healthy(request, revision=REVISION):
    if request.url.path == "/":
        return httpx.Response(200, headers={"content-type": "text/html; charset=UTF-8"}, text="<h1>Test</h1>")
    if request.url.path == "/api/__viroucaseai_revision":
        return httpx.Response(200, json={"revision_id": revision, "database": True})
    if request.url.path == "/api/health":
        return httpx.Response(200, json={"ok": True, "database": True})
    raise AssertionError(request.url.path)


def verifier(handler, clock=None, **kwargs):
    clock = clock or Clock()
    return RemoteVerifier(10, httpx.MockTransport(handler), clock=clock.monotonic, sleep=clock.sleep, **kwargs), clock


def test_all_public_checks_must_pass_in_the_same_attempt_without_credentials():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert request.headers["cache-control"] == "no-cache"
        assert "__viroucaseai_probe" in request.url.params
        return healthy(request)

    instance, clock = verifier(handler)
    result = instance.verify(URL, REVISION)
    assert result["passed"] and len(result["attempts"]) == 1
    assert len(calls) == 3 and clock.delays == []


@pytest.mark.parametrize("status", [404, 408, 425, 429, 500, 502, 503, 523, 524])
def test_transient_http_failure_waits_and_reads_again_without_redeployment(status):
    round_number = 0
    methods = []

    def handler(request):
        nonlocal round_number
        methods.append(request.method)
        if request.url.path == "/":
            round_number += 1
            if round_number == 1:
                return httpx.Response(status)
        return healthy(request)

    instance, clock = verifier(handler)
    result = instance.verify(URL, REVISION)
    assert result["passed"] and len(result["attempts"]) == 2
    assert result["attempts"][0]["checks"][0]["status"] == status
    assert clock.delays == [1] and methods == ["GET"] * 6


def test_revision_from_previous_stack_cannot_qualify_the_new_stack():
    rounds = 0

    def handler(request):
        nonlocal rounds
        if request.url.path == "/":
            rounds += 1
        return healthy(request, "previous-stack-revision" if rounds < 3 else REVISION)

    instance, _ = verifier(handler)
    result = instance.verify(URL, REVISION)
    assert len(result["attempts"]) == 3
    assert [r["passed"] for r in result["attempts"]] == [False, False, True]
    assert result["attempts"][0]["checks"][1]["code"] == "REVISION_MISMATCH"


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_transient_transport_failure_is_observed_without_logging_its_message(error_type):
    failed = False

    def handler(request):
        nonlocal failed
        if not failed:
            failed = True
            raise error_type("secret-value-must-not-be-recorded", request=request)
        return healthy(request)

    instance, _ = verifier(handler)
    result = instance.verify(URL, REVISION)
    assert result["passed"] and len(result["attempts"]) == 2
    assert "secret-value" not in json.dumps(result)


@pytest.mark.parametrize("status,code", [(401, "PUBLICATION_ACCESS_DENIED"), (403, "PUBLICATION_ACCESS_DENIED"), (302, "PUBLICATION_REDIRECTED"), (400, "PUBLICATION_FRONTEND_NOT_READY")])
def test_permanent_responses_do_not_retry_or_follow_redirects(status, code):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/":
            return httpx.Response(status, headers={"location": "http://127.0.0.1/secret"})
        return healthy(request)

    instance, clock = verifier(handler)
    with pytest.raises(VerificationFailure) as error:
        instance.verify(URL, REVISION)
    assert error.value.code == code
    assert len(calls) == 3 and clock.delays == []
    assert all(r.url.host == "probe.fixture.workers.dev" for r in calls)
    assert "127.0.0.1" not in json.dumps(error.value.diagnostic)


@pytest.mark.parametrize("database", [False, "true", 1, None])
def test_revision_endpoint_requires_a_real_database_confirmation(database):
    def handler(request):
        if request.url.path.endswith("revision"):
            return httpx.Response(200, json={"revision_id": REVISION, "database": database})
        return healthy(request)

    instance, clock = verifier(handler)
    with pytest.raises(VerificationFailure) as error:
        instance.verify(URL, REVISION)
    assert error.value.code == "PUBLICATION_REVISION_NOT_READY"
    assert error.value.diagnostic["failure_code"] == "DATABASE_UNCONFIRMED"
    assert clock.delays == []


@pytest.mark.parametrize("content,expected", [(b"invalid-json-sensitive-value", "INVALID_JSON"), (b"[]", "INVALID_JSON_SHAPE"), (b'{"ok":false}', "API_UNHEALTHY"), (b'{"database":false}', "API_UNHEALTHY"), (b"x" * 70000, "RESPONSE_TOO_LARGE")])
def test_api_response_failures_are_specific_and_bodies_are_not_retained(content, expected):
    def handler(request):
        if request.url.path == "/api/health":
            return httpx.Response(200, headers={"content-type": "application/json"}, content=content)
        return healthy(request)

    instance, _ = verifier(handler)
    with pytest.raises(VerificationFailure) as error:
        instance.verify(URL, REVISION)
    assert error.value.code == "PUBLICATION_API_NOT_READY"
    assert error.value.diagnostic["failure_code"] == expected
    assert "sensitive-value" not in json.dumps(error.value.diagnostic)


def test_persistent_outage_stops_at_budget_and_retains_all_probe_statuses():
    instance, clock = verifier(lambda request: httpx.Response(523, headers={"cf-ray": "abcdef0123456789-GRU"}))
    with pytest.raises(VerificationFailure) as error:
        instance.verify(URL, REVISION)
    report = error.value.diagnostic
    assert not report["passed"] and 1 < len(report["attempts"]) <= instance.max_attempts
    assert clock.now < instance.budget
    assert all(len(attempt["checks"]) == 3 for attempt in report["attempts"])
    assert report["attempts"][-1]["checks"][0]["cf_ray"] == "abcdef0123456789-GRU"


def test_exhausted_budget_never_accepts_a_late_success():
    clock = Clock()

    def handler(request):
        clock.now += 50
        return healthy(request)

    instance, _ = verifier(handler, clock)
    with pytest.raises(VerificationFailure) as error:
        instance.verify(URL, REVISION)
    assert not error.value.diagnostic["passed"]
    assert error.value.diagnostic["attempts"][-1]["checks"][-1]["code"] == "PROBE_DEADLINE"


def test_retry_after_longer_than_budget_does_not_trigger_another_request_round():
    instance, clock = verifier(lambda request: httpx.Response(429, headers={"retry-after": "600"}))
    with pytest.raises(VerificationFailure) as error:
        instance.verify(URL, REVISION)
    assert len(error.value.diagnostic["attempts"]) == 1 and clock.delays == []


def test_server_cookies_are_not_reused_by_the_public_checks():
    def handler(request):
        assert "cookie" not in request.headers
        response = healthy(request)
        response.headers["set-cookie"] = "session=private; Path=/; Secure"
        return response

    instance, _ = verifier(handler)
    assert instance.verify(URL, REVISION)["passed"]


@pytest.mark.parametrize("target", ["http://probe.fixture.workers.dev", "https://localhost", "https://probe.fixture.workers.dev.evil.example", "https://a:b@probe.fixture.workers.dev", "https://probe.fixture.workers.dev:443", "https://probe.fixture.workers.dev/x", "https://probe.fixture.workers.dev?x=1", "https://probe.fixture.workers.dev#fragment"])
def test_probe_targets_cannot_be_redirected_to_arbitrary_urls(target):
    def handler(request):
        pytest.fail("A forbidden target must not reach the transport")

    instance, _ = verifier(handler)
    with pytest.raises(RuleError) as error:
        instance.verify(target, REVISION)
    assert error.value.code == "CONFIGURATION_INVALID"


def test_loss_of_lease_stops_before_public_request():
    def before_request(method):
        raise RuleError("LEASE_LOST", "Lost lease")

    instance, clock = verifier(lambda request: pytest.fail("Lease lost"), before_request=before_request)
    with pytest.raises(RuleError) as error:
        instance.verify(URL, REVISION)
    assert error.value.code == "LEASE_LOST" and clock.delays == []


def settings(database="db", revision=REVISION):
    return {"bindings": [{"type": "d1", "name": "DB", "database_id": database}, {"type": "assets", "name": "ASSETS"}], "tags": ["viroucaseai", revision]}


@pytest.mark.parametrize("key", ["database_id", "id"])
def test_binding_contract_supports_current_and_legacy_database_id(key):
    value = settings()
    value["bindings"][0] = {"type": "d1", "name": "DB", key: "db"}
    assert binding_observation(value, "db", REVISION)["passed"]


def test_conflicting_database_identifiers_are_rejected():
    assert not database_binding_matches({"type": "d1", "name": "DB", "database_id": "db", "id": "foreign-db"}, "db")


@pytest.mark.parametrize("case", ["foreign_d1", "wrong_assets_name", "duplicate_db", "extra_binding", "malformed"])
def test_binding_isolation_is_not_weakened_by_the_retry_policy(case):
    value = settings()
    if case == "foreign_d1":
        value["bindings"][0]["database_id"] = "foreign"
    elif case == "wrong_assets_name":
        value["bindings"][1]["name"] = "WRONG"
    elif case == "duplicate_db":
        value["bindings"].append(dict(value["bindings"][0]))
    elif case == "extra_binding":
        value["bindings"].append({"type": "secret_text", "name": "SENSITIVE", "text": "secret"})
    else:
        value["bindings"] = ["invalid"]
    observation = binding_observation(value, "db", REVISION)
    assert not observation["passed"] and not observation["retryable"]
    assert "secret" not in json.dumps(observation)


def test_missing_settings_are_rechecked_until_current_revision_is_observed():
    values = iter([None, settings(revision="old"), settings()])
    remote = type("Remote", (), {"settings": lambda self, name: next(values)})()
    clock = Clock()
    result = confirm_bindings(remote, "probe", "db", REVISION, 10, clock=clock.monotonic, sleep=clock.sleep)
    assert result["passed"] and len(result["attempts"]) == 3
    assert clock.delays == [1, 2]


def test_missing_settings_do_not_silently_qualify_after_retry_limit():
    remote = type("Remote", (), {"settings": lambda self, name: None})()
    clock = Clock()
    with pytest.raises(VerificationFailure) as error:
        confirm_bindings(remote, "probe", "db", REVISION, 10, clock=clock.monotonic, sleep=clock.sleep)
    assert error.value.code == "PUBLICATION_BINDINGS_NOT_READY"
    assert len(error.value.diagnostic["attempts"]) == 5
