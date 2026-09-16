"""Read-only trace retrieval and streaming (API-003, §13.4, §3.3, §14.3).

This is an observability surface over the *existing* durable trace system
(DB-002's `trace_events`, append-only, `seq` per-run monotonic): it never
appends a trace event, never mutates run state, and never touches the tool
dispatcher, `ApprovalGate`, or a checkpoint. `HTTP -> TraceService ->
TraceEventRepository` is the only path; no ORM query is built here or in the
API layer (§12, DB-005).

**Pagination.** `since_seq` is the first sequence number the caller wants
back (§13.4): the service converts it to the repository's `after_seq =
since_seq - 1` (clamped at 0, since `seq` starts at 1) and fetches one extra
row to decide whether another page exists without a second round trip.

**Completion.** A page is `complete` only when there is nothing more to read
*and* the run has reached a terminal status (`TERMINAL_RUN_STATUSES`,
§5.4) — an active run with no new events is merely caught up, not finished,
so `complete` stays `false` and `next_seq` stays the same cursor for the
caller's next poll.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.agent.state import TERMINAL_RUN_STATUSES, RunStatus
from app.errors import NotFoundError
from app.persistence.models import TraceEvent, TraceEventKind, TraceEventSeverity
from app.persistence.protocols import UnitOfWorkFactory

__all__ = [
    "TERMINAL_TRACE_KINDS",
    "TracePage",
    "TraceService",
]

#: The three structural trace emitters (`@traced_node`, `ToolRegistry.dispatch`,
#: the DB-007 reconciler) write exactly one of these when a run reaches a
#: `TERMINAL_RUN_STATUSES` status (§14.2) — the SSE stream closes right after
#: relaying one of them, matching the run's own terminal semantics rather than
#: inventing a second one.
TERMINAL_TRACE_KINDS: frozenset[TraceEventKind] = frozenset(
    {
        TraceEventKind.RUN_COMPLETED,
        TraceEventKind.RUN_FAILED,
        TraceEventKind.RUN_REJECTED,
        TraceEventKind.RUN_EXPIRED,
        TraceEventKind.RUN_CANCELLED,
    }
)

#: `TraceEventSeverity` declaration order *is* the severity ordering
#: (`debug < info < warning < error`) — read from the real enum rather than
#: hardcoded, so a future reordering cannot silently desync this module.
_SEVERITY_ORDER: dict[TraceEventSeverity, int] = {
    severity: index for index, severity in enumerate(TraceEventSeverity)
}

#: How many raw rows one SSE poll reads. Independent of the REST page size
#: (§13.4's `limit`) — the stream just keeps draining until it is caught up.
_SSE_POLL_BATCH = 200


@dataclass(frozen=True)
class TracePage:
    """One page of `GET /runs/{run_id}/trace` (§13.4)."""

    run_id: uuid.UUID
    events: list[TraceEvent]
    next_seq: int | None
    complete: bool


def _filter_events(
    rows: list[TraceEvent],
    *,
    kinds: list[TraceEventKind] | None,
    severity_min: TraceEventSeverity | None,
) -> list[TraceEvent]:
    """Presentation-layer filtering only (§13.4) — the cursor above is always
    computed from the *unfiltered* rows, so a filtered page never perturbs
    the underlying `seq` walk (no arbitrary SQL filtering, no skipped or
    duplicated events across differently-filtered requests)."""
    result = rows
    if kinds:
        kind_set = set(kinds)
        result = [row for row in result if row.kind in kind_set]
    if severity_min is not None:
        threshold = _SEVERITY_ORDER[severity_min]
        result = [row for row in result if _SEVERITY_ORDER[row.severity] >= threshold]
    return result


class TraceService:
    """The one caller of `TraceEventRepository` for observability (§13.4).

    Strictly read-only: every method opens a unit of work, reads, and commits
    the (empty) read-only transaction — it never calls `trace_events.append`,
    `agent_runs.update_status`/`transition_status`, or any other mutating
    repository method.
    """

    def __init__(self, *, uow_factory: UnitOfWorkFactory, payload_max_bytes: int) -> None:
        self._uow_factory = uow_factory
        self.payload_max_bytes = payload_max_bytes

    async def get_page(
        self,
        run_id: uuid.UUID,
        *,
        since_seq: int = 0,
        limit: int = 100,
        kinds: list[TraceEventKind] | None = None,
        severity_min: TraceEventSeverity | None = None,
    ) -> TracePage:
        """The REST page (§13.4). Raises `NotFoundError` for an unknown run."""
        after_seq = max(since_seq - 1, 0)
        async with self._uow_factory() as uow:
            run = await uow.agent_runs.get(run_id)
            if run is None:
                raise NotFoundError(f"Run {run_id} not found")
            fetched = await uow.trace_events.list_by_run(
                run_id, after_seq=after_seq, limit=limit + 1
            )
            await uow.commit()

        has_more = len(fetched) > limit
        page_rows = fetched[:limit]
        last_seq = page_rows[-1].seq if page_rows else after_seq
        events = _filter_events(page_rows, kinds=kinds, severity_min=severity_min)

        if has_more:
            return TracePage(run_id=run_id, events=events, next_seq=last_seq + 1, complete=False)
        if run.status in TERMINAL_RUN_STATUSES:
            return TracePage(run_id=run_id, events=events, next_seq=None, complete=True)
        return TracePage(run_id=run_id, events=events, next_seq=last_seq + 1, complete=False)

    async def ensure_run_exists(self, run_id: uuid.UUID) -> RunStatus:
        """The pre-stream existence check (§13.4's SSE endpoint): a 404 must
        be an ordinary RFC 9457 response, decided before any `text/event-stream`
        byte is written."""
        async with self._uow_factory() as uow:
            run = await uow.agent_runs.get(run_id)
            if run is None:
                raise NotFoundError(f"Run {run_id} not found")
            await uow.commit()
            return run.status

    async def poll_since(
        self, run_id: uuid.UUID, *, after_seq: int
    ) -> tuple[list[TraceEvent], RunStatus]:
        """One SSE poll: events strictly after `after_seq`, plus the run's
        current status so the stream can decide whether to keep polling or
        close. Every subscriber calls this independently with its own
        cursor — no shared state, no broker, PostgreSQL is the only source
        of truth (§3.3)."""
        async with self._uow_factory() as uow:
            run = await uow.agent_runs.get(run_id)
            if run is None:
                raise NotFoundError(f"Run {run_id} not found")
            events = await uow.trace_events.list_by_run(
                run_id, after_seq=after_seq, limit=_SSE_POLL_BATCH
            )
            await uow.commit()
            return events, run.status
