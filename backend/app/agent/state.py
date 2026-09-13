"""The typed agent state and its reducers (§5).

The graph channel type is a `TypedDict` with `Annotated` reducers (LangGraph's
requirement); the values are Pydantic models so they validate, round-trip to
JSON and can be reused by the API layer.

The reducers are declared explicitly and deliberately. LangGraph re-executes
the interrupted node on resume, so a default last-write-wins reducer on a list
channel silently loses history, and a naive `approval_state` overwrite would
re-open an approval the operator already answered.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from app.errors import ErrorClass, RecoveryAction
from app.security import canonical_args_hash
from app.tools.contracts import RiskLevel, ToolName


# ---------------------------------------------------------------------------
# Lifecycle enums
# ---------------------------------------------------------------------------
class RunStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"  # a human declined: the mechanism working, not a failure
    CANCELLED = "cancelled"
    EXPIRED = "expired"  # an approval TTL elapsed


TERMINAL_RUN_STATUSES: Final[frozenset[RunStatus]] = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.REJECTED,
        RunStatus.CANCELLED,
        RunStatus.EXPIRED,
    }
)


class StepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    REJECTED = "rejected"


class VerificationStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PASSED = "passed"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"  # arguments changed after a human decided
    CANCELLED = "cancelled"


class ApprovalDecisionKind(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class PlannerKind(StrEnum):
    RULES = "rules"
    LLM = "llm"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Understanding and planning
# ---------------------------------------------------------------------------
class NormalizedTask(Model):
    """Separates understanding from planning, so planning is testable on
    structured input and out-of-scope requests are rejected before any plan
    tokens are spent."""

    intent: str
    entities: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    requires_mutation: bool = False
    in_scope: bool = True
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    notes: str | None = None


class FanOut(Model):
    """Expanded at decide-time once the referenced list exists (§4.5).
    `max_items` is mandatory: it is the only bound on plan expansion."""

    over: str
    as_: str = Field(alias="as")
    max_items: int = Field(ge=1, le=50)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PlanStep(Model):
    step_id: str  # stable: approvals, traces and $refs cite it
    tool: ToolName
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    rationale: str = ""
    optional: bool = False
    fanout: FanOut | None = None
    parent_step_id: str | None = None
    status: StepStatus = StepStatus.PENDING


class Plan(Model):
    plan_id: str
    revision: int = 0
    created_by: PlannerKind = PlannerKind.RULES
    steps: list[PlanStep] = Field(default_factory=list)

    def step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.step_id == step_id), None)


# ---------------------------------------------------------------------------
# Execution records
# ---------------------------------------------------------------------------
class ToolCall(Model):
    """One attempt. Failed attempts are recorded too — collapsing them would
    erase the evidence the retry metrics are computed from."""

    step_id: str
    tool: ToolName
    attempt: int = Field(ge=1)
    args_hash: str
    idempotency_key: str | None = None
    status: str = "started"
    error_class: ErrorClass | None = None
    error_message: str | None = None
    duration_ms: int | None = None
    started_at: datetime | None = None


class ToolResult(Model):
    """An entry in the artifact store that `$ref` resolves against."""

    step_id: str
    tool: ToolName
    output: dict[str, Any]
    produced_at: datetime


class VerificationCheck(Model):
    name: str
    passed: bool
    expected: Any = None
    observed: Any = None


class VerificationResult(Model):
    step_id: str
    status: VerificationStatus
    mode: str
    checks: list[VerificationCheck] = Field(default_factory=list)
    duration_ms: int | None = None
    detail: str | None = None


class AgentError(Model):
    step_id: str | None
    error_class: ErrorClass
    message: str
    attempt: int | None = None
    recovery: RecoveryAction | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------
class ApprovalRequest(Model):
    approval_id: str
    run_id: str
    step_id: str
    tool: ToolName
    risk: RiskLevel
    title: str
    summary: str
    #: The de-referenced effect — full draft text, or a field-level diff. An
    #: operator cannot meaningfully approve `{"draft_id": "d_91f"}`.
    payload_preview: dict[str, Any] = Field(default_factory=dict)
    args_hash: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime
    expires_at: datetime


class ApprovalDecision(Model):
    approval_id: str
    step_id: str
    decision: ApprovalDecisionKind
    args_hash: str  # the hash the human actually saw
    decided_by: str
    decided_at: datetime
    reason: str | None = None


class ApprovalState(Model):
    """The gate must be answerable from state alone at execute time, and
    decisions must survive a resume."""

    pending: ApprovalRequest | None = None
    decisions: dict[str, ApprovalDecision] = Field(default_factory=dict)

    def grants(self, step_id: str, args: dict[str, Any]) -> bool:
        """Is this step authorised for *these* arguments, right now?

        Three conditions, all required: a decision exists for the step, it is
        an approval, and its hash equals the hash of the arguments about to be
        sent. The third is what closes the time-of-check/time-of-use gap — a
        step-id-only grant would authorise arguments the human never saw.
        """
        decision = self.decisions.get(step_id)
        if decision is None or decision.decision is not ApprovalDecisionKind.APPROVE:
            return False
        return decision.args_hash == canonical_args_hash(args)

    def rejected(self, step_id: str) -> bool:
        decision = self.decisions.get(step_id)
        return decision is not None and decision.decision is ApprovalDecisionKind.REJECT


# ---------------------------------------------------------------------------
# Final response and metadata
# ---------------------------------------------------------------------------
class FinalResponse(Model):
    summary: str
    done: list[str] = Field(default_factory=list)
    not_done: list[str] = Field(default_factory=list)
    #: Effects that could not be verified. Never folded into `done`: an
    #: operator who believes an email was sent will not resend it (§11.4).
    unconfirmed: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    partial: bool = False


class Budgets(Model):
    max_retries: int = Field(default=2, ge=0, le=10)
    max_replans: int = Field(default=2, ge=0, le=10)
    max_steps: int = Field(default=25, ge=1, le=200)
    run_deadline_seconds: int = Field(default=300, ge=1)


class RunMetadata(Model):
    """Reproducibility. A run must be explainable months later, which requires
    knowing which planner, model and prompt version produced it."""

    planner_kind: PlannerKind = PlannerKind.RULES
    model_id: str | None = None
    prompt_version: str = "v1"
    seed: int | None = None
    actor_id: str | None = None
    trace_id: str | None = None
    budgets: Budgets = Field(default_factory=Budgets)
    evaluation_run_id: str | None = None
    eval_case_id: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------
def append_list(current: list[Any] | None, incoming: list[Any] | None) -> list[Any]:
    """History is never rewritten. Append also makes a resumed node's
    re-emission additive rather than destructive."""
    return [*(current or []), *(incoming or [])]


def merge_dict(current: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any]:
    """Key-wise merge: a node updating step `s3` must not drop `s1`'s entry."""
    return {**(current or {}), **(incoming or {})}


