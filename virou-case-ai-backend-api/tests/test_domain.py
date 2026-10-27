from datetime import timedelta
from decimal import Decimal
import ast
from pathlib import Path
import pytest
from viracase.domain.rules import (
    Access,
    RuleError,
    choose_stack,
    cost,
    money,
    reserve,
    settle,
    utcnow,
    validate_files,
)
from viracase.infrastructure.templates import starter
from viracase.infrastructure.learning import LearningLibrary, validate_guide


def test_access_exact_boundaries():
    start = utcnow()
    a = Access.activate(start)
    a.authorize("generate", start + timedelta(days=30, microseconds=-1))
    with pytest.raises(RuleError, match="Acesso"):
        a.authorize("generate", start + timedelta(days=30))
    a.authorize("export", start + timedelta(days=32, microseconds=-1))
    with pytest.raises(RuleError) as e:
        a.authorize("export", start + timedelta(days=32))
    assert e.value.code == "RETENTION_EXPIRED"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "not-money", "1000001"])
def test_invalid_money(value):
    with pytest.raises(RuleError):
        money(value)


def test_reserve_and_overrun():
    assert reserve("0.30", "0.10") == Decimal("0.20")
    with pytest.raises(RuleError):
        reserve("0.10", "0.10000001")
    assert settle("1", ".3") == (Decimal(".3"), Decimal(".7"))
    with pytest.raises(RuleError):
        settle("1", "1.00000001")


@pytest.mark.parametrize(
    "path",
    [
        "../token",
        "/tmp/file",
        "a/../b",
        "a\\b",
        ".env",
        "foo/.env.prod",
        "a//b",
        "a/./b",
        "node_modules/x",
        "x.pem",
        "a\x00b",
        "C:/secret",
    ],
)
def test_paths_rejected(path):
    files = starter("hono-typescript-d1")
    files[path] = "untrusted"
    with pytest.raises(RuleError):
        validate_files(files, "hono-typescript-d1")


@pytest.mark.parametrize("stack", ["hono-typescript-d1", "fastapi-python-d1"])
def test_template_contract(stack):
    assert len(validate_files(starter(stack), stack)) == 64


def test_secret_rejected():
    f = starter("hono-typescript-d1")
    f["public/token.txt"] = "sk-" + "X" * 30
    with pytest.raises(RuleError) as e:
        validate_files(f, "hono-typescript-d1")
    assert e.value.code == "SECRET_DETECTED"


def test_stack_guard():
    assert choose_stack("Criar aplicação em Python") == "fastapi-python-d1"
    assert choose_stack("Criar minha aplicação") == "hono-typescript-d1"
    with pytest.raises(RuleError):
        choose_stack("Quero Java Spring")


def test_usage_categories_do_not_double_count_reasoning():
    policy = {
        "input_usd_per_million": "2",
        "cached_usd_per_million": ".1",
        "output_usd_per_million": "10",
        "fx_brl_per_usd": "5",
    }
    assert cost(policy, {"input": 1000, "cached": 500, "output": 100}) == Decimal(".01025")
    with pytest.raises(RuleError):
        cost(policy, {"input": 2, "cached": 3, "output": 1})


def test_domain_has_no_framework_imports():
    root = Path(__file__).parents[1] / "src/viracase/domain"
    prohibited = {"fastapi", "sqlalchemy", "httpx", "pydantic", "viracase.infrastructure"}
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not any((node.module or "").startswith(x) for x in prohibited)
            if isinstance(node, ast.Import):
                assert not any(n.name.startswith(x) for n in node.names for x in prohibited)


def test_fourteen_complete_guides():
    library = LearningLibrary(Path(__file__).parents[1] / "content")
    assert len(library.guides) == 14
    assert {g["category"] for g in library.guides.values()} == {"planning", "context", "quality", "security"}
    broken = dict(next(iter(library.guides.values())))
    broken["security_notes"] = []
    with pytest.raises(ValueError):
        validate_guide(broken)
