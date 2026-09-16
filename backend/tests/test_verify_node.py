"""Integration tests for the `verify` graph node (§11, VERIFY-001).

Tests the integration between `NodeHandlers.verify`, `route_after_execute`,
`route_after_verify`, `recover`, and downstream terminal responses.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from app.agent.nodes import NodeHandlers
from app.agent.state import (
    AgentError,
    AgentState,
    Plan,
    PlanStep,
    RunStatus,
    StepStatus,
    ToolCall,
    ToolResult,
    VerificationResult,
    VerificationStatus,
)
from app.errors import ErrorClass, NotFoundError
from app.integrations.ports import Adapters, Customer, CustomerPort, MailPort, OutboxRecord
from app.runtime import FixedClock
from app.tools.contracts import ToolName

pytestmark = [pytest.mark.unit]

NOW = datetime.now(UTC)


def _make_adapters(
    mail_port: MailPort | None = None,
    customer_port: CustomerPort | None = None,
) -> Adapters:
    adapters = AsyncMock(spec=Adapters)
    adapters.mail = mail_port or AsyncMock(spec=MailPort)
    mail_count_rv = getattr(adapters.mail.count_outbox, "return_value", None)
    if mail_count_rv is None or isinstance(mail_count_rv, AsyncMock):
        adapters.mail.count_outbox.return_value = 1
    adapters.customers = customer_port or AsyncMock(spec=CustomerPort)
    adapters.drafts = AsyncMock()
    adapters.leads = AsyncMock()
    adapters.companies = AsyncMock()
    adapters.content = AsyncMock()
    return adapters


class TestVerifyNodeExecution:
    @pytest.mark.asyncio
    async def test_invariant_verification_passed_routes_to_decide(self) -> None:
        """search_leads succeeds and satisfies invariants -> routes to decide."""
        handlers = NodeHandlers(clock=FixedClock(NOW))
        step_id = "s1"
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(
                    step_id=step_id,
                    tool=ToolName.SEARCH_LEADS,
                    args={"status": "new", "limit": 10},
                    status=StepStatus.SUCCEEDED,
                )
            ],
        )

        state: AgentState = {
            "run_id": str(uuid.uuid4()),
            "current_step_id": step_id,
            "plan": plan,
            "tool_results": {
                step_id: ToolResult(
                    step_id=step_id,
                    tool=ToolName.SEARCH_LEADS,
                    output={
                        "leads": [{"lead_id": "l_1", "status": "new", "company_id": "c_1"}],
                        "total_matched": 1,
                    },
                    produced_at=NOW,
                )
            },
            "tool_calls": [
                ToolCall(
                    step_id=step_id,
                    tool=ToolName.SEARCH_LEADS,
                    attempt=1,
                    args_hash="hash_1",
                    status="succeeded",
                )
            ],
        }

        delta = await handlers.verify(state)
        assert "verification_result" in delta
        res: VerificationResult = delta["verification_result"][step_id]
        assert res.status == VerificationStatus.PASSED
        assert res.mode == "invariant"

        # Check routing
        state["verification_result"] = delta["verification_result"]
        assert handlers.route_after_verify(state) == "decide"

    @pytest.mark.asyncio
    async def test_readback_verification_passed_for_send_email_mock(self) -> None:
        """send_email_mock matches outbox record -> verify records PASSED -> routes to decide."""
        mock_mail = AsyncMock(spec=MailPort)
        mock_mail.get_outbox.return_value = OutboxRecord(
            outbox_id="out_1",
            message_id="msg_100",
            draft_id="drf_1",
            to_email="test@example.com",
            subject="Subj",
            body="Body",
            status="sent",
            provider="mock",
            idempotency_key="idem_key_1",
            sent_at=NOW,
        )
        adapters = _make_adapters(mail_port=mock_mail)
        handlers = NodeHandlers(clock=FixedClock(NOW), adapters=adapters)

        step_id = "s2"
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(
                    step_id=step_id,
                    tool=ToolName.SEND_EMAIL_MOCK,
                    args={"to_email": "test@example.com", "draft_id": "drf_1"},
                    status=StepStatus.SUCCEEDED,
                )
            ],
        )

        state: AgentState = {
            "run_id": str(uuid.uuid4()),
            "current_step_id": step_id,
            "plan": plan,
            "tool_results": {
                step_id: ToolResult(
                    step_id=step_id,
                    tool=ToolName.SEND_EMAIL_MOCK,
                    output={"message_id": "msg_100"},
                    produced_at=NOW,
                )
            },
            "tool_calls": [
                ToolCall(
                    step_id=step_id,
                    tool=ToolName.SEND_EMAIL_MOCK,
                    attempt=1,
                    args_hash="hash_2",
                    idempotency_key="idem_key_1",
                    status="succeeded",
                )
            ],
        }

        delta = await handlers.verify(state)
        assert "verification_result" in delta
        res: VerificationResult = delta["verification_result"][step_id]
        assert res.status == VerificationStatus.PASSED
        assert res.mode == "readback"
        assert "errors" not in delta

        state["verification_result"] = delta["verification_result"]
        assert handlers.route_after_verify(state) == "decide"

    @pytest.mark.asyncio
    async def test_tool_lying_missing_outbox_fails_verification_and_routes_to_recover(
        self,
    ) -> None:
        """Tool returns message_id, but outbox record is missing (tool lied / dropped write).
        verify records FAILED, appends VERIFICATION_FAILED error, and routes to recover.
        """
        mock_mail = AsyncMock(spec=MailPort)
        mock_mail.get_outbox.side_effect = NotFoundError("Message not found: msg_phantom")
        adapters = _make_adapters(mail_port=mock_mail)
        handlers = NodeHandlers(clock=FixedClock(NOW), adapters=adapters)

        step_id = "s3"
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(
                    step_id=step_id,
                    tool=ToolName.SEND_EMAIL_MOCK,
                    args={"to_email": "test@example.com", "draft_id": "drf_1"},
                    status=StepStatus.SUCCEEDED,
                )
            ],
        )

        state: AgentState = {
            "run_id": str(uuid.uuid4()),
            "current_step_id": step_id,
            "plan": plan,
            "tool_results": {
                step_id: ToolResult(
                    step_id=step_id,
                    tool=ToolName.SEND_EMAIL_MOCK,
                    output={"message_id": "msg_phantom"},
                    produced_at=NOW,
                )
            },
            "tool_calls": [
                ToolCall(
                    step_id=step_id,
                    tool=ToolName.SEND_EMAIL_MOCK,
                    attempt=1,
                    args_hash="hash_3",
                    status="succeeded",
                )
            ],
        }

        delta = await handlers.verify(state)
        assert "verification_result" in delta
        res: VerificationResult = delta["verification_result"][step_id]
        assert res.status == VerificationStatus.FAILED

        # Asserts AgentError(error_class=VERIFICATION_FAILED) is recorded
        assert "errors" in delta
        assert len(delta["errors"]) == 1
        agent_err: AgentError = delta["errors"][0]
        assert agent_err.error_class == ErrorClass.VERIFICATION_FAILED
        assert agent_err.step_id == step_id

        # Route after verify must transition to recover
        state["verification_result"] = delta["verification_result"]
        state["errors"] = delta["errors"]
        assert handlers.route_after_verify(state) == "recover"

    @pytest.mark.asyncio
    async def test_transient_port_error_classified_as_transient_not_verification_failed(
        self,
    ) -> None:
        """Architecture §11.4: Inability to check is TRANSIENT, not a verification failure."""
        mock_cust = AsyncMock(spec=CustomerPort)
        mock_cust.get.side_effect = ConnectionError("DB connection lost")
        adapters = _make_adapters(customer_port=mock_cust)
        handlers = NodeHandlers(clock=FixedClock(NOW), adapters=adapters)

        step_id = "s4"
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(
                    step_id=step_id,
                    tool=ToolName.UPDATE_CUSTOMER,
                    args={
                        "customer_id": "cust_1",
                        "expected_version": 1,
                        "patch": {"plan": "enterprise"},
                    },
                    status=StepStatus.SUCCEEDED,
                )
            ],
        )

        state: AgentState = {
            "run_id": str(uuid.uuid4()),
            "current_step_id": step_id,
            "plan": plan,
            "tool_results": {
                step_id: ToolResult(
                    step_id=step_id,
                    tool=ToolName.UPDATE_CUSTOMER,
                    output={"customer_id": "cust_1", "version": 2},
                    produced_at=NOW,
                )
            },
            "tool_calls": [
                ToolCall(
                    step_id=step_id,
                    tool=ToolName.UPDATE_CUSTOMER,
                    attempt=1,
                    args_hash="hash_4",
                    status="succeeded",
                )
            ],
        }

        delta = await handlers.verify(state)
        assert "errors" in delta
        err: AgentError = delta["errors"][0]
        # Must be classified as TRANSIENT, NOT VERIFICATION_FAILED
        assert err.error_class == ErrorClass.TRANSIENT

        state["verification_result"] = delta["verification_result"]
        state["errors"] = delta["errors"]
        assert handlers.route_after_verify(state) == "recover"

    @pytest.mark.asyncio
    async def test_cancellation_at_verify_node_entry(self) -> None:
        """If run was cancelled before entering verify, verify halts to failed(cancelled)."""
        handlers = NodeHandlers(clock=FixedClock(NOW))
        state: AgentState = {
            "run_id": str(uuid.uuid4()),
            "status": RunStatus.RUNNING,
            "status_reason": "cancelled",
        }
        delta = await handlers.verify(state)
        assert delta["status"] == RunStatus.FAILED
        assert delta["status_reason"] == "cancelled"

    @pytest.mark.asyncio
    async def test_duplicate_outbox_rows_fails_verification_and_routes_to_recover(
        self,
    ) -> None:
        """VERIFY-002 Integration: Outbox has 2 rows for idempotency key -> routes to recover."""
        mock_mail = AsyncMock(spec=MailPort)
        mock_mail.get_outbox.return_value = OutboxRecord(
            outbox_id="out_1",
            message_id="msg_100",
            draft_id="drf_1",
            to_email="test@example.com",
            subject="Subj",
            body="Body",
            status="sent",
            provider="mock",
            idempotency_key="idem_key_1",
            sent_at=NOW,
        )
        mock_mail.count_outbox.return_value = 2  # Duplicate detected!
        adapters = _make_adapters(mail_port=mock_mail)
        handlers = NodeHandlers(clock=FixedClock(NOW), adapters=adapters)

        step_id = "s_dup"
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(
                    step_id=step_id,
                    tool=ToolName.SEND_EMAIL_MOCK,
                    args={"to_email": "test@example.com", "draft_id": "drf_1"},
                    status=StepStatus.SUCCEEDED,
                )
            ],
        )

        state: AgentState = {
            "run_id": str(uuid.uuid4()),
            "current_step_id": step_id,
            "plan": plan,
            "tool_results": {
                step_id: ToolResult(
                    step_id=step_id,
                    tool=ToolName.SEND_EMAIL_MOCK,
                    output={"message_id": "msg_100"},
                    produced_at=NOW,
                )
            },
            "tool_calls": [
                ToolCall(
                    step_id=step_id,
                    tool=ToolName.SEND_EMAIL_MOCK,
                    attempt=1,
                    args_hash="hash_dup",
                    idempotency_key="idem_key_1",
                    status="succeeded",
                )
            ],
        }

        delta = await handlers.verify(state)
        assert "verification_result" in delta
        res = delta["verification_result"][step_id]
        assert res.status == VerificationStatus.FAILED

        state["verification_result"] = delta["verification_result"]
        state["errors"] = delta["errors"]
        assert handlers.route_after_verify(state) == "recover"

    @pytest.mark.asyncio
    async def test_customer_untouched_field_tampered_fails_verification_and_routes_to_recover(
        self,
    ) -> None:
        """VERIFY-002 Integration: Customer update tampered untouched field -> routes to recover."""
        mock_cust = AsyncMock(spec=CustomerPort)
        mock_cust.get.return_value = Customer(
            customer_id="cust_1",
            account_name="Compromised Corp",  # Tampered! Was "Acme Corp"
            primary_contact="Jane Doe",
            email="jane@acme.com",
            status="active",
            plan="enterprise",
            version=2,
            updated_at=NOW,
        )
        adapters = _make_adapters(customer_port=mock_cust)
        handlers = NodeHandlers(clock=FixedClock(NOW), adapters=adapters)

        step_id = "s_cust"
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(
                    step_id="step_prior_get",
                    tool=ToolName.GET_CUSTOMER,
                    args={"customer_id": "cust_1"},
                    status=StepStatus.SUCCEEDED,
                ),
                PlanStep(
                    step_id=step_id,
                    tool=ToolName.UPDATE_CUSTOMER,
                    args={
                        "customer_id": "cust_1",
                        "expected_version": 1,
                        "patch": {"plan": "enterprise"},
                    },
                    status=StepStatus.SUCCEEDED,
                ),
            ],
        )

        state: AgentState = {
            "run_id": str(uuid.uuid4()),
            "current_step_id": step_id,
            "plan": plan,
            "tool_results": {
                "step_prior_get": ToolResult(
                    step_id="step_prior_get",
                    tool=ToolName.GET_CUSTOMER,
                    output={
                        "customer": {
                            "customer_id": "cust_1",
                            "account_name": "Acme Corp",
                            "primary_contact": "Jane Doe",
                            "email": "jane@acme.com",
                            "status": "active",
                            "plan": "starter",
                            "version": 1,
                            "updated_at": NOW.isoformat(),
                        }
                    },
                    produced_at=NOW,
                ),
                step_id: ToolResult(
                    step_id=step_id,
                    tool=ToolName.UPDATE_CUSTOMER,
                    output={"customer_id": "cust_1", "version": 2},
                    produced_at=NOW,
                ),
            },
            "tool_calls": [
                ToolCall(
                    step_id=step_id,
                    tool=ToolName.UPDATE_CUSTOMER,
                    attempt=1,
                    args_hash="hash_cust",
                    status="succeeded",
                )
            ],
        }

        delta = await handlers.verify(state)
        assert "verification_result" in delta
        res = delta["verification_result"][step_id]
        assert res.status == VerificationStatus.FAILED

        state["verification_result"] = delta["verification_result"]
        state["errors"] = delta["errors"]
        assert handlers.route_after_verify(state) == "recover"
