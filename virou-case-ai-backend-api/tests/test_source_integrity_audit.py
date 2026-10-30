import importlib.util
from pathlib import Path


FILE = Path(__file__).parents[1] / "script" / "check-source-integrity.py"
SPEC = importlib.util.spec_from_file_location("source_storage_audit", FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def project(system):
    return system["repo"].create_project(
        system["users"][0], "audit", {"name": "Auditoria", "objective": "Exercitar auditoria de revisoes"}
    )


def test_audit_detects_missing_revision_without_modifying_db(system):
    repo = system["repo"]
    item = project(system)
    before = MODULE.check(repo)
    assert before["unavailable"] == 0
    target = repo.sources.path(system["users"][0], item["id"], item["current_revision_id"])
    payload = target.read_bytes()
    target.unlink()
    missing = MODULE.check(repo)
    assert missing["unavailable"] == 1
    assert missing["issues"][0]["code"] == "SOURCE_UNAVAILABLE"
    assert repo.project(system["users"][0], item["id"])["id"] == item["id"]
    target.write_bytes(payload)
    assert MODULE.check(repo)["unavailable"] == 0


def test_source_deletion_tombstone_guards_future_import(system):
    repo = system["repo"]
    item = project(system)
    actor = system["users"][0]
    path = repo.sources.path(actor, item["id"], item["current_revision_id"])
    repo.sources.remove_project(actor, item["id"])
    assert not path.exists()
    assert (repo.sources.root / ".deleted-projects" / actor / item["id"]).exists()
