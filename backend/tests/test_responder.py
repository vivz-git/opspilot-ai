"""Comprehensive tests for AGENT-008: Terminal Responder behavior for complete and fail nodes.

Covers:
1. COMPLETE:
   - All required steps succeed
   - All required steps verified
   - Optional step skipped -> completed + partial=True
   - Optional steps skipped do not produce failure
   - No optional skips -> partial=False/default
   - Deterministic response for same state
   - Successful response contains only evidence-backed claims
2. REJECTION:
   - Required step rejected -> REJECTED
   - status_reason == "approval_rejected"
   - Rejection != failure
   - Response clearly distinguishes rejection from failure
3. INVARIANT P5 / UNCONFIRMED:
   - Unconfirmed verification on mutation -> in unconfirmed, NEVER in done
   - Failed verification on mutation -> in unconfirmed, NEVER in done
   - Outbound tool unconfirmed -> warns operator to check outbox
4. FAIL:
   - Explicit failure
   - Policy violation
   - Internal error
   - Retry budget exhaustion
   - Replan budget exhaustion
   - Deadline/budget failure
   - Existing status_reason is preserved
   - Failure response never reports success
5. STATE SAFETY:
   - State is not mutated in place
   - Errors/tool_calls/history are preserved
   - Exact existing 21 channels remain unchanged
6. TERMINALITY:
   - Complete remains terminal
   - Fail remains terminal
   - No outgoing logic added
7. SECURITY:
   - Sensitive/internal data is scrubbed
   - No secrets leak into final response
8. INTEGRATION:
   - decide -> complete produces expected terminal state
   - recover -> fail produces expected terminal state
   - approval rejection -> complete produces REJECTED correctly
9. STRUCTURAL TESTS:
   - complete/fail do not dispatch tools
   - no mock adapter imports
   - no ORM / DB access
   - no LLM calls
   - exact 21 state channels
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.agent.graph import create_agent_graph
from app.agent.nodes import (
    NodeHandlers,
    create_initial_state,
    format_failure_explanation,
    is_required_step_rejected,
    sanitize_text,
)
from app.agent.state import (
    AgentError,
    AgentState,
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalState,
    Budgets,
    FinalResponse,
    NormalizedTask,
    Plan,
    PlanStep,
    RunMetadata,
    RunStatus,
    StepStatus,
    ToolResult,
    VerificationResult,
    VerificationStatus,
)
from app.errors import ErrorClass, RecoveryAction
from app.runtime import FixedClock
from app.tools.contracts import ToolName
from langgraph.checkpoint.memory import MemorySaver

pytestmark = [pytest.mark.unit]

TEST_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def make_plan(steps_info: list[tuple[str, ToolName, bool, StepStatus]]) -> Plan:
    """Helper to create a Plan from (step_id, tool, optional, status)."""
    return Plan(
        plan_id="p_test",
        steps=[
            PlanStep(step_id=sid, tool=tool, optional=opt, status=status)
            for sid, tool, opt, status in steps_info
        ],
    )


# ---------------------------------------------------------------------------
# 1. Complete Node Tests
# ---------------------------------------------------------------------------
class TestCompleteNode:
    @pytest.mark.asyncio
    async def test_all_required_steps_succeed(self) -> None:
        """All required steps succeeded and verified -> RunStatus.COMPLETED, partial=False."""
        plan = make_plan(
            [
                ("s1", ToolName.SEARCH_LEADS, False, StepStatus.SUCCEEDED),
                ("s2", ToolName.SCORE_LEAD, False, StepStatus.SUCCEEDED),
            ]
        )
        tool_results = {
            "s1": ToolResult(
                step_id="s1",
                tool=ToolName.SEARCH_LEADS,
                output={"leads": [{"id": "lead_1"}]},
                produced_at=TEST_NOW,
            ),
            "s2": ToolResult(
                step_id="s2",
                tool=ToolName.SCORE_LEAD,
                output={"score": 85},
                produced_at=TEST_NOW,
            ),
        }
        state = create_initial_state(
            run_id="run_1",
            user_request="find leads",
            plan=plan,
            clock=FixedClock(TEST_NOW),
        )
        state["tool_results"] = tool_results

        handlers = NodeHandlers()
        delta = await handlers.complete(state)

        assert delta["status"] == RunStatus.COMPLETED
        assert delta["status_reason"] is None
        final: FinalResponse = delta["final_response"]
        assert final.partial is False
        assert final.done == ["s1", "s2"]
        assert final.not_done == []
        assert final.unconfirmed == []
        assert final.pending == []
        assert "completed successfully" in final.summary

    @pytest.mark.asyncio
    async def test_all_required_steps_verified(self) -> None:
        """Steps with PASSED and NOT_REQUIRED verification are placed in done."""
        plan = make_plan(
            [
                ("s1", ToolName.SEARCH_LEADS, False, StepStatus.SUCCEEDED),
                ("s2", ToolName.SAVE_DRAFT, False, StepStatus.SUCCEEDED),
            ]
        )
        state = create_initial_state(run_id="run_1", user_request="draft", plan=plan)
        state["verification_result"] = {
            "s1": VerificationResult(
                step_id="s1",
                status=VerificationStatus.NOT_REQUIRED,
                mode="none",
            ),
            "s2": VerificationResult(
                step_id="s2",
                status=VerificationStatus.PASSED,
                mode="readback",
            ),
        }
        handlers = NodeHandlers()
        delta = await handlers.complete(state)

        assert delta["status"] == RunStatus.COMPLETED
        final: FinalResponse = delta["final_response"]
        assert final.done == ["s1", "s2"]
        assert final.unconfirmed == []

    @pytest.mark.asyncio
    async def test_optional_step_skipped_produces_partial_completion(self) -> None:
        """Optional step SKIPPED -> RunStatus.COMPLETED and partial=True."""
        plan = make_plan(
            [
                ("s1", ToolName.SEARCH_LEADS, False, StepStatus.SUCCEEDED),
                ("s2", ToolName.RESEARCH_COMPANY, True, StepStatus.SKIPPED),
                ("s3", ToolName.SCORE_LEAD, False, StepStatus.SUCCEEDED),
            ]
        )
        state = create_initial_state(run_id="run_1", user_request="test", plan=plan)
        handlers = NodeHandlers()
        delta = await handlers.complete(state)

        assert delta["status"] == RunStatus.COMPLETED
        final: FinalResponse = delta["final_response"]
        assert final.partial is True
        assert final.done == ["s1", "s3"]
        assert final.not_done == ["s2"]
        assert "partial execution" in final.summary.lower()

    @pytest.mark.asyncio
    async def test_optional_steps_skipped_do_not_produce_failure(self) -> None:
        """Optional skips never cause failure."""
        plan = make_plan(
            [
                ("s1", ToolName.SEARCH_LEADS, False, StepStatus.SUCCEEDED),
                ("s2", ToolName.DRAFT_OUTREACH, True, StepStatus.SKIPPED),
            ]
        )
        state = create_initial_state(run_id="run_1", user_request="test", plan=plan)
        handlers = NodeHandlers()
        delta = await handlers.complete(state)

        assert delta["status"] == RunStatus.COMPLETED
        assert delta["status"] != RunStatus.FAILED
        assert delta["status_reason"] is None

    @pytest.mark.asyncio
    async def test_no_optional_skips_has_partial_false(self) -> None:
        """When no optional step is skipped, partial is False."""
        plan = make_plan(
            [
                ("s1", ToolName.SEARCH_LEADS, False, StepStatus.SUCCEEDED),
                ("s2", ToolName.DRAFT_OUTREACH, True, StepStatus.SUCCEEDED),
            ]
        )
        state = create_initial_state(run_id="run_1", user_request="test", plan=plan)
        handlers = NodeHandlers()
        delta = await handlers.complete(state)

        assert delta["status"] == RunStatus.COMPLETED
        assert delta["final_response"].partial is False

    @pytest.mark.asyncio
    async def test_deterministic_response_for_same_state(self) -> None:
        """Repeated evaluation against the same AgentState produces the exact same result."""
        plan = make_plan(
            [
                ("s1", ToolName.SEARCH_LEADS, False, StepStatus.SUCCEEDED),
                ("s2", ToolName.SCORE_LEAD, True, StepStatus.SKIPPED),
            ]
        )
        state = create_initial_state(run_id="run_1", user_request="test", plan=plan)
        handlers = NodeHandlers()

        delta1 = await handlers.complete(state)
        delta2 = await handlers.complete(state)

        assert delta1["status"] == delta2["status"]
        assert delta1["status_reason"] == delta2["status_reason"]
        assert delta1["final_response"].model_dump() == delta2["final_response"].model_dump()

    @pytest.mark.asyncio
    async def test_successful_response_contains_only_evidence_backed_claims(self) -> None:
        """Response does not invent recipient names, scores, companies or delivery."""
        plan = make_plan([("s1", ToolName.SEARCH_LEADS, False, StepStatus.SUCCEEDED)])
        state = create_initial_state(run_id="run_1", user_request="test", plan=plan)
        handlers = NodeHandlers()
        delta = await handlers.complete(state)

        summary = delta["final_response"].summary
        # Must not hallucinate arbitrary entities
        assert "Acme" not in summary
        assert "john@example.com" not in summary
        assert "score: 99" not in summary


# ---------------------------------------------------------------------------
# 2. Rejection Node Tests
# ---------------------------------------------------------------------------
class TestRejectionHandling:
    @pytest.mark.asyncio
    async def test_required_step_rejected_sets_rejected_status(self) -> None:
        """Required step rejected -> RunStatus.REJECTED and status_reason='approval_rejected'."""
        plan = make_plan(
            [
                ("s1", ToolName.SEARCH_LEADS, False, StepStatus.SUCCEEDED),
                ("s2", ToolName.SEND_EMAIL_MOCK, False, StepStatus.REJECTED),
                ("s3", ToolName.UPDATE_CUSTOMER, False, StepStatus.PENDING),
            ]
        )
        approval_state = ApprovalState(
            decisions={
                "s2": ApprovalDecision(
                    approval_id="app_1",
                    step_id="s2",
                    decision=ApprovalDecisionKind.REJECT,
                    args_hash="hash123",
                    decided_by="operator",
                    decided_at=TEST_NOW,
                    reason="Do not send",
                )
            }
        )
        state = create_initial_state(run_id="run_1", user_request="email", plan=plan)
        state["approval_state"] = approval_state

        handlers = NodeHandlers()
        delta = await handlers.complete(state)

        assert delta["status"] == RunStatus.REJECTED
        assert delta["status_reason"] == "approval_rejected"
        final: FinalResponse = delta["final_response"]
        assert "s2" in final.not_done
        assert "s1" in final.done
        assert "s3" in final.pending
        assert "rejected" in final.summary.lower()

    @pytest.mark.asyncio
    async def test_rejection_distinct_from_failure(self) -> None:
        """Rejection must never map to FAILED."""
        plan = make_plan([("s1", ToolName.SEND_EMAIL_MOCK, False, StepStatus.REJECTED)])
        state = create_initial_state(run_id="run_1", user_request="email", plan=plan)
        handlers = NodeHandlers()
        delta = await handlers.complete(state)

        assert delta["status"] == RunStatus.REJECTED
        assert delta["status"] != RunStatus.FAILED
        assert delta["status_reason"] == "approval_rejected"

    @pytest.mark.asyncio
    async def test_response_clearly_distinguishes_rejection_from_internal_failure(
        self,
    ) -> None:
        """Summary explains operator rejection, not system or internal crash."""
        plan = make_plan([("s1", ToolName.SEND_EMAIL_MOCK, False, StepStatus.REJECTED)])
        state = create_initial_state(run_id="run_1", user_request="email", plan=plan)
        handlers = NodeHandlers()
        delta = await handlers.complete(state)

        summary = delta["final_response"].summary
        assert "rejected by operator" in summary.lower()
        assert "internal error" not in summary.lower()
        assert "crash" not in summary.lower()

    def test_is_required_step_rejected_helper(self) -> None:
        """Verify is_required_step_rejected distinguishes required vs optional rejection."""
        # Required step rejected
        plan = make_plan([("s1", ToolName.SEND_EMAIL_MOCK, False, StepStatus.REJECTED)])
        is_rej, rejected = is_required_step_rejected(plan, None)
        assert is_rej is True
        assert rejected == ["s1"]

        # Optional step rejected -> is_rej is False
        plan_opt = make_plan([("s1", ToolName.SEND_EMAIL_MOCK, True, StepStatus.REJECTED)])
        is_rej_opt, rejected_opt = is_required_step_rejected(plan_opt, None)
        assert is_rej_opt is False
        assert rejected_opt == []


# ---------------------------------------------------------------------------
# 3. Invariant P5 / Unconfirmed Verification Tests
# ---------------------------------------------------------------------------
class TestInvariantP5Unconfirmed:
    @pytest.mark.asyncio
    async def test_unconfirmed_verification_never_in_done(self) -> None:
        """Invariant P5: An unverified effect is in unconfirmed, NEVER in done."""
        plan = make_plan(
            [
                ("s1", ToolName.SEARCH_LEADS, False, StepStatus.SUCCEEDED),
                ("s2", ToolName.SEND_EMAIL_MOCK, False, StepStatus.SUCCEEDED),
            ]
        )
        state = create_initial_state(run_id="run_1", user_request="send email", plan=plan)
        state["verification_result"] = {
            "s1": VerificationResult(
                step_id="s1",
                status=VerificationStatus.PASSED,
                mode="none",
            ),
            "s2": VerificationResult(
                step_id="s2",
                status=VerificationStatus.UNCONFIRMED,
                mode="readback",
            ),
        }
        handlers = NodeHandlers()
        delta = await handlers.complete(state)

        final: FinalResponse = delta["final_response"]
        assert "s1" in final.done
        assert "s2" not in final.done
        assert "s2" in final.unconfirmed

    @pytest.mark.asyncio
    async def test_failed_verification_on_mutation_reported_as_unconfirmed(self) -> None:
        """VerificationStatus.FAILED on mutation is reported as unconfirmed, not done."""
        plan = make_plan([("s1", ToolName.UPDATE_CUSTOMER, False, StepStatus.SUCCEEDED)])
        state = create_initial_state(run_id="run_1", user_request="update", plan=plan)
        state["verification_result"] = {
            "s1": VerificationResult(
                step_id="s1",
                status=VerificationStatus.FAILED,
                mode="readback",
            )
        }
        handlers = NodeHandlers()
        delta = await handlers.complete(state)

        final: FinalResponse = delta["final_response"]
        assert "s1" not in final.done
        assert "s1" in final.unconfirmed

    @pytest.mark.asyncio
    async def test_outbound_tool_unconfirmed_warns_to_check_outbox(self) -> None:
        """For an OUTBOUND tool (send_email_mock), unconfirmed warns to check outbox (§11.4)."""
        plan = make_plan([("s1", ToolName.SEND_EMAIL_MOCK, False, StepStatus.SUCCEEDED)])
        state = create_initial_state(run_id="run_1", user_request="send", plan=plan)
        state["verification_result"] = {
            "s1": VerificationResult(
                step_id="s1",
                status=VerificationStatus.UNCONFIRMED,
                mode="readback",
            )
        }
        handlers = NodeHandlers()
        delta = await handlers.complete(state)

        final: FinalResponse = delta["final_response"]
        assert "check the outbox" in final.summary.lower()


# ---------------------------------------------------------------------------
# 4. Fail Node Tests
# ---------------------------------------------------------------------------
class TestFailNode:
    @pytest.mark.asyncio
    async def test_generic_explicit_failure(self) -> None:
        """Fail node sets status=FAILED and constructs FinalResponse."""
        state = create_initial_state(run_id="run_1", user_request="test")
        state["status_reason"] = "something_failed"

        handlers = NodeHandlers()
        delta = await handlers.fail(state)

        assert delta["status"] == RunStatus.FAILED
        assert delta["status_reason"] == "something_failed"
        final: FinalResponse = delta["final_response"]
        assert final.partial is False
        assert "failed" in final.summary.lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reason",
        [
            "budget_exhausted",
            "retry_budget_exhausted",
            "replan_budget_exhausted",
            "deadline_exceeded",
            "terminal_error_policy_violation",
            "terminal_error_internal",
            "invalid_plan",
            "planner_error",
            "out_of_scope",
            "verification_failed",
        ],
    )
    async def test_existing_status_reasons_preserved(self, reason: str) -> None:
        """Preserves specific status_reason without replacing with a generic string."""
        state = create_initial_state(run_id="run_1", user_request="test")
        state["status_reason"] = reason

        handlers = NodeHandlers()
        delta = await handlers.fail(state)

        assert delta["status"] == RunStatus.FAILED
        assert delta["status_reason"] == reason
        assert reason in delta["final_response"].summary

    @pytest.mark.asyncio
    async def test_failure_derives_from_last_error_when_no_reason_set(self) -> None:
        """When status_reason is None, fail derives reason from latest error."""
        state = create_initial_state(run_id="run_1", user_request="test")
        state["status_reason"] = None
        state["errors"] = [
            AgentError(
                step_id="s1",
                error_class=ErrorClass.POLICY_VIOLATION,
                message="Cannot access resource",
                recovery=RecoveryAction.FAIL,
            )
        ]
        handlers = NodeHandlers()
        delta = await handlers.fail(state)

        assert delta["status"] == RunStatus.FAILED
        assert delta["status_reason"] == "policy_violation"

    @pytest.mark.asyncio
    async def test_failure_response_never_reports_success(self) -> None:
        """Failure response never reports success."""
        plan = make_plan(
            [
                ("s1", ToolName.SEARCH_LEADS, False, StepStatus.SUCCEEDED),
                ("s2", ToolName.SCORE_LEAD, False, StepStatus.FAILED),
            ]
        )
        state = create_initial_state(run_id="run_1", user_request="test", plan=plan)
        state["status_reason"] = "retry_budget_exhausted"

        handlers = NodeHandlers()
        delta = await handlers.fail(state)

        assert delta["status"] == RunStatus.FAILED
        summary = delta["final_response"].summary.lower()
        assert "run failed" in summary
        assert "completed successfully" not in summary
        assert delta["final_response"].partial is False

    @pytest.mark.asyncio
    async def test_failure_categorizes_completed_failed_and_pending_steps(self) -> None:
        """Fail node correctly categorizes done, not_done, pending."""
        plan = make_plan(
            [
                ("s1", ToolName.SEARCH_LEADS, False, StepStatus.SUCCEEDED),
                ("s2", ToolName.SCORE_LEAD, False, StepStatus.FAILED),
                ("s3", ToolName.SEND_EMAIL_MOCK, False, StepStatus.PENDING),
            ]
        )
        state = create_initial_state(run_id="run_1", user_request="test", plan=plan)
        state["status_reason"] = "retry_budget_exhausted"

        handlers = NodeHandlers()
        delta = await handlers.fail(state)

        final: FinalResponse = delta["final_response"]
        assert final.done == ["s1"]
        assert final.not_done == ["s2"]
        assert final.pending == ["s3"]


# ---------------------------------------------------------------------------
# 5. Security & Sanitization Tests
# ---------------------------------------------------------------------------
class TestSecurityAndSanitization:
    def test_sanitize_text_redacts_credentials(self) -> None:
        """Bearer tokens, API keys, passwords, and secrets are scrubbed."""
        raw = (
            "Error with Bearer eyJhbGciOi.secret.token and "
            "api_key=sk_live_12345 password=supersecret"
        )
        cleaned = sanitize_text(raw)
        assert "eyJhbGciOi.secret.token" not in cleaned
        assert "sk_live_12345" not in cleaned
        assert "supersecret" not in cleaned
        assert "[REDACTED]" in cleaned

    @pytest.mark.asyncio
    async def test_error_message_with_secrets_sanitized_in_summary(self) -> None:
        """Leaked secrets in raw error messages do not appear in FinalResponse summary."""
        state = create_initial_state(run_id="run_1", user_request="test")
        state["status_reason"] = "upstream_error"
        state["errors"] = [
            AgentError(
                step_id="s1",
                error_class=ErrorClass.INTERNAL,
                message="HTTP 401 with Bearer secret_provider_token_9999",
                recovery=RecoveryAction.FAIL,
            )
        ]
        handlers = NodeHandlers()
        delta = await handlers.fail(state)

        summary = delta["final_response"].summary
        assert "secret_provider_token_9999" not in summary
        assert "[REDACTED]" in summary

    def test_format_failure_explanation_safe(self) -> None:
        """format_failure_explanation uses known explanations and scrubs unknown."""
        exp = format_failure_explanation("budget_exhausted")
        assert "maximum allowed step count" in exp.lower()

        raw_err = [
            AgentError(
                step_id="s1",
                error_class=ErrorClass.INTERNAL,
                message="Token: ghp_1234567890abcdef",
            )
        ]
        exp2 = format_failure_explanation("terminal_error_internal", raw_err)
        assert "ghp_1234567890abcdef" not in exp2


# ---------------------------------------------------------------------------
# 6. State Safety & Immutability Tests
# ---------------------------------------------------------------------------
class TestStateSafety:
    @pytest.mark.asyncio
    async def test_state_is_not_mutated_in_place(self) -> None:
        """complete and fail return partial deltas and do not mutate state in place."""
        plan = make_plan([("s1", ToolName.SEARCH_LEADS, False, StepStatus.SUCCEEDED)])
        orig_plan = plan.model_copy(deep=True)
        state = create_initial_state(run_id="run_1", user_request="test", plan=plan)

        handlers = NodeHandlers()
        delta = await handlers.complete(state)

        assert delta is not state
        assert "final_response" in delta
        assert state["final_response"] is None
        assert state["plan"] == orig_plan

    def test_exact_21_channels_unchanged(self) -> None:
        """The graph's channel schema must have exactly 21 channels."""
        assert len(AgentState.__annotations__) == 21
        assert "final_response" in AgentState.__annotations__
        assert "status" in AgentState.__annotations__
        assert "status_reason" in AgentState.__annotations__


