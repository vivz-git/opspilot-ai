"""The startup reconciler for orphaned runs (§2.4, §12.2, ADR-004, ADR-012,
ADR-023).

A process that dies mid-run leaves two things behind: a LangGraph checkpoint
(the resumable execution state — authoritative for *resumption*) and an
`agent_runs` row still in `running`/`queued` with a lease nobody renews (the
reporting state — authoritative for *reporting*). The reconciler brings the
two back into agreement, using the checkpoint to decide what the row should
say, never the other way round.

**Candidates.** Exactly the architecture's reconciler query (§12.3):
`status IN ('queued','running')` and the lease is expired or absent. A run
in `awaiting_approval` has no worker by design (§6.3) and is never a
candidate; a terminal run is never a candidate. This is what stops an
intentional pause from being mistaken for a crash.

**The deterministic state machine.** For each candidate:

| #   | Condition (after the claim)        | Row transition                | Resume | Event  |
|-----|------------------------------------|-------------------------------|--------|--------|
| R0  | claim lost to another worker       | none                          | no     | log    |
| R1  | no checkpoint exists               | `failed(orphaned)`, released  | no     | failed |
| R2  | checkpoint paused at `interrupt()` | `awaiting_approval`, released | no     | [1]    |
| R3  | checkpoint finished, has a status  | that status, released         | no     | [2]    |
| R3' | checkpoint finished, no status     | `failed(recovery_failed)`     | no     | failed |
| R4  | checkpoint mid-execution           | stays `running`, fresh lease  | yes    | [3]    |
| R5  | R4, and the resumed graph raised   | `failed(recovery_failed)`     | tried  | failed |
| R6  | R4, and the lease was lost         | none — someone else's now     | abort  | log    |
| R7  | a settling transaction failed      | none — rolled back as a unit  | —      | log    |

"failed" is `run_failed`; [1]–[3] are `run_recovered` with `status` =
`awaiting_approval`, the terminal status, and `resumed` respectively.
"Released" means the lease is cleared in the same statement as the
transition; no new owner survives the reconciliation except in R4, where
the reconciler itself is the run's worker until the graph next stops. In
R7 the run stays leased by the reconciler until that lease expires, after
which it is a candidate again — nothing partial is ever committed.

After R4 completes, the row is settled the same way the executor would
settle a fresh run: an interrupt → `awaiting_approval` (R2's transition,
silently); a finished graph → its terminal status (R3's). If the graph's own
nodes already wrote the terminal row (they do, once AGENT-00x lands), the
guarded transition matches zero rows and nothing is overwritten.

R2 is the DB-007 acceptance case "the process died between the checkpoint
and the row write": the interrupt was durably recorded but `status` never
became `awaiting_approval`. Without R2 the run would be re-entered (a
resume with no decision simply re-interrupts — harmless but wrong) or, worse,
failed as orphaned while a human was about to approve it.

**Why re-entry cannot duplicate a protected effect.** LangGraph re-executes
the node that was in flight when the process died (§9.7). If that node had
already performed a mutating tool call, the retry carries the same
attempt-invariant idempotency key `(run_id, step_id, args_hash)` (§10.4,
ADR-020) and the adapter's `UNIQUE(idempotency_key)` turns the repeat into
`duplicate_suppressed`; if the step was gated, the approval grant is in the
checkpointed `approval_state` and the `approvals` row, and `execute_tool`
re-asserts it (§9.5). Recovery adds no bypass and needs none — it is just
one more caller of the same execution path.

**Idempotency of reconciliation.** Every mutation is a conditional `UPDATE`
guarded by status and owner; the claim is one such update, so N reconcilers
over the same candidates hand each run to exactly one of them, and a second
pass finds nothing to do. Failure of any settling transaction rolls back as
a unit (status change and trace event share one unit of work), leaving the
run leased by the reconciler until that lease expires and it becomes a
candidate again.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

import structlog
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from app.agent.state import (
    TERMINAL_RUN_STATUSES,
    ApprovalDecisionKind,
    ApprovalStatus,
    RunStatus,
)
from app.execution.leases import LeaseConfig, LeaseHeartbeat, UnitOfWorkFactory
from app.persistence.checkpointing import DURABILITY, thread_config
from app.persistence.models import ApprovalRow, TraceEventKind, TraceEventSeverity
from app.runtime import Clock

__all__ = [
    "CheckpointInspection",
    "CheckpointPhase",
    "LangGraphRunDriver",
    "ReconciliationReport",
    "Reconciler",
    "RecoveryOutcome",
    "RunDriver",
]

_log = structlog.get_logger("opspilot.recovery")

#: `agent_runs.status_reason` values this module writes (§12.3's open list).
REASON_ORPHANED = "orphaned"
REASON_RECOVERY_FAILED = "recovery_failed"

_CLAIMABLE = (RunStatus.QUEUED, RunStatus.RUNNING)


class CheckpointPhase(StrEnum):
    NONE = "none"  # no checkpoint for this thread: nothing to resume
    PAUSED = "paused"  # a pending `interrupt()`: waiting for a human
    IN_PROGRESS = "in_progress"  # a node is next; the worker died mid-run
    FINISHED = "finished"  # the graph reached END


@dataclass(frozen=True)
class CheckpointInspection:
    phase: CheckpointPhase
    checkpoint_id: str | None = None
    next_nodes: tuple[str, ...] = ()
    #: The `status`/`status_reason` channels of the checkpointed state, when
    #: the graph carries them (`AgentState` does). Used to settle a finished
    #: checkpoint's row without re-running anything.
    status: RunStatus | None = None
    status_reason: str | None = None
    step_id: str | None = None


class RunDriver(Protocol):
    """What the reconciler needs from a graph: look, and continue."""

    async def inspect(self, run_id: uuid.UUID) -> CheckpointInspection: ...

    async def resume(
        self, run_id: uuid.UUID, resume_value: str | None = None
    ) -> CheckpointInspection:
        """Continue from the durable checkpoint with optional resume_value (for
        interrupted gates); return the resulting state."""
        ...


class LangGraphRunDriver:
    """`RunDriver` over a compiled LangGraph graph whose checkpointer is the
    Postgres saver (`app.persistence.checkpointing`). `thread_id` is the run
    id, so a run is addressed the same way here as everywhere else."""

    def __init__(self, graph: CompiledStateGraph[Any, Any, Any, Any]) -> None:
        self._graph = graph

    async def inspect(self, run_id: uuid.UUID) -> CheckpointInspection:
        snapshot = await self._graph.aget_state(thread_config(run_id))
        return classify_snapshot(snapshot)

    async def resume(
        self, run_id: uuid.UUID, resume_value: str | None = None
    ) -> CheckpointInspection:
        # `None` input = "continue from the checkpoint". When resume_value is
        # provided, Command(resume=decision) unblocks the interrupt gate.
        input_data: Any = Command(resume=resume_value) if resume_value is not None else None
        await self._graph.ainvoke(input_data, thread_config(run_id), durability=DURABILITY)
        return await self.inspect(run_id)


def classify_snapshot(snapshot: StateSnapshot) -> CheckpointInspection:
    """Map a `StateSnapshot` onto the four phases the reconciler distinguishes."""
    configurable: dict[str, Any] = (snapshot.config or {}).get("configurable") or {}
    checkpoint_id = configurable.get("checkpoint_id")
    if checkpoint_id is None:
        return CheckpointInspection(phase=CheckpointPhase.NONE)

    values: dict[str, Any] = snapshot.values if isinstance(snapshot.values, dict) else {}
    status = _run_status(values.get("status"))
    status_reason = values.get("status_reason")
    next_nodes = tuple(snapshot.next)

    interrupted = bool(snapshot.interrupts) or any(task.interrupts for task in snapshot.tasks)
    if interrupted:
        phase = CheckpointPhase.PAUSED
    elif next_nodes:
        phase = CheckpointPhase.IN_PROGRESS
    else:
        phase = CheckpointPhase.FINISHED

    step_id: str | None = None
    if isinstance(values.get("current_step_id"), str) and values["current_step_id"]:
        step_id = values["current_step_id"]
    if step_id is None and interrupted:
        all_interrupts = list(snapshot.interrupts)
        for task in snapshot.tasks:
            all_interrupts.extend(task.interrupts)
        for intr in all_interrupts:
            if isinstance(intr.value, dict) and intr.value.get("step_id"):
                step_id = str(intr.value["step_id"])
                break

    return CheckpointInspection(
        phase=phase,
        checkpoint_id=str(checkpoint_id),
        next_nodes=next_nodes,
        status=status,
        status_reason=status_reason if isinstance(status_reason, str) else None,
        step_id=step_id,
    )


def _run_status(value: object) -> RunStatus | None:
    if value is None:
        return None
    try:
        return RunStatus(str(value))
    except ValueError:
        return None


class RecoveryOutcome(StrEnum):
    SKIPPED = "skipped"  # R0
    ORPHANED = "orphaned"  # R1
    PAUSED = "paused"  # R2
    FINALIZED = "finalized"  # R3 / R3'
    RESUMED = "resumed"  # R4 (and settled)
    RECOVERY_FAILED = "recovery_failed"  # R5
    LEASE_LOST = "lease_lost"  # R6
    ERRORED = "errored"  # R7


@dataclass
class ReconciliationReport:
    checked_at: datetime
    outcomes: dict[uuid.UUID, RecoveryOutcome] = field(default_factory=dict)

    def count(self, outcome: RecoveryOutcome) -> int:
        return sum(1 for value in self.outcomes.values() if value is outcome)

    @property
    def candidates(self) -> int:
        return len(self.outcomes)


@dataclass(frozen=True)
class _Candidate:
    run_id: uuid.UUID
    status: RunStatus
    previous_owner: str | None
    lease_expired_at: datetime | None
    started_at: datetime | None


class Reconciler:
    """Finds orphaned runs and settles each one per the state machine above.

    `owner` is this reconciler's worker identity: while it resumes a run it
    *is* that run's worker, with a lease and a heartbeat like any other.
    """

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        driver: RunDriver,
        clock: Clock,
        owner: str,
        lease: LeaseConfig,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._uow_factory = uow_factory
        self._driver = driver
        self._clock = clock
        self._owner = owner
        self._lease = lease
        self._sleep = sleep

    @property
    def owner(self) -> str:
        return self._owner

    async def reconcile_once(self, *, limit: int = 50) -> ReconciliationReport:
        """One pass over every current candidate. Safe to call repeatedly and
        concurrently with other reconcilers."""
        now = self._clock.now()
        async with self._uow_factory() as uow:
            rows = await uow.agent_runs.list_orphaned_runs(now=now, limit=limit)
            candidates = [
                _Candidate(
                    run_id=row.id,
                    status=row.status,
                    previous_owner=row.lease_owner,
                    lease_expired_at=row.lease_expires_at,
                    started_at=row.started_at,
                )
                for row in rows
            ]
        report = ReconciliationReport(checked_at=now)
        _log.info("reconcile_started", owner=self._owner, candidates=len(candidates))
        for candidate in candidates:
            report.outcomes[candidate.run_id] = await self._recover_guarded(candidate)
        _log.info(
            "reconcile_finished",
            owner=self._owner,
            **{outcome.value: report.count(outcome) for outcome in RecoveryOutcome},
        )
        return report

    async def recover_run(self, run_id: uuid.UUID) -> RecoveryOutcome:
        """Recover one specific run (used by the pass above; public for
        targeted recovery). `SKIPPED` if it is not currently an orphan."""
        async with self._uow_factory() as uow:
            row = await uow.agent_runs.get(run_id)
            if row is None:
                return RecoveryOutcome.SKIPPED
            candidate = _Candidate(
                run_id=row.id,
                status=row.status,
                previous_owner=row.lease_owner,
                lease_expired_at=row.lease_expires_at,
                started_at=row.started_at,
            )
        return await self._recover_guarded(candidate)

    # -- one run ---------------------------------------------------------

    async def _recover_guarded(self, candidate: _Candidate) -> RecoveryOutcome:
        try:
            return await self._recover(candidate)
        except Exception as exc:  # noqa: BLE001 - R7: one poison run must not stop the pass
            _log.error(
                "recovery_errored",
                run_id=str(candidate.run_id),
                owner=self._owner,
                error=repr(exc),
            )
            return RecoveryOutcome.ERRORED

    async def _find_decided_approval(
        self, run_id: uuid.UUID, step_id: str | None = None
    ) -> ApprovalRow | None:
        async with self._uow_factory() as uow:
            approvals = await uow.approvals.list_by_run(run_id)
            await uow.commit()

        if not approvals:
            return None

        # Step-specific lookup: when step_id is known from the paused checkpoint
        if step_id is not None:
            step_approvals = [a for a in approvals if a.step_id == step_id]
            if step_approvals:
                if any(a.status is ApprovalStatus.PENDING for a in step_approvals):
                    return None
                decided = [
                    a
                    for a in step_approvals
                    if a.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)
                ]
                if not decided:
                    return None
                decided.sort(
                    key=lambda a: (a.decided_at or a.requested_at, a.requested_at),
                    reverse=True,
                )
                return decided[0]

        # Fallback when step_id was not captured: check entire run
        if any(a.status is ApprovalStatus.PENDING for a in approvals):
            return None

        decided_all = [
            a for a in approvals if a.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)
        ]
        if not decided_all:
            return None
        decided_all.sort(
            key=lambda a: (a.decided_at or a.requested_at, a.requested_at),
            reverse=True,
        )
        return decided_all[0]

    async def _recover(self, candidate: _Candidate) -> RecoveryOutcome:
        run_id = candidate.run_id
        log = _log.bind(run_id=str(run_id), owner=self._owner)

        # R0 — the claim. One conditional UPDATE, committed on its own so the
        # lease is visible to every other reconciler before any slow work.
        async with self._uow_factory() as uow:
            claimed = await uow.agent_runs.acquire_lease(
                run_id, owner=self._owner, now=self._clock.now(), ttl=self._lease.ttl
            )
            if claimed is None:
                log.info("recovery_skipped", reason="claimed_elsewhere_or_not_orphaned")
                return RecoveryOutcome.SKIPPED
            await uow.commit()
            # The claimed row is the truth from here on; the listing was a snapshot.
            candidate = replace(candidate, status=claimed.status, started_at=claimed.started_at)
        log.info(
            "orphan_claimed",
            previous_owner=candidate.previous_owner,
            lease_expired_at=_iso(candidate.lease_expired_at),
            status=str(claimed.status),
        )
        provenance: dict[str, Any] = {
            "previous_owner": candidate.previous_owner,
            "lease_expired_at": _iso(candidate.lease_expired_at),
            "recovered_by": self._owner,
        }

        try:
            inspection = await self._driver.inspect(run_id)
        except Exception as exc:  # noqa: BLE001 - a broken checkpoint must still settle the row
            log.error("recovery_inspect_failed", error=repr(exc))
            await self._fail(
                candidate,
                reason=REASON_RECOVERY_FAILED,
                detail="checkpoint could not be inspected",
                error=exc,
                payload={**provenance, "recovery": "inspect_raised"},
            )
            return RecoveryOutcome.RECOVERY_FAILED

        provenance["checkpoint_id"] = inspection.checkpoint_id
        provenance["next_nodes"] = list(inspection.next_nodes)

        if inspection.phase is CheckpointPhase.NONE:  # R1
            log.warning("orphan_without_checkpoint")
            await self._fail(
                candidate,
                reason=REASON_ORPHANED,
                detail="worker died before any checkpoint was written",
                error=None,
                payload={**provenance, "recovery": "no_checkpoint"},
            )
            return RecoveryOutcome.ORPHANED

        if inspection.phase is CheckpointPhase.PAUSED:  # R2 / crash-window resume
            decided_approval = await self._find_decided_approval(
                candidate.run_id, inspection.step_id
            )
            if decided_approval is None:
                log.info("orphan_is_paused_for_approval", checkpoint_id=inspection.checkpoint_id)
                await self._settle_paused(candidate, event=True, payload=provenance)
                return RecoveryOutcome.PAUSED

            stored_decision = (
                ApprovalDecisionKind.APPROVE.value
                if decided_approval.status == ApprovalStatus.APPROVED
                else ApprovalDecisionKind.REJECT.value
            )
            provenance["approval_id"] = str(decided_approval.id)
            provenance["approval_decision"] = stored_decision
            log.info(
                "orphan_is_paused_with_decision_resuming",
                checkpoint_id=inspection.checkpoint_id,
                approval_id=str(decided_approval.id),
                decision=stored_decision,
            )
            await self._emit_recovered(candidate, status="resumed", payload=provenance)
            return await self._resume(candidate, provenance, resume_value=stored_decision)

        if inspection.phase is CheckpointPhase.FINISHED:  # R3 / R3'
            await self._settle_finished(candidate, inspection, event=True, payload=provenance)
            return RecoveryOutcome.FINALIZED

        # R4 — resume from the checkpoint under our own heartbeat.
        log.info("recovery_resuming", checkpoint_id=inspection.checkpoint_id)
        await self._emit_recovered(candidate, status="resumed", payload=provenance)
        return await self._resume(candidate, provenance)

    async def _resume(
        self,
        candidate: _Candidate,
        provenance: dict[str, Any],
        *,
        resume_value: str | None = None,
    ) -> RecoveryOutcome:
        run_id = candidate.run_id
        log = _log.bind(run_id=str(run_id), owner=self._owner)
        if candidate.status in (RunStatus.QUEUED, RunStatus.AWAITING_APPROVAL):
            async with self._uow_factory() as uow:
                await uow.agent_runs.transition_status(
                    run_id,
                    expected=(candidate.status,),
                    status=RunStatus.RUNNING,
                    owner=self._owner,
                    started_at=self._clock.now(),
                )
                await uow.commit()

        heartbeat = LeaseHeartbeat(
            uow_factory=self._uow_factory,
            run_id=run_id,
            owner=self._owner,
            clock=self._clock,
            config=self._lease,
            sleep=self._sleep,
        )
        heartbeat.start()
        resume_coro = (
            self._driver.resume(run_id, resume_value=resume_value)
            if resume_value is not None
            else self._driver.resume(run_id)
        )
        resume_task = asyncio.create_task(resume_coro, name=f"resume:{run_id}")
        lost_task = asyncio.create_task(heartbeat.lost.wait(), name=f"lease-watch:{run_id}")
        try:
            done, _ = await asyncio.wait(
                {resume_task, lost_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if lost_task in done and resume_task not in done:  # R6
                log.warning("recovery_abandoned", reason="lease_lost")
                return RecoveryOutcome.LEASE_LOST
            try:
                result = resume_task.result()
            except Exception as exc:  # noqa: BLE001 - any graph failure settles the row, never leaks
                log.error("recovery_resume_failed", error=repr(exc))
                await self._fail(  # R5
                    candidate,
                    reason=REASON_RECOVERY_FAILED,
                    detail="graph raised while resuming from checkpoint",
                    error=exc,
                    payload={**provenance, "recovery": "resume_raised"},
                )
                return RecoveryOutcome.RECOVERY_FAILED
        finally:
            # Whatever path exits — R6, R5, success, or this reconciler being
            # cancelled mid-resume — nothing is left running on our behalf.
            for task in (resume_task, lost_task):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
            await heartbeat.stop()

        if heartbeat.lost.is_set():
            # The graph finished, but our lease was refused meanwhile: the
            # run is owned elsewhere and its row is not ours to settle.
            log.warning("recovery_abandoned", reason="lease_lost_after_resume")
            return RecoveryOutcome.LEASE_LOST

        if result.phase is CheckpointPhase.PAUSED:
            await self._settle_paused(candidate, event=False, payload=provenance)
        else:
            await self._settle_finished(candidate, result, event=False, payload=provenance)
        log.info("recovery_resumed", outcome=result.phase.value)
        return RecoveryOutcome.RESUMED

    # -- settling the row ------------------------------------------------

    async def _settle_paused(
        self, candidate: _Candidate, *, event: bool, payload: dict[str, Any]
    ) -> None:
        async with self._uow_factory() as uow:
            row = await uow.agent_runs.transition_status(
                candidate.run_id,
                expected=_CLAIMABLE,
                status=RunStatus.AWAITING_APPROVAL,
                owner=self._owner,
                release_lease=True,
            )
            if row is not None and event:
                await uow.trace_events.append(
                    run_id=candidate.run_id,
                    kind=TraceEventKind.RUN_RECOVERED,
                    status=RunStatus.AWAITING_APPROVAL.value,
                    payload={**payload, "recovery": "awaiting_approval"},
                )
            if row is None:
                await uow.agent_runs.release_lease(candidate.run_id, owner=self._owner)
            await uow.commit()

    async def _settle_finished(
        self,
        candidate: _Candidate,
        inspection: CheckpointInspection,
        *,
        event: bool,
        payload: dict[str, Any],
    ) -> None:
        terminal = inspection.status
        if terminal is None or terminal not in TERMINAL_RUN_STATUSES:  # R3'
            await self._fail(
                candidate,
                reason=REASON_RECOVERY_FAILED,
                detail="checkpoint finished without a terminal status",
                error=None,
                payload={**payload, "recovery": "finished_without_status"},
            )
            return
        now = self._clock.now()
        async with self._uow_factory() as uow:
            row = await uow.agent_runs.transition_status(
                candidate.run_id,
                expected=_CLAIMABLE,
                status=terminal,
                owner=self._owner,
                status_reason=inspection.status_reason,
                finished_at=now,
                duration_ms=_duration_ms(candidate.started_at, now),
                release_lease=True,
            )
            if row is not None and event:
                await uow.trace_events.append(
                    run_id=candidate.run_id,
                    kind=TraceEventKind.RUN_RECOVERED,
                    status=terminal.value,
                    payload={**payload, "recovery": "finalized"},
                )
            if row is None:
                await uow.agent_runs.release_lease(candidate.run_id, owner=self._owner)
            await uow.commit()

    async def _fail(
        self,
        candidate: _Candidate,
        *,
        reason: str,
        detail: str,
        error: BaseException | None,
        payload: dict[str, Any],
    ) -> None:
        now = self._clock.now()
        severity = TraceEventSeverity.ERROR if error is not None else TraceEventSeverity.WARNING
        async with self._uow_factory() as uow:
            row = await uow.agent_runs.transition_status(
                candidate.run_id,
                expected=_CLAIMABLE,
                status=RunStatus.FAILED,
                owner=self._owner,
                status_reason=reason,
                finished_at=now,
                duration_ms=_duration_ms(candidate.started_at, now),
                release_lease=True,
            )
            if row is not None:
                await uow.trace_events.append(
                    run_id=candidate.run_id,
                    kind=TraceEventKind.RUN_FAILED,
                    severity=severity,
                    status=RunStatus.FAILED.value,
                    error={
                        "class": reason,
                        "message": detail,
                        "detail": repr(error) if error is not None else None,
                    },
                    payload=payload,
                )
            else:
                await uow.agent_runs.release_lease(candidate.run_id, owner=self._owner)
            await uow.commit()

    async def _emit_recovered(
        self, candidate: _Candidate, *, status: str, payload: dict[str, Any]
    ) -> None:
        async with self._uow_factory() as uow:
            await uow.trace_events.append(
                run_id=candidate.run_id,
                kind=TraceEventKind.RUN_RECOVERED,
                status=status,
                payload={**payload, "recovery": status},
            )
            await uow.commit()


def _duration_ms(started_at: datetime | None, finished_at: datetime) -> int | None:
    if started_at is None:
        return None
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
