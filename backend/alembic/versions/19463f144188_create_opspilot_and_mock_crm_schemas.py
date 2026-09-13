"""create opspilot and mock_crm schemas

Revision ID: 19463f144188
Revises:
Create Date: 2026-09-13 11:53:20.328149

`opspilot` is the control plane, `mock_crm` the simulated system of record
(§12.1, ADR-011). `langgraph` is not created here — it is the LangGraph
Postgres saver's own schema, never touched by our migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "19463f144188"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS opspilot")
    op.execute("CREATE SCHEMA IF NOT EXISTS mock_crm")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS mock_crm CASCADE")
    op.execute("DROP SCHEMA IF EXISTS opspilot CASCADE")
