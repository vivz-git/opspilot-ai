"""Terminal settlement: the one way a run's row is ended (§5.4, §12.3, §14.2).

Three components end runs — `Executor` (a graph it drove to the end, or one
that raised), `ApprovalService` (a graph resumed by a human decision) and the
`Reconciler` (a run whose worker died). They used to each carry their own
copy of "transition the row, then append the terminal event", and the copies
drifted: the reconciler wrote `run_recovered` where the run's terminal event
belonged, and the approval path never materialised `duration_ms`. Every
terminal settle now goes through `settle_terminal`, so the policy exists
once:

1. **The row.** One conditional `transition_status` — guarded by `expected`
   and, when given, the lease `owner` — to the terminal status, with
   `finished_at`, `duration_ms` (`elapsed_ms` from `started_at`, on the same
   injected clock), `final_response`, and the lease released in the same
   statement. `None` means someone else moved the run; that verdict stands.
2. **The trace.** Only when this call won the transition: optionally a
   `run_recovered` *observation* (the reconciler finalising a checkpoint
   that had already finished), then the run's terminal event from
   `TERMINAL_TRACE_EVENTS` — always last, because the console closes its
   event stream on it (`use-run-events.ts`). A recovered run therefore ends
   with exactly the event an equivalent uninterrupted run ends with.
3. **The step projection.** Once the run is terminal — by this call, or
   because an operator cancelled it while its graph wound down — no
   `execution_steps` row may still claim to be executing: any left
   unsettled is closed `failed` with the run's reason, its
   `verification_status` untouched (an unconfirmed effect stays
   unconfirmed, P5). The run's `step_count`/`retry_total` are recounted from
   those rows in the same transaction.

All three share the caller's unit of work, so they commit or roll back
together (the reconciler's R7).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from app.agent.state import TERMINAL_RUN_STATUSES, RunStatus
from app.persistence.models import (
    TERMINAL_TRACE_EVENTS,
    AgentRun,
    TraceEventKind,
    TraceEventSeverity,
)
from app.persistence.protocols import UnitOfWork
from app.runtime import elapsed_ms

__all__ = ["close_step_projection", "settle_terminal"]


async def settle_terminal(
    uow: UnitOfWork,
    *,
    run_id: uuid.UUID,
    expected: Iterable[RunStatus],
    owner: str | None,
    status: RunStatus,
    status_reason: str | None,
    now: datetime,
    started_at: datetime | None,
    payload: dict[str, Any],
    final_response: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    severity: TraceEventSeverity | None = None,
    recovered: dict[str, Any] | None = None,
) -> AgentRun | None:
    """End `run_id` as `status`, or return `None` if it was not ours to end.

    `payload`/`error`/`severity` describe the terminal event; `severity`
    defaults to `warning` for `failed` and `info` otherwise. `recovered`,
    when given, is the payload of a `run_recovered` observation written
    immediately before the terminal event.
    """
    if status not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"settle_terminal needs a terminal status, got {status.value}")
    row = await uow.agent_runs.transition_status(
        run_id,
        expected=expected,
        status=status,
        owner=owner,
        status_reason=status_reason,
        finished_at=now,
        duration_ms=elapsed_ms(started_at, now),
        final_response=final_response,
        release_lease=True,
    )
    if row is not None:
        if recovered is not None:
            await uow.trace_events.append(
                run_id=run_id,
                kind=TraceEventKind.RUN_RECOVERED,
                status=status.value,
                payload=recovered,
            )
        await uow.trace_events.append(
            run_id=run_id,
            kind=TERMINAL_TRACE_EVENTS[status],
            severity=severity or _terminal_severity(status),
            status=status.value,
            duration_ms=row.duration_ms,
            error=error,
            payload=payload,
        )
    await close_step_projection(uow, run_id=run_id, now=now)
    return row


async def close_step_projection(uow: UnitOfWork, *, run_id: uuid.UUID, now: datetime) -> None:
    """Bring a *terminal* run's `execution_steps` and counters to rest.

    A no-op for a run that is not terminal (paused, still running under
    another owner, or unknown): nothing is closed while it can still move.
    """
    run = await uow.agent_runs.get(run_id)
    if run is None or run.status not in TERMINAL_RUN_STATUSES:
        return
    reason = run.status_reason or run.status.value
    await uow.execution_steps.settle_unsettled(
        run_id,
        finished_at=now,
        error={
            "class": reason,
            "message": f"the run ended {run.status.value} before this step settled",
            "detail": None,
        },
    )
    await uow.agent_runs.refresh_step_counters(run_id)


def _terminal_severity(status: RunStatus) -> TraceEventSeverity:
    return TraceEventSeverity.WARNING if status is RunStatus.FAILED else TraceEventSeverity.INFO
