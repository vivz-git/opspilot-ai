"""API-003: Run Trace Retrieval & Live Event Streaming tests (§13.4, §14).

Covers:
REST TRACE:
1. basic successful retrieval (200 OK, envelope, schema)
2. strict monotonic seq ordering
3. pagination with limit + 1 behavior and next_seq
4. complete flag behavior (false on active, true on terminal after final page)
5. empty trace on existing run (200 OK, events=[], next_seq=1, complete=false)
6. since_seq cursor progression
7. kind filtering (repeatable query params)
8. severity_min filtering (hierarchical rank filtering)
9. combined filters (kind + severity_min)
10. unknown run -> 404 Problem Details (code="not_found")
11. invalid parameters -> 422 Problem Details (code="validation_error")
12. limit bounds (ge=1, le=500) -> 422
13. malformed UUID -> 422
14. /runs/... and /api/v1/runs/... path parity
15. internal TraceEvent.id is NEVER exposed in public resource
16. input/output/error/payload presentation redaction
17. detached serialization does not mutate stored ORM object

SSE STREAMING:
18. successful SSE response with text/event-stream content type
19. event id equals durable trace seq (never DB surrogate id)
20. event name equals trace kind
21. payload formatting (valid JSON TraceEventResource)
22. Last-Event-ID replay (resumes at seq >= Last-Event-ID + 1)
23. since_seq fallback (resumes at seq >= since_seq)
24. Last-Event-ID takes precedence over since_seq
25. invalid Last-Event-ID -> 422 Problem Details
26. terminal event closes stream
27. already-terminal run replays and closes cleanly without hanging
28. heartbeat / keepalive (: keepalive comment)
29. client disconnect handling
30. multiple subscribers independent streaming
31. authorization behavior
32. unknown run behavior on SSE -> 404 Problem Details
33. SSE path parity (/runs and /api/v1/runs)

REAL POSTGRESQL INTEGRATION:
34. concurrent trace append while REST paging (zero duplicates, zero skips)
35. SSE reconnect with Last-Event-ID while active writers append events
36. already-terminal run SSE replay under real PostgreSQL
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.agent.state import RunStatus
from app.api.dependencies import get_run_service
from app.api.schemas import TraceEventResource
from app.config import AuthMode, Settings
from app.errors import NotFoundError
from app.execution.runs import RunService, RunTraceResult
from app.main import create_app
from app.persistence.models import (
    AgentRun,
    ToolName,
    TraceEvent,
    TraceEventKind,
    TraceEventSeverity,
)
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine
from sse_starlette.event import ServerSentEvent

from recovery_harness import (
    make_engine,
    migrate_to_head,
    require_database,
    uow_factory_for,
)
from recovery_harness import (
    settings as harness_settings,
)

pytestmark = [pytest.mark.unit]


def _make_sample_trace_event(
    run_id: uuid.UUID,
    seq: int,
    *,
    kind: TraceEventKind = TraceEventKind.NODE_ENTERED,
    severity: TraceEventSeverity = TraceEventSeverity.INFO,
    node: str | None = "execute_tool",
    tool: ToolName | None = "send_email_mock",
    step_id: str | None = "s1",
    attempt: int | None = 1,
    input_: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    status: str | None = "succeeded",
    duration_ms: int | None = 120,
    retry_count: int | None = 0,
    error: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    ts: datetime | None = None,
) -> TraceEvent:
    event = TraceEvent(
        run_id=run_id,
        seq=seq,
        kind=kind,
        severity=severity,
        node=node,
        tool=tool,
        step_id=step_id,
        attempt=attempt,
        input=input_ if input_ is not None else {},
        output=output,
        status=status,
        duration_ms=duration_ms,
        retry_count=retry_count,
        error=error,
        payload=payload if payload is not None else {},
        ts=ts or datetime(2026, 9, 16, 12, 0, seq, tzinfo=UTC),
    )
    event.id = 1000 + seq
    return event


# ---------------------------------------------------------------------------
# 1. REST Trace Retrieval Tests
# ---------------------------------------------------------------------------
class TestApiTraceRetrieval:
    def test_get_trace_success_and_envelope(self) -> None:
        run_id = uuid.uuid4()
        events = [
            _make_sample_trace_event(run_id, 1, kind=TraceEventKind.RUN_CREATED),
            _make_sample_trace_event(run_id, 2, kind=TraceEventKind.NODE_ENTERED, node="plan"),
        ]
        mock_service = AsyncMock(spec=RunService)
        mock_service.get_run_trace.return_value = RunTraceResult(
            run_id=run_id,
            events=events,
            next_seq=3,
            complete=False,
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_id}/trace")
            assert resp.status_code == 200
            data = resp.json()
            assert data["run_id"] == str(run_id)
            assert len(data["events"]) == 2
            assert data["events"][0]["seq"] == 1
            assert data["events"][0]["kind"] == "run_created"
            assert data["events"][1]["seq"] == 2
            assert data["events"][1]["node"] == "plan"
            assert data["next_seq"] == 3
            assert data["complete"] is False

    def test_get_trace_seq_ordering(self) -> None:
        run_id = uuid.uuid4()
        events = [
            _make_sample_trace_event(run_id, 1),
            _make_sample_trace_event(run_id, 2),
            _make_sample_trace_event(run_id, 3),
        ]
        mock_service = AsyncMock(spec=RunService)
        mock_service.get_run_trace.return_value = RunTraceResult(
            run_id=run_id,
            events=events,
            next_seq=4,
            complete=False,
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_id}/trace")
            assert resp.status_code == 200
            data = resp.json()
            seqs = [e["seq"] for e in data["events"]]
            assert seqs == [1, 2, 3]

    def test_get_trace_empty_run_returns_200(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)
        mock_service.get_run_trace.return_value = RunTraceResult(
            run_id=run_id,
            events=[],
            next_seq=1,
            complete=False,
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_id}/trace")
            assert resp.status_code == 200
            data = resp.json()
            assert data["run_id"] == str(run_id)
            assert data["events"] == []
            assert data["next_seq"] == 1
            assert data["complete"] is False

    def test_get_trace_unknown_run_404(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)
        mock_service.get_run_trace.side_effect = NotFoundError(f"Run {run_id} not found")

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_id}/trace")
            assert resp.status_code == 404
            assert resp.headers["content-type"] == "application/problem+json"
            data = resp.json()
            assert data["code"] == "not_found"
            assert f"Run {run_id} not found" in data["detail"]

    def test_get_trace_pagination_multiple_pages(self) -> None:
        run_id = uuid.uuid4()
        events_p1 = [
            _make_sample_trace_event(run_id, 1),
            _make_sample_trace_event(run_id, 2),
        ]
        events_p2 = [
            _make_sample_trace_event(run_id, 3),
            _make_sample_trace_event(run_id, 4),
        ]
        mock_service = AsyncMock(spec=RunService)

        def _mock_get_trace(
            rid: uuid.UUID, *, since_seq: int = 1, limit: int = 100, **kwargs: Any
        ) -> RunTraceResult:
            if since_seq == 1:
                return RunTraceResult(run_id=rid, events=events_p1, next_seq=3, complete=False)
            if since_seq == 3:
                return RunTraceResult(run_id=rid, events=events_p2, next_seq=5, complete=True)
            return RunTraceResult(run_id=rid, events=[], next_seq=None, complete=True)

        mock_service.get_run_trace.side_effect = _mock_get_trace

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp1 = client.get(f"/runs/{run_id}/trace?since_seq=1&limit=2")
            assert resp1.status_code == 200
            data1 = resp1.json()
            assert [e["seq"] for e in data1["events"]] == [1, 2]
            assert data1["next_seq"] == 3
            assert data1["complete"] is False

            resp2 = client.get(f"/runs/{run_id}/trace?since_seq=3&limit=2")
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert [e["seq"] for e in data2["events"]] == [3, 4]
            assert data2["next_seq"] == 5
            assert data2["complete"] is True

    def test_get_trace_no_duplicate_events(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)
        mock_service.get_run_trace.return_value = RunTraceResult(
            run_id=run_id,
            events=[_make_sample_trace_event(run_id, 1), _make_sample_trace_event(run_id, 2)],
            next_seq=3,
            complete=False,
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_id}/trace")
            seqs = [e["seq"] for e in resp.json()["events"]]
            assert len(seqs) == len(set(seqs))

    def test_get_trace_kind_filtering(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)
        mock_service.get_run_trace.return_value = RunTraceResult(
            run_id=run_id,
            events=[_make_sample_trace_event(run_id, 2, kind=TraceEventKind.TOOL_FAILED)],
            next_seq=3,
            complete=False,
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_id}/trace?kind=tool_failed&kind=tool_succeeded")
            assert resp.status_code == 200
            mock_service.get_run_trace.assert_awaited_once_with(
                run_id,
                since_seq=1,
                limit=100,
                kinds=[TraceEventKind.TOOL_FAILED, TraceEventKind.TOOL_SUCCEEDED],
                severity_min=None,
            )

    def test_get_trace_severity_filtering(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)
        mock_service.get_run_trace.return_value = RunTraceResult(
            run_id=run_id,
            events=[_make_sample_trace_event(run_id, 2, severity=TraceEventSeverity.WARNING)],
            next_seq=3,
            complete=False,
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_id}/trace?severity_min=warning")
            assert resp.status_code == 200
            mock_service.get_run_trace.assert_awaited_once_with(
                run_id,
                since_seq=1,
                limit=100,
                kinds=None,
                severity_min=TraceEventSeverity.WARNING,
            )

    def test_get_trace_combined_filters(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)
        mock_service.get_run_trace.return_value = RunTraceResult(
            run_id=run_id,
            events=[],
            next_seq=1,
            complete=False,
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_id}/trace?kind=policy_violation&severity_min=error")
            assert resp.status_code == 200
            mock_service.get_run_trace.assert_awaited_once_with(
                run_id,
                since_seq=1,
                limit=100,
                kinds=[TraceEventKind.POLICY_VIOLATION],
                severity_min=TraceEventSeverity.ERROR,
            )

    def test_get_trace_invalid_since_seq_422(self) -> None:
        run_id = uuid.uuid4()
        app = create_app()
        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_id}/trace?since_seq=-1")
            assert resp.status_code == 422
            assert resp.headers["content-type"] == "application/problem+json"
            assert resp.json()["code"] == "validation_error"

    def test_get_trace_invalid_limit_bounds_422(self) -> None:
        run_id = uuid.uuid4()
        app = create_app()
        with TestClient(app) as client:
            resp_zero = client.get(f"/runs/{run_id}/trace?limit=0")
            assert resp_zero.status_code == 422

            resp_over = client.get(f"/runs/{run_id}/trace?limit=501")
            assert resp_over.status_code == 422

    def test_get_trace_malformed_uuid_422(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/runs/not-a-valid-uuid/trace")
            assert resp.status_code == 422
            assert resp.json()["code"] == "validation_error"

    def test_get_trace_sensitive_payload_redaction(self) -> None:
        run_id = uuid.uuid4()
        sensitive_payload = {
            "api_key": "secret_key_12345",
            "Authorization": "Bearer sk-ant-api03-abcdef123456",
            "note": "call with bearer token",
        }
        sensitive_input = {
            "token": "ghp_1234567890abcdef",
            "safe_param": "regular_value",
        }
        event = _make_sample_trace_event(
            run_id,
            1,
            payload=sensitive_payload,
            input_=sensitive_input,
        )
        mock_service = AsyncMock(spec=RunService)
        mock_service.get_run_trace.return_value = RunTraceResult(
            run_id=run_id,
            events=[event],
            next_seq=2,
            complete=False,
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_id}/trace")
            assert resp.status_code == 200
            event_res = resp.json()["events"][0]
            assert event_res["payload"]["api_key"] == "[redacted]"
            assert event_res["payload"]["Authorization"] == "[redacted]"
            assert event_res["input"]["token"] == "[redacted]"  # noqa: S105
            assert event_res["input"]["safe_param"] == "regular_value"

    def test_get_trace_never_exposes_db_surrogate_id(self) -> None:
        run_id = uuid.uuid4()
        event = _make_sample_trace_event(run_id, 1)
        assert hasattr(event, "id")
        assert event.id == 1001

        mock_service = AsyncMock(spec=RunService)
        mock_service.get_run_trace.return_value = RunTraceResult(
            run_id=run_id,
            events=[event],
            next_seq=2,
            complete=False,
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_id}/trace")
            assert resp.status_code == 200
            event_res = resp.json()["events"][0]
            assert "id" not in event_res
            assert event_res["seq"] == 1

    def test_get_trace_detached_serialization_does_not_mutate_orm_object(self) -> None:
        run_id = uuid.uuid4()
        orig_payload = {"secret": "super_secret_value", "data": "hello"}
        event = _make_sample_trace_event(run_id, 1, payload=orig_payload)

        resource = TraceEventResource.from_row(event)
        assert resource.payload["secret"] == "[redacted]"  # noqa: S105
        # Underlying ORM object's payload dict must remain completely untouched
        assert event.payload["secret"] == "super_secret_value"  # noqa: S105

    def test_get_trace_active_run_complete_false(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)
        mock_service.get_run_trace.return_value = RunTraceResult(
            run_id=run_id,
            events=[_make_sample_trace_event(run_id, 1)],
            next_seq=2,
            complete=False,
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_id}/trace")
            assert resp.json()["complete"] is False
            assert resp.json()["next_seq"] == 2

    def test_get_trace_terminal_run_complete_true(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)
        mock_service.get_run_trace.return_value = RunTraceResult(
            run_id=run_id,
            events=[_make_sample_trace_event(run_id, 1, kind=TraceEventKind.RUN_COMPLETED)],
            next_seq=None,
            complete=True,
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_id}/trace")
            assert resp.json()["complete"] is True
            assert resp.json()["next_seq"] is None

    def test_get_trace_path_parity(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)
        mock_service.get_run_trace.return_value = RunTraceResult(
            run_id=run_id,
            events=[_make_sample_trace_event(run_id, 1)],
            next_seq=2,
            complete=False,
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp_bare = client.get(f"/runs/{run_id}/trace")
            resp_v1 = client.get(f"/api/v1/runs/{run_id}/trace")
            assert resp_bare.status_code == resp_v1.status_code == 200
            assert resp_bare.json() == resp_v1.json()


# ---------------------------------------------------------------------------
# 2. SSE Streaming Tests
# ---------------------------------------------------------------------------
class TestApiTraceSSE:
    @pytest.mark.asyncio
    async def test_sse_successful_stream_and_content_type(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)

        async def _mock_events(*args: Any, **kwargs: Any) -> AsyncIterator[ServerSentEvent]:
            yield ServerSentEvent(
                id="1",
                event="run_created",
                data='{"seq": 1, "kind": "run_created"}',
            )

        mock_service.stream_run_events.side_effect = _mock_events
        mock_service.verify_run_exists.return_value = None

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as client,
            client.stream("GET", f"/runs/{run_id}/events") as resp,
        ):
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            lines = [line async for line in resp.aiter_lines()]
            assert "id: 1" in lines
            assert "event: run_created" in lines

    @pytest.mark.asyncio
    async def test_sse_event_format_and_id_equals_seq(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)

        async def _mock_events(*args: Any, **kwargs: Any) -> AsyncIterator[ServerSentEvent]:
            yield ServerSentEvent(
                id="42",
                event="tool_succeeded",
                data='{"seq": 42, "kind": "tool_succeeded", "status": "succeeded"}',
            )

        mock_service.stream_run_events.side_effect = _mock_events
        mock_service.verify_run_exists.return_value = None

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as client,
            client.stream("GET", f"/runs/{run_id}/events") as resp,
        ):
            lines = [line async for line in resp.aiter_lines()]
            assert "id: 42" in lines
            assert "event: tool_succeeded" in lines
            # Never exposes DB surrogate id (e.g. 1042)
            assert not any(line.startswith("id: 10") for line in lines)

    @pytest.mark.asyncio
    async def test_sse_last_event_id_replay(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)
        recorded_start_seq: int | None = None

        async def _mock_events(
            rid: uuid.UUID, *, start_seq: int = 1, **kwargs: Any
        ) -> AsyncIterator[ServerSentEvent]:
            nonlocal recorded_start_seq
            recorded_start_seq = start_seq
            yield ServerSentEvent(id=str(start_seq), event="node_entered", data="{}")

        mock_service.stream_run_events.side_effect = _mock_events
        mock_service.verify_run_exists.return_value = None

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as client,
            client.stream("GET", f"/runs/{run_id}/events", headers={"Last-Event-ID": "20"}),
        ):
            pass

        assert recorded_start_seq == 21

    @pytest.mark.asyncio
    async def test_sse_since_seq_fallback(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)
        recorded_start_seq: int | None = None

        async def _mock_events(
            rid: uuid.UUID, *, start_seq: int = 1, **kwargs: Any
        ) -> AsyncIterator[ServerSentEvent]:
            nonlocal recorded_start_seq
            recorded_start_seq = start_seq
            yield ServerSentEvent(id=str(start_seq), event="node_entered", data="{}")

        mock_service.stream_run_events.side_effect = _mock_events
        mock_service.verify_run_exists.return_value = None

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as client,
            client.stream("GET", f"/runs/{run_id}/events?since_seq=15"),
        ):
            pass

        assert recorded_start_seq == 15

    @pytest.mark.asyncio
    async def test_sse_last_event_id_takes_precedence_over_since_seq(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)
        recorded_start_seq: int | None = None

        async def _mock_events(
            rid: uuid.UUID, *, start_seq: int = 1, **kwargs: Any
        ) -> AsyncIterator[ServerSentEvent]:
            nonlocal recorded_start_seq
            recorded_start_seq = start_seq
            yield ServerSentEvent(id=str(start_seq), event="node_entered", data="{}")

        mock_service.stream_run_events.side_effect = _mock_events
        mock_service.verify_run_exists.return_value = None

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as client,
            client.stream(
                "GET", f"/runs/{run_id}/events?since_seq=5", headers={"Last-Event-ID": "25"}
            ),
        ):
            pass

        # Last-Event-ID: 25 -> starts from 26, ignoring since_seq=5
        assert recorded_start_seq == 26

    @pytest.mark.asyncio
    async def test_sse_invalid_last_event_id_422(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)
        mock_service.verify_run_exists.return_value = None

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp_alpha = await client.get(
                f"/runs/{run_id}/events", headers={"Last-Event-ID": "invalid"}
            )
            assert resp_alpha.status_code == 422
            assert resp_alpha.json()["code"] == "validation_error"

            resp_neg = await client.get(f"/runs/{run_id}/events", headers={"Last-Event-ID": "-5"})
            assert resp_neg.status_code == 422
            assert resp_neg.json()["code"] == "validation_error"

    @pytest.mark.asyncio
    async def test_sse_terminal_event_closes_stream(self) -> None:
        run_id = uuid.uuid4()
        event_completed = _make_sample_trace_event(run_id, 1, kind=TraceEventKind.RUN_COMPLETED)

        # Build real RunService with in-memory UOW mock to test stream termination logic
        mock_uow = AsyncMock()
        mock_uow.trace_events.list_by_run.side_effect = [
            [event_completed],
            [],
        ]
        mock_run = AgentRun(
            id=run_id,
            status=RunStatus.COMPLETED,
            user_request="test",
            planner_kind="rules",
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        mock_uow.agent_runs.get.return_value = mock_run

        class FakeContextManager:
            async def __aenter__(self) -> Any:
                return mock_uow

            async def __aexit__(self, *args: Any) -> None:
                pass

        service = RunService(
            uow_factory=lambda: FakeContextManager(),
            settings=harness_settings(),
        )

        app = create_app(run_service=service)
        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as client,
            client.stream("GET", f"/runs/{run_id}/events") as resp,
        ):
            assert resp.status_code == 200
            lines = [line async for line in resp.aiter_lines()]
            assert "event: run_completed" in lines

    @pytest.mark.asyncio
    async def test_sse_already_terminal_run_closes_cleanly(self) -> None:
        run_id = uuid.uuid4()
        event1 = _make_sample_trace_event(run_id, 1, kind=TraceEventKind.RUN_CREATED)
        event2 = _make_sample_trace_event(run_id, 2, kind=TraceEventKind.RUN_COMPLETED)

        mock_uow = AsyncMock()
        mock_uow.trace_events.list_by_run.return_value = [event1, event2]
        mock_run = AgentRun(
            id=run_id,
            status=RunStatus.COMPLETED,
            user_request="test",
            planner_kind="rules",
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        mock_uow.agent_runs.get.return_value = mock_run

        class FakeContextManager:
            async def __aenter__(self) -> Any:
                return mock_uow

            async def __aexit__(self, *args: Any) -> None:
                pass

        service = RunService(
            uow_factory=lambda: FakeContextManager(),
            settings=harness_settings(),
        )

        app = create_app(run_service=service)
        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as client,
            client.stream("GET", f"/runs/{run_id}/events") as resp,
        ):
            assert resp.status_code == 200
            lines = [line async for line in resp.aiter_lines()]
            assert "event: run_created" in lines
            assert "event: run_completed" in lines

    @pytest.mark.asyncio
    async def test_sse_heartbeat_keepalive(self) -> None:
        from app.api.runs import ServerSentEvent

        def factory() -> ServerSentEvent:
            return ServerSentEvent(comment="keepalive")

        event = factory()
        assert event.comment == "keepalive"
        encoded = event.encode()
        assert b": keepalive" in encoded

    @pytest.mark.asyncio
    async def test_sse_client_disconnect(self) -> None:
        run_id = uuid.uuid4()
        mock_request = AsyncMock()
        mock_request.is_disconnected.return_value = True

        service = RunService(
            uow_factory=AsyncMock(),
            settings=harness_settings(),
        )

        gen = service.stream_run_events(run_id, start_seq=1, request=mock_request)
        events = [e async for e in gen]
        assert events == []

    @pytest.mark.asyncio
    async def test_sse_multiple_subscribers(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)

        async def _mock_events(
            rid: uuid.UUID, *, start_seq: int = 1, **kwargs: Any
        ) -> AsyncIterator[ServerSentEvent]:
            yield ServerSentEvent(id=str(start_seq), event="node_entered", data="{}")

        mock_service.stream_run_events.side_effect = _mock_events
        mock_service.verify_run_exists.return_value = None

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:

            async def _stream(last_id: str) -> list[str]:
                async with client.stream(
                    "GET", f"/runs/{run_id}/events", headers={"Last-Event-ID": last_id}
                ) as resp:
                    return [line async for line in resp.aiter_lines()]

            lines1, lines2 = await asyncio.gather(_stream("5"), _stream("10"))
            assert "id: 6" in lines1
            assert "id: 11" in lines2

    @pytest.mark.asyncio
    async def test_sse_authorization_behavior(self) -> None:
        run_id = uuid.uuid4()
        settings = Settings(_env_file=None, OPSPILOT_AUTH_MODE=AuthMode.PROXY)
        app = create_app(settings=settings)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/runs/{run_id}/events")
            assert resp.status_code == 401
            assert resp.json()["code"] == "policy_violation"

    @pytest.mark.asyncio
    async def test_sse_unknown_run_404(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)
        mock_service.verify_run_exists.side_effect = NotFoundError(f"Run {run_id} not found")

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/runs/{run_id}/events")
            assert resp.status_code == 404
            assert resp.headers["content-type"] == "application/problem+json"
            assert resp.json()["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_sse_path_parity(self) -> None:
        run_id = uuid.uuid4()
        mock_service = AsyncMock(spec=RunService)

        async def _mock_events(*args: Any, **kwargs: Any) -> AsyncIterator[ServerSentEvent]:
            yield ServerSentEvent(id="1", event="run_created", data="{}")

        mock_service.stream_run_events.side_effect = _mock_events
        mock_service.verify_run_exists.return_value = None

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            async with client.stream("GET", f"/runs/{run_id}/events") as resp_bare:
                lines_bare = [line async for line in resp_bare.aiter_lines()]
            async with client.stream("GET", f"/api/v1/runs/{run_id}/events") as resp_v1:
                lines_v1 = [line async for line in resp_v1.aiter_lines()]

            assert lines_bare == lines_v1


# ---------------------------------------------------------------------------
# 3. Real PostgreSQL Integration Tests
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestApiTracePostgresIntegration:
    @pytest.fixture(scope="class")
    @classmethod
    def _database(cls) -> None:
        require_database()
        migrate_to_head()

    @pytest.fixture
    async def engine(self, _database: None) -> AsyncIterator[AsyncEngine]:
        eng = await make_engine()
        try:
            yield eng
        finally:
            await eng.dispose()

    async def test_concurrent_trace_append_while_rest_paging(
        self, _database: None, engine: AsyncEngine
    ) -> None:
        """Paginating traces while writers append new events: zero duplicates, zero skips."""
        uow_factory = uow_factory_for(engine)
        settings = harness_settings()
        service = RunService(uow_factory=uow_factory, settings=settings)

        # 1. Create run (commits canonical seq=1 RUN_CREATED event)
        res = await service.create_run("Concurrent trace pagination test")
        run_id = res.run.id

        # 2. Append events seq 2, 3
        async with uow_factory() as uow:
            await uow.trace_events.append(
                run_id=run_id, kind=TraceEventKind.NODE_ENTERED, node="plan"
            )
            await uow.trace_events.append(
                run_id=run_id, kind=TraceEventKind.PLAN_CREATED, node="plan"
            )
            await uow.commit()

        # 3. Read page 1 (limit=2, since_seq=1) -> expects seq 1, 2
        p1 = await service.get_run_trace(run_id, since_seq=1, limit=2)
        assert [e.seq for e in p1.events] == [1, 2]
        assert p1.next_seq == 3
        assert p1.complete is False

        # 4. Concurrently, background worker commits events 4..6
        async with uow_factory() as uow:
            await uow.trace_events.append(
                run_id=run_id, kind=TraceEventKind.TOOL_STARTED, tool="send_email_mock"
            )
            await uow.trace_events.append(
                run_id=run_id, kind=TraceEventKind.TOOL_SUCCEEDED, tool="send_email_mock"
            )
            await uow.trace_events.append(run_id=run_id, kind=TraceEventKind.RUN_COMPLETED)
            run = await uow.agent_runs.get(run_id)
            assert run is not None
            run.status = RunStatus.COMPLETED
            await uow.commit()

        # 5. Read page 2 (since_seq=3, limit=2) -> expects seq 3, 4
        assert p1.next_seq is not None
        p2 = await service.get_run_trace(run_id, since_seq=p1.next_seq, limit=2)
        assert [e.seq for e in p2.events] == [3, 4]
        assert p2.next_seq == 5
        assert p2.complete is False

        # 6. Read page 3 (since_seq=5, limit=2) -> expects seq 5, 6, terminal completion
        assert p2.next_seq is not None
        p3 = await service.get_run_trace(run_id, since_seq=p2.next_seq, limit=2)
        assert [e.seq for e in p3.events] == [5, 6]
        assert p3.next_seq is None
        assert p3.complete is True

        all_seqs = [e.seq for e in p1.events + p2.events + p3.events]
        # Exact total ordering, zero duplicates, zero skips
        assert all_seqs == [1, 2, 3, 4, 5, 6]

    async def test_sse_reconnect_with_last_event_id_under_active_writers(
        self, _database: None, engine: AsyncEngine
    ) -> None:
        """Client reconnecting via Last-Event-ID while events are appended in Postgres."""
        uow_factory = uow_factory_for(engine)
        settings = harness_settings()
        service = RunService(uow_factory=uow_factory, settings=settings)

        res = await service.create_run("SSE reconnect test")
        run_id = res.run.id

        # 1. Write events 2, 3 (seq 1 was created by create_run)
        async with uow_factory() as uow:
            await uow.trace_events.append(
                run_id=run_id, kind=TraceEventKind.NODE_ENTERED, node="plan"
            )
            await uow.trace_events.append(
                run_id=run_id, kind=TraceEventKind.PLAN_CREATED, node="plan"
            )
            await uow.commit()

        # 2. First consumer reads seq 1 and disconnects
        gen1 = service.stream_run_events(run_id, start_seq=1)
        ev1 = await anext(gen1)
        assert ev1.id == "1"

        # 3. Active writer writes events 4, 5, 6
        async with uow_factory() as uow:
            await uow.trace_events.append(
                run_id=run_id, kind=TraceEventKind.TOOL_STARTED, tool="send_email_mock"
            )
            await uow.trace_events.append(
                run_id=run_id, kind=TraceEventKind.TOOL_SUCCEEDED, tool="send_email_mock"
            )
            await uow.trace_events.append(run_id=run_id, kind=TraceEventKind.RUN_COMPLETED)
            run = await uow.agent_runs.get(run_id)
            assert run is not None
            run.status = RunStatus.COMPLETED
            await uow.commit()

        # 4. Reconnect with Last-Event-ID: 1 -> resume at start_seq = 2
        gen2 = service.stream_run_events(run_id, start_seq=2)
        resumed_ids: list[str] = []
        async for sse_event in gen2:
            resumed_ids.append(str(sse_event.id))

        # Must receive 2, 3, 4, 5, 6 with no gaps and no duplicate of event 1
        assert resumed_ids == ["2", "3", "4", "5", "6"]

    async def test_already_terminal_run_sse_replay_in_postgres(
        self, _database: None, engine: AsyncEngine
    ) -> None:
        """SSE stream for already-completed run yields all events and terminates without hanging."""
        uow_factory = uow_factory_for(engine)
        settings = harness_settings()
        service = RunService(uow_factory=uow_factory, settings=settings)

        res = await service.create_run("Terminal run replay test")
        run_id = res.run.id

        async with uow_factory() as uow:
            await uow.trace_events.append(run_id=run_id, kind=TraceEventKind.RUN_COMPLETED)
            run = await uow.agent_runs.get(run_id)
            assert run is not None
            run.status = RunStatus.COMPLETED
            await uow.commit()

        gen = service.stream_run_events(run_id, start_seq=1)
        events: list[ServerSentEvent] = []
        async for ev in gen:
            events.append(ev)

        assert len(events) >= 1
        assert events[-1].event == TraceEventKind.RUN_COMPLETED.value
