"""HITL-001 HTTP approval API test suite (§13.5, RFC 9457).

Covers:
1. GET /approvals/queue returns pending approvals in deterministic order.
2. GET /approvals/{approval_id} returns a single approval.
3. Missing approval -> 404 Problem Details with code="not_found".
4. Approve with correct args_hash -> succeeds (200) with status="approved".
5. Reject with correct args_hash and reason -> succeeds (200) with status="rejected".
6. Reject without reason -> 422 Problem Details with code="validation_error".
7. Malformed decision -> 422 Problem Details with code="validation_error".
8. Omitted args_hash is accepted (§13.5: optional); the response echoes the persisted hash.
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

API-004 (the conflict family, each driven through the real service over PostgreSQL):
20. approval_expired for a swept row and for a pending row past its TTL; state untouched.
21. approval_superseded for a genuinely superseded row; state untouched.
22. run_not_resumable for a pending approval on a terminal run; approval stays pending.
23. A same-decision replay never steals a live lease and never resumes a run that has
    moved on to a later, undecided approval.
24. A concurrent race records exactly one approval_granted event.
25. The response never carries persistence internals (superseded_by, lease fields).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.agent.state import (
    TERMINAL_RUN_STATUSES,
    ApprovalDecisionKind,
    ApprovalStatus,
    RunStatus,
)
from app.api.dependencies import get_approval_service
from app.config import AuthMode, Settings
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
from app.persistence.models import (
    TERMINAL_TRACE_EVENTS,
    ApprovalRow,
    RiskLevel,
    ToolName,
    TraceEventKind,
)
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
    read_run,
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

    def test_response_carries_only_the_public_field_set(self) -> None:
        """The resource is the redaction boundary: no `superseded_by`, no lease or
        checkpoint fields, no token, and secrets inside the preview are masked."""
        app = create_app(settings=Settings(_env_file=None))
        mock_service = AsyncMock(spec=ApprovalService)
        row = _make_sample_row(
            status=ApprovalStatus.SUPERSEDED,
            payload_preview={"to_email": "dana@northwind.example", "token": "tok-1", "n": 1},
        )
        row.superseded_by = uuid.uuid4()
        mock_service.get_approval.return_value = row
        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/approvals/{row.id}")

        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {
            "approval_id",
            "run_id",
            "step_id",
            "tool",
            "risk",
            "title",
            "summary",
            "status",
            "args_hash",
            "payload_preview",
            "created_at",
            "requested_at",
            "expires_at",
            "decided_at",
            "decided_by",
            "reason",
        }
        assert body["payload_preview"] == {
            "to_email": "dana@northwind.example",
            "token": "[redacted]",
            "n": 1,
        }
        assert "tok-1" not in resp.text
        assert str(row.superseded_by) not in resp.text

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

    def test_omitted_args_hash_is_accepted_and_response_echoes_persisted_hash(self) -> None:
        """§13.5: `args_hash` is optional. Omitting it passes `None` to the service
        unchanged (no hash is invented on the caller's behalf) and the 200 body
        echoes the hash the decision was actually bound to."""
        app = create_app(settings=Settings(_env_file=None))
        mock_service = AsyncMock(spec=ApprovalService)
        approval_id = uuid.uuid4()
        row_approved = _make_sample_row(
            approval_id=approval_id,
            status=ApprovalStatus.APPROVED,
            decided_at=datetime.now(UTC),
            decided_by="operator@example.com",
        )
        mock_service.decide_approval.return_value = DecideApprovalResult(
            approval=row_approved, is_winner=True, inspection=None
        )
        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                f"/approvals/{approval_id}/decision",
                json={"decision": "approve", "decided_by": "operator@example.com"},
            )

        assert resp.status_code == 200
        mock_service.decide_approval.assert_awaited_once_with(
            approval_id,
            decision=ApprovalDecisionKind.APPROVE,
            args_hash=None,
            decided_by="operator@example.com",
            reason=None,
        )
        assert resp.json()["args_hash"] == "hash-1234"

    @pytest.mark.parametrize("bad_hash", ["", None])
    def test_explicit_empty_or_null_args_hash(self, bad_hash: str | None) -> None:
        """An empty string is a malformed echo (422); an explicit null is the same
        as omitting the field."""
        app = create_app(settings=Settings(_env_file=None))
        mock_service = AsyncMock(spec=ApprovalService)
        mock_service.decide_approval.return_value = DecideApprovalResult(
            approval=_make_sample_row(status=ApprovalStatus.APPROVED), is_winner=True
        )
        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                f"/approvals/{uuid.uuid4()}/decision",
                json={"decision": "approve", "args_hash": bad_hash},
            )

        if bad_hash == "":
            assert resp.status_code == 422
            assert resp.json()["code"] == "validation_error"
            mock_service.decide_approval.assert_not_awaited()
        else:
            assert resp.status_code == 200
            assert mock_service.decide_approval.await_args.kwargs["args_hash"] is None

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
        cfg = Settings(_env_file=None, OPSPILOT_AUTH_MODE=AuthMode.PROXY)
        app = create_app(settings=cfg)
        mock_service = AsyncMock(spec=ApprovalService)
        app.dependency_overrides[get_approval_service] = lambda: mock_service

        with TestClient(app) as client:
            # Nothing stamped by the access proxy
            resp = client.get("/approvals/queue")
            assert resp.status_code == 401
            assert resp.json()["code"] == "policy_violation"

            # The decision route is behind the same boundary; nothing reaches the service
            denied = client.post(
                f"/approvals/{uuid.uuid4()}/decision",
                json={"decision": "approve", "args_hash": "hash-1234"},
            )
            assert denied.status_code == 401
            assert denied.headers["content-type"].startswith("application/problem+json")
            assert denied.json()["code"] == "policy_violation"
            mock_service.decide_approval.assert_not_awaited()

            # A request the access proxy stamped passes through
            mock_service.list_pending_queue.return_value = []
            resp_authed = client.get(
                "/approvals/queue",
                headers={cfg.proxy_identity_header: "operator@example.com"},
            )
            assert resp_authed.status_code == 200


@pytest.mark.integration
@pytest.mark.usefixtures("_database")
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

            # The approval resumed the graph to its end, so this settle is the
            # only place that can record the run ending: `Executor._settle`
            # never sees a run resumed by a decision. A trace that stops at the
            # approval leaves an SSE client waiting for a terminal event that
            # never arrives (§13.4, §14.2).
            run_row = await uow.agent_runs.get(run_id)
            assert run_row is not None
            assert run_row.status in TERMINAL_RUN_STATUSES
            terminal_events = [e for e in events if e.kind == TERMINAL_TRACE_EVENTS[run_row.status]]
            assert len(terminal_events) == 1, "the run ended but the trace never says so"
            assert terminal_events[0].seq == max(e.seq for e in events)
            assert terminal_events[0].payload["approval_id"] == str(approval_id)

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

        # Exactly one resume and exactly one decision event: the loser's transaction
        # rolled back and its trace append with it.
        assert harness.calls(run_id)["finish"] == 1
        async with uow_factory() as uow:
            events = await uow.trace_events.list_by_run(run_id)
            await uow.commit()
        grants = [e for e in events if e.kind == TraceEventKind.APPROVAL_GRANTED]
        assert len(grants) == 1
        assert grants[0].payload["approval_id"] == str(approval_id)


# ---------------------------------------------------------------------------
# API-004: the conflict family, each for its exact persisted condition
# ---------------------------------------------------------------------------
async def _approval_events(uow_factory: Any, run_id: uuid.UUID) -> list[Any]:
    async with uow_factory() as uow:
        events = await uow.trace_events.list_by_run(run_id)
        await uow.commit()
    return [
        e
        for e in events
        if e.kind in (TraceEventKind.APPROVAL_GRANTED, TraceEventKind.APPROVAL_REJECTED)
    ]


async def _approval_status(uow_factory: Any, approval_id: uuid.UUID) -> ApprovalStatus:
    async with uow_factory() as uow:
        row = await uow.approvals.get(approval_id, fresh=True)
        assert row is not None
        await uow.commit()
        return row.status


@pytest.mark.integration
@pytest.mark.usefixtures("_database")
class TestApprovalConflictFamilyPostgres:
    """Each 409 in §9.8/§13.1 is produced by the real `ApprovalService` over a
    persisted row in the named state, reaches the client as RFC 9457
    problem+json with its own `code`, and changes nothing: no approval
    transition, no decision event, no resume, no lease."""

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

    async def _paused(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver, clock: FixedClock
    ) -> tuple[Any, Harness, uuid.UUID, uuid.UUID, AsyncClient]:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        harness = Harness()
        graph = harness.build(checkpointer)
        approval_id = await _pause_run_for_approval(
            engine, graph, run_id, clock, step_id="s1", args_hash="hash-s1"
        )
        service = ApprovalService(
            uow_factory=uow_factory,
            driver=LangGraphRunDriver(graph),
            clock=clock,
            lease=LEASE,
            owner="api-004-worker",
        )
        app = create_app(settings=harness_settings(), approval_service=service, clock=clock)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        return uow_factory, harness, run_id, approval_id, client

    @staticmethod
    def _assert_problem(resp: Any, code: str, approval_id: uuid.UUID) -> dict[str, Any]:
        assert resp.status_code == 409
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["code"] == code
        assert body["status"] == 409
        assert body["type"] == f"https://opspilot.dev/errors/{code.replace('_', '-')}"
        assert body["title"]
        assert body["instance"] == f"/approvals/{approval_id}/decision"
        assert body["errors"] == []
        return body

    async def _assert_untouched(
        self,
        uow_factory: Any,
        harness: Harness,
        run_id: uuid.UUID,
        approval_id: uuid.UUID,
        *,
        approval_status: ApprovalStatus,
        run_status: RunStatus = RunStatus.AWAITING_APPROVAL,
    ) -> None:
        assert await _approval_status(uow_factory, approval_id) is approval_status
        run = await read_run(uow_factory, run_id)
        assert run.status is run_status
        assert run.lease_owner is None
        assert await _approval_events(uow_factory, run_id) == []
        assert harness.calls(run_id)["finish"] == 0

    async def test_swept_expired_row_returns_approval_expired(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        clock = FixedClock(T0)
        uow_factory, harness, run_id, approval_id, client = await self._paused(
            engine, checkpointer, clock
        )
        async with uow_factory() as uow:
            # What the sweeper writes (§9.8): the conditional transition off `pending`.
            swept = await uow.approvals.decide(
                approval_id, status=ApprovalStatus.EXPIRED, decided_at=clock.now()
            )
            assert swept is not None
            await uow.commit()

        async with client:
            resp = await client.post(
                f"/approvals/{approval_id}/decision",
                json={"decision": "approve", "args_hash": "hash-s1"},
            )
        body = self._assert_problem(resp, "approval_expired", approval_id)
        assert str(approval_id) in body["detail"]
        await self._assert_untouched(
            uow_factory, harness, run_id, approval_id, approval_status=ApprovalStatus.EXPIRED
        )

    async def test_pending_row_past_its_ttl_returns_approval_expired(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """The sweeper has not run yet, but the TTL has elapsed: the decision is
        refused (§9.8) and the row is left for the sweeper, not silently decided."""
        clock = FixedClock(T0)
        uow_factory, harness, run_id, approval_id, client = await self._paused(
            engine, checkpointer, clock
        )
        clock.advance(seconds=timedelta(days=1).total_seconds())

        async with client:
            for decision in ("approve", "reject"):
                resp = await client.post(
                    f"/approvals/{approval_id}/decision",
                    json={"decision": decision, "args_hash": "hash-s1", "reason": "late"},
                )
                self._assert_problem(resp, "approval_expired", approval_id)
        await self._assert_untouched(
            uow_factory, harness, run_id, approval_id, approval_status=ApprovalStatus.PENDING
        )

    async def test_superseded_row_returns_approval_superseded(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        clock = FixedClock(T0)
        uow_factory, harness, run_id, approval_id, client = await self._paused(
            engine, checkpointer, clock
        )
        async with uow_factory() as uow:
            # What a replan does (§9.3, HITL-003): the same step re-requested for
            # revised arguments supersedes the open request and chains it forward.
            revised = await uow.approvals.upsert_request(
                run_id=run_id,
                step_id="s1",
                tool=ToolName.SEND_EMAIL_MOCK,
                risk=RiskLevel.HIGH,
                title="Send email",
                summary="Send outreach email (revised)",
                payload_preview={"draft_id": "d2"},
                args_hash="hash-s1-revised",
                requested_at=clock.now(),
                expires_at=clock.now() + timedelta(days=1),
            )
            assert revised.created is True
            successor_id = revised.row.id
            await uow.commit()
        assert successor_id != approval_id

        async with client:
            resp = await client.post(
                f"/approvals/{approval_id}/decision",
                json={"decision": "approve", "args_hash": "hash-s1"},
            )
        body = self._assert_problem(resp, "approval_superseded", approval_id)
        # The chain pointer is a persistence internal; the client learns the code only.
        assert str(successor_id) not in resp.text
        assert "superseded_by" not in body
        await self._assert_untouched(
            uow_factory, harness, run_id, approval_id, approval_status=ApprovalStatus.SUPERSEDED
        )
        assert await _approval_status(uow_factory, successor_id) is ApprovalStatus.PENDING

    async def test_pending_approval_on_a_terminal_run_returns_run_not_resumable(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """The conditional decision update succeeds, the lease claim on the terminal
        run does not, and the whole transaction rolls back: the approval is still
        pending afterwards and no decision event was recorded."""
        clock = FixedClock(T0)
        uow_factory, harness, run_id, approval_id, client = await self._paused(
            engine, checkpointer, clock
        )
        async with uow_factory() as uow:
            moved = await uow.agent_runs.transition_status(
                run_id,
                expected=(RunStatus.AWAITING_APPROVAL,),
                status=RunStatus.FAILED,
                status_reason="budget_exhausted",
                finished_at=clock.now(),
            )
            assert moved is not None
            await uow.commit()

        async with client:
            resp = await client.post(
                f"/approvals/{approval_id}/decision",
                json={"decision": "approve", "args_hash": "hash-s1"},
            )
        body = self._assert_problem(resp, "run_not_resumable", approval_id)
        assert str(run_id) in body["detail"]
        await self._assert_untouched(
            uow_factory,
            harness,
            run_id,
            approval_id,
            approval_status=ApprovalStatus.PENDING,
            run_status=RunStatus.FAILED,
        )

    async def test_omitted_args_hash_decides_against_the_persisted_binding(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """§13.5: the echo is optional. Without it the decision binds to the row's
        own hash, which the 200 body echoes; a wrong echo is still refused."""
        clock = FixedClock(T0)
        uow_factory, harness, run_id, approval_id, client = await self._paused(
            engine, checkpointer, clock
        )
        async with client:
            wrong = await client.post(
                f"/approvals/{approval_id}/decision",
                json={"decision": "approve", "args_hash": "hash-s1-stale"},
            )
            self._assert_problem(wrong, "approval_superseded", approval_id)
            assert await _approval_status(uow_factory, approval_id) is ApprovalStatus.PENDING

            ok = await client.post(
                f"/approvals/{approval_id}/decision",
                json={"decision": "approve", "decided_by": "operator@opspilot.dev"},
            )
        assert ok.status_code == 200
        assert ok.json()["args_hash"] == "hash-s1"
        assert ok.json()["status"] == "approved"
        assert await _approval_status(uow_factory, approval_id) is ApprovalStatus.APPROVED
        assert harness.calls(run_id)["finish"] == 1
        assert (await read_run(uow_factory, run_id)).status is RunStatus.COMPLETED


@pytest.mark.integration
@pytest.mark.usefixtures("_database")
class TestSameDecisionReplayPostgres:
    """§9.8, §13.8: a repeated decision is idempotent by design. It records
    nothing twice and — the part a naive retry gets wrong — never hands the
    run to a second resume: not by stealing a live lease (HITL-004) and not
    by re-entering a run that has since paused on a *later* approval."""

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

    async def test_replay_never_resumes_a_run_paused_on_a_later_approval(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """Approval #1 was approved and the run has since paused on approval #2.
        A stale retry of #1 (same decision, same hash) must be a 200 no-op: the
        graph is not resumed with a decision #2 never received, #2 stays pending,
        the run stays awaiting approval with no lease, and no second
        `approval_granted` is recorded."""
        clock = FixedClock(T0)
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        harness = Harness()
        graph = harness.build(checkpointer)
        first_id = await _pause_run_for_approval(
            engine, graph, run_id, clock, step_id="s1", args_hash="hash-s1"
        )
        second_id = uuid.uuid4()
        async with uow_factory() as uow:
            # What the winner's transaction wrote for #1 …
            decided = await uow.approvals.decide(
                first_id,
                status=ApprovalStatus.APPROVED,
                decided_by="operator-1",
                decided_at=clock.now(),
            )
            assert decided is not None
            await uow.trace_events.append(
                run_id=run_id,
                kind=TraceEventKind.APPROVAL_GRANTED,
                status=ApprovalStatus.APPROVED.value,
                step_id="s1",
                payload={"approval_id": str(first_id), "decision": "approve"},
            )
            # … and the state the resumed graph left behind: paused again on s2,
            # settled to AWAITING_APPROVAL with the lease released.
            await uow.approvals.create_request(
                id=second_id,
                run_id=run_id,
                step_id="s2",
                tool=ToolName.UPDATE_CUSTOMER,
                risk=RiskLevel.HIGH,
                title="Update customer",
                summary="Change the account owner",
                payload_preview={"customer_id": "c1"},
                args_hash="hash-s2",
                requested_at=clock.now(),
                expires_at=clock.now() + timedelta(days=1),
            )
            await uow.commit()

        service = ApprovalService(
            uow_factory=uow_factory,
            driver=LangGraphRunDriver(graph),
            clock=clock,
            lease=LEASE,
            owner="api-004-replayer",
        )
        app = create_app(settings=harness_settings(), approval_service=service, clock=clock)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(2):
                resp = await client.post(
                    f"/approvals/{first_id}/decision",
                    json={"decision": "approve", "args_hash": "hash-s1"},
                )
                assert resp.status_code == 200
                assert resp.json()["status"] == "approved"
                assert resp.json()["args_hash"] == "hash-s1"

        assert harness.calls(run_id)["finish"] == 0, "a stale replay resumed the graph"
        run = await read_run(uow_factory, run_id)
        assert run.status is RunStatus.AWAITING_APPROVAL
        assert run.lease_owner is None
        assert await _approval_status(uow_factory, first_id) is ApprovalStatus.APPROVED
        assert await _approval_status(uow_factory, second_id) is ApprovalStatus.PENDING
        assert len(await _approval_events(uow_factory, run_id)) == 1

    async def test_replay_while_another_worker_holds_the_lease_is_a_no_op(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver
    ) -> None:
        """Over HTTP: the winner is still running under its lease; the replay is
        200 with the recorded decision and touches neither lease nor graph."""
        clock = FixedClock(T0)
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        harness = Harness()
        graph = harness.build(checkpointer)
        approval_id = await _pause_run_for_approval(
            engine, graph, run_id, clock, step_id="s1", args_hash="hash-s1"
        )
        async with uow_factory() as uow:
            decided = await uow.approvals.decide(
                approval_id,
                status=ApprovalStatus.REJECTED,
                decided_by="operator-1",
                decision_reason="not this draft",
                decided_at=clock.now(),
            )
            assert decided is not None
            claimed = await uow.agent_runs.acquire_lease(
                run_id,
                owner="live-winner",
                now=clock.now(),
                ttl=LEASE.ttl,
                expected=(RunStatus.AWAITING_APPROVAL,),
                status=RunStatus.RUNNING,
            )
            assert claimed is not None
            await uow.commit()

        service = ApprovalService(
            uow_factory=uow_factory,
            driver=LangGraphRunDriver(graph),
            clock=clock,
            lease=LEASE,
            owner="api-004-replayer",
        )
        app = create_app(settings=harness_settings(), approval_service=service, clock=clock)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/approvals/{approval_id}/decision",
                json={"decision": "reject", "args_hash": "hash-s1", "reason": "retry"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "rejected"
            assert resp.json()["reason"] == "not this draft"

            # The opposite decision is still a conflict, and still records nothing.
            flip = await client.post(
                f"/approvals/{approval_id}/decision",
                json={"decision": "approve", "args_hash": "hash-s1"},
            )
            assert flip.status_code == 409
            assert flip.json()["code"] == "approval_not_pending"

        assert harness.calls(run_id)["finish"] == 0
        run = await read_run(uow_factory, run_id)
        assert run.status is RunStatus.RUNNING
        assert run.lease_owner == "live-winner"
        assert await _approval_status(uow_factory, approval_id) is ApprovalStatus.REJECTED
        assert await _approval_events(uow_factory, run_id) == []
