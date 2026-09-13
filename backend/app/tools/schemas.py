"""Typed input/output contracts for all nine business tools (§8.4).

Every model is `extra="forbid"`: an unexpected field from a planner or an
adapter is an error, never a silent pass-through. These models are the
normative IO contract — the registry references them, the API publishes their
JSON Schema at `GET /tools`, and the dashboard renders them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.security import ApprovalToken


class Strict(BaseModel):
    """Base for every tool payload: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Shared domain types
# ---------------------------------------------------------------------------
class LeadStatus(StrEnum):
    NEW = "new"
    WORKING = "working"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"


class ScoreBand(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class CustomerStatus(StrEnum):
    PROSPECT = "prospect"
    ACTIVE = "active"
    CHURNED = "churned"


class LeadSummary(Strict):
    lead_id: str
    full_name: str
    title: str | None = None
    email: EmailStr
    company_id: str
    company_name: str
    status: LeadStatus
    source: str | None = None
    created_at: datetime


class LeadDetail(LeadSummary):
    phone: str | None = None
    timezone: str | None = None
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    last_contacted_at: datetime | None = None
    notes: str | None = None


class Signal(Strict):
    kind: str
    summary: str
    observed_at: datetime | None = None


class CompanyProfile(Strict):
    """Enrichment output. `summary`, `recent_signals` and `sources` are
    third-party text and are the system's prompt-injection surface: the
    contract marks this tool `untrusted_output=True` (§16.3)."""

    company_id: str
    name: str
    domain: str
    industry: str | None = None
    employee_count: int | None = Field(default=None, ge=0)
    revenue_band: str | None = None
    hq_location: str | None = None
    funding_stage: str | None = None
    tech_stack: list[str] = Field(default_factory=list)
    recent_signals: list[Signal] = Field(default_factory=list)
    summary: str
    sources: list[str] = Field(default_factory=list)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    retrieved_at: datetime


class ScoreFactor(Strict):
    name: str
    weight: float
    value: float
    contribution: float


class ScoringWeights(Strict):
    company_fit: float = 0.4
    engagement: float = 0.3
    signal_strength: float = 0.2
    data_quality: float = 0.1


# ---------------------------------------------------------------------------
# search_leads
# ---------------------------------------------------------------------------
class SearchLeadsInput(Strict):
    industry: str | None = None
    location: str | None = None
    min_employees: int | None = Field(default=None, ge=0)
    max_employees: int | None = Field(default=None, ge=0)
    status: LeadStatus | None = None
    query: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=10, ge=1, le=50)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check(self) -> SearchLeadsInput:
        if (
            self.min_employees is not None
            and self.max_employees is not None
            and self.max_employees < self.min_employees
        ):
            raise ValueError("max_employees must be >= min_employees")
        filters = (
            self.industry,
            self.location,
            self.status,
            self.query,
            self.min_employees,
            self.max_employees,
        )
        if not any(v is not None for v in filters):
            # A cost bound: the planner may not request the whole table.
            raise ValueError("at least one filter or query is required")
        return self


class SearchLeadsOutput(Strict):
    leads: list[LeadSummary]
    total_matched: int = Field(ge=0)
    truncated: bool = False


# ---------------------------------------------------------------------------
# get_lead
# ---------------------------------------------------------------------------
class GetLeadInput(Strict):
    lead_id: str = Field(min_length=1)


class GetLeadOutput(Strict):
    lead: LeadDetail


# ---------------------------------------------------------------------------
# research_company
# ---------------------------------------------------------------------------
class ResearchDepth(StrEnum):
    BASIC = "basic"
    STANDARD = "standard"


class ResearchCompanyInput(Strict):
    company_id: str | None = None
    domain: str | None = None
    depth: ResearchDepth = ResearchDepth.STANDARD

    @model_validator(mode="after")
    def _exactly_one(self) -> ResearchCompanyInput:
        if (self.company_id is None) == (self.domain is None):
            raise ValueError("exactly one of company_id or domain is required")
        return self


class ResearchCompanyOutput(Strict):
    profile: CompanyProfile


