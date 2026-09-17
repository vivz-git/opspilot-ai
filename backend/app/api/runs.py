"""Agent run HTTP control-plane endpoints (§13.2, §13.3).

Exposes:
- `POST /runs` (create a durable run with optional idempotency key)
- `GET /runs/{run_id}` (retrieve full run state, execution steps, and open approvals)

Delegates all persistence, idempotency evaluation, and status retrieval to `RunService`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from sse_starlette.event import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from app.agent.state import RunStatus
from app.api.dependencies import get_run_service, require_authorization
from app.api.schemas import (
    RunCancelRequest,
    RunCreateRequest,
    RunListResponse,
    RunResource,
    RunRetryRequest,
    RunSummary,
    TraceEventResource,
    TraceResponse,
)
from app.errors import InputValidationError, NotFoundError
from app.execution.runs import RunService
from app.persistence.models import TraceEventKind, TraceEventSeverity

__all__ = [
    "router",
]

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post("", response_model=RunResource, status_code=status.HTTP_201_CREATED)
async def create_run(
    body: RunCreateRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    service: RunService = Depends(get_run_service),
    _auth: None = Depends(require_authorization),
) -> RunResource:
    """Create a new durable run or return the existing run on idempotent replay (§13.2)."""
    result = await service.create_run(
        body.user_request,
        idempotency_key=idempotency_key,
        metadata=body.metadata,
        auto_start=body.auto_start,
        planner_kind=body.planner_kind,
    )

    if result.is_duplicate:
        response.status_code = status.HTTP_200_OK
    else:
        response.status_code = status.HTTP_201_CREATED
        response.headers["Location"] = f"/runs/{result.run.id}"

    details = await service.get_run_details(result.run.id)
    if details is not None:
        return RunResource.from_row(
            details.run,
            steps=details.steps,
            pending_approval=details.pending_approval,
        )
    return RunResource.from_row(result.run)


@router.get("", response_model=RunListResponse)
async def list_runs(
    status: list[RunStatus] | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    parent_run_id: uuid.UUID | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None),
    service: RunService = Depends(get_run_service),
    _auth: None = Depends(require_authorization),
) -> RunListResponse:
    """List runs with optional filtering and keyset cursor pagination (§13.2)."""
    runs, next_cursor, total_estimate = await service.list_runs(
        statuses=status,
        since=since,
        until=until,
        parent_run_id=parent_run_id,
        query=q,
        cursor_str=cursor,
        limit=limit,
    )
    items = [RunSummary.from_row(r) for r in runs]
    return RunListResponse(
        items=items,
        next_cursor=next_cursor,
        total_estimate=total_estimate,
    )


@router.get("/{run_id}", response_model=RunResource)
async def get_run(
    run_id: uuid.UUID,
    service: RunService = Depends(get_run_service),
    _auth: None = Depends(require_authorization),
) -> RunResource:
    """Retrieve full run details including execution steps and pending approval (§13.3)."""
    details = await service.get_run_details(run_id)
    if details is None:
        raise NotFoundError(f"Run {run_id} not found")

    return RunResource.from_row(
        details.run,
        steps=details.steps,
        pending_approval=details.pending_approval,
    )


@router.post("/{run_id}/start", response_model=RunSummary, status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    run_id: uuid.UUID,
    service: RunService = Depends(get_run_service),
    _auth: None = Depends(require_authorization),
) -> RunSummary:
    """Transition a run from created to queued and schedule execution (§13.2)."""
    run = await service.start_run(run_id)
    return RunSummary.from_row(run)


@router.post("/{run_id}/cancel", response_model=RunSummary, status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    run_id: uuid.UUID,
    body: RunCancelRequest | None = None,
    service: RunService = Depends(get_run_service),
    _auth: None = Depends(require_authorization),
) -> RunSummary:
    """Cooperatively cancel an active or non-terminal run (§13.2)."""
    reason = body.reason if body else None
    run = await service.cancel_run(run_id, reason=reason)
    return RunSummary.from_row(run)


@router.post("/{run_id}/retry", response_model=RunResource, status_code=status.HTTP_201_CREATED)
async def retry_run(
    run_id: uuid.UUID,
    response: Response,
    body: RunRetryRequest | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    service: RunService = Depends(get_run_service),
    _auth: None = Depends(require_authorization),
) -> RunResource:
    """Create a new run linked to parent run_id, copying user_request (§10.6, §13.2)."""
    auto_start = body.auto_start if body else False
    metadata = body.metadata if body else None
    result = await service.retry_run(
        run_id,
        idempotency_key=idempotency_key,
        auto_start=auto_start,
        metadata=metadata,
    )

    if result.is_duplicate:
        response.status_code = status.HTTP_200_OK
    else:
        response.status_code = status.HTTP_201_CREATED
        response.headers["Location"] = f"/runs/{result.run.id}"

    details = await service.get_run_details(result.run.id)
    if details is not None:
        return RunResource.from_row(
            details.run,
            steps=details.steps,
            pending_approval=details.pending_approval,
        )
    return RunResource.from_row(result.run)


@router.get("/{run_id}/trace", response_model=TraceResponse)
async def get_run_trace(
    run_id: uuid.UUID,
    since_seq: int = Query(
        default=1, ge=0, description="Inclusive sequence start cursor (seq >= since_seq)."
    ),
    limit: int = Query(default=100, ge=1, le=500, description="Max events to return per page."),
    kind: list[TraceEventKind] | None = Query(
        default=None, description="Filter by event kind (repeatable)."
    ),
    severity_min: TraceEventSeverity | None = Query(
        default=None, description="Filter by minimum severity level."
    ),
    service: RunService = Depends(get_run_service),
    _auth: None = Depends(require_authorization),
) -> TraceResponse:
    """Retrieve paginated execution trace events ordered strictly by monotonic seq (§13.4)."""
    result = await service.get_run_trace(
        run_id,
        since_seq=since_seq,
        limit=limit,
        kinds=kind,
        severity_min=severity_min,
    )
    return TraceResponse(
        run_id=result.run_id,
        events=[TraceEventResource.from_row(e) for e in result.events],
        next_seq=result.next_seq,
        complete=result.complete,
    )


@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: uuid.UUID,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    since_seq: int | None = Query(
        default=None, ge=0, description="Fallback starting sequence if Last-Event-ID is absent."
    ),
    service: RunService = Depends(get_run_service),
    _auth: None = Depends(require_authorization),
) -> EventSourceResponse:
    """Stream live trace events via Server-Sent Events (SSE) with Last-Event-ID replay (§13.4)."""
    start_seq = 1
    if last_event_id is not None and last_event_id.strip():
        try:
            parsed_id = int(last_event_id.strip())
            if parsed_id < 0:
                raise InputValidationError("Last-Event-ID must be a non-negative integer")
            start_seq = parsed_id + 1
        except ValueError as exc:
            raise InputValidationError(
                f"Invalid Last-Event-ID '{last_event_id}': must be an integer"
            ) from exc
    elif since_seq is not None:
        start_seq = since_seq

    await service.verify_run_exists(run_id)

    event_generator = service.stream_run_events(run_id, start_seq=start_seq, request=request)
    return EventSourceResponse(
        event_generator,
        ping=15,
        ping_message_factory=lambda: ServerSentEvent(comment="keepalive"),
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
