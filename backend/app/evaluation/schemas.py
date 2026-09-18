"""The typed evaluation-case contract (§15.3, EVAL-001).

A case is data: what the operator asked for, how the world is pinned
(fixtures, planner, seed, scripted approvals, injected failures) and what
must be true afterwards. Nothing in this module executes anything — the
runner (EVAL-002) reads these models and drives the *real* service path
(§15.5). Keeping the assertions declarative is what lets the same case be
rendered, diffed, reviewed and re-run without the case file knowing how the
runner works.

Every model is `extra="forbid"`: a misspelt key in a case file is a
validation error, not a silently ignored assertion. Wherever the runtime
already has a canonical vocabulary — `RunStatus`, `PlannerKind`, `ToolName`,
`StepStatus`, `VerificationStatus`, `Budgets` — the case reuses it rather
than restating it, so a case cannot name a tool, a status or a planner the
system does not have.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.state import (
    TERMINAL_RUN_STATUSES,
    Budgets,
    PlannerKind,
    RunStatus,
    StepStatus,
    VerificationStatus,
)
from app.tools.contracts import REGISTRY, ToolName, VerificationMode
from app.tools.schemas import CustomerStatus, LeadStatus

__all__ = [
    "ASSERTABLE_TABLES",
    "CASE_ID_PATTERN",
    "DEFAULT_FIXTURE_SET",
    "MAX_REQUEST_LENGTH",
    "MAX_TITLE_LENGTH",
    "REQUIRED_CASE_IDS",
    "REQUIRED_SUITES",
    "RFC2606_DOMAIN_SUFFIXES",
    "RUN_ID_PLACEHOLDER",
    "ApprovalExpectation",
    "ApprovalPolicyKind",
    "ApprovalsSpec",
    "BackoffExpectation",
    "CompanyFixture",
    "CompanySignalFixture",
    "CustomerFixture",
    "DbAssertion",
    "EvalCase",
    "EvalModel",
    "ExpectSpec",
    "FailureInjection",
    "FailureInjectionKind",
    "FixtureDataset",
    "GivenSpec",
    "LeadFixture",
    "PlanExpectation",
    "PlanStepExpectation",
    "RankingExpectation",
    "StepExpectation",
    "SuiteSpec",
    "SuitesManifest",
    "ToolOutputExpectation",
]

# ---------------------------------------------------------------------------
# Vocabulary and bounds
# ---------------------------------------------------------------------------

#: A case id is a Python-identifier-like slug; it is also the file stem and
#: the `evaluation_results.case_id` value (§12.8), so it must be filesystem-
#: and database-safe.
CASE_ID_PATTERN: Final = r"^[a-z][a-z0-9_]{2,63}$"
_SLUG_PATTERN: Final = r"^[a-z][a-z0-9_]*$"
_STEP_ID_PATTERN: Final = r"^s[1-9][0-9]*(\[[0-9]+\])?$"
_COLUMN_PATTERN: Final = r"^[a-z][a-z0-9_]*$"

MAX_TITLE_LENGTH: Final = 120
#: Mirrors `RunCreateRequest.user_request` (§13.2): a case request must be
#: one the API would accept, or the suite would be testing something the
#: product cannot do. `tests/test_evaluation_cases.py` pins the two together.
MAX_REQUEST_LENGTH: Final = 4000
MAX_DURATION_MS: Final = 600_000

DEFAULT_FIXTURE_SET: Final = "default"
RUN_ID_PLACEHOLDER: Final = "$run_id"

#: The seven cases the architecture requires (§15.3). The loader does not
#: enforce this list — a scratch directory may hold one case — but the
#: canonical `backend/evals/` tree must, and the test suite asserts it.
REQUIRED_CASE_IDS: Final[tuple[str, ...]] = (
    "happy_path_multi_step",
    "lead_ranking",
    "company_research",
    "approval_required",
    "approval_rejected",
    "retryable_failure",
    "invalid_tool_result",
)

#: The named groupings `suites.yaml` must declare (§15.3). `all` must list
#: every case; the loader enforces both.
REQUIRED_SUITES: Final[frozenset[str]] = frozenset({"all", "smoke", "safety"})

#: The tables a case may assert on (§12.3-12.7, §12.9). Fully qualified, so
#: a case cannot reach the LangGraph saver's tables or the evaluation tables
#: themselves. Membership in the real ORM metadata is pinned by a test, not
#: by importing the persistence layer here.
ASSERTABLE_TABLES: Final[frozenset[str]] = frozenset(
    {
        "opspilot.agent_runs",
        "opspilot.execution_steps",
        "opspilot.tool_calls",
        "opspilot.approvals",
        "opspilot.trace_events",
        "mock_crm.companies",
        "mock_crm.leads",
        "mock_crm.customers",
        "mock_crm.outreach_drafts",
        "mock_crm.email_outbox",
    }
)

#: RFC 2606 reserved names. Fixture domains and addresses must resolve to
#: nothing, ever (§16.7, §19.2).
RFC2606_DOMAIN_SUFFIXES: Final[tuple[str, ...]] = (
    ".example",
    ".test",
    ".invalid",
    ".localhost",
    "example.com",
    "example.net",
    "example.org",
)

_EMAIL_RE: Final = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")


def _is_reserved_domain(domain: str) -> bool:
    lowered = domain.lower()
    return any(lowered == s.lstrip(".") or lowered.endswith(s) for s in RFC2606_DOMAIN_SUFFIXES)


def _require_reserved_email(value: str) -> str:
    match = _EMAIL_RE.match(value)
    if match is None:
        raise ValueError(f"{value!r} is not an email address")
    if not _is_reserved_domain(match.group(1)):
        raise ValueError(f"{value!r} is not on an RFC 2606 reserved domain")
    return value


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value


def _require_unique(values: list[str], *, what: str) -> list[str]:
    seen: set[str] = set()
    for v in values:
        if v in seen:
            raise ValueError(f"duplicate {what}: {v!r}")
        seen.add(v)
    return values


#: Per fixture table, the fields the database keeps unique (§12.9).
_FIXTURE_UNIQUE_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "companies": ("company_id", "domain"),
    "leads": ("lead_id", "email"),
    "customers": ("customer_id", "email"),
}

#: Step statuses that imply the tool was dispatched at least once.
_EXECUTED_STEP_STATUSES: Final[frozenset[StepStatus]] = frozenset(
    {StepStatus.RUNNING, StepStatus.SUCCEEDED, StepStatus.FAILED}
)


class EvalModel(BaseModel):
    """Strict, immutable base for every evaluation definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# given: how the world is pinned
