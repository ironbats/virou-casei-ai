import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest

TOOLCHAIN = Path(__file__).resolve().parents[1] / "build-toolchain"


def test_lockfile_is_portable_and_matches_the_manifest():
    manifest = json.loads((TOOLCHAIN / "package.json").read_text())
    lock = json.loads((TOOLCHAIN / "package-lock.json").read_text())
    packages = lock["packages"]
    assert lock["lockfileVersion"] == 3
    assert packages[""]["dependencies"] == manifest["dependencies"]
    assert not lock.get("version", "").startswith("file:")
    for location, package in packages.items():
        if not location:
            continue
        path = PurePosixPath(location)
        assert location.startswith("node_modules/"), location
        assert not path.is_absolute() and ".." not in path.parts, location
        assert not package.get("link"), location
        assert package["resolved"].startswith("https://registry.npmjs.org/"), location
        assert package["integrity"].startswith("sha512-"), location
    for name, version in manifest["dependencies"].items():
        assert packages["node_modules/" + name]["version"] == version


def test_lockfile_includes_every_esbuild_platform_binary():
    packages = json.loads((TOOLCHAIN / "package-lock.json").read_text())["packages"]
    compiler = packages["node_modules/esbuild"]
    for name, version in compiler["optionalDependencies"].items():
        binary = packages["node_modules/" + name]
        assert binary["version"] == version == compiler["version"]
        assert binary["optional"]
        assert binary["cpu"] and binary["os"]


@pytest.mark.skipif(
    os.getenv("VIRA_TEST_BUILD_TOOLCHAIN") != "1",
    reason="Defina VIRA_TEST_BUILD_TOOLCHAIN=1 para executar npm ci real com Node/npm e registry acessível.",
)
def test_clean_install_compiles_and_runs_hono_without_changing_lockfile(tmp_path):
    assert shutil.which("npm") and shutil.which("node"), "Node e npm são necessários"
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(TOOLCHAIN / name, tmp_path / name)
    before = hashlib.sha256((tmp_path / "package-lock.json").read_bytes()).hexdigest()
    environment = {key: value for key, value in os.environ.items() if not key.startswith("VIRA_")}
    for command in (
        ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        ["node", "node_modules/esbuild/install.js"],
    ):
        result = subprocess.run(
            command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=180, check=False
        )
        assert result.returncode == 0, result.stdout + result.stderr
    smoke = """
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { build, version } from 'esbuild';
const manifest = JSON.parse(readFileSync('package.json', 'utf8'));
assert.equal(version, manifest.dependencies.esbuild);
assert.equal(JSON.parse(readFileSync('node_modules/hono/package.json', 'utf8')).version, manifest.dependencies.hono);
await build({
  stdin: { contents: 'import { Hono } from "hono"; const app = new Hono(); app.get("/api/health", c => c.json({status:"ok"})); export default app;', resolveDir: process.cwd(), loader: 'ts' },
  bundle: true, platform: 'neutral', format: 'esm', target: 'es2022', outfile: 'smoke.mjs'
});
const { default: app } = await import(pathToFileURL(process.cwd() + '/smoke.mjs').href);
const response = await app.request('/api/health');
assert.equal(response.status, 200);
assert.deepEqual(await response.json(), {status:'ok'});
console.log(JSON.stringify({esbuild:version,hono:manifest.dependencies.hono,status:response.status}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", smoke],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == 200
    assert hashlib.sha256((tmp_path / "package-lock.json").read_bytes()).hexdigest() == before
