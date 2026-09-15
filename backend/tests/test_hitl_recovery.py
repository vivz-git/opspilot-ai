"""HITL crash-window Option 1 integration tests (§9.6, §12.6, ADR-004, ADR-023).

Tests the durable handoff between approval decision commit and LangGraph resumption:
1. APPROVED crash window: commit succeeds -> process dies -> reconciler recovers to completion.
2. REJECTED crash window: commit succeeds -> process dies -> reconciler recovers to REJECTED.
3. Genuine undecided approval: checkpoint PAUSED + PENDING approval -> reconciler preserves R2.
4. Duplicate same-decision request: idempotent success, resume occurs once.
5. Approve-vs-reject race: exactly one winner commits and resumes; loser conflicts.
6. Wrong args_hash: decision rejected with PolicyViolation; state remains unchanged.
7. Correct args_hash: decision succeeds.
8. Step-specific recovery: unrelated approval in same run cannot be consumed.
9. No duplicate tool execution after reconciliation: idempotency constraints enforced.
10. Lease fencing during recovery: reconciler aborts without overwriting state on lease loss.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from app.agent.state import ApprovalDecisionKind, ApprovalStatus, RunStatus
from app.errors import ApprovalConflictError, PolicyViolation
from app.execution.approvals import ApprovalService
from app.execution.recovery import (
    CheckpointInspection,
    LangGraphRunDriver,
    Reconciler,
    RecoveryOutcome,
)
from app.persistence.checkpointing import open_checkpointer
from app.persistence.models import RiskLevel, ToolName
from app.runtime import FixedClock
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import (
    LEASE,
    T0,
    Harness,
    create_run,
    migrate_to_head,
    outbox_effect,
    outbox_rows_for,
    read_run,
    require_database,
    seed_draft,
    settings,
    start_worker,
    uow_factory_for,
)

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
def _database() -> None:
    require_database()
    migrate_to_head()


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(
        settings().database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=20,
    )
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    async with open_checkpointer(settings()) as saver:
        yield saver


async def _instant(_seconds: float) -> None:
    await asyncio.sleep(0)


def reconciler(engine: AsyncEngine, driver: Any, clock: FixedClock, owner: str) -> Reconciler:
    return Reconciler(
        uow_factory=uow_factory_for(engine),
        driver=driver,
        clock=clock,
        owner=owner,
        lease=LEASE,
        sleep=_instant,
    )


async def _pause_run_for_approval(
    engine: AsyncEngine,
    graph: Any,
    run_id: uuid.UUID,
    clock: FixedClock,
    *,
    step_id: str = "s1",
    args_hash: str = "hash-s1",
    owner: str = "worker-A",
) -> uuid.UUID:
    """Pause a run at the approval gate, create a PENDING approval row,
    and settle the run to AWAITING_APPROVAL with lease released."""
    uow_factory = uow_factory_for(engine)
    worker = await start_worker(
        uow_factory=uow_factory,
        graph=graph,
        run_id=run_id,
        owner=owner,
        clock=clock,
        needs_approval=True,
    )
    await worker.task
    await worker.heartbeat.stop()

    approval_id = uuid.uuid4()
    async with uow_factory() as uow:
        await uow.approvals.create_request(
            id=approval_id,
            run_id=run_id,
            step_id=step_id,
            tool=ToolName.SEND_EMAIL_MOCK,
            risk=RiskLevel.HIGH,
            title="Send email",
            summary="Send outreach email",
            payload_preview={"draft_id": "d1"},
            args_hash=args_hash,
            requested_at=clock.now(),
            expires_at=clock.now() + timedelta(days=1),
        )
        moved = await uow.agent_runs.transition_status(
            run_id,
            expected=(RunStatus.RUNNING,),
            status=RunStatus.AWAITING_APPROVAL,
            owner=owner,
            release_lease=True,
        )
        assert moved is not None
        await uow.commit()

    return approval_id


@pytest.mark.usefixtures("_database")
class TestHitlCrashWindowRecovery:
    async def test_approved_crash_window_reconciler_recovers_to_completion(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """1. APPROVED: commit succeeds, worker process dies before initial resume;
        reconciler discovers expired lease and resumes paused checkpoint with 'approve'."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        approval_id = await _pause_run_for_approval(engine, graph, run_id, clock)

        # Simulate process crash right after commit, before resume
        async def crash_after_commit() -> None:
            raise RuntimeError("process crashed immediately after commit")

        service = ApprovalService(
            uow_factory=uow_factory,
            driver=LangGraphRunDriver(graph),
            clock=clock,
            lease=LEASE,
            owner="crashed-worker-1",
            before_resume=crash_after_commit,
        )

        with pytest.raises(RuntimeError, match="crashed immediately after commit"):
            await service.decide_approval(
                approval_id,
                decision=ApprovalDecisionKind.APPROVE,
                args_hash="hash-s1",
                decided_by="operator-1",
            )

        # Verify durable DB state: approval is APPROVED, run is RUNNING under worker lease
        row = await read_run(uow_factory, run_id)
        assert row.status is RunStatus.RUNNING
        assert row.lease_owner == "crashed-worker-1"
        assert row.lease_expires_at == clock.now() + LEASE.ttl

        async with uow_factory() as uow:
            appr = await uow.approvals.get(approval_id)
            assert appr is not None and appr.status is ApprovalStatus.APPROVED
            await uow.commit()

        # Advance clock past lease TTL — worker lease expires
        clock.advance(seconds=LEASE.ttl.total_seconds() + 5)

        # Reconciler runs and recovers the crashed run
        rec = reconciler(engine, LangGraphRunDriver(graph), clock, "reconciler-B")
        report = await rec.reconcile_once(limit=500)

        assert report.outcomes.get(run_id) is RecoveryOutcome.RESUMED
        settled_row = await read_run(uow_factory, run_id)
        assert settled_row.status is RunStatus.COMPLETED
        assert settled_row.lease_owner is None
        assert harness.calls(run_id)["finish"] == 1

    async def test_rejected_crash_window_reconciler_recovers_to_rejected(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """2. REJECTED: commit succeeds, worker dies before resume;
        reconciler resumes with 'reject', terminating as REJECTED without mutating effects."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        approval_id = await _pause_run_for_approval(engine, graph, run_id, clock)

        async def crash_after_commit() -> None:
            raise RuntimeError("crash after reject commit")

        service = ApprovalService(
            uow_factory=uow_factory,
            driver=LangGraphRunDriver(graph),
            clock=clock,
            lease=LEASE,
            owner="crashed-worker-2",
            before_resume=crash_after_commit,
        )

        with pytest.raises(RuntimeError, match="crash after reject commit"):
            await service.decide_approval(
                approval_id,
                decision=ApprovalDecisionKind.REJECT,
                args_hash="hash-s1",
                decided_by="operator-2",
                reason="High risk outreach",
            )

        clock.advance(seconds=LEASE.ttl.total_seconds() + 5)

        rec = reconciler(engine, LangGraphRunDriver(graph), clock, "reconciler-B")
        report = await rec.reconcile_once(limit=500)

        assert report.outcomes.get(run_id) is RecoveryOutcome.RESUMED
        settled_row = await read_run(uow_factory, run_id)
        assert settled_row.status is RunStatus.REJECTED
        assert settled_row.status_reason == "approval_rejected"
        assert settled_row.lease_owner is None

    async def test_genuine_undecided_approval_preserves_r2_awaiting_approval(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """3. Genuine undecided approval: checkpoint PAUSED + PENDING approval;
        reconciler preserves R2 awaiting_approval without stepping past the gate."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        approval_id = await _pause_run_for_approval(engine, graph, run_id, clock)

        # Simulate a crash before row write was settled to AWAITING_APPROVAL:
        # leave run in RUNNING with an expired lease, but approval is STILL PENDING
        async with uow_factory() as uow:
            acquired = await uow.agent_runs.acquire_lease(
                run_id,
                owner="dead-worker",
                now=clock.now(),
                ttl=LEASE.ttl,
                expected=(RunStatus.AWAITING_APPROVAL,),
                status=RunStatus.RUNNING,
            )
            assert acquired is not None
            await uow.commit()

        clock.advance(seconds=LEASE.ttl.total_seconds() + 5)

        rec = reconciler(engine, LangGraphRunDriver(graph), clock, "reconciler-B")
        report = await rec.reconcile_once(limit=500)

        assert report.outcomes.get(run_id) is RecoveryOutcome.PAUSED
        row = await read_run(uow_factory, run_id)
        assert row.status is RunStatus.AWAITING_APPROVAL
        assert row.lease_owner is None
        assert harness.calls(run_id)["finish"] == 0

        async with uow_factory() as uow:
            appr = await uow.approvals.get(approval_id)
            assert appr is not None and appr.status is ApprovalStatus.PENDING
            await uow.commit()

    async def test_duplicate_same_decision_request_is_idempotent(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """4. Duplicate request: second call with identical decision is idempotent;
        resumption is dispatched only once."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        approval_id = await _pause_run_for_approval(engine, graph, run_id, clock)

        service = ApprovalService(
            uow_factory=uow_factory,
            driver=LangGraphRunDriver(graph),
            clock=clock,
            lease=LEASE,
            owner="worker-idemp",
        )

        res1 = await service.decide_approval(
            approval_id,
            decision="approve",
            args_hash="hash-s1",
            decided_by="operator-1",
        )
        assert res1.is_winner is True
        assert res1.status is ApprovalStatus.APPROVED
        assert harness.calls(run_id)["finish"] == 1

        # Second call with the same decision
        res2 = await service.decide_approval(
            approval_id,
            decision="approve",
            args_hash="hash-s1",
            decided_by="operator-1",
        )
        assert res2.is_winner is False
        assert res2.status is ApprovalStatus.APPROVED
        assert harness.calls(run_id)["finish"] == 1  # Still 1, never resumed twice

    async def test_approve_vs_reject_race_has_exactly_one_winner(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """5. Approve-vs-reject race: exactly one transaction commits and resumes;
        the conflicting decision raises ApprovalConflictError."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        approval_id = await _pause_run_for_approval(engine, graph, run_id, clock)

        service1 = ApprovalService(
            uow_factory=uow_factory,
            driver=LangGraphRunDriver(graph),
            clock=clock,
            lease=LEASE,
            owner="worker-racer-1",
        )
        service2 = ApprovalService(
            uow_factory=uow_factory,
            driver=LangGraphRunDriver(graph),
            clock=clock,
            lease=LEASE,
            owner="worker-racer-2",
        )

        t1 = service1.decide_approval(
            approval_id,
            decision="approve",
            args_hash="hash-s1",
            decided_by="operator-A",
        )
        t2 = service2.decide_approval(
            approval_id,
            decision="reject",
            args_hash="hash-s1",
            decided_by="operator-B",
        )

        results = await asyncio.gather(t1, t2, return_exceptions=True)

        winners = [r for r in results if not isinstance(r, Exception) and r.is_winner]
        conflicts = [r for r in results if isinstance(r, ApprovalConflictError)]

        assert len(winners) == 1
        assert len(conflicts) == 1
        assert harness.calls(run_id)["finish"] == 1

    async def test_wrong_args_hash_is_rejected_and_state_unchanged(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """6. Wrong args_hash: decision rejected with PolicyViolation; state remains PENDING."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        approval_id = await _pause_run_for_approval(
            engine, graph, run_id, clock, args_hash="canonical-hash"
        )

        service = ApprovalService(
            uow_factory=uow_factory,
            driver=LangGraphRunDriver(graph),
            clock=clock,
            lease=LEASE,
            owner="worker-hash-test",
        )

        with pytest.raises(PolicyViolation, match="Approval args_hash mismatch"):
            await service.decide_approval(
                approval_id,
                decision="approve",
                args_hash="forged-or-tampered-hash",
            )

        # Verify state is untouched
        row = await read_run(uow_factory, run_id)
        assert row.status is RunStatus.AWAITING_APPROVAL
        async with uow_factory() as uow:
            appr = await uow.approvals.get(approval_id)
            assert appr is not None and appr.status is ApprovalStatus.PENDING
            await uow.commit()

        assert harness.calls(run_id)["finish"] == 0

    async def test_correct_args_hash_succeeds(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """7. Correct args_hash: decision matches and execution proceeds."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        approval_id = await _pause_run_for_approval(
            engine, graph, run_id, clock, args_hash="expected-hash"
        )

        service = ApprovalService(
            uow_factory=uow_factory,
            driver=LangGraphRunDriver(graph),
            clock=clock,
            lease=LEASE,
            owner="worker-correct-hash",
        )

        res = await service.decide_approval(
            approval_id,
            decision="approve",
            args_hash="expected-hash",
        )
        assert res.is_winner is True
        assert (await read_run(uow_factory, run_id)).status is RunStatus.COMPLETED

    async def test_step_specific_approval_recovery(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """8. Step-specific recovery: an approved decision for step s1 is not consumed
        when the paused checkpoint is at step s2."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)

        # 1. Simulate step s1: previously approved and completed in history
        appr_s1_id = uuid.uuid4()
        async with uow_factory() as uow:
            await uow.approvals.create_request(
                id=appr_s1_id,
                run_id=run_id,
                step_id="s1",
                tool=ToolName.SEND_EMAIL_MOCK,
                risk=RiskLevel.HIGH,
                title="Send s1",
                summary="Summary s1",
                payload_preview={},
                args_hash="hash-s1",
                requested_at=clock.now() - timedelta(hours=1),
                expires_at=clock.now() + timedelta(days=1),
            )
            await uow.approvals.decide(
                appr_s1_id,
                status=ApprovalStatus.APPROVED,
                decided_by="operator",
                decided_at=clock.now() - timedelta(minutes=30),
            )
            await uow.commit()

        # 2. Step s2 is paused at gate with step_id="s2"
        appr_s2_id = await _pause_run_for_approval(
            engine, graph, run_id, clock, step_id="s2", args_hash="hash-s2"
        )

        # Worker crashed leaving an expired lease on RUNNING run
        async with uow_factory() as uow:
            acquired = await uow.agent_runs.acquire_lease(
                run_id,
                owner="dead-worker-s2",
                now=clock.now(),
                ttl=LEASE.ttl,
                expected=(RunStatus.AWAITING_APPROVAL,),
                status=RunStatus.RUNNING,
            )
            assert acquired is not None
            await uow.commit()

        clock.advance(seconds=LEASE.ttl.total_seconds() + 5)

        # Mock driver inspect returning step_id="s2" on PAUSED checkpoint
        class Step2Driver(LangGraphRunDriver):
            async def inspect(self, rid: uuid.UUID) -> CheckpointInspection:
                base = await super().inspect(rid)
                return CheckpointInspection(
                    phase=base.phase,
                    checkpoint_id=base.checkpoint_id,
                    next_nodes=base.next_nodes,
                    status=base.status,
                    status_reason=base.status_reason,
                    step_id="s2",
                )

        rec = reconciler(engine, Step2Driver(graph), clock, "reconciler-B")
        report = await rec.reconcile_once(limit=500)

        # Reconciler MUST NOT consume s1's approved decision!
        # It must see s2 is still PENDING and preserve R2
        assert report.outcomes.get(run_id) is RecoveryOutcome.PAUSED
        row = await read_run(uow_factory, run_id)
        assert row.status is RunStatus.AWAITING_APPROVAL

        # s2 is still PENDING
        async with uow_factory() as uow:
            s2_row = await uow.approvals.get(appr_s2_id)
            assert s2_row is not None and s2_row.status is ApprovalStatus.PENDING
            await uow.commit()

    async def test_no_duplicate_tool_execution_after_reconciliation(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """9. No duplicate tool execution: idempotency key ensures protected mutations
        are executed exactly once across crash and recovery."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        draft_id, to_email = await seed_draft(uow_factory)
        harness = Harness(effect=outbox_effect(uow_factory, draft_id, to_email))
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        approval_id = await _pause_run_for_approval(engine, graph, run_id, clock)

        # Work already ran before pause -> exactly 1 email was sent
        rows_before = await outbox_rows_for(uow_factory, run_id)
        assert len(rows_before) == 1

        async def crash_after_commit() -> None:
            raise RuntimeError("crash after commit")

        service = ApprovalService(
            uow_factory=uow_factory,
            driver=LangGraphRunDriver(graph),
            clock=clock,
            lease=LEASE,
            owner="crashed-worker-outbox",
            before_resume=crash_after_commit,
        )

        with pytest.raises(RuntimeError):
            await service.decide_approval(
                approval_id,
                decision="approve",
                args_hash="hash-s1",
            )

        clock.advance(seconds=LEASE.ttl.total_seconds() + 5)

        rec = reconciler(engine, LangGraphRunDriver(graph), clock, "reconciler-B")
        report = await rec.reconcile_once(limit=500)
        assert report.outcomes.get(run_id) is RecoveryOutcome.RESUMED

        # Outbox rows count is STILL exactly 1: no duplicate execution!
        rows_after = await outbox_rows_for(uow_factory, run_id)
        assert len(rows_after) == 1
        assert (await read_run(uow_factory, run_id)).status is RunStatus.COMPLETED

    async def test_lease_fencing_during_approval_recovery(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """10. Lease fencing: if reconciler's lease is lost during recovery resume,
        reconciler aborts safely and does not overwrite state."""
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        harness = Harness()
        graph = harness.build(checkpointer)
        run_id = await create_run(uow_factory)
        approval_id = await _pause_run_for_approval(engine, graph, run_id, clock)

        async def crash_after_commit() -> None:
            raise RuntimeError("crash")

        service = ApprovalService(
            uow_factory=uow_factory,
            driver=LangGraphRunDriver(graph),
            clock=clock,
            lease=LEASE,
            owner="crashed-worker-fencing",
            before_resume=crash_after_commit,
        )

        with pytest.raises(RuntimeError):
            await service.decide_approval(
                approval_id,
                decision="approve",
                args_hash="hash-s1",
            )

        clock.advance(seconds=LEASE.ttl.total_seconds() + 5)

        blocked = asyncio.Event()

        class BlockingResumeDriver(LangGraphRunDriver):
            async def resume(
                self, rid: uuid.UUID, resume_value: str | None = None
            ) -> CheckpointInspection:
                blocked.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        rec = reconciler(engine, BlockingResumeDriver(graph), clock, "reconciler-B")
        rec_task = asyncio.create_task(rec.reconcile_once(limit=500))
        await blocked.wait()

        # Steal lease while reconciler is mid-resume
        clock.advance(seconds=LEASE.ttl.total_seconds() + 5)
        async with uow_factory() as uow:
            stolen = await uow.agent_runs.acquire_lease(
                run_id, owner="worker-C", now=clock.now(), ttl=LEASE.ttl
            )
            assert stolen is not None
            await uow.commit()

        report = await rec_task
        assert report.outcomes.get(run_id) is RecoveryOutcome.LEASE_LOST
        row = await read_run(uow_factory, run_id)
        assert row.lease_owner == "worker-C"