# ---------------------------------------------------------------------------
class ApprovalPolicyKind(StrEnum):
    """The scripted `ApprovalPolicy` vocabulary (§15.5). It replaces the
    human, never the gate: the runner posts real decisions through the
    approval service and the case sees real `approvals` rows."""

    APPROVE = "approve"
    REJECT = "reject"
    APPROVE_AFTER = "approve_after"
    NEVER = "never"


class ApprovalsSpec(EvalModel):
    policy: ApprovalPolicyKind
    reason: str | None = Field(default=None, max_length=500)
    #: `approve_after(n)`: approve the n-th request, reject those before it.
    after: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _after_matches_policy(self) -> ApprovalsSpec:
        if self.policy is ApprovalPolicyKind.APPROVE_AFTER and self.after is None:
            raise ValueError("policy 'approve_after' needs 'after'")
        if self.policy is not ApprovalPolicyKind.APPROVE_AFTER and self.after is not None:
            raise ValueError(
                f"'after' is only valid with policy 'approve_after', not {self.policy}"
            )
        return self


class FailureInjectionKind(StrEnum):
    """What the `FailureInjector` does to a `(tool, attempt)` (§15.2).

    `lying_success` is the §11 case: the tool reports success and persists
    nothing, so only an independent read-back can catch it.
    """

    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    LYING_SUCCESS = "lying_success"