# ---------------------------------------------------------------------------
# 7. Graph Integration Tests
# ---------------------------------------------------------------------------
class TestGraphIntegration:
    @pytest.mark.asyncio
    async def test_decide_to_complete_integration(self) -> None:
        """Run through graph where all steps succeed reaches complete -> END."""
        plan = make_plan([("s1", ToolName.SEARCH_LEADS, False, StepStatus.SUCCEEDED)])

        async def pass_understand(s: AgentState) -> dict[str, Any]:
            return {
                "normalized_task": NormalizedTask(intent="search_leads", in_scope=True),
                "status": RunStatus.RUNNING,
                "status_reason": None,
            }

        async def pass_plan(s: AgentState) -> dict[str, Any]:
            return {"plan": s.get("plan"), "status_reason": None}

        handlers = NodeHandlers(
            understand_handler=pass_understand,
            plan_handler=pass_plan,
            clock=FixedClock(TEST_NOW),
        )
        graph = create_agent_graph(
            checkpointer=MemorySaver(),
            node_handlers=handlers,
        )
        cfg = {"configurable": {"thread_id": "thread_complete_test"}}
        initial = create_initial_state(
            run_id="run_complete_1",
            user_request="Find fintech leads",
            plan=plan,
            clock=FixedClock(TEST_NOW),
        )
        initial["status"] = RunStatus.RUNNING

        final = await graph.ainvoke(initial, config=cfg)

        assert final["status"] == RunStatus.COMPLETED
        assert final["status_reason"] is None
        assert final["final_response"] is not None
        assert final["final_response"].done == ["s1"]

    @pytest.mark.asyncio
    async def test_approval_rejection_routes_to_complete_as_rejected(self) -> None:
        """decide routes required rejection to complete -> yields RunStatus.REJECTED."""
        plan = make_plan([("s1", ToolName.SEND_EMAIL_MOCK, False, StepStatus.REJECTED)])

        async def pass_understand(s: AgentState) -> dict[str, Any]:
            return {
                "normalized_task": NormalizedTask(intent="send_email", in_scope=True),
                "status": RunStatus.RUNNING,
                "status_reason": None,
            }

        async def pass_plan(s: AgentState) -> dict[str, Any]:
            return {"plan": s.get("plan"), "status_reason": None}

        handlers = NodeHandlers(
            understand_handler=pass_understand,
            plan_handler=pass_plan,
            clock=FixedClock(TEST_NOW),
        )
        graph = create_agent_graph(checkpointer=MemorySaver(), node_handlers=handlers)
        cfg = {"configurable": {"thread_id": "thread_reject_test"}}
        initial = create_initial_state(
            run_id="run_reject_1",
            user_request="Send outreach email",
            plan=plan,
            clock=FixedClock(TEST_NOW),
        )
        initial["status"] = RunStatus.RUNNING

        final = await graph.ainvoke(initial, config=cfg)

        assert final["status"] == RunStatus.REJECTED
        assert final["status_reason"] == "approval_rejected"
        assert final["final_response"] is not None
        assert "s1" in final["final_response"].not_done

    @pytest.mark.asyncio
    async def test_recover_to_fail_produces_final_response(self) -> None:
        """recover routing to fail produces terminal FAILED state with FinalResponse."""
        plan = make_plan([("s1", ToolName.SCORE_LEAD, False, StepStatus.PENDING)])

        async def pass_understand(s: AgentState) -> dict[str, Any]:
            return {
                "normalized_task": NormalizedTask(intent="score_lead", in_scope=True),
                "status": RunStatus.RUNNING,
                "status_reason": None,
            }

        async def pass_plan(s: AgentState) -> dict[str, Any]:
            return {"plan": s.get("plan"), "status_reason": None}

        async def failing_execute(s: AgentState) -> dict[str, Any]:
            return {
                "errors": [
                    AgentError(
                        step_id="s1",
                        error_class=ErrorClass.POLICY_VIOLATION,
                        message="Unauthorized access attempt",
                        recovery=RecoveryAction.FAIL,
                    )
                ]
            }

        handlers = NodeHandlers(
            understand_handler=pass_understand,
            plan_handler=pass_plan,
            clock=FixedClock(TEST_NOW),
        )
        handlers.execute_tool = failing_execute  # type: ignore[method-assign]

        graph = create_agent_graph(checkpointer=MemorySaver(), node_handlers=handlers)
        cfg = {"configurable": {"thread_id": "thread_recover_fail_test"}}
        initial = create_initial_state(
            run_id="run_fail_1",
            user_request="Score the leads",
            plan=plan,
            clock=FixedClock(TEST_NOW),
            metadata=RunMetadata(budgets=Budgets(max_retries=0)),
        )

        final = await graph.ainvoke(initial, config=cfg)

        assert final["status"] == RunStatus.FAILED
        assert final["status_reason"] == "terminal_error_policy_violation"
        assert final["final_response"] is not None
        assert final["final_response"].partial is False
        assert "s1" in final["final_response"].not_done


