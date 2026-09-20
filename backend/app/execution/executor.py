"""The in-process run executor (§2.4, §5.4, ADR-004, ADR-023, API-007).

`Executor` owns the `asyncio` task that drives one run's graph from
`queued` to wherever the graph next stops. It is orchestration only: it
never calls a tool, touches CRM data or decides an approval — the graph's
nodes do that through `ToolRegistry` and the approval gate, and the
executor only owns the run while they run.

**Lifecycle (§5.4).** `RunService.start_run` performs the durable
`created → queued` transition and calls `schedule`; the response returns
before anything else happens. The background task then:

1. acquires the run lease (`hold_lease`, DB-007) — or exits without
   touching anything if another live worker owns the run;
2. moves `queued → running` under that lease;
3. enters the graph while the heartbeat renews the lease;
4. settles the row from what the checkpoint says, the same way the
   reconciler and the approval service do: a pause → `awaiting_approval`
   with the lease released; a finished graph → its terminal status, its
   `status_reason` and its `final_response`, with a terminal trace event;
   a graph that raised → `failed(execution_failed)`.

**A pause is not a live worker.** When `request_approval` interrupts, the
graph call returns, the row becomes `awaiting_approval`, the lease is
released and this task ends (§6.3). The resume is `ApprovalService`'s
(§9.6): the decision transaction re-acquires ownership and re-enters the
graph — one resume path, not two.

**Fencing.** The graph task runs alongside `heartbeat.lost.wait()`. A
refused heartbeat cancels the graph task at once (R6 in `recovery`); the
row is then someone else's and is not settled here. Every settling write
is a conditional `UPDATE` guarded by `status = running AND lease_owner =
me`, so a run that was cancelled or reclaimed meanwhile is never
overwritten — the database, not this task, has the last word.

**Cancellation stays cooperative.** `RunService.cancel_run` flags the
`CancellationSource` the nodes consult at their boundaries; a tool in
flight finishes and the graph exits through `fail(cancelled)`. The row is
already `cancelled` by then, so the guarded settle matches nothing.

**Shutdown.** `shutdown()` cancels every in-flight task; `hold_lease`
releases each lease on the way out, leaving `running` rows with no owner
for the reconciler to resume from their checkpoints at the next start.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

import structlog

from app.agent.nodes import create_initial_state
from app.agent.state import TERMINAL_RUN_STATUSES, Budgets, RunMetadata, RunStatus
from app.execution.leases import LeaseConfig, LeaseNotAcquired, UnitOfWorkFactory, hold_lease
from app.execution.recovery import CheckpointInspection, CheckpointPhase, RunDriver
from app.persistence.models import (
    TERMINAL_TRACE_EVENTS,
    AgentRun,
    TraceEventKind,
    TraceEventSeverity,
)
from app.runtime import Clock

__all__ = [
    "REASON_EXECUTION_FAILED",
    "ExecutionOutcome",
    "Executor",
]

_log = structlog.get_logger("opspilot.executor")

#: `agent_runs.status_reason` when the graph itself raised (§12.3's open
#: list, beside `orphaned`, `recovery_failed` and `resume_failed`).
REASON_EXECUTION_FAILED: Final = "execution_failed"


class ExecutionOutcome(StrEnum):
    NOT_ACQUIRED = "not_acquired"  # another worker owns the run, or it moved on
    PAUSED = "paused"  # interrupted for a human; lease released
    FINISHED = "finished"  # the graph ended; row settled (or already terminal)
    FAILED = "failed"  # the graph raised; row failed(execution_failed)
    LEASE_LOST = "lease_lost"  # heartbeat refused; the run is someone else's
    ERRORED = "errored"  # a control-plane write failed; the reconciler will see the row


class Executor:
    """Owns the background task that drives one run (§2.4). One per process,
    identified by `owner` for the lifetime of that process."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        driver: RunDriver,
        clock: Clock,
        lease: LeaseConfig,
        owner: str,
        budgets: Budgets,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._uow_factory = uow_factory
        self._driver = driver
        self._clock = clock
        self._lease = lease
        self._owner = owner
        self._budgets = budgets
        self._sleep = sleep
        self._tasks: dict[uuid.UUID, asyncio.Task[ExecutionOutcome]] = {}

    @property
    def owner(self) -> str:
        return self._owner

    def schedule(self, run_id: uuid.UUID) -> asyncio.Task[ExecutionOutcome]:
        """Start driving `run_id` in the background. Idempotent while a task
        for the run is still alive in this process; across processes the
        lease decides."""
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            return task
        task = asyncio.create_task(self.execute(run_id), name=f"run:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run_id, None))
        return task

    async def shutdown(self) -> None:
        """Stop every in-flight run. Leases are released by `hold_lease`; the
        rows stay `running` for the reconciler at the next start."""
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def execute(self, run_id: uuid.UUID) -> ExecutionOutcome:
        """Drive one queued run to its next stop. Safe to call for a run that
        is no longer ours to run: the lease refuses, and nothing happens."""
        log = _log.bind(run_id=str(run_id), owner=self._owner)
        try:
            async with hold_lease(
                uow_factory=self._uow_factory,
                run_id=run_id,
                owner=self._owner,
                clock=self._clock,
                config=self._lease,
                expected=(RunStatus.QUEUED,),
                sleep=self._sleep,
            ) as (claimed, heartbeat):
                async with self._uow_factory() as uow:
                    run = await uow.agent_runs.transition_status(
                        run_id,
                        expected=(RunStatus.QUEUED,),
                        status=RunStatus.RUNNING,
                        owner=self._owner,
                        started_at=claimed.started_at or self._clock.now(),
                    )
                    await uow.commit()
                if run is None:
                    log.info("execution_skipped", reason="run_moved_before_running")
                    return ExecutionOutcome.NOT_ACQUIRED
                log.info("execution_started")

                graph_task = asyncio.create_task(
                    self._driver.start(run_id, self._initial_state(run)), name=f"graph:{run_id}"
                )
                lost_task = asyncio.create_task(heartbeat.lost.wait(), name=f"lease-watch:{run_id}")
                try:
                    done, _ = await asyncio.wait(
                        {graph_task, lost_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if graph_task not in done:
                        log.warning("execution_abandoned", reason="lease_lost")
                        return ExecutionOutcome.LEASE_LOST
                    try:
                        result = graph_task.result()
                    except Exception as exc:  # noqa: BLE001 - any graph failure settles the row
                        if heartbeat.lost.is_set():
                            log.warning("execution_abandoned", reason="lease_lost_after_raise")
                            return ExecutionOutcome.LEASE_LOST
                        log.error("execution_failed", error=repr(exc))
                        await self._fail(run, exc)
                        return ExecutionOutcome.FAILED
                finally:
                    # Whatever path exits — lease lost, the graph raised, this
                    # task cancelled at shutdown — nothing keeps running for us.
                    for task in (graph_task, lost_task):
                        if not task.done():
                            task.cancel()
                            with suppress(asyncio.CancelledError, Exception):
                                await task

                if heartbeat.lost.is_set():
                    log.warning("execution_abandoned", reason="lease_lost_after_finish")
                    return ExecutionOutcome.LEASE_LOST
                return await self._settle(run, result)
        except LeaseNotAcquired:
            log.info("execution_skipped", reason="lease_not_acquired")
            return ExecutionOutcome.NOT_ACQUIRED
        except Exception as exc:  # noqa: BLE001 - a background task must not lose its failure
            # A control-plane write (not the graph) failed: `hold_lease` has
            # released the lease, the row says what it said, and the
            # reconciler settles it from the checkpoint at the next start.
            log.error("execution_errored", error=repr(exc))
            return ExecutionOutcome.ERRORED

    # -- the graph's input ------------------------------------------------

    def _initial_state(self, run: AgentRun) -> dict[str, Any]:
        metadata = RunMetadata(
            planner_kind=run.planner_kind,
            model_id=run.model_id,
            prompt_version=run.prompt_version or "v1",
            seed=run.seed,
            actor_id=run.actor_id,
            budgets=self._budgets,
            evaluation_run_id=str(run.evaluation_run_id) if run.evaluation_run_id else None,
            eval_case_id=run.eval_case_id,
            extra=dict(run.metadata_ or {}),
        )
        return dict(
            create_initial_state(
                run.id,
                run.user_request,
                metadata=metadata,
                deadline_at=run.deadline_at,
                clock=self._clock,
            )
        )

    # -- settling the row ---------------------------------------------------

    async def _settle(self, run: AgentRun, result: CheckpointInspection) -> ExecutionOutcome:
        log = _log.bind(run_id=str(run.id), owner=self._owner)
        if result.phase is CheckpointPhase.PAUSED:
            async with self._uow_factory() as uow:
                row = await uow.agent_runs.transition_status(
                    run.id,
                    expected=(RunStatus.RUNNING,),
                    status=RunStatus.AWAITING_APPROVAL,
                    owner=self._owner,
                    release_lease=True,
                )
                await uow.commit()
            log.info("execution_paused", settled=row is not None)
            return ExecutionOutcome.PAUSED

        terminal = result.status
        if terminal is None or terminal not in TERMINAL_RUN_STATUSES:
            # A graph that reached END without a terminal status is a bug in
            # the graph, and the row must still stop saying `running`.
            await self._fail(run, None, detail="graph finished without a terminal status")
            return ExecutionOutcome.FAILED

        now = self._clock.now()
        async with self._uow_factory() as uow:
            row = await uow.agent_runs.transition_status(
                run.id,
                expected=(RunStatus.RUNNING,),
                status=terminal,
                owner=self._owner,
                status_reason=result.status_reason,
                finished_at=now,
                duration_ms=_duration_ms(run.started_at, now),
                final_response=result.final_response,
                release_lease=True,
            )
            if row is not None:
                await uow.trace_events.append(
                    run_id=run.id,
                    kind=TERMINAL_TRACE_EVENTS[terminal],
                    severity=(
                        TraceEventSeverity.WARNING
                        if terminal is RunStatus.FAILED
                        else TraceEventSeverity.INFO
                    ),
                    status=terminal.value,
                    duration_ms=row.duration_ms,
                    payload={"status_reason": result.status_reason, "owner": self._owner},
                )
            await uow.commit()
        # `row is None` means the run was already terminal — cancelled by the
        # operator while the graph wound down — and that verdict stands.
        log.info("execution_finished", status=terminal.value, settled=row is not None)
        return ExecutionOutcome.FINISHED

    async def _fail(
        self,
        run: AgentRun,
        error: BaseException | None,
        *,
        detail: str = "graph raised during execution",
    ) -> None:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            row = await uow.agent_runs.transition_status(
                run.id,
                expected=(RunStatus.RUNNING,),
                status=RunStatus.FAILED,
                owner=self._owner,
                status_reason=REASON_EXECUTION_FAILED,
                finished_at=now,
                duration_ms=_duration_ms(run.started_at, now),
                release_lease=True,
            )
            if row is not None:
                await uow.trace_events.append(
                    run_id=run.id,
                    kind=TraceEventKind.RUN_FAILED,
                    severity=TraceEventSeverity.ERROR,
                    status=RunStatus.FAILED.value,
                    error={
                        "class": REASON_EXECUTION_FAILED,
                        "message": detail,
                        "detail": repr(error) if error is not None else None,
                    },
                    payload={"owner": self._owner},
                )
            await uow.commit()


def _duration_ms(started_at: datetime | None, finished_at: datetime) -> int | None:
    if started_at is None:
        return None
    return max(0, int((finished_at - started_at).total_seconds() * 1000))
