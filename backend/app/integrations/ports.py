"""Integration port protocols and carrier types (§19.1, TOOL-001).

Ports define WHAT capabilities external or simulated systems provide to the
OpsPilot agent and verifiers, decoupled from infrastructure or persistence details.

Mutating methods require an `ApprovalToken` in their signature to guarantee
safety across all future adapters. Independent read paths (`DraftPort.get`,
`MailPort.get_outbox`) support contract-driven readback verification (§11).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.security import ApprovalToken
from app.tools.schemas import (
    CompanyProfile,
    Customer,
    CustomerPatch,
    LeadDetail,
    LeadStatus,
    LeadSummary,
    ResearchDepth,
)

__all__ = [
    "Adapters",
    "CompanyPort",
    "ContentPort",
    "CustomerPort",
    "DraftContent",
    "DraftInput",
    "DraftPort",
    "DraftRecord",
    "LeadFilter",
    "LeadPage",
    "LeadPort",
    "MailPort",
    "OutboundMessage",
    "OutboxRecord",
    "OutreachBrief",
    "SendReceipt",
]


class StrictModel(BaseModel):
    """Base carrier model with extra='forbid'."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# LeadPort types & Protocol
# ---------------------------------------------------------------------------
class LeadFilter(StrictModel):
    """Filters used to query candidate leads."""

    industry: str | None = None
    location: str | None = None
    min_employees: int | None = Field(default=None, ge=0)
    max_employees: int | None = Field(default=None, ge=0)
    status: LeadStatus | None = None
    query: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=10, ge=1, le=50)
    offset: int = Field(default=0, ge=0)


class LeadPage(StrictModel):
    """Paginated result of a lead search."""

    leads: list[LeadSummary]
    total_matched: int = Field(ge=0)
    truncated: bool = False


@runtime_checkable
class LeadPort(Protocol):
    """Port for searching and fetching prospect leads."""

    async def search(self, f: LeadFilter) -> LeadPage:
        """Find candidate leads matching criteria."""
        ...

    async def get(self, lead_id: str) -> LeadDetail:
        """Fetch full details for one lead."""
        ...


# ---------------------------------------------------------------------------
# CompanyPort Protocol
# ---------------------------------------------------------------------------
@runtime_checkable
class CompanyPort(Protocol):
    """Port for firmographic research and company enrichment."""

    async def profile(
        self,
        *,
        company_id: str | None = None,
        domain: str | None = None,
        depth: ResearchDepth = ResearchDepth.STANDARD,
    ) -> CompanyProfile:
        """Enrich and retrieve a company profile."""
        ...


# ---------------------------------------------------------------------------
# CustomerPort Protocol
# ---------------------------------------------------------------------------
@runtime_checkable
class CustomerPort(Protocol):
    """Port for system-of-record customer management."""

    async def get(
        self,
        *,
        customer_id: str | None = None,
        email: str | None = None,
    ) -> Customer:
        """Fetch a customer record by ID or email."""
        ...

    async def update(
        self,
        customer_id: str,
        patch: CustomerPatch,
        *,
        expected_version: int,
        token: ApprovalToken,
        idempotency_key: str,
    ) -> Customer:
        """Apply an allowlisted patch with optimistic concurrency and approval token."""
        ...


# ---------------------------------------------------------------------------
# DraftPort types & Protocol
# ---------------------------------------------------------------------------
class DraftInput(StrictModel):
    """Input payload to persist a generated outreach draft."""

    lead_id: str
    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1)
    channel: str = "email"
    content_hash: str = Field(min_length=16)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DraftRecord(StrictModel):
    """Durable outreach draft record for approval and readback verification."""

    draft_id: str
    lead_id: str
    subject: str
    body: str
    channel: str = "email"
    status: str = "saved"
    version: int = 1
    content_hash: str
    saved_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class DraftPort(Protocol):
    """Port for persisting and retrieving outreach drafts."""

    async def save(self, draft: DraftInput) -> DraftRecord:
        """Persist generated copy as a durable draft."""
        ...

    async def get(self, draft_id: str) -> DraftRecord:
        """Readback verification path for saved drafts."""
        ...


# ---------------------------------------------------------------------------
# MailPort types & Protocol
# ---------------------------------------------------------------------------
class OutboundMessage(StrictModel):
    """Carrier for outbound message execution."""

    draft_id: str
    to_email: EmailStr


class SendReceipt(StrictModel):
    """Receipt returned upon dispatching an outbound email."""

    message_id: str
    outbox_id: str
    status: str = "sent"
    provider: str = "mock"
    to_email: EmailStr
    draft_id: str
    sent_at: datetime


class OutboxRecord(StrictModel):
    """Outbox record read back to verify email dispatch."""

    outbox_id: str
    message_id: str
    draft_id: str
    to_email: EmailStr
    subject: str
    body: str
    status: str
    provider: str
    idempotency_key: str
    sent_at: datetime | None = None


@runtime_checkable
class MailPort(Protocol):
    """Port for outbound email dispatch and verification."""

    async def send(
        self,
        msg: OutboundMessage,
        *,
        token: ApprovalToken,
        idempotency_key: str,
    ) -> SendReceipt:
        """Send an outbound email authorized by an approval token."""
        ...

    async def get_outbox(self, message_id: str) -> OutboxRecord:
        """Readback verification path for sent outbox records."""
        ...


# ---------------------------------------------------------------------------
# ContentPort types & Protocol
# ---------------------------------------------------------------------------
class OutreachBrief(StrictModel):
    """Parameters for copy generation."""

    lead_id: str
    lead_name: str
    company_name: str
    title: str | None = None
    tone: str = "direct"
    max_words: int = Field(default=180, ge=40, le=400)
    company_summary: str | None = None
    recent_signals: list[str] = Field(default_factory=list)


class DraftContent(StrictModel):
    """Generated copy content."""

    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1)
    word_count: int = Field(ge=1)
    personalization_notes: list[str] = Field(default_factory=list)
    content_hash: str = Field(min_length=16)
    model_version: str = "mock-v1"


@runtime_checkable
class ContentPort(Protocol):
    """Port for generating personalized outreach copy."""

    async def draft(self, brief: OutreachBrief) -> DraftContent:
        """Generate personalized outreach copy from a brief."""
        ...


# ---------------------------------------------------------------------------
# Adapters Container
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Adapters:
    """Bundle of all integration ports required by the agent runtime."""

    leads: LeadPort
    companies: CompanyPort
    customers: CustomerPort
    drafts: DraftPort
    mail: MailPort
    content: ContentPort
