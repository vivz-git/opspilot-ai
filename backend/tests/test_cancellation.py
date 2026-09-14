"""AGENT-009: Cooperative cancellation and budget termination tests (§13.2).

Covers:
1. Basic cancellation mechanics and terminal response synthesis
2. Cancellation observed at node-entry boundaries for all nodes
3. Execute tool safety: zero dispatches if cancelled before; tool allowed to finish if in flight
4. Approval safety: no pause/interrupt if cancelled; clean exit on resume
5. Recovery safety: no retries, no replans, no skip-and-continue once cancelled
6. Budget interaction: cancellation reason takes precedence over budget exhaustion
7. Terminal state invariants: completed/rejected runs never convert to cancelled
8. Checkpoint / resume durability and safety across pause
9. Concurrency & idempotency: repeated/racing cancellation calls
10. Structural checks: no dispatch outside execute_tool, exactly 21 channels
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app.agent.graph import create_agent_graph
from app.agent.nodes import NodeHandlers, create_initial_state
from app.agent.state import (
    AgentError,
    AgentState,
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalState,
    Budgets,
    NormalizedTask,
    Plan,
    PlanStep,
    RunMetadata,
    RunStatus,
    StepStatus,
)
from app.errors import ErrorClass, RecoveryAction
from app.runtime import FixedClock, InMemoryCancellationSource
from app.tools.contracts import ToolName
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

pytestmark = [pytest.mark.unit]

TEST_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. Basic Cancellation
# ---------------------------------------------------------------------------
class TestBasicCancellation:
    @pytest.mark.asyncio
    async def test_cancellation_produces_failed_status_and_cancelled_reason(self) -> None:
        """Cancellation transitions run to RunStatus.FAILED with status_reason='cancelled'."""
        source = InMemoryCancellationSource()
        source.cancel("run_1")

        graph = create_agent_graph(cancellation_source=source, clock=FixedClock(TEST_NOW))
        state = create_initial_state(run_id="run_1", user_request="Find leads")

        res = await graph.ainvoke(state)
        assert res["status"] == RunStatus.FAILED
        assert res["status_reason"] == "cancelled"
        assert res["final_response"] is not None
        assert "cancelled" in res["final_response"].summary.lower()

    @pytest.mark.asyncio
    async def test_cancellation_is_idempotent(self) -> None:
        """Calling cancel multiple times produces the identical terminal result."""
        source = InMemoryCancellationSource()
        source.cancel("run_dup")
        source.cancel("run_dup")

        graph = create_agent_graph(cancellation_source=source, clock=FixedClock(TEST_NOW))
        state = create_initial_state(run_id="run_dup", user_request="Find leads")

        res = await graph.ainvoke(state)
        assert res["status"] == RunStatus.FAILED
        assert res["status_reason"] == "cancelled"

    @pytest.mark.asyncio
    async def test_state_with_status_cancelled_is_honored(self) -> None:
        """Pre-existing RunStatus.CANCELLED in state leads to fail(cancelled)."""
        graph = create_agent_graph(clock=FixedClock(TEST_NOW))
        state = create_initial_state(run_id="run_state", user_request="Find leads")
        state["status"] = RunStatus.CANCELLED

        res = await graph.ainvoke(state)
        assert res["status"] == RunStatus.FAILED
        assert res["status_reason"] == "cancelled"


# ---------------------------------------------------------------------------
# 2. Node Entry Boundaries
# ---------------------------------------------------------------------------
class TestNodeEntryBoundaries:
    @pytest.mark.asyncio
    async def test_cancellation_before_understand_skips_normalization(self) -> None:
        """understand node observes cancellation and normalizer is never invoked."""
        normalized = False

        class SpyingNormalizer:
            async def normalize(self, req: str) -> NormalizedTask:
                nonlocal normalized
                normalized = True
                return NormalizedTask(intent="search_leads", in_scope=True)

        source = InMemoryCancellationSource()
        source.cancel("run_norm")

        handlers = NodeHandlers(
            normalizer=SpyingNormalizer(),  # type: ignore[arg-type]
            cancellation_source=source,
            clock=FixedClock(TEST_NOW),
        )
        state = create_initial_state(run_id="run_norm", user_request="Test req")
        delta = await handlers.understand(state)

        assert delta["status"] == RunStatus.FAILED
        assert delta["status_reason"] == "cancelled"
        assert normalized is False

    @pytest.mark.asyncio
    async def test_cancellation_before_plan_skips_planner(self) -> None:
        """plan node observes cancellation and planner is never called."""
        planner_called = False

        class SpyingPlanner:
            identity = None

            async def plan(self, *args: Any, **kwargs: Any) -> Plan:
                nonlocal planner_called
                planner_called = True
                return Plan(plan_id="p1", steps=[])

        source = InMemoryCancellationSource()
        source.cancel("run_plan")

        handlers = NodeHandlers(
            planner=SpyingPlanner(),  # type: ignore[arg-type]
            cancellation_source=source,
            clock=FixedClock(TEST_NOW),
        )
        state = create_initial_state(run_id="run_plan", user_request="Test req")
        state["normalized_task"] = NormalizedTask(intent="search_leads", in_scope=True)
        delta = await handlers.plan(state)

        assert delta["status"] == RunStatus.FAILED
        assert delta["status_reason"] == "cancelled"
        assert planner_called is False

    @pytest.mark.asyncio
    async def test_cancellation_before_decide_routes_to_fail(self) -> None:
        """decide node observes cancellation and routes directly to fail."""
        source = InMemoryCancellationSource()
        source.cancel("run_decide")

        handlers = NodeHandlers(cancellation_source=source, clock=FixedClock(TEST_NOW))
        state = create_initial_state(run_id="run_decide", user_request="Test req")
        state["plan"] = Plan(
            plan_id="p1", steps=[PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS)]
        )

        delta = await handlers.decide(state)
        assert delta.get("status_reason") == "cancelled"
        assert handlers.route_after_decide(state) == "fail"

    @pytest.mark.asyncio
    async def test_cancellation_before_verify_skips_verification(self) -> None:
        """verify node observes cancellation and does not record verified results."""
        source = InMemoryCancellationSource()
        source.cancel("run_verify")

        handlers = NodeHandlers(cancellation_source=source, clock=FixedClock(TEST_NOW))
        state = create_initial_state(run_id="run_verify", user_request="Test req")
        state["current_step_id"] = "s1"

        delta = await handlers.verify(state)
        assert delta["status"] == RunStatus.FAILED
        assert delta["status_reason"] == "cancelled"
        assert "verification_result" not in delta
        assert handlers.route_after_verify(state) == "decide"

    @pytest.mark.asyncio
    async def test_cancellation_before_recover_skips_recovery(self) -> None:
        """recover node observes cancellation and does not retry or backoff."""
        source = InMemoryCancellationSource()
        source.cancel("run_recover")

        handlers = NodeHandlers(cancellation_source=source, clock=FixedClock(TEST_NOW))
        state = create_initial_state(run_id="run_recover", user_request="Test req")
        state["current_step_id"] = "s1"
        state["plan"] = Plan(
            plan_id="p1", steps=[PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS)]
        )
        state["errors"] = [
            AgentError(
                step_id="s1",
                error_class=ErrorClass.TRANSIENT,
                message="Timeout",
                recovery=RecoveryAction.RETRY,
                occurred_at=TEST_NOW,
            )
        ]

        delta = await handlers.recover(state)
        assert delta["status"] == RunStatus.FAILED
        assert delta["status_reason"] == "cancelled"
        assert "retry_count" not in delta
        assert handlers.route_after_recover(state) == "fail"


# ---------------------------------------------------------------------------
# 3. Execute Tool Safety & In-Flight Invariant
# ---------------------------------------------------------------------------
class TestExecuteToolSafety:
    @pytest.mark.asyncio
    async def test_cancellation_before_tool_dispatch_prevents_execution(self) -> None:
        """execute_tool observes cancellation and never invokes ToolRegistry.dispatch."""
        dispatched = False

        class MockRegistry:
            async def dispatch(self, *args: Any, **kwargs: Any) -> Any:
                nonlocal dispatched
                dispatched = True
                raise AssertionError("dispatch must not be called")

        source = InMemoryCancellationSource()
        source.cancel("run_exec")

        handlers = NodeHandlers(
            registry=MockRegistry(),  # type: ignore[arg-type]
            cancellation_source=source,
            clock=FixedClock(TEST_NOW),
        )
        state = create_initial_state(run_id="run_exec", user_request="Test req")
        state["current_step_id"] = "s1"
        state["plan"] = Plan(
            plan_id="p1",
            steps=[PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS, args={"query": "test"})],
        )

        delta = await handlers.execute_tool(state)
        assert delta["status"] == RunStatus.FAILED
        assert delta["status_reason"] == "cancelled"
        assert dispatched is False

    @pytest.mark.asyncio
    async def test_tool_already_in_flight_is_never_interrupted(self) -> None:
        """Critical Invariant: In-flight tool finishes; next node boundary observes cancellation."""
        tool_started = asyncio.Event()
        tool_allow_finish = asyncio.Event()
        tool_completed = False

        class SlowToolRegistry:
            def contract(self, name: ToolName) -> Any:
                return None

            async def dispatch(self, *args: Any, **kwargs: Any) -> Any:
                nonlocal tool_completed
                tool_started.set()
                await tool_allow_finish.wait()
                tool_completed = True

                from app.tools.registry import DispatchOutcome, DispatchResult
                from app.tools.schemas import SearchLeadsOutput

                return DispatchResult(
                    tool=ToolName.SEARCH_LEADS,
                    tool_version="1.0",
                    step_id="s1",
                    attempt=1,
                    outcome=DispatchOutcome.SUCCEEDED,
                    args_hash="hash1",
                    idempotency_key="key1",
                    output=SearchLeadsOutput(leads=[], total_matched=0),
                    output_data={"leads": [], "total_matched": 0, "truncated": False},
                    tool_call_id=uuid.uuid4(),
                    port=None,
                    adapter=None,
                    started_at=TEST_NOW,
                    finished_at=TEST_NOW,
                    duration_ms=10,
                )

        source = InMemoryCancellationSource()
        reg = SlowToolRegistry()

        handlers = NodeHandlers(
            registry=reg,  # type: ignore[arg-type]
            cancellation_source=source,
            clock=FixedClock(TEST_NOW),
        )

        run_id = "11111111-1111-1111-1111-111111111111"
        state = create_initial_state(run_id=run_id, user_request="Test req")
        state["current_step_id"] = "s1"
        state["plan"] = Plan(
            plan_id="p1",
            steps=[PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS, args={"query": "test"})],
        )

        # Launch execute_tool as a concurrent task
        exec_task = asyncio.create_task(handlers.execute_tool(state))

        # Wait until tool has actually started executing
        await asyncio.wait_for(tool_started.wait(), timeout=5.0)

        # Cancellation arrives WHILE tool is executing mid-flight
        source.cancel(run_id)

        # Tool is allowed to finish normally
        tool_allow_finish.set()
        delta = await exec_task

        # Verify tool completed without being cancelled
        assert tool_completed is True
        assert "tool_results" in delta
        assert delta["tool_results"]["s1"].output == {
            "leads": [],
            "total_matched": 0,
            "truncated": False,
        }

        # Now simulate next node boundary (decide)
        next_state: AgentState = {**state, **delta}  # type: ignore[misc]
        next_delta = await handlers.decide(next_state)
        assert next_delta.get("status_reason") == "cancelled"
        assert handlers.route_after_decide(next_state) == "fail"


# ---------------------------------------------------------------------------
# 4. Approval Safety
# ---------------------------------------------------------------------------
class TestApprovalSafety:
    @pytest.mark.asyncio
    async def test_cancellation_before_approval_prevents_pause(self) -> None:
        """request_approval does not pause or interrupt when cancellation is pending."""
        source = InMemoryCancellationSource()
        source.cancel("run_appr")

        handlers = NodeHandlers(cancellation_source=source, clock=FixedClock(TEST_NOW))
        state = create_initial_state(run_id="run_appr", user_request="Test req")
        state["current_step_id"] = "s1"
        state["plan"] = Plan(
            plan_id="p1",
            steps=[
                PlanStep(step_id="s1", tool=ToolName.SEND_EMAIL_MOCK, args={"to": "a@example.com"})
            ],
        )

        delta = await handlers.request_approval(state)
        assert delta["status"] == RunStatus.FAILED
        assert delta["status_reason"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancellation_during_approval_pause_exits_cleanly_on_resume(self) -> None:
        """When paused for approval and cancelled, resuming exits cleanly to fail(cancelled)."""
        saver = MemorySaver()
        source = InMemoryCancellationSource()

        graph = create_agent_graph(
            checkpointer=saver, cancellation_source=source, clock=FixedClock(TEST_NOW)
        )
        config = {"configurable": {"thread_id": "run_pause_1"}}

        step_id = "s6"
        send_args = {"draft_id": "d_100", "to_email": "ada@example.com"}
        state = create_initial_state(
            run_id="run_pause_1",
            user_request="Send outreach email",
            clock=FixedClock(TEST_NOW),
            plan=Plan(
                plan_id="p_hitl",
                steps=[
                    PlanStep(
                        step_id=step_id,
                        tool=ToolName.SEND_EMAIL_MOCK,
                        args=send_args,
                        status=StepStatus.PENDING,
                    )
                ],
            ),
        )

        # 1. Run until paused at request_approval
        paused = await graph.ainvoke(state, config=config)
        assert paused["status"] == RunStatus.RUNNING

        # 2. Operator cancels the run while paused
        source.cancel("run_pause_1")

        # 3. Attempt to resume
        resumed = await graph.ainvoke(
            Command(resume={"decision": "approve", "decided_by": "operator"}),
            config=config,
        )

        assert resumed["status"] == RunStatus.FAILED
        assert resumed["status_reason"] == "cancelled"
        assert "cancelled" in resumed["final_response"].summary.lower()


# ---------------------------------------------------------------------------
# 5. Recovery Safety
# ---------------------------------------------------------------------------
class TestRecoverySafety:
    @pytest.mark.asyncio
    async def test_cancellation_suppresses_retries(self) -> None:
        """Retryable error + cancellation routes to fail, suppressing retries."""
        source = InMemoryCancellationSource()
        source.cancel("run_retry")

        handlers = NodeHandlers(cancellation_source=source, clock=FixedClock(TEST_NOW))
        state = create_initial_state(run_id="run_retry", user_request="Search leads")
        state["current_step_id"] = "s1"
        state["plan"] = Plan(
            plan_id="p1", steps=[PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS)]
        )
        state["errors"] = [
            AgentError(
                step_id="s1",
                error_class=ErrorClass.TRANSIENT,
                message="Timeout",
                recovery=RecoveryAction.RETRY,
                occurred_at=TEST_NOW,
            )
        ]

        delta = await handlers.recover(state)
        assert delta["status"] == RunStatus.FAILED
        assert delta["status_reason"] == "cancelled"
        assert handlers.route_after_recover(state) == "fail"

    @pytest.mark.asyncio
    async def test_cancellation_suppresses_replanning(self) -> None:
        """Replannable fault + cancellation routes to fail, suppressing replan."""
        source = InMemoryCancellationSource()
        source.cancel("run_replan")

        handlers = NodeHandlers(cancellation_source=source, clock=FixedClock(TEST_NOW))
        state = create_initial_state(run_id="run_replan", user_request="Search leads")
        state["current_step_id"] = "s1"
        state["plan"] = Plan(
            plan_id="p1", steps=[PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS)]
        )
        state["errors"] = [
            AgentError(
                step_id="s1",
                error_class=ErrorClass.REFERENCE_RESOLUTION,
                message="Unresolvable ref",
                recovery=RecoveryAction.REPLAN,
                occurred_at=TEST_NOW,
            )
        ]

        delta = await handlers.recover(state)
        assert delta["status"] == RunStatus.FAILED
        assert delta["status_reason"] == "cancelled"
        assert handlers.route_after_recover(state) == "fail"

    @pytest.mark.asyncio
    async def test_cancellation_suppresses_optional_skip(self) -> None:
        """Optional step failure + cancellation routes to fail, suppressing skip-and-continue."""
        source = InMemoryCancellationSource()
        source.cancel("run_skip")

        handlers = NodeHandlers(cancellation_source=source, clock=FixedClock(TEST_NOW))
        state = create_initial_state(run_id="run_skip", user_request="Search leads")
        state["current_step_id"] = "s1"
        state["plan"] = Plan(
            plan_id="p1",
            steps=[PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS, optional=True)],
        )
        state["errors"] = [
            AgentError(
                step_id="s1",
                error_class=ErrorClass.TRANSIENT,
                message="Timeout",
                recovery=RecoveryAction.SKIP,
                occurred_at=TEST_NOW,
            )
        ]

        delta = await handlers.recover(state)
        assert delta["status"] == RunStatus.FAILED
        assert delta["status_reason"] == "cancelled"
        assert handlers.route_after_recover(state) == "fail"


# ---------------------------------------------------------------------------
# 6. Budget Interaction
# ---------------------------------------------------------------------------
class TestBudgetInteraction:
    @pytest.mark.asyncio
    async def test_cancellation_precedes_step_budget_exhaustion(self) -> None:
        """When step budget is exhausted AND cancelled,
        reason is 'cancelled' not 'budget_exhausted'."""
        source = InMemoryCancellationSource()
        source.cancel("run_budget")

        handlers = NodeHandlers(cancellation_source=source, clock=FixedClock(TEST_NOW))
        state = create_initial_state(
            run_id="run_budget",
            user_request="test",
            metadata=RunMetadata(budgets=Budgets(max_steps=5)),
        )
        state["step_count"] = 10
        state["plan"] = Plan(
            plan_id="p1", steps=[PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS)]
        )

        delta = await handlers.decide(state)
        assert delta.get("status_reason") == "cancelled"

    @pytest.mark.asyncio
    async def test_cancellation_precedes_deadline_passed(self) -> None:
        """When deadline passed AND cancelled, reason is 'cancelled' not 'deadline_exceeded'."""
        source = InMemoryCancellationSource()
        source.cancel("run_deadline")

        handlers = NodeHandlers(cancellation_source=source, clock=FixedClock(TEST_NOW))
        state = create_initial_state(run_id="run_deadline", user_request="test")
        state["deadline_at"] = TEST_NOW - timedelta(seconds=100)
        state["plan"] = Plan(
            plan_id="p1", steps=[PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS)]
        )

        delta = await handlers.decide(state)
        assert delta.get("status_reason") == "cancelled"


# ---------------------------------------------------------------------------
# 7. Terminal State Invariants
# ---------------------------------------------------------------------------
class TestTerminalStateInvariants:
    @pytest.mark.asyncio
    async def test_completed_run_cannot_convert_to_cancelled(self) -> None:
        """A run already in RunStatus.COMPLETED is never converted to cancelled."""
        source = InMemoryCancellationSource()
        source.cancel("run_completed")

        handlers = NodeHandlers(cancellation_source=source, clock=FixedClock(TEST_NOW))
        state = create_initial_state(run_id="run_completed", user_request="test")
        state["status"] = RunStatus.COMPLETED

        delta = await handlers.complete(state)
        assert delta["status"] == RunStatus.COMPLETED
        assert delta["status_reason"] is None

    @pytest.mark.asyncio
    async def test_rejected_run_cannot_convert_to_cancelled(self) -> None:
        """A run already in RunStatus.REJECTED is never converted to cancelled."""
        source = InMemoryCancellationSource()
        source.cancel("run_rejected")

        handlers = NodeHandlers(cancellation_source=source, clock=FixedClock(TEST_NOW))
        state = create_initial_state(run_id="run_rejected", user_request="test")
        state["status"] = RunStatus.REJECTED
        state["status_reason"] = "approval_rejected"
        state["approval_state"] = ApprovalState(
            decisions={
                "s1": ApprovalDecision(
                    approval_id="appr_1",
                    step_id="s1",
                    decision=ApprovalDecisionKind.REJECT,
                    args_hash="hash",
                    decided_by="operator",
                    decided_at=TEST_NOW,
                )
            }
        )

        delta = await handlers.complete(state)
        assert delta["status"] == RunStatus.REJECTED
        assert delta["status_reason"] == "approval_rejected"


# ---------------------------------------------------------------------------
# 8. Checkpoint & Resume Durability
# ---------------------------------------------------------------------------
class TestCheckpointAndResumeDurability:
    @pytest.mark.asyncio
    async def test_checkpoint_persists_cancellation_cleanly(self) -> None:
        """LangGraph checkpointer cleanly preserves the terminal cancelled state."""
        saver = MemorySaver()
        source = InMemoryCancellationSource()
        source.cancel("run_cp_1")

        graph = create_agent_graph(
            checkpointer=saver, cancellation_source=source, clock=FixedClock(TEST_NOW)
        )
        config = {"configurable": {"thread_id": "run_cp_1"}}

        state = create_initial_state(
            run_id="run_cp_1", user_request="Find leads", clock=FixedClock(TEST_NOW)
        )
        res = await graph.ainvoke(state, config=config)

        assert res["status"] == RunStatus.FAILED
        assert res["status_reason"] == "cancelled"

        # Verify state in checkpointer
        checkpoint_tuple = await saver.aget_tuple(config)
        assert checkpoint_tuple is not None
        cp_state = checkpoint_tuple.checkpoint["channel_values"]
        assert cp_state["status"] == RunStatus.FAILED
        assert cp_state["status_reason"] == "cancelled"


# ---------------------------------------------------------------------------
# 9. Concurrency & Idempotency
# ---------------------------------------------------------------------------
class TestConcurrencyAndIdempotency:
    @pytest.mark.asyncio
    async def test_concurrent_cancel_calls_are_safe(self) -> None:
        """Concurrent cancel calls against the same run succeed without race."""
        source = InMemoryCancellationSource()

        async def worker_cancel() -> None:
            for _ in range(50):
                source.cancel("run_race")
                await asyncio.sleep(0.001)

        await asyncio.gather(worker_cancel(), worker_cancel(), worker_cancel())
        assert source.is_cancelled("run_race") is True


# ---------------------------------------------------------------------------
# 10. Structural Invariants
# ---------------------------------------------------------------------------
class TestCancellationStructuralInvariants:
    def test_no_dispatch_calls_in_cancellation_logic(self) -> None:
        """Cancellation logic never invokes ToolRegistry.dispatch."""
        nodes_file = Path(__file__).resolve().parent.parent / "app" / "agent" / "nodes.py"
        tree = ast.parse(nodes_file.read_text(encoding="utf-8"))

        cancel_helpers = {"_is_cancelled", "_is_cancelled_sync"}
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in cancel_helpers
            ):
                for child in ast.walk(node):
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                        assert child.func.attr != "dispatch", f"dispatch in {node.name}"

    def test_no_task_cancel_invocations_in_nodes(self) -> None:
        """nodes.py never calls task.cancel() or future.cancel() against tools."""
        nodes_file = Path(__file__).resolve().parent.parent / "app" / "agent" / "nodes.py"
        tree = ast.parse(nodes_file.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr != "cancel", (
                    f"Forbidden task.cancel() call detected at line {node.lineno}"
                )

    def test_exact_twenty_one_state_channels_preserved(self) -> None:
        """AgentState schema must retain exactly 21 channels."""
        from app.agent.state import AgentState

        assert len(AgentState.__annotations__) == 21
