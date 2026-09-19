"""TEST-003 — the approval-gating suite: the six tests of §9.9, which are
what §18.4 calls "the tests that matter most".

The claim under test is one sentence: **a consequential mutation cannot
occur without a human decision for those exact arguments.** §9.5 builds it
from three independent barriers, and this module proves each of them alone
and all of them composed:

1. **Control flow** — `decide` rule 6 routes a gated step to
   `request_approval`, so `execute_tool` is not reachable until a decision
   exists.
2. **Re-assertion** — `execute_tool` and `ToolRegistry.dispatch` each
   re-check the grant against the arguments about to be sent, so one bug in
   the router is not enough to cause a mutation.
3. **Type level** — every mutating port method demands an `ApprovalToken`,
   and an `ApprovalToken` is only mintable by `ApprovalGate` from a
   persisted `approved` row.

Mapped to §9.9, in its numbering:

| § | Claim | Class |
|---|---|---|
| 1 | a run at a gated step performs no `mock_crm` write | `TestAPausedRunWritesNothing` |
| 2 | `execute_tool` alone refuses with no grant | `TestExecuteToolRefusesWithoutAGrant` |
| 3 | `MailPort.send` is uncallable without a token | `TestMutationIsUncallableWithoutAToken` |
| 4 | rejection is `rejected`, zero effects, named | `TestRejectionIsTheMechanismNotAFailure` |
| 5 | a grant for hash A does not authorise hash B | `TestAGrantForHashADoesNotAuthoriseHashB` |
| 6 | resuming twice sends exactly one email | `TestResumingTwiceSendsExactlyOneEmail` |

**No test here disables the gate.** §18.2 lists the approval gate among the
things deliberately *not* doubled ("never disabled, ever"), so every test
scripts the *human* — a real `approvals` row, written either by the real
`ApprovalService` through the real HTTP-facing decision path or by the real
repository — and never the gate, the token or the dispatcher. The
end-to-end tests run the production composition: `build_driver` (the one
function `wire_runtime` and the evaluation runner call) → `RunService` →
`Executor` → `ApprovalService`, against real PostgreSQL, the real
`ToolRegistry`, the real mock adapters and the real Postgres saver. The
isolation tests reach one barrier at a time with the same real objects.

Evidence is always the durable record — `mock_crm` rows compared before and
after, `tool_calls`, `approvals`, `trace_events` — never the graph's own
report of what it did.

Barrier 3 has a compile-time half that no runtime assertion can reach, so it
is covered the way §9.9 #3 and TEST-003's acceptance criteria say: by mypy
(asserted to run over `app` in CI, and re-run here against calls that omit
the token) *plus* a runtime constructor test.

Not re-proven here, and deliberately: the gate's own refusal matrix
(`tests/test_hitl_gate.py`), the idempotency of the pause
(`tests/test_hitl_request_approval.py`), single-flight resume under
contention (`tests/test_hitl_recovery.py`), the dispatcher's full contract
(`tests/test_tool_dispatch.py`), the bypass scans that keep every mutation
on this path (`tests/test_structure.py`) and the graph's other control paths
(`tests/test_graph_behavior.py`). This module is the safety claim itself,
stated once, end to end.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import yaml
from app.agent.nodes import NodeHandlers, create_initial_state
from app.agent.state import (
    AgentState,
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalState,
    ApprovalStatus,
    Plan,
    PlanStep,
    RunStatus,
    StepStatus,
)
from app.config import PlannerMode, Settings
from app.errors import (
    ApprovalConflictError,
    ApprovalInvalidError,
    ApprovalRequiredError,
    ErrorClass,
    OpsPilotError,
    PolicyViolation,
)
from app.execution.approvals import ApprovalService
from app.execution.executor import ExecutionOutcome, Executor
from app.execution.leases import LeaseConfig, new_worker_id
from app.execution.recovery import CheckpointPhase, LangGraphRunDriver
from app.execution.runs import RunService
from app.execution.runtime import build_driver
from app.integrations.mock import build_mock_adapters, seed_database
from app.integrations.ports import Adapters, CustomerPatch, DraftInput, OutboundMessage
from app.persistence.checkpointing import open_checkpointer
from app.persistence.models import AgentRun, ApprovalRow, ToolCallRow, TraceEvent
from app.persistence.protocols import UnitOfWorkFactory
from app.persistence.session import create_session_factory, unit_of_work
from app.runtime import (
    FixedClock,
    InMemoryCancellationSource,
    SequentialIdGenerator,
    UuidIdGenerator,
)
from app.security import (
    ApprovalGate,
    ApprovalToken,
    canonical_args_hash,
    idempotency_key_for,
)
from app.tools.contracts import REGISTRY, RiskLevel, ToolName
from app.tools.registry import ToolRegistry
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from recovery_harness import migrate_to_head, require_database, settings, uow_factory_for

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent

T0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
TTL = timedelta(hours=1)

#: Requests whose rule-planner skeletons are fixed by AGENT-003/AGENT-004 and
#: pinned by their own tests. Each ends in exactly one gated step, which is
#: the step every test here is about.
DRAFT_AND_SEND = "Draft outreach to lead L-104 and email it."  # s6 = send_email_mock
UPDATE_A_CUSTOMER = "Update customer cust_1 status to active"  # s2 = update_customer
SEND_STEP = "s6"
UPDATE_STEP = "s2"

#: Lead L-104's address in the seed dataset — an RFC 2606 reserved domain, so
#: nothing here could reach a real person even if the mock could send mail.
DANA = "dana@northwind.example"
#: The address a compromised plan would substitute after the human approved.
ELSEWHERE = "not-dana@elsewhere.example"

#: Every `mock_crm` table that records a *consequential* effect. §9.9 #1 names
#: the outbox and the customer table; both are compared row for row, so an
#: in-place update is caught as well as an insert.
CONSEQUENTIAL_TABLES = ("email_outbox", "customers")


# ---------------------------------------------------------------------------
# Fixtures — the real database, the real adapters, the real registry
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def _database() -> None:
    require_database()
    migrate_to_head()


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        settings().database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=20,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
def uow_factory(session_factory: async_sessionmaker[AsyncSession]) -> UnitOfWorkFactory:
    return partial(unit_of_work, session_factory)


@pytest.fixture
async def checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    async with open_checkpointer(settings()) as saver:
        yield saver


@pytest.fixture
async def seeded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """The mock CRM reset to its fixtures before every test, so a before/after
    comparison of whole tables is exact rather than approximate."""
    await seed_database(session_factory, reset=True)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def adapters(session_factory: async_sessionmaker[AsyncSession], clock: FixedClock) -> Adapters:
    return build_mock_adapters(session_factory, clock, SequentialIdGenerator())


@pytest.fixture
def registry(adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock) -> ToolRegistry:
    """The production registry: the nine real implementations over the real
    mock adapters. Nothing between a caller and the outbox is doubled."""
    return ToolRegistry(adapters=adapters, uow_factory=uow_factory, clock=clock)


@pytest.fixture
def handlers(
    registry: ToolRegistry, uow_factory: UnitOfWorkFactory, clock: FixedClock
) -> NodeHandlers:
    return NodeHandlers(registry=registry, uow_factory=uow_factory, clock=clock)


def pinned_settings() -> Settings:
    """The deterministic planner, no probabilistic tool faults, a fixed seed —
    §15.2's recipe, so a gate assertion is never a scheduling accident."""
    return settings().model_copy(
        update={"planner": PlannerMode.RULES, "seed": 1337, "tool_failure_rate": 0.0}
    )