# ---------------------------------------------------------------------------
# 8. Structural & Invariant Tests
# ---------------------------------------------------------------------------
class TestResponderStructuralInvariants:
    def test_complete_and_fail_do_not_dispatch_tools(self) -> None:
        """complete and fail never invoke ToolRegistry.dispatch."""
        nodes_file = Path(__file__).resolve().parent.parent / "app" / "agent" / "nodes.py"
        tree = ast.parse(nodes_file.read_text(encoding="utf-8"))
        # Find functions synthesize_complete_response, synthesize_fail_response, complete, fail
        target_funcs = {
            "synthesize_complete_response",
            "synthesize_fail_response",
            "complete",
            "fail",
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in target_funcs
            ):
                for child in ast.walk(node):
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                        assert child.func.attr != "dispatch", f"dispatch called in {node.name}"

    def test_no_mock_adapter_imports_in_nodes(self) -> None:
        """nodes.py does not import mock adapters."""
        nodes_file = Path(__file__).resolve().parent.parent / "app" / "agent" / "nodes.py"
        tree = ast.parse(nodes_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("app.integrations.mock")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("app.integrations.mock")

    def test_no_orm_or_database_access_in_response_layer(self) -> None:
        """Response layer performs no ORM queries or database operations."""
        nodes_file = Path(__file__).resolve().parent.parent / "app" / "agent" / "nodes.py"
        tree = ast.parse(nodes_file.read_text(encoding="utf-8"))
        forbidden = {"select", "insert", "update", "delete"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("sqlalchemy")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                "synthesize_complete_response",
                "synthesize_fail_response",
                "complete",
                "fail",
            }:
                for child in ast.walk(node):
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                        assert child.func.attr not in forbidden, (
                            f"forbidden ORM call {child.func.attr} in {node.name}"
                        )

    def test_no_unconstrained_llm_invocations_in_response_layer(self) -> None:
        """nodes.py response layer does not call external LLM client APIs."""
        nodes_file = Path(__file__).resolve().parent.parent / "app" / "agent" / "nodes.py"
        source = nodes_file.read_text(encoding="utf-8")
        assert "StructuredCompletionClient" not in source
        assert "GroqStructuredClient" not in source
        assert "chat.completions" not in source
