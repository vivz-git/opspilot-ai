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


# ---------------------------------------------------------------------------
# TOOL-002 — the dispatch choke point cannot be bypassed (§8.5, §9.5, §16.2)
# ---------------------------------------------------------------------------
#: Where a mutating port method may legitimately be *called*: the adapters
#: that implement the ports, and the tool implementations the dispatcher
#: invokes (TOOL-003). Nothing else — not a node, not a service, not an
#: endpoint, not the dispatcher itself, which only hands a port to an
#: implementation it resolved. `persistence/` is exempt because it *defines*
#: the `customers` repository the adapters write through; everything above
#: it that touches a customer, draft or mailbox does so through a port.
MUTATING_PORT_CALLERS = ("integrations/", "tools/impl/", "persistence/")

#: The mutation-capable port surface (§19.1). `test_the_mutating_port_surface_
#: is_declared` fails closed if `app.integrations.ports` grows a token-taking
#: method that is not listed here, so a new mutation cannot appear without
#: being added to the bypass scan below.
MUTATING_PORT_METHODS: dict[str, frozenset[str]] = {
    "MailPort": frozenset({"send"}),
    "CustomerPort": frozenset({"update"}),
    "DraftPort": frozenset({"save"}),  # INTERNAL_WRITE (ADR-008): no token, still a mutation
}
MUTATING_METHOD_NAMES = frozenset().union(*MUTATING_PORT_METHODS.values())
MUTATING_PORT_FIELDS = frozenset({"mail", "customers", "drafts"})  # `Adapters` attributes
#: Read paths on the same ports, used by the verifiers (§11.3).
READ_METHOD_NAMES = frozenset({"get", "get_outbox"})


def _rel(path: Path) -> str:
    return path.relative_to(APP).as_posix()


def _under(path: Path, *prefixes: str) -> bool:
    rel = _rel(path)
    return any(rel.startswith(p) for p in prefixes)


def _receiver_names(node: ast.expr) -> list[str]:
    """`adapters.mail` → ["adapters", "mail"]; `mail` → ["mail"]."""
    names: list[str] = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
    return list(reversed(names))


def test_the_mutating_port_surface_is_declared() -> None:
    """Every port method that takes an `ApprovalToken` is in
    `MUTATING_PORT_METHODS`, and every listed token-taking method exists —
    the scan below can only be trusted if this list is complete."""
    import inspect

    from app.integrations import ports

    token_taking: dict[str, set[str]] = {}
    for name, cls in inspect.getmembers(ports, inspect.isclass):
        if not name.endswith("Port"):
            continue
        for method, fn in inspect.getmembers(cls, inspect.isfunction):
            if "token" in inspect.signature(fn).parameters:
                token_taking.setdefault(name, set()).add(method)
    assert token_taking == {"MailPort": {"send"}, "CustomerPort": {"update"}}, (
        f"token-taking port methods changed: {token_taking}; update MUTATING_PORT_METHODS "
        "and the dispatcher's structural tests deliberately"
    )
    for port, methods in token_taking.items():
        assert methods <= MUTATING_PORT_METHODS[port]


