import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
SECRET = "diagnostic-secret-must-not-appear"


@pytest.fixture
def diagnostic_installation(tmp_path):
    root = tmp_path / "installation"
    binaries = root / "bin"
    binaries.mkdir(parents=True)
    script = root / "diagnose-docker-build.sh"
    shutil.copy(ROOT / "script/diagnose-docker-build.sh", script)
    (root / ".env").write_text(f"PRIVATE_SECRET={SECRET}\n")
    log = root / "calls.log"
    prefix = """#!/bin/bash
set -u
printf '%s' "${0##*/}" >> "$DIAGNOSTIC_TEST_LOG"
printf '\\t%s' "$@" >> "$DIAGNOSTIC_TEST_LOG"
printf '\\n' >> "$DIAGNOSTIC_TEST_LOG"
"""
    programs = {
        "timeout": r'''
[[ "$1" == --kill-after=2s && "$2" == 12s ]] || exit 99
shift 2
if [[ -n "${DIAGNOSTIC_TEST_TIMEOUT:-}" && "$*" == "$DIAGNOSTIC_TEST_TIMEOUT"* ]]; then
  printf '%s\n' 'diagnostic-secret-must-not-appear' >&2
  exit 124
fi
exec "$@"
''',
        "docker": r'''
printf '%s\n' 'diagnostic-secret-must-not-appear' >&2
case "$1:${2:-}" in
  info:*) printf '%s\n' 'diagnostic-secret-must-not-appear'; exit "${DIAGNOSTIC_TEST_INFO_EXIT:-0}" ;;
  context:inspect)
    printf '%s\n' "${DIAGNOSTIC_TEST_ENDPOINT:-unix:///var/run/docker.sock}"
    exit "${DIAGNOSTIC_TEST_CONTEXT_EXIT:-0}"
    ;;
  buildx:version)
    printf '%s\n' 'diagnostic-secret-must-not-appear'
    exit "${DIAGNOSTIC_TEST_BUILDX_EXIT:-0}"
    ;;
  buildx:inspect)
    printf 'Name: diagnostic-secret-must-not-appear\nDriver: %s\nEndpoint: diagnostic-secret-must-not-appear\n' "${DIAGNOSTIC_TEST_DRIVER:-docker}"
    exit "${DIAGNOSTIC_TEST_INSPECT_EXIT:-0}"
    ;;
  *) exit 99 ;;
esac
''',
        "getent": r'''
printf '%s\n' 'diagnostic-secret-must-not-appear' >&2
if [[ "$2" == registry-1.docker.io ]]; then
  result="${DIAGNOSTIC_TEST_REGISTRY_DNS_EXIT:-0}"
else
  result="${DIAGNOSTIC_TEST_AUTH_DNS_EXIT:-0}"
fi
if [[ "$result" == 0 && "${DIAGNOSTIC_TEST_EMPTY_DNS:-0}" != 1 ]]; then
  printf '%s\n' '192.0.2.1 STREAM registry.example'
fi
exit "$result"
''',
        "curl": r'''
printf '%s\n' 'diagnostic-secret-must-not-appear' >&2
printf '%s' "${DIAGNOSTIC_TEST_HTTP_CODE:-401}"
exit "${DIAGNOSTIC_TEST_CURL_EXIT:-0}"
''',
    }
    for name, body in programs.items():
        path = binaries / name
        path.write_text(prefix + body)
        path.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(binaries),
        "DIAGNOSTIC_TEST_LOG": str(log),
        "HTTPS_PROXY": f"http://user:{SECRET}@proxy.invalid:3128",
    }
    for key in ("DOCKER_CONTEXT", "DOCKER_HOST", "BASH_ENV", "ENV"):
        env.pop(key, None)
    return root, env