# ---------------------------------------------------------------------------
# The production stack, composed exactly as `wire_runtime` composes it
# ---------------------------------------------------------------------------
@dataclass
class Run:
    """One run's worth of the production stack, plus the evidence readers.

    Nothing on the path from a plan step to `mock_crm` is substituted: the
    graph is `build_driver`'s, the tools are the nine real implementations,
    and a decision is whatever `ApprovalService` durably wrote.
    """

    run_id: uuid.UUID
    driver: LangGraphRunDriver
    executor: Executor
    runs: RunService
    approvals: ApprovalService
    clock: FixedClock
    uow_factory: UnitOfWorkFactory
    session_factory: async_sessionmaker[AsyncSession]
    config: Settings

    # -- driving -------------------------------------------------------------
    async def start(self) -> ExecutionOutcome:
        await self.runs.start_run(self.run_id)
        return await self.executor.schedule(self.run_id)

    async def pending_approval(self) -> ApprovalRow:
        [pending] = [a for a in await self.approval_rows() if a.status is ApprovalStatus.PENDING]
        return pending

    async def decide(self, decision: str, *, reason: str | None = None) -> Any:
        return await self.approvals.decide_approval(
            (await self.pending_approval()).id,
            decision=decision,
            decided_by="operator",
            reason=reason,
        )

    def approval_service(self, **over: Any) -> ApprovalService:
        """A second `ApprovalService` over the same driver — a second worker
        posting the same decision, which is what §9.9 #6 is about."""
        return ApprovalService(
            uow_factory=self.uow_factory,
            driver=self.driver,
            clock=self.clock,
            lease=LeaseConfig.from_settings(self.config),
            ids=UuidIdGenerator(),
            **over,
        )

    # -- evidence: the checkpoint --------------------------------------------
    async def state(self) -> dict[str, Any]:
        return await self.driver.state(self.run_id)

    async def phase(self) -> CheckpointPhase:
        return (await self.driver.inspect(self.run_id)).phase

    # -- evidence: the durable rows ------------------------------------------
    async def run_row(self) -> AgentRun:
        async with self.uow_factory() as uow:
            row = await uow.agent_runs.get(self.run_id)
            await uow.commit()
        assert row is not None
        return row

    async def events(self) -> list[TraceEvent]:
        async with self.uow_factory() as uow:
            rows = await uow.trace_events.list_by_run(self.run_id, limit=1000)
            await uow.commit()
        return rows

    async def kinds(self, *, step_id: str | None = None) -> list[str]:
        return [
            e.kind.value for e in await self.events() if step_id is None or e.step_id == step_id
        ]

    async def tool_calls(self, *, step_id: str | None = None) -> list[ToolCallRow]:
        async with self.uow_factory() as uow:
            rows = await uow.tool_calls.list_by_run(self.run_id)
            await uow.commit()
        return [r for r in rows if step_id is None or r.step_id == step_id]

    async def approval_rows(self) -> list[ApprovalRow]:
        async with self.uow_factory() as uow:
            rows = await uow.approvals.list_by_run(self.run_id)
            await uow.commit()
        return rows

    async def count(self, table: str, where: Mapping[str, Any]) -> int:
        async with self.uow_factory() as uow:
            total = await uow.count_rows(table, dict(where))
            await uow.commit()
        return total


def compose(
    *,
    engine: AsyncEngine,
    checkpointer: AsyncPostgresSaver,
    clock: FixedClock,
    config: Settings,
) -> tuple[LangGraphRunDriver, Executor, RunService, ApprovalService]:
    session_factory = create_session_factory(engine)
    uow_factory = uow_factory_for(engine)
    ids = UuidIdGenerator()
    driver = build_driver(
        config,
        session_factory=session_factory,
        uow_factory=uow_factory,
        checkpointer=checkpointer,
        clock=clock,
        ids=ids,
        cancellation_source=InMemoryCancellationSource(),
    )
    lease = LeaseConfig.from_settings(config)
    executor = Executor(
        uow_factory=uow_factory,
        driver=driver,
        clock=clock,
        lease=lease,
        owner=new_worker_id(ids, label="gating-test"),
        budgets=config.budgets,
    )
    runs = RunService(
        uow_factory=uow_factory,
        settings=config,
        clock=clock,
        ids=ids,
        cancellation_source=InMemoryCancellationSource(),
        executor=executor,
    )
    approvals = ApprovalService(
        uow_factory=uow_factory, driver=driver, clock=clock, lease=lease, ids=ids
    )
    return driver, executor, runs, approvals


@pytest.fixture
def make_run(
    engine: AsyncEngine,
    checkpointer: AsyncPostgresSaver,
    session_factory: async_sessionmaker[AsyncSession],
    clock: FixedClock,
    seeded: None,
) -> Callable[[str], Awaitable[Run]]:
    async def _make(request: str) -> Run:
        config = pinned_settings()
        driver, executor, runs, approvals = compose(
            engine=engine, checkpointer=checkpointer, clock=clock, config=config
        )
        created = await runs.create_run(request)
        return Run(
            run_id=created.run.id,
            driver=driver,
            executor=executor,
            runs=runs,
            approvals=approvals,
            clock=clock,
            uow_factory=uow_factory_for(engine),
            session_factory=session_factory,
            config=config,
        )

    return _make


# ---------------------------------------------------------------------------
# Evidence helpers
# ---------------------------------------------------------------------------
async def snapshot(
    session_factory: async_sessionmaker[AsyncSession], table: str
) -> list[Mapping[str, Any]]:
    """Every row of one `mock_crm` table, whole and ordered.

    Whole rows rather than a count: an approval gate that let an *update*
    through while inserting nothing would pass a count comparison, and
    `update_customer` is exactly such a mutation (§9.1).
    """
    async with session_factory() as session:
        result = await session.execute(
            sa.text(f"SELECT * FROM mock_crm.{table}")  # noqa: S608 - a literal from a fixed tuple
        )
        rows = [dict(r._mapping) for r in result]  # noqa: SLF001 - the documented Row mapping
    return sorted(rows, key=lambda r: str(sorted(r.items(), key=lambda kv: kv[0])))