def port_bypasses(tree: ast.AST) -> list[str]:
    """Every way a module could reach a mutating port around the dispatcher:
    calling a mutating method on anything port-shaped, calling `.send(...)`
    on anything at all, or referencing a mutation-capable `Adapters` field
    other than as the immediate receiver of one of its *read* methods (the
    verifiers' readback path). Aliasing (`mail = adapters.mail`) and passing
    a port on are references, so they are caught too."""
    found: list[str] = []
    read_receivers: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in READ_METHOD_NAMES
        ):
            read_receivers.add(id(node.func.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            chain = _receiver_names(node.func.value)
            if attr == "send":
                found.append(f"{'.'.join(chain)}.send(...)")
            elif (
                attr in MUTATING_METHOD_NAMES
                and chain
                and (
                    chain[-1] in MUTATING_PORT_FIELDS
                    or any(m in chain[-1].lower() for m in ("port", "adapter", "ctx"))
                )
            ):
                found.append(f"{'.'.join(chain)}.{attr}(...)")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in MUTATING_PORT_FIELDS
            and id(node) not in read_receivers
        ):
            found.append(f"reference to .{node.attr} at line {node.lineno}")
    return sorted(set(found))


def test_no_direct_mutating_port_calls_outside_tool_implementations() -> None:
    """The bypass a future developer adds by accident — `agent → adapters.mail
    .send(...)` — is refused at review time by this scan. Outside the adapters
    and the tool implementations, a mutating port method is never called, and
    a mutation-capable port is never aliased, passed on or referenced except
    as the immediate receiver of one of its *read* methods."""
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP):
        if _under(path, *MUTATING_PORT_CALLERS):
            continue
        found = port_bypasses(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if found:
            offenders[_rel(path)] = found
    assert not offenders, (
        "mutating ports are reached only through ToolRegistry.dispatch → tool "
        f"implementation → port (§8.5): {offenders}"
    )


BYPASS_SNIPPETS = {
    "mail.send": "await deps.adapters.mail.send(msg, token=t, idempotency_key=k)",
    "customers.update": "await deps.adapters.customers.update(cid, patch, token=t)",
    "drafts.save": "await deps.adapters.drafts.save(draft)",
    "alias-then-send": "mail = adapters.mail\nawait mail.send(msg, token=t, idempotency_key=k)",
    "alias-only": "port = adapters.customers",
    "ctx.port.send": "ctx.port.send(msg, token=t, idempotency_key=k)",
    "port-named-receiver": "await mail_port.update(x)",
    "pass-port-on": "send_via(adapters.mail)",
}


@pytest.mark.parametrize("snippet", list(BYPASS_SNIPPETS.values()), ids=list(BYPASS_SNIPPETS))
def test_the_port_bypass_scan_catches_the_bypass(snippet: str) -> None:
    """The scan is only worth having if it fires. Each snippet is a way a
    future node could reach a mutating port around the dispatcher."""
    assert port_bypasses(ast.parse(snippet)), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "record = await adapters.mail.get_outbox(message_id)",  # verifier readback
        "draft = await adapters.drafts.get(draft_id)",
        "customer = await adapters.customers.get(customer_id=cid)",
        "state.update({'a': 1})",  # dict.update is not CustomerPort.update
        "await uow.agent_runs.update_status(run_id, status=s)",
    ],
    ids=[
        "mail.get_outbox",
        "drafts.get",
        "customers.get",
        "dict.update",
        "repository.update_status",
    ],
)
def test_the_port_bypass_scan_allows_read_paths(snippet: str) -> None:
    assert port_bypasses(ast.parse(snippet)) == [], snippet


def test_mock_adapters_are_not_imported_outside_the_integration_package() -> None:
    """`app.integrations.mock` is an implementation detail of `build_adapters`.
    Agent, tool, API and execution code sees ports, never adapters."""
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP):
        if _under(path, "integrations/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = sorted(
            {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("app.integrations.mock")
            }
            | {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
                if alias.name.startswith("app.integrations.mock")
            }
        )
        if found:
            offenders[_rel(path)] = found
    assert not offenders, f"mock adapters imported outside app/integrations: {offenders}"


def test_tool_implementations_are_reached_only_through_the_registry() -> None:
    """Two locks on the implementation layer: `app.tools.impl` (TOOL-003) is
    imported only inside `app/tools/` and by the composition root, and a
    `ToolContext` — which every implementation requires — is constructed only
    by the dispatcher. An implementation therefore cannot be invoked from a
    node, even by someone who imports it."""
    composition_roots = ("main.py",)
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP):
        rel = _rel(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: list[str] = []
        if not (_under(path, "tools/") or rel in composition_roots):
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.startswith("app.tools.impl")
                ):
                    found.append(f"import {node.module}")
        if rel != "tools/registry.py":
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and (
                    (isinstance(node.func, ast.Name) and node.func.id == "ToolContext")
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "ToolContext")
                ):
                    found.append(f"ToolContext(...) at line {node.lineno}")
        if found:
            offenders[rel] = found
    assert not offenders, f"tool implementations reachable around the dispatcher: {offenders}"


