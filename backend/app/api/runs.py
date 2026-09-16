"""Run trace retrieval and live event streaming (API-003, §13.4, §3.3, §14.3).

Exposes:
- `GET /runs/{run_id}/trace` (paginated, `since_seq` cursor)
- `GET /runs/{run_id}/events` (SSE, `Last-Event-ID` replay)

This is an API/control-plane observability feature over the *existing*
durable trace system (DB-002), not a new tracing system: every byte served
here comes from `TraceService` -> `TraceEventRepository.list_by_run`, the
same repository AGENT/TOOL/HITL code already appends to. The module is
strictly read-only — it never calls `ToolRegistry.dispatch`, mints an
`ApprovalToken`, touches a checkpoint, or writes a trace event merely
because a client is reading one — and never opens a socket, queue, or
process-local buffer of its own: SSE fan-out is independent PostgreSQL
polling per subscriber (§3.3), so one client's cursor can never affect
another's.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, Query, Request
from sse_starlette.sse import EventSourceResponse

from app.agent.state import TERMINAL_RUN_STATUSES
from app.api.dependencies import get_trace_service, require_authorization
from app.api.schemas import TraceEventResource, TracePageResponse
from app.errors import InputValidationError
from app.execution.trace import TERMINAL_TRACE_KINDS, TraceService
from app.persistence.models import TraceEvent, TraceEventKind, TraceEventSeverity

__all__ = ["router"]

router = APIRouter(prefix="/runs", tags=["runs"])

_log = structlog.get_logger("opspilot.api.trace")

#: §13.4 — `limit ≤ 500`.
_MAX_TRACE_LIMIT = 500
#: The gap between two PostgreSQL polls for one SSE subscriber (§3.3): short
#: enough to feel live, long enough not to hammer the database per client.
_SSE_POLL_INTERVAL_SECONDS = 0.5
#: `: keepalive` cadence (§13.4).
_SSE_HEARTBEAT_SECONDS = 15.0


def _parse_last_event_id(raw: str | None) -> int | None:
    """`Last-Event-ID` -> the durable `seq` it names, or `None` if absent.

    Malformed before any stream byte is written, so it is a normal RFC 9457
    `422`, never a mid-stream error.
    """
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise InputValidationError(f"Last-Event-ID must be an integer, got {raw!r}") from exc
    if value < 0:
        raise InputValidationError("Last-Event-ID must be >= 0")
    return value


@router.get("/{run_id}/trace", response_model=TracePageResponse)
async def get_trace(
    run_id: uuid.UUID,
    since_seq: int = Query(default=0, ge=0, description="First seq to return."),
    limit: int = Query(default=100, ge=1, le=_MAX_TRACE_LIMIT),
    kind: list[TraceEventKind] | None = Query(default=None, description="Repeatable kind filter."),
    severity_min: TraceEventSeverity | None = Query(default=None),
    service: TraceService = Depends(get_trace_service),
    _auth: None = Depends(require_authorization),
) -> TracePageResponse:
    """§13.4 — paginated trace retrieval over the durable `seq` cursor.

    Delegates entirely to `TraceService.get_page`; no ORM query is built
    here (§12, DB-005).
    """
    page = await service.get_page(
        run_id,
        since_seq=since_seq,
        limit=limit,
        kinds=kind,
        severity_min=severity_min,
    )
    return TracePageResponse(
        run_id=page.run_id,
        events=[
            TraceEventResource.from_row(event, max_bytes=service.payload_max_bytes)
            for event in page.events
        ],
        next_seq=page.next_seq,
        complete=page.complete,
    )


def _event_message(event: TraceEvent, *, max_bytes: int) -> dict[str, Any]:
    resource = TraceEventResource.from_row(event, max_bytes=max_bytes)
    return {
        "id": str(event.seq),
        "event": event.kind.value,
        "data": resource.model_dump_json(),
    }


async def iter_trace_events(
    service: TraceService,
    run_id: uuid.UUID,
    *,
    cursor: int,
    is_disconnected: Callable[[], Awaitable[bool]],
    poll_interval: float = _SSE_POLL_INTERVAL_SECONDS,
    heartbeat_seconds: float = _SSE_HEARTBEAT_SECONDS,
) -> AsyncIterator[dict[str, Any]]:
    """The SSE body, independent of any ASGI `Request` (§3.3, §13.4).

    Extracted from the route so it can be driven directly in tests with a
    fake `is_disconnected` and tiny `poll_interval`/`heartbeat_seconds`,
    without a real socket or a real 15-second wait. Each call polls
    PostgreSQL through `service` with its own `cursor` — no shared state
    between subscribers, no in-process queue or broadcaster.
    """
    loop = asyncio.get_event_loop()
    last_activity = loop.time()
    while True:
        if await is_disconnected():
            return
        try:
            events, status = await service.poll_since(run_id, after_seq=cursor)
        except Exception:
            # Transient DB/read failure after the stream is already open:
            # log with the existing structured logger, never leak SQL or a
            # stack trace to the client, close cleanly. This is never turned
            # into a trace event — reading a trace does not write one.
            _log.exception("trace_stream_read_failed", run_id=str(run_id))
            return

        for event in events:
            cursor = event.seq
            yield _event_message(event, max_bytes=service.payload_max_bytes)
            last_activity = loop.time()
            if event.kind in TERMINAL_TRACE_KINDS:
                return

        if events:
            continue

        if status in TERMINAL_RUN_STATUSES:
            # The run is terminal but its final trace event was not among
            # `events` above (§14.4's emitters commit the terminal status and
            # its event together, but a stream reader must not assume that
            # ordering). One more sweep before closing: if it is still not
            # there, the run is genuinely done without a terminal-kind event
            # to relay, and the stream ends rather than hanging forever.
            sweep_events, _ = await service.poll_since(run_id, after_seq=cursor)
            for event in sweep_events:
                cursor = event.seq
                yield _event_message(event, max_bytes=service.payload_max_bytes)
            return

        now = loop.time()
        if now - last_activity >= heartbeat_seconds:
            yield {"comment": "keepalive"}
            last_activity = now
        await asyncio.sleep(poll_interval)


@router.get("/{run_id}/events")
async def stream_events(
    request: Request,
    run_id: uuid.UUID,
    since_seq: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    service: TraceService = Depends(get_trace_service),
    _auth: None = Depends(require_authorization),
) -> EventSourceResponse:
    """§13.4/§3.3 — SSE trace stream, an optimization over polling that
    carries no unique data.

    Cursor precedence (never an off-by-one): `Last-Event-ID: 20` resumes
    strictly after 20 (`seq >= 21`, since the client already has 20);
    `since_seq=20` with no `Last-Event-ID` starts *at* 20 (`seq >= 20`,
    §13.4's "first seq to return"); neither given starts at `seq >= 1`.
    """
    last_id = _parse_last_event_id(last_event_id)
    cursor = last_id if last_id is not None else max(since_seq - 1, 0)

    # A 404 here is an ordinary RFC 9457 response — decided before any
    # `text/event-stream` byte is written, never a mid-stream error.
    await service.ensure_run_exists(run_id)

    return EventSourceResponse(
        iter_trace_events(
            service,
            run_id,
            cursor=cursor,
            is_disconnected=request.is_disconnected,
        )
    )
