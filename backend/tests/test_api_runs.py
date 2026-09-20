"""API-001: Public Run Creation & Run Status API Foundation tests (§13.1, §13.2, §13.3).

Covers:
1. POST /runs with valid request creates run (201 Created) with Location header.
2. POST /runs with auto_start=True sets initial status to "queued".
3. POST /runs empty request -> 422 Problem Details (code="validation_error").
4. POST /runs whitespace only -> 422 Problem Details (code="validation_error").
5. POST /runs oversized request (>4000 chars) -> 422 Problem Details (code="validation_error").
6. POST /runs extra fields in request -> 422 Problem Details (code="validation_error").
7. POST /runs with Idempotency-Key replay with identical body -> 200 OK with same run.
8. POST /runs with Idempotency-Key replay with conflicting body
   -> 409 Problem Details (code="idempotency_conflict").
9. GET /runs/{run_id} returns existing run (200 OK).
10. GET /runs/{run_id} with unknown UUID -> 404 Problem Details (code="not_found").
11. GET /runs/{run_id} with malformed UUID -> 422 Problem Details (code="validation_error").
12. GET /runs/{run_id} awaiting approval exposes safe, redacted pending approval and resumable=True.
13. GET /runs/{run_id} terminal states expose resumable=False.
14. Authorization boundary enforcement (when auth_mode configured).
15. Path prefix parity: /api/v1/runs and /runs behave identically.
16. Real PostgreSQL integration test: durable persistence and canonical RUN_CREATED trace event.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.agent.state import ApprovalStatus, PlannerKind, RunStatus
from app.api.dependencies import get_run_service
from app.config import AuthMode, Settings
from app.errors import IdempotencyConflictError
from app.execution.runs import RunCreateResult, RunDetails, RunService
from app.main import create_app
from app.persistence.models import AgentRun, ApprovalRow, RiskLevel, ToolName, TraceEventKind
from app.runtime import FixedClock
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import (
    T0,
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
    user_request: str = "Find fintech leads",
    idempotency_key: str | None = None,
    metadata: dict[str, Any] | None = None,
    parent_run_id: uuid.UUID | None = None,
    planner_kind: PlannerKind = PlannerKind.RULES,
) -> AgentRun:
    r_id = run_id or uuid.uuid4()
    now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
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
class TestPostRunsEndpoint:
    def test_post_runs_valid_creates_run_and_returns_201(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_row = _make_sample_run()
        mock_service.create_run.return_value = RunCreateResult(run=run_row, is_duplicate=False)
        mock_service.get_run_details.return_value = RunDetails(
            run=run_row, steps=[], pending_approval=None
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                "/runs",
                json={
                    "user_request": "Find top 3 fintech leads in London",
                    "auto_start": False,
                    "metadata": {"source": "test_suite"},
                },
            )

        assert resp.status_code == 201
        assert resp.headers["Location"] == f"/runs/{run_row.id}"
        data = resp.json()
        assert data["run_id"] == str(run_row.id)
        assert data["status"] == "created"
        assert data["user_request"] == "Find fintech leads"
        assert data["resumable"] is False
        assert data["counters"] == {"step_count": 0, "retry_total": 0, "replan_count": 0}
        assert "created_at" in data["timestamps"]
        assert "deadline_at" in data["timestamps"]

        mock_service.create_run.assert_awaited_once_with(
            "Find top 3 fintech leads in London",
            idempotency_key=None,
            metadata={"source": "test_suite"},
            auto_start=False,
            planner_kind=None,
        )

    def test_post_runs_with_auto_start_passes_flag(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_row = _make_sample_run(status=RunStatus.QUEUED)
        mock_service.create_run.return_value = RunCreateResult(run=run_row, is_duplicate=False)
        mock_service.get_run_details.return_value = RunDetails(
            run=run_row, steps=[], pending_approval=None
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                "/runs",
                json={
                    "user_request": "Execute task immediately",
                    "auto_start": True,
                },
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "queued"
        mock_service.create_run.assert_awaited_once_with(
            "Execute task immediately",
            idempotency_key=None,
            metadata={},
            auto_start=True,
            planner_kind=None,
        )

    def test_post_runs_empty_request_returns_422(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            resp = client.post("/runs", json={"user_request": ""})

        assert resp.status_code == 422
        assert resp.headers["content-type"] == "application/problem+json"
        data = resp.json()
        assert data["code"] == "validation_error"
        assert data["title"] == "Validation error"

    def test_post_runs_whitespace_only_returns_422(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            resp = client.post("/runs", json={"user_request": "     "})

        assert resp.status_code == 422
        assert resp.headers["content-type"] == "application/problem+json"
        data = resp.json()
        assert data["code"] == "validation_error"

    def test_post_runs_oversized_request_returns_422(self) -> None:
        app = create_app()
        oversized = "a" * 4001
        with TestClient(app) as client:
            resp = client.post("/runs", json={"user_request": oversized})

        assert resp.status_code == 422
        data = resp.json()
        assert data["code"] == "validation_error"

    def test_post_runs_extra_fields_forbidden(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            resp = client.post(
                "/runs",
                json={
                    "user_request": "Valid request",
                    "unexpected_field": "disallowed",
                },
            )

        assert resp.status_code == 422
        data = resp.json()
        assert data["code"] == "validation_error"

    def test_post_runs_idempotent_replay_returns_200_and_same_run(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_row = _make_sample_run(idempotency_key="key-abc")
        # is_duplicate=True
        mock_service.create_run.return_value = RunCreateResult(run=run_row, is_duplicate=True)
        mock_service.get_run_details.return_value = RunDetails(
            run=run_row, steps=[], pending_approval=None
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                "/runs",
                headers={"Idempotency-Key": "key-abc"},
                json={"user_request": "Find fintech leads"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == str(run_row.id)
        mock_service.create_run.assert_awaited_once_with(
            "Find fintech leads",
            idempotency_key="key-abc",
            metadata={},
            auto_start=False,
            planner_kind=None,
        )

    def test_post_runs_idempotency_conflict_returns_409(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        mock_service.create_run.side_effect = IdempotencyConflictError(
            "Idempotency-Key 'key-abc' reused with a different body",
            detail={"idempotency_key": "key-abc"},
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                "/runs",
                headers={"Idempotency-Key": "key-abc"},
                json={"user_request": "Conflicting different request"},
            )

        assert resp.status_code == 409
        assert resp.headers["content-type"] == "application/problem+json"
        data = resp.json()
        assert data["code"] == "idempotency_conflict"
        assert "different body" in data["detail"]


class TestGetRunsEndpoint:
    def test_get_runs_existing_run_returns_200(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_row = _make_sample_run()
        mock_service.get_run_details.return_value = RunDetails(
            run=run_row, steps=[], pending_approval=None
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_row.id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == str(run_row.id)
        assert data["status"] == "created"
        assert data["resumable"] is False

    def test_get_runs_unknown_run_returns_404(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        mock_service.get_run_details.return_value = None

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        random_id = uuid.uuid4()
        with TestClient(app) as client:
            resp = client.get(f"/runs/{random_id}")

        assert resp.status_code == 404
        assert resp.headers["content-type"] == "application/problem+json"
        data = resp.json()
        assert data["code"] == "not_found"
        assert str(random_id) in data["detail"]

    def test_get_runs_malformed_uuid_returns_422(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/runs/not-a-uuid")

        assert resp.status_code == 422
        assert resp.headers["content-type"] == "application/problem+json"
        data = resp.json()
        assert data["code"] == "validation_error"

    def test_get_runs_awaiting_approval_exposes_pending_approval(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_id = uuid.uuid4()
        run_row = _make_sample_run(run_id=run_id, status=RunStatus.AWAITING_APPROVAL)
        now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
        approval_row = ApprovalRow(
            id=uuid.uuid4(),
            run_id=run_id,
            step_id="step_send",
            tool=ToolName.SEND_EMAIL_MOCK,
            risk=RiskLevel.HIGH,
            title="Send outreach email",
            summary="Sends email to prospect",
            payload_preview={"to": "ceo@target.example", "subject": "Hello"},
            args_hash="hash-abc-123",
            status=ApprovalStatus.PENDING,
            superseded_by=None,
            requested_at=now,
            expires_at=now + timedelta(hours=24),
            decided_at=None,
            decided_by=None,
            decision_reason=None,
        )

        mock_service.get_run_details.return_value = RunDetails(
            run=run_row, steps=[], pending_approval=approval_row
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/runs/{run_id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "awaiting_approval"
        assert data["resumable"] is True
        assert data["pending_approval"] is not None
        pa = data["pending_approval"]
        assert pa["approval_id"] == str(approval_row.id)
        assert pa["step_id"] == "step_send"
        assert pa["tool"] == "send_email_mock"
        assert pa["risk"] == "high"
        assert pa["payload_preview"]["to"] == "ceo@target.example"

    def test_get_runs_terminal_resumable_false(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        for term_status in (RunStatus.COMPLETED, RunStatus.REJECTED, RunStatus.FAILED):
            run_row = _make_sample_run(status=term_status)
            mock_service.get_run_details.return_value = RunDetails(
                run=run_row, steps=[], pending_approval=None
            )

            app = create_app()
            app.dependency_overrides[get_run_service] = lambda: mock_service

            with TestClient(app) as client:
                resp = client.get(f"/runs/{run_row.id}")

            assert resp.status_code == 200
            data = resp.json()
            assert data["resumable"] is False


class TestAuthorizationBoundary:
    """§16.6 / ADR-026. This is the *access* boundary, not authentication: it
    refuses traffic that did not arrive through the declared access proxy."""

    def test_require_authorization_enforced_when_configured(self) -> None:
        settings = Settings(_env_file=None, OPSPILOT_AUTH_MODE=AuthMode.PROXY)
        app = create_app(settings=settings)

        with TestClient(app) as client:
            # Nothing stamped by the proxy -> 401 policy_violation
            unauth = client.post("/runs", json={"user_request": "Valid request"})
            assert unauth.status_code == 401
            assert unauth.json()["code"] == "policy_violation"

            # An Authorization header is not the proxy's assertion, and the
            # app has no credential it could verify: it must not open the door.
            bearer = client.post(
                "/runs",
                headers={"Authorization": "Bearer token123"},
                json={"user_request": "Valid request"},
            )
            assert bearer.status_code == 401

            # With mock service and valid auth header
            mock_service = AsyncMock(spec=RunService)
            run_row = _make_sample_run()
            mock_service.create_run.return_value = RunCreateResult(run=run_row, is_duplicate=False)
            mock_service.get_run_details.return_value = RunDetails(
                run=run_row, steps=[], pending_approval=None
            )
            app.dependency_overrides[get_run_service] = lambda: mock_service

            auth = client.post(
                "/runs",
                headers={settings.proxy_identity_header: "operator@example.com"},
                json={"user_request": "Valid request"},
            )
            assert auth.status_code == 201

    def test_a_custom_proxy_header_name_is_honoured(self) -> None:
        settings = Settings(
            _env_file=None,
            OPSPILOT_AUTH_MODE=AuthMode.PROXY,
            OPSPILOT_PROXY_IDENTITY_HEADER="X-Access-User",
        )
        app = create_app(settings=settings)

        with TestClient(app) as client:
            wrong = client.get(
                "/runs", headers={"Cf-Access-Authenticated-User-Email": "operator@example.com"}
            )
            assert wrong.status_code == 401

    def test_nothing_is_enforced_in_the_localhost_shape(self) -> None:
        """ADR-017: with no access layer declared there is nothing exposed to
        fence, and `OPSPILOT_ENV=production` refuses to start in this state."""
        app = create_app(settings=Settings(_env_file=None))
        with TestClient(app) as client:
            assert client.get("/runs").status_code != 401


class TestPrefixParity:
    def test_api_v1_and_root_runs_parity(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_row = _make_sample_run()
        mock_service.create_run.return_value = RunCreateResult(run=run_row, is_duplicate=False)
        mock_service.get_run_details.return_value = RunDetails(
            run=run_row, steps=[], pending_approval=None
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        with TestClient(app) as client:
            # Root prefix
            resp_root = client.post("/runs", json={"user_request": "Parity test"})
            assert resp_root.status_code == 201

            # /api/v1 prefix
            resp_v1 = client.post("/api/v1/runs", json={"user_request": "Parity test"})
            assert resp_v1.status_code == 201

            # Detail endpoints
            assert client.get(f"/runs/{run_row.id}").status_code == 200
            assert client.get(f"/api/v1/runs/{run_row.id}").status_code == 200


# ---------------------------------------------------------------------------
# Integration Tests with Real PostgreSQL (§18.3)
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestRunsApiPostgresIntegration:
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

    async def test_full_run_creation_cycle_with_real_persistence(
        self, _database: None, engine: AsyncEngine
    ) -> None:
        clock = FixedClock(T0)
        uow_factory = uow_factory_for(engine)
        settings = harness_settings()

        run_service = RunService(
            uow_factory=uow_factory,
            settings=settings,
            clock=clock,
        )

        app = create_app(
            settings=settings,
            run_service=run_service,
            clock=clock,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Create a fresh run with Idempotency-Key
            idempotency_key = f"idem-{uuid.uuid4()}"
            create_resp = await client.post(
                "/runs",
                headers={"Idempotency-Key": idempotency_key},
                json={
                    "user_request": "Find 3 fintech leads in London and email them",
                    "auto_start": False,
                    "metadata": {"test": "real_postgres"},
                },
            )
            assert create_resp.status_code == 201
            created_data = create_resp.json()
            run_id = created_data["run_id"]
            assert created_data["status"] == "created"
            assert created_data["counters"]["step_count"] == 0
            assert create_resp.headers["Location"] == f"/runs/{run_id}"

            # 2. Inspect persisted state directly in Postgres
            async with uow_factory() as uow:
                run_db = await uow.agent_runs.get(uuid.UUID(run_id))
                assert run_db is not None
                assert run_db.user_request == "Find 3 fintech leads in London and email them"
                assert run_db.status == RunStatus.CREATED
                assert run_db.idempotency_key == idempotency_key

                # 3. Canonical RUN_CREATED trace event is persisted
                traces = await uow.trace_events.list_by_run(uuid.UUID(run_id))
                assert len(traces) == 1
                assert traces[0].kind == TraceEventKind.RUN_CREATED
                assert traces[0].status == "created"
                assert traces[0].seq == 1
                await uow.commit()

            # 4. GET /runs/{run_id} returns accurate resource
            get_resp = await client.get(f"/runs/{run_id}")
            assert get_resp.status_code == 200
            get_data = get_resp.json()
            assert get_data["run_id"] == run_id
            assert get_data["status"] == "created"
            assert get_data["resumable"] is False

            # 5. Idempotent replay with same key returns 200 OK and same run
            replay_resp = await client.post(
                "/runs",
                headers={"Idempotency-Key": idempotency_key},
                json={
                    "user_request": "Find 3 fintech leads in London and email them",
                    "auto_start": False,
                    "metadata": {"test": "real_postgres"},
                },
            )
            assert replay_resp.status_code == 200
            replay_data = replay_resp.json()
            assert replay_data["run_id"] == run_id

            # 6. Idempotent replay with different body returns 409 idempotency_conflict
            conflict_resp = await client.post(
                "/runs",
                headers={"Idempotency-Key": idempotency_key},
                json={"user_request": "Completely different text"},
            )
            assert conflict_resp.status_code == 409
            conflict_data = conflict_resp.json()
            assert conflict_data["code"] == "idempotency_conflict"