def test_approval_checks_have_one_execution_path() -> None:
    """The gate is asserted in exactly the places §9.5 names — the router
    (`app/agent/decide.py`, rule 6, over `ApprovalState.grants`), the
    dispatcher and `execute_tool`'s re-assertion (barrier 2) and the token
    itself — and tokens are issued on exactly one path (HITL-002): the
    minting primitive `ApprovalGate.issue` is called only inside
    `security.py`, and the application's one issuing call,
    `ApprovalGate.issue_from_persisted`, is made only by `execute_tool`. A
    second, independent check is a second place to get it wrong."""
    allowed_to_check = {
        "security.py",
        "agent/state.py",
        "agent/decide.py",
        "tools/registry.py",
        "agent/nodes.py",
    }
    allowed_to_mint = {"security.py"}
    allowed_to_issue = {"agent/nodes.py"}
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP):
        rel = _rel(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                attr = node.func.attr
                chain = _receiver_names(node.func.value)
                if attr == "issue" and chain and chain[-1] == "ApprovalGate":
                    if rel not in allowed_to_mint:
                        found.append(f"ApprovalGate.issue(...) at line {node.lineno}")
                elif attr == "issue_from_persisted" and rel not in allowed_to_issue:
                    found.append(f"ApprovalGate.issue_from_persisted(...) at line {node.lineno}")
                elif attr in ("authorises", "grants") and rel not in allowed_to_check:
                    found.append(f".{attr}(...) at line {node.lineno}")
            elif (
                isinstance(node.func, ast.Name)
                and node.func.id == "canonical_args_hash"
                and rel not in allowed_to_check
            ):
                found.append(f"canonical_args_hash(...) at line {node.lineno}")
        if found:
            offenders[rel] = found
    assert not offenders, f"approval logic duplicated outside the three barriers: {offenders}"


def test_tool_call_rows_are_written_only_by_the_dispatcher() -> None:
    """§10.7, §14.4: every attempt is recorded by the choke point, so no other
    code path may write a `tool_calls` row or a `tool_*` trace event."""
    tool_kinds = {
        "TOOL_STARTED",
        "TOOL_SUCCEEDED",
        "TOOL_FAILED",
        "TOOL_TIMEOUT",
        "TOOL_DUPLICATE_SUPPRESSED",
    }
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP):
        rel = _rel(path)
        if rel == "tools/registry.py" or _under(path, "persistence/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "record_call"
            ):
                found.append(f"record_call(...) at line {node.lineno}")
            if (
                isinstance(node, ast.Attribute)
                and node.attr in tool_kinds
                and _receiver_names(node.value)[-1:] == ["TraceEventKind"]
            ):
                found.append(f"TraceEventKind.{node.attr} at line {node.lineno}")
        if found:
            offenders[rel] = found
    assert not offenders, f"tool attempts recorded outside ToolRegistry.dispatch: {offenders}"


def test_idempotency_keys_are_derived_only_by_the_dispatcher() -> None:
    """ADR-020: one derivation, one scheme. A second call site is a second
    scheme waiting to diverge."""
    offenders = []
    for path in python_files(APP):
        rel = _rel(path)
        if rel in {"security.py", "tools/registry.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "idempotency_key_for"
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"idempotency keys derived outside the dispatcher: {offenders}"


def test_only_execute_tool_invokes_registry_dispatch() -> None:
    """§8.5, ADR-024: ToolRegistry.dispatch is called exclusively from the execute_tool node."""
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP):
        rel = _rel(path)
        if rel in {"tools/registry.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "dispatch"
                and rel != "agent/nodes.py"
            ):
                found.append(f"dispatch(...) at line {node.lineno}")
        if found:
            offenders[rel] = found
    assert not offenders, f"ToolRegistry.dispatch invoked outside execute_tool node: {offenders}"


def test_no_static_interrupt_lists() -> None:
    """ADR-007: pausing is dynamic via interrupt(); interrupt_before/after must be empty."""
    offenders: dict[str, list[str]] = {}
    agent_dir = APP / "agent"
    for path in python_files(agent_dir):
        rel = _rel(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "compile"
            ):
                for kw in node.keywords:
                    if kw.arg in ("interrupt_before", "interrupt_after") and not (
                        isinstance(kw.value, (ast.List, ast.Tuple)) and len(kw.value.elts) == 0
                    ):
                        found.append(f"static {kw.arg} at line {node.lineno}")
        if found:
            offenders[rel] = found
    assert not offenders, f"static interrupt lists forbidden: {offenders}"


