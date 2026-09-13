"""add evaluation_runs, evaluation_results and the deferred agent_runs fk

Revision ID: 62fe5fff7640
Revises: 21765d8fa136
Create Date: 2026-09-13 13:25:07.451399

Matches §12.8 exactly (DB-003). Written by hand and self-contained, like
c6d1db7aa718 and 21765d8fa136: enum values are spelled out as literals rather
than imported from `app.persistence.models`, so this revision's behaviour
never drifts if that module changes later.

`git_sha`, `prompt_version` and `seed` on `evaluation_runs` are what let a
metrics regression be attributed to a specific change (§12.8) — without them
the numbers in `metrics` have no cause.

This revision also adds the foreign key `c6d1db7aa718` deliberately left off
`agent_runs.evaluation_run_id`, since `evaluation_runs` did not exist yet at
that point (DB-003 depends on DB-001, not the reverse). Neither new FK
(`agent_runs.evaluation_run_id -> evaluation_runs.id`,
`evaluation_results.run_id -> agent_runs.id`) uses `ON DELETE CASCADE`: both
point *sideways* at a row that is not owned by the referencing table (an
`agent_runs` row is not part of the `evaluation_runs` aggregate, and an
`evaluation_results` row's `run_id` names a real, independently-owned run),
so the database blocks the delete instead of silently erasing execution or
evaluation history. `evaluation_results.evaluation_run_id -> evaluation_runs
.id` is the one true ownership relationship here and does cascade, per
§12.8's explicit "ON DELETE CASCADE".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "62fe5fff7640"
down_revision: str | None = "21765d8fa136"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

SCHEMA = "opspilot"


def upgrade() -> None:
    op.create_table(
        "evaluation_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("suite", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "completed",
                "failed",
                name="evaluation_run_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("git_sha", sa.Text(), nullable=True),
        sa.Column("planner_kind", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("seed", sa.BigInteger(), nullable=True),
        sa.Column("case_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("passed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "metrics", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_evaluation_runs_suite_started_at",
        "evaluation_runs",
        ["suite", sa.text("started_at DESC")],
        schema=SCHEMA,
    )

    op.create_table(
        "evaluation_results",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "evaluation_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.evaluation_runs.id",
                ondelete="CASCADE",
                name="fk_evaluation_results_evaluation_run_id_evaluation_runs",
            ),
            nullable=False,
        ),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{SCHEMA}.agent_runs.id",
                name="fk_evaluation_results_run_id_agent_runs",
            ),
            nullable=False,
        ),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column(
            "assertions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tool_calls_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("approval_outcome", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "evaluation_run_id", "case_id", name="uq_evaluation_results_evaluation_run_id_case_id"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_evaluation_results_case_id_passed",
        "evaluation_results",
        ["case_id", "passed"],
        schema=SCHEMA,
    )

    op.create_foreign_key(
        "fk_agent_runs_evaluation_run_id_evaluation_runs",
        "agent_runs",
        "evaluation_runs",
        ["evaluation_run_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_agent_runs_evaluation_run_id_evaluation_runs",
        "agent_runs",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_table("evaluation_results", schema=SCHEMA)
    op.drop_table("evaluation_runs", schema=SCHEMA)