async def consequential_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, list[Mapping[str, Any]]]:
    return {t: await snapshot(session_factory, t) for t in CONSEQUENTIAL_TABLES}


async def create_run_row(uow_factory: UnitOfWorkFactory) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with uow_factory() as uow:
        await uow.agent_runs.create(
            id=run_id,
            user_request="send the approved email",
            deadline_at=T0 + timedelta(minutes=5),
            status=RunStatus.RUNNING,
        )
        await uow.commit()
    return run_id


async def create_step_row(
    uow_factory: UnitOfWorkFactory, run_id: uuid.UUID, step_id: str, tool: ToolName
) -> uuid.UUID:
    async with uow_factory() as uow:
        step = await uow.execution_steps.create(
            run_id=run_id, step_id=step_id, plan_revision=0, seq=1, tool=tool
        )
        await uow.commit()
        return step.id


async def script_the_human(
    uow_factory: UnitOfWorkFactory,
    *,
    run_id: uuid.UUID,
    step_id: str,
    args: dict[str, Any],
    tool: ToolName = ToolName.SEND_EMAIL_MOCK,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
) -> uuid.UUID:
    """A real decision, written through the real repository: the request the
    node makes, then the conditional `decide` the API performs. The *human* is
    scripted; the gate, the token and the dispatcher are not."""
    async with uow_factory() as uow:
        row = await uow.approvals.create_request(
            run_id=run_id,
            step_id=step_id,
            tool=tool,
            risk=RiskLevel.HIGH,
            title="Send outreach email",
            summary="scripted decision",
            payload_preview={},
            args_hash=canonical_args_hash(args),
            requested_at=T0,
            expires_at=T0 + TTL,
        )
        if status is not ApprovalStatus.PENDING:
            decided = await uow.approvals.decide(
                row.id, status=status, decided_by="operator", decided_at=T0
            )
            assert decided is not None
        await uow.commit()
        return row.id


async def save_a_draft(adapters: Adapters, lead_id: str = "L-104") -> str:
    saved = await adapters.drafts.save(
        DraftInput(
            lead_id=lead_id,
            subject="Hello",
            body="A short note.",
            content_hash="0123456789abcdef0123",
        )
    )
    return saved.draft_id


def gated_state(
    run_id: uuid.UUID,
    *,
    args: dict[str, Any],
    step_id: str = SEND_STEP,
    tool: ToolName = ToolName.SEND_EMAIL_MOCK,
    decision: ApprovalDecision | None = None,
    approval_state: ApprovalState | None = None,
) -> AgentState:
    """A run positioned exactly at `execute_tool` for a gated step.

    Reaching the node this way is the point of §9.9 #2: barrier 1 (the
    router) is stepped over deliberately, so what the assertion sees is
    barrier 2 and barrier 3 answering on their own.
    """
    plan = Plan(
        plan_id=f"p_{run_id.hex[:8]}",
        steps=[PlanStep(step_id=step_id, tool=tool, args=args, status=StepStatus.PENDING)],
    )
    state = create_initial_state(
        run_id=run_id, user_request="send", plan=plan, clock=FixedClock(T0)
    )
    state["current_step_id"] = step_id
    state["status"] = RunStatus.RUNNING
    if approval_state is not None:
        state["approval_state"] = approval_state
    elif decision is not None:
        state["approval_state"] = ApprovalState(decisions={decision.step_id: decision})
    return state


def grant(step_id: str, args: dict[str, Any], *, approval_id: str = "scripted") -> ApprovalDecision:
    return ApprovalDecision(
        approval_id=approval_id,
        step_id=step_id,
        decision=ApprovalDecisionKind.APPROVE,
        args_hash=canonical_args_hash(args),
        decided_by="operator",
        decided_at=T0,
    )


def assert_refused(delta: Mapping[str, Any], *, step_id: str = SEND_STEP) -> Any:
    """What a refusal by `execute_tool` looks like: a terminal
    `policy_violation`, a recorded failed attempt, and no result.

    The node reports rather than raises (§7: a node returns its error into
    `errors` for `recover` to route), so the `PolicyViolation` surfaces as an
    `AgentError` whose class is `policy_violation` and whose recovery is
    `fail` — never retried (§10.1), never routed to a tool.
    """
    assert "tool_results" not in delta, "a refused step must produce no result"
    (err,) = delta["errors"]
    assert err.error_class is ErrorClass.POLICY_VIOLATION
    assert err.recovery is not None and err.recovery.value == "fail"
    assert err.step_id == step_id
    (call,) = delta["tool_calls"]
    assert call.status == "failed"
    assert call.error_class is ErrorClass.POLICY_VIOLATION
    return err


def as_dict(value: object) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value) if isinstance(value, Mapping) else {}


def step_statuses(state: Mapping[str, Any]) -> dict[str, str]:
    return {s["step_id"]: s["status"] for s in as_dict(state.get("plan")).get("steps", [])}


def final_response(state: Mapping[str, Any]) -> dict[str, Any]:
    return as_dict(state.get("final_response"))


