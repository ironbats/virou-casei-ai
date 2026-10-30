import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest


FILE = Path(__file__).parents[1] / "script" / "source-storage.py"
SPEC = importlib.util.spec_from_file_location("source_storage_upgrade", FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def record(root, payload=None):
    account, project, revision = str(uuid4()), str(uuid4()), str(uuid4())
    path = root / account / project / (revision + ".json")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload or {"src/index.ts": "export const ok = true"}))
    return path


def test_upgrade_copies_sources_without_changing_original(tmp_path):
    previous, target = tmp_path / "old", tmp_path / "persisted"
    old = record(previous)
    before = old.read_bytes()
    assert MODULE.copy_sources(target, [previous]) == 1
    assert (target / old.relative_to(previous)).read_bytes() == before
    assert old.read_bytes() == before
    assert MODULE.copy_sources(target, [previous]) == 0


def test_upgrade_fails_closed_on_conflicting_revision(tmp_path):
    previous, target = tmp_path / "old", tmp_path / "persisted"
    old = record(previous)
    new = target / old.relative_to(previous)
    new.parent.mkdir(parents=True)
    new.write_text('{"src/index.ts":"different"}')
    with pytest.raises(ValueError, match="SOURCE_STORAGE_CONFLICT"):
        MODULE.copy_sources(target, [previous])
    assert new.read_text() == '{"src/index.ts":"different"}'
    assert old.exists()


def test_upgrade_rejects_symlink(tmp_path):
    previous, target = tmp_path / "old", tmp_path / "persisted"
    old = record(previous)
    shortcut = previous / old.relative_to(previous).parts[0] / "linked"
    shortcut.symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="Link simbolico"):
        MODULE.copy_sources(target, [previous])


def test_configured_relative_storage_is_preserved(tmp_path, monkeypatch):
    monkeypatch.delenv("VIRA_SOURCE_ROOT", raising=False)
    (tmp_path / ".env").write_text("VIRA_SOURCE_ROOT=./custom-storage\n")
    assert MODULE.env_source(tmp_path) == (tmp_path / "custom-storage").resolve()
    (tmp_path / ".env").write_text("VIRA_SOURCE_ROOT=var/sources\n")
    assert MODULE.env_source(tmp_path) is None


def test_copies_only_uuid_revision_files(tmp_path):
    previous, target = tmp_path / "old", tmp_path / "persisted"
    old = record(previous)
    (previous / "passwords.txt").write_text("not-for-copy")
    assert MODULE.copy_sources(target, [previous]) == 1
    assert not (target / "passwords.txt").exists()
    assert (target / old.relative_to(previous)).exists()


def test_deleted_project_is_not_restored_from_old_versions(tmp_path):
    previous, target = tmp_path / "old", tmp_path / "persisted"
    old = record(previous)
    account, project = old.relative_to(previous).parts[:2]
    marker = target / ".deleted-projects" / account / project
    marker.parent.mkdir(parents=True)
    marker.touch()
    assert MODULE.copy_sources(target, [previous]) == 0
    assert not (target / old.relative_to(previous)).exists()
    assert old.exists()