class FailureInjection(EvalModel):
    """Explicit, not probabilistic: which attempts of which tool misbehave."""

    tool: ToolName
    kind: FailureInjectionKind
    attempts: list[int] | Literal["all"]
    note: str | None = Field(default=None, max_length=500)

    @field_validator("attempts")
    @classmethod
    def _attempts_are_positive_unique_sorted(cls, v: list[int] | str) -> list[int] | str:
        if isinstance(v, str):
            return v
        if not v:
            raise ValueError("attempts must name at least one attempt or be 'all'")
        if any(a < 1 for a in v):
            raise ValueError("attempts are 1-based")
        if v != sorted(set(v)):
            raise ValueError("attempts must be unique and ascending")
        return v

    @model_validator(mode="after")
    def _lying_success_needs_a_readback(self) -> FailureInjection:
        """A lie is only observable where the contract promises an independent
        read-back; injecting it elsewhere would assert nothing."""
        if self.kind is FailureInjectionKind.LYING_SUCCESS:
            contract = REGISTRY[self.tool]
            if contract.verification is not VerificationMode.READBACK:
                raise ValueError(
                    f"lying_success requires a read-back verified tool; "
                    f"{self.tool} is verified by {contract.verification}"
                )
        return self


class GivenSpec(EvalModel):
    request: str = Field(min_length=1, max_length=MAX_REQUEST_LENGTH)
    fixtures: str = Field(default=DEFAULT_FIXTURE_SET, pattern=_SLUG_PATTERN)
    planner: PlannerKind
    seed: int = Field(ge=0)
    approvals: ApprovalsSpec | None = None
    inject: list[FailureInjection] = Field(default_factory=list)
    #: Budget overrides; absent means the runtime defaults (§17.1).
    budgets: Budgets | None = None

    @field_validator("request")
    @classmethod
    def _request_is_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("request must not be blank")
        return v

    @model_validator(mode="after")
    def _injections_do_not_overlap(self) -> GivenSpec:
        """One behaviour per `(tool, attempt)`; `all` claims every attempt."""
        claimed: dict[ToolName, set[int | str]] = {}
        for inj in self.inject:
            existing = claimed.setdefault(inj.tool, set())
            new: set[int | str] = {"all"} if isinstance(inj.attempts, str) else set(inj.attempts)
            if existing and ("all" in existing or "all" in new or existing & new):
                raise ValueError(f"conflicting injections for {inj.tool.value}")
            existing |= new
        return self


# ---------------------------------------------------------------------------
# expect: what must be true afterwards
# ---------------------------------------------------------------------------
class DbAssertion(EvalModel):
    """`count` rows in `table` matching `where`. Values are literals, except
    `$run_id`, which the runner substitutes with the case's run."""

    table: str
    where: dict[str, str | int | bool | None] = Field(default_factory=dict)
    count: int = Field(ge=0)

    @field_validator("table")
    @classmethod
    def _table_is_assertable(cls, v: str) -> str:
        if v not in ASSERTABLE_TABLES:
            raise ValueError(f"{v!r} is not an assertable table: {sorted(ASSERTABLE_TABLES)}")
        return v

    @field_validator("where")
    @classmethod
    def _columns_are_plain(cls, v: dict[str, Any]) -> dict[str, Any]:
        for column in v:
            if not re.match(_COLUMN_PATTERN, column):
                raise ValueError(f"{column!r} is not a column name")
        return v


class BackoffExpectation(EvalModel):
    """The formula of §10.4: `min(base * 2^(attempt-1), max) * jitter`, with
    jitter drawn from the seeded generator inside `[jitter_min, jitter_max]`."""

    base_ms: int = Field(ge=0)
    max_ms: int = Field(ge=0)
    jitter_min: float = Field(gt=0.0, le=1.0)
    jitter_max: float = Field(ge=1.0)

    @model_validator(mode="after")
    def _max_at_least_base(self) -> BackoffExpectation:
        if self.max_ms < self.base_ms:
            raise ValueError("max_ms must be >= base_ms")
        return self