# ---------------------------------------------------------------------------
# §9.9 #1 — a run reaching a gated step performs no `mock_crm` write
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("seeded")
class TestAPausedRunWritesNothing:
    """§18.4 #1, "the safety claim": the tables the business cares about are
    byte-for-byte what they were before the run started."""

    async def test_a_run_paused_on_a_send_has_written_nothing_consequential(
        self, make_run: Callable[[str], Awaitable[Run]], session_factory: async_sessionmaker[Any]
    ) -> None:
        r = await make_run(DRAFT_AND_SEND)
        before = await consequential_snapshot(session_factory)
        drafts_before = await snapshot(session_factory, "outreach_drafts")

        assert await r.start() is ExecutionOutcome.PAUSED

        assert await consequential_snapshot(session_factory) == before, (
            "a paused run must not have touched the outbox or a customer record"
        )
        assert await r.count("mock_crm.email_outbox", {"run_id": str(r.run_id)}) == 0

        # The control that makes the assertion mean something: the run really
        # did reach the gate. `save_draft` is an internal artifact write and
        # is deliberately ungated (ADR-008), so it has already happened.
        assert len(await snapshot(session_factory, "outreach_drafts")) == len(drafts_before) + 1
        assert await r.phase() is CheckpointPhase.PAUSED
        assert step_statuses(await r.state())[SEND_STEP] == StepStatus.PENDING

        row = await r.run_row()
        assert (row.status, row.lease_owner) == (RunStatus.AWAITING_APPROVAL, None)
        approval = await r.pending_approval()
        assert (approval.step_id, approval.tool) == (SEND_STEP, ToolName.SEND_EMAIL_MOCK)

        # Nothing was dispatched for the gated step: no attempt, no tool event.
        assert await r.tool_calls(step_id=SEND_STEP) == []
        assert await r.kinds(step_id=SEND_STEP) == ["approval_requested"]

    async def test_a_run_paused_on_a_customer_update_has_changed_no_customer_row(
        self, make_run: Callable[[str], Awaitable[Run]], session_factory: async_sessionmaker[Any]
    ) -> None:
        """The other gated class of §9.1 — a modification of a record the
        business owns. A row-for-row comparison is what catches it: an update
        leaves the row count unchanged."""
        r = await make_run(UPDATE_A_CUSTOMER)
        before = await consequential_snapshot(session_factory)

        assert await r.start() is ExecutionOutcome.PAUSED

        after = await consequential_snapshot(session_factory)
        assert after == before
        assert [c["version"] for c in after["customers"]] == [
            c["version"] for c in before["customers"]
        ], "an approved-looking update must not have bumped a version"

        approval = await r.pending_approval()
        assert (approval.step_id, approval.tool) == (UPDATE_STEP, ToolName.UPDATE_CUSTOMER)
        # The ungated read ran; the gated write did not.
        assert [c.tool for c in await r.tool_calls()] == [ToolName.GET_CUSTOMER]
        assert await r.kinds(step_id=UPDATE_STEP) == ["approval_requested"]


# ---------------------------------------------------------------------------
# §9.9 #2 — `execute_tool` refuses a gated step with no grant
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("seeded")
class TestExecuteToolRefusesWithoutAGrant:
    """Barrier 2 of §9.5, in isolation: "One bug in the router is not
    sufficient to cause a mutation." Every test here calls `execute_tool`
    directly, so the router has already failed by construction."""

    async def test_execute_tool_alone_refuses_a_gated_step_with_no_grant(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        session_factory: async_sessionmaker[Any],
    ) -> None:
        run_id = await create_run_row(uow_factory)
        await create_step_row(uow_factory, run_id, SEND_STEP, ToolName.SEND_EMAIL_MOCK)
        args = {"draft_id": await save_a_draft(adapters), "to_email": DANA}
        before = await consequential_snapshot(session_factory)

        delta = await handlers.execute_tool(gated_state(run_id, args=args))

        err = assert_refused(delta)
        assert "requires approval" in err.message
        assert await consequential_snapshot(session_factory) == before

    async def test_a_rejected_decision_is_not_a_grant(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        session_factory: async_sessionmaker[Any],
    ) -> None:
        run_id = await create_run_row(uow_factory)
        await create_step_row(uow_factory, run_id, SEND_STEP, ToolName.SEND_EMAIL_MOCK)
        args = {"draft_id": await save_a_draft(adapters), "to_email": DANA}
        declined = ApprovalDecision(
            approval_id="scripted",
            step_id=SEND_STEP,
            decision=ApprovalDecisionKind.REJECT,
            args_hash=canonical_args_hash(args),
            decided_by="operator",
            decided_at=T0,
        )
        before = await consequential_snapshot(session_factory)

        delta = await handlers.execute_tool(gated_state(run_id, args=args, decision=declined))

        assert_refused(delta)
        assert await consequential_snapshot(session_factory) == before

    async def test_a_grant_for_another_step_does_not_carry(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        session_factory: async_sessionmaker[Any],
    ) -> None:
        """Approval is bound to a step *and* to its arguments (§9.4); a
        decision recorded for a neighbouring step authorises nothing."""
        run_id = await create_run_row(uow_factory)
        await create_step_row(uow_factory, run_id, SEND_STEP, ToolName.SEND_EMAIL_MOCK)
        args = {"draft_id": await save_a_draft(adapters), "to_email": DANA}
        before = await consequential_snapshot(session_factory)

        delta = await handlers.execute_tool(
            gated_state(run_id, args=args, decision=grant("s5", args))
        )

        assert_refused(delta)
        assert await consequential_snapshot(session_factory) == before

    async def test_the_dispatcher_refuses_a_gated_tool_presented_with_no_token(
        self,
        registry: ToolRegistry,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        session_factory: async_sessionmaker[Any],
    ) -> None:
        """The same barrier at the choke point every tool call passes through
        (§8.5, ADR-024): `dispatch` re-asserts the gate itself, and refuses
        before any I/O — the refusal is still recorded as an attempt that
        never reached the port."""
        run_id = await create_run_row(uow_factory)
        step_uuid = await create_step_row(uow_factory, run_id, SEND_STEP, ToolName.SEND_EMAIL_MOCK)
        args = {"draft_id": await save_a_draft(adapters), "to_email": DANA}
        before = await consequential_snapshot(session_factory)

        with pytest.raises(ApprovalRequiredError) as caught:
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id=SEND_STEP,
                tool_name=ToolName.SEND_EMAIL_MOCK,
                arguments=args,
                attempt=1,
                approval_token=None,
            )

        assert isinstance(caught.value, PolicyViolation)
        assert caught.value.error_class is ErrorClass.POLICY_VIOLATION
        assert await consequential_snapshot(session_factory) == before
        async with uow_factory() as uow:
            rows = await uow.tool_calls.list_by_step(step_uuid)
            await uow.commit()
        (recorded,) = rows
        assert recorded.status.value == "failed"
        assert recorded.adapter is None, "a refusal never reached an adapter (ADR-024)"

    async def test_a_state_grant_without_a_durable_decision_still_refuses(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        session_factory: async_sessionmaker[Any],
    ) -> None:
        """The barriers are independent, which is the whole design (§9.5).
        Here barrier 2 is satisfied — the checkpointed `approval_state` grants
        exactly these arguments — and the mutation still does not happen,
        because no `approved` row exists for the gate to mint from."""
        run_id = await create_run_row(uow_factory)
        await create_step_row(uow_factory, run_id, SEND_STEP, ToolName.SEND_EMAIL_MOCK)
        args = {"draft_id": await save_a_draft(adapters), "to_email": DANA}
        before = await consequential_snapshot(session_factory)

        delta = await handlers.execute_tool(
            gated_state(run_id, args=args, decision=grant(SEND_STEP, args))
        )

        err = assert_refused(delta)
        assert "no approved decision is stored" in err.message
        assert await consequential_snapshot(session_factory) == before

    async def test_a_pending_row_is_not_a_decision(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        session_factory: async_sessionmaker[Any],
    ) -> None:
        """A request is not an answer: an un-decided `pending` row leaves the
        gate with nothing to mint from, however the run got to the node."""
        run_id = await create_run_row(uow_factory)
        await create_step_row(uow_factory, run_id, SEND_STEP, ToolName.SEND_EMAIL_MOCK)
        args = {"draft_id": await save_a_draft(adapters), "to_email": DANA}
        await script_the_human(
            uow_factory,
            run_id=run_id,
            step_id=SEND_STEP,
            args=args,
            status=ApprovalStatus.PENDING,
        )
        before = await consequential_snapshot(session_factory)

        delta = await handlers.execute_tool(
            gated_state(run_id, args=args, decision=grant(SEND_STEP, args))
        )

        assert_refused(delta)
        assert await consequential_snapshot(session_factory) == before


