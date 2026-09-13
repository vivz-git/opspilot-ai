"""Abstract repository protocols (§12, DB-005).

Defines typed, runtime-checkable protocols for all control-plane and mock-CRM
persistence aggregates. Higher layers (services, agent nodes, tools, API handlers)
depend strictly on these protocol interfaces, never on concrete SQLAlchemy
sessions or ORM query construction.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Any, Protocol, Self, runtime_checkable

from app.agent.state import ApprovalStatus, PlannerKind, RunStatus, StepStatus, VerificationStatus
from app.persistence.mock_crm import (
    Company,
    Customer,
    CustomerStatus,
    EmailOutbox,
    Lead,
    LeadStatus,
    OutreachDraft,
    OutreachDraftStatus,
)
from app.persistence.models import (
    AgentRun,
    ApprovalRow,
    EvaluationResult,
    EvaluationRun,
    EvaluationRunStatus,
    ExecutionStep,
    ToolCallRow,
    ToolCallStatus,
    TraceEvent,
    TraceEventKind,
    TraceEventSeverity,
)
from app.tools.contracts import RiskLevel, ToolName

__all__ = [
    "AgentRunRepository",
    "ApprovalRepository",
    "CompanyRepository",
    "CustomerRepository",
    "EmailOutboxRepository",
    "EvaluationRepository",
    "ExecutionStepRepository",
    "LeadRepository",
    "OutreachDraftRepository",
    "ToolCallRepository",
    "TraceEventRepository",
    "UnitOfWork",
]


@runtime_checkable
class AgentRunRepository(Protocol):
    """Repository protocol for the `opspilot.agent_runs` aggregate (§12.3)."""

    async def get(self, run_id: uuid.UUID) -> AgentRun | None:
        """Retrieve a run by its UUID."""
        ...

    async def get_by_idempotency_key(self, idempotency_key: str) -> AgentRun | None:
        """Retrieve a run by its unique idempotency key."""
        ...

    async def create(
        self,
        *,
        user_request: str,
        planner_kind: PlannerKind = PlannerKind.RULES,
        deadline_at: datetime,
        id: uuid.UUID | None = None,
        parent_run_id: uuid.UUID | None = None,
        status: RunStatus = RunStatus.CREATED,
        status_reason: str | None = None,
        normalized_task: dict[str, Any] | None = None,
        plan: dict[str, Any] | None = None,
        plan_history: list[Any] | None = None,
        plan_revision: int = 0,
        final_response: dict[str, Any] | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        seed: int | None = None,
        idempotency_key: str | None = None,
        actor_id: str | None = None,
        evaluation_run_id: uuid.UUID | None = None,
        eval_case_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRun:
        """Create and persist a new agent run."""
        ...

    async def update_status(
        self,
        run_id: uuid.UUID,
        *,
        status: RunStatus,
        status_reason: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
    ) -> AgentRun | None:
        """Update run lifecycle status and timestamps."""
        ...

    async def update_plan(
        self,
        run_id: uuid.UUID,
        *,
        plan: dict[str, Any],
        plan_revision: int,
        plan_history: list[Any] | None = None,
    ) -> AgentRun | None:
        """Update current plan and append to plan history."""
        ...

    async def update_final_response(
        self,
        run_id: uuid.UUID,
        *,
        final_response: dict[str, Any],
        status: RunStatus = RunStatus.COMPLETED,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
    ) -> AgentRun | None:
        """Record the terminal final response of a run."""
        ...

    async def increment_counters(
        self,
        run_id: uuid.UUID,
        *,
        step_count_delta: int = 0,
        retry_delta: int = 0,
        replan_delta: int = 0,
    ) -> AgentRun | None:
        """Atomically increment denormalized execution counters."""
        ...

    async def transition_status(
        self,
        run_id: uuid.UUID,
        *,
        expected: Iterable[RunStatus],
        status: RunStatus,
        owner: str | None = None,
        status_reason: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
        release_lease: bool = False,
    ) -> AgentRun | None:
        """Conditional lifecycle transition (§5.4), decided by the database.

        One `UPDATE … WHERE id = :id AND status IN :expected [AND lease_owner
        = :owner] RETURNING *`. Returns the updated row when this call won,
        or `None` when the run was not in an expected status (or, when
        `owner` is given, is not currently leased by that owner) — the
        caller must treat `None` as "someone else moved this run", never
        retry blindly. `release_lease=True` clears the lease in the same
        statement, for terminal transitions and for handing a paused run
        back to nobody (§6.3: the driving task ends on interrupt).
        """
        ...

    async def acquire_lease(
        self,
        run_id: uuid.UUID,
        *,
        owner: str,
        now: datetime,
        ttl: timedelta,
        expected: Iterable[RunStatus] = (RunStatus.QUEUED, RunStatus.RUNNING),
        status: RunStatus | None = None,
    ) -> AgentRun | None:
        """Atomically take ownership of a run (DB-007, ADR-023).

        Succeeds only if the run is in an `expected` status AND the lease is
        free: no owner, already this owner (idempotent re-acquire), or
        expired as of `now` (`lease_expires_at <= now` — the exact complement
        of `heartbeat_lease`'s liveness test). Optionally moves `status` in the same
        statement (e.g. `awaiting_approval → running` on approval resume).
        Returns the row on success, `None` if another live owner holds it.
        """
        ...

    async def heartbeat_lease(
        self, run_id: uuid.UUID, *, owner: str, now: datetime, ttl: timedelta
    ) -> bool:
        """Extend the lease to `now + ttl` — only for its current, unexpired
        owner on a `queued`/`running` run. Returns `False` for a stale or
        non-owner worker, which **must** then stop driving the run: an
        expired lease is never revived, because someone else may already
        own it (§2.4)."""
        ...

    async def release_lease(self, run_id: uuid.UUID, *, owner: str) -> bool:
        """Clear the lease if `owner` still holds it. `False` means it was
        already lost or released — not an error, just nothing to do."""
        ...

    async def list_runs(
        self,
        *,
        status: RunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentRun]:
        """List runs with optional status filtering and pagination."""
        ...

    async def list_orphaned_runs(self, *, now: datetime, limit: int = 50) -> list[AgentRun]:
        """The reconciler's only query (§12.3): `queued`/`running` runs whose
        lease has expired as of `now` or was never taken. Terminal and
        `awaiting_approval` runs are never candidates — a paused run has no
        worker by design (§6.3) and is not an orphan."""
        ...


@runtime_checkable
class ExecutionStepRepository(Protocol):
    """Repository protocol for `opspilot.execution_steps` (§12.4)."""

    async def get(self, step_uuid: uuid.UUID) -> ExecutionStep | None:
        """Retrieve an execution step by its UUID PK."""
        ...

    async def get_by_step_id(
        self, run_id: uuid.UUID, step_id: str, plan_revision: int
    ) -> ExecutionStep | None:
        """Retrieve a specific step instance for a run and plan revision."""
        ...

    async def list_by_run(self, run_id: uuid.UUID) -> list[ExecutionStep]:
        """List all execution steps for a run ordered by sequence."""
        ...

    async def create(
        self,
        *,
        run_id: uuid.UUID,
        step_id: str,
        plan_revision: int,
        seq: int,
        tool: ToolName,
        tool_version: str = "1.0.0",
        parent_step_id: str | None = None,
        status: StepStatus = StepStatus.PENDING,
        args: dict[str, Any] | None = None,
        args_hash: str | None = None,
        depends_on: list[str] | None = None,
        optional: bool = False,
        id: uuid.UUID | None = None,
    ) -> ExecutionStep:
        """Create and persist a planned execution step."""
        ...

    async def update_status(
        self,
        step_uuid: uuid.UUID,
        *,
        status: StepStatus,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
        error: dict[str, Any] | None = None,
    ) -> ExecutionStep | None:
        """Update step execution status."""
        ...

    async def record_result(
        self,
        step_uuid: uuid.UUID,
        *,
        result: dict[str, Any],
        status: StepStatus = StepStatus.SUCCEEDED,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
    ) -> ExecutionStep | None:
        """Record step output and mark terminal outcome."""
        ...

    async def record_verification(
        self,
        step_uuid: uuid.UUID,
        *,
        verification_status: VerificationStatus,
        verification: dict[str, Any] | None = None,
    ) -> ExecutionStep | None:
        """Record verification outcome for the step."""
        ...

    async def increment_attempts(
        self, step_uuid: uuid.UUID, *, retry_count_delta: int = 0
    ) -> ExecutionStep | None:
        """Increment attempts and retry count on a step."""
        ...


@runtime_checkable
class ToolCallRepository(Protocol):
    """Repository protocol for `opspilot.tool_calls` (§12.5)."""

    async def get(self, tool_call_id: uuid.UUID) -> ToolCallRow | None:
        """Retrieve a tool call by its UUID PK."""
        ...

    async def get_by_attempt(
        self, execution_step_id: uuid.UUID, attempt: int
    ) -> ToolCallRow | None:
        """Retrieve a specific attempt of an execution step."""
        ...

    async def get_by_idempotency_key(self, idempotency_key: str) -> ToolCallRow | None:
        """Retrieve a tool call by its idempotency key."""
        ...

    async def list_by_step(self, execution_step_id: uuid.UUID) -> list[ToolCallRow]:
        """List all tool call attempts for an execution step."""
        ...

    async def list_by_run(self, run_id: uuid.UUID) -> list[ToolCallRow]:
        """List all tool call attempts for a run."""
        ...

    async def record_call(
        self,
        *,
        run_id: uuid.UUID,
        execution_step_id: uuid.UUID,
        step_id: str,
        attempt: int,
        tool: ToolName,
        input: dict[str, Any],
        status: ToolCallStatus,
        tool_version: str = "1.0.0",
        output: dict[str, Any] | None = None,
        input_hash: str | None = None,
        error_class: str | None = None,
        error_message: str | None = None,
        idempotency_key: str | None = None,
        port: str | None = None,
        adapter: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
        id: uuid.UUID | None = None,
    ) -> ToolCallRow:
        """Record a single tool call attempt."""
        ...


@runtime_checkable
class ApprovalRepository(Protocol):
    """Repository protocol for `opspilot.approvals` (§12.6, §9.6)."""

    async def get(self, approval_id: uuid.UUID) -> ApprovalRow | None:
        """Retrieve an approval by its UUID PK."""
        ...

    async def get_pending(self, run_id: uuid.UUID, step_id: str) -> ApprovalRow | None:
        """Retrieve the pending approval for a given run and step, if any."""
        ...

    async def create_request(
        self,
        *,
        run_id: uuid.UUID,
        step_id: str,
        tool: ToolName,
        risk: RiskLevel,
        title: str,
        summary: str,
        payload_preview: dict[str, Any],
        args_hash: str,
        requested_at: datetime,
        expires_at: datetime,
        id: uuid.UUID | None = None,
    ) -> ApprovalRow:
        """Create and persist a pending approval request."""
        ...

    async def decide(
        self,
        approval_id: uuid.UUID,
        *,
        status: ApprovalStatus,
        decided_by: str | None = None,
        decision_reason: str | None = None,
        decided_at: datetime,
    ) -> ApprovalRow | None:
        """Atomic conditional decision: UPDATE WHERE status='pending' RETURNING *.

        Returns the updated row if this update won the race, or None if the
        approval was not pending (409 conflict).
        """
        ...

    async def supersede(
        self, approval_id: uuid.UUID, superseded_by: uuid.UUID
    ) -> ApprovalRow | None:
        """Mark an approval superseded when arguments change during replanning."""
        ...

    async def list_pending(self, *, limit: int = 50) -> list[ApprovalRow]:
        """List active pending approvals for the operator queue."""
        ...

    async def list_expired(self, *, now: datetime, limit: int = 50) -> list[ApprovalRow]:
        """List pending approvals whose TTL has expired."""
        ...

    async def list_by_run(self, run_id: uuid.UUID) -> list[ApprovalRow]:
        """List all approvals associated with a run."""
        ...


@runtime_checkable
class TraceEventRepository(Protocol):
    """Repository protocol for `opspilot.trace_events` (§12.7, §14)."""

    async def append(
        self,
        *,
        run_id: uuid.UUID,
        kind: TraceEventKind,
        severity: TraceEventSeverity = TraceEventSeverity.INFO,
        node: str | None = None,
        tool: ToolName | None = None,
        step_id: str | None = None,
        attempt: int | None = None,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        retry_count: int | None = None,
        error: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> TraceEvent:
        """Atomically allocate monotonic `seq` via advisory lock and insert."""
        ...

    async def list_by_run(
        self, run_id: uuid.UUID, *, after_seq: int = 0, limit: int = 100
    ) -> list[TraceEvent]:
        """List events for a run ordered by `seq` for SSE streaming or polling."""
        ...

    async def get_latest(self, run_id: uuid.UUID) -> TraceEvent | None:
        """Get the latest trace event for a run."""
        ...


@runtime_checkable
class EvaluationRepository(Protocol):
    """Repository protocol for `opspilot.evaluation_runs` / `results` (§12.8)."""

    async def get_run(self, evaluation_run_id: uuid.UUID) -> EvaluationRun | None:
        """Retrieve an evaluation suite run by its UUID."""
        ...

    async def create_run(
        self,
        *,
        suite: str,
        planner_kind: PlannerKind = PlannerKind.RULES,
        git_sha: str | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        seed: int | None = None,
        id: uuid.UUID | None = None,
    ) -> EvaluationRun:
        """Create a new evaluation suite run."""
        ...

    async def complete_run(
        self,
        evaluation_run_id: uuid.UUID,
        *,
        status: EvaluationRunStatus,
        finished_at: datetime,
        case_count: int,
        passed: int,
        failed: int,
        metrics: dict[str, Any],
    ) -> EvaluationRun | None:
        """Mark an evaluation suite run complete with metrics snapshot."""
        ...

    async def record_result(
        self,
        *,
        evaluation_run_id: uuid.UUID,
        case_id: str,
        run_id: uuid.UUID,
        passed: bool,
        assertions: list[Any],
        duration_ms: int | None = None,
        retry_count: int = 0,
        tool_calls_count: int = 0,
        approval_outcome: str | None = None,
        failure_reason: str | None = None,
        id: uuid.UUID | None = None,
    ) -> EvaluationResult:
        """Record the outcome of a single evaluation test case."""
        ...

    async def list_results(self, evaluation_run_id: uuid.UUID) -> list[EvaluationResult]:
        """List all case results for a suite run."""
        ...

    async def list_runs(self, *, suite: str | None = None, limit: int = 50) -> list[EvaluationRun]:
        """List evaluation runs ordered by started_at DESC."""
        ...


@runtime_checkable
class CompanyRepository(Protocol):
    """Repository protocol for `mock_crm.companies` (§12.9)."""

    async def get(self, company_id: str) -> Company | None:
        """Retrieve a company by its ID."""
        ...

    async def get_by_domain(self, domain: str) -> Company | None:
        """Retrieve a company by its unique domain."""
        ...

    async def list_all(self, *, industry: str | None = None, limit: int = 100) -> list[Company]:
        """List companies with optional industry filter."""
        ...

    async def create(self, company: Company) -> Company:
        """Persist a company record."""
        ...


@runtime_checkable
class LeadRepository(Protocol):
    """Repository protocol for `mock_crm.leads` (§12.9)."""

    async def get(self, lead_id: str) -> Lead | None:
        """Retrieve a lead by its ID."""
        ...

    async def get_by_email(self, email: str) -> Lead | None:
        """Retrieve a lead by its email."""
        ...

    async def list_by_company(self, company_id: str) -> list[Lead]:
        """List all leads belonging to a company."""
        ...

    async def list_by_status(self, status: LeadStatus, *, limit: int = 100) -> list[Lead]:
        """List leads by status."""
        ...

    async def update_status(
        self,
        lead_id: str,
        status: LeadStatus,
        *,
        last_contacted_at: datetime | None = None,
    ) -> Lead | None:
        """Update a lead's status and optional last_contacted_at."""
        ...

    async def create(self, lead: Lead) -> Lead:
        """Persist a lead record."""
        ...

    async def search(
        self,
        *,
        industry: str | None = None,
        location: str | None = None,
        min_employees: int | None = None,
        max_employees: int | None = None,
        status: LeadStatus | None = None,
        query: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[Lead], int]:
        """Search leads with multi-field filters, returning (leads, total_count)."""
        ...


@runtime_checkable
class CustomerRepository(Protocol):
    """Repository protocol for `mock_crm.customers` (§12.9, §8.5)."""

    async def get(self, customer_id: str) -> Customer | None:
        """Retrieve a customer by its ID."""
        ...

    async def get_by_email(self, email: str) -> Customer | None:
        """Retrieve a customer by unique email."""
        ...

    async def create(self, customer: Customer) -> Customer:
        """Persist a customer record."""
        ...

    async def update_optimistic(
        self,
        customer_id: str,
        *,
        expected_version: int,
        account_name: str | None = None,
        primary_contact: str | None = None,
        phone: str | None = None,
        status: CustomerStatus | None = None,
        plan: str | None = None,
        mrr: Decimal | None = None,
        owner: str | None = None,
        notes: str | None = None,
    ) -> Customer | None:
        """Optimistically update customer where version = expected_version.

        Increments `version` and sets `updated_at`. Returns the updated Customer
        if successful, or None if the record was modified concurrently or missing.
        """
        ...


@runtime_checkable
class OutreachDraftRepository(Protocol):
    """Repository protocol for `mock_crm.outreach_drafts` (§12.9)."""

    async def get(self, draft_id: str) -> OutreachDraft | None:
        """Retrieve a draft by ID."""
        ...

    async def list_by_lead(self, lead_id: str) -> list[OutreachDraft]:
        """List drafts associated with a lead."""
        ...

    async def create(self, draft: OutreachDraft) -> OutreachDraft:
        """Persist an outreach draft."""
        ...

    async def update_status(
        self, draft_id: str, status: OutreachDraftStatus
    ) -> OutreachDraft | None:
        """Update draft status (saved, sent, archived)."""
        ...


@runtime_checkable
class EmailOutboxRepository(Protocol):
    """Repository protocol for `mock_crm.email_outbox` (§12.9)."""

    async def get(self, outbox_id: str) -> EmailOutbox | None:
        """Retrieve an outbox message by ID."""
        ...

    async def get_by_message_id(self, message_id: str) -> EmailOutbox | None:
        """Retrieve an outbox message by unique message_id."""
        ...

    async def get_by_idempotency_key(self, idempotency_key: str) -> EmailOutbox | None:
        """Retrieve an outbox message by unique idempotency_key."""
        ...

    async def list_by_run(self, run_id: str) -> list[EmailOutbox]:
        """List all outbox messages recorded for a run."""
        ...

    async def create(self, entry: EmailOutbox) -> EmailOutbox:
        """Insert an outbox entry. Raises on duplicate idempotency_key or message_id."""
        ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Unit of Work interface managing explicit transaction boundaries (§12).

    Exposes all domain repositories within a shared transaction context.
    Transactions require an EXPLICIT call to `commit()`; exiting the context
    without commit (or upon exception) automatically executes a rollback.
    """

    @property
    def agent_runs(self) -> AgentRunRepository: ...

    @property
    def execution_steps(self) -> ExecutionStepRepository: ...

    @property
    def tool_calls(self) -> ToolCallRepository: ...

    @property
    def approvals(self) -> ApprovalRepository: ...

    @property
    def trace_events(self) -> TraceEventRepository: ...

    @property
    def evaluations(self) -> EvaluationRepository: ...

    @property
    def companies(self) -> CompanyRepository: ...

    @property
    def leads(self) -> LeadRepository: ...

    @property
    def customers(self) -> CustomerRepository: ...

    @property
    def outreach_drafts(self) -> OutreachDraftRepository: ...

    @property
    def email_outbox(self) -> EmailOutboxRepository: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None:
        """Explicitly commit the transaction."""
        ...

    async def rollback(self) -> None:
        """Explicitly roll back the transaction."""
        ...