class StepExpectation(EvalModel):
    step_id: str = Field(pattern=_STEP_ID_PATTERN)
    tool: ToolName
    status: StepStatus | None = None
    attempts: int | None = Field(default=None, ge=1)
    retry_count: int | None = Field(default=None, ge=0)
    verification_status: VerificationStatus | None = None
    backoff: BackoffExpectation | None = None

    @model_validator(mode="after")
    def _retries_are_attempts_minus_one(self) -> StepExpectation:
        if (
            self.attempts is not None
            and self.retry_count is not None
            and self.retry_count != self.attempts - 1
        ):
            raise ValueError(
                f"retry_count {self.retry_count} contradicts attempts {self.attempts} "
                f"(expected {self.attempts - 1})"
            )
        if self.backoff is not None and (self.retry_count == 0 or self.attempts == 1):
            raise ValueError("backoff is asserted on a step that never retries")
        return self


class PlanStepExpectation(EvalModel):
    step_id: str = Field(pattern=_STEP_ID_PATTERN)
    tool: ToolName
    #: When given, the step's literal arguments must equal these exactly.
    args: dict[str, Any] | None = None


class PlanExpectation(EvalModel):
    """The intended plan. Asserting it is how `company_research` proves an
    injected instruction changed nothing (§16.3 rule 7)."""

    revision: int = Field(default=0, ge=0)
    steps: list[PlanStepExpectation] = Field(min_length=1)

    @field_validator("steps")
    @classmethod
    def _step_ids_unique(cls, v: list[PlanStepExpectation]) -> list[PlanStepExpectation]:
        _require_unique([s.step_id for s in v], what="plan step id")
        return v