def test_api_layer_does_not_call_tool_registry_dispatch() -> None:
    """§13, §8.5: API layer must never invoke ToolRegistry.dispatch()."""
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP / "api"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "dispatch"
            ):
                found.append(f"dispatch(...) at line {node.lineno}")
        if found:
            offenders[_rel(path)] = found
    assert not offenders, f"API layer calls ToolRegistry.dispatch: {offenders}"


def test_api_layer_does_not_perform_orm_queries_directly() -> None:
    """§13.5: API layer must delegate persistence operations to services rather than
    executing SQLAlchemy ORM queries directly."""
    forbidden_sql_calls = {"select", "insert", "delete"}
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP / "api"):
        rel = _rel(path)
        if rel == "api/health.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in forbidden_sql_calls:
                    found.append(f"{node.func.id}(...) at line {node.lineno}")
                elif isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_sql_calls:
                    found.append(f".{node.func.attr}(...) at line {node.lineno}")
        if found:
            offenders[rel] = found
    assert not offenders, f"API endpoints perform direct ORM queries: {offenders}"


def test_api_layer_does_not_directly_call_langgraph_resume() -> None:
    """§9.6, ADR-023: Graph resumption belongs exclusively to ApprovalService;
    the HTTP layer must not invoke LangGraph resume directly."""
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP / "api"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr in ("resume", "ainvoke"):
                    found.append(f".{node.func.attr}(...) at line {node.lineno}")
                elif isinstance(node.func, ast.Name) and node.func.id == "Command":
                    found.append(f"Command(...) at line {node.lineno}")
        if found:
            offenders[_rel(path)] = found
    assert not offenders, f"API layer directly invokes LangGraph resume: {offenders}"


def test_api_layer_delegates_approval_decisions_to_approval_service() -> None:
    """§13.5: decide_approval route handler must delegate to ApprovalService.decide_approval."""
    path = APP / "api" / "approvals.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    decide_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "decide_approval":
            decide_fn = node
            break
    assert decide_fn is not None, "decide_approval endpoint not found in app/api/approvals.py"

    service_delegated = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "decide_approval"
        for n in ast.walk(decide_fn)
    )
    assert service_delegated, "decide_approval handler must call service.decide_approval"


