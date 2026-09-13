"""add agent_runs lease_owner and run_recovered trace kind

Revision ID: c88ad060adfa
Revises: e35beebb06d0
Create Date: 2026-09-13 18:41:07.000000

DB-007 — run ownership and crash recovery (§2.4, §12.3, ADR-004, ADR-012,
ADR-023).

Two changes, both to `opspilot` tables we own. The LangGraph checkpoint
tables live in the `langgraph` schema and are created by the saver's own
`setup()` (`app.persistence.checkpointing`), never here (ADR-011).

1. `agent_runs.lease_owner text null` — the identity of the worker that
   currently holds the run's execution lease. §12.3 specified only
   `lease_expires_at`; an expiry alone can tell you a lease is *stale* but
   not *whose* it is, and without an owner a heartbeat cannot be fenced —
   a worker that lost its lease could silently extend a lease that now
   belongs to someone else. The `CHECK` keeps the pair consistent: a lease
   is either fully present (owner + expiry) or fully absent, so no state
   can be "leased by nobody until t" or "leased by X indefinitely".

2. `trace_events.kind` gains `run_recovered` — the one product-visible
   recovery event: the reconciler took over a run whose worker died and
   either resumed it from its checkpoint, repaired it to
   `awaiting_approval`, or finalised it from a finished checkpoint. The
   timeline would otherwise show an unexplained gap. Lease acquisition and
   heartbeats are infrastructure telemetry and go to structured logs, not
   the product trace (§14.1, §14.6). A run that had nothing to resume uses
   the existing `run_failed` kind with `status_reason=orphaned`.

Downgrade removes the column and the constraint, and restores the previous
`kind` vocabulary. Rows with `kind='run_recovered'` cannot satisfy the
restored constraint and are deleted first — a downgrade that drops the
lease column is already lossy, and this keeps it reversible rather than
failing halfway.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c88ad060adfa"
down_revision: str | None = "e35beebb06d0"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

SCHEMA = "opspilot"

#: Full constraint names, wrapped in `op.f()` below so Alembic does not apply
#: `Base.metadata`'s naming convention a second time (it would otherwise look
#: for `ck_trace_events_ck_trace_events_trace_event_kind`).
TRACE_KIND_CONSTRAINT = "ck_trace_events_trace_event_kind"
LEASE_CONSTRAINT = "ck_agent_runs_lease_owner_and_expiry_together"

#: The §14.2 vocabulary as DB-002 created it (spelled out, not imported —
#: this revision's behaviour must not drift if the model module changes).
_PREVIOUS_TRACE_KINDS = (
    "run_created",
    "run_started",
    "run_completed",
    "run_failed",
    "run_rejected",
    "run_expired",
    "run_cancelled",
    "node_entered",
    "node_exited",
    "plan_created",
    "plan_revised",
    "fanout_expanded",
    "tool_started",
    "tool_succeeded",
    "tool_failed",
    "tool_timeout",
    "tool_duplicate_suppressed",
    "retry_scheduled",
    "step_skipped",
    "budget_exhausted",
    "approval_requested",
    "approval_granted",
    "approval_rejected",
    "approval_expired",
    "approval_superseded",
    "verification_passed",
    "verification_failed",
    "verification_skipped",
    "policy_violation",
)
_NEW_TRACE_KINDS = (*_PREVIOUS_TRACE_KINDS, "run_recovered")


def _kind_check(values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"kind IN ({quoted})"


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("lease_owner", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        op.f(LEASE_CONSTRAINT),
        "agent_runs",
        "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
        schema=SCHEMA,
    )

    op.drop_constraint(op.f(TRACE_KIND_CONSTRAINT), "trace_events", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        op.f(TRACE_KIND_CONSTRAINT), "trace_events", _kind_check(_NEW_TRACE_KINDS), schema=SCHEMA
    )


def downgrade() -> None:
    trace_events = sa.table("trace_events", sa.column("kind"), schema=SCHEMA)
    op.execute(sa.delete(trace_events).where(trace_events.c.kind == "run_recovered"))
    op.drop_constraint(op.f(TRACE_KIND_CONSTRAINT), "trace_events", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        op.f(TRACE_KIND_CONSTRAINT),
        "trace_events",
        _kind_check(_PREVIOUS_TRACE_KINDS),
        schema=SCHEMA,
    )

    op.drop_constraint(op.f(LEASE_CONSTRAINT), "agent_runs", schema=SCHEMA, type_="check")
    op.drop_column("agent_runs", "lease_owner", schema=SCHEMA)
