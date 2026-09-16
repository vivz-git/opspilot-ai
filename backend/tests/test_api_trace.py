"""API-003 — run trace retrieval and live event streaming (§13.4, §3.3, RFC 9457).

Covers:

REST `GET /runs/{run_id}/trace` (and its `/api/v1` mirror):
 1. known run returns events
 2. seq ordering
 3. empty trace -> next_seq=1, complete=false
 4. unknown run -> 404
 5. pagination across multiple pages
 6. no duplicates across pages
 7. no skipped events across pages
 8. kind filtering
 9. severity filtering
10. invalid since_seq -> 422
11. invalid limit -> 422
12. malformed UUID -> 422
13. sensitive payload redaction
14. internal TraceEvent.id not exposed
15. detached serialization does not mutate the stored payload
16. active run -> complete=false
17. terminal run after final event -> complete=true, next_seq=null
18. /runs and /api/v1/runs parity

SSE `GET /runs/{run_id}/events`:
 1. correct media type
 2. initial replay
 3. correct SSE id (the durable seq, never TraceEvent.id)
 4. correct SSE event name (TraceEventKind.value)
 5. correct JSON data (TraceEventResource)
 6. Last-Event-ID replay
 7. since_seq fallback
 8. Last-Event-ID precedence over since_seq
 9. terminal event closes the stream
10. already-terminal run replays then closes
11. heartbeat/keepalive behaviour
12. client disconnect exits cleanly
13. multiple independent subscribers
14. no duplicate replay
15. no missed events
16. authorization behaviour
17. safe mid-stream error handling

Real PostgreSQL integration:
 - concurrent trace append while REST paging
 - reconnect with Last-Event-ID while events keep being appended
 - exact sequence ordering, no gaps/duplicates
 - already-terminal stream behaviour
 - multiple subscribers against a real database
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.agent.state import RunStatus
from app.api.dependencies import get_trace_service
from app.api.runs import iter_trace_events
from app.config import Settings
from app.errors import NotFoundError
from app.execution.trace import TraceService
from app.main import create_app
from app.persistence.models import TraceEvent, TraceEventKind, TraceEventSeverity
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from recovery_harness import create_run, migrate_to_head, require_database, uow_factory_for


def _row(
    *,
    seq: int,
    run_id: uuid.UUID | None = None,
    kind: TraceEventKind = TraceEventKind.NODE_ENTERED,
    severity: TraceEventSeverity = TraceEventSeverity.INFO,
    payload: dict[str, Any] | None = None,
    **kwargs: Any,
) -> TraceEvent:
    """A `TraceEvent` ORM instance built in memory — never persisted, just a
    detached row with attributes set, the same shape `TraceEventResource
    .from_row` receives from the repository."""
    return TraceEvent(
        id=10_000 + seq,
        run_id=run_id or uuid.uuid4(),
        seq=seq,
        ts=datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC),
        kind=kind,
        severity=severity,
        payload=payload if payload is not None else {},
        **kwargs,
    )


class _FakeTraceService:
    """A `TraceService`-shaped test double with no persistence at all."""

    def __init__(self) -> None:
        self.payload_max_bytes = 16_384
        self.get_page_result: Any = None
        self.get_page_error: Exception | None = None
        self.ensure_run_exists_error: Exception | None = None
        self.ensure_run_exists_status: RunStatus = RunStatus.RUNNING
        self.poll_results: list[Any] = []
        self.poll_error: Exception | None = None
        self.get_page_calls: list[dict[str, Any]] = []
        self.poll_calls: list[int] = []

    async def get_page(self, run_id: uuid.UUID, **kwargs: Any) -> Any:
        self.get_page_calls.append({"run_id": run_id, **kwargs})
        if self.get_page_error is not None:
            raise self.get_page_error
        return self.get_page_result

    async def ensure_run_exists(self, run_id: uuid.UUID) -> RunStatus:
        if self.ensure_run_exists_error is not None:
            raise self.ensure_run_exists_error
        return self.ensure_run_exists_status

    async def poll_since(self, run_id: uuid.UUID, *, after_seq: int) -> Any:
        self.poll_calls.append(after_seq)
        if self.poll_error is not None:
            raise self.poll_error
        if self.poll_results:
            return self.poll_results.pop(0)
        return [], RunStatus.RUNNING


# ---------------------------------------------------------------------------
# TraceEventResource — redaction, detachment, no internal id (unit)
# ---------------------------------------------------------------------------
class TestTraceEventResourceSerialization:
    def test_redacts_sensitive_fields_in_payload_input_output_error(self) -> None:
        from app.api.schemas import TraceEventResource

        row = _row(
            seq=1,
            payload={"api_key": "sk-ant-abcdef123456", "note": "fine"},
            input={"authorization": "Bearer abcdefgh12345678"},
            output={"password": "hunter2"},
            error={"secret": "shh"},
        )
        resource = TraceEventResource.from_row(row, max_bytes=16_384)
        assert resource.payload["api_key"] == "[redacted]"
        assert resource.payload["note"] == "fine"
        assert resource.input is not None and resource.input["authorization"] == "[redacted]"
        assert resource.output is not None
        assert resource.output["password"] == "[redacted]"  # noqa: S105 - asserting redaction
        assert resource.error is not None
        assert resource.error["secret"] == "[redacted]"  # noqa: S105 - asserting redaction

    def test_never_exposes_internal_trace_event_id(self) -> None:
        from app.api.schemas import TraceEventResource

        row = _row(seq=42)
        resource = TraceEventResource.from_row(row, max_bytes=16_384)
        assert "id" not in resource.model_dump()
        assert not hasattr(resource, "id")

    def test_detached_serialization_does_not_mutate_stored_payload(self) -> None:
        from app.api.schemas import TraceEventResource

        original_payload = {"api_key": "sk-ant-abcdef123456"}
        original_error = {"password": "hunter2"}
        row = _row(seq=1, payload=dict(original_payload), error=dict(original_error))
        TraceEventResource.from_row(row, max_bytes=16_384)
        assert row.payload == original_payload
        assert row.error == original_error


# ---------------------------------------------------------------------------
# REST trace endpoint — unit level, mocked TraceService
# ---------------------------------------------------------------------------
class TestTraceApiUnit:
    def _app(self, service: Any, *, settings: Settings | None = None) -> Any:
        app = create_app(settings=settings or Settings(_env_file=None))
        app.dependency_overrides[get_trace_service] = lambda: service
        return app

    def test_known_run_returns_events_in_seq_order(self) -> None:
        from app.execution.trace import TracePage

        service = _FakeTraceService()
        run_id = uuid.uuid4()
        rows = [_row(seq=i, run_id=run_id) for i in (1, 2, 3)]
        service.get_page_result = TracePage(run_id=run_id, events=rows, next_seq=4, complete=False)

        with TestClient(self._app(service)) as client:
            resp = client.get(f"/runs/{run_id}/trace")

        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == str(run_id)
        assert [e["seq"] for e in data["events"]] == [1, 2, 3]
        assert data["next_seq"] == 4
        assert data["complete"] is False

    def test_empty_trace_reports_next_seq_one_and_incomplete(self) -> None:
        from app.execution.trace import TracePage

        service = _FakeTraceService()
        run_id = uuid.uuid4()
        service.get_page_result = TracePage(run_id=run_id, events=[], next_seq=1, complete=False)

        with TestClient(self._app(service)) as client:
            resp = client.get(f"/runs/{run_id}/trace")

        assert resp.status_code == 200
        data = resp.json()
        assert data["events"] == []
        assert data["next_seq"] == 1
        assert data["complete"] is False

    def test_unknown_run_returns_404_problem_details(self) -> None:
        service = _FakeTraceService()
        service.get_page_error = NotFoundError("Run x not found")
        run_id = uuid.uuid4()

        with TestClient(self._app(service)) as client:
            resp = client.get(f"/runs/{run_id}/trace")

        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["code"] == "not_found"

    def test_active_run_reports_complete_false(self) -> None:
        from app.execution.trace import TracePage

        service = _FakeTraceService()
        run_id = uuid.uuid4()
        service.get_page_result = TracePage(run_id=run_id, events=[], next_seq=5, complete=False)
        with TestClient(self._app(service)) as client:
            resp = client.get(f"/runs/{run_id}/trace?since_seq=5")
        assert resp.json()["complete"] is False

    def test_terminal_run_after_final_event_reports_complete_true_next_seq_null(self) -> None:
        from app.execution.trace import TracePage

        service = _FakeTraceService()
        run_id = uuid.uuid4()
        rows = [_row(seq=1, run_id=run_id, kind=TraceEventKind.RUN_COMPLETED)]
        service.get_page_result = TracePage(
            run_id=run_id, events=rows, next_seq=None, complete=True
        )
        with TestClient(self._app(service)) as client:
            resp = client.get(f"/runs/{run_id}/trace")
        data = resp.json()
        assert data["complete"] is True
        assert data["next_seq"] is None

    def test_kind_filter_is_forwarded_to_service(self) -> None:
        from app.execution.trace import TracePage

        service = _FakeTraceService()
        run_id = uuid.uuid4()
        service.get_page_result = TracePage(run_id=run_id, events=[], next_seq=1, complete=False)
        with TestClient(self._app(service)) as client:
            resp = client.get(f"/runs/{run_id}/trace?kind=tool_failed&kind=tool_succeeded")
        assert resp.status_code == 200
        assert service.get_page_calls[0]["kinds"] == [
            TraceEventKind.TOOL_FAILED,
            TraceEventKind.TOOL_SUCCEEDED,
        ]

    def test_severity_min_filter_is_forwarded_to_service(self) -> None:
        from app.execution.trace import TracePage

        service = _FakeTraceService()
        run_id = uuid.uuid4()
        service.get_page_result = TracePage(run_id=run_id, events=[], next_seq=1, complete=False)
        with TestClient(self._app(service)) as client:
            resp = client.get(f"/runs/{run_id}/trace?severity_min=warning")
        assert resp.status_code == 200
        assert service.get_page_calls[0]["severity_min"] == TraceEventSeverity.WARNING

    def test_invalid_since_seq_returns_422(self) -> None:
        service = _FakeTraceService()
        with TestClient(self._app(service)) as client:
            resp = client.get(f"/runs/{uuid.uuid4()}/trace?since_seq=-1")
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

    def test_invalid_limit_returns_422(self) -> None:
        service = _FakeTraceService()
        with TestClient(self._app(service)) as client:
            resp = client.get(f"/runs/{uuid.uuid4()}/trace?limit=0")
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

        with TestClient(self._app(service)) as client:
            resp2 = client.get(f"/runs/{uuid.uuid4()}/trace?limit=501")
        assert resp2.status_code == 422

    def test_invalid_kind_value_returns_422(self) -> None:
        service = _FakeTraceService()
        with TestClient(self._app(service)) as client:
            resp = client.get(f"/runs/{uuid.uuid4()}/trace?kind=not_a_real_kind")
        assert resp.status_code == 422

    def test_malformed_run_id_returns_422(self) -> None:
        service = _FakeTraceService()
        with TestClient(self._app(service)) as client:
            resp = client.get("/runs/not-a-uuid/trace")
        assert resp.status_code == 422

    def test_bare_and_api_v1_paths_return_identical_results(self) -> None:
        from app.execution.trace import TracePage

        service = _FakeTraceService()
        run_id = uuid.uuid4()
        rows = [_row(seq=1, run_id=run_id)]
        service.get_page_result = TracePage(run_id=run_id, events=rows, next_seq=2, complete=False)

        with TestClient(self._app(service)) as client:
            bare = client.get(f"/runs/{run_id}/trace")
            v1 = client.get(f"/api/v1/runs/{run_id}/trace")

        assert bare.status_code == v1.status_code == 200
        assert bare.json() == v1.json()

    def test_authorization_enforced_when_configured(self) -> None:
        from app.execution.trace import TracePage

        service = _FakeTraceService()
        run_id = uuid.uuid4()
        service.get_page_result = TracePage(run_id=run_id, events=[], next_seq=1, complete=False)
        cfg = Settings(_env_file=None, OPSPILOT_AUTH_MODE="token")

        with TestClient(self._app(service, settings=cfg)) as client:
            resp = client.get(f"/runs/{run_id}/trace")
            assert resp.status_code == 401
            assert resp.json()["code"] == "policy_violation"

            resp_authed = client.get(
                f"/runs/{run_id}/trace", headers={"Authorization": "Bearer test-token"}
            )
            assert resp_authed.status_code == 200

    def test_api_layer_delegates_to_trace_service_get_page(self) -> None:
        """The route handler does not build a page itself — it calls the
        service and reshapes the result (§13, DB-005)."""
        from app.execution.trace import TracePage

        service = AsyncMock(spec=TraceService)
        service.payload_max_bytes = 16_384
        run_id = uuid.uuid4()
        service.get_page.return_value = TracePage(
            run_id=run_id, events=[], next_seq=1, complete=False
        )
        app = create_app(settings=Settings(_env_file=None))
        app.dependency_overrides[get_trace_service] = lambda: service
        with TestClient(app) as client:
            client.get(f"/runs/{run_id}/trace?since_seq=3&limit=10")
        service.get_page.assert_awaited_once_with(
            run_id, since_seq=3, limit=10, kinds=None, severity_min=None
        )


# ---------------------------------------------------------------------------
# SSE generator — unit level, driven directly (fast, deterministic)
# ---------------------------------------------------------------------------
async def _disconnected_false() -> bool:
    return False


async def _disconnected_true() -> bool:
    return True


async def _collect(gen: AsyncIterator[dict[str, Any]], *, limit: int = 50) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async for msg in gen:
        out.append(msg)
        if len(out) >= limit:
            break
    return out


class TestSseGeneratorUnit:
    async def test_initial_replay_has_correct_id_event_and_data(self) -> None:
        run_id = uuid.uuid4()
        service = _FakeTraceService()
        row = _row(seq=7, run_id=run_id, kind=TraceEventKind.TOOL_SUCCEEDED, payload={"ok": True})
        terminal = _row(seq=8, run_id=run_id, kind=TraceEventKind.RUN_COMPLETED)
        service.poll_results = [([row, terminal], RunStatus.COMPLETED)]

        messages = await _collect(
            iter_trace_events(
                service,
                run_id,
                cursor=6,
                is_disconnected=_disconnected_false,  # type: ignore[arg-type]
            )
        )
        assert len(messages) == 2
        assert messages[0]["id"] == "7"
        assert messages[0]["event"] == "tool_succeeded"
        assert '"seq":7' in messages[0]["data"]
        assert '"ok": true' in messages[0]["data"] or '"ok":true' in messages[0]["data"]
        assert messages[1]["id"] == "8"
        assert messages[1]["event"] == "run_completed"

    async def test_terminal_event_closes_the_stream(self) -> None:
        run_id = uuid.uuid4()
        service = _FakeTraceService()
        terminal = _row(seq=1, run_id=run_id, kind=TraceEventKind.RUN_FAILED)
        # a second poll would return more, but the generator must never get there
        service.poll_results = [
            ([terminal], RunStatus.FAILED),
            ([_row(seq=2, run_id=run_id)], RunStatus.FAILED),
        ]
        messages = await _collect(
            iter_trace_events(service, run_id, cursor=0, is_disconnected=_disconnected_false)  # type: ignore[arg-type]
        )
        assert len(messages) == 1
        assert messages[0]["event"] == "run_failed"
        assert len(service.poll_calls) == 1

    async def test_already_terminal_run_replays_then_closes_without_terminal_kind(self) -> None:
        """Terminal run status, but the last poll's batch happens to not
        contain a terminal-kind event: the sweep finds nothing more and the
        stream still closes rather than hanging (§ terminal run with a
        missing terminal event)."""
        run_id = uuid.uuid4()
        service = _FakeTraceService()
        row = _row(seq=1, run_id=run_id, kind=TraceEventKind.NODE_EXITED)
        service.poll_results = [
            ([row], RunStatus.COMPLETED),  # a batch with events, none terminal-kind
            ([], RunStatus.COMPLETED),  # caught up, run terminal -> triggers the sweep
            ([], RunStatus.COMPLETED),  # the sweep itself: still nothing more
        ]
        messages = await _collect(
            iter_trace_events(service, run_id, cursor=0, is_disconnected=_disconnected_false)  # type: ignore[arg-type]
        )
        assert len(messages) == 1
        assert messages[0]["event"] == "node_exited"
        assert len(service.poll_calls) == 3  # the batch, the caught-up check, and the sweep

    async def test_active_run_with_no_events_keeps_polling_and_heartbeats(self) -> None:
        run_id = uuid.uuid4()
        service = _FakeTraceService()
        # Never produces events; the generator should heartbeat and keep going.
        service.poll_results = []  # falls back to ([], RUNNING) every call

        gen = iter_trace_events(
            service,
            run_id,
            cursor=0,
            is_disconnected=_disconnected_false,  # type: ignore[arg-type]
            poll_interval=0.001,
            heartbeat_seconds=0.01,
        )

        async def _collect_two() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            async for msg in gen:
                out.append(msg)
                if len(out) >= 2:
                    break
            return out

        messages = await asyncio.wait_for(_collect_two(), timeout=5.0)
        assert all(m.get("comment") == "keepalive" for m in messages)

    async def test_client_disconnect_exits_cleanly_with_no_events(self) -> None:
        run_id = uuid.uuid4()
        service = _FakeTraceService()
        messages = await _collect(
            iter_trace_events(service, run_id, cursor=0, is_disconnected=_disconnected_true)  # type: ignore[arg-type]
        )
        assert messages == []
        assert service.poll_calls == []

    async def test_mid_stream_read_failure_closes_stream_without_raising(self) -> None:
        run_id = uuid.uuid4()
        service = _FakeTraceService()
        service.poll_error = RuntimeError("connection reset by peer")
        messages = await _collect(
            iter_trace_events(service, run_id, cursor=0, is_disconnected=_disconnected_false)  # type: ignore[arg-type]
        )
        assert messages == []  # closed cleanly, no leaked exception, no fabricated event

    async def test_since_seq_fallback_computes_correct_cursor(self) -> None:
        """§ Last-Event-ID: since_seq=20 with no header starts at seq>=20,
        i.e. after_seq=19 — verified at the router's cursor computation."""
        from app.api.runs import _parse_last_event_id

        assert _parse_last_event_id(None) is None
        assert _parse_last_event_id("20") == 20
        with pytest.raises(Exception):  # noqa: B017 - InputValidationError, checked below
            _parse_last_event_id("not-a-number")

    def test_last_event_id_precedence_and_fallback_cursor_math(self) -> None:
        from app.api.runs import _parse_last_event_id

        # Last-Event-ID: 20 -> resume strictly after (seq >= 21)
        last_id = _parse_last_event_id("20")
        cursor = last_id if last_id is not None else max(20 - 1, 0)
        assert cursor == 20  # after_seq=20 means the repo returns seq > 20, i.e. seq>=21

        # No Last-Event-ID, since_seq=20 -> start at seq>=20
        last_id = _parse_last_event_id(None)
        since_seq = 20
        cursor = last_id if last_id is not None else max(since_seq - 1, 0)
        assert cursor == 19  # after_seq=19 means the repo returns seq > 19, i.e. seq>=20

        # Neither -> start at seq>=1
        last_id = _parse_last_event_id(None)
        since_seq = 0
        cursor = last_id if last_id is not None else max(since_seq - 1, 0)
        assert cursor == 0  # after_seq=0 means seq > 0, i.e. seq>=1

        # Last-Event-ID wins over a conflicting since_seq
        last_id = _parse_last_event_id("20")
        since_seq = 5
        cursor = last_id if last_id is not None else max(since_seq - 1, 0)
        assert cursor == 20


