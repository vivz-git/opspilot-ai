"""DB-007 — crash recovery and the startup reconciler (§2.4, §12.2, ADR-004,
ADR-012, ADR-023).

Real Postgres, the real LangGraph Postgres saver, the real lease repository
and the real `Reconciler`; the only test double is the graph itself
(`recovery_harness.Harness`), because AGENT-002 has not landed. Worker death
is simulated at the ownership boundary exactly as a crash would leave things:
the graph task is cancelled mid-node, the heartbeat stops, and *nothing* is
released or written (see the harness module docstring).

Covers, by section:

- `TestClassifySnapshot`      the four checkpoint phases (unit, no I/O)
- `TestOrphanDetection`       who is and is not a candidate
- `TestCrashRecovery`         the acceptance scenario, and every row of the
                              state machine (R1–R6)
- `TestHitlSafety`            an intentional pause is never a crash
- `TestReconciliationIdempotency`   twice, and concurrently
- `TestObservability`         `run_recovered` / `run_failed` and their payloads
- `TestRollback`              a failed settling transaction commits nothing
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from app.agent.state import ApprovalStatus, RunStatus
from app.execution.recovery import (
    REASON_ORPHANED,
    REASON_RECOVERY_FAILED,
    CheckpointInspection,
    CheckpointPhase,
    LangGraphRunDriver,
    Reconciler,
    RecoveryOutcome,
    classify_snapshot,
)
from app.persistence.checkpointing import DURABILITY, open_checkpointer, thread_config
from app.persistence.models import TraceEventKind, TraceEventSeverity
from app.runtime import FixedClock
from app.tools.contracts import RiskLevel, ToolName
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command, Interrupt, PregelTask, StateSnapshot
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import (
    LEASE,
    T0,
    Harness,
    create_run,
    faulty_uow_factory,
    leave_stale_lease,
    migrate_to_head,
    outbox_effect,
    outbox_rows_for,
    read_run,
    require_database,
    seed_draft,
    settings,
    start_worker,
    trace_events,
    trace_kinds,
    uow_factory_for,
    wait_for,
)

pytestmark = [pytest.mark.integration]

PAST = T0 - timedelta(minutes=10)  # a lease taken then is long expired at T0


@pytest.fixture(scope="module")
def _database() -> None:
    """Integration classes opt in via `usefixtures`; the pure-function classes
    below run everywhere, database or not."""
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
async def checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    async with open_checkpointer(settings()) as saver:
        yield saver


def reconciler(engine: AsyncEngine, driver: Any, clock: FixedClock, owner: str) -> Reconciler:
    return Reconciler(
        uow_factory=uow_factory_for(engine),
        driver=driver,
        clock=clock,
        owner=owner,
        lease=LEASE,
        sleep=_instant,
    )


async def _instant(_seconds: float) -> None:
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Unit: snapshot classification
# ---------------------------------------------------------------------------
def _snapshot(
    *,
    checkpoint_id: str | None,
    next_: tuple[str, ...] = (),
    interrupts: tuple[Interrupt, ...] = (),
    values: dict[str, Any] | None = None,
) -> StateSnapshot:
    config: RunnableConfig = {"configurable": {"thread_id": "t"}}
    if checkpoint_id is not None:
        config["configurable"]["checkpoint_id"] = checkpoint_id
    tasks = tuple(
        PregelTask(
            id=f"task-{name}", name=name, path=("__pregel_pull", name), interrupts=interrupts
        )
        for name in next_
    )
    return StateSnapshot(
        values=values or {},
        next=next_,
        config=config,
        metadata=None,
        created_at=None,
        parent_config=None,
        tasks=tasks,
        interrupts=interrupts,
    )


class TestClassifySnapshot:
    def test_no_checkpoint(self) -> None:
        assert classify_snapshot(_snapshot(checkpoint_id=None)).phase is CheckpointPhase.NONE

    def test_paused_wins_over_in_progress(self) -> None:
        result = classify_snapshot(
            _snapshot(checkpoint_id="c1", next_=("gate",), interrupts=(Interrupt(value="?"),))
        )
        assert result.phase is CheckpointPhase.PAUSED
        assert result.next_nodes == ("gate",)
        assert result.checkpoint_id == "c1"

    def test_in_progress(self) -> None:
        result = classify_snapshot(
            _snapshot(checkpoint_id="c1", next_=("work",), values={"status": "running"})
        )
        assert result.phase is CheckpointPhase.IN_PROGRESS
        assert result.status is RunStatus.RUNNING

    def test_finished_carries_the_terminal_status_and_reason(self) -> None:
        result = classify_snapshot(
            _snapshot(
                checkpoint_id="c9",
                values={"status": "rejected", "status_reason": "approval_rejected"},
            )
        )
        assert result.phase is CheckpointPhase.FINISHED
        assert result.status is RunStatus.REJECTED
        assert result.status_reason == "approval_rejected"

    def test_unknown_status_value_is_not_guessed(self) -> None:
        result = classify_snapshot(_snapshot(checkpoint_id="c9", values={"status": "weird"}))
        assert result.status is None


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("_database")
class TestOrphanDetection:
    async def test_only_active_runs_with_expired_or_absent_leases_are_reconciled(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        uow_factory = uow_factory_for(engine)
        driver = LangGraphRunDriver(Harness().build(checkpointer))
        clock = FixedClock(T0)

        expired_running = await create_run(uow_factory, status=RunStatus.RUNNING)
        await leave_stale_lease(uow_factory, expired_running, owner="dead", acquired_at=PAST)
        expired_queued = await create_run(uow_factory, status=RunStatus.QUEUED)
        await leave_stale_lease(uow_factory, expired_queued, owner="dead", acquired_at=PAST)
        unleased_running = await create_run(uow_factory, status=RunStatus.RUNNING)
        healthy = await create_run(uow_factory, status=RunStatus.RUNNING)
        await leave_stale_lease(uow_factory, healthy, owner="alive", acquired_at=T0)
        paused = await create_run(uow_factory, status=RunStatus.AWAITING_APPROVAL)
        await leave_stale_lease(
            uow_factory,
            paused,
            owner="dead",
            acquired_at=PAST,
            expected=(RunStatus.AWAITING_APPROVAL,),
        )
        completed = await create_run(uow_factory, status=RunStatus.COMPLETED)
        rejected = await create_run(uow_factory, status=RunStatus.REJECTED)
        created = await create_run(uow_factory, status=RunStatus.CREATED)

        report = await reconciler(engine, driver, clock, "reconciler-1").reconcile_once(limit=500)

        assert report.outcomes[expired_running] is RecoveryOutcome.ORPHANED
        assert report.outcomes[expired_queued] is RecoveryOutcome.ORPHANED
        assert report.outcomes[unleased_running] is RecoveryOutcome.ORPHANED
        for untouched in (healthy, paused, completed, rejected, created):
            assert untouched not in report.outcomes

        assert (await read_run(uow_factory, healthy)).lease_owner == "alive"
        paused_row = await read_run(uow_factory, paused)
        assert paused_row.status is RunStatus.AWAITING_APPROVAL
        assert paused_row.lease_owner == "dead"  # a stale lease on a pause is inert
        assert (await read_run(uow_factory, completed)).status is RunStatus.COMPLETED

    async def test_targeted_recovery_of_a_non_orphan_is_a_no_op(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        uow_factory = uow_factory_for(engine)
        driver = LangGraphRunDriver(Harness().build(checkpointer))
        rec = reconciler(engine, driver, FixedClock(T0), "reconciler-1")
        for status in (RunStatus.AWAITING_APPROVAL, RunStatus.COMPLETED, RunStatus.CREATED):
            run_id = await create_run(uow_factory, status=status)
            assert await rec.recover_run(run_id) is RecoveryOutcome.SKIPPED
            assert (await read_run(uow_factory, run_id)).status is status
        healthy = await create_run(uow_factory, status=RunStatus.RUNNING)
        await leave_stale_lease(uow_factory, healthy, owner="alive", acquired_at=T0)
        assert await rec.recover_run(healthy) is RecoveryOutcome.SKIPPED
        assert await rec.recover_run(uuid.uuid4()) is RecoveryOutcome.SKIPPED


# ---------------------------------------------------------------------------
# Crash recovery — the acceptance scenario and the state machine
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("_database")
class TestCrashRecovery:
    async def test_worker_dies_mid_node_and_a_new_worker_resumes_from_the_checkpoint(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """The DB-007 acceptance scenario, end to end:

        1. the run gets a valid lease;
        2. a durable checkpoint exists (after `prepare`);
        3. the worker disappears without clean completion, mid-`work`;
        4. the lease expires (the clock moves; nothing else does);
        5. reconciliation detects the orphan;
        6. a new worker — the reconciler — takes ownership;
        7. execution resumes from the checkpoint, not from the start;
        8. the run reaches `completed`;
        9. `prepare` ran exactly once across both workers.
        """
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness(die_in_work=True)
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)

        # 1–3: a worker owns the run, checkpoints `prepare`, then dies in `work`.
        worker = await start_worker(
            uow_factory=uow_factory, graph=graph, run_id=run_id, owner="worker-A", clock=clock
        )
        await harness.work_started.wait()
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.lease_owner) == (RunStatus.RUNNING, "worker-A")
        assert row.lease_expires_at == T0 + LEASE.ttl
        before = await checkpointer.aget_tuple(thread_config(run_id))
        assert before is not None
        checkpoint_before_crash = before.config["configurable"]["checkpoint_id"]
        assert before.checkpoint["channel_values"]["log"] == ["prepare"]
        await worker.die()
        assert harness.calls(run_id) == {"prepare": 1, "work": 1, "finish": 0}

        # The row is exactly as the dead worker left it.
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.lease_owner) == (RunStatus.RUNNING, "worker-A")

        # 4: while the lease is live, nothing happens — a slow worker is not a dead one.
        clock.advance(seconds=LEASE.ttl.total_seconds() - 1)
        driver = LangGraphRunDriver(graph)
        rec = reconciler(engine, driver, clock, "reconciler-B")
        assert (await rec.reconcile_once(limit=500)).outcomes.get(run_id) is None

        # 5–8: the lease lapses; the next pass claims, resumes and finishes the run.
        clock.advance(seconds=2)
        report = await rec.reconcile_once(limit=500)
        assert report.outcomes[run_id] is RecoveryOutcome.RESUMED

        row = await read_run(uow_factory, run_id)
        assert row.status is RunStatus.COMPLETED
        assert (row.lease_owner, row.lease_expires_at) == (None, None)
        assert row.finished_at == clock.now()

        # 9: resumed, not restarted — `prepare` never re-ran; `work` re-executed
        # from the checkpoint (LangGraph's re-execution semantics, §9.7).
        assert harness.calls(run_id) == {"prepare": 1, "work": 2, "finish": 1}
        after = await graph.aget_state(thread_config(run_id))
        assert after.values["log"] == ["prepare", "work", "finish"]
        assert after.next == ()

        kinds = await trace_kinds(uow_factory, run_id)
        assert kinds == [("run_recovered", "resumed")]
        [event] = await trace_events(uow_factory, run_id)
        assert event.payload["previous_owner"] == "worker-A"
        assert event.payload["recovered_by"] == "reconciler-B"
        assert event.payload["checkpoint_id"] == checkpoint_before_crash
        assert event.payload["next_nodes"] == ["work"]

    async def test_recovered_run_does_not_repeat_a_keyed_mutation(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """Duplicate-execution safety (§10.4, ADR-020). `work` sends a real
        `email_outbox` row and *then* the worker dies — the effect happened
        but no checkpoint records it, so recovery re-executes the node. The
        attempt-invariant idempotency key and the database's
        `UNIQUE(idempotency_key)` turn the repeat into `duplicate_suppressed`:
        exactly one outbox row, with recovery adding no bypass."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        draft_id, to_email = await seed_draft(uow_factory)
        harness = Harness(die_in_work=True, effect=outbox_effect(uow_factory, draft_id, to_email))
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)

        worker = await start_worker(
            uow_factory=uow_factory, graph=graph, run_id=run_id, owner="worker-A", clock=clock
        )
        await harness.work_started.wait()
        assert harness.effect_results == ["sent"]
        assert len(await outbox_rows_for(uow_factory, run_id)) == 1
        await worker.die()

        clock.advance(seconds=LEASE.ttl.total_seconds() + 1)
        rec = reconciler(engine, LangGraphRunDriver(graph), clock, "reconciler-B")
        assert (await rec.reconcile_once(limit=500)).outcomes[run_id] is RecoveryOutcome.RESUMED

        assert harness.calls(run_id)["work"] == 2
        assert harness.effect_results == ["sent", "duplicate_suppressed"]
        assert len(await outbox_rows_for(uow_factory, run_id)) == 1
        assert (await read_run(uow_factory, run_id)).status is RunStatus.COMPLETED

    async def test_worker_dies_between_the_interrupt_checkpoint_and_the_status_write(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """The acceptance criterion's 'kills the task between checkpoint and
        row write': the graph paused on `interrupt()` and the checkpoint is
        durable, but the executor died before writing `awaiting_approval`.
        The row still says `running` with a stale lease — which is exactly
        what a crash looks like. The reconciler must repair it to
        `awaiting_approval` (R2), not fail it and not re-enter it; then the
        normal approval path resumes it to completion."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)

        worker = await start_worker(
            uow_factory=uow_factory,
            graph=graph,
            run_id=run_id,
            owner="worker-A",
            clock=clock,
            needs_approval=True,
        )
        result = await worker.task  # the graph returns on interrupt
        assert "__interrupt__" in result
        await _request_approval_row(engine, run_id, clock)  # the node wrote its row...
        await worker.heartbeat.stop()  # ... and the process dies right here.
        assert (await read_run(uow_factory, run_id)).status is RunStatus.RUNNING

        clock.advance(seconds=LEASE.ttl.total_seconds() + 1)
        rec = reconciler(engine, LangGraphRunDriver(graph), clock, "reconciler-B")
        assert (await rec.reconcile_once(limit=500)).outcomes[run_id] is RecoveryOutcome.PAUSED

        row = await read_run(uow_factory, run_id)
        assert row.status is RunStatus.AWAITING_APPROVAL
        assert (row.lease_owner, row.lease_expires_at) == (None, None)
        assert harness.calls(run_id)["finish"] == 0  # nothing was stepped past the gate
        snapshot = await graph.aget_state(thread_config(run_id))
        assert snapshot.interrupts and snapshot.next == ("gate",)
        assert await trace_kinds(uow_factory, run_id) == [("run_recovered", "awaiting_approval")]

        # A second pass has nothing to do: the pause is intentional now.
        assert run_id not in (await rec.reconcile_once(limit=500)).outcomes

        # The human decides; the approval path resumes it (§9.6).
        await _approve_and_resume(engine, graph, run_id, clock, owner="worker-C")
        row = await read_run(uow_factory, run_id)
        assert row.status is RunStatus.COMPLETED
        assert harness.calls(run_id)["finish"] == 1
        assert (await graph.aget_state(thread_config(run_id))).values["decision"] == "approve"

    async def test_orphan_without_a_checkpoint_is_failed_as_orphaned(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """R1 — claimed, then died before the graph ever checkpointed."""
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory, status=RunStatus.RUNNING)
        await leave_stale_lease(uow_factory, run_id, owner="dead", acquired_at=PAST)
        clock = FixedClock(T0)
        rec = reconciler(engine, LangGraphRunDriver(Harness().build(checkpointer)), clock, "r")
        assert (await rec.reconcile_once(limit=500)).outcomes[run_id] is RecoveryOutcome.ORPHANED
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.status_reason) == (RunStatus.FAILED, REASON_ORPHANED)
        assert (row.lease_owner, row.lease_expires_at) == (None, None)
        assert row.finished_at == T0
        [event] = await trace_events(uow_factory, run_id)
        assert event.kind is TraceEventKind.RUN_FAILED
        assert event.severity is TraceEventSeverity.WARNING
        assert event.error["class"] == REASON_ORPHANED
        assert event.payload["recovery"] == "no_checkpoint"
        assert event.payload["previous_owner"] == "dead"

    async def test_finished_checkpoint_is_finalized_from_its_state(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """R3 — the graph reached END but the process died before the row
        became terminal. The checkpoint's own `status` settles it; nothing
        is re-executed."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        worker = await start_worker(
            uow_factory=uow_factory, graph=graph, run_id=run_id, owner="worker-A", clock=clock
        )
        await worker.task
        await worker.heartbeat.stop()  # died before writing `completed`
        assert harness.calls(run_id) == {"prepare": 1, "work": 1, "finish": 1}

        clock.advance(seconds=LEASE.ttl.total_seconds() + 1)
        rec = reconciler(engine, LangGraphRunDriver(graph), clock, "reconciler-B")
        assert (await rec.reconcile_once(limit=500)).outcomes[run_id] is RecoveryOutcome.FINALIZED
        row = await read_run(uow_factory, run_id)
        assert row.status is RunStatus.COMPLETED
        assert (row.lease_owner, row.lease_expires_at) == (None, None)
        assert harness.calls(run_id) == {"prepare": 1, "work": 1, "finish": 1}
        assert await trace_kinds(uow_factory, run_id) == [("run_recovered", "completed")]

    async def test_finished_checkpoint_carries_a_rejected_outcome_faithfully(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """`rejected` is not `failed` (ADR-022): finalising from a checkpoint
        must preserve the human's decision, reason included."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        worker = await start_worker(
            uow_factory=uow_factory,
            graph=graph,
            run_id=run_id,
            owner="worker-A",
            clock=clock,
            needs_approval=True,
        )
        await worker.task
        await graph.ainvoke(Command(resume="reject"), thread_config(run_id), durability=DURABILITY)
        await worker.heartbeat.stop()

        clock.advance(seconds=LEASE.ttl.total_seconds() + 1)
        rec = reconciler(engine, LangGraphRunDriver(graph), clock, "reconciler-B")
        assert (await rec.reconcile_once(limit=500)).outcomes[run_id] is RecoveryOutcome.FINALIZED
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.status_reason) == (RunStatus.REJECTED, "approval_rejected")

    async def test_a_resume_that_raises_settles_the_row_as_recovery_failed(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """R5 — no run is left `running` with a lease nobody renews."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness(die_in_work=True)
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        worker = await start_worker(
            uow_factory=uow_factory, graph=graph, run_id=run_id, owner="worker-A", clock=clock
        )
        await harness.work_started.wait()
        await worker.die()

        class ExplodingDriver(LangGraphRunDriver):
            async def resume(self, run_id: uuid.UUID) -> CheckpointInspection:
                raise RuntimeError("checkpoint deserialisation exploded")

        clock.advance(seconds=LEASE.ttl.total_seconds() + 1)
        rec = reconciler(engine, ExplodingDriver(graph), clock, "reconciler-B")
        outcome = (await rec.reconcile_once(limit=500)).outcomes[run_id]
        assert outcome is RecoveryOutcome.RECOVERY_FAILED
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.status_reason) == (RunStatus.FAILED, REASON_RECOVERY_FAILED)
        assert (row.lease_owner, row.lease_expires_at) == (None, None)
        kinds = await trace_kinds(uow_factory, run_id)
        assert kinds == [("run_recovered", "resumed"), ("run_failed", "failed")]
        failed = (await trace_events(uow_factory, run_id))[-1]
        assert failed.severity is TraceEventSeverity.ERROR
        assert "exploded" in failed.error["detail"]
        assert failed.payload["recovery"] == "resume_raised"

    async def test_losing_the_lease_mid_resume_abandons_the_run_untouched(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """R6 — the reconciler is a worker like any other: if its own lease
        lapses while it resumes (a stall) and someone else claims the run,
        it must stop and write nothing, or two workers would own one run."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness(die_in_work=True)
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        worker = await start_worker(
            uow_factory=uow_factory, graph=graph, run_id=run_id, owner="worker-A", clock=clock
        )
        await harness.work_started.wait()
        await worker.die()

        # The resume blocks, so the reconciler is mid-resume when its lease
        # lapses and a competitor takes the run over.
        blocked = asyncio.Event()

        class BlockingDriver(LangGraphRunDriver):
            async def resume(self, run_id: uuid.UUID) -> CheckpointInspection:
                blocked.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        clock.advance(seconds=LEASE.ttl.total_seconds() + 1)
        rec = reconciler(engine, BlockingDriver(graph), clock, "reconciler-B")
        pass_task = asyncio.create_task(rec.reconcile_once(limit=500))
        await blocked.wait()
        assert (await read_run(uow_factory, run_id)).lease_owner == "reconciler-B"

        clock.advance(seconds=LEASE.ttl.total_seconds() + 1)
        async with uow_factory() as uow:
            stolen = await uow.agent_runs.acquire_lease(
                run_id, owner="worker-C", now=clock.now(), ttl=LEASE.ttl
            )
            assert stolen is not None
            await uow.commit()

        report = await pass_task
        assert report.outcomes[run_id] is RecoveryOutcome.LEASE_LOST
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.lease_owner) == (RunStatus.RUNNING, "worker-C")
        assert await trace_kinds(uow_factory, run_id) == [("run_recovered", "resumed")]


# ---------------------------------------------------------------------------
# HITL safety
# ---------------------------------------------------------------------------
async def _approve_and_resume(
    engine: AsyncEngine, graph: Any, run_id: uuid.UUID, clock: FixedClock, *, owner: str
) -> None:
    """What HITL-00x/API-007 will do: decide (conditionally), take the lease
    with the `awaiting_approval → running` transition, resume with the
    decision, and settle the row from the graph's terminal state."""
    uow_factory = uow_factory_for(engine)
    async with uow_factory() as uow:
        pending = [
            a for a in await uow.approvals.list_by_run(run_id) if a.status is ApprovalStatus.PENDING
        ]
        assert len(pending) == 1
        decided = await uow.approvals.decide(
            pending[0].id,
            status=ApprovalStatus.APPROVED,
            decided_by="operator",
            decided_at=clock.now(),
        )
        assert decided is not None
        claimed = await uow.agent_runs.acquire_lease(
            run_id,
            owner=owner,
            now=clock.now(),
            ttl=LEASE.ttl,
            expected=(RunStatus.AWAITING_APPROVAL,),
            status=RunStatus.RUNNING,
        )
        assert claimed is not None
        await uow.commit()
    await graph.ainvoke(Command(resume="approve"), thread_config(run_id), durability=DURABILITY)
    final = classify_snapshot(await graph.aget_state(thread_config(run_id)))
    assert final.phase is CheckpointPhase.FINISHED and final.status is not None
    async with uow_factory() as uow:
        settled = await uow.agent_runs.transition_status(
            run_id,
            expected=(RunStatus.RUNNING,),
            status=final.status,
            owner=owner,
            finished_at=clock.now(),
            release_lease=True,
        )
        assert settled is not None
        await uow.commit()


async def _request_approval_row(engine: AsyncEngine, run_id: uuid.UUID, clock: FixedClock) -> None:
    """The `approvals` row `request_approval` writes before interrupting (§9.7)."""
    async with uow_factory_for(engine)() as uow:
        await uow.approvals.create_request(
            run_id=run_id,
            step_id="s1",
            tool=ToolName.SEND_EMAIL_MOCK,
            risk=RiskLevel.HIGH,
            title="Send outreach",
            summary="Send the draft",
            payload_preview={"to": "ada@example.com"},
            args_hash="args-hash",
            requested_at=clock.now(),
            expires_at=clock.now() + timedelta(days=1),
        )
        await uow.commit()


async def _pause_for_approval(
    engine: AsyncEngine, graph: Any, harness: Harness, run_id: uuid.UUID, clock: FixedClock
) -> None:
    """A run paused the way the executor leaves it: interrupted checkpoint,
    a pending `approvals` row, status `awaiting_approval`, no lease."""
    uow_factory = uow_factory_for(engine)
    worker = await start_worker(
        uow_factory=uow_factory,
        graph=graph,
        run_id=run_id,
        owner="worker-A",
        clock=clock,
        needs_approval=True,
    )
    await worker.task
    await worker.heartbeat.stop()
    await _request_approval_row(engine, run_id, clock)
    async with uow_factory() as uow:
        moved = await uow.agent_runs.transition_status(
            run_id,
            expected=(RunStatus.RUNNING,),
            status=RunStatus.AWAITING_APPROVAL,
            owner="worker-A",
            release_lease=True,
        )
        assert moved is not None
        await uow.commit()


@pytest.mark.usefixtures("_database")
class TestHitlSafety:
    async def test_a_paused_run_is_never_treated_as_a_crash(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        await _pause_for_approval(engine, graph, harness, run_id, clock)

        # Hours pass with no heartbeat — that is what waiting for a human is.
        clock.advance(seconds=6 * 3600)
        rec = reconciler(engine, LangGraphRunDriver(graph), clock, "reconciler-B")
        report = await rec.reconcile_once(limit=500)
        assert run_id not in report.outcomes

        row = await read_run(uow_factory, run_id)
        assert row.status is RunStatus.AWAITING_APPROVAL
        async with uow_factory() as uow:
            [approval] = await uow.approvals.list_by_run(run_id)
            assert approval.status is ApprovalStatus.PENDING
            await uow.commit()
        snapshot = await graph.aget_state(thread_config(run_id))
        assert snapshot.interrupts and snapshot.next == ("gate",)
        assert harness.calls(run_id)["finish"] == 0
        assert await trace_kinds(uow_factory, run_id) == []

    async def test_an_expired_stale_lease_on_a_paused_run_does_not_destroy_the_approval(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """A crash *after* the pause can leave a lease behind on an
        `awaiting_approval` row. Its expiry means nothing: the approval and
        the checkpoint are untouched, and the run resumes on approval."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        await _pause_for_approval(engine, graph, harness, run_id, clock)
        await leave_stale_lease(
            uow_factory,
            run_id,
            owner="crashed-after-pause",
            acquired_at=PAST,
            expected=(RunStatus.AWAITING_APPROVAL,),
        )

        clock.advance(seconds=3600)
        rec = reconciler(engine, LangGraphRunDriver(graph), clock, "reconciler-B")
        assert run_id not in (await rec.reconcile_once(limit=500)).outcomes
        assert await rec.recover_run(run_id) is RecoveryOutcome.SKIPPED
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.lease_owner) == (
            RunStatus.AWAITING_APPROVAL,
            "crashed-after-pause",
        )

        # The stale lease does not block the approval path either: the
        # resume claim is allowed to take over an expired lease.
        await _approve_and_resume(engine, graph, run_id, clock, owner="worker-C")
        assert (await read_run(uow_factory, run_id)).status is RunStatus.COMPLETED
        assert harness.calls(run_id)["finish"] == 1

    async def test_reconciliation_never_steps_a_paused_checkpoint_past_its_gate(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """Even when the row is wrong (`running`, stale lease) and the
        reconciler *does* claim it, a paused checkpoint is repaired, not
        resumed: `resume(None)` is never issued against an interrupt."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        worker = await start_worker(
            uow_factory=uow_factory,
            graph=graph,
            run_id=run_id,
            owner="worker-A",
            clock=clock,
            needs_approval=True,
        )
        await worker.task
        await worker.heartbeat.stop()

        resumes: list[uuid.UUID] = []

        class CountingDriver(LangGraphRunDriver):
            async def resume(
                self, run_id: uuid.UUID, *args: Any, **kwargs: Any
            ) -> CheckpointInspection:
                resumes.append(run_id)
                return await super().resume(run_id, *args, **kwargs)

        clock.advance(seconds=LEASE.ttl.total_seconds() + 1)
        rec = reconciler(engine, CountingDriver(graph), clock, "reconciler-B")
        for _ in range(3):
            await rec.reconcile_once(limit=500)
        assert resumes == []
        assert harness.calls(run_id)["finish"] == 0
        assert (await read_run(uow_factory, run_id)).status is RunStatus.AWAITING_APPROVAL

    async def test_concurrent_approval_and_resume_is_single_flight(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """§9.6: the conditional approval update picks one winner, and the
        lease makes the resume itself single-flight even if two resumers
        raced past the decision — exactly one email, one finish."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        await _pause_for_approval(engine, graph, harness, run_id, clock)
        start = asyncio.Event()

        async def resumer(owner: str) -> str:
            await start.wait()
            async with uow_factory() as uow:
                [approval] = await uow.approvals.list_by_run(run_id)
                decided = await uow.approvals.decide(
                    approval.id,
                    status=ApprovalStatus.APPROVED,
                    decided_by=owner,
                    decided_at=clock.now(),
                )
                claimed = await uow.agent_runs.acquire_lease(
                    run_id,
                    owner=owner,
                    now=clock.now(),
                    ttl=LEASE.ttl,
                    expected=(RunStatus.AWAITING_APPROVAL,),
                    status=RunStatus.RUNNING,
                )
                await uow.commit()
            if decided is None or claimed is None:
                return "lost"
            await graph.ainvoke(
                Command(resume="approve"), thread_config(run_id), durability=DURABILITY
            )
            return "won"

        tasks = [asyncio.create_task(resumer(f"resumer-{i}")) for i in range(6)]
        await asyncio.sleep(0)
        start.set()
        results = await asyncio.gather(*tasks)
        assert sorted(results) == ["lost"] * 5 + ["won"]
        assert harness.calls(run_id)["finish"] == 1
        async with uow_factory() as uow:
            [approval] = await uow.approvals.list_by_run(run_id)
            assert approval.status is ApprovalStatus.APPROVED
            await uow.commit()


# ---------------------------------------------------------------------------
# Idempotency of reconciliation
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("_database")
class TestReconciliationIdempotency:
    async def test_a_second_pass_finds_nothing_and_changes_nothing(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness(die_in_work=True)
        graph = harness.build(checkpointer)
        crashed = await create_run(uow_factory)
        worker = await start_worker(
            uow_factory=uow_factory, graph=graph, run_id=crashed, owner="worker-A", clock=clock
        )
        await harness.work_started.wait()
        await worker.die()
        never_started = await create_run(uow_factory, status=RunStatus.RUNNING)
        await leave_stale_lease(uow_factory, never_started, owner="dead", acquired_at=PAST)

        clock.advance(seconds=LEASE.ttl.total_seconds() + 1)
        rec = reconciler(engine, LangGraphRunDriver(graph), clock, "reconciler-B")
        first = await rec.reconcile_once(limit=500)
        assert first.outcomes[crashed] is RecoveryOutcome.RESUMED
        assert first.outcomes[never_started] is RecoveryOutcome.ORPHANED

        snapshot_rows = {
            run_id: (await read_run(uow_factory, run_id)).status
            for run_id in (crashed, never_started)
        }
        events_before = {
            run_id: await trace_kinds(uow_factory, run_id) for run_id in (crashed, never_started)
        }
        second = await rec.reconcile_once(limit=500)
        assert crashed not in second.outcomes and never_started not in second.outcomes
        for run_id in (crashed, never_started):
            assert (await read_run(uow_factory, run_id)).status is snapshot_rows[run_id]
            assert await trace_kinds(uow_factory, run_id) == events_before[run_id]
        assert harness.calls(crashed) == {"prepare": 1, "work": 2, "finish": 1}

    async def test_concurrent_reconcilers_recover_each_orphan_exactly_once(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """Four reconcilers (four owners, separate connections) race over the
        same eight orphans. The atomic claim hands each run to exactly one
        of them: one `run_recovered` per run, one resume per run, and no
        run ends up with conflicting ownership."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)

        orphans: list[uuid.UUID] = []
        for _ in range(8):
            crashed = Harness(die_in_work=True)
            run_id = await create_run(uow_factory)
            worker = await start_worker(
                uow_factory=uow_factory,
                graph=crashed.build(checkpointer),
                run_id=run_id,
                owner=f"worker-{run_id.hex[:6]}",
                clock=clock,
            )
            await crashed.work_started.wait()
            await worker.die()
            orphans.append(run_id)
        clock.advance(seconds=LEASE.ttl.total_seconds() + 1)

        # The restarted process compiles the same topology over the same saver.
        harness = Harness()
        graph = harness.build(checkpointer)
        reconcilers = [
            reconciler(engine, LangGraphRunDriver(graph), clock, f"reconciler-{i}")
            for i in range(4)
        ]
        reports = await asyncio.gather(*(r.reconcile_once(limit=500) for r in reconcilers))

        for run_id in orphans:
            outcomes = [
                rep.outcomes[run_id]
                for rep in reports
                if run_id in rep.outcomes and rep.outcomes[run_id] is not RecoveryOutcome.SKIPPED
            ]
            assert outcomes == [RecoveryOutcome.RESUMED], run_id
            row = await read_run(uow_factory, run_id)
            assert row.status is RunStatus.COMPLETED
            assert (row.lease_owner, row.lease_expires_at) == (None, None)
            assert await trace_kinds(uow_factory, run_id) == [("run_recovered", "resumed")]
        for run_id in orphans:
            assert harness.calls(run_id) == {"prepare": 0, "work": 1, "finish": 1}


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("_database")
class TestObservability:
    async def test_run_recovered_is_an_admitted_trace_kind_with_full_provenance(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness(die_in_work=True)
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        worker = await start_worker(
            uow_factory=uow_factory, graph=graph, run_id=run_id, owner="worker-A", clock=clock
        )
        await harness.work_started.wait()
        await worker.die()
        clock.advance(seconds=LEASE.ttl.total_seconds() + 1)
        await reconciler(engine, LangGraphRunDriver(graph), clock, "reconciler-B").reconcile_once(
            limit=500
        )
        [event] = await trace_events(uow_factory, run_id)
        assert event.kind is TraceEventKind.RUN_RECOVERED
        assert event.severity is TraceEventSeverity.INFO
        assert event.seq == 1
        assert event.status == "resumed"
        assert set(event.payload) >= {
            "recovery",
            "previous_owner",
            "recovered_by",
            "lease_expired_at",
            "checkpoint_id",
            "next_nodes",
        }
        assert event.payload["lease_expired_at"] == (T0 + LEASE.ttl).isoformat()


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("_database")
class TestRollback:
    async def test_a_failed_settling_transaction_commits_nothing_and_is_retried_later(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """R7 — the status transition and the trace event are one unit of
        work. If the event write fails, the transition rolls back with it:
        the run stays `running`, leased by the reconciler, and becomes a
        candidate again once that lease expires — no half-written state."""
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory, status=RunStatus.RUNNING)
        await leave_stale_lease(uow_factory, run_id, owner="dead", acquired_at=PAST)
        clock = FixedClock(T0)
        driver = LangGraphRunDriver(Harness().build(checkpointer))

        faulty = Reconciler(
            uow_factory=faulty_uow_factory(uow_factory, fail_kinds={"run_failed"}),
            driver=driver,
            clock=clock,
            owner="reconciler-faulty",
            lease=LEASE,
            sleep=_instant,
        )
        report = await faulty.reconcile_once(limit=500)
        assert report.outcomes[run_id] is RecoveryOutcome.ERRORED

        row = await read_run(uow_factory, run_id)
        assert row.status is RunStatus.RUNNING  # the FAILED transition rolled back
        assert row.status_reason is None
        assert row.lease_owner == "reconciler-faulty"  # the committed claim stands
        assert await trace_kinds(uow_factory, run_id) == []

        # Not a candidate while the faulty reconciler's lease is live...
        healthy = reconciler(engine, driver, clock, "reconciler-healthy")
        assert run_id not in (await healthy.reconcile_once(limit=500)).outcomes
        # ... and recovered normally once it lapses.
        clock.advance(seconds=LEASE.ttl.total_seconds() + 1)
        assert (await healthy.reconcile_once(limit=500)).outcomes[run_id] is (
            RecoveryOutcome.ORPHANED
        )
        row = await read_run(uow_factory, run_id)
        assert (row.status, row.status_reason) == (RunStatus.FAILED, REASON_ORPHANED)
        assert await trace_kinds(uow_factory, run_id) == [("run_failed", "failed")]

    async def test_wait_for_helper_is_bounded(self) -> None:
        with pytest.raises(AssertionError):
            await wait_for(lambda: False, attempts=3)