class ApprovalExpectation(EvalModel):
    """What the gate did. Counts are over `approvals` rows for the run."""

    requested: int = Field(ge=0)
    approved: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)
    #: The gated tool the (first) approval is for.
    tool: ToolName | None = None
    #: Keys that must be present in `approvals.payload_preview` (§9.3).
    preview_contains: list[str] = Field(default_factory=list)
    #: Asserted while the run is `awaiting_approval`, before the policy acts.
    while_paused: list[DbAssertion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _decisions_fit_requests(self) -> ApprovalExpectation:
        if self.approved + self.rejected > self.requested:
            raise ValueError("approved + rejected exceeds requested")
        if self.tool is not None and not REGISTRY[self.tool].requires_approval:
            raise ValueError(f"{self.tool} is not a gated tool")
        if self.while_paused and self.requested == 0:
            raise ValueError("while_paused needs at least one requested approval")
        return self


class ToolOutputExpectation(EvalModel):
    """Shape checks on a tool's validated output. Field names are dotted
    paths into the output (`profile.confidence`)."""

    tool: ToolName
    step_id: str | None = Field(default=None, pattern=_STEP_ID_PATTERN)
    required_fields: list[str] = Field(default_factory=list)
    #: Inclusive numeric bounds per field, e.g. `confidence: [0, 1]`.
    ranges: dict[str, tuple[float, float]] = Field(default_factory=dict)
    equals: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ranges")
    @classmethod
    def _ranges_are_ordered(
        cls, v: dict[str, tuple[float, float]]
    ) -> dict[str, tuple[float, float]]:
        for name, (lo, hi) in v.items():
            if lo > hi:
                raise ValueError(f"range for {name!r} is inverted")
        return v


class RankingExpectation(EvalModel):
    """`score_lead` results, best first."""

    expected_order: list[str] = Field(min_length=1)
    expected_scores: dict[str, int] = Field(default_factory=dict)
    factors_sum_to_score: bool = True
    tolerance: int = Field(default=1, ge=0)
    stable_across_reruns: bool = True

    @field_validator("expected_order")
    @classmethod
    def _order_unique(cls, v: list[str]) -> list[str]:
        return _require_unique(v, what="lead id in expected_order")

    @field_validator("expected_scores")
    @classmethod
    def _scores_in_range(cls, v: dict[str, int]) -> dict[str, int]:
        for lead_id, score in v.items():
            if not 0 <= score <= 100:
                raise ValueError(f"score for {lead_id!r} must be within 0..100")
        return v

    @model_validator(mode="after")
    def _scores_name_ranked_leads(self) -> RankingExpectation:
        unknown = set(self.expected_scores) - set(self.expected_order)
        if unknown:
            raise ValueError(
                f"expected_scores names leads not in expected_order: {sorted(unknown)}"
            )
        ordered = [
            self.expected_scores[x] for x in self.expected_order if x in self.expected_scores
        ]
        if ordered != sorted(ordered, reverse=True):
            raise ValueError("expected_scores are not descending in expected_order")
        return self


class ExpectSpec(EvalModel):
    final_status: RunStatus
    status_reason: str | None = Field(default=None, pattern=_SLUG_PATTERN, max_length=64)
    tools_called: list[ToolName] = Field(default_factory=list)
    tools_not_called: list[ToolName] = Field(default_factory=list)
    #: Exact order of successful tool calls, when the order itself matters.
    tool_sequence: list[ToolName] | None = None
    #: `tool_calls` rows per tool (every attempt counts, §12.5).
    tool_call_counts: dict[ToolName, int] = Field(default_factory=dict)
    steps: list[StepExpectation] = Field(default_factory=list)
    plan: PlanExpectation | None = None
    approval: ApprovalExpectation | None = None
    tool_outputs: list[ToolOutputExpectation] = Field(default_factory=list)
    ranking: RankingExpectation | None = None
    db: list[DbAssertion] = Field(default_factory=list)
    response_mentions: list[str] = Field(default_factory=list)
    response_not_mentions: list[str] = Field(default_factory=list)
    max_duration_ms: int = Field(ge=1, le=MAX_DURATION_MS)
    #: Global invariant 7 (§15.6): a `policy_violation` event fails the case
    #: unless it explicitly expects one.
    policy_violation_expected: bool = False

    @field_validator("tools_called", "tools_not_called")
    @classmethod
    def _tools_unique(cls, v: list[ToolName]) -> list[ToolName]:
        _require_unique([t.value for t in v], what="tool")
        return v

    @field_validator("response_mentions", "response_not_mentions")
    @classmethod
    def _phrases_not_blank(cls, v: list[str]) -> list[str]:
        if any(not p.strip() for p in v):
            raise ValueError("phrases must not be blank")
        return v

    @field_validator("tool_call_counts")
    @classmethod
    def _counts_positive(cls, v: dict[ToolName, int]) -> dict[ToolName, int]:
        if any(n < 1 for n in v.values()):
            raise ValueError("tool_call_counts must be >= 1; use tools_not_called for zero")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> ExpectSpec:
        if self.final_status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"final_status must be terminal, not {self.final_status}")
        if self.final_status is RunStatus.COMPLETED and self.status_reason is not None:
            raise ValueError("a completed run carries no status_reason")
        if (
            self.final_status in (RunStatus.FAILED, RunStatus.REJECTED, RunStatus.EXPIRED)
            and self.status_reason is None
        ):
            raise ValueError(f"a {self.final_status} run must name its status_reason")

        called = set(self.tools_called)
        overlap = called & set(self.tools_not_called)
        if overlap:
            raise ValueError(
                f"tools_called and tools_not_called overlap: {sorted(t.value for t in overlap)}"
            )
        if self.tool_sequence is not None and set(self.tool_sequence) != called:
            raise ValueError("tool_sequence must contain exactly the tools in tools_called")
        for label, tools in (
            ("tool_call_counts", set(self.tool_call_counts)),
            ("tool_outputs", {o.tool for o in self.tool_outputs}),
        ):
            stray = tools - called
            if stray:
                raise ValueError(
                    f"{label} names tools not in tools_called: {sorted(t.value for t in stray)}"
                )
        _require_unique([s.step_id for s in self.steps], what="step id in steps")
        for step in self.steps:
            # A step whose tool never ran may still be asserted on — it is
            # how a case pins "the send stayed pending / was rejected" — but
            # it cannot claim attempts, an outcome or a verification verdict.
            if step.tool in called:
                continue
            if step.status in _EXECUTED_STEP_STATUSES or step.attempts or step.retry_count:
                raise ValueError(
                    f"steps[{step.step_id}] asserts execution of {step.tool.value}, "
                    "which is not in tools_called"
                )
            if step.verification_status not in (None, VerificationStatus.NOT_REQUIRED):
                raise ValueError(
                    f"steps[{step.step_id}] asserts a verification verdict for "
                    f"{step.tool.value}, which is not in tools_called"
                )
        if self.plan is not None:
            planned = {s.tool for s in self.plan.steps}
            stray = called - planned
            if stray:
                raise ValueError(
                    f"tools_called names tools the expected plan never schedules: "
                    f"{sorted(t.value for t in stray)}"
                )
        if self.approval is not None and self.approval.tool is not None:
            if self.approval.approved and self.approval.tool not in called:
                raise ValueError(f"an approved {self.approval.tool} must appear in tools_called")
            if not self.approval.approved and self.approval.tool in called:
                raise ValueError(
                    f"{self.approval.tool} cannot be called without an approved approval"
                )
        if self.ranking is not None and ToolName.SCORE_LEAD not in called:
            raise ValueError("ranking needs score_lead in tools_called")
        return self


