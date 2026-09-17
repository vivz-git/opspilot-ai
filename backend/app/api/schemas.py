"""Pydantic v2 schemas for the human-in-the-loop approval API (§13.5).

Enforces strict input validation (extra="forbid", reject requires reason) and
safe output representations that never expose internal persistence, tokens,
or credentials.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.state import ApprovalDecisionKind, ApprovalStatus, PlannerKind, RunStatus
from app.errors import InputValidationError
from app.observability.redaction import redact_payload
from app.persistence.models import (
    AgentRun,
    ApprovalRow,
    ExecutionStep,
    TraceEvent,
    TraceEventKind,
    TraceEventSeverity,
)

__all__ = [
    "ApprovalDecisionRequest",
    "ApprovalResource",
    "RunCancelRequest",
    "RunCounters",
    "RunCreateRequest",
    "RunListResponse",
    "RunPendingApproval",
    "RunResource",
    "RunRetryRequest",
    "RunStepSummary",
    "RunSummary",
    "RunTimestamps",
    "TraceEventResource",
    "TraceResponse",
    "decode_cursor",
    "encode_cursor",
]


class ApprovalDecisionRequest(BaseModel):
    """Operator decision payload for POST /approvals/{approval_id}/decision."""

    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecisionKind
    args_hash: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description=(
            "Canonical SHA-256 hash the operator saw (§13.5). Optional but recommended: "
            "when supplied it must equal the persisted hash exactly, else 409 approval_superseded."
        ),
    )
    decided_by: str | None = Field(default=None, max_length=255, description="Client attribution.")
    reason: str | None = Field(default=None, max_length=500, description="Operator reason.")

    @model_validator(mode="after")
    def validate_reject_reason(self) -> Self:
        if self.decision == ApprovalDecisionKind.REJECT and not (
            self.reason and self.reason.strip()
        ):
            raise ValueError("reason is required when rejecting an approval")
        return self


class ApprovalResource(BaseModel):
    """Safe external representation of an approval awaiting or settled by human decision."""

    model_config = ConfigDict(extra="forbid")

    approval_id: uuid.UUID
    run_id: uuid.UUID
    step_id: str
    tool: str
    risk: str
    title: str
    summary: str
    status: ApprovalStatus
    args_hash: str
    payload_preview: dict[str, Any]
    created_at: datetime
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    reason: str | None = None

    @classmethod
    def from_row(cls, row: ApprovalRow) -> ApprovalResource:
        """Construct a safe ApprovalResource from an internal persistence row."""
        safe_preview = redact_payload(row.payload_preview or {}, max_bytes=4096)
        risk_value = row.risk.value if hasattr(row.risk, "value") else str(row.risk)
        return cls(
            approval_id=row.id,
            run_id=row.run_id,
            step_id=row.step_id,
            tool=str(row.tool),
            risk=risk_value,
            title=row.title,
            summary=row.summary,
            status=row.status,
            args_hash=row.args_hash,
            payload_preview=safe_preview,
            created_at=row.requested_at,
            requested_at=row.requested_at,
            expires_at=row.expires_at,
            decided_at=row.decided_at,
            decided_by=row.decided_by,
            reason=row.decision_reason,
        )


class RunCreateRequest(BaseModel):
    """Payload for POST /runs creating a new agent run (§13.2)."""

    model_config = ConfigDict(extra="forbid")

    user_request: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="Natural-language business request for the agent to execute.",
    )
    auto_start: bool = Field(
        default=False,
        description="Whether to immediately schedule execution in the background.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary client metadata.",
    )
    planner_kind: PlannerKind | None = Field(
        default=None,
        description="Optional planner override (rules or llm).",
    )

    @model_validator(mode="after")
    def validate_user_request(self) -> Self:
        if not self.user_request.strip():
            raise ValueError("user_request cannot be empty or whitespace only")
        return self


class RunCounters(BaseModel):
    """Denormalized execution counters for a run (§13.3)."""

    model_config = ConfigDict(extra="forbid")

    step_count: int = Field(default=0, ge=0)
    retry_total: int = Field(default=0, ge=0)
    replan_count: int = Field(default=0, ge=0)


class RunTimestamps(BaseModel):
    """Lifecycle timestamp snapshot for a run (§13.3)."""

    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    deadline_at: datetime


class RunStepSummary(BaseModel):
    """Public summary of a single execution step instance (§13.3)."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    seq: int
    tool: str
    status: str
    attempts: int = 1
    retry_count: int = 0
    verification_status: str | None = None
    duration_ms: int | None = None
    error: dict[str, Any] | None = None


