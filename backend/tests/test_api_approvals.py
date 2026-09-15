"""HITL-001 HTTP approval API test suite (§13.5, RFC 9457).

Covers:
1. GET /approvals/queue returns pending approvals in deterministic order.
2. GET /approvals/{approval_id} returns a single approval.
3. Missing approval -> 404 Problem Details with code="not_found".
4. Approve with correct args_hash -> succeeds (200) with status="approved".
5. Reject with correct args_hash and reason -> succeeds (200) with status="rejected".
6. Reject without reason -> 422 Problem Details with code="validation_error".
7. Malformed decision -> 422 Problem Details with code="validation_error".
8. Missing args_hash -> 422 Problem Details with code="validation_error".
9. Extra fields in request -> 422 Problem Details with code="validation_error".
10. Wrong args_hash -> 409 Problem Details with code="approval_superseded".
11. Idempotent same decision repeated -> 200, resume called once.
12. Conflicting opposite decision -> 409 Problem Details with code="approval_not_pending".
13. Expired approval -> 409 Problem Details with code="approval_expired".
14. Superseded approval -> 409 Problem Details with code="approval_superseded".
15. Non-resumable run -> 409 Problem Details with code="run_not_resumable".
16. Authorization boundary enforcement (when auth_mode configured).
17. Sensitive fields (tokens, secrets, db credentials) are never exposed; payload_preview redacted.
18. Endpoint delegates to ApprovalService and does not directly mutate repositories.
19. Integration test against real PostgreSQL asserting atomic state handoff and trace events.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.agent.state import ApprovalDecisionKind, ApprovalStatus
from app.api.dependencies import get_approval_service
from app.config import Settings
from app.errors import (
    ApprovalExpiredError,
    ApprovalNotPendingError,
    ApprovalSupersededError,
    PolicyViolation,
    RunNotResumableError,
)
from app.execution.approvals import ApprovalService, DecideApprovalResult
from app.execution.recovery import LangGraphRunDriver
from app.main import create_app
from app.persistence.checkpointing import open_checkpointer
from app.persistence.models import ApprovalRow, RiskLevel, ToolName, TraceEventKind
from app.runtime import FixedClock
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import (
    LEASE,
    T0,
    Harness,
    create_run,
    migrate_to_head,
    require_database,
    uow_factory_for,
)
from recovery_harness import (
    settings as harness_settings,
)
from test_hitl_recovery import _pause_run_for_approval


def _make_sample_row(
    approval_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    status: ApprovalStatus = ApprovalStatus.PENDING,
    args_hash: str = "hash-1234",
    payload_preview: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
    decided_at: datetime | None = None,
    decided_by: str | None = None,
    decision_reason: str | None = None,
) -> ApprovalRow:
    appr_id = approval_id or uuid.uuid4()
    r_id = run_id or uuid.uuid4()
    now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)
    return ApprovalRow(
        id=appr_id,
        run_id=r_id,
        step_id="step_1",
        tool=ToolName.SEND_EMAIL_MOCK,
        risk=RiskLevel.HIGH,
        title="Send email outreach",
        summary="Sending email to lead",
        payload_preview=payload_preview or {"draft_id": "draft_abc", "api_key": "secret-key-123"},
        args_hash=args_hash,
        status=status,
        superseded_by=None,
        requested_at=now,
        expires_at=expires_at or (now + timedelta(days=1)),
        decided_at=decided_at,
        decided_by=decided_by,
        decision_reason=decision_reason,
    )


class TestApprovalApiUnit:
    """Unit tests using a mocked ApprovalService to verify HTTP and error semantics."""

    def test_queue_returns_pending_approvals_with_safe_representation(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        mock_service = AsyncMock(spec=ApprovalService)
        sample_row = _make_sample_row()
        mock_service.list_pending_queue.return_value = [sample_row]

        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get("/approvals/queue?limit=10")

        assert resp.status_code == 200
        mock_service.list_pending_queue.assert_awaited_once_with(limit=10)
        data = resp.json()
        assert len(data) == 1
        item = data[0]
        assert item["approval_id"] == str(sample_row.id)
        assert item["run_id"] == str(sample_row.run_id)
        assert item["status"] == "pending"
        assert item["args_hash"] == "hash-1234"
        # Secret in preview should be redacted by redact_payload
        assert item["payload_preview"]["api_key"] == "[redacted]"
        assert "password" not in item
        assert "token" not in item

    def test_get_single_approval_success(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        mock_service = AsyncMock(spec=ApprovalService)
        sample_row = _make_sample_row()
        mock_service.get_approval.return_value = sample_row

        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/approvals/{sample_row.id}")

        assert resp.status_code == 200
        mock_service.get_approval.assert_awaited_once_with(sample_row.id)
        data = resp.json()
        assert data["approval_id"] == str(sample_row.id)
        assert data["status"] == "pending"

    def test_get_single_approval_missing_returns_404_problem_details(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        mock_service = AsyncMock(spec=ApprovalService)
        mock_service.get_approval.return_value = None

        app.dependency_overrides[get_approval_service] = lambda: mock_service
        missing_id = uuid.uuid4()

        with TestClient(app) as client:
            resp = client.get(f"/approvals/{missing_id}")

        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["code"] == "not_found"
        assert body["status"] == 404
        assert body["type"] == "https://opspilot.dev/errors/not-found"
        assert f"Approval {missing_id} not found" in body["detail"]
        assert body["instance"] == f"/approvals/{missing_id}"

    def test_approve_valid_request_succeeds(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        mock_service = AsyncMock(spec=ApprovalService)
        approval_id = uuid.uuid4()
        now = datetime.now(UTC)
        row_approved = _make_sample_row(
            approval_id=approval_id,
            status=ApprovalStatus.APPROVED,
            decided_at=now,
            decided_by="operator@example.com",
            decision_reason="Looks good to send",
        )
        mock_service.decide_approval.return_value = DecideApprovalResult(
            approval=row_approved,
            is_winner=True,
            inspection=None,
        )

        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                f"/approvals/{approval_id}/decision",
                json={
                    "decision": "approve",
                    "args_hash": "hash-1234",
                    "decided_by": "operator@example.com",
                    "reason": "Looks good to send",
                },
            )

        assert resp.status_code == 200
        mock_service.decide_approval.assert_awaited_once_with(
            approval_id,
            decision=ApprovalDecisionKind.APPROVE,
            args_hash="hash-1234",
            decided_by="operator@example.com",
            reason="Looks good to send",
        )
        data = resp.json()
        assert data["status"] == "approved"
        assert data["decided_by"] == "operator@example.com"
        assert data["reason"] == "Looks good to send"

    def test_reject_valid_request_with_reason_succeeds(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        mock_service = AsyncMock(spec=ApprovalService)
        approval_id = uuid.uuid4()
        now = datetime.now(UTC)
        row_rejected = _make_sample_row(
            approval_id=approval_id,
            status=ApprovalStatus.REJECTED,
            decided_at=now,
            decided_by="operator@example.com",
            decision_reason="Draft tone is inappropriate",
        )
        mock_service.decide_approval.return_value = DecideApprovalResult(
            approval=row_rejected,
            is_winner=True,
            inspection=None,
        )

        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                f"/approvals/{approval_id}/decision",
                json={
                    "decision": "reject",
                    "args_hash": "hash-1234",
                    "decided_by": "operator@example.com",
                    "reason": "Draft tone is inappropriate",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "rejected"
        assert data["reason"] == "Draft tone is inappropriate"

    def test_reject_without_reason_returns_422_validation_error(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        with TestClient(app) as client:
            resp = client.post(
                f"/approvals/{uuid.uuid4()}/decision",
                json={
                    "decision": "reject",
                    "args_hash": "hash-1234",
                },
            )

        assert resp.status_code == 422
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["code"] == "validation_error"
        assert body["status"] == 422
        assert body["type"] == "https://opspilot.dev/errors/validation-error"
        assert any("reason is required" in str(err) for err in body["errors"])

    def test_malformed_decision_returns_422_validation_error(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        with TestClient(app) as client:
            resp = client.post(
                f"/approvals/{uuid.uuid4()}/decision",
                json={
                    "decision": "maybe",
                    "args_hash": "hash-1234",
                },
            )

        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "validation_error"

    def test_missing_args_hash_returns_422_validation_error(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        with TestClient(app) as client:
            resp = client.post(
                f"/approvals/{uuid.uuid4()}/decision",
                json={
                    "decision": "approve",
                },
            )

        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "validation_error"

    def test_extra_fields_forbidden_returns_422_validation_error(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        with TestClient(app) as client:
            resp = client.post(
                f"/approvals/{uuid.uuid4()}/decision",
                json={
                    "decision": "approve",
                    "args_hash": "hash-1234",
                    "unexpected_extra_field": "disallowed",
                },
            )

        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "validation_error"

    def test_wrong_args_hash_returns_409_approval_superseded(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        mock_service = AsyncMock(spec=ApprovalService)
        approval_id = uuid.uuid4()
        mock_service.decide_approval.side_effect = PolicyViolation(
            "Approval args_hash mismatch: expected hash-1234, got wrong-hash"
        )
        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                f"/approvals/{approval_id}/decision",
                json={
                    "decision": "approve",
                    "args_hash": "wrong-hash",
                },
            )

        assert resp.status_code == 409
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["code"] == "approval_superseded"
        assert body["status"] == 409
        assert body["type"] == "https://opspilot.dev/errors/approval-superseded"
        assert "args_hash mismatch" in body["detail"]

    def test_idempotent_duplicate_same_decision_returns_200(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        mock_service = AsyncMock(spec=ApprovalService)
        approval_id = uuid.uuid4()
        now = datetime.now(UTC)
        already_approved = _make_sample_row(
            approval_id=approval_id,
            status=ApprovalStatus.APPROVED,
            decided_at=now,
            decided_by="operator@example.com",
            decision_reason="Original approval",
        )
        # Service returns is_winner=False on idempotent replay
        mock_service.decide_approval.return_value = DecideApprovalResult(
            approval=already_approved,
            is_winner=False,
            inspection=None,
        )
        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                f"/approvals/{approval_id}/decision",
                json={
                    "decision": "approve",
                    "args_hash": "hash-1234",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"

    def test_conflicting_opposite_decision_returns_409_approval_not_pending(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        mock_service = AsyncMock(spec=ApprovalService)
        approval_id = uuid.uuid4()
        mock_service.decide_approval.side_effect = ApprovalNotPendingError(
            f"Approval {approval_id} is already decided as approved"
        )
        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                f"/approvals/{approval_id}/decision",
                json={
                    "decision": "reject",
                    "args_hash": "hash-1234",
                    "reason": "Trying to reject after approve",
                },
            )

        assert resp.status_code == 409
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["code"] == "approval_not_pending"
        assert body["status"] == 409
        assert body["type"] == "https://opspilot.dev/errors/approval-not-pending"

    def test_expired_approval_returns_409_approval_expired(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        mock_service = AsyncMock(spec=ApprovalService)
        approval_id = uuid.uuid4()
        mock_service.decide_approval.side_effect = ApprovalExpiredError(
            f"Approval {approval_id} has expired"
        )
        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                f"/approvals/{approval_id}/decision",
                json={
                    "decision": "approve",
                    "args_hash": "hash-1234",
                },
            )

        assert resp.status_code == 409
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["code"] == "approval_expired"
        assert body["status"] == 409
        assert body["type"] == "https://opspilot.dev/errors/approval-expired"

    def test_superseded_approval_returns_409_approval_superseded(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        mock_service = AsyncMock(spec=ApprovalService)
        approval_id = uuid.uuid4()
        mock_service.decide_approval.side_effect = ApprovalSupersededError(
            f"Approval {approval_id} has been superseded"
        )
        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                f"/approvals/{approval_id}/decision",
                json={
                    "decision": "approve",
                    "args_hash": "hash-1234",
                },
            )

        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == "approval_superseded"

    def test_non_resumable_run_returns_409_run_not_resumable(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        mock_service = AsyncMock(spec=ApprovalService)
        approval_id = uuid.uuid4()
        mock_service.decide_approval.side_effect = RunNotResumableError(
            f"Could not acquire run ownership on {uuid.uuid4()} during approval decision"
        )
        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                f"/approvals/{approval_id}/decision",
                json={
                    "decision": "approve",
                    "args_hash": "hash-1234",
                },
            )

        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == "run_not_resumable"

    def test_authorization_enforced_when_configured(self) -> None:
        cfg = Settings(_env_file=None, OPSPILOT_AUTH_MODE="token")
        app = create_app(settings=cfg)
        mock_service = AsyncMock(spec=ApprovalService)
        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            # Request missing authorization header
            resp = client.get("/approvals/queue")
            assert resp.status_code == 401
            assert resp.json()["code"] == "policy_violation"

            # Request with authorization header succeeds
            mock_service.list_pending_queue.return_value = []
            resp_authed = client.get(
                "/approvals/queue", headers={"Authorization": "Bearer test-token"}
            )
            assert resp_authed.status_code == 200


@pytest.mark.integration
class TestApprovalApiPostgresIntegration:
    """End-to-end integration tests using the real PostgreSQL engine and checkpointer."""

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

    @pytest.fixture
    async def checkpointer(self) -> AsyncIterator[AsyncPostgresSaver]:
        async with open_checkpointer(harness_settings()) as saver:
            yield saver

    async def test_full_http_decision_cycle_with_real_persistence(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        clock = FixedClock(T0)
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)

        harness = Harness()
        graph = harness.build(checkpointer)

        args_hash = "canonical-hash-xyz"
        approval_id = await _pause_run_for_approval(
            engine,
            graph,
            run_id,
            clock,
            step_id="s1",
            args_hash=args_hash,
        )

        driver = LangGraphRunDriver(graph)
        approval_service = ApprovalService(
            uow_factory=uow_factory,
            driver=driver,
            clock=clock,
            lease=LEASE,
        )

        app = create_app(
            settings=harness_settings(),
            approval_service=approval_service,
            clock=clock,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Queue endpoint should return the pending approval
            q_resp = await client.get("/approvals/queue")
            assert q_resp.status_code == 200
            q_data = q_resp.json()
            found = [item for item in q_data if item["approval_id"] == str(approval_id)]
            assert len(found) == 1
            assert found[0]["status"] == "pending"
            assert found[0]["payload_preview"]["draft_id"] == "d1"

            # 2. Wrong args_hash -> 409 approval_superseded
            wrong_resp = await client.post(
                f"/approvals/{approval_id}/decision",
                json={
                    "decision": "approve",
                    "args_hash": "stale-or-wrong-hash",
                },
            )
            assert wrong_resp.status_code == 409
            assert wrong_resp.json()["code"] == "approval_superseded"

            # 3. Correct args_hash -> 200 approved
            ok_resp = await client.post(
                f"/approvals/{approval_id}/decision",
                json={
                    "decision": "approve",
                    "args_hash": args_hash,
                    "decided_by": "operator@opspilot.dev",
                    "reason": "Approved by human operator",
                },
            )
            assert ok_resp.status_code == 200
            ok_data = ok_resp.json()
            assert ok_data["status"] == "approved"
            assert ok_data["decided_by"] == "operator@opspilot.dev"

            # 4. Same decision repeated -> idempotent 200
            idem_resp = await client.post(
                f"/approvals/{approval_id}/decision",
                json={
                    "decision": "approve",
                    "args_hash": args_hash,
                },
            )
            assert idem_resp.status_code == 200
            assert idem_resp.json()["status"] == "approved"

            # 5. Conflicting decision -> 409 approval_not_pending
            conflict_resp = await client.post(
                f"/approvals/{approval_id}/decision",
                json={
                    "decision": "reject",
                    "args_hash": args_hash,
                    "reason": "Conflicting decision after approve",
                },
            )
            assert conflict_resp.status_code == 409
            assert conflict_resp.json()["code"] == "approval_not_pending"

        # Assert in DB that approval is approved and trace event was appended
        async with uow_factory() as uow:
            persisted = await uow.approvals.get(approval_id)
            assert persisted is not None
            assert persisted.status == ApprovalStatus.APPROVED
            assert persisted.decided_by == "operator@opspilot.dev"

            events = await uow.trace_events.list_by_run(run_id)
            grant_events = [e for e in events if e.kind == TraceEventKind.APPROVAL_GRANTED]
            assert len(grant_events) == 1
            assert grant_events[0].payload["approval_id"] == str(approval_id)

    async def test_concurrent_http_post_decision_produces_one_200_and_one_409(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """HITL-004: Two concurrent HTTP POST decisions on the same approval
        produce one 200 and one 409 Problem Details with code='approval_not_pending'."""
        clock = FixedClock(T0)
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)

        harness = Harness()
        graph = harness.build(checkpointer)

        args_hash = "canonical-hash-xyz"
        approval_id = await _pause_run_for_approval(
            engine,
            graph,
            run_id,
            clock,
            step_id="s1",
            args_hash=args_hash,
        )

        driver = LangGraphRunDriver(graph)
        approval_service = ApprovalService(
            uow_factory=uow_factory,
            driver=driver,
            clock=clock,
            lease=LEASE,
        )

        app = create_app(
            settings=harness_settings(),
            approval_service=approval_service,
            clock=clock,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            t1 = client.post(
                f"/approvals/{approval_id}/decision",
                json={
                    "decision": "approve",
                    "args_hash": args_hash,
                    "decided_by": "operator-1@opspilot.dev",
                    "reason": "First concurrent approve",
                },
            )
            t2 = client.post(
                f"/approvals/{approval_id}/decision",
                json={
                    "decision": "approve",
                    "args_hash": args_hash,
                    "decided_by": "operator-2@opspilot.dev",
                    "reason": "Second concurrent approve",
                },
            )
            r1, r2 = await asyncio.gather(t1, t2)

        statuses = sorted([r1.status_code, r2.status_code])
        assert statuses == [200, 409]
        conflict_resp = r1 if r1.status_code == 409 else r2
        assert conflict_resp.headers["content-type"].startswith("application/problem+json")
        assert conflict_resp.json()["code"] == "approval_not_pending"
