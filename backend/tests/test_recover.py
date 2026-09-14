"""AGENT-007: Recover node mechanics and retry backoff tests (§10, FOUND-004).

Covers:
- Deterministic recovery decision handling (RETRY, REPLAN, SKIP, FAIL)
- Retry counting and boundary enforcement (1 + MAX_RETRIES)
- Exponential backoff delay calculation, caps, and server hints (retry_after)
- Virtual clock-backed delay with no real-time sleeping in tests
- Injected SeededRandom jitter vs deterministic no-jitter behavior
- Bounded replan routing without double-incrementing replan_count
- Optional-step skipping (StepStatus.SKIPPED) and required-step non-skip enforcement
- Terminal failure on non-recoverable errors and exhausted budgets
- Crash/resume safety: no double retry increment or double sleep on checkpoint resume
- Graph integration: execute_tool -> recover -> execute_tool / plan / decide / fail
- Structural AST checks: no ToolRegistry.dispatch in recover, no mock adapters, no ORM
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.agent.graph import create_agent_graph
from app.agent.nodes import NodeHandlers, create_initial_state
from app.agent.state import (
    AgentError,
    AgentState,
    Budgets,
    Plan,
    PlanStep,
    RunMetadata,
    RunStatus,
    StepStatus,
    ToolCall,
)
from app.errors import (
    ErrorClass,
)
from app.runtime import DeterministicRandom, FixedClock
from app.tools.contracts import ToolName
from langgraph.checkpoint.memory import MemorySaver

pytestmark = [pytest.mark.unit]

TEST_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def make_test_state(
    *,
    step_id: str = "s1",
    tool: ToolName = ToolName.SEARCH_LEADS,
    optional: bool = False,
    error_class: ErrorClass = ErrorClass.TRANSIENT,
    error_message: str = "timeout",
    attempt: int = 1,
    retry_count: int = 0,
    max_retries: int = 2,
    replan_count: int = 0,
    max_replans: int = 2,
    step_count: int = 1,
    max_steps: int = 25,
    deadline_at: datetime | None = None,
    run_status: RunStatus = RunStatus.RUNNING,
    status_reason: str | None = None,
    err_detail: dict[str, Any] | None = None,
    target_err_step_id: str | None = None,
) -> AgentState:
    now = TEST_NOW
    deadline = deadline_at or (now + timedelta(seconds=300))
    plan = Plan(
        plan_id="p1",
        steps=[PlanStep(step_id=step_id, tool=tool, optional=optional, status=StepStatus.RUNNING)],
    )
    err = AgentError(
        step_id=target_err_step_id or step_id,
        error_class=error_class,
        message=error_message,
        attempt=attempt,
        detail=err_detail or {},
        occurred_at=now,
    )
    return {
        "run_id": "test_run",
        "user_request": "test request",
        "plan": plan,
        "current_step_id": step_id,
        "errors": [err],
        "tool_calls": [
            ToolCall(
                step_id=step_id,
                tool=tool,
                attempt=attempt,
                args_hash="hash",
                status="failed",
                error_class=error_class,
                error_message=error_message,
            )
        ],
        "retry_count": {step_id: retry_count} if retry_count > 0 else {},
        "replan_count": replan_count,
        "step_count": step_count,
        "deadline_at": deadline,
        "status": run_status,
        "status_reason": status_reason,
        "metadata": RunMetadata(
            budgets=Budgets(
                max_retries=max_retries,
                max_replans=max_replans,
                max_steps=max_steps,
            )
        ),
    }


# ===========================================================================
# 1. RETRY Mechanics & Backoff
# ===========================================================================
class TestRetryMechanics:
    @pytest.mark.asyncio
    async def test_retryable_error_schedules_retry_and_increments_count(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock, retry_base_delay_ms=250, retry_max_delay_ms=8000)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.TRANSIENT,
            attempt=1,
            retry_count=0,
            max_retries=2,
        )

        delta = await handlers.recover(state)
        assert delta.get("retry_count") == {"s1": 1}
        assert delta.get("status_reason") == "retry_attempt_1"
        assert clock.slept_seconds == 0.25  # 250ms virtual sleep
        assert clock.sleep_calls == [0.25]
        assert clock.now() == TEST_NOW + timedelta(milliseconds=250)

        # Route after recover on updated state
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "execute_tool"

    @pytest.mark.asyncio
    async def test_second_retry_backoff_and_counting(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock, retry_base_delay_ms=250, retry_max_delay_ms=8000)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.TRANSIENT,
            attempt=2,
            retry_count=1,
            max_retries=2,
        )

        delta = await handlers.recover(state)
        assert delta.get("retry_count") == {"s1": 2}
        assert delta.get("status_reason") == "retry_attempt_2"
        assert clock.slept_seconds == 0.50  # 500ms virtual sleep (attempt 2)
        assert clock.now() == TEST_NOW + timedelta(milliseconds=500)

        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "execute_tool"

    @pytest.mark.asyncio
    async def test_exhausted_retry_budget_routes_to_fail(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock, retry_base_delay_ms=250, retry_max_delay_ms=8000)
        # Attempt 3 was made, retry_count is 2, max_retries is 2 -> exhausted
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.TRANSIENT,
            attempt=3,
            retry_count=2,
            max_retries=2,
        )

        delta = await handlers.recover(state)
        assert "retry_count" not in delta
        assert delta.get("status_reason") == "retry_budget_exhausted"
        assert clock.slept_seconds == 0.0  # No sleeping when failing

        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "fail"

    @pytest.mark.asyncio
    async def test_capped_maximum_delay(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock, retry_base_delay_ms=1000, retry_max_delay_ms=3000)
        # Attempt 5 would be 1000 * 2^4 = 16000ms, capped at 3000ms (3.0s)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.TRANSIENT,
            attempt=5,
            retry_count=4,
            max_retries=10,
        )

        delta = await handlers.recover(state)
        assert delta.get("retry_count") == {"s1": 5}
        assert clock.slept_seconds == 3.0

    @pytest.mark.asyncio
    async def test_server_hint_retry_after_ms_overrides_calculated_backoff(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock, retry_base_delay_ms=250, retry_max_delay_ms=8000)
        # Server specified retry_after_ms = 4000
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.RATE_LIMITED,
            attempt=1,
            retry_count=0,
            max_retries=2,
            err_detail={"retry_after_ms": 4000},
        )

        delta = await handlers.recover(state)
        assert delta.get("retry_count") == {"s1": 1}
        assert clock.slept_seconds == 4.0  # 4000ms wins over 250ms

    @pytest.mark.asyncio
    async def test_server_hint_retry_after_seconds_overrides_calculated_backoff(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock, retry_base_delay_ms=250, retry_max_delay_ms=8000)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.RATE_LIMITED,
            attempt=1,
            retry_count=0,
            max_retries=2,
            err_detail={"retry_after": 5},  # 5 seconds
        )

        delta = await handlers.recover(state)
        assert delta.get("retry_count") == {"s1": 1}
        assert clock.slept_seconds == 5.0

    @pytest.mark.asyncio
    async def test_jitter_is_deterministic_under_seeded_random(self) -> None:
        clock1 = FixedClock(TEST_NOW)
        rng1 = DeterministicRandom(seed=1337)
        handlers1 = NodeHandlers(clock=clock1, seeded_random=rng1)
        state1 = make_test_state(attempt=1, retry_count=0)
        await handlers1.recover(state1)

        clock2 = FixedClock(TEST_NOW)
        rng2 = DeterministicRandom(seed=1337)
        handlers2 = NodeHandlers(clock=clock2, seeded_random=rng2)
        state2 = make_test_state(attempt=1, retry_count=0)
        await handlers2.recover(state2)

        assert clock1.slept_seconds == clock2.slept_seconds
        assert 0.20 <= clock1.slept_seconds <= 0.30  # 250ms * [0.8, 1.2] = [200ms, 300ms]


# ===========================================================================
# 2. REPLAN Mechanics
# ===========================================================================
class TestReplanMechanics:
    @pytest.mark.asyncio
    async def test_replannable_error_routes_to_plan_when_budget_remains(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.INPUT_VALIDATION,
            replan_count=0,
            max_replans=2,
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "replannable_fault"
        # Recover must NOT increment replan_count; the plan node owns it!
        assert "replan_count" not in delta
        assert clock.slept_seconds == 0.0

        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "plan"

    @pytest.mark.asyncio
    async def test_reference_resolution_routes_to_plan(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s2",
            error_class=ErrorClass.REFERENCE_RESOLUTION,
            replan_count=1,
            max_replans=2,
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "replannable_fault"
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "plan"

    @pytest.mark.asyncio
    async def test_stale_write_routes_to_plan(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.STALE_WRITE,
            replan_count=0,
            max_replans=2,
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "replannable_fault"
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "plan"

    @pytest.mark.asyncio
    async def test_exhausted_replan_budget_routes_to_fail(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.INPUT_VALIDATION,
            replan_count=2,
            max_replans=2,
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "replan_budget_exhausted"
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "fail"


# ===========================================================================
# 3. SKIP Mechanics (Optional vs Required Steps)
# ===========================================================================
class TestSkipMechanics:
    @pytest.mark.asyncio
    async def test_optional_step_skips_and_routes_to_decide(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s1",
            optional=True,
            error_class=ErrorClass.NOT_FOUND,
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "optional_step_skipped"
        plan = delta.get("plan")
        assert plan is not None
        step = plan.step("s1")
        assert step is not None
        assert step.status == StepStatus.SKIPPED

        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "decide"

    @pytest.mark.asyncio
    async def test_required_step_not_found_fails_when_replan_exhausted(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        # Required step (optional=False), replan_count=2 -> fails
        state = make_test_state(
            step_id="s1",
            optional=False,
            error_class=ErrorClass.NOT_FOUND,
            replan_count=2,
            max_replans=2,
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") in ("replan_budget_exhausted", "required_step_not_found")
        assert "plan" not in delta  # Not skipped

        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "fail"

    @pytest.mark.asyncio
    async def test_optional_step_prefers_retry_if_retryable(self) -> None:
        """§10.2: retryable error with retries remaining retries even if step is optional."""
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s1",
            optional=True,
            error_class=ErrorClass.TRANSIENT,
            retry_count=0,
            max_retries=2,
        )

        delta = await handlers.recover(state)
        assert delta.get("retry_count") == {"s1": 1}
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "execute_tool"

    @pytest.mark.asyncio
    async def test_optional_step_skips_after_retry_budget_exhausted(self) -> None:
        """When retries are spent on an optional step, it skips rather than failing."""
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s1",
            optional=True,
            error_class=ErrorClass.TRANSIENT,
            attempt=3,
            retry_count=2,
            max_retries=2,
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "optional_step_skipped"
        plan = delta.get("plan")
        assert plan is not None
        assert plan.step("s1").status == StepStatus.SKIPPED

        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "decide"


# ===========================================================================
# 4. FAIL & Terminal Safety
# ===========================================================================
class TestFailMechanics:
    @pytest.mark.asyncio
    async def test_policy_violation_fails_immediately(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.POLICY_VIOLATION,
            optional=True,  # Even if optional!
            retry_count=0,
            max_retries=10,
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "terminal_error_policy_violation"
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "fail"

    @pytest.mark.asyncio
    async def test_internal_error_fails_immediately(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.INTERNAL,
            retry_count=0,
            max_retries=10,
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "terminal_error_internal"
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "fail"

    @pytest.mark.asyncio
    async def test_cancelled_run_cannot_recover(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.TRANSIENT,
            run_status=RunStatus.CANCELLED,
            status_reason="operator_cancelled",
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "operator_cancelled"
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "fail"

    @pytest.mark.asyncio
    async def test_completed_run_cannot_recover(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.TRANSIENT,
            run_status=RunStatus.COMPLETED,
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "terminal_run"
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "fail"


# ===========================================================================
# 5. Boundary Conditions & Stale Errors
# ===========================================================================
class TestBoundaryConditions:
    @pytest.mark.asyncio
    async def test_max_retries_zero_never_retries(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.TRANSIENT,
            attempt=1,
            retry_count=0,
            max_retries=0,
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "retry_budget_exhausted"
        assert "retry_count" not in delta

        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "fail"

    @pytest.mark.asyncio
    async def test_max_replans_zero_never_replans(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.INPUT_VALIDATION,
            replan_count=0,
            max_replans=0,
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "replan_budget_exhausted"
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "fail"

    @pytest.mark.asyncio
    async def test_deadline_passed_terminates_recovery(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.TRANSIENT,
            deadline_at=TEST_NOW - timedelta(seconds=1),
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "deadline_exceeded"
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "fail"

    @pytest.mark.asyncio
    async def test_max_steps_exhausted_terminates_recovery(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.TRANSIENT,
            step_count=25,
            max_steps=25,
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "budget_exhausted"
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "fail"

    @pytest.mark.asyncio
    async def test_stale_error_from_previous_step_fails_closed(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        # current_step_id is s2, but latest error was on s1
        state = make_test_state(
            step_id="s2",
            target_err_step_id="s1",
            error_class=ErrorClass.TRANSIENT,
        )

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "stale_error"
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "fail"

    @pytest.mark.asyncio
    async def test_missing_error_fails_closed(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state()
        state["errors"] = []

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "missing_error"
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "fail"

    @pytest.mark.asyncio
    async def test_missing_current_step_fails_closed(self) -> None:
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        state = make_test_state()
        state["current_step_id"] = None

        delta = await handlers.recover(state)
        assert delta.get("status_reason") == "missing_current_step"
        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "fail"


# ===========================================================================
# 6. Crash / Resume Safety
# ===========================================================================
class TestCrashResumeSafety:
    @pytest.mark.asyncio
    async def test_re_entering_recover_does_not_double_increment_retry(self) -> None:
        """When recover is resumed from checkpoint where retry_count was already updated."""
        clock = FixedClock(TEST_NOW)
        handlers = NodeHandlers(clock=clock)
        # Attempt 1 failed, but retry_count is already 1 (checkpoint saved after recover increment)
        state = make_test_state(
            step_id="s1",
            error_class=ErrorClass.TRANSIENT,
            attempt=1,
            retry_count=1,
            max_retries=2,
        )

        delta = await handlers.recover(state)
        # Must NOT increment to 2, and must NOT sleep again!
        assert delta.get("retry_count") == {"s1": 1}
        assert clock.slept_seconds == 0.0

        updated_state = {**state, **delta}
        assert handlers.route_after_recover(updated_state) == "execute_tool"


# ===========================================================================
# 7. Graph Integration Tests
# ===========================================================================
class TestGraphIntegration:
    @pytest.mark.asyncio
    async def test_graph_retries_and_succeeds(self) -> None:
        """Tool fails on attempt 1, recover increments retry and routes back to execute_tool,
        which then succeeds on attempt 2."""
        clock = FixedClock(TEST_NOW)
        attempts = 0

        async def execute_mock(state: AgentState) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            step_id = state["current_step_id"]
            retries = state.get("retry_count", {}).get(step_id, 0)
            attempt = 1 + retries

            if attempt == 1:
                return {
                    "errors": [
                        AgentError(
                            step_id=step_id,
                            error_class=ErrorClass.TRANSIENT,
                            message="network blip",
                            attempt=1,
                            occurred_at=clock.now(),
                        )
                    ],
                    "tool_calls": [
                        ToolCall(
                            step_id=step_id,
                            tool=ToolName.GET_LEAD,
                            attempt=1,
                            args_hash="hash",
                            status="failed",
                            error_class=ErrorClass.TRANSIENT,
                            error_message="network blip",
                        )
                    ],
                    "step_count": state.get("step_count", 0) + 1,
                }
            # Attempt 2 succeeds
            plan = state["plan"]
            updated_steps = [
                s.model_copy(update={"status": StepStatus.SUCCEEDED}) if s.step_id == step_id else s
                for s in plan.steps
            ]
            return {
                "plan": plan.model_copy(update={"steps": updated_steps}),
                "tool_calls": [
                    ToolCall(
                        step_id=step_id,
                        tool=ToolName.GET_LEAD,
                        attempt=2,
                        args_hash="hash",
                        status="succeeded",
                    )
                ],
                "step_count": state.get("step_count", 0) + 1,
            }

        handlers = NodeHandlers(clock=clock)
        # Wrap execute_tool
        handlers.execute_tool = execute_mock  # type: ignore[method-assign]

        graph = create_agent_graph(checkpointer=MemorySaver(), node_handlers=handlers)
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(
                    step_id="s1",
                    tool=ToolName.GET_LEAD,
                    args={"lead_id": "lead_1"},
                    status=StepStatus.PENDING,
                )
            ],
        )
        init_state = create_initial_state("run_1", "Find fintech leads", clock=clock, plan=plan)

        final_state = await graph.ainvoke(init_state, {"configurable": {"thread_id": "run_1"}})
        assert attempts == 2
        assert final_state.get("retry_count", {}).get("s1") == 1
        assert clock.slept_seconds == 0.25  # exactly one 250ms backoff
        # Successful completion
        assert final_state["status"] == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_graph_permanently_failing_tool_reaches_exact_attempts(self) -> None:
        """§10.5 acceptance test: exactly 1 + MAX_RETRIES attempts on a permanently failing tool."""
        clock = FixedClock(TEST_NOW)
        attempts = 0
        max_retries = 2

        async def failing_execute(state: AgentState) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            step_id = state["current_step_id"]
            retries = state.get("retry_count", {}).get(step_id, 0)
            attempt = 1 + retries
            return {
                "errors": [
                    AgentError(
                        step_id=step_id,
                        error_class=ErrorClass.TRANSIENT,
                        message="upstream 503",
                        attempt=attempt,
                        occurred_at=clock.now(),
                    )
                ],
                "tool_calls": [
                    ToolCall(
                        step_id=step_id,
                        tool=ToolName.GET_LEAD,
                        attempt=attempt,
                        args_hash="hash",
                        status="failed",
                        error_class=ErrorClass.TRANSIENT,
                        error_message="upstream 503",
                    )
                ],
                "step_count": state.get("step_count", 0) + 1,
            }

        handlers = NodeHandlers(clock=clock)
        handlers.execute_tool = failing_execute  # type: ignore[method-assign]

        graph = create_agent_graph(checkpointer=MemorySaver(), node_handlers=handlers)
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(
                    step_id="s1",
                    tool=ToolName.GET_LEAD,
                    args={"lead_id": "lead_1"},
                    status=StepStatus.PENDING,
                )
            ],
        )
        meta = RunMetadata(budgets=Budgets(max_retries=max_retries))
        init_state = create_initial_state(
            "run_fail", "Find fintech leads", clock=clock, plan=plan, metadata=meta
        )

        final_state = await graph.ainvoke(init_state, {"configurable": {"thread_id": "run_fail"}})
        # Exactly 1 + MAX_RETRIES = 3 attempts
        assert attempts == 1 + max_retries
        assert final_state.get("retry_count", {}).get("s1") == max_retries
        # First retry 250ms, second retry 500ms -> total 750ms
        assert clock.slept_seconds == 0.75
        assert final_state["status"] == RunStatus.FAILED
        assert final_state.get("status_reason") == "retry_budget_exhausted"

    @pytest.mark.asyncio
    async def test_graph_replannable_failure_routes_to_plan(self) -> None:
        """execute_tool failure with replannable error -> recover -> plan."""
        clock = FixedClock(TEST_NOW)
        plan_called = False

        async def failing_execute(state: AgentState) -> dict[str, Any]:
            step_id = state["current_step_id"]
            return {
                "errors": [
                    AgentError(
                        step_id=step_id,
                        error_class=ErrorClass.INPUT_VALIDATION,
                        message="invalid lead payload",
                        attempt=1,
                        occurred_at=clock.now(),
                    )
                ],
                "tool_calls": [
                    ToolCall(
                        step_id=step_id,
                        tool=ToolName.GET_LEAD,
                        attempt=1,
                        args_hash="hash",
                        status="failed",
                        error_class=ErrorClass.INPUT_VALIDATION,
                        error_message="invalid lead payload",
                    )
                ],
                "step_count": state.get("step_count", 0) + 1,
            }

        async def mock_plan(state: AgentState) -> dict[str, Any]:
            nonlocal plan_called
            plan_called = True
            # Revised plan clears the fault and finishes
            return {
                "plan": Plan(plan_id="revised", steps=[]),
                "replan_count": state.get("replan_count", 0) + 1,
            }

        handlers = NodeHandlers(clock=clock, plan_handler=mock_plan)
        handlers.execute_tool = failing_execute  # type: ignore[method-assign]

        graph = create_agent_graph(checkpointer=MemorySaver(), node_handlers=handlers)
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(
                    step_id="s1",
                    tool=ToolName.GET_LEAD,
                    args={"lead_id": "lead_1"},
                    status=StepStatus.PENDING,
                )
            ],
        )
        init_state = create_initial_state(
            "run_replan", "Find fintech leads", clock=clock, plan=plan
        )

        final_state = await graph.ainvoke(init_state, {"configurable": {"thread_id": "run_replan"}})
        assert plan_called is True
        assert final_state["status"] == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_graph_optional_step_failure_skips_and_completes_next_step(self) -> None:
        """execute_tool optional failure -> recover -> decide -> next step succeeds."""
        clock = FixedClock(TEST_NOW)
        executed_steps: list[str] = []

        async def execute_steps(state: AgentState) -> dict[str, Any]:
            step_id = state["current_step_id"]
            executed_steps.append(step_id)
            plan = state["plan"]

            if step_id == "s1":  # optional step fails with NOT_FOUND
                return {
                    "errors": [
                        AgentError(
                            step_id=step_id,
                            error_class=ErrorClass.NOT_FOUND,
                            message="company not found",
                            attempt=1,
                            occurred_at=clock.now(),
                        )
                    ],
                    "tool_calls": [
                        ToolCall(
                            step_id=step_id,
                            tool=ToolName.RESEARCH_COMPANY,
                            attempt=1,
                            args_hash="hash",
                            status="failed",
                            error_class=ErrorClass.NOT_FOUND,
                            error_message="company not found",
                        )
                    ],
                    "step_count": state.get("step_count", 0) + 1,
                }
            # s2 succeeds
            updated = [
                s.model_copy(update={"status": StepStatus.SUCCEEDED}) if s.step_id == step_id else s
                for s in plan.steps
            ]
            return {
                "plan": plan.model_copy(update={"steps": updated}),
                "tool_calls": [
                    ToolCall(
                        step_id=step_id,
                        tool=ToolName.GET_LEAD,
                        attempt=1,
                        args_hash="hash",
                        status="succeeded",
                    )
                ],
                "step_count": state.get("step_count", 0) + 1,
            }

        handlers = NodeHandlers(clock=clock)
        handlers.execute_tool = execute_steps  # type: ignore[method-assign]

        graph = create_agent_graph(checkpointer=MemorySaver(), node_handlers=handlers)
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(
                    step_id="s1",
                    tool=ToolName.RESEARCH_COMPANY,
                    args={"company_id": "c_unknown"},
                    optional=True,
                    status=StepStatus.PENDING,
                ),
                PlanStep(
                    step_id="s2",
                    tool=ToolName.GET_LEAD,
                    args={"lead_id": "lead_1"},
                    optional=False,
                    status=StepStatus.PENDING,
                ),
            ],
        )
        init_state = create_initial_state("run_skip", "Find fintech leads", clock=clock, plan=plan)

        final_state = await graph.ainvoke(init_state, {"configurable": {"thread_id": "run_skip"}})
        assert executed_steps == ["s1", "s2"]
        final_plan = final_state["plan"]
        assert final_plan.step("s1").status == StepStatus.SKIPPED
        assert final_plan.step("s2").status == StepStatus.SUCCEEDED
        assert final_state["status"] == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_graph_unrecoverable_failure_routes_to_fail(self) -> None:
        """execute_tool policy violation -> recover -> fail."""
        clock = FixedClock(TEST_NOW)

        async def violating_execute(state: AgentState) -> dict[str, Any]:
            step_id = state["current_step_id"]
            return {
                "errors": [
                    AgentError(
                        step_id=step_id,
                        error_class=ErrorClass.POLICY_VIOLATION,
                        message="unauthorized action",
                        attempt=1,
                        occurred_at=clock.now(),
                    )
                ],
                "tool_calls": [
                    ToolCall(
                        step_id=step_id,
                        tool=ToolName.GET_LEAD,
                        attempt=1,
                        args_hash="hash",
                        status="failed",
                        error_class=ErrorClass.POLICY_VIOLATION,
                        error_message="unauthorized action",
                    )
                ],
                "step_count": state.get("step_count", 0) + 1,
            }

        handlers = NodeHandlers(clock=clock)
        handlers.execute_tool = violating_execute  # type: ignore[method-assign]

        graph = create_agent_graph(checkpointer=MemorySaver(), node_handlers=handlers)
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(
                    step_id="s1",
                    tool=ToolName.GET_LEAD,
                    args={"lead_id": "lead_1"},
                    status=StepStatus.PENDING,
                )
            ],
        )
        init_state = create_initial_state("run_pol", "Find fintech leads", clock=clock, plan=plan)

        final_state = await graph.ainvoke(init_state, {"configurable": {"thread_id": "run_pol"}})
        assert final_state["status"] == RunStatus.FAILED
        assert final_state.get("status_reason") == "terminal_error_policy_violation"


# ===========================================================================
# 8. Structural AST Invariants
# ===========================================================================
class TestStructuralInvariants:
    def test_recover_does_not_call_tool_dispatch(self) -> None:
        """Recover must NEVER invoke ToolRegistry.dispatch()."""
        from app.agent import nodes

        source = inspect.getsource(nodes.NodeHandlers.recover)
        assert "dispatch" not in source
        assert "registry" not in source

    def test_recover_does_not_import_mock_adapters_or_orm(self) -> None:
        """No mock adapters, SQL or ORM queries in nodes.py."""
        from app.agent import nodes

        src = inspect.getsource(nodes)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("app.integrations.mock")
                assert not node.module.startswith("sqlalchemy")
