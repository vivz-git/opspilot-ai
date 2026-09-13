"""Control-plane ORM models: `agent_runs`, `execution_steps`, `tool_calls`,
`approvals` (§12.3-12.6, DB-001) and `trace_events` (§12.7, DB-002).

Column types follow §12.10 (ADR-014) literally: a column typed `enum` in the
architecture becomes a real database-enforced enum (`Enum(..., native_enum=
False, create_constraint=True)` — a `CHECK` constraint, portable and cheap to
alter); a column typed `text` stays a plain, unconstrained text column even
when its values happen to come from a Python `StrEnum` (`planner_kind`,
`tool`), because the architecture deliberately did not gate those at the
database.

These are plain SQLAlchemy declarative models — nothing here is imported by
`app.agent` or `app.tools`. DB-005 adds the repository layer that keeps
`select()` out of services; until then, this module is the only place that
knows what the tables look like.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.agent.state import ApprovalStatus, PlannerKind, RunStatus, StepStatus, VerificationStatus
from app.persistence.base import Base
from app.tools.contracts import RiskLevel, ToolName

__all__ = [
    "AgentRun",
    "ApprovalRow",
    "ExecutionStep",
    "ToolCallRow",
    "ToolCallStatus",
    "TraceEvent",
    "TraceEventKind",
    "TraceEventSeverity",
]


class ToolCallStatus(StrEnum):
    """One attempt's terminal outcome (§12.5).

    Distinct from `app.agent.state.ToolCall.status`, which is an in-flight
    string (`"started"`) on the graph channel — this is the row written once
    the attempt has actually finished.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    DUPLICATE_SUPPRESSED = "duplicate_suppressed"


class TraceEventKind(StrEnum):
    """§14.2 — the fixed vocabulary of product-trace events.

    Structural (§14.4): `@traced_node` and `ToolRegistry.dispatch` are the
    only emitters, so this list is exactly what those choke points can ever
    produce, not an open vocabulary a future call site might extend ad hoc.
    """

    RUN_CREATED = "run_created"
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_REJECTED = "run_rejected"
    RUN_EXPIRED = "run_expired"
    RUN_CANCELLED = "run_cancelled"
    NODE_ENTERED = "node_entered"
    NODE_EXITED = "node_exited"
    PLAN_CREATED = "plan_created"
    PLAN_REVISED = "plan_revised"
    FANOUT_EXPANDED = "fanout_expanded"
    TOOL_STARTED = "tool_started"
    TOOL_SUCCEEDED = "tool_succeeded"
    TOOL_FAILED = "tool_failed"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_DUPLICATE_SUPPRESSED = "tool_duplicate_suppressed"
    RETRY_SCHEDULED = "retry_scheduled"
    STEP_SKIPPED = "step_skipped"
    BUDGET_EXHAUSTED = "budget_exhausted"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_SUPERSEDED = "approval_superseded"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_SKIPPED = "verification_skipped"
    POLICY_VIOLATION = "policy_violation"


