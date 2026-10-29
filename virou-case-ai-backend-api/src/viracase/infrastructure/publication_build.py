import json
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from viracase.domain.rules import RuleError, canonical_hash, validate_files
from viracase.infrastructure.publication_build_diagnostics import (
    BuildFailure,
    compiler_diagnostic,
    normalized_issue,
)
from viracase.infrastructure.publication_diagnostics import (
    REMEDIATIONS,
    classify_failure,
    runtime_environment,
)
from viracase.infrastructure.publication_logging import emit, safe_text
from viracase.infrastructure.publication_resources import resource_evidence, resource_snapshot
from viracase.infrastructure.publication_sandbox import sandbox_command

TOOLCHAIN = Path("/opt/viracase-build")
if not TOOLCHAIN.is_dir():
    TOOLCHAIN = Path(__file__).resolve().parents[3] / "build-toolchain"


class IsolatedBuild:
    def fingerprint(self):
        paths = [
            TOOLCHAIN / name
            for name in ("build.mjs", "probe.mjs", "package-lock.json", "python-profile/pylock.toml")
        ]
        if (TOOLCHAIN / "python-vendor.json").is_file():
            paths.append(TOOLCHAIN / "python-vendor.json")
        paths.extend(
            Path(__file__).parent / name
            for name in (
                "publication_build.py",
                "publication_build_diagnostics.py",
                "publication_sandbox.py",
                "publication_diagnostics.py",
                "publication_resources.py",
                "publication_check.py",
                "build_limits.py",
                "cloudflare_gateway.py",
                "publication_worker.py",
                "publication_migrations.py",
                "publication_lifecycle.py",
            )
        )
        self._fingerprint_error = None
        hashes = {}
        for path in paths:
            try:
                hashes[path.name] = sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                self._fingerprint_error = {
                    "artifact": path.name,
                    "exception_type": type(exc).__name__,
                    "errno": exc.errno,
                }
                return None
        return canonical_hash(hashes)

    def supports(self, stack):
        return stack == "hono-typescript-d1" or (
            stack == "fastapi-python-d1" and (TOOLCHAIN / "python-vendor.json").is_file()
        )

    def _sandbox_command(self):
        return sandbox_command() + ["--proc", "/proc", "--dev", "/dev", "/usr/bin/true"]

    def _capture(self, stage, started, result=None, exc=None, resources_before=None, include_stderr=True):
        stderr = getattr(result, "stderr", b"") if result is not None else getattr(exc, "stderr", b"")
        detail = {
            "stage": stage,
            "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
            "exit_code": getattr(result, "returncode", None),
            "stderr": safe_text(stderr) if include_stderr else "",
        }
        if exc is not None:
            detail["exception_type"] = type(exc).__name__
            if isinstance(exc, OSError):
                detail["errno"] = exc.errno
        detail["resources_before"] = resources_before or {}
        detail["resources_after"] = resource_snapshot()
        detail["resource_evidence"] = resource_evidence(detail["resources_before"], detail["resources_after"])
        self._detail = detail

    def _preflight(self):
        self._environment = runtime_environment()
        required_files = (
            (shutil.which("bwrap"), "bubblewrap"),
            (Path("/usr/bin/node").is_file(), "Node.js em /usr/bin/node"),
            ((TOOLCHAIN / "build.mjs").is_file(), "build.mjs"),
            ((TOOLCHAIN / "probe.mjs").is_file(), "probe.mjs"),
            ((TOOLCHAIN / "node_modules/esbuild").is_dir(), "esbuild"),
            (Path("/usr/bin/unshare").is_file(), "util-linux/unshare"),
            (Path("/usr/bin/prlimit").is_file(), "util-linux/prlimit"),
        )
        missing = [name for found, name in required_files if not found]
        if missing:
            return "TOOLCHAIN_MISSING", "Dependências ausentes: " + ", ".join(missing) + ". Recrie a imagem do worker."
        started = time.monotonic()
        resources_before = resource_snapshot()
        try:
            run = subprocess.run(
                self._sandbox_command(),
                capture_output=True,
                timeout=5,
                env={"PATH": "/usr/bin:/bin"},
            )
        except subprocess.TimeoutExpired as exc:
            self._capture("sandbox_preflight", started, exc=exc, resources_before=resources_before)
            return "SANDBOX_TIMEOUT", REMEDIATIONS["SANDBOX_TIMEOUT"]
        except OSError as exc:
            self._capture("sandbox_preflight", started, exc=exc, resources_before=resources_before)
            return "SANDBOX_START_FAILED", REMEDIATIONS["SANDBOX_START_FAILED"]
        self._capture("sandbox_preflight", started, result=run, resources_before=resources_before)
        if run.returncode == 0:
            if "(complain)" in self._environment.get("apparmor_profile", ""):
                return "APPARMOR_NOT_ENFORCING", REMEDIATIONS["APPARMOR_NOT_ENFORCING"]
            return "READY", "Executor de namespaces disponível."
        code = classify_failure(run.stderr, run.returncode, self._environment, self._detail)
        if code == "APPARMOR_PROFILE_MISMATCH":
            self._detail["expected_apparmor_profile"] = "viracase-publication (enforce)"
            self._detail["observed_apparmor_profile"] = self._environment.get("apparmor_profile")
        return code, REMEDIATIONS[code]

    def _probe(self):
        with tempfile.TemporaryDirectory(prefix="viracase-probe-") as root:
            source = Path(root) / "project"
            source.mkdir()
            (Path(root) / "private-marker").write_text("synthetic-isolation-marker")
            (source / "probe.json").write_text(json.dumps({"outside": str(Path(root) / "private-marker")}))
            started = time.monotonic()
            resources_before = resource_snapshot()
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "viracase.infrastructure.build_limits",
                        str(source),
                        "__probe__",
                        "probe",
                    ],
                    capture_output=True,
                    timeout=15,
                    env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).parents[2])},
                )
                self._capture("isolation_probe", started, result=result, resources_before=resources_before)
                if result.returncode:
                    self._detail["code"] = classify_failure(result.stderr, result.returncode, getattr(self, "_environment", None), self._detail)
                    checks = {}
                else:
                    checks = json.loads(result.stdout)
                    if not isinstance(checks, dict):
                        raise ValueError("probe_object_required")
                    self._detail["probe_completed"] = True
            except subprocess.TimeoutExpired as exc:
                self._capture("isolation_probe", started, exc=exc, resources_before=resources_before)
                self._detail["code"] = "SANDBOX_TIMEOUT"
                checks = {}
            except OSError as exc:
                self._capture("isolation_probe", started, exc=exc, resources_before=resources_before)
                self._detail["code"] = "SANDBOX_START_FAILED"
                checks = {}
            except ValueError:
                self._detail["code"] = "PROBE_OUTPUT_INVALID"
                checks = {}
        return checks if isinstance(checks, dict) else {}

    def _failed_checks(self, checks):
        required = {
            "no_network",
            "no_host_secrets",
            "no_cross_project_files",
            "read_only_sources",
            "resource_limits",
            "no_nested_userns",
        }
        return sorted(name for name in required if checks.get(name) is not True) + sorted(
            name for name in checks if name not in required
        )

    def diagnostics(self):
        started = time.monotonic()
        self._detail = {"stage": "toolchain", "exit_code": None, "stderr": ""}
        digest = self.fingerprint()
        code, message = self._preflight()
        checks = {}
        if code == "READY":
            checks = self._probe()
            if self._failed_checks(checks) or self._detail.get("code"):
                code = self._detail.get("code", "ISOLATION_PROBE_FAILED")
                message = REMEDIATIONS.get(code, REMEDIATIONS["ISOLATION_PROBE_FAILED"])
        required = self._failed_checks({})
        failed_checks = sorted(name for name in required if checks.get(name) is False)
        unverified_checks = sorted(name for name in required if not isinstance(checks.get(name), bool))
        if not digest and code == "READY":
            code, message = "TOOLCHAIN_MISSING", "Não foi possível calcular a identidade do executor."
        available = code == "READY" and bool(digest)
        return {
            "available": available,
            "fingerprint": digest,
            "fingerprint_error": getattr(self, "_fingerprint_error", None),
            "stacks": [
                stack for stack in ("hono-typescript-d1", "fastapi-python-d1") if self.supports(stack)
            ],
            "code": "READY" if available else code,
            "message": "Executor isolado e controles de contenção verificados." if available else message,
            "failed_checks": failed_checks,
            "unverified_checks": unverified_checks,
            "checks_completed": bool(self._detail.get("probe_completed")) and not unverified_checks,
            "diagnostic_id": str(uuid4()),
            "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
            "details": self._detail,
            "environment": getattr(self, "_environment", None) or runtime_environment(),
            "remediation": None if available else REMEDIATIONS.get(code, message),
        }

    def verify_isolation(self):
        self._detail = {}
        checks = self._probe()
        if self._failed_checks(checks):
            emit("isolation_verification_failed", "error", failed_checks=self._failed_checks(checks), **self._detail)
            raise RuleError(
                "EXECUTOR_UNAVAILABLE", "O executor não comprovou todos os controles de isolamento."
            )
        return checks

    def available(self):
        code, _ = self._preflight()
        return code == "READY"

    def _build_failure(
        self,
        code,
        message,
        files,
        stack,
        revision,
        started,
        *,
        result=None,
        exc=None,
        secrets=(),
        fallback_code=None,
        status=409,
    ):
        diagnostic = compiler_diagnostic(
            getattr(result, "stdout", b"") if result is not None else getattr(exc, "stdout", b""),
            getattr(result, "stderr", b"") if result is not None else getattr(exc, "stderr", b""),
            getattr(result, "returncode", None),
            files if isinstance(files, dict) else {},
            duration_ms=round((time.monotonic() - started) * 1000),
            secrets=secrets,
            environment=getattr(self, "_environment", None),
            resources=getattr(self, "_detail", None),
            fallback_code=fallback_code,
        )
        if fallback_code in {"PROJECT_VALIDATION_FAILED", "DEPENDENCY_OUTSIDE_CATALOG"}:
            item = {"code": fallback_code, "message": message}
            if fallback_code == "DEPENDENCY_OUTSIDE_CATALOG" and "pyproject.toml" in files:
                item["path"] = "pyproject.toml"
            diagnostic["issues"] = [normalized_issue(item, files if isinstance(files, dict) else {}, secrets)]
            diagnostic["category"] = (
                "source" if fallback_code == "PROJECT_VALIDATION_FAILED" else "dependency"
            )
        fields = {}
        if diagnostic["category"] == "executor":
            fields.update(
                {
                    key: getattr(self, "_detail", {}).get(key)
                    for key in ("resources_before", "resources_after", "resource_evidence")
                }
            )
        if exc is not None:
            fields["exception_type"] = type(exc).__name__
            if isinstance(exc, OSError):
                fields["errno"] = exc.errno
        emit(
            "build_failed",
            "error",
            error_code=code,
            stage="publication_building",
            stack=stack,
            revision_id=revision,
            build=diagnostic,
            exit_code=diagnostic["exit_code"],
            duration_ms=diagnostic["duration_ms"],
            stdout_bytes=diagnostic["stdout_bytes"],
            stderr_bytes=diagnostic["stderr_bytes"],
            **fields,
        )
        return BuildFailure(code, message, diagnostic, status=status)

    def build(self, files, stack, revision, secrets=()):
        started = time.monotonic()
        try:
            validate_files(files, stack, secrets)
        except RuleError as exc:
            raise self._build_failure(
                exc.code,
                exc.message,
                files,
                stack,
                revision,
                started,
                secrets=secrets,
                fallback_code="PROJECT_VALIDATION_FAILED",
                status=exc.status,
            ) from None
        if stack == "fastapi-python-d1":
            try:
                config = tomllib.loads(files["pyproject.toml"])
                dependencies = config["project"]["dependencies"]
                approved = {
                    "fastapi",
                    "fastapi==0.162.0",
                    "workers-runtime-sdk",
                    "workers-runtime-sdk==1.9.5",
                }
                if (
                    not isinstance(dependencies, list)
                    or not dependencies
                    or any(item not in approved for item in dependencies)
                ):
                    raise ValueError()
                if "build-system" in config or config.get("tool", {}).get("uv", {}).get("sources"):
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                raise self._build_failure(
                    "UNSUPPORTED_DEPENDENCY",
                    "Use somente as dependências Python do catálogo aprovado; instalação dinâmica não é permitida.",
                    files,
                    stack,
                    revision,
                    started,
                    secrets=secrets,
                    fallback_code="DEPENDENCY_OUTSIDE_CATALOG",
                ) from None
        if not self.available():
            emit("build_executor_unavailable", "error", **getattr(self, "_detail", {}))
            raise self._build_failure(
                "EXECUTOR_UNAVAILABLE",
                "O executor isolado ainda não está disponível. Consulte o responsável.",
                files,
                stack,
                revision,
                started,
                secrets=secrets,
                fallback_code="EXECUTOR_UNAVAILABLE",
            )
        with tempfile.TemporaryDirectory(prefix="viracase-build-") as root:
            source = Path(root)
            for name, content in files.items():
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            command = [
                sys.executable,
                "-m",
                "viracase.infrastructure.build_limits",
                str(source),
                stack,
                revision,
            ]
            started = time.monotonic()
            resources_before = resource_snapshot()
            emit("build_started", stage="publication_building", stack=stack, revision_id=revision)
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=45,
                    env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).parents[2])},
                )
            except subprocess.TimeoutExpired as exc:
                raise self._build_failure(
                    "BUILD_TIMEOUT",
                    "A preparação excedeu o limite do executor.",
                    files,
                    stack,
                    revision,
                    started,
                    exc=exc,
                    secrets=secrets,
                    fallback_code="BUILD_TIMEOUT",
                ) from None
            except OSError as exc:
                raise self._build_failure(
                    "EXECUTOR_UNAVAILABLE",
                    "O processo de build não iniciou. Consulte o diagnóstico do executor.",
                    files,
                    stack,
                    revision,
                    started,
                    exc=exc,
                    secrets=secrets,
                    fallback_code="EXECUTOR_UNAVAILABLE",
                ) from None
            self._capture(
                "publication_building",
                started,
                result=result,
                resources_before=resources_before,
                include_stderr=False,
            )
            if result.returncode or len(result.stdout) > 64_000_000:
                raise self._build_failure(
                    "BUILD_FAILED",
                    "A preparação não foi concluída. Consulte os detalhes de compilação desta tentativa.",
                    files,
                    stack,
                    revision,
                    started,
                    result=result,
                    secrets=secrets,
                    fallback_code="BUILD_OUTPUT_LIMIT" if len(result.stdout) > 64_000_000 else None,
                )
            try:
                built = json.loads(result.stdout)
                if (
                    not isinstance(built, dict)
                    or not isinstance(built.get("modules"), dict)
                    or not isinstance(built.get("main_module"), str)
                    or built["main_module"] not in built["modules"]
                ):
                    raise ValueError("invalid_artifact")
            except (ValueError, TypeError, RecursionError):
                raise self._build_failure(
                    "BUILD_FAILED",
                    "O executor não retornou um artefato válido.",
                    files,
                    stack,
                    revision,
                    started,
                    result=result,
                    secrets=secrets,
                    fallback_code="BUILD_OUTPUT_INVALID",
                ) from None
        artifact = {
            **built,
            "revision_id": revision,
            "assets": {k[7:]: v for k, v in files.items() if k.startswith("public/")},
            "migrations": {
                k: v for k, v in files.items() if k.startswith("migrations/") and k.endswith(".sql")
            },
        }
        emit("build_completed", stage="publication_building", stack=stack, revision_id=revision, duration_ms=round((time.monotonic() - started) * 1000))
        return {**artifact, "digest": canonical_hash(artifact)}