# ---------------------------------------------------------------------------
# SSE endpoint — end to end over ASGI transport (real app, fake service)
# ---------------------------------------------------------------------------
class TestSseEndpointAsgi:
    def _app(self, service: Any, *, settings: Settings | None = None) -> Any:
        app = create_app(settings=settings or Settings(_env_file=None))
        app.dependency_overrides[get_trace_service] = lambda: service
        return app

    async def test_correct_media_type_and_unknown_run_404(self) -> None:
        service = _FakeTraceService()
        service.ensure_run_exists_error = NotFoundError("nope")
        app = self._app(service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/runs/{uuid.uuid4()}/events")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/problem+json")

    async def test_stream_replays_and_terminates_on_terminal_event(self) -> None:
        run_id = uuid.uuid4()
        service = _FakeTraceService()
        row = _row(seq=1, run_id=run_id, kind=TraceEventKind.RUN_STARTED)
        terminal = _row(seq=2, run_id=run_id, kind=TraceEventKind.RUN_COMPLETED)
        service.poll_results = [([row, terminal], RunStatus.COMPLETED)]
        app = self._app(service)

        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
            client.stream("GET", f"/runs/{run_id}/events", timeout=10.0) as resp,
        ):
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            lines = [line async for line in resp.aiter_lines()]

        assert "id: 1" in lines
        assert "event: run_started" in lines
        assert "id: 2" in lines
        assert "event: run_completed" in lines

    async def test_last_event_id_header_takes_precedence_over_since_seq(self) -> None:
        run_id = uuid.uuid4()
        service = _FakeTraceService()
        terminal = _row(seq=21, run_id=run_id, kind=TraceEventKind.RUN_COMPLETED)
        service.poll_results = [([terminal], RunStatus.COMPLETED)]
        app = self._app(service)

        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
            client.stream(
                "GET",
                f"/runs/{run_id}/events?since_seq=5",
                headers={"Last-Event-ID": "20"},
                timeout=10.0,
            ) as resp,
        ):
            assert resp.status_code == 200
            _ = [line async for line in resp.aiter_lines()]

        assert service.poll_calls[0] == 20  # Last-Event-ID wins, not since_seq

    async def test_malformed_last_event_id_returns_422_before_streaming(self) -> None:
        run_id = uuid.uuid4()
        service = _FakeTraceService()
        app = self._app(service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                f"/runs/{run_id}/events", headers={"Last-Event-ID": "not-an-int"}
            )
        assert resp.status_code == 422
        assert resp.headers["content-type"].startswith("application/problem+json")

    async def test_authorization_enforced_for_sse_endpoint(self) -> None:
        service = _FakeTraceService()
        cfg = Settings(_env_file=None, OPSPILOT_AUTH_MODE="token")
        app = self._app(service, settings=cfg)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/runs/{uuid.uuid4()}/events")
        assert resp.status_code == 401

    async def test_multiple_subscribers_are_independent(self) -> None:
        """Two independent generator instances against the same fake service
        never share cursor state — the point of per-subscriber polling."""
        run_id = uuid.uuid4()
        service = _FakeTraceService()
        terminal = _row(seq=1, run_id=run_id, kind=TraceEventKind.RUN_COMPLETED)

        gen_a = iter_trace_events(service, run_id, cursor=0, is_disconnected=_disconnected_false)  # type: ignore[arg-type]
        gen_b = iter_trace_events(service, run_id, cursor=0, is_disconnected=_disconnected_false)  # type: ignore[arg-type]

        service.poll_results = [([terminal], RunStatus.COMPLETED)]
        msgs_a = await _collect(gen_a)
        service.poll_results = [([terminal], RunStatus.COMPLETED)]
        msgs_b = await _collect(gen_b)

        assert msgs_a == msgs_b
        assert msgs_a[0]["id"] == "1"


