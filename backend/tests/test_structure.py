"""Tests that enforce architecture rather than behaviour (§18.6).

These are cheap and they prevent whole classes of regression that reviewers
reliably miss.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

APP = Path(__file__).resolve().parent.parent / "app"
#: Anything that could open a socket. Reachable from the mock integration
#: package, any of these would make "send_email_mock cannot send mail" false.
NETWORK_MODULES = frozenset(
    {"socket", "smtplib", "aiosmtplib", "httpx", "requests", "urllib", "urllib3", "http", "ftplib"}
)


def python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_only_config_reads_the_environment() -> None:
    """§17.1 — one settings object, no exceptions. A stray
    os.getenv("ANTHROPIC_API_KEY") is how secrets reach log lines."""
    offenders = []
    for path in python_files(APP):
        if path.name == "config.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "os.environ" in source or "os.getenv" in source:
            offenders.append(str(path.relative_to(APP.parent)))
    assert not offenders, f"these modules must read Settings instead: {offenders}"


def test_mock_integrations_cannot_reach_the_network() -> None:
    """§19.2 — the strongest of the three reasons send_email_mock cannot send
    mail. Applies as soon as the package exists."""
    mock_dir = APP / "integrations" / "mock"
    if not mock_dir.exists():
        pytest.skip("mock adapters not implemented yet (TOOL-001)")
    offenders = {}
    for path in python_files(mock_dir):
        bad = imported_modules(path) & NETWORK_MODULES
        if bad:
            offenders[str(path.relative_to(APP.parent))] = sorted(bad)
    assert not offenders, f"network-capable imports in the mock package: {offenders}"


def test_no_real_integration_adapter_exists_yet() -> None:
    """OPSPILOT_INTEGRATIONS=real refuses to start; nothing should quietly
    appear under integrations/real without the config fuse being revisited."""
    real_dir = APP / "integrations" / "real"
    if real_dir.exists():
        from app.config import IntegrationMode, Settings
        from app.errors import ConfigurationError

        with pytest.raises(ConfigurationError):
            Settings(_env_file=None, OPSPILOT_INTEGRATIONS=IntegrationMode.REAL).validate_runtime()


def test_no_dynamic_execution_of_model_output() -> None:
    """§16.3 — no code path executes planner or tool output."""
    forbidden = {"eval", "exec", "compile", "__import__"}
    offenders = {}
    for path in python_files(APP):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in forbidden
        }
        if found:
            offenders[str(path.relative_to(APP.parent))] = sorted(found)
    assert not offenders, f"dynamic execution is forbidden: {offenders}"


def test_only_runtime_generates_time_ids_and_randomness() -> None:
    """§1.4, §18.2 — `app/runtime.py` is the only sanctioned source of wall
    time, ids and randomness. A stray `datetime.now()`, `uuid4()` or
    `random.random()` elsewhere breaks determinism invisibly: the whole point
    of `Clock`/`IdGenerator`/`SeededRandom` is that every run can be replayed
    against a frozen clock, a fixed id sequence and a fixed seed (FOUND-004).
    """
    exempt = {APP / "runtime.py"}
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP):
        if path in exempt:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("now", "utcnow")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "datetime"
                ):
                    found.add("datetime.now")
                if (isinstance(node.func, ast.Attribute) and node.func.attr == "uuid4") or (
                    isinstance(node.func, ast.Name) and node.func.id == "uuid4"
                ):
                    found.add("uuid4")
        if "random" in imported_modules(path):
            found.add("import random")
        if found:
            offenders[str(path.relative_to(APP.parent))] = sorted(found)
    assert not offenders, f"bypassed injected time/id/randomness (use app.runtime): {offenders}"


def test_the_leaf_modules_stay_leaves() -> None:
    """errors.py and security.py must remain importable from any layer, so
    they may not depend on higher layers."""
    for leaf, allowed in (("errors.py", set()), ("security.py", {"errors"})):
        tree = ast.parse((APP / leaf).read_text(encoding="utf-8"), filename=leaf)
        internal = {
            node.module.split(".")[1]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.")
        }
        assert internal <= allowed, f"{leaf} gained a dependency on {internal - allowed}"


def test_no_orm_query_construction_outside_persistence() -> None:
    """§12, DB-005 — no `select()` or equivalent ORM query construction outside
    `app/persistence/`. Services and other layers must depend exclusively on
    repository protocols, not build queries or execute raw ORM sessions."""
    persistence_dir = APP / "persistence"
    forbidden_symbols = {"select", "insert", "update", "delete"}
    offenders: dict[str, list[str]] = {}

    for path in python_files(APP):
        if persistence_dir in path.parents or path == persistence_dir:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("sqlalchemy") or node.module == "sqlalchemy":
                    for alias in node.names:
                        if alias.name in forbidden_symbols:
                            found.add(f"import {alias.name} from {node.module}")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_symbols:
                    if isinstance(node.func.value, ast.Name) and node.func.value.id in (
                        "sa",
                        "sqlalchemy",
                    ):
                        found.add(f"{node.func.value.id}.{node.func.attr}()")
                elif isinstance(node.func, ast.Name) and node.func.id in forbidden_symbols:
                    found.add(f"{node.func.id}() call")

        if found:
            offenders[str(path.relative_to(APP.parent))] = sorted(found)

    assert not offenders, f"ORM query construction forbidden outside app/persistence: {offenders}"