# ---------------------------------------------------------------------------
# §9.9 #3 — `MailPort.send` is uncallable without an `ApprovalToken`
# ---------------------------------------------------------------------------
#: The compile-time half of barrier 3, expressed as source. Each entry is
#: `(source, expected mypy error code or None)`; the `None` entries are the
#: positive controls that keep the assertion honest — a checker that rejected
#: everything would prove nothing.
MYPY_CASES: tuple[tuple[str, str | None], ...] = (
    ("await mail.send(msg, idempotency_key='k')", "call-arg"),
    ("await mail.send(msg, token=token, idempotency_key='k')", None),
    (
        "await customers.update('c', patch, expected_version=1, idempotency_key='k')",
        "call-arg",
    ),
    (
        "await customers.update('c', patch, expected_version=1, token=token, idempotency_key='k')",
        None,
    ),
    ("await mail.send(msg, token=None, idempotency_key='k')", "arg-type"),
    ("await mail.send(msg, token='approved', idempotency_key='k')", "arg-type"),
)

MYPY_PREAMBLE = """\
from app.integrations.ports import CustomerPatch, CustomerPort, MailPort, OutboundMessage
from app.security import ApprovalToken


async def call(
    mail: MailPort,
    customers: CustomerPort,
    msg: OutboundMessage,
    patch: CustomerPatch,
    token: ApprovalToken,
) -> None:
"""


def mypy_available() -> bool:
    from importlib.util import find_spec

    return find_spec("mypy") is not None


def run_mypy(source: Path) -> dict[int, set[str]]:
    """Type-check one file with the project's own configuration and report
    `{line: {error codes}}`. Runs the same checker CI runs, from the same
    root, so the cache `mypy app` already warmed is reused."""
    completed = subprocess.run(  # noqa: S603 - a fixed argv, no shell
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(BACKEND_ROOT / "pyproject.toml"),
            "--no-error-summary",
            "--no-pretty",
            str(source),
        ],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        check=False,
    )
    found: dict[int, set[str]] = {}
    for line in completed.stdout.splitlines():
        parts = line.split(":", 3)
        if len(parts) < 4 or " error: " not in line:
            continue
        code = line.rsplit("[", 1)[-1].rstrip("]") if line.rstrip().endswith("]") else "?"
        found.setdefault(int(parts[1]), set()).add(code)
    return found


