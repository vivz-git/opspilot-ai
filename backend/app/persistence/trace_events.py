"""Per-run monotonic `seq` allocation for `trace_events` (§12.7, DB-002).

**The concurrency problem.** Two concurrent writers appending events for the
same run must never produce a duplicate or a gapped `seq`.
`UNIQUE(run_id, seq)` alone does not guarantee this: under Postgres's default
`READ COMMITTED` isolation, two transactions can both read the same
`MAX(seq)` before either has inserted, both compute the same "next" value,
and race to insert it — one wins, the other gets an `IntegrityError` it must
retry, and neither outcome is a gap, but the naive read-then-insert is a real
race that a busy run would hit constantly. Retrying in a loop after a
constraint violation would work, but it burns work under contention and pushes
the retry policy onto every caller.

**The chosen mechanism.** A transaction-scoped Postgres advisory lock, keyed
by `run_id`, taken immediately before the read-then-insert:

    SELECT pg_advisory_xact_lock(hashtextextended(run_id::text, 0));
    SELECT coalesce(max(seq), 0) + 1 FROM trace_events WHERE run_id = :run_id;
    INSERT INTO trace_events (..., seq) VALUES (..., :next_seq);

`pg_advisory_xact_lock` blocks the caller until the lock is free and releases
it automatically at `COMMIT` or `ROLLBACK` — no explicit unlock, and no
leaked lock if the transaction fails. This serializes the read-then-insert
for a single run to one writer at a time, which is exactly the granularity
that matters: two concurrent writers for *different* runs still proceed
independently (`hashtextextended`'s 64-bit output makes a collision between
two concurrently-active run ids practically impossible; a spurious collision
would only cost extra serialization between unrelated runs, never an
incorrect `seq`). `UNIQUE(run_id, seq)` remains as the database-level
backstop that turns any bug that bypasses this function into a loud
`IntegrityError` instead of a silently wrong trace, not as the mechanism that
makes concurrent appends correct in the first place — that is this lock.

This is the simplest mechanism that is actually correct under concurrency
without adding a new piece of infrastructure (no Redis, no per-run row to
lock via `SELECT ... FOR UPDATE` that would need to exist before the first
event, no separate sequence-per-run table): every run already has exactly one
natural lock key, its own `run_id`, and Postgres advisory locks are built for
precisely this "serialize by an application key, not a row" case (ADR-019 —
Postgres is the only infrastructure dependency).

This module is deliberately narrow: DB-005 adds the real repository layer.
Until then, this is the one function anything appending a trace event must
go through, so the allocation mechanism has exactly one implementation.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.persistence.models import TraceEvent, TraceEventKind, TraceEventSeverity

__all__ = ["append_trace_event"]


def append_trace_event(
    session: Session,
    *,
    run_id: uuid.UUID,
    kind: TraceEventKind,
    severity: TraceEventSeverity = TraceEventSeverity.INFO,
    node: str | None = None,
    tool: str | None = None,
    step_id: str | None = None,
    attempt: int | None = None,
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    status: str | None = None,
    duration_ms: int | None = None,
    retry_count: int | None = None,
    error: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> TraceEvent:
    """Allocate the next `seq` for `run_id` and insert the event, atomically.

    Must run inside a transaction that the caller commits promptly — the
    advisory lock this takes is held until that commit (or rollback), and
    holding it open serializes every other appender for the same run.
    """
    lock_key = sa.func.hashtextextended(sa.cast(str(run_id), sa.Text), 0)
    session.execute(sa.select(sa.func.pg_advisory_xact_lock(lock_key)))
    next_seq = session.execute(
        sa.select(sa.func.coalesce(sa.func.max(TraceEvent.seq), 0) + 1).where(
            TraceEvent.run_id == run_id
        )
    ).scalar_one()

    event = TraceEvent(
        run_id=run_id,
        seq=next_seq,
        kind=kind,
        severity=severity,
        node=node,
        tool=tool,
        step_id=step_id,
        attempt=attempt,
        input=input,
        output=output,
        status=status,
        duration_ms=duration_ms,
        retry_count=retry_count,
        error=error,
        payload=payload if payload is not None else {},
    )
    session.add(event)
    session.flush()
    return event