# ---------------------------------------------------------------------------
# The case
# ---------------------------------------------------------------------------
class EvalCase(EvalModel):
    id: str = Field(pattern=CASE_ID_PATTERN)
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    suite: list[str] = Field(min_length=1)
    given: GivenSpec
    expect: ExpectSpec

    @field_validator("title")
    @classmethod
    def _title_is_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("title must not be blank")
        return v

    @field_validator("suite")
    @classmethod
    def _suites_are_slugs(cls, v: list[str]) -> list[str]:
        for name in v:
            if not re.match(_SLUG_PATTERN, name):
                raise ValueError(f"{name!r} is not a suite name")
        return _require_unique(v, what="suite")

    @model_validator(mode="after")
    def _gated_tools_need_a_policy(self) -> EvalCase:
        """A case that expects a gated tool to run must say who approves it;
        there is no other way for the run to get past the gate (§9)."""
        gated = [t for t in self.expect.tools_called if REGISTRY[t].requires_approval]
        if gated and self.given.approvals is None:
            raise ValueError(
                f"tools_called includes gated tools {[t.value for t in gated]} "
                "but given.approvals declares no policy"
            )
        if self.expect.approval is not None and self.given.approvals is None:
            raise ValueError("expect.approval needs given.approvals")
        return self


# ---------------------------------------------------------------------------
# suites.yaml
# ---------------------------------------------------------------------------
class SuiteSpec(EvalModel):
    description: str | None = Field(default=None, max_length=500)
    cases: list[str] = Field(min_length=1)

    @field_validator("cases")
    @classmethod
    def _cases_are_ids(cls, v: list[str]) -> list[str]:
        for case_id in v:
            if not re.match(CASE_ID_PATTERN, case_id):
                raise ValueError(f"{case_id!r} is not a case id")
        return _require_unique(v, what="case in suite")


class SuitesManifest(EvalModel):
    suites: dict[str, SuiteSpec] = Field(min_length=1)

    @field_validator("suites")
    @classmethod
    def _names_are_slugs(cls, v: dict[str, SuiteSpec]) -> dict[str, SuiteSpec]:
        for name in v:
            if not re.match(_SLUG_PATTERN, name):
                raise ValueError(f"{name!r} is not a suite name")
        return v


