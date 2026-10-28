import errno
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys

import pytest

from viracase.infrastructure import build_limits, publication_build, publication_resources
from viracase.infrastructure.publication_build import IsolatedBuild
from viracase.infrastructure.publication_diagnostics import classify_failure
from viracase.infrastructure.publication_resources import resource_evidence, resource_snapshot, workload_nproc
from test_publication_diagnostics import CHECKS, toolchain

ROOT = Path(__file__).resolve().parents[1]


def write(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def fixture_tree(tmp_path, membership="0::/parent/worker\n"):
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    write(proc, "self/cgroup", membership)
    return proc, cgroup


@pytest.mark.parametrize("inherited,expected", [((-1, -1), 64), ((4096, 4096), 64), ((32, 4096), 32), ((16, 16), 16), ((0, 1024), 0)])
def test_workload_process_limit_never_raises_an_inherited_limit(monkeypatch, inherited, expected):
    monkeypatch.setattr(publication_resources.resource, "getrlimit", lambda kind: inherited)
    assert workload_nproc() == expected


@pytest.mark.parametrize("stack", ["__probe__", "hono-typescript-d1", "fastapi-python-d1"])
def test_limit_is_applied_after_namespace_and_before_every_workload(monkeypatch, stack):
    calls, limits = [], {}
    monkeypatch.setattr(build_limits.sys, "argv", ["limits", "/tmp/source", stack, "revision"])
    monkeypatch.setattr(build_limits.resource, "setrlimit", lambda key, value: limits.update({key: value}))
    monkeypatch.setattr(publication_resources.resource, "getrlimit", lambda kind: (4096, 4096))
    monkeypatch.setattr(build_limits.os, "execvpe", lambda name, argv, env: calls.append((name, argv, env)))
    build_limits.main()
    executable, command, environment = calls[0]
    assert executable == "bwrap" and environment == {"PATH": "/usr/bin:/bin"}
    assert resource.RLIMIT_NPROC not in limits
    assert limits == {resource.RLIMIT_CORE: (0, 0), resource.RLIMIT_CPU: (30, 30), resource.RLIMIT_AS: (3_000_000_000, 3_000_000_000), resource.RLIMIT_FSIZE: (8_000_000, 8_000_000), resource.RLIMIT_NOFILE: (128, 128)}
    start = command.index("/usr/bin/prlimit")
    assert start > command.index("--disable-userns")
    assert command[start:start + 5] == ["/usr/bin/prlimit", "--nproc=64:64", "--", "/usr/bin/node", "--max-old-space-size=256"]
    assert command[-3:] == ["/toolchain/probe.mjs" if stack == "__probe__" else "/toolchain/build.mjs", stack, "revision"]
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "--clearenv" in command and "--unshare-user" in command


def test_real_prlimit_executes_without_mutating_parent_limits():
    executable = shutil.which("prlimit")
    if executable is None:
        pytest.skip("util-linux prlimit is required")
    before = resource.getrlimit(resource.RLIMIT_NPROC)
    maximum = workload_nproc()
    script = "import json,resource;print(json.dumps(list(resource.getrlimit(resource.RLIMIT_NPROC))))"
    result = subprocess.run([executable, f"--nproc={maximum}:{maximum}", "--", sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [maximum, maximum]
    assert resource.getrlimit(resource.RLIMIT_NPROC) == before


def test_cgroup_v2_collects_current_and_visible_ancestors_without_paths_or_payloads(tmp_path):
    proc, group = fixture_tree(tmp_path)
    for path, current, maximum in (("parent/worker", "12", "128"), ("parent", "100", "100"), ("", "120", "max")):
        write(group, path + "/pids.current" if path else "pids.current", current)
        write(group, path + "/pids.max" if path else "pids.max", maximum)
    write(group, "parent/worker/pids.events", "max 0\nunknown sensitive-value\n")
    write(group, "parent/pids.events", "max 3\n")
    write(group, "parent/worker/memory.current", "4000000")
    write(group, "parent/worker/memory.max", "536870912")
    write(group, "parent/worker/memory.events", "oom 0\noom_kill 0\nsecret 7777\n")
    result = resource_snapshot(proc, group)
    assert [item["current"] for item in result["pids_cgroup"]["levels"]] == [12, 100, 120]
    assert result["pids_cgroup"]["version"] == "v2"
    assert result["memory_cgroup"]["levels"][0]["max"] == 536870912
    serialized = json.dumps(result)
    assert "sensitive" not in serialized and "secret" not in serialized and str(tmp_path) not in serialized
    assert result["nproc_application_stage"] == "inside_sandbox_before_node"
    assert result["workload_nproc_max"] == 64


def test_cgroup_v1_collects_controller_specific_limits(tmp_path):
    proc, group = fixture_tree(tmp_path, "5:pids:/worker\n6:memory:/worker\n")
    write(group, "pids/worker/pids.current", "6")
    write(group, "pids/worker/pids.max", "120")
    write(group, "pids/worker/pids.events", "max 0")
    write(group, "memory/worker/memory.usage_in_bytes", "1000000")
    write(group, "memory/worker/memory.limit_in_bytes", "2000000")
    write(group, "memory/worker/memory.failcnt", "0")
    result = resource_snapshot(proc, group)
    assert result["pids_cgroup"]["version"] == "v1"
    assert result["memory_cgroup"]["levels"][0]["max"] == 2000000


@pytest.mark.parametrize("membership", ["", "not-a-cgroup", "0::/../../private\n", "0::relative/path\n"])
def test_missing_or_invalid_membership_is_unknown_not_zero(tmp_path, membership):
    proc, group = fixture_tree(tmp_path, membership)
    result = resource_snapshot(proc, group)
    assert result["pids_cgroup"]["levels"] == []
    assert result["pids_cgroup"]["version"] is None


def test_stale_cgroup_events_do_not_prove_current_failure():
    previous = {"pids_cgroup": {"levels": [{"depth": 0, "current": 10, "max": 128, "events": {"max": 8}}]}}
    assert resource_evidence(previous, previous) == []
    after = {"pids_cgroup": {"levels": [{"depth": 0, "current": 10, "max": 128, "events": {"max": 9}}]}}
    evidence = resource_evidence(previous, after)
    assert evidence == [{"code": "CGROUP_PIDS_LIMIT_REACHED", "depth": 0, "event_increased": True, "at_limit": False}]


def test_saturated_ancestor_is_reported_not_ignored():
    snapshot = {"pids_cgroup": {"levels": [{"depth": 0, "current": 5, "max": 128, "events": {}}, {"depth": 1, "current": 200, "max": 200, "events": {}}]}}
    assert resource_evidence({}, snapshot) == [{"code": "CGROUP_PIDS_LIMIT_REACHED", "depth": 1, "event_increased": False, "at_limit": True}]


def test_eagain_alone_does_not_claim_cgroup_or_memory_root_cause():
    message = "bwrap: Creating new namespace failed: Resource temporarily unavailable\n"
    assert classify_failure(message, 1) == "SANDBOX_RESOURCE_EXHAUSTED"
    evidence = {"resource_evidence": [{"code": "CGROUP_PIDS_LIMIT_REACHED"}]}
    assert classify_failure(message, 1, {}, evidence) == "CGROUP_PIDS_EXHAUSTED"


@pytest.mark.parametrize("message,expected", [("prlimit: failed to set the NPROC resource limit: Operation not permitted", "WORKLOAD_LIMIT_FAILED"), ("bwrap: Creating new namespace failed: nesting depth or /proc/sys/user/max_*_namespaces exceeded (ENOSPC)", "NAMESPACE_QUOTA_EXCEEDED")])
def test_resource_related_errors_remain_distinguishable(message, expected):
    assert classify_failure(message, 1) == expected


def test_logged_failure_is_not_six_executed_security_failures(toolchain, monkeypatch):
    responses = iter([subprocess.CompletedProcess([], 0, b"", b""), subprocess.CompletedProcess([], 1, b"", b"bwrap: Creating new namespace failed: Resource temporarily unavailable\n")])
    monkeypatch.setattr(publication_build.subprocess, "run", lambda *args, **kwargs: next(responses))
    result = IsolatedBuild().diagnostics()
    assert result["code"] == "SANDBOX_RESOURCE_EXHAUSTED" and result["available"] is False
    assert result["checks_completed"] is False
    assert result["failed_checks"] == []
    assert result["unverified_checks"] == sorted(CHECKS)
    assert result["details"]["stage"] == "isolation_probe"
    assert result["details"]["resources_before"]["workload_nproc_max"] == 64
    assert result["details"]["resources_after"]["rlimits"]["nproc"]


def test_an_executed_failed_check_remains_fail_closed(toolchain, monkeypatch):
    results = iter([subprocess.CompletedProcess([], 0, b"", b""), subprocess.CompletedProcess([], 0, json.dumps({**CHECKS, "resource_limits": False}).encode(), b"")])
    monkeypatch.setattr(publication_build.subprocess, "run", lambda *args, **kwargs: next(results))
    result = IsolatedBuild().diagnostics()
    assert result["code"] == "ISOLATION_PROBE_FAILED" and result["available"] is False
    assert result["checks_completed"] is True and result["failed_checks"] == ["resource_limits"]
    assert result["unverified_checks"] == []


def test_true_checks_still_require_preflight_and_fingerprint(toolchain, monkeypatch):
    results = iter([subprocess.CompletedProcess([], 0, b"", b""), subprocess.CompletedProcess([], 0, json.dumps(CHECKS).encode(), b"")])
    monkeypatch.setattr(publication_build.subprocess, "run", lambda *args, **kwargs: next(results))
    result = IsolatedBuild().diagnostics()
    assert result["available"] is True and result["code"] == "READY"
    assert result["checks_completed"] is True and result["unverified_checks"] == result["failed_checks"] == []


def test_image_checks_prlimit_and_probe_enforces_both_process_limits():
    docker = (ROOT / "Dockerfile").read_text()
    probe = (ROOT / "build-toolchain/probe.mjs").read_text()
    assert "test -x /usr/bin/prlimit" in docker
    assert '.slice(0, 2)' in probe and 'values.every' in probe
    assert 'limit("Max processes", 64)' in probe


@pytest.mark.skipif(os.getenv("VIRA_TEST_LINUX_NPROC") != "1", reason="Enable VIRA_TEST_LINUX_NPROC=1 for bounded real Linux namespace regression")
def test_real_linux_shared_uid_pressure_before_vs_after_namespace(tmp_path):
    compiler = shutil.which("cc")
    if sys.platform != "linux" or compiler is None:
        pytest.skip("Linux C compiler and user namespaces are required")
    executable = tmp_path / "nproc-probe"
    compiled = subprocess.run([compiler, "-Wall", "-Wextra", "-O2", "-pthread", str(ROOT / "tests/fixtures/nproc_namespace_probe.c"), "-o", str(executable)], capture_output=True, text=True, timeout=20)
    assert compiled.returncode == 0, compiled.stderr
    reports = {}
    for order in ("before", "after"):
        result = subprocess.run([str(executable), order], capture_output=True, text=True, timeout=15)
        if result.returncode == 77:
            pytest.skip("Host does not support the bounded test: " + result.stderr[:200])
        assert result.returncode == 0, result.stderr
        reports[order] = json.loads(result.stdout)
    if reports["after"]["clone_errno"] in (errno.EPERM, errno.EINVAL, errno.ENOSPC):
        pytest.skip("Host blocks CLONE_NEWUSER: " + json.dumps(reports))
    assert reports["before"]["clone_errno"] == errno.EAGAIN, reports
    assert reports["after"] == {"order": "after", "clone_errno": 0, "child_exit": 0}, reports


@pytest.mark.parametrize("soft,hard,expected", [("64", "64", True), ("64", "unlimited", False), ("32", "128", False)])
def test_actual_javascript_probe_requires_soft_and_hard_nproc(soft, hard, expected):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the probe logic")
    harness = '''
import fs from "node:fs";
import vm from "node:vm";
const data = JSON.parse(fs.readFileSync(0, "utf8"));
let output = "";
const context = {
  fs: {
    readFileSync: (name) => name === "/project/probe.json" ? '{"outside":"/private"}' : name === "/proc/self/limits" ? data.limits : "NoNewPrivs:\\t1\\nCapEff:\\t0000000000000000\\n",
    writeFileSync: () => { throw Error("EROFS"); },
    existsSync: () => false,
  },
  os: { networkInterfaces: () => ({lo: [{internal: true}]}) },
  spawnSync: () => ({status: 1}),
  process: { env: {PATH: "/usr/bin:/bin"}, stdout: {write: (text) => { output = text; }} },
};
vm.runInNewContext(data.source.replace(/^import.*;$/gm, ""), context, {timeout: 1000});
process.stdout.write(output);
'''
    limits = f"Max cpu time 30 30 seconds\nMax address space 3000000000 3000000000 bytes\nMax processes {soft} {hard} processes\nMax open files 128 128 files\n"
    data = {"source": (ROOT / "build-toolchain/probe.mjs").read_text(), "limits": limits}
    result = subprocess.run([node, "--input-type=module", "--eval", harness], input=json.dumps(data), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    checks = json.loads(result.stdout)
    assert checks["resource_limits"] is expected
    assert all(value is True for key, value in checks.items() if key != "resource_limits")
