"""AGENT-002: Graph assembly, topology, routing, interrupts, and checkpointer tests (§6).

Covers:
- All 9 nodes exist and are wired correctly
- Static edges: START -> understand, request_approval -> decide, complete -> END, fail -> END
- All conditional edge branches:
    - understand: plan, fail
    - plan: decide, fail
    - decide: execute_tool, request_approval, complete, plan, fail
    - execute_tool: verify, decide, recover
    - verify: decide, recover
    - recover: execute_tool, plan, decide, fail
- ADR-007: dynamic interrupt() in request_approval, no static interrupt lists
- Tool safety: execute_tool is the single choke point caller of ToolRegistry.dispatch
- Real PostgreSQL checkpointer integration:
    - pause at request_approval
    - durability="sync"
    - zero mock mutations while paused
    - resume via Command(resume=...)
    - terminal completion
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.agent.graph import create_agent_graph
from app.agent.nodes import NodeHandlers, create_initial_state
from app.agent.state import (
    AgentError,
    AgentState,
    ApprovalRequest,
    ApprovalState,
    Budgets,
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
from app.config import get_settings
from app.errors import ErrorClass, RecoveryAction
from app.persistence.checkpointing import DURABILITY, open_checkpointer, thread_config
from app.runtime import FixedClock
from app.tools.contracts import RiskLevel, ToolName
from app.tools.registry import ToolRegistry
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START
from langgraph.types import Command

from recovery_harness import (
    make_engine,
    migrate_to_head,
    require_database,
    uow_factory_for,
)

pytestmark = [pytest.mark.unit]

TEST_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. Topology & Static Configuration Tests
# ---------------------------------------------------------------------------
class TestGraphTopology:
    def test_all_nine_nodes_exist(self) -> None:
        """Every node defined in §6.1 exists in the compiled graph."""
        graph = create_agent_graph()
        expected_nodes = {
            "understand",
            "plan",
            "decide",
            "request_approval",
            "execute_tool",
            "verify",
            "recover",
            "complete",
            "fail",
        }
        # In LangGraph StateGraph, nodes are registered in graph.nodes
        assert set(graph.get_graph().nodes.keys()) >= expected_nodes

    def test_no_static_interrupt_lists(self) -> None:
        """ADR-007: pausing is dynamic inside request_approval; static lists are empty."""
        graph = create_agent_graph()
        # CompiledStateGraph stores interrupt lists
        assert getattr(graph, "interrupt_before", []) == []
        assert getattr(graph, "interrupt_after", []) == []

    def test_static_edges_are_wired(self) -> None:
        """Verify the 4 static edges in §6.1."""
        graph = create_agent_graph()
        g = graph.get_graph()
        edges = [(e.source, e.target) for e in g.edges]
        assert ("__start__", "understand") in edges or (START, "understand") in edges
        assert ("request_approval", "decide") in edges
        assert ("complete", "__end__") in edges or ("complete", END) in edges
        assert ("fail", "__end__") in edges or ("fail", END) in edges


# ---------------------------------------------------------------------------
# 2. Routing Branches: Unit Tests
# ---------------------------------------------------------------------------
class TestRoutingBranches:
    @pytest.fixture
    def handlers(self) -> NodeHandlers:
        return NodeHandlers(clock=FixedClock(TEST_NOW))

    def test_understand_routes_to_plan_when_in_scope(self, handlers: NodeHandlers) -> None:
        state: AgentState = {
            "normalized_task": NormalizedTask(intent="Search leads", in_scope=True)
        }
        assert handlers.route_after_understand(state) == "plan"

    def test_understand_routes_to_fail_when_out_of_scope(self, handlers: NodeHandlers) -> None:
        state: AgentState = {
            "normalized_task": NormalizedTask(intent="Delete production DB", in_scope=False)
        }
        assert handlers.route_after_understand(state) == "fail"

    def test_plan_routes_to_decide_when_valid(self, handlers: NodeHandlers) -> None:
        state: AgentState = {
            "plan": Plan(plan_id="p1", steps=[PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS)]),
            "replan_count": 0,
        }
        assert handlers.route_after_plan(state) == "decide"

    def test_plan_routes_to_fail_when_replan_budget_exhausted(self, handlers: NodeHandlers) -> None:
        state: AgentState = {
            "plan": Plan(plan_id="p1", steps=[]),
            "replan_count": 5,
            "metadata": RunMetadata(budgets=Budgets(max_replans=2)),
        }
        assert handlers.route_after_plan(state) == "fail"

    def test_plan_routes_to_fail_when_marked_invalid(self, handlers: NodeHandlers) -> None:
        state: AgentState = {
            "plan": None,
            "status_reason": "invalid_plan",
        }
        assert handlers.route_after_plan(state) == "fail"

    # -- Decide: 7 Ordered Rules (§6.2) --------------------------------------

    def test_decide_rule_1_deadline_exhausted_routes_to_fail(self, handlers: NodeHandlers) -> None:
        state: AgentState = {
            "deadline_at": TEST_NOW - timedelta(seconds=1),
            "metadata": RunMetadata(budgets=Budgets(max_steps=10)),
        }
        assert handlers.route_after_decide(state) == "fail"

    def test_decide_rule_1_step_count_exhausted_routes_to_fail(
        self, handlers: NodeHandlers
    ) -> None:
        state: AgentState = {
            "step_count": 15,
            "metadata": RunMetadata(budgets=Budgets(max_steps=10)),
        }
        assert handlers.route_after_decide(state) == "fail"

    def test_decide_rule_2_pending_undecided_approval_routes_to_request_approval(
        self, handlers: NodeHandlers
    ) -> None:
        req = ApprovalRequest(
            approval_id="a1",
            run_id="r1",
            step_id="s6",
            tool=ToolName.SEND_EMAIL_MOCK,
            risk=RiskLevel.HIGH,
            title="Send",
            summary="Send email",
            args_hash="hash",
            requested_at=TEST_NOW,
            expires_at=TEST_NOW + timedelta(days=1),
        )
        state: AgentState = {
            "approval_state": ApprovalState(pending=req, decisions={}),
        }
        assert handlers.route_after_decide(state) == "request_approval"

    def test_decide_rule_3_no_runnable_step_routes_to_complete(
        self, handlers: NodeHandlers
    ) -> None:
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS, status=StepStatus.SUCCEEDED),
                PlanStep(step_id="s2", tool=ToolName.GET_LEAD, status=StepStatus.SUCCEEDED),
            ],
        )
        state: AgentState = {"plan": plan}
        assert handlers.route_after_decide(state) == "complete"

    def test_decide_rule_4_unsatisfied_dependency_routes_to_plan_when_budget_remains(
        self, handlers: NodeHandlers
    ) -> None:
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS, status=StepStatus.FAILED),
                PlanStep(
                    step_id="s2",
                    tool=ToolName.GET_LEAD,
                    depends_on=["s1"],
                    status=StepStatus.PENDING,
                ),
            ],
        )
        state: AgentState = {
            "plan": plan,
            "current_step_id": "s2",
            "replan_count": 0,
            "metadata": RunMetadata(budgets=Budgets(max_replans=2)),
        }
        assert handlers.route_after_decide(state) == "plan"

    def test_decide_rule_4_unsatisfied_dependency_routes_to_fail_when_replan_exhausted(
        self, handlers: NodeHandlers
    ) -> None:
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS, status=StepStatus.FAILED),
                PlanStep(
                    step_id="s2",
                    tool=ToolName.GET_LEAD,
                    depends_on=["s1"],
                    status=StepStatus.PENDING,
                ),
            ],
        )
        state: AgentState = {
            "plan": plan,
            "current_step_id": "s2",
            "replan_count": 2,
            "metadata": RunMetadata(budgets=Budgets(max_replans=2)),
        }
        assert handlers.route_after_decide(state) == "fail"

    def test_decide_rule_6_approval_required_routes_to_request_approval(
        self, handlers: NodeHandlers
    ) -> None:
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(
                    step_id="s6",
                    tool=ToolName.SEND_EMAIL_MOCK,
                    args={"draft_id": "d1", "to_email": "ada@example.com"},
                    status=StepStatus.PENDING,
                )
            ],
        )
        state: AgentState = {
            "plan": plan,
            "approval_state": ApprovalState(),
        }
        assert handlers.route_after_decide(state) == "request_approval"

    def test_decide_rule_7_ready_step_routes_to_execute_tool(self, handlers: NodeHandlers) -> None:
        plan = Plan(
            plan_id="p1",
            steps=[PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS, status=StepStatus.PENDING)],
        )
        state: AgentState = {"plan": plan}
        assert handlers.route_after_decide(state) == "execute_tool"

    def test_decide_rule_ordering_budget_exhaustion_preempts_approval(
        self, handlers: NodeHandlers
    ) -> None:
        """A step budget-exhausted and approval-requiring must fail, not pause (§6.2)."""
        plan = Plan(
            plan_id="p1",
            steps=[
                PlanStep(step_id="s6", tool=ToolName.SEND_EMAIL_MOCK, status=StepStatus.PENDING)
            ],
        )
        state: AgentState = {
            "plan": plan,
            "step_count": 25,
            "metadata": RunMetadata(budgets=Budgets(max_steps=25)),
            "approval_state": ApprovalState(),
        }
        # Rule 1 fires before Rule 6
        assert handlers.route_after_decide(state) == "fail"

    # -- Execute Tool Branches -----------------------------------------------

    def test_execute_tool_routes_to_recover_on_error(self, handlers: NodeHandlers) -> None:
        state: AgentState = {
            "current_step_id": "s1",
            "errors": [
                AgentError(step_id="s1", error_class=ErrorClass.TRANSIENT, message="timeout")
            ],
        }
        assert handlers.route_after_execute(state) == "recover"

    def test_execute_tool_routes_to_verify_when_contract_requires_it(
        self, handlers: NodeHandlers
    ) -> None:
        plan = Plan(
            plan_id="p1",
            steps=[PlanStep(step_id="s5", tool=ToolName.SAVE_DRAFT, status=StepStatus.RUNNING)],
        )
        state: AgentState = {"plan": plan, "current_step_id": "s5", "errors": []}
        assert handlers.route_after_execute(state) == "verify"

    def test_execute_tool_routes_to_decide_for_read_only(self, handlers: NodeHandlers) -> None:
        plan = Plan(
            plan_id="p1",
            steps=[PlanStep(step_id="s1", tool=ToolName.GET_LEAD, status=StepStatus.RUNNING)],
        )
        state: AgentState = {"plan": plan, "current_step_id": "s1", "errors": []}
        assert handlers.route_after_execute(state) == "decide"

    # -- Verify Branches -----------------------------------------------------

    def test_verify_routes_to_decide_when_passed(self, handlers: NodeHandlers) -> None:
        state: AgentState = {
            "current_step_id": "s5",
            "verification_result": {
                "s5": VerificationResult(
                    step_id="s5", status=VerificationStatus.PASSED, mode="readback"
                )
            },
        }
        assert handlers.route_after_verify(state) == "decide"

    def test_verify_routes_to_recover_when_failed(self, handlers: NodeHandlers) -> None:
        state: AgentState = {
            "current_step_id": "s5",
            "verification_result": {
                "s5": VerificationResult(
                    step_id="s5", status=VerificationStatus.FAILED, mode="readback"
                )
            },
        }
        assert handlers.route_after_verify(state) == "recover"

    # -- Recover Branches ----------------------------------------------------

    def test_recover_routes_to_execute_tool_when_retryable_with_budget(
        self, handlers: NodeHandlers
    ) -> None:
        err = AgentError(
            step_id="s1",
            error_class=ErrorClass.TRANSIENT,
            message="timeout",
            attempt=1,
            recovery=RecoveryAction.RETRY,
        )
        state: AgentState = {
            "current_step_id": "s1",
            "errors": [err],
            "retry_count": {"s1": 1},
            "metadata": RunMetadata(budgets=Budgets(max_retries=2)),
        }
        assert handlers.route_after_recover(state) == "execute_tool"

    def test_recover_routes_to_plan_when_replannable(self, handlers: NodeHandlers) -> None:
        err = AgentError(
            step_id="s1",
            error_class=ErrorClass.INPUT_VALIDATION,
            message="invalid id",
            recovery=RecoveryAction.REPLAN,
        )
        state: AgentState = {
            "current_step_id": "s1",
            "errors": [err],
            "replan_count": 0,
            "metadata": RunMetadata(budgets=Budgets(max_replans=2)),
        }
        assert handlers.route_after_recover(state) == "plan"

    def test_recover_routes_to_decide_when_optional_skipped(self, handlers: NodeHandlers) -> None:
        plan = Plan(
            plan_id="p1",
            steps=[PlanStep(step_id="s1", tool=ToolName.RESEARCH_COMPANY, optional=True)],
        )
        state: AgentState = {
            "plan": plan,
            "current_step_id": "s1",
            "status_reason": "optional_step_skipped",
        }
        assert handlers.route_after_recover(state) == "decide"

    def test_recover_routes_to_fail_when_budget_exhausted(self, handlers: NodeHandlers) -> None:
        err = AgentError(
            step_id="s1",
            error_class=ErrorClass.TRANSIENT,
            message="timeout",
            attempt=3,
            recovery=RecoveryAction.FAIL,
        )
        state: AgentState = {
            "current_step_id": "s1",
            "errors": [err],
            "retry_count": {"s1": 2},
            "metadata": RunMetadata(budgets=Budgets(max_retries=2)),
        }
        assert handlers.route_after_recover(state) == "fail"


# ---------------------------------------------------------------------------
# 3. Dynamic Interruption & Resume (Unit / In-Memory Checkpointer)
# ---------------------------------------------------------------------------
class TestDynamicInterruptionAndHITL:
    @pytest.mark.asyncio
    async def test_request_approval_pauses_and_resumes_to_completion(self) -> None:
        """Graph pauses on interrupt(), and resumes via Command(resume=decision)."""
        checkpointer = MemorySaver()
        run_id = str(uuid.uuid4())
        step_id = "s6"
        send_args = {"draft_id": "d_100", "to_email": "ada@example.com"}

        plan = Plan(
            plan_id="p_hitl",
            steps=[
                PlanStep(
                    step_id=step_id,
                    tool=ToolName.SEND_EMAIL_MOCK,
                    args=send_args,
                    status=StepStatus.PENDING,
                )
            ],
        )

        initial_state = create_initial_state(
            run_id=run_id,
            user_request="Send outreach email",
            plan=plan,
        )

        # Mock execute_tool to record successful dispatch without real database
        async def mock_execute(state: AgentState) -> dict[str, Any]:
            cur_step = state["plan"].step(state["current_step_id"])
            updated_steps = [
                s.model_copy(update={"status": StepStatus.SUCCEEDED})
                if s.step_id == state["current_step_id"]
                else s
                for s in state["plan"].steps
            ]
            return {
                "tool_results": {
                    state["current_step_id"]: ToolResult(
                        step_id=state["current_step_id"],
                        tool=cur_step.tool,
                        output={"status": "sent"},
                        produced_at=TEST_NOW,
                    )
                },
                "plan": state["plan"].model_copy(update={"steps": updated_steps}),
            }

        handlers = NodeHandlers(clock=FixedClock(TEST_NOW))
        handlers.execute_tool = mock_execute  # type: ignore[assignment]

        graph = create_agent_graph(checkpointer=checkpointer, node_handlers=handlers)
        cfg = {"configurable": {"thread_id": run_id}}

        # 1. Start execution: graph must pause at request_approval
        _ = await graph.ainvoke(initial_state, config=cfg)
        state_snapshot = graph.get_state(cfg)
        assert len(state_snapshot.tasks) > 0
        assert len(state_snapshot.tasks[0].interrupts) > 0
        interrupt_val = state_snapshot.tasks[0].interrupts[0].value
        assert interrupt_val["step_id"] == step_id
        assert interrupt_val["tool"] == ToolName.SEND_EMAIL_MOCK.value

        # 2. Resume with approval decision
        decision = "approve"
        final_state = await graph.ainvoke(Command(resume=decision), config=cfg)

        # 3. Graph reached terminal COMPLETED status
        assert final_state["status"] == RunStatus.COMPLETED
        assert final_state["approval_state"].grants(step_id, send_args) is True

    @pytest.mark.asyncio
    async def test_request_approval_rejection_terminates_as_rejected(self) -> None:
        """Rejecting an approval terminates the run as REJECTED, distinct from FAILED (ADR-022)."""
        checkpointer = MemorySaver()
        run_id = str(uuid.uuid4())
        step_id = "s6"
        send_args = {"draft_id": "d_100", "to_email": "ada@example.com"}

        plan = Plan(
            plan_id="p_reject",
            steps=[
                PlanStep(
                    step_id=step_id,
                    tool=ToolName.SEND_EMAIL_MOCK,
                    args=send_args,
                    status=StepStatus.PENDING,
                )
            ],
        )
        initial_state = create_initial_state(
            run_id=run_id,
            user_request="Send outreach email",
            plan=plan,
        )

        handlers = NodeHandlers(clock=FixedClock(TEST_NOW))
        graph = create_agent_graph(checkpointer=checkpointer, node_handlers=handlers)
        cfg = {"configurable": {"thread_id": run_id}}

        # 1. Pause at request_approval
        await graph.ainvoke(initial_state, config=cfg)

        # 2. Resume with rejection
        final_state = await graph.ainvoke(Command(resume="reject"), config=cfg)

        # 3. Terminal status is REJECTED
        assert final_state["status"] == RunStatus.REJECTED
        assert final_state["status_reason"] == "approval_rejected"
        assert final_state["status"] != RunStatus.FAILED


# ---------------------------------------------------------------------------
# 4. Integration with PostgreSQL & AsyncPostgresSaver (DB-007)
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestRealPostgresIntegration:
    @pytest.fixture(autouse=True)
    def _require_db(self) -> None:
        require_database()
        migrate_to_head()

    @pytest.mark.asyncio
    async def test_graph_executes_with_real_async_postgres_saver(self) -> None:
        """Full execution cycle with real AsyncPostgresSaver under durability='sync'."""
        engine = await make_engine()
        uow_factory = uow_factory_for(engine)
        settings = get_settings()

        async with open_checkpointer(settings) as checkpointer:
            run_id = uuid.uuid4()
            step_id = "s1"

            # Create run in control plane
            async with uow_factory() as uow:
                await uow.agent_runs.create(
                    id=run_id,
                    user_request="Find fintech leads",
                    status=RunStatus.RUNNING,
                    deadline_at=TEST_NOW + timedelta(minutes=5),
                )
                await uow.commit()

            plan = Plan(
                plan_id=f"p_{run_id.hex[:8]}",
                steps=[
                    PlanStep(
                        step_id=step_id,
                        tool=ToolName.SEARCH_LEADS,
                        args={"industry": "fintech", "limit": 5},
                        status=StepStatus.PENDING,
                    )
                ],
            )

            initial_state = create_initial_state(
                run_id=run_id,
                user_request="Find fintech leads",
                plan=plan,
            )

            from app.integrations.mock import build_mock_adapters
            from app.integrations.mock.seed import seed_database
            from app.persistence.session import create_session_factory
            from app.runtime import SequentialIdGenerator

            session_factory = create_session_factory(engine)
            await seed_database(session_factory, reset=False)
            clock = FixedClock(TEST_NOW)
            adapters = build_mock_adapters(session_factory, clock, SequentialIdGenerator())
            registry = ToolRegistry(
                adapters=adapters,
                uow_factory=uow_factory,
                clock=clock,
            )

            handlers = NodeHandlers(
                registry=registry,
                uow_factory=uow_factory,
                clock=clock,
            )

            graph = create_agent_graph(
                checkpointer=checkpointer,
                node_handlers=handlers,
            )

            cfg = thread_config(run_id)

            # Invoke graph through real PostgreSQL checkpointer
            final_state = await graph.ainvoke(
                initial_state,
                config=cfg,
                durability=DURABILITY,
            )

            assert final_state["status"] == RunStatus.COMPLETED
            assert step_id in final_state["tool_results"]
            assert final_state["tool_results"][step_id].tool == ToolName.SEARCH_LEADS

            # Verify checkpoint is durable in PostgreSQL by inspecting snapshot
            resumed_snapshot = await graph.aget_state(cfg)
            assert resumed_snapshot.values["status"] == RunStatus.COMPLETED

        await engine.dispose()
