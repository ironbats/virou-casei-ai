import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PARSER = shutil.which("apparmor_parser")


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "backend"
    for name in ("script", "config", "bin", "host/apparmor.d"):
        (root / name).mkdir(parents=True, exist_ok=True)
    for name in ("setup-publication-host.sh", "publication-apparmor-policy.sh"):
        text = (ROOT / "script" / name).read_text()
        text = text.replace("/sys/kernel/security/apparmor/profiles", str(root / "host/profiles"))
        text = text.replace("/etc/apparmor.d/", str(root / "host/apparmor.d") + "/")
        text = text.replace("/proc/sys/kernel/apparmor_restrict_unprivileged_userns", str(root / "host/restriction"))
        (root / "script" / name).write_text(text)
    shutil.copy(ROOT / "config/publication-apparmor", root / "config/publication-apparmor")
    (root / "host/profiles").write_text("")
    (root / "host/restriction").write_text("0\n")
    parser = root / "bin/apparmor_parser"
    parser.write_text(f"#!{sys.executable} -S\n" + r'''
import json
import os
import re
import sys
from pathlib import Path

args = sys.argv[1:]
root = Path(os.environ["AA_TEST_ROOT"])
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\n")
kind = os.environ.get("AA_TEST_PARSER", "modern")
if "--version" in args:
    print("AppArmor parser version " + ("3.0.4" if kind == "legacy" else "4.1.0"))
    sys.exit(0)
text = Path(args[-1]).read_text()
if kind == "broken":
    print("parser internal failure", file=sys.stderr)
    sys.exit(1)
if "userns," in text:
    if kind == "io_error":
        print("Cannot allocate memory", file=sys.stderr)
        sys.exit(1)
    if kind == "legacy":
        print("syntax error, unexpected TOK_END_OF_RULE, expecting TOK_MODE", file=sys.stderr)
        sys.exit(1)
if "INVALID_POLICY" in text:
    print("syntax error: invalid profile rule", file=sys.stderr)
    sys.exit(1)
if "-r" in args:
    if os.environ.get("AA_TEST_LOAD_FAIL") == "1":
        print("Permission denied while loading", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("AA_TEST_NO_ENFORCE") != "1":
        (root / "host/profiles").write_text("viracase-publication (enforce)\n")
''')
    parser.chmod(0o755)
    identity = root / "bin/id"
    identity.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "${AA_TEST_UID:-0}"\n')
    identity.chmod(0o755)
    env = {**os.environ, "PATH": str(root / "bin") + ":" + os.environ["PATH"], "AA_TEST_ROOT": str(root)}
    for key in ("AA_TEST_PARSER", "AA_TEST_LOAD_FAIL", "AA_TEST_NO_ENFORCE", "AA_TEST_UID"):
        env.pop(key, None)
    return root, env


def execute(installation, *args):
    root, env = installation
    return subprocess.run(["bash", "script/setup-publication-host.sh", *args], cwd=root, env=env, capture_output=True, text=True, timeout=15)


def calls(root):
    path = root / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def installed(root):
    return root / "host/apparmor.d/viracase-publication"


def set_restriction(root, value):
    path = root / "host/restriction"
    if value is None:
        path.unlink()
    else:
        path.write_text(value + "\n")


@pytest.mark.parametrize("restriction", ["0", "1", "invalid", None])
def test_modern_parser_keeps_exact_source(installation, restriction):
    root, _ = installation
    set_restriction(root, restriction)
    result = execute(installation)
    assert result.returncode == 0, result.stdout + result.stderr
    assert installed(root).read_bytes() == (root / "config/publication-apparmor").read_bytes()
    assert "variant=explicit-userns" in result.stdout
    assert "LEGACY" not in result.stderr


@pytest.mark.parametrize("restriction", ["0", None])
def test_legacy_omits_only_unsupported_allow_rule(installation, restriction):
    root, env = installation
    env["AA_TEST_PARSER"] = "legacy"
    set_restriction(root, restriction)
    original = (root / "config/publication-apparmor").read_bytes()
    result = execute(installation)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = b"".join(line for line in original.splitlines(keepends=True) if line.strip() != b"userns,")
    assert installed(root).read_bytes() == expected
    assert (root / "config/publication-apparmor").read_bytes() == original
    assert "APPARMOR_LEGACY_COMPATIBILITY" in result.stderr
    assert "variant=legacy-no-userns" in result.stdout
    if restriction is not None:
        assert (root / "host/restriction").read_text() == restriction + "\n"
    else:
        assert not (root / "host/restriction").exists()


@pytest.mark.parametrize("restriction", ["1", "garbage", "2", ""])
def test_legacy_cannot_bypass_enabled_or_unknown_restriction(installation, restriction):
    root, env = installation
    env["AA_TEST_PARSER"] = "legacy"
    set_restriction(root, restriction)
    installed(root).write_text("previous-policy")
    result = execute(installation)
    assert result.returncode != 0
    assert "APPARMOR_PARSER_USERNS_REQUIRED" in result.stderr
    assert installed(root).read_text() == "previous-policy"
    assert not any("-r" in call for call in calls(root))
    assert (root / "host/restriction").read_text() == restriction + "\n"


def test_unreadable_or_broken_restriction_is_not_absent(installation):
    root, env = installation
    env["AA_TEST_PARSER"] = "legacy"
    (root / "host/restriction").unlink()
    (root / "host/restriction").symlink_to(root / "does-not-exist")
    result = execute(installation)
    assert result.returncode != 0
    assert "restriction" not in result.stdout
    assert "restricao do host=unknown" in result.stderr
    assert not installed(root).exists()