def merge_approval_state(
    current: ApprovalState | None, incoming: ApprovalState | None
) -> ApprovalState:
    """Never regress a decided approval to pending.

    `request_approval` runs at least twice per approval because LangGraph
    re-executes the interrupted node, so this merge is the idempotency guard
    (§9.7). Decisions accumulate; a pending request is cleared once the step it
    refers to has been decided.
    """
    if current is None:
        return incoming or ApprovalState()
    if incoming is None:
        return current

    decisions = {**current.decisions, **incoming.decisions}
    pending = incoming.pending if incoming.pending is not None else current.pending
    if pending is not None and pending.step_id in decisions:
        pending = None
    return ApprovalState(pending=pending, decisions=decisions)


class AgentState(TypedDict, total=False):
    """The graph's channel schema. Every field is justified in §5.2."""

    run_id: str
    user_request: str
    normalized_task: NormalizedTask | None
    plan: Plan | None
    plan_history: Annotated[list[Plan], append_list]
    current_step_id: str | None
    tool_calls: Annotated[list[ToolCall], append_list]
    tool_results: Annotated[dict[str, ToolResult], merge_dict]
    approval_state: Annotated[ApprovalState, merge_approval_state]
    errors: Annotated[list[AgentError], append_list]
    retry_count: Annotated[dict[str, int], merge_dict]
    replan_count: int
    step_count: int
    verification_result: Annotated[dict[str, VerificationResult], merge_dict]
    final_response: FinalResponse | None
    status: RunStatus
    status_reason: str | None
    created_at: datetime
    updated_at: datetime
    deadline_at: datetime
    metadata: RunMetadata
