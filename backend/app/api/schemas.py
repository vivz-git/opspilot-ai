"""Pydantic v2 schemas for the human-in-the-loop approval API (§13.5).

Enforces strict input validation (extra="forbid", reject requires reason) and
safe output representations that never expose internal persistence, tokens,
or credentials.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.state import ApprovalDecisionKind, ApprovalStatus
from app.observability.redaction import redact_payload
from app.persistence.models import ApprovalRow

__all__ = [
    "ApprovalDecisionRequest",
    "ApprovalResource",
]


class ApprovalDecisionRequest(BaseModel):
    """Operator decision payload for POST /approvals/{approval_id}/decision."""

    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecisionKind
    args_hash: str = Field(..., min_length=1, max_length=128, description="Canonical SHA-256 hash.")
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
