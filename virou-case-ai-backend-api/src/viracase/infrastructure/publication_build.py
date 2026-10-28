import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from hashlib import sha256
from pathlib import Path

from viracase.domain.rules import RuleError, canonical_hash, validate_files

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
                "publication_check.py",
                "build_limits.py",
                "cloudflare_gateway.py",
                "publication_worker.py",
                "publication_migrations.py",
                "publication_lifecycle.py",
            )
        )
        try:
            return canonical_hash({path.name: sha256(path.read_bytes()).hexdigest() for path in paths})
        except OSError:
            return None

    def supports(self, stack):
        return stack == "hono-typescript-d1" or (
            stack == "fastapi-python-d1" and (TOOLCHAIN / "python-vendor.json").is_file()
        )

    def diagnostics(self):
        digest = self.fingerprint()
        available = self.available()
        return {
            "available": available and bool(digest),
            "fingerprint": digest,
            "stacks": [
                stack for stack in ("hono-typescript-d1", "fastapi-python-d1") if self.supports(stack)
            ],
            "code": "READY" if available and digest else "EXECUTOR_UNAVAILABLE",
            "message": "Executor isolado disponível."
            if available and digest
            else "Confira a imagem/toolchain e as permissões de namespaces, seccomp e AppArmor do worker. Consulte ENTREGA.md.",
        }

    def verify_isolation(self):
        with tempfile.TemporaryDirectory(prefix="viracase-probe-") as root:
            source = Path(root) / "project"
            source.mkdir()
            (Path(root) / "private-marker").write_text("synthetic-isolation-marker")
            (source / "probe.json").write_text(json.dumps({"outside": str(Path(root) / "private-marker")}))
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
                checks = json.loads(result.stdout) if result.returncode == 0 else {}
            except (OSError, ValueError, subprocess.TimeoutExpired):
                checks = {}
        required = {
            "no_network",
            "no_host_secrets",
            "no_cross_project_files",
            "read_only_sources",
            "resource_limits",
            "no_nested_userns",
        }
        if set(checks) != required or not all(value is True for value in checks.values()):
            raise RuleError(
                "EXECUTOR_UNAVAILABLE", "O executor não comprovou todos os controles de isolamento."
            )
        return checks

    def available(self):
        if (
            not shutil.which("bwrap")
            or not Path("/usr/bin/node").is_file()
            or not (TOOLCHAIN / "build.mjs").is_file()
            or not (TOOLCHAIN / "node_modules/esbuild").is_dir()
        ):
            return False
        try:
            run = subprocess.run(
                [
                    "bwrap",
                    "--unshare-all",
                    "--disable-userns",
                    "--die-with-parent",
                    "--new-session",
                    "--ro-bind",
                    "/usr",
                    "/usr",
                    "--ro-bind",
                    "/lib",
                    "/lib",
                    "--ro-bind",
                    "/lib64",
                    "/lib64",
                    "--proc",
                    "/proc",
                    "--dev",
                    "/dev",
                    "/usr/bin/true",
                ],
                capture_output=True,
                timeout=5,
                env={"PATH": "/usr/bin:/bin"},
            )
            return run.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def build(self, files, stack, revision, secrets=()):
        validate_files(files, stack, secrets)
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
                raise RuleError(
                    "UNSUPPORTED_DEPENDENCY",
                    "Use somente as dependências Python do catálogo aprovado; instalação dinâmica não é permitida.",
                ) from None
        if not self.available():
            raise RuleError(
                "EXECUTOR_UNAVAILABLE",
                "O executor isolado ainda não está disponível. Consulte o responsável.",
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
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=45,
                    env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).parents[2])},
                )
            except subprocess.TimeoutExpired:
                raise RuleError("BUILD_TIMEOUT", "A preparação excedeu o limite do executor.") from None
            if result.returncode or len(result.stdout) > 64_000_000:
                raise RuleError(
                    "BUILD_FAILED",
                    "O projeto não passou na preparação isolada. Confira imports e dependências do catálogo.",
                )
            try:
                built = json.loads(result.stdout)
            except ValueError:
                raise RuleError("BUILD_FAILED", "O executor não retornou um artefato válido.") from None
        artifact = {
            **built,
            "revision_id": revision,
            "assets": {k[7:]: v for k, v in files.items() if k.startswith("public/")},
            "migrations": {
                k: v for k, v in files.items() if k.startswith("migrations/") and k.endswith(".sql")
            },
        }
        return {**artifact, "digest": canonical_hash(artifact)}