# ---------------------------------------------------------------------------
# fixtures/*.yaml — the shape of `app/integrations/mock/fixtures.py`
# ---------------------------------------------------------------------------
class CompanySignalFixture(EvalModel):
    """A buying signal (`Signal` in the tool schemas). `summary` is third-party
    prose: it is the prompt-injection surface, and a fixture may carry an
    injected instruction *as data* precisely so the suite can prove it stays
    data (§16.3)."""

    kind: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=1000)
    observed_at: datetime | None = None

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _require_aware(v)


class CompanyFixture(EvalModel):
    company_id: str = Field(pattern=r"^comp_[a-z0-9_]+$")
    name: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    industry: str | None = None
    employee_count: int | None = Field(default=None, ge=0)
    revenue_band: str | None = None
    hq_location: str | None = None
    funding_stage: str | None = None
    tech_stack: list[str] = Field(default_factory=list)
    signals: list[CompanySignalFixture] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("domain")
    @classmethod
    def _domain_is_reserved(cls, v: str) -> str:
        if not _is_reserved_domain(v):
            raise ValueError(f"{v!r} is not an RFC 2606 reserved domain")
        return v


class LeadFixture(EvalModel):
    lead_id: str = Field(pattern=r"^L-[0-9]+$")
    company_id: str = Field(pattern=r"^comp_[a-z0-9_]+$")
    full_name: str = Field(min_length=1)
    title: str | None = None
    email: str
    status: LeadStatus
    source: str | None = None
    owner: str | None = None
    phone: str | None = None
    timezone: str | None = None
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    last_contacted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("last_contacted_at")
    @classmethod
    def _aware_optional(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _require_aware(v)

    @field_validator("email")
    @classmethod
    def _email_is_reserved(cls, v: str) -> str:
        return _require_reserved_email(v)


class CustomerFixture(EvalModel):
    customer_id: str = Field(pattern=r"^cust_[a-z0-9_]+$")
    account_name: str = Field(min_length=1)
    primary_contact: str = Field(min_length=1)
    email: str
    phone: str | None = None
    status: CustomerStatus
    plan: str | None = None
    mrr: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    owner: str | None = None
    notes: str | None = None
    version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("email")
    @classmethod
    def _email_is_reserved(cls, v: str) -> str:
        return _require_reserved_email(v)


class FixtureDataset(EvalModel):
    """One named dataset (`given.fixtures`), assembled from the three files.
    Referential integrity is checked here so a broken dataset fails at load,
    not as a foreign-key error halfway through a case."""

    name: str = Field(pattern=_SLUG_PATTERN)
    companies: list[CompanyFixture] = Field(min_length=1)
    leads: list[LeadFixture] = Field(min_length=1)
    customers: list[CustomerFixture] = Field(default_factory=list)

    @model_validator(mode="after")
    def _referentially_sound(self) -> FixtureDataset:
        # The same uniqueness rule per table, driven by a table of (rows,
        # field) pairs: the database enforces each of these as a primary key
        # or a UNIQUE constraint (§12.9), so a violation here is a seed error.
        for table, unique_fields in _FIXTURE_UNIQUE_FIELDS.items():
            rows: list[EvalModel] = getattr(self, table)
            for field_name in unique_fields:
                values = [str(getattr(row, field_name)).lower() for row in rows]
                _require_unique(values, what=f"{table}.{field_name}")
        known = {c.company_id for c in self.companies}
        for lead in self.leads:
            if lead.company_id not in known:
                raise ValueError(
                    f"lead {lead.lead_id} references unknown company {lead.company_id}"
                )
        return self

    def company(self, company_id: str) -> CompanyFixture | None:
        return next((c for c in self.companies if c.company_id == company_id), None)

    def lead(self, lead_id: str) -> LeadFixture | None:
        return next((lead for lead in self.leads if lead.lead_id == lead_id), None)