class RunPendingApproval(BaseModel):
    """Public summary of a currently open approval gate on the run (§13.3)."""

    model_config = ConfigDict(extra="forbid")

    approval_id: uuid.UUID
    step_id: str
    tool: str
    risk: str
    title: str
    summary: str
    payload_preview: dict[str, Any]
    args_hash: str
    expires_at: datetime


class RunResource(BaseModel):
    """The canonical run detail resource returned by POST /runs and GET /runs/{id} (§13.3)."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    parent_run_id: uuid.UUID | None = None
    status: RunStatus
    status_reason: str | None = None
    user_request: str
    normalized_task: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    steps: list[RunStepSummary] = Field(default_factory=list)
    pending_approval: RunPendingApproval | None = None
    counters: RunCounters
    resumable: bool
    final_response: dict[str, Any] | None = None
    timestamps: RunTimestamps
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_row(
        cls,
        row: AgentRun,
        *,
        steps: list[ExecutionStep] | None = None,
        pending_approval: ApprovalRow | None = None,
    ) -> RunResource:
        """Construct a safe RunResource from an internal AgentRun persistence row."""
        # Clean & redact metadata
        meta = dict(row.metadata_ or {})
        planner_val = (
            row.planner_kind.value if hasattr(row.planner_kind, "value") else str(row.planner_kind)
        )
        meta.setdefault("planner_kind", planner_val)
        meta.setdefault("model_id", row.model_id)
        meta.setdefault("prompt_version", row.prompt_version)
        meta.setdefault("seed", row.seed)

        # Pending approval
        approval_summary: RunPendingApproval | None = None
        if pending_approval is not None and pending_approval.status == ApprovalStatus.PENDING:
            safe_preview = redact_payload(pending_approval.payload_preview or {}, max_bytes=4096)
            risk_val = (
                pending_approval.risk.value
                if hasattr(pending_approval.risk, "value")
                else str(pending_approval.risk)
            )
            approval_summary = RunPendingApproval(
                approval_id=pending_approval.id,
                step_id=pending_approval.step_id,
                tool=str(pending_approval.tool),
                risk=risk_val,
                title=pending_approval.title,
                summary=pending_approval.summary,
                payload_preview=safe_preview,
                args_hash=pending_approval.args_hash,
                expires_at=pending_approval.expires_at,
            )

        # Execution steps
        steps_summary: list[RunStepSummary] = []
        if steps:
            for s in steps:
                status_val = s.status.value if hasattr(s.status, "value") else str(s.status)
                verify_val = (
                    s.verification_status.value
                    if hasattr(s.verification_status, "value")
                    else (str(s.verification_status) if s.verification_status else None)
                )
                err_dict = (
                    redact_payload(s.error, max_bytes=2048) if isinstance(s.error, dict) else None
                )
                steps_summary.append(
                    RunStepSummary(
                        step_id=s.step_id,
                        seq=s.seq,
                        tool=str(s.tool),
                        status=status_val,
                        attempts=s.attempts,
                        retry_count=s.retry_count,
                        verification_status=verify_val,
                        duration_ms=s.duration_ms,
                        error=err_dict,
                    )
                )

        # Resumable evaluation
        is_resumable = row.status == RunStatus.AWAITING_APPROVAL

        # Final response
        clean_final = (
            redact_payload(row.final_response, max_bytes=4096)
            if isinstance(row.final_response, dict)
            else None
        )

        return cls(
            run_id=row.id,
            parent_run_id=row.parent_run_id,
            status=row.status,
            status_reason=row.status_reason,
            user_request=row.user_request,
            normalized_task=row.normalized_task,
            plan=row.plan,
            steps=steps_summary,
            pending_approval=approval_summary,
            counters=RunCounters(
                step_count=row.step_count,
                retry_total=row.retry_total,
                replan_count=row.replan_count,
            ),
            resumable=is_resumable,
            final_response=clean_final,
            timestamps=RunTimestamps(
                created_at=row.created_at,
                started_at=row.started_at,
                finished_at=row.finished_at,
                deadline_at=row.deadline_at,
            ),
            metadata=meta,
        )


class RunSummary(BaseModel):
    """Lightweight summary of a run for listing and operational status (§13.2, §13.3)."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    parent_run_id: uuid.UUID | None = None
    status: RunStatus
    status_reason: str | None = None
    user_request: str
    counters: RunCounters
    resumable: bool
    timestamps: RunTimestamps
    duration_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_row(cls, row: AgentRun) -> RunSummary:
        meta = dict(row.metadata_ or {})
        planner_val = (
            row.planner_kind.value if hasattr(row.planner_kind, "value") else str(row.planner_kind)
        )
        meta.setdefault("planner_kind", planner_val)
        meta.setdefault("model_id", row.model_id)
        meta.setdefault("prompt_version", row.prompt_version)
        meta.setdefault("seed", row.seed)
        is_resumable = row.status == RunStatus.AWAITING_APPROVAL
        return cls(
            run_id=row.id,
            parent_run_id=row.parent_run_id,
            status=row.status,
            status_reason=row.status_reason,
            user_request=row.user_request,
            counters=RunCounters(
                step_count=row.step_count,
                retry_total=row.retry_total,
                replan_count=row.replan_count,
            ),
            resumable=is_resumable,
            timestamps=RunTimestamps(
                created_at=row.created_at,
                started_at=row.started_at,
                finished_at=row.finished_at,
                deadline_at=row.deadline_at,
            ),
            duration_ms=row.duration_ms,
            metadata=meta,
        )


