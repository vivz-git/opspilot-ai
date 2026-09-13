"""add trace_events with per-run monotonic seq

Revision ID: 21765d8fa136
Revises: c6d1db7aa718
Create Date: 2026-09-13 12:56:25.756778

Matches §12.7 exactly (DB-002). Written by hand and self-contained, like
c6d1db7aa718: enum values are spelled out as literals rather than imported
from `app.persistence.models`, so this revision's behaviour never drifts if
that module changes later.

`id` is a `bigserial`, not a uuid like every other control-plane table
(§12.7 is explicit about this) — `trace_events` is the fastest-growing table
by an order of magnitude, and a purely sequential key avoids the write
amplification a random uuid primary key would cause here.

`seq` is the per-run monotonic ordering key (not `ts` — two events in the
same millisecond still need a total order). `UNIQUE(run_id, seq)` is the
database-level backstop against a duplicate or a gap; the mechanism that
makes concurrent appends for the same run actually allocate a gapless `seq`
in the first place is `app.persistence.trace_events.append_trace_event`'s
`pg_advisory_xact_lock(run_id)`, not this constraint alone — see that
module's docstring for the full concurrency reasoning.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "21765d8fa136"
down_revision: str | None = "c6d1db7aa718"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

SCHEMA = "opspilot"


def upgrade() -> None:
    op.create_table(
        "trace_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.agent_runs.id",
                ondelete="CASCADE",
                name="fk_trace_events_run_id_agent_runs",
            ),
            nullable=False,
        ),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "kind",
            sa.Enum(
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
                name="trace_event_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "severity",
            sa.Enum(
                "debug",
                "info",
                "warning",
                "error",
                name="trace_event_severity",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default=sa.text("'info'"),
        ),
        sa.Column("node", sa.Text(), nullable=True),
        sa.Column("tool", sa.Text(), nullable=True),
        sa.Column("step_id", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("input", postgresql.JSONB(), nullable=True),
        sa.Column("output", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.UniqueConstraint("run_id", "seq", name="uq_trace_events_run_id_seq"),
        schema=SCHEMA,
    )
    op.create_index("ix_trace_events_run_id_id", "trace_events", ["run_id", "id"], schema=SCHEMA)
    op.create_index(
        "ix_trace_events_kind_ts",
        "trace_events",
        ["kind", sa.text("ts DESC")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_trace_events_ts_brin",
        "trace_events",
        ["ts"],
        schema=SCHEMA,
        postgresql_using="brin",
    )


def downgrade() -> None:
    op.drop_table("trace_events", schema=SCHEMA)