@pytest.mark.unit
class TestMutationIsUncallableWithoutAToken:
    """Barrier 3 of §9.5, the structural one: "code that calls
    `MailPort.send(...)` from anywhere — a script, a test, a future endpoint,
    a mistaken refactor — cannot compile a call without a token, and cannot
    obtain a token without a stored human decision for those exact
    arguments."

    Covered as §9.9 #3 and TEST-003's acceptance criteria require: mypy (run
    over `app` in CI, and re-run here over calls that omit the token) plus a
    runtime constructor test.
    """

    def test_the_token_constructor_refuses_everyone_but_the_gate(self) -> None:
        """The runtime half. `ApprovalToken` is an in-process capability whose
        constructor demands a module-private sentinel, so ordinary code —
        including this test — cannot produce one."""
        with pytest.raises(PolicyViolation) as caught:
            ApprovalToken(approval_id="forged", run_id="r", step_id=SEND_STEP, args_hash="deadbeef")
        assert "must be minted by ApprovalGate" in str(caught.value)

        for guess in (None, object(), "mint", True):
            with pytest.raises(PolicyViolation):
                ApprovalToken(
                    approval_id="forged",
                    run_id="r",
                    step_id=SEND_STEP,
                    args_hash="deadbeef",
                    mint=guess,
                )

    def test_a_minted_token_cannot_be_re_aimed(self) -> None:
        """Minting one legitimately does not hand over a template: the frozen
        dataclass refuses mutation, and `replace` re-enters `__post_init__`
        without the sentinel."""
        args = {"draft_id": "d_1", "to_email": DANA}
        token = ApprovalGate.issue(
            approval_id="a_1",
            run_id="r_1",
            step_id=SEND_STEP,
            args=args,
            approved_args_hash=canonical_args_hash(args),
            decision="approve",
        )
        assert token.authorises(run_id="r_1", step_id=SEND_STEP, args=args)

        with pytest.raises(dataclasses.FrozenInstanceError):
            token.args_hash = canonical_args_hash({"draft_id": "d_1", "to_email": ELSEWHERE})  # type: ignore[misc]
        with pytest.raises(PolicyViolation):
            dataclasses.replace(token, step_id="s7")
        assert "mint" not in vars(token), "the sentinel must never reach the instance"

    def test_the_gate_will_not_mint_without_a_stored_decision(self) -> None:
        """The other half of the claim: obtaining a token requires a durable
        human decision. With no record there is nothing to mint from."""
        with pytest.raises(ApprovalRequiredError):
            ApprovalGate.issue_from_persisted(
                None,
                run_id="r_1",
                step_id=SEND_STEP,
                tool=ToolName.SEND_EMAIL_MOCK.value,
                args={"draft_id": "d_1", "to_email": DANA},
                now=T0,
            )

    def test_every_mutating_port_method_demands_a_token_by_signature(self) -> None:
        """The property a future adapter inherits (§19.1): `token` is a
        required keyword-only parameter of every token-taking port method, so
        omitting it is not a runtime oversight but an unmakeable call."""
        from app.integrations import ports

        demanded = {
            (name, method)
            for name, cls in inspect.getmembers(ports, inspect.isclass)
            if name.endswith("Port")
            for method, fn in inspect.getmembers(cls, inspect.isfunction)
            if "token" in inspect.signature(fn).parameters
        }
        assert demanded == {("MailPort", "send"), ("CustomerPort", "update")}
        for name, method in demanded:
            signature = inspect.signature(getattr(getattr(ports, name), method))
            parameter = signature.parameters["token"]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
            assert parameter.default is inspect.Parameter.empty

    def test_mypy_rejects_a_mutating_port_call_that_omits_the_token(self, tmp_path: Path) -> None:
        """The compile-time half, executed. The same checker CI runs over
        `app` is pointed at calls that omit or forge the token; the calls that
        present a real one must type-check clean, or this proves nothing."""
        if not mypy_available():
            pytest.skip("mypy is not installed in this environment (CI installs the dev extra)")

        source = tmp_path / "token_barrier.py"
        lines = [MYPY_PREAMBLE]
        expected: dict[int, str | None] = {}
        for call, code in MYPY_CASES:
            lines.append(f"    {call}\n")
            expected[MYPY_PREAMBLE.count("\n") + len(expected) + 1] = code
        source.write_text("".join(lines), encoding="utf-8")

        found = run_mypy(source)
        for line, code in expected.items():
            if code is None:
                assert line not in found, (
                    f"a call presenting a real token must type-check: line {line}"
                )
            else:
                assert code in found.get(line, set()), (
                    f"mypy must reject line {line} with [{code}]; got {found.get(line)}"
                )

    def test_ci_typechecks_the_application_so_the_barrier_is_enforced(self) -> None:
        """ "asserted via mypy in CI" (§9.9 #3) is only true while CI actually
        runs it, and only while it runs *before* the suite that depends on
        it."""
        workflow = yaml.safe_load(
            (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        commands = [s["run"] for s in workflow["jobs"]["backend"]["steps"] if "run" in s]
        assert any("mypy app" in c for c in commands), (
            "the type-level approval barrier is enforced by `mypy app` in CI"
        )
        assert next(i for i, c in enumerate(commands) if "mypy app" in c) < next(
            i for i, c in enumerate(commands) if "pytest" in c
        )


@pytest.mark.usefixtures("seeded")
class TestTheAdapterRefusesAnythingThatIsNotAToken:
    """The runtime floor under the type barrier: an adapter reached with
    something token-shaped but not a token performs no effect."""

    async def test_the_mail_adapter_refuses_a_forged_token(
        self, adapters: Adapters, session_factory: async_sessionmaker[Any]
    ) -> None:
        draft_id = await save_a_draft(adapters)
        before = await consequential_snapshot(session_factory)

        for forged in (None, "approved", object()):
            with pytest.raises((PolicyViolation, TypeError, AttributeError)):
                await adapters.mail.send(
                    OutboundMessage(draft_id=draft_id, to_email=DANA),
                    token=forged,  # type: ignore[arg-type]
                    idempotency_key="k_forged",
                )
        assert await consequential_snapshot(session_factory) == before

    async def test_omitting_the_token_entirely_is_a_type_error_at_runtime_too(
        self, adapters: Adapters, session_factory: async_sessionmaker[Any]
    ) -> None:
        draft_id = await save_a_draft(adapters)
        before = await consequential_snapshot(session_factory)

        with pytest.raises(TypeError):
            await adapters.mail.send(  # type: ignore[call-arg]
                OutboundMessage(draft_id=draft_id, to_email=DANA),
                idempotency_key="k_missing",
            )
        with pytest.raises(TypeError):
            await adapters.customers.update(  # type: ignore[call-arg]
                "cust_1",
                CustomerPatch(status="active"),
                expected_version=1,
                idempotency_key="k_missing",
            )
        assert await consequential_snapshot(session_factory) == before


# ---------------------------------------------------------------------------
# §9.9 #4 — rejection: `rejected`, zero effects, and a response that names it
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("seeded")
class TestRejectionIsTheMechanismNotAFailure:
    """ "Rejection is not an error path. It is the mechanism working" (§9.6),
    so the run is `rejected`, never `failed` (§5.4, handoff rule 7)."""

    async def test_a_declined_send_ends_rejected_with_zero_effects_and_names_the_step(
        self, make_run: Callable[[str], Awaitable[Run]], session_factory: async_sessionmaker[Any]
    ) -> None:
        r = await make_run(DRAFT_AND_SEND)
        assert await r.start() is ExecutionOutcome.PAUSED
        before = await consequential_snapshot(session_factory)

        await r.decide("reject", reason="Not this quarter.")

        state = await r.state()
        assert RunStatus(str(state["status"])) is RunStatus.REJECTED
        assert state["status_reason"] == "approval_rejected"
        assert step_statuses(state)[SEND_STEP] == StepStatus.REJECTED

        response = final_response(state)
        assert SEND_STEP in response["not_done"]
        assert SEND_STEP not in response["done"]
        assert SEND_STEP not in response["unconfirmed"], (
            "a declined action was never attempted, so it is not an unconfirmed effect"
        )
        assert SEND_STEP in response["summary"] and "rejected" in response["summary"]

        row = await r.run_row()
        assert (row.status, row.status_reason) == (RunStatus.REJECTED, "approval_rejected")
        assert row.final_response is not None
        assert SEND_STEP in row.final_response["not_done"]

        assert await consequential_snapshot(session_factory) == before
        assert await r.count("mock_crm.email_outbox", {"run_id": str(r.run_id)}) == 0
        assert await r.tool_calls(step_id=SEND_STEP) == []
        assert await r.kinds(step_id=SEND_STEP) == ["approval_requested", "approval_rejected"]
        [approval] = await r.approval_rows()
        assert approval.status is ApprovalStatus.REJECTED
        assert approval.decision_reason == "Not this quarter."

    async def test_a_declined_customer_update_leaves_the_record_untouched(
        self, make_run: Callable[[str], Awaitable[Run]], session_factory: async_sessionmaker[Any]
    ) -> None:
        r = await make_run(UPDATE_A_CUSTOMER)
        assert await r.start() is ExecutionOutcome.PAUSED
        before = await consequential_snapshot(session_factory)

        await r.decide("reject", reason="Owner has not signed off.")

        state = await r.state()
        assert RunStatus(str(state["status"])) is RunStatus.REJECTED
        assert step_statuses(state)[UPDATE_STEP] == StepStatus.REJECTED
        assert await consequential_snapshot(session_factory) == before
        assert [c.tool for c in await r.tool_calls()] == [ToolName.GET_CUSTOMER]

    async def test_the_decline_is_auditable_and_no_tool_event_names_the_declined_step(
        self, make_run: Callable[[str], Awaitable[Run]]
    ) -> None:
        """Rejection is auditable: who declined and why survive in the trace,
        and no tool event anywhere in the run names the declined step."""
        r = await make_run(DRAFT_AND_SEND)
        await r.start()
        await r.decide("reject", reason="Wrong audience.")

        events = await r.events()
        rejected = next(e for e in events if e.kind.value == "approval_rejected")
        assert rejected.payload["decided_by"] == "operator"
        assert rejected.payload["reason"] == "Wrong audience."
        assert rejected.status == ApprovalStatus.REJECTED.value
        assert all(e.step_id != SEND_STEP for e in events if e.kind.value.startswith("tool_")), (
            "the declined step was never dispatched, so it has no tool event"
        )


# ---------------------------------------------------------------------------
# §9.9 #5 — a grant for hash A does not authorise arguments hashing to B
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("seeded")
class TestAGrantForHashADoesNotAuthoriseHashB:
    """§9.4's time-of-check/time-of-use gap, closed at all three barriers.

    `A` is what the human read: send draft `d` to Dana. `B` is the same draft
    to a different recipient — the substitution a revised plan, a re-resolved
    `$ref` or a prompt injection would make after the grant.
    """

    @staticmethod
    def _a_and_b(draft_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            {"draft_id": draft_id, "to_email": DANA},
            {"draft_id": draft_id, "to_email": ELSEWHERE},
        )

    async def test_barrier_two_refuses_b_before_anything_durable_is_read(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        session_factory: async_sessionmaker[Any],
    ) -> None:
        run_id = await create_run_row(uow_factory)
        await create_step_row(uow_factory, run_id, SEND_STEP, ToolName.SEND_EMAIL_MOCK)
        a, b = self._a_and_b(await save_a_draft(adapters))
        assert canonical_args_hash(a) != canonical_args_hash(b)
        # The row says `approved`, for A — everything a step-id-only gate
        # would need. The arguments about to be sent are B.
        await script_the_human(uow_factory, run_id=run_id, step_id=SEND_STEP, args=a)
        before = await consequential_snapshot(session_factory)

        delta = await handlers.execute_tool(
            gated_state(run_id, args=b, decision=grant(SEND_STEP, a))
        )

        assert_refused(delta)
        assert await consequential_snapshot(session_factory) == before

    async def test_the_gate_refuses_to_mint_for_arguments_the_human_did_not_see(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        session_factory: async_sessionmaker[Any],
    ) -> None:
        """Barrier 3. Barrier 2 is *satisfied for B* here — the checkpointed
        decision claims B's hash — so only the durable row stands between the
        run and the wrong recipient."""
        run_id = await create_run_row(uow_factory)
        await create_step_row(uow_factory, run_id, SEND_STEP, ToolName.SEND_EMAIL_MOCK)
        a, b = self._a_and_b(await save_a_draft(adapters))
        await script_the_human(uow_factory, run_id=run_id, step_id=SEND_STEP, args=a)
        before = await consequential_snapshot(session_factory)

        delta = await handlers.execute_tool(
            gated_state(run_id, args=b, decision=grant(SEND_STEP, b))
        )

        err = assert_refused(delta)
        assert "does not authorise this call" in err.message
        assert await consequential_snapshot(session_factory) == before

    async def test_the_dispatcher_refuses_a_real_token_presented_with_other_arguments(
        self,
        registry: ToolRegistry,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        session_factory: async_sessionmaker[Any],
    ) -> None:
        """The last barrier, with a genuinely gate-minted token for A in hand
        and B on the wire — the strongest form of the claim, because nothing
        about the token is forged."""
        run_id = await create_run_row(uow_factory)
        step_uuid = await create_step_row(uow_factory, run_id, SEND_STEP, ToolName.SEND_EMAIL_MOCK)
        a, b = self._a_and_b(await save_a_draft(adapters))
        await script_the_human(uow_factory, run_id=run_id, step_id=SEND_STEP, args=a)
        async with uow_factory() as uow:
            record = await uow.approvals.get_approved(run_id, SEND_STEP)
            await uow.commit()
        token = ApprovalGate.issue_from_persisted(
            record,
            run_id=str(run_id),
            step_id=SEND_STEP,
            tool=ToolName.SEND_EMAIL_MOCK.value,
            args=a,
            now=T0,
        )
        before = await consequential_snapshot(session_factory)

        with pytest.raises(ApprovalInvalidError) as caught:
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id=SEND_STEP,
                tool_name=ToolName.SEND_EMAIL_MOCK,
                arguments=b,
                attempt=1,
                approval_token=token,
            )

        assert "args_hash" in caught.value.detail["mismatch"]
        assert not token.authorises(run_id=str(run_id), step_id=SEND_STEP, args=b)
        assert await consequential_snapshot(session_factory) == before

    async def test_the_same_grant_still_authorises_the_arguments_it_was_given_for(
        self,
        registry: ToolRegistry,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        session_factory: async_sessionmaker[Any],
    ) -> None:
        """The positive control. A gate that refused everything would satisfy
        every assertion above and be useless, so A must go through — and be
        the only thing that does."""
        run_id = await create_run_row(uow_factory)
        step_uuid = await create_step_row(uow_factory, run_id, SEND_STEP, ToolName.SEND_EMAIL_MOCK)
        a, _ = self._a_and_b(await save_a_draft(adapters))
        await script_the_human(uow_factory, run_id=run_id, step_id=SEND_STEP, args=a)
        async with uow_factory() as uow:
            record = await uow.approvals.get_approved(run_id, SEND_STEP)
            await uow.commit()
        token = ApprovalGate.issue_from_persisted(
            record,
            run_id=str(run_id),
            step_id=SEND_STEP,
            tool=ToolName.SEND_EMAIL_MOCK.value,
            args=a,
            now=T0,
        )

        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id=SEND_STEP,
            tool_name=ToolName.SEND_EMAIL_MOCK,
            arguments=a,
            attempt=1,
            approval_token=token,
        )

        assert result.output_data["status"] == "sent"
        sent = await snapshot(session_factory, "email_outbox")
        assert [r["to_email"] for r in sent] == [DANA]
        assert sent[0]["approval_id"] == str(record.id)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# §9.9 #6 — resuming twice sends exactly one email
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("seeded")
class TestResumingTwiceSendsExactlyOneEmail:
    """§18.4 #4, the idempotency claim, stated where it matters most: a human
    double-clicking approve, two operators racing, or a worker that died
    between the commit and the resume must all leave one outbox row."""

    @staticmethod
    def _one_send(rows: Sequence[Mapping[str, Any]], run_id: uuid.UUID) -> Mapping[str, Any]:
        mine = [r for r in rows if r["run_id"] == str(run_id)]
        assert len(mine) == 1, f"exactly one effect, always — found {len(mine)}"
        return mine[0]

    async def test_the_same_decision_posted_twice_resumes_once_and_sends_once(
        self, make_run: Callable[[str], Awaitable[Run]], session_factory: async_sessionmaker[Any]
    ) -> None:
        r = await make_run(DRAFT_AND_SEND)
        assert await r.start() is ExecutionOutcome.PAUSED
        approval = await r.pending_approval()

        first = await r.approvals.decide_approval(
            approval.id, decision="approve", decided_by="operator"
        )
        second = await r.approvals.decide_approval(
            approval.id, decision="approve", decided_by="operator"
        )

        assert first.is_winner and not second.is_winner
        assert second.approval.status is ApprovalStatus.APPROVED
        self._one_send(await snapshot(session_factory, "email_outbox"), r.run_id)

        kinds = await r.kinds()
        assert kinds.count("approval_granted") == 1
        assert [(c.attempt, c.status.value) for c in await r.tool_calls(step_id=SEND_STEP)] == [
            (1, "succeeded")
        ]
        assert RunStatus(str((await r.state())["status"])) is RunStatus.COMPLETED

    async def test_concurrent_decisions_have_one_winner_and_one_outbox_row(
        self, make_run: Callable[[str], Awaitable[Run]], session_factory: async_sessionmaker[Any]
    ) -> None:
        """ "Single-flight resume is a consequence of the conditional update,
        not a separate lock" (§9.6) — four workers, one effect."""
        r = await make_run(DRAFT_AND_SEND)
        assert await r.start() is ExecutionOutcome.PAUSED
        approval = await r.pending_approval()
        services = [r.approvals] + [r.approval_service() for _ in range(3)]

        outcomes = await asyncio.gather(
            *(
                s.decide_approval(approval.id, decision="approve", decided_by=f"operator-{i}")
                for i, s in enumerate(services)
            ),
            return_exceptions=True,
        )

        winners = [o for o in outcomes if not isinstance(o, BaseException) and o.is_winner]
        assert len(winners) == 1, "the conditional update admits exactly one decision"
        for other in outcomes:
            if isinstance(other, BaseException):
                # §9.6: "First writer wins, audibly" — a loser is told the row
                # is no longer pending, never silently dropped.
                assert isinstance(other, ApprovalConflictError), repr(other)
                assert isinstance(other, OpsPilotError)
                assert other.error_class is ErrorClass.POLICY_VIOLATION
        self._one_send(await snapshot(session_factory, "email_outbox"), r.run_id)
        assert (await r.kinds()).count("approval_granted") == 1
        assert len(await r.tool_calls(step_id=SEND_STEP)) == 1

    async def test_a_decision_committed_before_the_worker_died_is_replayed_and_sends_once(
        self, make_run: Callable[[str], Awaitable[Run]], session_factory: async_sessionmaker[Any]
    ) -> None:
        """The crash window of §9.6 step 3: the decision is durable, the
        resume never ran. The replay is the *second* resume of the same
        decision, and it is the one that performs the effect — exactly once.
        """
        r = await make_run(DRAFT_AND_SEND)
        assert await r.start() is ExecutionOutcome.PAUSED
        approval = await r.pending_approval()

        class Died(RuntimeError): ...

        async def die() -> None:
            raise Died

        crashing = r.approval_service(before_resume=die, owner=r.approvals.owner)
        with pytest.raises(Died):
            await crashing.decide_approval(approval.id, decision="approve", decided_by="operator")

        # Nothing was sent: the decision is stored, the graph never moved.
        assert await snapshot(session_factory, "email_outbox") == []
        async with r.uow_factory() as uow:
            stored = await uow.approvals.get(approval.id, fresh=True)
            await uow.commit()
        assert stored is not None and stored.status is ApprovalStatus.APPROVED

        replayed = await r.approval_service(owner=r.approvals.owner).decide_approval(
            approval.id, decision="approve", decided_by="operator"
        )

        assert replayed.is_winner, "the replay must finish the work the dead worker committed"
        self._one_send(await snapshot(session_factory, "email_outbox"), r.run_id)
        assert (await r.kinds()).count("approval_granted") == 1
        assert RunStatus(str((await r.state())["status"])) is RunStatus.COMPLETED

    async def test_the_effect_key_is_what_makes_a_second_attempt_a_replay(
        self, make_run: Callable[[str], Awaitable[Run]], session_factory: async_sessionmaker[Any]
    ) -> None:
        """Why one resume cannot become two effects even if a node re-executes:
        the idempotency key is derived from `(run_id, step_id, args_hash)` and
        nothing attempt-dependent (§10.4, ADR-020), so the key the second
        attempt would present is the key the first row already holds."""
        r = await make_run(DRAFT_AND_SEND)
        await r.start()
        await r.decide("approve")

        sent = self._one_send(await snapshot(session_factory, "email_outbox"), r.run_id)
        [call] = await r.tool_calls(step_id=SEND_STEP)
        [approval] = await r.approval_rows()
        assert sent["idempotency_key"] == call.idempotency_key
        assert call.idempotency_key == idempotency_key_for(
            run_id=str(r.run_id), step_id=SEND_STEP, args_hash=approval.args_hash
        )


