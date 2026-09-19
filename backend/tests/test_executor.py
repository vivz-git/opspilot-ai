"""API-007: `Executor` + `RunService` lifecycle ownership (§2.4, §5.4, §6.3,
ADR-004, ADR-023).

Covers:
1. `POST /runs/{id}/start` returns 202 `queued` before the graph runs.
2. A queued run becomes `running` under the executor's lease and the graph
   receives the initial `AgentState` built from the durable row.
3. A finished graph settles its terminal status, `status_reason`,
   `final_response` and one terminal trace event; the lease is released.
4. A graph that raises settles `failed(execution_failed)` with the error.
5. An interrupt settles `awaiting_approval`, releases the lease and ends the
   background task — a pause is not a live worker.
6. The approval decision re-acquires ownership through `ApprovalService` and
   the resumed graph completes; the executor is not re-entered.
7. Cancellation is cooperative: the in-flight effect finishes, the run stays
   `cancelled`, and the executor's settle overwrites nothing.
8. A live lease held elsewhere prevents execution; two executors racing for
   one run execute it once; `schedule` is idempotent per process.
9. A refused heartbeat stops the graph and leaves the row to its new owner.
10. The lifespan reconciles orphans on start-up through the existing
    `Reconciler`, leaves `awaiting_approval` and terminal runs alone, and
    wires the executor so a run started through the API executes.
11. A shutdown mid-run leaves a row the DB-007 reconciler resumes from its
    checkpoint, under the real saver.
12. The production graph's real approval gate pauses the executor and the
    decision drives the run to completion with its final response persisted.

Integration tests run against real PostgreSQL (the lease rules are the
database's); the graph is scripted where the graph's behaviour is not the
point, and real where it is.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from app.agent.graph import create_agent_graph
from app.agent.nodes import NodeHandlers
from app.agent.state import (
    AgentState,
    ApprovalStatus,
    Plan,
    PlanStep,
    RunStatus,
    StepStatus,
    ToolResult,
)
from app.execution.approvals import ApprovalService
from app.execution.executor import REASON_EXECUTION_FAILED, ExecutionOutcome, Executor
from app.execution.recovery import (
    CheckpointInspection,
    CheckpointPhase,
    LangGraphRunDriver,
    Reconciler,
    RecoveryOutcome,
)
from app.execution.runs import RunService
from app.main import create_app
from app.persistence.checkpointing import open_checkpointer
from app.persistence.models import RiskLevel, ToolName, TraceEventSeverity
from app.runtime import FixedClock, InMemoryCancellationSource
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import (
    LEASE,
    T0,
    Harness,
    Ticker,
    create_run,
    leave_stale_lease,
    migrate_to_head,
    never_sleep,
    read_run,
    require_database,
    settings,
    trace_events,
    trace_kinds,
    uow_factory_for,
    wait_for,
)

pytestmark = [pytest.mark.integration]

COMPLETED = CheckpointInspection(
    phase=CheckpointPhase.FINISHED,
    checkpoint_id="ck-1",
    status=RunStatus.COMPLETED,
    final_response={"summary": "done", "done": ["s1"], "not_done": [], "pending": []},
)
PAUSED = CheckpointInspection(phase=CheckpointPhase.PAUSED, checkpoint_id="ck-1", step_id="s1")

StartScript = Callable[[uuid.UUID, dict[str, Any]], Awaitable[CheckpointInspection]]
ResumeScript = Callable[[uuid.UUID, str | None], Awaitable[CheckpointInspection]]


class ScriptedDriver:
    """A `RunDriver` whose `start`/`resume` are scripted by the test: no graph,
    no checkpoint — exactly the outcome under test, deterministically."""

    def __init__(
        self, *, on_start: StartScript | None = None, on_resume: ResumeScript | None = None
    ) -> None:
        self.started: list[tuple[uuid.UUID, dict[str, Any]]] = []
        self.resumed: list[tuple[uuid.UUID, str | None]] = []
        self.snapshots: dict[uuid.UUID, CheckpointInspection] = {}
        self._on_start = on_start
        self._on_resume = on_resume

    async def start(self, run_id: uuid.UUID, state: dict[str, Any]) -> CheckpointInspection:
        self.started.append((run_id, state))
        result = await self._on_start(run_id, state) if self._on_start else COMPLETED
        self.snapshots[run_id] = result
        return result

    async def inspect(self, run_id: uuid.UUID) -> CheckpointInspection:
        return self.snapshots.get(run_id, CheckpointInspection(phase=CheckpointPhase.NONE))

    async def resume(
        self, run_id: uuid.UUID, resume_value: str | None = None
    ) -> CheckpointInspection:
        self.resumed.append((run_id, resume_value))
        result = await self._on_resume(run_id, resume_value) if self._on_resume else COMPLETED
        self.snapshots[run_id] = result
        return result


def finished(status: RunStatus, reason: str | None = None) -> CheckpointInspection:
    return CheckpointInspection(
        phase=CheckpointPhase.FINISHED,
        checkpoint_id="ck-1",
        status=status,
        status_reason=reason,
        final_response={"summary": f"ended {status.value}"},
    )


def executor_for(
    engine: AsyncEngine,
    driver: Any,
    clock: FixedClock,
    owner: str,
    *,
    sleep: Callable[[float], Awaitable[None]] = never_sleep,
) -> Executor:
    return Executor(
        uow_factory=uow_factory_for(engine),
        driver=driver,
        clock=clock,
        lease=LEASE,
        owner=owner,
        budgets=settings().budgets,
        sleep=sleep,
    )


def run_service_for(
    engine: AsyncEngine,
    clock: FixedClock,
    executor: Executor | None,
    cancellation_source: InMemoryCancellationSource | None = None,
) -> RunService:
    return RunService(
        uow_factory=uow_factory_for(engine),
        settings=settings(),
        clock=clock,
        cancellation_source=cancellation_source,
        executor=executor,
    )


@pytest.fixture(scope="module")
def _database() -> None:
    require_database()
    migrate_to_head()


@pytest.fixture
async def engine(_database: None) -> AsyncIterator[AsyncEngine]:
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
async def checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    async with open_checkpointer(settings()) as saver:
        yield saver


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


# ---------------------------------------------------------------------------
# 1. The API returns before the graph runs
# ---------------------------------------------------------------------------
class TestStartIsAsynchronous:
    async def test_start_returns_202_queued_and_the_graph_runs_afterwards(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        gate = asyncio.Event()

        async def blocked_start(_run_id: uuid.UUID, _state: dict[str, Any]) -> CheckpointInspection:
            await gate.wait()
            return COMPLETED

        driver = ScriptedDriver(on_start=blocked_start)
        executor = executor_for(engine, driver, clock, "executor-A")
        service = run_service_for(engine, clock, executor)
        app = create_app(settings=settings(), run_service=service, clock=clock)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/runs", json={"user_request": "Find fintech leads"})
            assert created.status_code == 201
            run_id = uuid.UUID(created.json()["run_id"])

            # The response is the durable created → queued transition and nothing else.
            started = await client.post(f"/runs/{run_id}/start")
            assert started.status_code == 202
            assert started.json()["status"] == "queued"
            assert driver.started == []

            # The background task then takes the lease and enters the graph.
            await wait_for(lambda: len(driver.started) == 1)
            row = await read_run(uow_factory_for(engine), run_id)
            assert (row.status, row.lease_owner) == (RunStatus.RUNNING, "executor-A")
            assert (await client.get(f"/runs/{run_id}")).json()["status"] == "running"

            gate.set()
            await wait_for(lambda: not executor._tasks)  # noqa: SLF001
            assert (await client.get(f"/runs/{run_id}")).json()["status"] == "completed"

        # A second start is refused by the state machine, not re-executed.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            again = await client.post(f"/runs/{run_id}/start")
            assert again.status_code == 409
            assert again.json()["code"] == "run_not_startable"
        assert len(driver.started) == 1


# ---------------------------------------------------------------------------
# 2–5. Lifecycle transitions from a scripted graph
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("_database")
class TestLifecycle:
    async def test_queued_run_becomes_running_and_the_graph_gets_the_row_as_state(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        uow_factory = uow_factory_for(engine)
        seen: dict[str, Any] = {}

        async def observe(run_id: uuid.UUID, state: dict[str, Any]) -> CheckpointInspection:
            seen["row"] = await read_run(uow_factory, run_id)
            return COMPLETED

        driver = ScriptedDriver(on_start=observe)
        executor = executor_for(engine, driver, clock, "executor-A")
        service = run_service_for(engine, clock, executor)
        created = await service.create_run("Find fintech leads in London", metadata={"k": "v"})
        await service.start_run(created.run.id)

        outcome = await executor.schedule(created.run.id)
        assert outcome is ExecutionOutcome.FINISHED

        # While the graph ran, the row was `running` and ours.
        assert (seen["row"].status, seen["row"].lease_owner) == (RunStatus.RUNNING, "executor-A")
        assert seen["row"].lease_expires_at == T0 + LEASE.ttl

        # The initial state is the durable row, not a second source of truth.
        [(run_id, state)] = driver.started
        assert run_id == created.run.id
        assert state["run_id"] == str(created.run.id)
        assert state["user_request"] == "Find fintech leads in London"
        assert state["status"] is RunStatus.CREATED
        assert state["plan"] is None
        assert state["deadline_at"] == created.run.deadline_at
        assert state["metadata"].budgets == settings().budgets
        assert state["metadata"].planner_kind == created.run.planner_kind
        assert state["metadata"].extra == {"k": "v"}

    @pytest.mark.parametrize(
        ("result", "status", "reason", "kind", "severity"),
        [
            (COMPLETED, RunStatus.COMPLETED, None, "run_completed", TraceEventSeverity.INFO),
            (
                finished(RunStatus.REJECTED, "approval_rejected"),
                RunStatus.REJECTED,
                "approval_rejected",
                "run_rejected",
                TraceEventSeverity.INFO,
            ),
            (
                finished(RunStatus.FAILED, "out_of_scope"),
                RunStatus.FAILED,
                "out_of_scope",
                "run_failed",
                TraceEventSeverity.WARNING,
            ),
        ],
    )
    async def test_a_finished_graph_settles_its_terminal_status_faithfully(
        self,
        engine: AsyncEngine,
        clock: FixedClock,
        result: CheckpointInspection,
        status: RunStatus,
        reason: str | None,
        kind: str,
        severity: TraceEventSeverity,
    ) -> None:
        uow_factory = uow_factory_for(engine)

        async def script(_run_id: uuid.UUID, _state: dict[str, Any]) -> CheckpointInspection:
            clock.advance(seconds=3)
            return result

        run_id = await create_run(uow_factory)
        executor = executor_for(engine, ScriptedDriver(on_start=script), clock, "executor-A")

        assert await executor.execute(run_id) is ExecutionOutcome.FINISHED

        row = await read_run(uow_factory, run_id)
        assert (row.status, row.status_reason) == (status, reason)
        assert (row.lease_owner, row.lease_expires_at) == (None, None)
        assert row.finished_at == clock.now()
        assert row.duration_ms == 3_000
        assert row.final_response == result.final_response
        [event] = await trace_events(uow_factory, run_id)
        assert (event.kind.value, event.status, event.severity) == (kind, status.value, severity)
        assert event.payload["status_reason"] == reason

    async def test_a_graph_that_raises_settles_failed_with_execution_failed(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        uow_factory = uow_factory_for(engine)

        async def boom(_run_id: uuid.UUID, _state: dict[str, Any]) -> CheckpointInspection:
            raise RuntimeError("planner exploded")

        run_id = await create_run(uow_factory)
        executor = executor_for(engine, ScriptedDriver(on_start=boom), clock, "executor-A")

        assert await executor.execute(run_id) is ExecutionOutcome.FAILED

        row = await read_run(uow_factory, run_id)
        assert (row.status, row.status_reason) == (RunStatus.FAILED, REASON_EXECUTION_FAILED)
        assert (row.lease_owner, row.lease_expires_at) == (None, None)
        assert row.finished_at == T0
        [event] = await trace_events(uow_factory, run_id)
        assert (event.kind.value, event.severity) == ("run_failed", TraceEventSeverity.ERROR)
        assert event.error["class"] == REASON_EXECUTION_FAILED
        assert "planner exploded" in event.error["detail"]

    async def test_a_graph_that_ends_without_a_terminal_status_is_failed_not_left_running(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        uow_factory = uow_factory_for(engine)
        no_status = CheckpointInspection(phase=CheckpointPhase.FINISHED, checkpoint_id="ck-1")

        async def script(_run_id: uuid.UUID, _state: dict[str, Any]) -> CheckpointInspection:
            return no_status

        run_id = await create_run(uow_factory)
        executor = executor_for(engine, ScriptedDriver(on_start=script), clock, "executor-A")

        assert await executor.execute(run_id) is ExecutionOutcome.FAILED
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.status_reason) == (RunStatus.FAILED, REASON_EXECUTION_FAILED)
        assert row.lease_owner is None

    async def test_an_interrupt_settles_awaiting_approval_releases_the_lease_and_ends_the_task(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        uow_factory = uow_factory_for(engine)

        async def pause(_run_id: uuid.UUID, _state: dict[str, Any]) -> CheckpointInspection:
            return PAUSED

        run_id = await create_run(uow_factory)
        executor = executor_for(engine, ScriptedDriver(on_start=pause), clock, "executor-A")

        task = executor.schedule(run_id)
        assert await task is ExecutionOutcome.PAUSED
        assert task.done()
        assert executor._tasks == {}  # noqa: SLF001 - no worker remains for a paused run

        row = await read_run(uow_factory, run_id)
        assert row.status is RunStatus.AWAITING_APPROVAL
        assert (row.lease_owner, row.lease_expires_at) == (None, None)
        assert row.finished_at is None
        assert await trace_kinds(uow_factory, run_id) == []  # the gate's own events are the node's

    async def test_the_approval_decision_reacquires_ownership_and_resumes_through_the_service(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        uow_factory = uow_factory_for(engine)
        seen: dict[str, Any] = {}

        async def pause(_run_id: uuid.UUID, _state: dict[str, Any]) -> CheckpointInspection:
            return PAUSED

        async def observe_resume(run_id: uuid.UUID, _value: str | None) -> CheckpointInspection:
            seen["row"] = await read_run(uow_factory, run_id)
            return COMPLETED

        driver = ScriptedDriver(on_start=pause, on_resume=observe_resume)
        run_id = await create_run(uow_factory)
        executor = executor_for(engine, driver, clock, "executor-A")
        assert await executor.execute(run_id) is ExecutionOutcome.PAUSED

        approval_id = uuid.uuid4()
        async with uow_factory() as uow:
            await uow.approvals.create_request(
                id=approval_id,
                run_id=run_id,
                step_id="s1",
                tool=ToolName.SEND_EMAIL_MOCK,
                risk=RiskLevel.HIGH,
                title="Send email",
                summary="Send outreach email",
                payload_preview={"draft_id": "d1"},
                args_hash="hash-s1",
                requested_at=clock.now(),
                expires_at=clock.now() + timedelta(days=1),
            )
            await uow.commit()

        approvals = ApprovalService(
            uow_factory=uow_factory, driver=driver, clock=clock, lease=LEASE, owner="approver-B"
        )
        result = await approvals.decide_approval(approval_id, decision="approve", decided_by="op")
        assert result.is_winner
        assert result.status is ApprovalStatus.APPROVED

        # The resume ran under the *approval service's* lease — ownership was
        # re-acquired, not inherited from the executor that paused the run.
        assert (seen["row"].status, seen["row"].lease_owner) == (RunStatus.RUNNING, "approver-B")
        assert seen["row"].lease_expires_at == T0 + LEASE.ttl
        assert driver.resumed == [(run_id, "approve")]
        assert len(driver.started) == 1  # the executor was never re-entered

        row = await read_run(uow_factory, run_id)
        assert row.status is RunStatus.COMPLETED
        assert (row.lease_owner, row.lease_expires_at) == (None, None)
        assert row.final_response == COMPLETED.final_response
        # The decision path settles the run, so it also writes the run's
        # terminal event — `Executor._settle` never sees a resumed graph.
        assert await trace_kinds(uow_factory, run_id) == [
            ("approval_granted", "approved"),
            ("run_completed", "completed"),
        ]


# ---------------------------------------------------------------------------
# 7. Cooperative cancellation
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("_database")
class TestCancellation:
    async def test_cancellation_lets_the_effect_finish_and_never_overwrites_the_cancelled_row(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        uow_factory = uow_factory_for(engine)
        cancellation = InMemoryCancellationSource()
        in_flight = asyncio.Event()
        effect_done = asyncio.Event()
        graph: dict[str, bool] = {"cancelled": False, "finished": False}

        async def tool_in_flight(run_id: uuid.UUID, _state: dict[str, Any]) -> CheckpointInspection:
            in_flight.set()
            try:
                await effect_done.wait()  # the external effect the graph must not abandon
            except asyncio.CancelledError:
                graph["cancelled"] = True
                raise
            graph["finished"] = True
            # The node observed the flag at its next boundary and exited via `fail`.
            assert cancellation.is_cancelled(str(run_id))
            return finished(RunStatus.FAILED, "cancelled")

        driver = ScriptedDriver(on_start=tool_in_flight)
        executor = executor_for(engine, driver, clock, "executor-A")
        service = run_service_for(engine, clock, executor, cancellation)
        created = await service.create_run("Email the leads", auto_start=True)
        run_id = created.run.id
        task = executor._tasks[run_id]  # noqa: SLF001 - auto_start scheduled it

        await in_flight.wait()
        cancelled = await service.cancel_run(run_id, reason="operator changed their mind")
        assert cancelled.status is RunStatus.CANCELLED
        assert not task.done()  # the executor did not kill the in-flight effect

        effect_done.set()
        assert await task is ExecutionOutcome.FINISHED
        assert graph == {"cancelled": False, "finished": True}

        row = await read_run(uow_factory, run_id)
        assert (row.status, row.status_reason) == (
            RunStatus.CANCELLED,
            "operator changed their mind",
        )
        assert (row.lease_owner, row.lease_expires_at) == (None, None)
        kinds = await trace_kinds(uow_factory, run_id)
        assert ("run_cancelled", "cancelled") in kinds
        assert not any(kind == "run_failed" for kind, _ in kinds)

    async def test_a_run_cancelled_while_queued_is_never_executed(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        uow_factory = uow_factory_for(engine)
        driver = ScriptedDriver()
        executor = executor_for(engine, driver, clock, "executor-A")
        service = run_service_for(engine, clock, None)
        created = await service.create_run("Email the leads")
        await service.start_run(created.run.id)
        await service.cancel_run(created.run.id)

        assert await executor.execute(created.run.id) is ExecutionOutcome.NOT_ACQUIRED
        assert driver.started == []
        assert (await read_run(uow_factory, created.run.id)).status is RunStatus.CANCELLED


# ---------------------------------------------------------------------------
# 8–9. Single-owner invariant
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("_database")
class TestOwnership:
    async def test_a_live_lease_held_elsewhere_prevents_execution(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        await leave_stale_lease(uow_factory, run_id, owner="worker-A", acquired_at=clock.now())
        driver = ScriptedDriver()
        executor = executor_for(engine, driver, clock, "executor-B")

        assert await executor.execute(run_id) is ExecutionOutcome.NOT_ACQUIRED
        assert driver.started == []
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.lease_owner) == (RunStatus.QUEUED, "worker-A")

    async def test_two_executors_racing_for_one_run_execute_it_exactly_once(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)

        async def slow(_run_id: uuid.UUID, _state: dict[str, Any]) -> CheckpointInspection:
            for _ in range(20):
                await asyncio.sleep(0)
            return COMPLETED

        driver = ScriptedDriver(on_start=slow)
        a = executor_for(engine, driver, clock, "executor-A")
        b = executor_for(engine, driver, clock, "executor-B")

        outcomes = await asyncio.gather(a.execute(run_id), b.execute(run_id))
        assert sorted(outcomes) == [ExecutionOutcome.FINISHED, ExecutionOutcome.NOT_ACQUIRED]
        assert len(driver.started) == 1
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.lease_owner) == (RunStatus.COMPLETED, None)
        assert await trace_kinds(uow_factory, run_id) == [("run_completed", "completed")]

    async def test_schedule_is_idempotent_while_the_task_is_alive(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        gate = asyncio.Event()

        async def blocked(_run_id: uuid.UUID, _state: dict[str, Any]) -> CheckpointInspection:
            await gate.wait()
            return COMPLETED

        driver = ScriptedDriver(on_start=blocked)
        run_id = await create_run(uow_factory_for(engine))
        executor = executor_for(engine, driver, clock, "executor-A")

        first = executor.schedule(run_id)
        assert executor.schedule(run_id) is first
        gate.set()
        assert await first is ExecutionOutcome.FINISHED
        assert len(driver.started) == 1
        # Once finished the run is terminal, so a re-schedule is refused by the lease.
        assert await executor.schedule(run_id) is ExecutionOutcome.NOT_ACQUIRED
        assert len(driver.started) == 1

    @pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.RUNNING])
    async def test_a_run_that_is_not_queued_is_never_entered(
        self, engine: AsyncEngine, clock: FixedClock, status: RunStatus
    ) -> None:
        """Terminal runs are never re-entered; a `running` run with no worker
        is the reconciler's, not a fresh start's."""
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory, status=status)
        driver = ScriptedDriver()
        executor = executor_for(engine, driver, clock, "executor-A")

        assert await executor.execute(run_id) is ExecutionOutcome.NOT_ACQUIRED
        assert driver.started == []
        assert (await read_run(uow_factory, run_id)).status is status

    async def test_a_refused_heartbeat_stops_the_graph_and_leaves_the_row_to_its_new_owner(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        uow_factory = uow_factory_for(engine)
        ticker = Ticker()
        entered = asyncio.Event()
        graph: dict[str, bool] = {"cancelled": False}

        async def forever(_run_id: uuid.UUID, _state: dict[str, Any]) -> CheckpointInspection:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                graph["cancelled"] = True
                raise
            raise AssertionError("unreachable")

        run_id = await create_run(uow_factory)
        driver = ScriptedDriver(on_start=forever)
        executor = executor_for(engine, driver, clock, "executor-A", sleep=ticker.sleep)
        task = executor.schedule(run_id)
        await entered.wait()
        await wait_for(lambda: ticker.sleeps == 1)  # the heartbeat loop is parked on its sleep

        # The lease lapses (the clock moves; nothing else does) and another
        # worker claims the run — as the reconciler would after a crash.
        clock.advance(seconds=LEASE.ttl.total_seconds() + 1)
        async with uow_factory() as uow:
            claimed = await uow.agent_runs.acquire_lease(
                run_id, owner="worker-B", now=clock.now(), ttl=LEASE.ttl
            )
            assert claimed is not None
            await uow.commit()

        # A's next heartbeat is refused: it stops driving the graph at once.
        ticker.tick()
        assert await task is ExecutionOutcome.LEASE_LOST
        assert graph["cancelled"] is True

        # Nothing of A's touched the row, which is B's now: still `running`, B's lease intact.
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.lease_owner) == (RunStatus.RUNNING, "worker-B")
        assert row.lease_expires_at == clock.now() + LEASE.ttl
        assert await trace_kinds(uow_factory, run_id) == []


# ---------------------------------------------------------------------------
# 10. Start-up reconciliation through the lifespan
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("_database")
class TestStartupReconciliation:
    async def test_the_lifespan_reconciles_orphans_and_only_orphans_then_serves_runs(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        uow_factory = uow_factory_for(engine)
        orphan = await create_run(uow_factory, status=RunStatus.QUEUED)
        expired = await create_run(uow_factory, status=RunStatus.RUNNING)
        await leave_stale_lease(
            uow_factory, expired, owner="dead-worker", acquired_at=T0 - timedelta(hours=1)
        )
        paused = await create_run(uow_factory, status=RunStatus.AWAITING_APPROVAL)
        done = await create_run(uow_factory, status=RunStatus.COMPLETED)
        live = await create_run(uow_factory, status=RunStatus.RUNNING)
        await leave_stale_lease(uow_factory, live, owner="live-worker", acquired_at=clock.now())

        driver = ScriptedDriver()  # no checkpoint for anyone → orphans fail as `orphaned`
        app = create_app(settings=settings(), driver=driver, clock=clock)

        async with app.router.lifespan_context(app):
            # The existing Reconciler ran on start-up over exactly the orphan query.
            assert isinstance(app.state.reconciler, Reconciler)
            for run_id in (orphan, expired):
                row = await read_run(uow_factory, run_id)
                assert (row.status, row.status_reason) == (RunStatus.FAILED, "orphaned")
                assert row.lease_owner is None
            assert (await read_run(uow_factory, paused)).status is RunStatus.AWAITING_APPROVAL
            assert (await read_run(uow_factory, done)).status is RunStatus.COMPLETED
            live_row = await read_run(uow_factory, live)
            assert (live_row.status, live_row.lease_owner) == (RunStatus.RUNNING, "live-worker")
            assert driver.resumed == []

            # The same lifespan wired the executor: a run started through the
            # API is executed by it, under its own worker identity.
            assert isinstance(app.state.executor, Executor)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post(
                    "/runs", json={"user_request": "Find fintech leads", "auto_start": True}
                )
                assert created.status_code == 201
                assert created.json()["status"] == "queued"
                run_id = uuid.UUID(created.json()["run_id"])
                await wait_for(lambda: bool(driver.started))
                await wait_for(lambda: not app.state.executor._tasks)  # noqa: SLF001
                shown = (await client.get(f"/runs/{run_id}")).json()
                assert shown["status"] == "completed"
                assert shown["final_response"]["summary"] == "done"
            assert driver.started[0][0] == run_id
            assert app.state.executor.owner.startswith("executor-")
            assert app.state.reconciler.owner.startswith("reconciler-")
            assert app.state.reconciler.owner != app.state.executor.owner

    async def test_start_up_drains_more_orphans_than_one_page_holds(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        """A killed process leaves no run permanently `running`, however many
        it was driving: the start-up drain pages through the orphan query
        until a pass comes back short."""
        uow_factory = uow_factory_for(engine)
        orphans = [await create_run(uow_factory, status=RunStatus.QUEUED) for _ in range(7)]
        reconciler = Reconciler(
            uow_factory=uow_factory,
            driver=ScriptedDriver(),
            clock=clock,
            owner="reconciler-B",
            lease=LEASE,
        )

        report = await reconciler.reconcile_all(page=3)

        assert set(orphans) <= set(report.outcomes)
        assert all(report.outcomes[run_id] is RecoveryOutcome.ORPHANED for run_id in orphans)
        for run_id in orphans:
            assert (await read_run(uow_factory, run_id)).status is RunStatus.FAILED
        # Drained: a further pass has nothing left.
        assert (await reconciler.reconcile_once(limit=500)).candidates == 0

    async def test_a_second_start_up_finds_nothing_to_reconcile(
        self, engine: AsyncEngine, clock: FixedClock
    ) -> None:
        uow_factory = uow_factory_for(engine)
        orphan = await create_run(uow_factory, status=RunStatus.QUEUED)
        driver = ScriptedDriver()

        for _ in range(2):
            app = create_app(settings=settings(), driver=driver, clock=clock)
            async with app.router.lifespan_context(app):
                pass

        row = await read_run(uow_factory, orphan)
        assert (row.status, row.status_reason) == (RunStatus.FAILED, "orphaned")
        events = await trace_events(uow_factory, orphan)
        assert [e.kind.value for e in events] == ["run_failed"]  # settled once, not twice


# ---------------------------------------------------------------------------
# 11–12. Composition with the real saver: DB-007 recovery and the real gate
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("_database")
class TestRealGraphComposition:
    async def test_the_executor_drives_a_graph_to_completion_under_the_real_saver(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver, clock: FixedClock
    ) -> None:
        uow_factory = uow_factory_for(engine)
        harness = Harness()
        driver = LangGraphRunDriver(harness.build(checkpointer))
        run_id = await create_run(uow_factory)
        executor = executor_for(engine, driver, clock, "executor-A")

        assert await executor.execute(run_id) is ExecutionOutcome.FINISHED
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.lease_owner) == (RunStatus.COMPLETED, None)
        assert harness.calls(run_id) == {"prepare": 1, "work": 1, "finish": 1}
        assert (await driver.inspect(run_id)).phase is CheckpointPhase.FINISHED

    async def test_shutdown_mid_run_leaves_a_row_the_reconciler_resumes_from_its_checkpoint(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver, clock: FixedClock
    ) -> None:
        """The process goes away while a node is in flight — `prepare` has
        checkpointed, `work` has not. The executor releases its lease on the
        way out; the row stays `running` with no owner; the next start-up's
        reconciler (DB-007) resumes from the checkpoint, and `prepare` never
        re-runs."""
        uow_factory = uow_factory_for(engine)
        harness = Harness(die_in_work=True)
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        executor = executor_for(engine, LangGraphRunDriver(graph), clock, "executor-A")

        executor.schedule(run_id)
        await harness.work_started.wait()
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.lease_owner) == (RunStatus.RUNNING, "executor-A")

        await executor.shutdown()
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.lease_owner) == (RunStatus.RUNNING, None)
        assert row.lease_expires_at is None
        assert harness.calls(run_id) == {"prepare": 1, "work": 1, "finish": 0}

        reconciler = Reconciler(
            uow_factory=uow_factory,
            driver=LangGraphRunDriver(graph),
            clock=clock,
            owner="reconciler-B",
            lease=LEASE,
        )
        report = await reconciler.reconcile_once(limit=500)
        assert report.outcomes[run_id] is RecoveryOutcome.RESUMED
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.lease_owner) == (RunStatus.COMPLETED, None)
        assert harness.calls(run_id) == {"prepare": 1, "work": 2, "finish": 1}
        assert await trace_kinds(uow_factory, run_id) == [("run_recovered", "resumed")]

    async def test_the_real_approval_gate_pauses_the_executor_and_the_decision_completes_the_run(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver, clock: FixedClock
    ) -> None:
        """The production graph, the real `request_approval` gate and the real
        saver: the executor's task ends on the interrupt with the lease
        released, the gate's own `approvals` row is what the operator
        decides, and `ApprovalService` resumes the run to `completed` with
        its final response persisted."""
        uow_factory = uow_factory_for(engine)
        send_args = {"draft_id": "d_100", "to_email": "ada@example.com"}
        plan = Plan(
            plan_id="p_api007",
            steps=[
                PlanStep(
                    step_id="s6",
                    tool=ToolName.SEND_EMAIL_MOCK,
                    args=send_args,
                    status=StepStatus.PENDING,
                )
            ],
        )

        async def fixed_plan(_state: AgentState) -> dict[str, Any]:
            return {"plan": plan, "status": RunStatus.RUNNING}

        async def recorded_execute(state: AgentState) -> dict[str, Any]:
            step_id = state["current_step_id"]
            steps = [
                s.model_copy(update={"status": StepStatus.SUCCEEDED}) if s.step_id == step_id else s
                for s in state["plan"].steps
            ]
            return {
                "tool_results": {
                    step_id: ToolResult(
                        step_id=step_id,
                        tool=ToolName.SEND_EMAIL_MOCK,
                        output={"status": "sent"},
                        produced_at=clock.now(),
                    )
                },
                "plan": state["plan"].model_copy(update={"steps": steps}),
            }

        handlers = NodeHandlers(clock=clock, uow_factory=uow_factory, plan_handler=fixed_plan)
        handlers.execute_tool = recorded_execute  # type: ignore[method-assign]
        driver = LangGraphRunDriver(create_agent_graph(checkpointer, node_handlers=handlers))
        executor = executor_for(engine, driver, clock, "executor-A")
        service = run_service_for(engine, clock, executor)
        created = await service.create_run("Send outreach email to ada@example.com")
        await service.start_run(created.run.id)
        run_id = created.run.id

        assert await executor._tasks[run_id] is ExecutionOutcome.PAUSED  # noqa: SLF001
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.lease_owner, row.lease_expires_at) == (
            RunStatus.AWAITING_APPROVAL,
            None,
            None,
        )
        async with uow_factory() as uow:
            [approval] = await uow.approvals.list_by_run(run_id)
            await uow.commit()
        assert (approval.status, approval.step_id) == (ApprovalStatus.PENDING, "s6")

        approvals = ApprovalService(
            uow_factory=uow_factory, driver=driver, clock=clock, lease=LEASE
        )
        result = await approvals.decide_approval(approval.id, decision="approve", decided_by="op")
        assert result.is_winner

        row = await read_run(uow_factory, run_id)
        assert (row.status, row.lease_owner) == (RunStatus.COMPLETED, None)
        assert row.final_response is not None
        assert row.final_response["done"] == ["s6"]
        kinds = [kind for kind, _ in await trace_kinds(uow_factory, run_id)]
        assert kinds[:3] == ["run_created", "run_started", "approval_requested"]
        assert "approval_granted" in kinds
