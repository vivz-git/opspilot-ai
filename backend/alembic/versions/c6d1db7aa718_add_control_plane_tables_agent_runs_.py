"""add control-plane tables: agent_runs, execution_steps, tool_calls, approvals

Revision ID: c6d1db7aa718
Revises: 19463f144188
Create Date: 2026-09-13 12:13:48.291028

Matches §12.3-12.6 exactly (DB-001). Written by hand, self-contained: enum
values are spelled out as literals rather than imported from `app.persistence
.models` so this revision's behaviour never drifts if that module changes
later — a migration is a historical record, not a live view of the code.

Enum-typed columns (`agent_runs.status`, `execution_steps.status`,
`execution_steps.verification_status`, `tool_calls.status`,
`approvals.status`, `approvals.risk`) use `native_enum=False` — a `CHECK`
constraint on a text column rather than a Postgres `CREATE TYPE`, so adding a
value later is a plain constraint migration instead of an `ALTER TYPE`
lifecycle. `evaluation_run_id` on `agent_runs` has no foreign key yet: DB-003
creates `evaluation_runs` and adds the constraint then (DB-003 depends on
DB-001, not the reverse).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c6d1db7aa718"
down_revision: str | None = "19463f144188"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

SCHEMA = "opspilot"


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "parent_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.agent_runs.id", name="fk_agent_runs_parent_run_id_agent_runs"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "created",
                "queued",
                "running",
                "awaiting_approval",
                "completed",
                "failed",
                "rejected",
                "cancelled",
                "expired",
                name="agent_run_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default=sa.text("'created'"),
        ),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("user_request", sa.Text(), nullable=False),
        sa.Column("normalized_task", postgresql.JSONB(), nullable=True),
        sa.Column("plan", postgresql.JSONB(), nullable=True),
        sa.Column(
            "plan_history",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("plan_revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("final_response", postgresql.JSONB(), nullable=True),
        sa.Column("planner_kind", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("seed", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("step_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("retry_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("replan_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("deadline_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("evaluation_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("eval_case_id", sa.Text(), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_runs_idempotency_key"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_agent_runs_status_created_at",
        "agent_runs",
        ["status", sa.text("created_at DESC")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_agent_runs_created_at", "agent_runs", [sa.text("created_at DESC")], schema=SCHEMA
    )
    op.create_index("ix_agent_runs_parent_run_id", "agent_runs", ["parent_run_id"], schema=SCHEMA)
    op.create_index(
        "ix_agent_runs_evaluation_run_id", "agent_runs", ["evaluation_run_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_agent_runs_lease_expires_at_active",
        "agent_runs",
        ["lease_expires_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("status IN ('running', 'queued')"),
    )

    op.create_table(
        "execution_steps",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.agent_runs.id",
                ondelete="CASCADE",
                name="fk_execution_steps_run_id_agent_runs",
            ),
            nullable=False,
        ),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("parent_step_id", sa.Text(), nullable=True),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("tool_version", sa.Text(), nullable=False, server_default=sa.text("'1.0.0'")),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "ready",
                "awaiting_approval",
                "running",
                "succeeded",
                "failed",
                "skipped",
                "rejected",
                name="execution_step_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "args", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("args_hash", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "verification_status",
            sa.Enum(
                "not_required",
                "passed",
                "failed",
                "unconfirmed",
                name="execution_step_verification_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default=sa.text("'not_required'"),
        ),
        sa.Column("verification", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column(
            "depends_on",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("optional", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.UniqueConstraint(
            "run_id", "step_id", "plan_revision", name="uq_execution_steps_run_step_revision"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_execution_steps_run_id_seq", "execution_steps", ["run_id", "seq"], schema=SCHEMA
    )
    op.create_index(
        "ix_execution_steps_run_id_status", "execution_steps", ["run_id", "status"], schema=SCHEMA
    )
    op.create_index(
        "ix_execution_steps_verification_failed",
        "execution_steps",
        ["verification_status"],
        schema=SCHEMA,
        postgresql_where=sa.text("verification_status = 'failed'"),
    )

    op.create_table(
        "tool_calls",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.agent_runs.id",
                ondelete="CASCADE",
                name="fk_tool_calls_run_id_agent_runs",
            ),
            nullable=False,
        ),
        sa.Column(
            "execution_step_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.execution_steps.id",
                ondelete="CASCADE",
                name="fk_tool_calls_execution_step_id_execution_steps",
            ),
            nullable=False,
        ),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("tool_version", sa.Text(), nullable=False, server_default=sa.text("'1.0.0'")),
        sa.Column(
            "input", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("output", postgresql.JSONB(), nullable=True),
        sa.Column("input_hash", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "succeeded",
                "failed",
                "timeout",
                "duplicate_suppressed",
                name="tool_call_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("port", sa.Text(), nullable=True),
        sa.Column("adapter", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "execution_step_id", "attempt", name="uq_tool_calls_execution_step_id_attempt"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_tool_calls_run_id_started_at", "tool_calls", ["run_id", "started_at"], schema=SCHEMA
    )
    op.create_index("ix_tool_calls_tool_status", "tool_calls", ["tool", "status"], schema=SCHEMA)
    op.create_index(
        "ix_tool_calls_idempotency_key", "tool_calls", ["idempotency_key"], schema=SCHEMA
    )
    op.create_index(
        "ix_tool_calls_error_class_failed",
        "tool_calls",
        ["error_class"],
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'failed'"),
    )

    op.create_table(
        "approvals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.agent_runs.id", ondelete="CASCADE", name="fk_approvals_run_id_agent_runs"
            ),
            nullable=False,
        ),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column(
            "risk",
            sa.Enum(
                "low",
                "medium",
                "high",
                name="approval_risk",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "payload_preview",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("args_hash", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "approved",
                "rejected",
                "expired",
                "superseded",
                "cancelled",
                name="approval_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "superseded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.approvals.id", name="fk_approvals_superseded_by_approvals"),
            nullable=True,
        ),
        sa.Column("requested_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_approvals_run_id_step_id_pending",
        "approvals",
        ["run_id", "step_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_approvals_status_expires_at", "approvals", ["status", "expires_at"], schema=SCHEMA
    )
    op.create_index(
        "ix_approvals_status_requested_at",
        "approvals",
        ["status", sa.text("requested_at DESC")],
        schema=SCHEMA,
    )
    op.create_index("ix_approvals_run_id", "approvals", ["run_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("approvals", schema=SCHEMA)
    op.drop_table("tool_calls", schema=SCHEMA)
    op.drop_table("execution_steps", schema=SCHEMA)
    op.drop_table("agent_runs", schema=SCHEMA)