@pytest.mark.parametrize("kind,code", [("broken", "APPARMOR_PARSER_FAILED"), ("io_error", "APPARMOR_USERNS_PROBE_FAILED")])
def test_non_syntax_failures_never_trigger_legacy(installation, kind, code):
    root, env = installation
    env["AA_TEST_PARSER"] = kind
    result = execute(installation)
    assert result.returncode != 0
    assert code in result.stderr
    assert not installed(root).exists()
    assert "APPARMOR_LEGACY_COMPATIBILITY" not in result.stderr


@pytest.mark.parametrize("kind", ["modern", "legacy"])
def test_other_policy_errors_are_not_masked(installation, kind):
    root, env = installation
    env["AA_TEST_PARSER"] = kind
    source = root / "config/publication-apparmor"
    source.write_text(source.read_text().replace("  file,", "  INVALID_POLICY,"))
    result = execute(installation)
    assert result.returncode != 0
    assert "APPARMOR_PROFILE_INVALID" in result.stderr
    assert not installed(root).exists()
    assert not any("-r" in call for call in calls(root))


@pytest.mark.parametrize("count", [0, 2])
def test_source_with_missing_or_duplicate_userns_stops(installation, count):
    root, _ = installation
    source = root / "config/publication-apparmor"
    source.write_text(source.read_text().replace("  userns,\n", "  userns,\n" * count))
    result = execute(installation)
    assert result.returncode != 0
    assert "APPARMOR_SOURCE_INVALID" in result.stderr
    assert not installed(root).exists()


@pytest.mark.parametrize("kind", ["modern", "legacy"])
def test_check_requires_no_root_and_never_installs(installation, kind):
    root, env = installation
    env.update(AA_TEST_PARSER=kind, AA_TEST_UID="1000")
    result = execute(installation, "--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "APPARMOR_CHECK_OK" in result.stdout
    assert not installed(root).exists()
    assert not any("-r" in call for call in calls(root))
    assert all("-Q" in call and "-K" in call and "-T" in call for call in calls(root) if "--version" not in call)


def test_install_still_requires_root(installation):
    root, env = installation
    env["AA_TEST_UID"] = "1000"
    result = execute(installation)
    assert result.returncode != 0
    assert "sudo bash" in result.stderr
    assert not calls(root)
    assert not installed(root).exists()


@pytest.mark.parametrize("kind", ["modern", "legacy"])
def test_repeat_install_compares_selected_profile_not_source(installation, kind):
    root, env = installation
    env["AA_TEST_PARSER"] = kind
    assert execute(installation).returncode == 0
    before = installed(root).stat().st_mtime_ns
    (root / "calls.jsonl").unlink()
    result = execute(installation)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "atualizado e em enforce" in result.stdout
    assert not any("-r" in call for call in calls(root))
    assert installed(root).stat().st_mtime_ns == before


def test_parser_upgrade_restores_explicit_userns(installation):
    root, env = installation
    env["AA_TEST_PARSER"] = "legacy"
    assert execute(installation).returncode == 0
    assert "userns," not in installed(root).read_text()
    env["AA_TEST_PARSER"] = "modern"
    assert execute(installation).returncode == 0
    assert "userns," in installed(root).read_text()
    assert installed(root).read_bytes() == (root / "config/publication-apparmor").read_bytes()


@pytest.mark.parametrize("failure", ["AA_TEST_LOAD_FAIL", "AA_TEST_NO_ENFORCE"])
def test_failed_load_or_enforce_preserves_persisted_file(installation, failure):
    root, env = installation
    env[failure] = "1"
    installed(root).write_text("previous-policy")
    result = execute(installation)
    assert result.returncode != 0
    assert installed(root).read_text() == "previous-policy"
    assert not list((root / "host/apparmor.d").glob(".viracase-publication.*"))
    assert "carregado e persistido" not in result.stdout


@pytest.mark.parametrize("arguments", [("--bad",), ("--check", "extra")])
def test_unknown_cli_options_stop_without_actions(installation, arguments):
    root, _ = installation
    assert execute(installation, *arguments).returncode == 2
    assert not calls(root)


@pytest.mark.skipif(PARSER is None, reason="No native AppArmor parser available")
@pytest.mark.parametrize("variant", ["explicit", "legacy"])
def test_profile_variants_match_installed_real_parser_capability(tmp_path, variant):
    source = (ROOT / "config/publication-apparmor").read_text()
    if variant == "legacy":
        source = re.sub(r"^\s*userns\s*,\s*$", "", source, flags=re.MULTILINE)
    profile = tmp_path / "profile"
    profile.write_text(source)
    probe = tmp_path / "probe"
    probe.write_text("profile viracase-test-userns-probe {\n  userns,\n}\n")
    supported = subprocess.run([PARSER, "-Q", "-T", "-K", str(probe)], capture_output=True, text=True, timeout=15)
    result = subprocess.run([PARSER, "-Q", "-T", "-K", str(profile)], capture_output=True, text=True, timeout=15)
    if variant == "explicit" and supported.returncode != 0:
        assert result.returncode != 0
    else:
        assert result.returncode == 0, result.stdout + result.stderr


def test_compatibility_never_writes_global_policy_or_weakens_container():
    helper = (ROOT / "script/publication-apparmor-policy.sh").read_text()
    installer = (ROOT / "script/setup-publication-host.sh").read_text()
    for forbidden in ("sysctl -w", "systemctl", "--privileged", "apparmor=unconfined", "seccomp=unconfined"):
        assert forbidden not in helper + installer
    assert "sed '/^[[:space:]]*userns[[:space:]]*,[[:space:]]*$/d'" in helper
    assert "(enforce)" in installer