def run_diagnostic(installation, *args):
    root, env = installation
    return subprocess.run(
        [BASH, str(root / "diagnose-docker-build.sh"), *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def calls(installation):
    path = installation[0] / "calls.log"
    return [line.split("\t") for line in path.read_text().splitlines()] if path.exists() else []


def test_success_checks_network_without_claiming_a_successful_build(diagnostic_installation):
    root, _ = diagnostic_installation
    result = run_diagnostic(diagnostic_installation)
    assert result.returncode == 0, result.stderr
    assert "DOCKER_DAEMON=REACHABLE" in result.stdout
    assert "DOCKER_ENDPOINT=LOCAL_SOCKET" in result.stdout
    assert "BUILDER_DRIVER=docker" in result.stdout
    assert "CLIENT_DNS[registry-1.docker.io]=RESOLVED" in result.stdout
    assert "CLIENT_DNS[auth.docker.io]=RESOLVED" in result.stdout
    assert "CLIENT_HTTPS=REACHABLE HTTP=401" in result.stdout
    assert "o build ainda precisa ser executado e validado" in result.stdout
    assert "Cliente, daemon Docker e builder podem usar redes" in result.stdout
    assert SECRET not in result.stdout + result.stderr
    assert (root / ".env").read_text() == f"PRIVATE_SECRET={SECRET}\n"
    observed = calls(diagnostic_installation)
    assert [c for c in observed if c[0] == "docker"] == [
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
        ["docker", "buildx", "version"],
        ["docker", "buildx", "inspect"],
    ]
    assert len([c for c in observed if c[0] == "timeout"]) == 7
    assert {c[0] for c in observed} == {"timeout", "docker", "getent", "curl"}
    curl_call = next(c for c in observed if c[0] == "curl")
    assert curl_call == [
        "curl", "-q", "--silent", "--output", "/dev/null", "--write-out", "%{http_code}",
        "--connect-timeout", "5", "--max-time", "10", "--proto", "=https",
        "https://registry-1.docker.io/v2/",
    ]


@pytest.mark.parametrize(
    "context,host,endpoint,selection,kind,inspect_context",
    [
        ("remote-context", "unix:///ignored.sock", f"ssh://user:{SECRET}@remote.invalid", "DOCKER_CONTEXT", "NETWORK", True),
        ("local-context", f"tcp://user:{SECRET}@ignored.invalid:2376", "unix:///chosen.sock", "DOCKER_CONTEXT", "LOCAL_SOCKET", True),
        ("", f"ssh://user:{SECRET}@remote.invalid", "unix:///ignored.sock", "DOCKER_HOST", "NETWORK", False),
        ("", "unix:///chosen.sock", "ssh://ignored.invalid", "DOCKER_HOST", "LOCAL_SOCKET", False),
        ("", "", "tcp://remote.invalid:2376", "CURRENT_CONTEXT", "NETWORK", True),
    ],
)
def test_effective_endpoint_respects_context_precedence_without_exposing_it(
    diagnostic_installation, context, host, endpoint, selection, kind, inspect_context
):
    _, env = diagnostic_installation
    env.update(DOCKER_CONTEXT=context, DOCKER_HOST=host, DIAGNOSTIC_TEST_ENDPOINT=endpoint)
    result = run_diagnostic(diagnostic_installation)
    assert result.returncode == 0, result.stderr
    assert f"DOCKER_SELECTION={selection}" in result.stdout
    assert f"DOCKER_ENDPOINT={kind}" in result.stdout
    assert any(c[:3] == ["docker", "context", "inspect"] for c in calls(diagnostic_installation)) == inspect_context
    assert SECRET not in result.stdout + result.stderr
    assert endpoint not in result.stdout + result.stderr


@pytest.mark.parametrize("driver", ["docker-container", "kubernetes", "remote"])
def test_reports_only_supported_driver_name(diagnostic_installation, driver):
    diagnostic_installation[1]["DIAGNOSTIC_TEST_DRIVER"] = driver
    result = run_diagnostic(diagnostic_installation)
    assert result.returncode == 0
    assert f"BUILDER_DRIVER={driver}" in result.stdout
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("field", ["DIAGNOSTIC_TEST_REGISTRY_DNS_EXIT", "DIAGNOSTIC_TEST_AUTH_DNS_EXIT"])
def test_dns_failure_is_not_overruled_by_successful_https_through_proxy(diagnostic_installation, field):
    diagnostic_installation[1][field] = "2"
    result = run_diagnostic(diagnostic_installation)
    assert result.returncode == 1
    assert "CLIENT_DNS[" in result.stderr
    assert "CLIENT_HTTPS=REACHABLE HTTP=401" in result.stdout
    assert "DIAGNOSTIC_COMPLETED" not in result.stdout


@pytest.mark.parametrize(
    "code,label",
    [(5, "DNS_FAILED"), (6, "DNS_FAILED"), (35, "TLS_FAILED"), (60, "TLS_FAILED"), (28, "TIMEOUT"), (7, "FAILED")],
)
def test_https_failure_is_separate_from_successful_local_dns(diagnostic_installation, code, label):
    diagnostic_installation[1]["DIAGNOSTIC_TEST_CURL_EXIT"] = str(code)
    result = run_diagnostic(diagnostic_installation)
    assert result.returncode == 1
    assert "CLIENT_DNS[registry-1.docker.io]=RESOLVED" in result.stdout
    assert f"CLIENT_HTTPS={label}" in result.stderr
    assert "CLIENT_HTTPS=REACHABLE" not in result.stdout
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("status,expected", [(200, 0), (301, 1), (403, 1), (429, 1), (500, 1), (SECRET, 1)])
def test_http_response_classification_does_not_follow_redirects_or_print_untrusted_text(
    diagnostic_installation, status, expected
):
    diagnostic_installation[1]["DIAGNOSTIC_TEST_HTTP_CODE"] = str(status)
    result = run_diagnostic(diagnostic_installation)
    assert result.returncode == expected
    assert SECRET not in result.stdout + result.stderr
    assert "--location" not in next(c for c in calls(diagnostic_installation) if c[0] == "curl")


@pytest.mark.parametrize(
    "command",
    ["docker info", "docker context inspect", "docker buildx version", "docker buildx inspect",
     "getent ahosts registry-1.docker.io", "getent ahosts auth.docker.io", "curl"],
)
def test_each_external_probe_is_bounded_and_timeout_cannot_report_success(diagnostic_installation, command):
    diagnostic_installation[1]["DIAGNOSTIC_TEST_TIMEOUT"] = command
    result = run_diagnostic(diagnostic_installation)
    assert result.returncode == 1
    assert "TIMEOUT" in result.stderr
    assert "DIAGNOSTIC_COMPLETED" not in result.stdout
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("tool", ["docker", "getent", "curl"])
def test_missing_tool_reports_incomplete(diagnostic_installation, tool):
    (diagnostic_installation[0] / "bin" / tool).unlink()
    result = run_diagnostic(diagnostic_installation)
    assert result.returncode == 2
    assert "DIAGNOSTIC_INCOMPLETE" in result.stderr
    assert not any(c[0] == tool for c in calls(diagnostic_installation))


def test_absent_timeout_executes_no_unbounded_probes(diagnostic_installation):
    (diagnostic_installation[0] / "bin/timeout").unlink()
    result = run_diagnostic(diagnostic_installation)
    assert result.returncode == 2
    assert "nenhuma sondagem foi executada" in result.stderr
    assert calls(diagnostic_installation) == []


def test_missing_buildx_is_incomplete_and_never_bootstraps_a_builder(diagnostic_installation):
    diagnostic_installation[1]["DIAGNOSTIC_TEST_BUILDX_EXIT"] = "127"
    result = run_diagnostic(diagnostic_installation)
    assert result.returncode == 2
    assert "BUILDX=UNAVAILABLE" in result.stderr
    assert not any(c[:3] == ["docker", "buildx", "inspect"] for c in calls(diagnostic_installation))


@pytest.mark.parametrize(
    "field,value,label",
    [("DIAGNOSTIC_TEST_ENDPOINT", SECRET, "DOCKER_ENDPOINT=UNKNOWN"),
     ("DIAGNOSTIC_TEST_DRIVER", SECRET, "BUILDER_DRIVER=UNKNOWN")],
)
def test_unrecognized_metadata_is_not_printed_or_treated_as_complete(diagnostic_installation, field, value, label):
    diagnostic_installation[1][field] = value
    result = run_diagnostic(diagnostic_installation)
    assert result.returncode == 2
    assert label in result.stderr
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("field", ["DIAGNOSTIC_TEST_INFO_EXIT", "DIAGNOSTIC_TEST_CONTEXT_EXIT", "DIAGNOSTIC_TEST_INSPECT_EXIT"])
def test_docker_probe_failure_is_reported_without_raw_output(diagnostic_installation, field):
    diagnostic_installation[1][field] = "1"
    result = run_diagnostic(diagnostic_installation)
    assert result.returncode == 1
    assert "DIAGNOSTIC_FAILED" in result.stderr
    assert SECRET not in result.stdout + result.stderr


def test_empty_dns_response_does_not_count_as_resolution(diagnostic_installation):
    diagnostic_installation[1]["DIAGNOSTIC_TEST_EMPTY_DNS"] = "1"
    result = run_diagnostic(diagnostic_installation)
    assert result.returncode == 1
    assert "CLIENT_DNS[registry-1.docker.io]=FAILED" in result.stderr


def test_observed_failure_takes_precedence_over_incomplete_probe(diagnostic_installation):
    (diagnostic_installation[0] / "bin/curl").unlink()
    diagnostic_installation[1]["DIAGNOSTIC_TEST_REGISTRY_DNS_EXIT"] = "2"
    result = run_diagnostic(diagnostic_installation)
    assert result.returncode == 1
    assert "CLIENT_HTTPS=INCOMPLETE" in result.stderr
    assert "DIAGNOSTIC_FAILED" in result.stderr


def test_unrecognized_arguments_execute_no_probes(diagnostic_installation):
    result = run_diagnostic(diagnostic_installation, "--repair")
    assert result.returncode == 2
    assert "Uso:" in result.stderr
    assert calls(diagnostic_installation) == []


def test_bash_syntax_is_valid():
    result = subprocess.run(
        [BASH, "-n", str(ROOT / "script/diagnose-docker-build.sh")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
