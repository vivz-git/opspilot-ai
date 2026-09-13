"""Mock CRM persistence models (§12.9, DB-004).

Simulated system-of-record models bound to the `mock_crm` schema:
- `companies`: static firmographic fixtures
- `leads`: prospect records referencing companies
- `customers`: system-of-record customer accounts with concurrency `version`
- `outreach_drafts`: stored drafts linked to leads
- `email_outbox`: outbound message records with unique `idempotency_key`

Kept cleanly separated from control-plane persistence (`opspilot` schema) per
ADR-011 and §12.1.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.base import MOCK_CRM_SCHEMA, Base
from app.tools.schemas import CustomerStatus, LeadStatus

__all__ = [
    "Company",
    "Customer",
    "CustomerStatus",
    "EmailOutbox",
    "EmailOutboxStatus",
    "Lead",
    "LeadStatus",
    "OutreachDraft",
    "OutreachDraftStatus",
    "reset_mock_crm",
]


class OutreachDraftStatus(StrEnum):
    """§12.9 — lifecycle of an outreach draft."""

    SAVED = "saved"
    SENT = "sent"
    ARCHIVED = "archived"


class EmailOutboxStatus(StrEnum):
    """§12.9 — delivery status in the simulated outbox."""

    SENT = "sent"
    FAILED = "failed"


def _enum_values(enum_cls: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_cls]


def _enum_column(enum_cls: type[StrEnum], name: str) -> sa.Enum:
    """A `CHECK`-constrained text column for an enum-typed field (§12.10)."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=_enum_values,
    )


def _timestamptz() -> sa.TIMESTAMP:
    return sa.TIMESTAMP(timezone=True)


class Company(Base):
    """§12.9 — `mock_crm.companies`: static firmographic fixture."""

    __tablename__ = "companies"
    __table_args__ = (
        Index("ix_companies_industry", "industry"),
        Index("ix_companies_name", "name"),
        {"schema": MOCK_CRM_SCHEMA},
    )

    company_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    domain: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    industry: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    employee_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    revenue_band: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    hq_location: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    funding_stage: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    tech_stack: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    signals: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
    )

    leads: Mapped[list[Lead]] = relationship("Lead", back_populates="company")


class Lead(Base):
    """§12.9 — `mock_crm.leads`: prospect records."""

    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_company_id", "company_id"),
        Index("ix_leads_email", "email"),
        Index("ix_leads_status", "status"),
        {"schema": MOCK_CRM_SCHEMA},
    )

    lead_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    company_id: Mapped[str] = mapped_column(
        sa.Text, ForeignKey(f"{MOCK_CRM_SCHEMA}.companies.company_id"), nullable=False
    )
    full_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    email: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[LeadStatus] = mapped_column(
        _enum_column(LeadStatus, "lead_status"),
        nullable=False,
        server_default=sa.text("'new'"),
    )
    source: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    owner: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    timezone: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")
    )
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    last_contacted_at: Mapped[datetime | None] = mapped_column(_timestamptz(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
    )

    company: Mapped[Company] = relationship("Company", back_populates="leads")
    drafts: Mapped[list[OutreachDraft]] = relationship("OutreachDraft", back_populates="lead")


class Customer(Base):
    """§12.9 — `mock_crm.customers`: system of record customer accounts.

    The `version` column is the optimistic-concurrency token that `update_customer`
    requires (§8.5, §12.9).
    """

    __tablename__ = "customers"
    __table_args__ = (
        Index("ix_customers_email", "email"),
        Index("ix_customers_status", "status"),
        {"schema": MOCK_CRM_SCHEMA},
    )

    customer_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    account_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    primary_contact: Mapped[str] = mapped_column(sa.Text, nullable=False)
    email: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    phone: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    status: Mapped[CustomerStatus] = mapped_column(
        _enum_column(CustomerStatus, "customer_status"),
        nullable=False,
        server_default=sa.text("'prospect'"),
    )
    plan: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    mrr: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 2), nullable=True)
    owner: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
    )


class OutreachDraft(Base):
    """§12.9 — `mock_crm.outreach_drafts`: stored drafts linked to leads."""

    __tablename__ = "outreach_drafts"
    __table_args__ = (
        Index("ix_outreach_drafts_lead_id", "lead_id"),
        Index("ix_outreach_drafts_status", "status"),
        {"schema": MOCK_CRM_SCHEMA},
    )

    draft_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    lead_id: Mapped[str] = mapped_column(
        sa.Text, ForeignKey(f"{MOCK_CRM_SCHEMA}.leads.lead_id"), nullable=False
    )
    channel: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("'email'"))
    subject: Mapped[str] = mapped_column(sa.Text, nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[OutreachDraftStatus] = mapped_column(
        _enum_column(OutreachDraftStatus, "outreach_draft_status"),
        nullable=False,
        server_default=sa.text("'saved'"),
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
    )

    lead: Mapped[Lead] = relationship("Lead", back_populates="drafts")
    outbox_entries: Mapped[list[EmailOutbox]] = relationship("EmailOutbox", back_populates="draft")


class EmailOutbox(Base):
    """§12.9 — `mock_crm.email_outbox`: outbound message records.

    `idempotency_key UNIQUE` is enforced at the database level to ensure a retried
    send cannot physically produce a second outbox record.
    `run_id` and `approval_id` make every simulated send traceable to the
    human authorization that granted it (§12.9, §15.6).
    """

    __tablename__ = "email_outbox"
    __table_args__ = (
        Index("ix_email_outbox_draft_id", "draft_id"),
        Index("ix_email_outbox_status", "status"),
        Index("ix_email_outbox_run_id", "run_id"),
        Index("ix_email_outbox_approval_id", "approval_id"),
        {"schema": MOCK_CRM_SCHEMA},
    )

    outbox_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    message_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    draft_id: Mapped[str] = mapped_column(
        sa.Text, ForeignKey(f"{MOCK_CRM_SCHEMA}.outreach_drafts.draft_id"), nullable=False
    )
    to_email: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject: Mapped[str] = mapped_column(sa.Text, nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[EmailOutboxStatus] = mapped_column(
        _enum_column(EmailOutboxStatus, "email_outbox_status"),
        nullable=False,
        server_default=sa.text("'sent'"),
    )
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("'mock'"))
    idempotency_key: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    run_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    approval_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=sa.func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(_timestamptz(), nullable=True)

    draft: Mapped[OutreachDraft] = relationship("OutreachDraft", back_populates="outbox_entries")


async def reset_mock_crm(session: AsyncSession) -> None:
    """Clear all mock_crm tables for deterministic reseeding."""
    await session.execute(
        sa.text(
            f"TRUNCATE {MOCK_CRM_SCHEMA}.email_outbox, "
            f"{MOCK_CRM_SCHEMA}.outreach_drafts, "
            f"{MOCK_CRM_SCHEMA}.leads, "
            f"{MOCK_CRM_SCHEMA}.customers, "
            f"{MOCK_CRM_SCHEMA}.companies "
            f"CASCADE"
        )
    )