class RunListResponse(BaseModel):
    """Paginated list of run summaries with keyset cursor (§13.2)."""

    model_config = ConfigDict(extra="forbid")

    items: list[RunSummary]
    next_cursor: str | None = None
    total_estimate: int


class RunCancelRequest(BaseModel):
    """Optional payload for POST /runs/{run_id}/cancel (§13.2)."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500, description="Cancellation reason.")


class RunRetryRequest(BaseModel):
    """Optional payload for POST /runs/{run_id}/retry (§13.2)."""

    model_config = ConfigDict(extra="forbid")

    auto_start: bool = Field(default=False, description="Whether to queue the new run immediately.")
    metadata: dict[str, Any] | None = Field(
        default=None, description="Metadata override for child run."
    )


def encode_cursor(created_at: datetime, run_id: uuid.UUID) -> str:
    """Encode keyset cursor (created_at, id) into an opaque URL-safe base64 string (§13.2)."""
    payload = {
        "created_at": created_at.isoformat(),
        "id": str(run_id),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor_str: str) -> tuple[datetime, uuid.UUID]:
    """Decode and validate an opaque keyset cursor string (§13.2).

    Raises:
        InputValidationError: If the cursor is malformed, corrupt, or invalid.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor_str.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Cursor payload must be a JSON object")
        created_at_str = payload.get("created_at")
        id_str = payload.get("id")
        if not created_at_str or not id_str:
            raise ValueError("Cursor payload missing required fields ('created_at', 'id')")
        dt = datetime.fromisoformat(created_at_str)
        u_id = uuid.UUID(id_str)
        return dt, u_id
    except Exception as exc:
        raise InputValidationError(f"Invalid pagination cursor: {exc}") from exc


class TraceEventResource(BaseModel):
    """Public, safe representation of a durable trace event (§13.4, §14.3)."""

    model_config = ConfigDict(extra="forbid")

    seq: int
    ts: datetime
    kind: TraceEventKind
    severity: TraceEventSeverity
    node: str | None = None
    tool: str | None = None
    step_id: str | None = None
    attempt: int | None = None
    status: str | None = None
    duration_ms: int | None = None
    retry_count: int | None = None
    error: dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None

    @classmethod
    def from_row(cls, row: TraceEvent) -> TraceEventResource:
        """Construct a safe, redacted, detached TraceEventResource from an internal row."""
        safe_payload = (
            redact_payload(row.payload, max_bytes=4096) if isinstance(row.payload, dict) else {}
        )
        safe_error = (
            redact_payload(row.error, max_bytes=2048) if isinstance(row.error, dict) else None
        )
        safe_input = (
            redact_payload(row.input, max_bytes=4096) if isinstance(row.input, dict) else None
        )
        safe_output = (
            redact_payload(row.output, max_bytes=4096) if isinstance(row.output, dict) else None
        )
        tool_val = str(row.tool) if row.tool is not None else None
        return cls(
            seq=row.seq,
            ts=row.ts,
            kind=row.kind,
            severity=row.severity,
            node=row.node,
            tool=tool_val,
            step_id=row.step_id,
            attempt=row.attempt,
            status=row.status,
            duration_ms=row.duration_ms,
            retry_count=row.retry_count,
            error=safe_error,
            payload=safe_payload,
            input=safe_input,
            output=safe_output,
        )


class TraceResponse(BaseModel):
    """Paginated trace envelope (§13.4)."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    events: list[TraceEventResource]
    next_seq: int | None = None
    complete: bool
