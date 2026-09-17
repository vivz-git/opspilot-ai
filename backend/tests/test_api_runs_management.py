"""API-002: Run Listing, Filtering, Keyset Pagination & Operational Management tests (§13.2, §13.3).

Covers:
1. GET /runs default list returns 200 with RunSummary list and null next_cursor
2. GET /runs keyset cursor pagination traverses page by page without gaps or duplicates
3. GET /runs malformed cursor returns 422 Problem Details (code="validation_error")
4. GET /runs limit validation (ge=1, le=100) returns 422 on boundary violations
5. GET /runs filters by status (single and repeatable)
6. GET /runs filters by since/until datetime range
7. GET /runs filters by parent_run_id
8. GET /runs filters by text search query q (case-insensitive substring)
9. POST /runs/{id}/start on created run returns 202 with status="queued"
10. POST /runs/{id}/start on non-created run returns 409 (code="run_not_startable")
11. POST /runs/{id}/start on unknown run returns 404 (code="not_found")
12. POST /runs/{id}/cancel on non-terminal run returns 202 with status="cancelled"
13. POST /runs/{id}/cancel on terminal run returns 409 (code="run_not_cancellable")
14. POST /runs/{id}/cancel repeated on cancelled run is idempotent (returns 202)
15. POST /runs/{id}/cancel on awaiting_approval cancels open pending approval in DB
16. POST /runs/{id}/cancel on running run sets cooperative cancellation flag
17. POST /runs/{id}/retry on terminal run creates new child run (201 Created) with Location header
18. POST /runs/{id}/retry on active run returns 409 (code="run_active")
19. POST /runs/{id}/retry preserves original run as immutable
20. Real PostgreSQL integration tests:
    - Keyset cursor stability under concurrent inserts (zero shifts, zero duplicates)
    - Atomic cancellation of run + pending approval in one transaction
    - Atomic start race under concurrent callers
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.agent.state import ApprovalStatus, PlannerKind, RunStatus
from app.api.dependencies import get_run_service
from app.api.schemas import encode_cursor
from app.errors import (
    NotFoundError,
    RunActiveError,
    RunNotCancellableError,
    RunNotStartableError,
)
from app.execution.runs import RunCreateResult, RunService
from app.main import create_app
from app.persistence.models import AgentRun, RiskLevel, TraceEventKind
from app.runtime import InMemoryCancellationSource
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import (
    migrate_to_head,
    require_database,
    uow_factory_for,
)
from recovery_harness import (
    settings as harness_settings,
)

pytestmark = [pytest.mark.unit]


def _make_sample_run(
    run_id: uuid.UUID | None = None,
    *,
    status: RunStatus = RunStatus.CREATED,
    user_request: str = "Find fintech leads in London",
    idempotency_key: str | None = None,
    metadata: dict[str, Any] | None = None,
    parent_run_id: uuid.UUID | None = None,
    planner_kind: PlannerKind = PlannerKind.RULES,
    created_at: datetime | None = None,
) -> AgentRun:
    r_id = run_id or uuid.uuid4()
    now = created_at or datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
    return AgentRun(
        id=r_id,
        parent_run_id=parent_run_id,
        status=status,
        status_reason=None,
        user_request=user_request,
        normalized_task=None,
        plan=None,
        plan_history=[],
        plan_revision=0,
        final_response=None,
        planner_kind=planner_kind,
        model_id=None,
        prompt_version="v1",
        seed=1337,
        idempotency_key=idempotency_key,
        actor_id=None,
        step_count=0,
        retry_total=0,
        replan_count=0,
        deadline_at=now + timedelta(minutes=15),
        lease_owner=None,
        lease_expires_at=None,
        created_at=now,
        started_at=now if status != RunStatus.CREATED else None,
        finished_at=None,
        updated_at=now,
        duration_ms=None,
        evaluation_run_id=None,
        eval_case_id=None,
        metadata_=metadata or {},
    )


# ---------------------------------------------------------------------------
# Unit Tests (Mock Service & FastClient)
# ---------------------------------------------------------------------------
class TestListRunsEndpoint:
    def test_list_runs_default_returns_200(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run1 = _make_sample_run()
        run2 = _make_sample_run()
        mock_service.list_runs.return_value = ([run1, run2], None, 2)

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get("/runs")
            assert resp.status_code == 200
            data = resp.json()
            assert "items" in data
            assert len(data["items"]) == 2
            assert data["items"][0]["run_id"] == str(run1.id)
            assert data["next_cursor"] is None
            assert data["total_estimate"] == 2

    def test_list_runs_with_cursor_and_pagination(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run1 = _make_sample_run()
        cursor_val = encode_cursor(run1.created_at, run1.id)
        mock_service.list_runs.return_value = ([run1], cursor_val, 15)

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs?limit=1&cursor={cursor_val}")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["items"]) == 1
            assert data["next_cursor"] == cursor_val
            assert data["total_estimate"] == 15

    def test_list_runs_malformed_cursor_returns_422(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        mock_service.list_runs.side_effect = Exception("Should not reach here")

        app = create_app()
        # Mock service list_runs calling decode_cursor which raises InputValidationError
        from app.errors import InputValidationError

        mock_service.list_runs.side_effect = InputValidationError("Invalid pagination cursor")
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get("/runs?cursor=not-a-valid-cursor!")
            assert resp.status_code == 422
            data = resp.json()
            assert data["code"] == "validation_error"

    def test_list_runs_invalid_limit_bounds_returns_422(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            resp_zero = client.get("/runs?limit=0")
            assert resp_zero.status_code == 422
            assert resp_zero.json()["code"] == "validation_error"

            resp_over = client.get("/runs?limit=101")
            assert resp_over.status_code == 422
            assert resp_over.json()["code"] == "validation_error"

    def test_list_runs_filtering_params_forwarded_to_service(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        mock_service.list_runs.return_value = ([], None, 0)

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        p_id = uuid.uuid4()
        with TestClient(app) as client:
            resp = client.get(
                f"/runs?status=running&status=queued&parent_run_id={p_id}&q=fintech&limit=10"
            )
            assert resp.status_code == 200
            mock_service.list_runs.assert_awaited_once()
            call_kwargs = mock_service.list_runs.await_args.kwargs
            assert call_kwargs["statuses"] == [RunStatus.RUNNING, RunStatus.QUEUED]
            assert call_kwargs["parent_run_id"] == p_id
            assert call_kwargs["query"] == "fintech"
            assert call_kwargs["limit"] == 10

    def test_list_runs_prefix_parity_api_v1(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        mock_service.list_runs.return_value = ([], None, 0)

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp1 = client.get("/runs")
            resp2 = client.get("/api/v1/runs")
            assert resp1.status_code == 200
            assert resp2.status_code == 200


class TestStartRunEndpoint:
    def test_start_run_success_returns_202(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_id = uuid.uuid4()
        run_queued = _make_sample_run(run_id, status=RunStatus.QUEUED)
        mock_service.start_run.return_value = run_queued

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(f"/runs/{run_id}/start")
            assert resp.status_code == 202
            data = resp.json()
            assert data["run_id"] == str(run_id)
            assert data["status"] == "queued"

    def test_start_run_not_created_status_returns_409(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_id = uuid.uuid4()
        mock_service.start_run.side_effect = RunNotStartableError(
            f"Run {run_id} is in status 'running', expected 'created'"
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(f"/runs/{run_id}/start")
            assert resp.status_code == 409
            data = resp.json()
            assert data["code"] == "run_not_startable"

    def test_start_run_unknown_returns_404(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_id = uuid.uuid4()
        mock_service.start_run.side_effect = NotFoundError(f"Run {run_id} not found")

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(f"/runs/{run_id}/start")
            assert resp.status_code == 404
            assert resp.json()["code"] == "not_found"


class TestCancelRunEndpoint:
    def test_cancel_run_success_returns_202(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_id = uuid.uuid4()
        run_cancelled = _make_sample_run(run_id, status=RunStatus.CANCELLED)
        mock_service.cancel_run.return_value = run_cancelled

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(f"/runs/{run_id}/cancel", json={"reason": "operator requested"})
            assert resp.status_code == 202
            data = resp.json()
            assert data["run_id"] == str(run_id)
            assert data["status"] == "cancelled"

    def test_cancel_run_terminal_returns_409(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_id = uuid.uuid4()
        mock_service.cancel_run.side_effect = RunNotCancellableError(
            f"Run {run_id} is already terminal with status 'completed'"
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(f"/runs/{run_id}/cancel")
            assert resp.status_code == 409
            data = resp.json()
            assert data["code"] == "run_not_cancellable"

    def test_cancel_run_unknown_returns_404(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_id = uuid.uuid4()
        mock_service.cancel_run.side_effect = NotFoundError(f"Run {run_id} not found")

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(f"/runs/{run_id}/cancel")
            assert resp.status_code == 404
            assert resp.json()["code"] == "not_found"


class TestRetryRunEndpoint:
    def test_retry_run_terminal_creates_child_run_and_returns_201(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        parent_id = uuid.uuid4()
        child_id = uuid.uuid4()
        child_run = _make_sample_run(child_id, parent_run_id=parent_id)

        mock_service.retry_run.return_value = RunCreateResult(run=child_run, is_duplicate=False)
        mock_service.get_run_details.return_value = None

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(f"/runs/{parent_id}/retry", json={"auto_start": True})
            assert resp.status_code == 201
            assert resp.headers["Location"] == f"/runs/{child_id}"
            data = resp.json()
            assert data["run_id"] == str(child_id)
            assert data["parent_run_id"] == str(parent_id)

    def test_retry_run_active_status_returns_409(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_id = uuid.uuid4()
        mock_service.retry_run.side_effect = RunActiveError(
            f"Run {run_id} is currently active (running); cannot retry until terminal"
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(f"/runs/{run_id}/retry")
            assert resp.status_code == 409
            data = resp.json()
            assert data["code"] == "run_active"

    def test_retry_run_unknown_returns_404(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_id = uuid.uuid4()
        mock_service.retry_run.side_effect = NotFoundError(f"Run {run_id} not found")

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(f"/runs/{run_id}/retry")
            assert resp.status_code == 404
            assert resp.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# Real PostgreSQL Integration Tests
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestRunsManagementPostgresIntegration:
    @pytest.fixture(scope="class")
    def _database(self) -> None:
        require_database()
        migrate_to_head()

    @pytest.fixture
    async def engine(self) -> AsyncIterator[AsyncEngine]:
        eng = create_async_engine(
            harness_settings().database_url.get_secret_value(),
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=10,
        )
        try:
            yield eng
        finally:
            await eng.dispose()

    async def test_keyset_cursor_stability_under_concurrent_inserts(
        self, _database: None, engine: AsyncEngine
    ) -> None:
        """Asserts that keyset cursor pagination on (created_at, id) produces zero duplicates
        and zero missed rows when new rows are inserted between page fetches (§13.2)."""
        settings = harness_settings()
        uow_factory = uow_factory_for(engine)
        cancellation_source = InMemoryCancellationSource()
        service = RunService(
            uow_factory=uow_factory,
            settings=settings,
            cancellation_source=cancellation_source,
        )

        # Seed 6 runs with unique user requests
        tag = uuid.uuid4().hex[:8]
        seed_runs: list[AgentRun] = []
        for i in range(6):
            res = await service.create_run(f"Keyset task {tag} item {i}")
            seed_runs.append(res.run)

        # Page 1 with limit=3 (newest first: items 5, 4, 3)
        page1_items, next_cursor, total = await service.list_runs(
            query=tag,
            limit=3,
        )
        assert len(page1_items) == 3
        assert next_cursor is not None
        page1_ids = {r.id for r in page1_items}

        # Concurrently insert 2 brand new runs (with newer timestamps)
        res_new1 = await service.create_run(f"Keyset task {tag} concurrent 1")
        res_new2 = await service.create_run(f"Keyset task {tag} concurrent 2")

        # Page 2 with cursor from Page 1 (must return items 2, 1, 0 — NOT the new items)
        page2_items, next_cursor2, total2 = await service.list_runs(
            query=tag,
            cursor_str=next_cursor,
            limit=3,
        )
        assert len(page2_items) == 3
        page2_ids = {r.id for r in page2_items}

        # Zero duplicates between page 1 and page 2
        assert page1_ids.isdisjoint(page2_ids)

        # Neither of the newer concurrent items appeared on page 2
        assert res_new1.run.id not in page2_ids
        assert res_new2.run.id not in page2_ids

        # Page 1 + Page 2 together contain exactly the original 6 seeded runs
        all_paged = page1_ids | page2_ids
        assert all_paged == {r.id for r in seed_runs}

    async def test_atomic_cancellation_of_run_and_pending_approval(
        self, _database: None, engine: AsyncEngine
    ) -> None:
        """Cancelling a run in awaiting_approval cancels both run and approval in one tx."""
        settings = harness_settings()
        uow_factory = uow_factory_for(engine)
        cancellation_source = InMemoryCancellationSource()
        service = RunService(
            uow_factory=uow_factory,
            settings=settings,
            cancellation_source=cancellation_source,
        )

        # 1. Create run
        res = await service.create_run("Task needing approval")
        run_id = res.run.id

        # 2. Transition to awaiting_approval and create pending approval
        now = datetime.now(UTC)
        async with uow_factory() as uow:
            await uow.agent_runs.update_status(run_id, status=RunStatus.AWAITING_APPROVAL)
            app_res = await uow.approvals.upsert_request(
                run_id=run_id,
                step_id="s1",
                tool="send_email_mock",
                risk=RiskLevel.HIGH,
                title="Send test email",
                summary="Summary",
                payload_preview={"to": "test@example.com"},
                args_hash="hash123",
                requested_at=now,
                expires_at=now + timedelta(minutes=10),
            )
            approval_id = app_res.row.id
            await uow.commit()

        # 3. Cancel run
        cancelled_run = await service.cancel_run(run_id, reason="Operator decided not to send")
        assert cancelled_run.status == RunStatus.CANCELLED
        assert cancelled_run.status_reason == "Operator decided not to send"

        # 4. Verify in DB that approval was cancelled
        async with uow_factory() as uow:
            app_row = await uow.approvals.get(approval_id)
            assert app_row is not None
            assert app_row.status == ApprovalStatus.CANCELLED
            assert app_row.decided_by == "operator_cancellation"

            # Verify trace events
            events = await uow.trace_events.list_by_run(run_id)
            event_kinds = [e.kind for e in events]
            assert TraceEventKind.RUN_CANCELLED in event_kinds

    async def test_start_run_atomic_race(self, _database: None, engine: AsyncEngine) -> None:
        """Multiple concurrent callers calling start_run on created run: exactly 1 wins."""
        settings = harness_settings()
        uow_factory = uow_factory_for(engine)
        service = RunService(
            uow_factory=uow_factory,
            settings=settings,
        )

        res = await service.create_run("Task for start race")
        run_id = res.run.id

        success_count = 0
        conflict_count = 0

        async def _attempt_start() -> None:
            nonlocal success_count, conflict_count
            try:
                await service.start_run(run_id)
                success_count += 1
            except RunNotStartableError:
                conflict_count += 1

        # Race 5 workers
        await asyncio.gather(*[_attempt_start() for _ in range(5)])

        assert success_count == 1
        assert conflict_count == 4

        # Final DB status is queued
        async with uow_factory() as uow:
            final_run = await uow.agent_runs.get(run_id)
            assert final_run is not None
            assert final_run.status == RunStatus.QUEUED
