"""Human-in-the-loop approval HTTP endpoints (§13.5).

Exposes:
- `GET /approvals/queue`
- `GET /approvals/{approval_id}`
- `POST /approvals/{approval_id}/decision`

Delegates 100% of decision logic, exact args_hash binding, atomic transitions,
trace events, and graph resumption to `ApprovalService`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_approval_service, require_authorization
from app.api.schemas import ApprovalDecisionRequest, ApprovalResource
from app.errors import NotFoundError
from app.execution.approvals import ApprovalService

__all__ = [
    "router",
]

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("/queue", response_model=list[ApprovalResource])
async def list_approval_queue(
    limit: int = Query(default=50, ge=1, le=100, description="Max pending approvals to return."),
    service: ApprovalService = Depends(get_approval_service),
    _auth: None = Depends(require_authorization),
) -> list[ApprovalResource]:
    """Return pending approvals ordered deterministically for operator review."""
    approvals = await service.list_pending_queue(limit=limit)
    return [ApprovalResource.from_row(row) for row in approvals]


@router.get("/{approval_id}", response_model=ApprovalResource)
async def get_approval(
    approval_id: uuid.UUID,
    service: ApprovalService = Depends(get_approval_service),
    _auth: None = Depends(require_authorization),
) -> ApprovalResource:
    """Retrieve a single approval by its UUID."""
    approval = await service.get_approval(approval_id)
    if approval is None:
        raise NotFoundError(f"Approval {approval_id} not found")
    return ApprovalResource.from_row(approval)


@router.post("/{approval_id}/decision", response_model=ApprovalResource)
async def decide_approval(
    approval_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    service: ApprovalService = Depends(get_approval_service),
    _auth: None = Depends(require_authorization),
) -> ApprovalResource:
    """Record a human operator's decision and safely resume the execution graph.

    Passes the caller's exact `args_hash` to `ApprovalService.decide_approval(...)`.
    The database conditional update resolves races and idempotency.
    """
    result = await service.decide_approval(
        approval_id,
        decision=body.decision,
        args_hash=body.args_hash,
        decided_by=body.decided_by,
        reason=body.reason,
    )
    return ApprovalResource.from_row(result.approval)
