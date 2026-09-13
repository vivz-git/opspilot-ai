"""add mock_crm persistence tables

Revision ID: e35beebb06d0
Revises: 62fe5fff7640
Create Date: 2026-09-13 14:58:08.067505

Matches §12.9 exactly (DB-004). Creates the simulated system-of-record tables
in the `mock_crm` schema: `companies`, `leads`, `customers` (with concurrency
`version`), `outreach_drafts` and `email_outbox` (with `idempotency_key UNIQUE`).

Self-contained: enum values are spelled out as literals rather than imported
from `app.persistence.mock_crm`, so migration behaviour never drifts if that
module changes later.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e35beebb06d0"
down_revision: str | None = "62fe5fff7640"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

SCHEMA = "mock_crm"


def upgrade() -> None:
    # 1. companies
    op.create_table(
        "companies",
        sa.Column("company_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("industry", sa.Text(), nullable=True),
        sa.Column("employee_count", sa.Integer(), nullable=True),
        sa.Column("revenue_band", sa.Text(), nullable=True),
        sa.Column("hq_location", sa.Text(), nullable=True),
        sa.Column("funding_stage", sa.Text(), nullable=True),
        sa.Column(
            "tech_stack",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "signals",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("domain", name="uq_companies_domain"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_companies_industry",
        "companies",
        ["industry"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_companies_name",
        "companies",
        ["name"],
        schema=SCHEMA,
    )

    # 2. leads
    op.create_table(
        "leads",
        sa.Column("lead_id", sa.Text(), primary_key=True),
        sa.Column(
            "company_id",
            sa.Text(),
            sa.ForeignKey(
                f"{SCHEMA}.companies.company_id",
                name="fk_leads_company_id_companies",
            ),
            nullable=False,
        ),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "new",
                "working",
                "qualified",
                "disqualified",
                name="lead_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default=sa.text("'new'"),
        ),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("last_contacted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_leads_company_id", "leads", ["company_id"], schema=SCHEMA)
    op.create_index("ix_leads_email", "leads", ["email"], schema=SCHEMA)
    op.create_index("ix_leads_status", "leads", ["status"], schema=SCHEMA)

    # 3. customers
    op.create_table(
        "customers",
        sa.Column("customer_id", sa.Text(), primary_key=True),
        sa.Column("account_name", sa.Text(), nullable=False),
        sa.Column("primary_contact", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "prospect",
                "active",
                "churned",
                name="customer_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default=sa.text("'prospect'"),
        ),
        sa.Column("plan", sa.Text(), nullable=True),
        sa.Column("mrr", sa.Numeric(12, 2), nullable=True),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("email", name="uq_customers_email"),
        schema=SCHEMA,
    )
    op.create_index("ix_customers_email", "customers", ["email"], schema=SCHEMA)
    op.create_index("ix_customers_status", "customers", ["status"], schema=SCHEMA)

    # 4. outreach_drafts
    op.create_table(
        "outreach_drafts",
        sa.Column("draft_id", sa.Text(), primary_key=True),
        sa.Column(
            "lead_id",
            sa.Text(),
            sa.ForeignKey(
                f"{SCHEMA}.leads.lead_id",
                name="fk_outreach_drafts_lead_id_leads",
            ),
            nullable=False,
        ),
        sa.Column("channel", sa.Text(), nullable=False, server_default=sa.text("'email'")),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "saved",
                "sent",
                "archived",
                name="outreach_draft_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default=sa.text("'saved'"),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_outreach_drafts_lead_id",
        "outreach_drafts",
        ["lead_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_outreach_drafts_status",
        "outreach_drafts",
        ["status"],
        schema=SCHEMA,
    )

    # 5. email_outbox
    op.create_table(
        "email_outbox",
        sa.Column("outbox_id", sa.Text(), primary_key=True),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column(
            "draft_id",
            sa.Text(),
            sa.ForeignKey(
                f"{SCHEMA}.outreach_drafts.draft_id",
                name="fk_email_outbox_draft_id_outreach_drafts",
            ),
            nullable=False,
        ),
        sa.Column("to_email", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "sent",
                "failed",
                name="email_outbox_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default=sa.text("'sent'"),
        ),
        sa.Column("provider", sa.Text(), nullable=False, server_default=sa.text("'mock'")),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("approval_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_email_outbox_idempotency_key"),
        sa.UniqueConstraint("message_id", name="uq_email_outbox_message_id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_email_outbox_draft_id",
        "email_outbox",
        ["draft_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_email_outbox_status",
        "email_outbox",
        ["status"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_email_outbox_run_id",
        "email_outbox",
        ["run_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_email_outbox_approval_id",
        "email_outbox",
        ["approval_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("email_outbox", schema=SCHEMA)
    op.drop_table("outreach_drafts", schema=SCHEMA)
    op.drop_table("customers", schema=SCHEMA)
    op.drop_table("leads", schema=SCHEMA)
    op.drop_table("companies", schema=SCHEMA)