class TraceEventSeverity(StrEnum):
    """§12.7 — always `error` for `policy_violation` (§14.2)."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


def _enum_values(enum_cls: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_cls]


def _enum_column(enum_cls: type[StrEnum], name: str) -> sa.Enum:
    """A `CHECK`-constrained text column for a genuinely `enum`-typed field.

    `native_enum=False` avoids a separate `CREATE TYPE` lifecycle to manage
    across migrations (adding a value would otherwise require `ALTER TYPE …
    ADD VALUE`, which cannot run inside a transaction on older Postgres); the
    constraint still gives the database-level enforcement the architecture
    asks for.
    """
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=_enum_values,
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _timestamptz() -> sa.TIMESTAMP:
    return sa.TIMESTAMP(timezone=True)


class AgentRun(Base):
    """§12.3 — one row per run. The aggregate root."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_status_created_at", "status", sa.text("created_at DESC")),
        Index("ix_agent_runs_created_at", sa.text("created_at DESC")),
        Index("ix_agent_runs_parent_run_id", "parent_run_id"),
        Index("ix_agent_runs_evaluation_run_id", "evaluation_run_id"),
        Index(
            "ix_agent_runs_lease_expires_at_active",
            "lease_expires_at",
            postgresql_where=sa.text("status IN ('running', 'queued')"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), ForeignKey("opspilot.agent_runs.id"), nullable=True
    )
    status: Mapped[RunStatus] = mapped_column(
        _enum_column(RunStatus, "agent_run_status"),
        nullable=False,
        server_default=sa.text("'created'"),
    )
    status_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    user_request: Mapped[str] = mapped_column(sa.Text, nullable=False)
    normalized_task: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    plan_history: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    plan_revision: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    final_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Reproducibility (§12.3). Plain text by design (§12.10) — `rules`/`llm`
    #: is not gated at the database.
    planner_kind: Mapped[PlannerKind] = mapped_column(sa.Text, nullable=False)
    model_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    seed: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(sa.Text, nullable=True, unique=True)
    actor_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    step_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    retry_total: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    replan_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    deadline_at: Mapped[datetime] = mapped_column(_timestamptz(), nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(_timestamptz(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=sa.func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(_timestamptz(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(_timestamptz(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
    )
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    #: FK to `evaluation_runs` is added by DB-003, which is the migration
    #: that actually creates that table (DB-003 depends on DB-001, not the
    #: reverse — the column exists now so the run schema does not change
    #: shape twice).
    evaluation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), nullable=True
    )
    eval_case_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )


class ExecutionStep(Base):
    """§12.4 — one row per planned step instance, including fan-out children."""

    __tablename__ = "execution_steps"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "step_id", "plan_revision", name="uq_execution_steps_run_step_revision"
        ),
        Index("ix_execution_steps_run_id_seq", "run_id", "seq"),
        Index("ix_execution_steps_run_id_status", "run_id", "status"),
        Index(
            "ix_execution_steps_verification_failed",
            "verification_status",
            postgresql_where=sa.text("verification_status = 'failed'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("opspilot.agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    parent_step_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    plan_revision: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    tool: Mapped[ToolName] = mapped_column(sa.Text, nullable=False)
    tool_version: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'1.0.0'")
    )
    status: Mapped[StepStatus] = mapped_column(
        _enum_column(StepStatus, "execution_step_status"),
        nullable=False,
        server_default=sa.text("'pending'"),
    )
    args: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    args_hash: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    retry_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    verification_status: Mapped[VerificationStatus] = mapped_column(
        _enum_column(VerificationStatus, "execution_step_verification_status"),
        nullable=False,
        server_default=sa.text("'not_required'"),
    )
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    depends_on: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")
    )
    optional: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    started_at: Mapped[datetime | None] = mapped_column(_timestamptz(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(_timestamptz(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)


class ToolCallRow(Base):
    """§12.5 — one row per **attempt**, kept separate from `execution_steps`
    precisely because a step has many attempts and collapsing them would
    erase retry evidence."""

    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint(
            "execution_step_id", "attempt", name="uq_tool_calls_execution_step_id_attempt"
        ),
        Index("ix_tool_calls_run_id_started_at", "run_id", "started_at"),
        Index("ix_tool_calls_tool_status", "tool", "status"),
        Index("ix_tool_calls_idempotency_key", "idempotency_key"),
        Index(
            "ix_tool_calls_error_class_failed",
            "error_class",
            postgresql_where=sa.text("status = 'failed'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("opspilot.agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_step_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("opspilot.execution_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    tool: Mapped[ToolName] = mapped_column(sa.Text, nullable=False)
    tool_version: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'1.0.0'")
    )
    input: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    status: Mapped[ToolCallStatus] = mapped_column(
        _enum_column(ToolCallStatus, "tool_call_status"), nullable=False
    )
    error_class: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    #: e.g. `MailPort` / `mock` — an audit record that the mock adapter
    #: actually served the call (§19.3). Null for tools with no port
    #: (`score_lead` is pure computation).
    port: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    adapter: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(_timestamptz(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(_timestamptz(), nullable=True)


class ApprovalRow(Base):
    """§12.6 — the human-in-the-loop gate's persisted state."""

    __tablename__ = "approvals"
    __table_args__ = (
        # The database guarantees at most one open approval per step, which
        # is what makes the re-executed `request_approval` node idempotent
        # (§9.7, ADR-007).
        Index(
            "uq_approvals_run_id_step_id_pending",
            "run_id",
            "step_id",
            unique=True,
            postgresql_where=sa.text("status = 'pending'"),
        ),
        Index("ix_approvals_status_expires_at", "status", "expires_at"),
        Index("ix_approvals_status_requested_at", "status", sa.text("requested_at DESC")),
        Index("ix_approvals_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("opspilot.agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    tool: Mapped[ToolName] = mapped_column(sa.Text, nullable=False)
    risk: Mapped[RiskLevel] = mapped_column(
        _enum_column(RiskLevel, "approval_risk"), nullable=False
    )
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    summary: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload_preview: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    args_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        _enum_column(ApprovalStatus, "approval_status"),
        nullable=False,
        server_default=sa.text("'pending'"),
    )
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), ForeignKey("opspilot.approvals.id"), nullable=True
    )
    requested_at: Mapped[datetime] = mapped_column(_timestamptz(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(_timestamptz(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(_timestamptz(), nullable=True)
    #: Client-supplied, not authenticated (§16.6, ADR-017) — attribution, not
    #: an audit guarantee.
    decided_by: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


class TraceEvent(Base):
    """§12.7 — append-only. Never updated, never deleted except by retention.

    `seq` is **per-run monotonic** and is the SSE event id / polling cursor
    (§14.3): it, not `ts`, is the ordering key, because two events written in
    the same millisecond must still have a total order. Allocating a
    duplicate-free, gap-free `seq` under concurrent writers for the same run
    is DB-002's concurrency requirement — see `app.persistence.trace_events
    .append_trace_event` for the allocation mechanism. `UNIQUE(run_id, seq)`
    here is the database-level backstop, not the sole correctness mechanism.

    `id` is a `bigserial`, not a uuid like the other control-plane tables
    (§12.7 is explicit): this is the fastest-growing table by an order of
    magnitude, and a purely sequential key avoids the write-amplification a
    random uuid primary key causes on the very table retention/partitioning
    (OBS-004) cares most about.
    """

    __tablename__ = "trace_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_trace_events_run_id_seq"),
        Index("ix_trace_events_run_id_id", "run_id", "id"),
        Index("ix_trace_events_kind_ts", "kind", sa.text("ts DESC")),
        Index("ix_trace_events_ts_brin", "ts", postgresql_using="brin"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("opspilot.agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    ts: Mapped[datetime] = mapped_column(
        _timestamptz(), nullable=False, server_default=sa.func.now()
    )
    kind: Mapped[TraceEventKind] = mapped_column(
        _enum_column(TraceEventKind, "trace_event_kind"), nullable=False
    )
    severity: Mapped[TraceEventSeverity] = mapped_column(
        _enum_column(TraceEventSeverity, "trace_event_severity"),
        nullable=False,
        server_default=sa.text("'info'"),
    )
    node: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    tool: Mapped[ToolName | None] = mapped_column(sa.Text, nullable=True)
    step_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    attempt: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Plain text by design (§12.10): unlike the lifecycle `status` columns on
    #: sibling tables, this field's vocabulary varies by `kind` rather than
    #: naming one fixed state machine, so the architecture does not type it
    #: `enum`.
    status: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    retry_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