# ---------------------------------------------------------------------------
# HITL-002 — one mint authority, one issuing path, no ORM in the leaf
# (§9.5, §12, ADR-010)
# ---------------------------------------------------------------------------
def token_forgeries(tree: ast.AST) -> list[str]:
    """Every way a module could produce an `ApprovalToken` without the gate:
    calling the constructor, reaching for the module-private `_MINT`
    sentinel (by import or attribute), or allocating an instance around
    `__init__` (`object.__new__(ApprovalToken)`, `ApprovalToken.__new__`).
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.security":
            for alias in node.names:
                if alias.name == "_MINT":
                    found.append(f"import _MINT at line {node.lineno}")
        elif isinstance(node, ast.Name) and node.id == "_MINT":
            found.append(f"_MINT at line {node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr == "_MINT":
            found.append(f"._MINT at line {node.lineno}")
        elif isinstance(node, ast.Call):
            if (isinstance(node.func, ast.Name) and node.func.id == "ApprovalToken") or (
                isinstance(node.func, ast.Attribute) and node.func.attr == "ApprovalToken"
            ):
                found.append(f"ApprovalToken(...) at line {node.lineno}")
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "__new__":
                receiver = _receiver_names(node.func.value)
                first_arg = node.args[0] if node.args else None
                if receiver[-1:] == ["ApprovalToken"] or (
                    isinstance(first_arg, ast.Name) and first_arg.id == "ApprovalToken"
                ):
                    found.append(f"__new__(ApprovalToken) at line {node.lineno}")
    return sorted(set(found))


def test_only_security_can_mint_an_approval_token() -> None:
    """Barrier 3 is structural only if the sentinel and the constructor are
    unreachable from every other module. Before HITL-002 `execute_tool`
    allocated a placeholder token around `__init__` for a validation
    preview; that is exactly the forgery this scan refuses."""
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP):
        rel = _rel(path)
        if rel == "security.py":
            continue
        found = token_forgeries(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if found:
            offenders[rel] = found
    assert not offenders, f"ApprovalToken forged outside ApprovalGate: {offenders}"


FORGERY_SNIPPETS = {
    "constructor": "t = ApprovalToken(approval_id=a, run_id=r, step_id=s, args_hash=h)",
    "constructor-with-sentinel": (
        "ApprovalToken(approval_id=a, run_id=r, step_id=s, args_hash=h, mint=x)"
    ),
    "import-sentinel": "from app.security import _MINT",
    "module-attribute-sentinel": "import app.security as sec\nmint = sec._MINT",
    "object-new": "t = object.__new__(ApprovalToken)",
    "class-new": "t = ApprovalToken.__new__(ApprovalToken)",
}


@pytest.mark.parametrize("snippet", list(FORGERY_SNIPPETS.values()), ids=list(FORGERY_SNIPPETS))
def test_the_token_forgery_scan_catches_the_forgery(snippet: str) -> None:
    assert token_forgeries(ast.parse(snippet)), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "t = ApprovalGate.issue_from_persisted(row, run_id=r, step_id=s, tool=x, args=a, now=n)",
        "def f(token: ApprovalToken | None = None) -> None: ...",
        "assert isinstance(token, ApprovalToken)",
    ],
    ids=["gate-issue", "type-annotation", "isinstance"],
)
def test_the_token_forgery_scan_allows_legitimate_uses(snippet: str) -> None:
    assert token_forgeries(ast.parse(snippet)) == [], snippet


def test_security_imports_nothing_but_the_standard_library_and_errors() -> None:
    """The leaf test above bounds `app.` imports; this bounds everything
    else. `security.py` consumes an `ApprovalRecordProtocol`, never an ORM
    row type, a session or SQLAlchemy — the persistence layer runs the query
    and hands over a record."""
    import sys

    tree = ast.parse((APP / "security.py").read_text(encoding="utf-8"), filename="security.py")
    third_party = {
        name
        for name in imported_modules(APP / "security.py")
        if name not in sys.stdlib_module_names
    }
    assert third_party == {"app"}, f"security.py imports outside the stdlib: {third_party}"
    internal = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.")
    }
    assert internal == {"app.errors"}


def test_the_approved_row_lookup_feeds_only_the_gate() -> None:
    """`ApprovalRepository.get_approved` exists for one caller — the node
    that hands the row to `ApprovalGate.issue_from_persisted`. A second
    caller would be a second place to decide what "approved" means."""
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP):
        rel = _rel(path)
        if _under(path, "persistence/") or rel == "agent/nodes.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = [
            f"get_approved(...) at line {node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_approved"
        ]
        if found:
            offenders[rel] = found
    assert not offenders, f"approved-row lookup outside execute_tool: {offenders}"


#: The one place the graph writes an approval *request* (HITL-003, §9.7): the
#: `request_approval` node and the helper that performs its transaction. Every
#: other function in the modules below is on the token path and must neither
#: write an approval row nor emit an approval event.
APPROVAL_REQUEST_PATH = frozenset({"request_approval", "_persist_approval_request"})
APPROVAL_WRITERS = frozenset({"create_request", "upsert_request", "decide", "supersede"})
APPROVAL_EVENTS = frozenset(
    {
        "APPROVAL_REQUESTED",
        "APPROVAL_GRANTED",
        "APPROVAL_REJECTED",
        "APPROVAL_EXPIRED",
        "APPROVAL_SUPERSEDED",
    }
)


def _approval_mutations(tree: ast.AST) -> list[tuple[int, str]]:
    """Approval-row writes and approval trace events, as `(line, what)`."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in APPROVAL_WRITERS
            and _receiver_names(node.func.value)[-1:] == ["approvals"]
        ):
            found.append((node.lineno, f"approvals.{node.func.attr}(...)"))
        if (
            isinstance(node, ast.Attribute)
            and node.attr in APPROVAL_EVENTS
            and _receiver_names(node.value)[-1:] == ["TraceEventKind"]
        ):
            found.append((node.lineno, f"TraceEventKind.{node.attr}"))
    return found