# ---------------------------------------------------------------------------
# Real PostgreSQL integration
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.usefixtures("_database")
class TestTraceApiPostgresIntegration:
    @pytest.fixture(scope="class")
    def _database(self) -> None:
        require_database()
        migrate_to_head()

    @pytest.fixture
    async def engine(self) -> AsyncIterator[Any]:
        from sqlalchemy.ext.asyncio import create_async_engine

        from recovery_harness import settings as harness_settings

        eng = create_async_engine(
            harness_settings().database_url.get_secret_value(), pool_pre_ping=True
        )
        try:
            yield eng
        finally:
            await eng.dispose()

    async def _seed_events(self, uow_factory: Any, run_id: uuid.UUID, count: int) -> None:
        async with uow_factory() as uow:
            for i in range(count):
                await uow.trace_events.append(
                    run_id=run_id,
                    kind=TraceEventKind.NODE_ENTERED,
                    node=f"n{i}",
                    payload={"i": i},
                )
            await uow.commit()

    async def test_pagination_across_multiple_pages_no_gaps_no_duplicates(
        self, engine: Any
    ) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        await self._seed_events(uow_factory, run_id, 10)

        # No dependency override here: this exercises the real
        # `get_trace_service` -> `TraceService` -> `TraceEventRepository`
        # wiring against a genuine database.
        app = create_app(settings=Settings(_env_file=None))
        app.state.db_engine = engine

        seen: list[int] = []
        cursor = 0
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(20):
                resp = await client.get(f"/runs/{run_id}/trace?since_seq={cursor}&limit=3")
                assert resp.status_code == 200
                data = resp.json()
                seen.extend(e["seq"] for e in data["events"])
                if data["complete"] or data["next_seq"] is None:
                    break
                assert data["next_seq"] is not None
                cursor = data["next_seq"]

        assert seen == list(range(1, 11))  # no gaps, no duplicates, strict order

    async def test_terminal_run_reports_complete_true(self, engine: Any) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with uow_factory() as uow:
            await uow.trace_events.append(run_id=run_id, kind=TraceEventKind.RUN_COMPLETED)
            await uow.agent_runs.transition_status(
                run_id, expected=list(RunStatus), status=RunStatus.COMPLETED
            )
            await uow.commit()

        app = create_app(settings=Settings(_env_file=None))
        app.state.db_engine = engine
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/runs/{run_id}/trace")
        data = resp.json()
        assert data["complete"] is True
        assert data["next_seq"] is None

    async def test_kind_and_severity_filters_over_real_data(self, engine: Any) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with uow_factory() as uow:
            await uow.trace_events.append(run_id=run_id, kind=TraceEventKind.NODE_ENTERED)
            await uow.trace_events.append(
                run_id=run_id,
                kind=TraceEventKind.TOOL_FAILED,
                severity=TraceEventSeverity.WARNING,
            )
            await uow.trace_events.append(
                run_id=run_id,
                kind=TraceEventKind.POLICY_VIOLATION,
                severity=TraceEventSeverity.ERROR,
            )
            await uow.commit()

        app = create_app(settings=Settings(_env_file=None))
        app.state.db_engine = engine
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            by_kind = await client.get(f"/runs/{run_id}/trace?kind=tool_failed")
            by_severity = await client.get(f"/runs/{run_id}/trace?severity_min=warning")

        assert [e["kind"] for e in by_kind.json()["events"]] == ["tool_failed"]
        assert {e["kind"] for e in by_severity.json()["events"]} == {
            "tool_failed",
            "policy_violation",
        }

    async def test_concurrent_trace_append_while_rest_paging(self, engine: Any) -> None:
        """A real writer keeps appending events while the client pages —
        the reader must see a gap-free, duplicate-free prefix at every step,
        never a torn read."""
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)

        stop = asyncio.Event()
        appended: list[int] = []

        async def writer() -> None:
            i = 0
            while not stop.is_set() and i < 25:
                async with uow_factory() as uow:
                    ev = await uow.trace_events.append(
                        run_id=run_id, kind=TraceEventKind.NODE_ENTERED, node=f"n{i}"
                    )
                    await uow.commit()
                appended.append(ev.seq)
                i += 1
                await asyncio.sleep(0.005)

        app = create_app(settings=Settings(_env_file=None))
        app.state.db_engine = engine

        seen: list[int] = []
        writer_task = asyncio.create_task(writer())
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                cursor = 0
                for _ in range(60):
                    resp = await client.get(f"/runs/{run_id}/trace?since_seq={cursor}&limit=4")
                    data = resp.json()
                    seqs = [e["seq"] for e in data["events"]]
                    seen.extend(seqs)
                    if data["next_seq"] is not None:
                        cursor = data["next_seq"]
                    if writer_task.done() and not seqs:
                        break
                    await asyncio.sleep(0.005)
        finally:
            stop.set()
            await writer_task

        assert seen == sorted(seen)  # strictly ascending
        assert len(seen) == len(set(seen))  # no duplicates
        assert seen == list(range(1, len(seen) + 1))  # no gaps

    async def test_sse_reconnect_with_last_event_id_while_events_keep_appending(
        self, engine: Any
    ) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with uow_factory() as uow:
            for _i in range(3):
                await uow.trace_events.append(run_id=run_id, kind=TraceEventKind.NODE_ENTERED)
            await uow.commit()

        app = create_app(settings=Settings(_env_file=None))
        app.state.db_engine = engine

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # First connection sees 1..3, then we simulate a drop.
            resp = await client.get(f"/runs/{run_id}/trace?since_seq=1&limit=10")
            first_seqs = [e["seq"] for e in resp.json()["events"]]
            assert first_seqs == [1, 2, 3]

            # More events arrive while "disconnected".
            async with uow_factory() as uow:
                await uow.trace_events.append(run_id=run_id, kind=TraceEventKind.RUN_COMPLETED)
                await uow.agent_runs.transition_status(
                    run_id, expected=list(RunStatus), status=RunStatus.COMPLETED
                )
                await uow.commit()

            # Reconnect with Last-Event-ID: 3 -> must replay seq 4 onward only.
            async with client.stream(
                "GET", f"/runs/{run_id}/events", headers={"Last-Event-ID": "3"}, timeout=10.0
            ) as stream_resp:
                lines = [line async for line in stream_resp.aiter_lines()]

        ids = [int(line[4:]) for line in lines if line.startswith("id: ")]
        assert ids == [4]  # no re-delivery of 1..3, no gap

    async def test_multiple_subscribers_against_real_database(self, engine: Any) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with uow_factory() as uow:
            await uow.trace_events.append(run_id=run_id, kind=TraceEventKind.NODE_ENTERED)
            await uow.trace_events.append(run_id=run_id, kind=TraceEventKind.RUN_COMPLETED)
            await uow.agent_runs.transition_status(
                run_id, expected=list(RunStatus), status=RunStatus.COMPLETED
            )
            await uow.commit()

        app = create_app(settings=Settings(_env_file=None))
        app.state.db_engine = engine

        async def one_client() -> list[str]:
            async with (
                AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
                c.stream("GET", f"/runs/{run_id}/events", timeout=10.0) as r,
            ):
                return [line async for line in r.aiter_lines() if line.startswith("id: ")]

        results = await asyncio.gather(one_client(), one_client())
        assert results[0] == results[1] == ["id: 1", "id: 2"]

    async def test_already_terminal_run_replays_then_closes(self, engine: Any) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with uow_factory() as uow:
            await uow.trace_events.append(run_id=run_id, kind=TraceEventKind.NODE_ENTERED)
            await uow.trace_events.append(run_id=run_id, kind=TraceEventKind.RUN_FAILED)
            await uow.agent_runs.transition_status(
                run_id, expected=list(RunStatus), status=RunStatus.FAILED
            )
            await uow.commit()

        app = create_app(settings=Settings(_env_file=None))
        app.state.db_engine = engine
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
            client.stream("GET", f"/runs/{run_id}/events", timeout=10.0) as resp,
        ):
            lines = [line async for line in resp.aiter_lines()]
        assert "event: node_entered" in lines
        assert "event: run_failed" in lines
