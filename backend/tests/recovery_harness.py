"""Shared harness for the DB-007 integration tests (`test_checkpointing.py`,
`test_leases.py`, `test_recovery.py`).

**The graph.** AGENT-002 has not landed, so these tests drive a small
purpose-built LangGraph graph through the *real* Postgres saver, the real
lease repository and the real reconciler. It has the three shapes the
recovery state machine distinguishes — a node that can be killed
mid-execution, a node that performs a keyed mutating effect, and a node that
pauses on `interrupt()` for a human — and carries the same `status` /
`status_reason` channels `AgentState` does, so the reconciler reads it the
way it will read the production graph.

    prepare ──► work ──┬──(needs_approval)──► gate ──► finish ──► END
                       └──────────────────────────────► finish ──► END

**Simulating a dead worker.** A real process crash never runs `finally`
blocks: no lease release, no status write, no further heartbeats. The
smallest faithful simulation is therefore: the worker's graph task is
*cancelled* while a node is blocked (so no checkpoint is written for that
node), its heartbeat task is stopped, and — critically — nothing else is
touched. The lease then expires only because the injected `FixedClock`
moves, exactly as wall time would for a dead process.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from operator import add
from pathlib import Path
from typing import Annotated, Any, TypedDict

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from app.agent.state import RunStatus
from app.config import Settings, get_settings
from app.execution.leases import LeaseConfig, LeaseHeartbeat, UnitOfWorkFactory
from app.persistence.checkpointing import DURABILITY, thread_config
from app.persistence.mock_crm import Company, EmailOutbox, Lead, OutreachDraft
from app.persistence.protocols import UnitOfWork
from app.persistence.session import create_session_factory, unit_of_work
from app.runtime import FixedClock
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parent.parent

T0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
LEASE = LeaseConfig(ttl=timedelta(seconds=30), heartbeat_interval=timedelta(seconds=10))


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def require_database() -> None:
    try:
        with sa.create_engine(
            _sync_url(get_settings().database_url.get_secret_value()),
            connect_args={"connect_timeout": 3},
        ).connect():
            pass
    except OperationalError:
        pytest.skip("no reachable Postgres for this session (DB-007 integration test)")


def migrate_to_head() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url.get_secret_value())
    command.upgrade(config, "head")


def settings() -> Settings:
    return get_settings()


async def make_engine() -> AsyncEngine:
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(settings().database_url.get_secret_value(), pool_pre_ping=True)


def uow_factory_for(engine: AsyncEngine) -> UnitOfWorkFactory:
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    return partial(unit_of_work, factory)


# ---------------------------------------------------------------------------
# Control-plane rows
# ---------------------------------------------------------------------------
async def create_run(
    uow_factory: UnitOfWorkFactory,
    *,
    status: RunStatus = RunStatus.QUEUED,
    now: datetime = T0,
    run_id: uuid.UUID | None = None,
) -> uuid.UUID:
    run_id = run_id or uuid.uuid4()
    async with uow_factory() as uow:
        await uow.agent_runs.create(
            id=run_id,
            user_request="recover me",
            deadline_at=now + timedelta(minutes=5),
            status=status,
        )
        await uow.commit()
    return run_id


async def leave_stale_lease(
    uow_factory: UnitOfWorkFactory,
    run_id: uuid.UUID,
    *,
    owner: str,
    acquired_at: datetime,
    status: RunStatus | None = None,
    expected: tuple[RunStatus, ...] = (RunStatus.QUEUED, RunStatus.RUNNING),
) -> None:
    """Leave the row exactly as a dead worker would: leased by `owner` from
    `acquired_at`, and nothing else touched since."""
    async with uow_factory() as uow:
        claimed = await uow.agent_runs.acquire_lease(
            run_id,
            owner=owner,
            now=acquired_at,
            ttl=LEASE.ttl,
            expected=expected,
            status=status,
        )
        assert claimed is not None, "harness precondition: the lease must be free"
        await uow.commit()


async def read_run(uow_factory: UnitOfWorkFactory, run_id: uuid.UUID) -> Any:
    # Commit the read-only transaction: exiting uncommitted rolls back, which
    # expires every loaded attribute on the detached row (§12, DB-005).
    async with uow_factory() as uow:
        row = await uow.agent_runs.get(run_id)
        assert row is not None
        await uow.commit()
        return row


async def trace_kinds(uow_factory: UnitOfWorkFactory, run_id: uuid.UUID) -> list[tuple[str, Any]]:
    async with uow_factory() as uow:
        events = await uow.trace_events.list_by_run(run_id, limit=1000)
        await uow.commit()
        return [(e.kind.value, e.status) for e in events]


async def trace_events(uow_factory: UnitOfWorkFactory, run_id: uuid.UUID) -> list[Any]:
    async with uow_factory() as uow:
        events = await uow.trace_events.list_by_run(run_id, limit=1000)
        await uow.commit()
        return events


async def seed_draft(uow_factory: UnitOfWorkFactory) -> tuple[str, str]:
    """A `mock_crm` draft to send, so the harness's mutating effect can be a
    *real* `email_outbox` insert guarded by the real `UNIQUE(idempotency_key)`
    (ADR-020) rather than a mocked one. Returns `(draft_id, to_email)`."""
    suffix = uuid.uuid4().hex[:8]
    async with uow_factory() as uow:
        company = Company(
            company_id=f"c_{suffix}", name=f"Northwind {suffix}", domain=f"nw-{suffix}.example"
        )
        await uow.companies.create(company)
        lead = Lead(
            lead_id=f"l_{suffix}",
            company_id=company.company_id,
            full_name="Ada Example",
            title="VP Ops",
            email=f"ada-{suffix}@nw-{suffix}.example",
        )
        await uow.leads.create(lead)
        draft = OutreachDraft(
            draft_id=f"d_{suffix}",
            lead_id=lead.lead_id,
            subject="Hello",
            body="Body",
            content_hash=f"h_{suffix}",
        )
        await uow.outreach_drafts.create(draft)
        await uow.commit()
        return draft.draft_id, lead.email


async def outbox_rows_for(uow_factory: UnitOfWorkFactory, run_id: uuid.UUID) -> list[Any]:
    async with uow_factory() as uow:
        rows = await uow.email_outbox.list_by_run(str(run_id))
        await uow.commit()
        return rows


# ---------------------------------------------------------------------------
# The harness graph
# ---------------------------------------------------------------------------
class HarnessState(TypedDict, total=False):
    run_id: str
    log: Annotated[list[str], add]
    needs_approval: bool
    decision: str | None
    status: str
    status_reason: str | None


@dataclass
class Harness:
    """Counters and switches the nodes consult. `die_in_work` makes the
    first execution of `work` block forever (the test then kills the worker
    task); `effect` is the optional keyed side effect `work` performs
    *before* blocking, which is what proves re-execution is idempotent."""

    #: Node executions per run id. Per run, not per harness: a reconciler
    #: pass over a shared database may legitimately resume *other* orphans
    #: through this graph, and a test must only ever assert on its own run.
    calls_by_run: dict[str, dict[str, int]] = field(default_factory=dict)
    die_in_work: bool = False
    work_started: asyncio.Event = field(default_factory=asyncio.Event)
    effect: Callable[[uuid.UUID], Awaitable[str]] | None = None
    effect_results: list[str] = field(default_factory=list)
    _never: asyncio.Event = field(default_factory=asyncio.Event)

    def calls(self, run_id: uuid.UUID) -> dict[str, int]:
        return self.calls_by_run.get(str(run_id), {"prepare": 0, "work": 0, "finish": 0})

    def _count(self, state: HarnessState, node: str) -> int:
        counts = self.calls_by_run.setdefault(
            state["run_id"], {"prepare": 0, "work": 0, "finish": 0}
        )
        counts[node] += 1
        return counts[node]

    def build(self, checkpointer: AsyncPostgresSaver) -> CompiledStateGraph[Any, Any, Any, Any]:
        async def prepare(state: HarnessState) -> dict[str, Any]:
            self._count(state, "prepare")
            return {"log": ["prepare"], "status": RunStatus.RUNNING.value}

        async def work(state: HarnessState) -> dict[str, Any]:
            attempt = self._count(state, "work")
            if self.effect is not None:
                self.effect_results.append(await self.effect(uuid.UUID(state["run_id"])))
            self.work_started.set()
            if self.die_in_work and attempt == 1:
                await self._never.wait()  # the worker dies here; never returns
            return {"log": ["work"]}

        async def gate(state: HarnessState) -> dict[str, Any]:
            decision = interrupt({"approve": "send the email?"})
            return {"log": [f"gate:{decision}"], "decision": decision}

        async def finish(state: HarnessState) -> dict[str, Any]:
            self._count(state, "finish")
            if state.get("needs_approval") and state.get("decision") != "approve":
                return {
                    "log": ["finish"],
                    "status": RunStatus.REJECTED.value,
                    "status_reason": "approval_rejected",
                }
            return {"log": ["finish"], "status": RunStatus.COMPLETED.value}

        def after_work(state: HarnessState) -> str:
            return "gate" if state.get("needs_approval") else "finish"

        builder: StateGraph[HarnessState] = StateGraph(HarnessState)
        builder.add_node("prepare", prepare)
        builder.add_node("work", work)
        builder.add_node("gate", gate)
        builder.add_node("finish", finish)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "work")
        builder.add_conditional_edges("work", after_work, {"gate": "gate", "finish": "finish"})
        builder.add_edge("gate", "finish")
        builder.add_edge("finish", END)
        # No static interrupt lists (ADR-007): the pause is `interrupt()` in `gate`.
        return builder.compile(checkpointer=checkpointer, interrupt_before=[])


def outbox_effect(
    uow_factory: UnitOfWorkFactory, draft_id: str, to_email: str
) -> Callable[[uuid.UUID], Awaitable[str]]:
    """The harness's protected mutation: one `email_outbox` row keyed by an
    attempt-invariant idempotency key (§10.4). A repeat is turned into
    `duplicate_suppressed` by the database constraint, not by a flag."""

    async def send(run_id: uuid.UUID) -> str:
        idempotency_key = f"{run_id}:s1:args-hash"
        async with uow_factory() as uow:
            try:
                await uow.email_outbox.create(
                    EmailOutbox(
                        outbox_id=f"o_{uuid.uuid4().hex[:8]}",
                        message_id=f"m_{uuid.uuid4().hex[:8]}",
                        draft_id=draft_id,
                        to_email=to_email,
                        subject="Hello",
                        body="Body",
                        idempotency_key=idempotency_key,
                        run_id=str(run_id),
                    )
                )
                await uow.commit()
            except IntegrityError:
                await uow.rollback()
                return "duplicate_suppressed"
            return "sent"

    return send


# ---------------------------------------------------------------------------
# A scripted worker — what API-007's Executor will do around a graph run
# ---------------------------------------------------------------------------
class Ticker:
    """An injectable `sleep` for `LeaseHeartbeat`: the loop only advances when
    the test calls `tick()`, so heartbeats are asserted on, never waited for."""

    def __init__(self) -> None:
        self._wake = asyncio.Event()
        self.sleeps = 0

    async def sleep(self, _seconds: float) -> None:
        self.sleeps += 1
        await self._wake.wait()
        self._wake.clear()

    def tick(self) -> None:
        self._wake.set()


async def never_sleep(_seconds: float) -> None:
    await asyncio.Event().wait()


@dataclass
class Worker:
    owner: str
    run_id: uuid.UUID
    task: asyncio.Task[Any]
    heartbeat: LeaseHeartbeat

    async def die(self) -> None:
        """Process death: the task is torn down, the heartbeat stops, and
        nothing is released or written. See the module docstring."""
        self.task.cancel()
        with suppress(asyncio.CancelledError, Exception):  # dying is the point
            await self.task
        await self.heartbeat.stop()


async def start_worker(
    *,
    uow_factory: UnitOfWorkFactory,
    graph: CompiledStateGraph[Any, Any, Any, Any],
    run_id: uuid.UUID,
    owner: str,
    clock: FixedClock,
    needs_approval: bool = False,
    sleep: Callable[[float], Awaitable[None]] = never_sleep,
) -> Worker:
    async with uow_factory() as uow:
        claimed = await uow.agent_runs.acquire_lease(
            run_id,
            owner=owner,
            now=clock.now(),
            ttl=LEASE.ttl,
            expected=(RunStatus.QUEUED, RunStatus.RUNNING),
            status=RunStatus.RUNNING,
        )
        assert claimed is not None, "harness precondition: the worker must get the lease"
        await uow.commit()
    heartbeat = LeaseHeartbeat(
        uow_factory=uow_factory, run_id=run_id, owner=owner, clock=clock, config=LEASE, sleep=sleep
    )
    heartbeat.start()
    task = asyncio.create_task(
        graph.ainvoke(
            {"run_id": str(run_id), "log": [], "needs_approval": needs_approval},
            thread_config(run_id),
            durability=DURABILITY,
        )
    )
    return Worker(owner=owner, run_id=run_id, task=task, heartbeat=heartbeat)


async def wait_for(predicate: Callable[[], bool], *, attempts: int = 5000) -> None:
    """Poll until `predicate()` holds — a background task's database round
    trip has to actually complete, so this yields for a millisecond at a
    time rather than spinning. Bounded (5 s) so a broken test fails instead
    of hanging. This waits on *I/O completion*, never on the clock the code
    under test uses: that is the injected `FixedClock`, which only moves
    when a test moves it."""
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition not reached")


# ---------------------------------------------------------------------------
# Fault injection for the rollback test
# ---------------------------------------------------------------------------
class FailingTraceEvents:
    def __init__(self, inner: Any, *, fail_kinds: set[str]) -> None:
        self._inner = inner
        self._fail_kinds = fail_kinds

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def append(self, **kwargs: Any) -> Any:
        if kwargs["kind"].value in self._fail_kinds:
            raise RuntimeError("injected trace failure")
        return await self._inner.append(**kwargs)


class FaultyUnitOfWork:
    """Wraps a real unit of work so `trace_events.append` raises for the
    given kinds — the status transition before it must then roll back too."""

    def __init__(self, inner: UnitOfWork, *, fail_kinds: set[str]) -> None:
        self._inner = inner
        self._fail_kinds = fail_kinds

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def trace_events(self) -> Any:
        return FailingTraceEvents(self._inner.trace_events, fail_kinds=self._fail_kinds)


def faulty_uow_factory(inner: UnitOfWorkFactory, *, fail_kinds: set[str]) -> UnitOfWorkFactory:
    class _Ctx(AbstractAsyncContextManager[Any]):
        def __init__(self) -> None:
            self._cm = inner()

        async def __aenter__(self) -> Any:
            uow = await self._cm.__aenter__()
            return FaultyUnitOfWork(uow, fail_kinds=fail_kinds)

        async def __aexit__(self, *exc: Any) -> None:
            await self._cm.__aexit__(*exc)

    return _Ctx


async def checkpointer_for_tests() -> AsyncIterator[AsyncPostgresSaver]:
    from app.persistence.checkpointing import open_checkpointer

    async with open_checkpointer(settings()) as saver:
        yield saver