# ---------------------------------------------------------------------------
# The suite's own guard: nothing above weakened the gate
# ---------------------------------------------------------------------------
#: Names that would mean the gate had been reached around rather than through:
#: its private stages, the token's minting sentinel, or a stub in place of any
#: of them. Compared against identifiers this module actually *uses* — string
#: constants in a test file prove nothing either way.
GATE_INTERNALS = frozenset(
    {
        "_assert_gate",
        "_verify_stored_decision",
        "_issue_approval_token",
        "_check_argument_hygiene",
        "_MINT",
    }
)


@pytest.mark.unit
def test_this_suite_never_disables_the_gate() -> None:
    """§18.2 puts the approval gate among the things deliberately not doubled
    ("never disabled, ever") and §18.3 makes it this row's acceptance
    criterion: "no test may disable the gate". A suite that proved the gate
    works by switching it off would be worse than no suite, so the module's
    own syntax tree is the evidence.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))

    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not used & GATE_INTERNALS, (
        f"TEST-003 exercises the gate through its public path only; found {used & GATE_INTERNALS}"
    )

    passed = {k.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for k in n.keywords}
    assert "requires_approval" not in passed, "a contract's gating flag is never overridden here"
    assert "implementations" not in passed, (
        "the gated tools are the real ones; only the human is scripted (§18.2)"
    )

    parameters = {
        a.arg
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        for a in n.args.args
    }
    assert "monkeypatch" not in parameters, "nothing on the approval path is patched out"

    # And the contracts themselves still declare the two subjects gated (P1).
    gated = {name for name, contract in REGISTRY.items() if contract.requires_approval}
    assert {ToolName.SEND_EMAIL_MOCK, ToolName.UPDATE_CUSTOMER} <= gated