# ---------------------------------------------------------------------------
# score_lead
# ---------------------------------------------------------------------------
class ScoreLeadInput(Strict):
    lead_id: str
    company: CompanyProfile
    weights: ScoringWeights | None = None


class ScoreLeadOutput(Strict):
    lead_id: str
    score: int = Field(ge=0, le=100)
    band: ScoreBand
    factors: list[ScoreFactor]
    rationale: str
    model_version: str


# ---------------------------------------------------------------------------
# draft_outreach
# ---------------------------------------------------------------------------
class Tone(StrEnum):
    DIRECT = "direct"
    WARM = "warm"
    FORMAL = "formal"


class DraftOutreachInput(Strict):
    lead_id: str
    company: CompanyProfile
    score: ScoreLeadOutput | None = None
    channel: str = Field(default="email", pattern="^email$")
    tone: Tone = Tone.DIRECT
    max_words: int = Field(default=180, ge=40, le=400)


class DraftOutreachOutput(Strict):
    lead_id: str
    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1)
    word_count: int = Field(ge=1)
    personalization_notes: list[str] = Field(default_factory=list)
    content_hash: str
    model_version: str
    generated_at: datetime


# ---------------------------------------------------------------------------
# save_draft
# ---------------------------------------------------------------------------
class SaveDraftInput(Strict):
    lead_id: str
    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1)
    channel: str = Field(default="email", pattern="^email$")
    content_hash: str = Field(min_length=16)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SaveDraftOutput(Strict):
    draft_id: str
    version: int = Field(ge=1)
    status: str = Field(pattern="^saved$")
    content_hash: str
    saved_at: datetime


# ---------------------------------------------------------------------------
# send_email_mock  — APPROVAL REQUIRED. Never sends real mail (§19.2).
# ---------------------------------------------------------------------------
class SendEmailMockInput(Strict):
    """Takes a `draft_id`, never raw subject/body, so the content that is sent
    is provably the content that was saved, verified and approved (§8.4)."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    draft_id: str
    to_email: EmailStr
    idempotency_key: str = Field(min_length=8)
    approval_token: ApprovalToken


class SendEmailMockOutput(Strict):
    message_id: str
    outbox_id: str
    status: str = Field(pattern="^sent$")
    provider: str = Field(pattern="^mock$")
    to_email: EmailStr
    draft_id: str
    sent_at: datetime


# ---------------------------------------------------------------------------
# get_customer
# ---------------------------------------------------------------------------
class Customer(Strict):
    customer_id: str
    account_name: str
    primary_contact: str
    email: EmailStr
    phone: str | None = None
    status: CustomerStatus
    plan: str | None = None
    mrr: float | None = Field(default=None, ge=0)
    owner: str | None = None
    version: int = Field(ge=1)
    updated_at: datetime


class GetCustomerInput(Strict):
    customer_id: str | None = None
    email: EmailStr | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> GetCustomerInput:
        if (self.customer_id is None) == (self.email is None):
            raise ValueError("exactly one of customer_id or email is required")
        return self


class GetCustomerOutput(Strict):
    customer: Customer


# ---------------------------------------------------------------------------
# update_customer  — APPROVAL REQUIRED.
# ---------------------------------------------------------------------------
class CustomerPatch(Strict):
    """The write allowlist. `id`, `email`, `created_at` and `mrr` are absent by
    design: identity and billing fields are unwritable by the agent whatever
    the plan says (§16.3)."""

    status: CustomerStatus | None = None
    plan: str | None = None
    owner: str | None = None
    phone: str | None = None
    primary_contact: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _non_empty(self) -> CustomerPatch:
        if not self.model_dump(exclude_none=True):
            raise ValueError("patch must change at least one field")
        return self


class UpdateCustomerInput(Strict):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    customer_id: str
    expected_version: int = Field(ge=1)
    patch: CustomerPatch
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8)
    approval_token: ApprovalToken


class UpdateCustomerOutput(Strict):
    customer_id: str
    version: int = Field(ge=2)
    updated_fields: list[str] = Field(min_length=1)
    previous: dict[str, Any]
    updated_at: datetime