def _request_path_lines(tree: ast.Module) -> set[int]:
    """The source lines belonging to the request path's functions."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in APPROVAL_REQUEST_PATH
        ):
            lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return lines


def test_token_issuance_never_mutates_approval_state() -> None:
    """HITL-002 bridges a stored decision to a capability; it decides,
    requests, supersedes and traces nothing. Approval rows are written by
    the approval service (HITL-001) and, for requests, by `request_approval`
    (HITL-003) — never by the gate, the router, the dispatcher or
    `execute_tool`. The request path is exempt by function; every other line
    of `agent/nodes.py` is still held to it."""
    offenders: dict[str, list[str]] = {}
    for rel in ("security.py", "agent/nodes.py", "agent/decide.py", "tools/registry.py"):
        path = APP / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        exempt = _request_path_lines(tree) if rel == "agent/nodes.py" else set()
        found = [
            f"{what} at line {line}"
            for line, what in _approval_mutations(tree)
            if line not in exempt
        ]
        if found:
            offenders[rel] = found
    assert not offenders, f"approval state mutated on the token path: {offenders}"


def test_approval_requests_are_made_only_by_request_approval() -> None:
    """HITL-003, §9.7: the graph requests approval in exactly one place. The
    idempotent primitive `ApprovalRepository.upsert_request` and the
    `approval_requested`/`approval_superseded` events it justifies are used
    by `request_approval`'s transaction and nowhere else in the application
    (the persistence package defines the primitive); `create_request` — the
    unconditional insert — is never called by application code, so a
    re-executed node cannot reach a path that inserts twice."""
    request_only = ("upsert_request", "create_request", "APPROVAL_REQUESTED", "APPROVAL_SUPERSEDED")
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP):
        rel = _rel(path)
        if _under(path, "persistence/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        exempt = _request_path_lines(tree) if rel == "agent/nodes.py" else set()
        found = [
            f"{what} at line {line}"
            for line, what in _approval_mutations(tree)
            if any(name in what for name in request_only) and line not in exempt
        ]
        if found:
            offenders[rel] = found
    assert not offenders, f"approval requested outside request_approval: {offenders}"

    nodes = ast.parse((APP / "agent" / "nodes.py").read_text(encoding="utf-8"))
    on_path = {
        what for line, what in _approval_mutations(nodes) if line in _request_path_lines(nodes)
    }
    assert "approvals.upsert_request(...)" in on_path, "the request must go through the upsert"
    assert "TraceEventKind.APPROVAL_REQUESTED" in on_path
    assert "approvals.create_request(...)" not in on_path
    assert not {"approvals.decide(...)", "approvals.supersede(...)"} & on_path, (
        "request_approval never decides or supersedes on its own; the upsert does"
    )
    assert (
        not {
            "TraceEventKind.APPROVAL_GRANTED",
            "TraceEventKind.APPROVAL_REJECTED",
            "TraceEventKind.APPROVAL_EXPIRED",
        }
        & on_path
    ), "decision events belong to the approval service (HITL-001)"


def test_api_layer_is_uninvolved_in_token_minting() -> None:
    """§13, §16.2: the HTTP layer records a human's decision; it never
    holds, mints or forwards the capability that executes it."""
    offenders: dict[str, list[str]] = {}
    for path in python_files(APP / "api"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.security":
                found.extend(
                    f"import {alias.name} at line {node.lineno}"
                    for alias in node.names
                    if alias.name in {"ApprovalGate", "ApprovalToken", "_MINT"}
                )
            elif isinstance(node, ast.Name) and node.id in {"ApprovalGate", "ApprovalToken"}:
                found.append(f"{node.id} at line {node.lineno}")
        if found:
            offenders[_rel(path)] = found
    assert not offenders, f"API layer touches the token: {offenders}"


def test_node_handlers_accept_no_token_or_gate_injection() -> None:
    """The one issuing path cannot be swapped out from the outside: no
    constructor parameter of `NodeHandlers` or `create_agent_graph` names a
    token issuer, a gate or a mint. (The pre-HITL-002 `token_issuer` hook was
    such a parameter — an injectable second authority — and is gone.)"""
    import inspect

    from app.agent.graph import create_agent_graph
    from app.agent.nodes import NodeHandlers

    for fn in (NodeHandlers.__init__, create_agent_graph):
        for name in inspect.signature(fn).parameters:
            lowered = name.lower()
            assert not any(word in lowered for word in ("token", "gate", "mint", "issuer")), (
                f"{fn.__qualname__} accepts an authorisation injection point: {name}"
            )
