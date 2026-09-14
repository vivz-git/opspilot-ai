"""AGENT-004: the `decide` router — seven ordered rules plus fan-out (§6.2, ADR-006).

`decide` is the safety router: rule *ordering* is the security property. Each
rule is tested in isolation, every boundary is pinned, and precedence is
tested by constructing states in which several rules are simultaneously true
and asserting which one wins — including the acceptance case: a step that is
both budget-exhausted and approval-requiring must fail, not pause.

Covers:
- rules 1–7 in isolation, with every boundary condition
- the §5.4 lifecycle guard ahead of rule 1 (a terminal run never re-enters execution)
- precedence when several rules hold at once (a parametrised ladder)
- all five legal routes, and that the router's route names are the graph's edge keys
- fan-out inside the router: expansion, re-evaluation, gate on children, budget
- determinism and purity: identical decisions on repeat, no state mutation,
  no tool dispatched, node delta and conditional edge always agree
- graph integration on `MemorySaver`: fan-out end to end, children count against
  `MAX_STEPS`, each gated child pauses separately, re-entry adds no duplicate
  child, unresolvable fan-out replans a bounded number of times then fails,
  a required rejection ends the run without executing later steps
- graph integration on the real Postgres saver with the real registry
- structural (AST) checks: the router is pure, imports nothing that can do I/O,
  and encodes the rules in exactly one ascending order
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app.agent.decide import (
    STATUS_REASON_BUDGET_EXHAUSTED,
    STATUS_REASON_REPLAN_REQUIRED,
    STATUS_REASON_UNRESOLVABLE_PLAN,
    UNCHANGED,
    Decision,
    DecisionRoute,
    DecisionRule,
    dependencies_satisfied,
    evaluate_decision,
    next_runnable_step,
)
from app.agent.fanout import REF_KEY, FanOutResolutionError, is_expanded
from app.agent.graph import create_agent_graph
from app.agent.nodes import NodeHandlers, create_initial_state
from app.agent.state import (
    TERMINAL_RUN_STATUSES,
    AgentState,
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalRequest,
    ApprovalState,
    Budgets,
    FanOut,
    Plan,
    PlanStep,
    RunMetadata,
    RunStatus,
    StepStatus,
    ToolResult,
)
from app.config import get_settings
from app.persistence.checkpointing import DURABILITY, open_checkpointer, thread_config
from app.runtime import FixedClock
from app.security import canonical_args_hash
from app.tools.contracts import REGISTRY, RiskLevel, ToolName
from app.tools.registry import ToolRegistry
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from recovery_harness import (
    make_engine,
    migrate_to_head,
    require_database,
    uow_factory_for,
)

pytestmark = [pytest.mark.unit]

APP = Path(__file__).resolve().parent.parent / "app"
TEST_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
SEND_ARGS = {"draft_id": "d_1", "to_email": "ada@example.com"}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def literal_args(state: AgentState, step: PlanStep) -> dict[str, Any]:
    return dict(step.args)


def evaluate(state: AgentState, *, now: datetime = TEST_NOW) -> Decision:
    return evaluate_decision(state, now=now, contract_for=REGISTRY.get, resolve_args=literal_args)


def step(
    step_id: str,
    tool: ToolName = ToolName.SEARCH_LEADS,
    *,
    status: StepStatus = StepStatus.PENDING,
    args: dict[str, Any] | None = None,
    depends_on: list[str] | None = None,
    optional: bool = False,
    fanout: FanOut | None = None,
    parent_step_id: str | None = None,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        tool=tool,
        args=args or {},
        depends_on=depends_on or [],
        optional=optional,
        fanout=fanout,
        parent_step_id=parent_step_id,
        status=status,
    )


def plan_of(*steps: PlanStep, plan_id: str = "p1") -> Plan:
    return Plan(plan_id=plan_id, steps=list(steps))


def fanout(over: str = "s1.output.leads", alias: str = "lead", max_items: int = 10) -> FanOut:
    return FanOut.model_validate({"over": over, "as": alias, "max_items": max_items})


def leads(n: int) -> list[dict[str, Any]]:
    return [
        {"lead_id": f"lead_{i}", "company_id": f"co_{i}", "email": f"lead{i}@example.com"}
        for i in range(n)
    ]


def result(
    step_id: str, output: dict[str, Any], tool: ToolName = ToolName.SEARCH_LEADS
) -> ToolResult:
    return ToolResult(step_id=step_id, tool=tool, output=output, produced_at=TEST_NOW)


def pending_request(step_id: str, args: dict[str, Any] | None = None) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=f"appr_{step_id}",
        run_id="r1",
        step_id=step_id,
        tool=ToolName.SEND_EMAIL_MOCK,
        risk=RiskLevel.HIGH,
        title="Send",
        summary="Send email",
        args_hash=canonical_args_hash(args or SEND_ARGS),
        requested_at=TEST_NOW,
        expires_at=TEST_NOW + timedelta(hours=1),
    )


def decision(
    step_id: str, kind: ApprovalDecisionKind, args: dict[str, Any] | None = None
) -> ApprovalDecision:
    return ApprovalDecision(
        approval_id=f"appr_{step_id}",
        step_id=step_id,
        decision=kind,
        args_hash=canonical_args_hash(args or SEND_ARGS),
        decided_by="operator@example.com",
        decided_at=TEST_NOW,
    )


def approved(step_id: str, args: dict[str, Any] | None = None) -> ApprovalState:
    return ApprovalState(decisions={step_id: decision(step_id, ApprovalDecisionKind.APPROVE, args)})


def rejected(step_id: str) -> ApprovalState:
    return ApprovalState(decisions={step_id: decision(step_id, ApprovalDecisionKind.REJECT)})


def budgets(**kwargs: int) -> RunMetadata:
    return RunMetadata(budgets=Budgets(**kwargs))


def fanout_plan(
    *,
    child_tool: ToolName = ToolName.RESEARCH_COMPANY,
    child_args: dict[str, Any] | None = None,
    max_items: int = 10,
    optional: bool = False,
    trailing: tuple[PlanStep, ...] = (),
) -> Plan:
    return plan_of(
        step("s1", status=StepStatus.SUCCEEDED),
        step(
            "s2",
            child_tool,
            args=child_args or {"company_id": {REF_KEY: "lead.company_id"}},
            depends_on=["s1"],
            optional=optional,
            fanout=fanout(max_items=max_items),
        ),
        *trailing,
    )


def fanout_state(n_leads: int = 3, **overrides: Any) -> AgentState:
    state: AgentState = {
        "plan": fanout_plan(),
        "tool_results": {"s1": result("s1", {"leads": leads(n_leads)})},
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


# ---------------------------------------------------------------------------
# 1. Rule 1 — budgets
# ---------------------------------------------------------------------------
class TestRule1BudgetExhausted:
    def test_deadline_passed_fails_with_budget_exhausted(self) -> None:
        d = evaluate({"deadline_at": TEST_NOW - timedelta(microseconds=1)})
        assert d.route is DecisionRoute.FAIL
        assert d.rule is DecisionRule.BUDGET_EXHAUSTED
        assert d.status_reason == STATUS_REASON_BUDGET_EXHAUSTED

    def test_deadline_exactly_now_has_not_passed(self) -> None:
        """`deadline_at passed` is strict: a run is allowed its whole budget."""
        d = evaluate({"deadline_at": TEST_NOW, "plan": plan_of(step("s1"))})
        assert d.rule is DecisionRule.READY

    def test_deadline_is_read_from_the_injected_clock(self) -> None:
        state: AgentState = {"deadline_at": TEST_NOW, "plan": plan_of(step("s1"))}
        assert evaluate(state, now=TEST_NOW + timedelta(seconds=1)).rule is (
            DecisionRule.BUDGET_EXHAUSTED
        )
        assert evaluate(state, now=TEST_NOW - timedelta(seconds=1)).rule is DecisionRule.READY

    def test_missing_deadline_is_not_a_budget_fault(self) -> None:
        assert evaluate({"plan": plan_of(step("s1"))}).rule is DecisionRule.READY

    def test_step_count_at_max_steps_fails(self) -> None:
        d = evaluate(
            {"step_count": 5, "metadata": budgets(max_steps=5), "plan": plan_of(step("s1"))}
        )
        assert d.route is DecisionRoute.FAIL
        assert d.rule is DecisionRule.BUDGET_EXHAUSTED
        assert d.status_reason == STATUS_REASON_BUDGET_EXHAUSTED

    def test_step_count_one_below_max_steps_proceeds(self) -> None:
        d = evaluate(
            {"step_count": 4, "metadata": budgets(max_steps=5), "plan": plan_of(step("s1"))}
        )
        assert d.rule is DecisionRule.READY

    def test_step_count_over_max_steps_fails(self) -> None:
        d = evaluate({"step_count": 99, "metadata": budgets(max_steps=5)})
        assert d.rule is DecisionRule.BUDGET_EXHAUSTED

    def test_default_budgets_apply_when_metadata_is_absent(self) -> None:
        default_max = Budgets().max_steps
        assert evaluate({"step_count": default_max}).rule is DecisionRule.BUDGET_EXHAUSTED
        assert evaluate({"step_count": default_max - 1}).rule is DecisionRule.NO_RUNNABLE_STEP

    def test_budget_failure_keeps_current_step_and_plan(self) -> None:
        """Rule 1 records the reason; it does not rewrite where the run was."""
        d = evaluate({"step_count": 9, "metadata": budgets(max_steps=1), "current_step_id": "s3"})
        assert d.current_step_id == "s3"
        assert d.plan is None
        assert d.state_delta() == {
            "current_step_id": "s3",
            "status_reason": STATUS_REASON_BUDGET_EXHAUSTED,
        }


# ---------------------------------------------------------------------------
# 2. Rule 2 — undecided pending approval
# ---------------------------------------------------------------------------
class TestRule2PendingApproval:
    def test_undecided_pending_approval_re_pauses(self) -> None:
        state: AgentState = {
            "approval_state": ApprovalState(pending=pending_request("s6")),
            "plan": plan_of(step("s1")),
        }
        d = evaluate(state)
        assert d.route is DecisionRoute.REQUEST_APPROVAL
        assert d.rule is DecisionRule.PENDING_APPROVAL
        assert d.current_step_id == "s6", "request_approval reads the pending step"
        assert d.status_reason is UNCHANGED, "a re-pause does not touch the reason"
        assert d.state_delta() == {"current_step_id": "s6"}

    def test_pending_with_a_matching_decision_is_not_undecided(self) -> None:
        """The reducer normally clears this, but rule 2 must not depend on it."""
        pending = pending_request("s6")
        state: AgentState = {
            "approval_state": ApprovalState(
                pending=pending, decisions={"s6": decision("s6", ApprovalDecisionKind.APPROVE)}
            ),
            "plan": plan_of(step("s6", ToolName.SEND_EMAIL_MOCK, args=SEND_ARGS)),
        }
        d = evaluate(state)
        assert d.rule is DecisionRule.READY

    def test_no_pending_request_skips_rule_2(self) -> None:
        d = evaluate({"approval_state": ApprovalState(), "plan": plan_of(step("s1"))})
        assert d.rule is DecisionRule.READY

    def test_pending_approval_pre_empts_completion(self) -> None:
        """Even with nothing else to run, an unanswered pause is re-entered."""
        state: AgentState = {
            "approval_state": ApprovalState(pending=pending_request("s6")),
            "plan": plan_of(step("s1", status=StepStatus.SUCCEEDED)),
        }
        assert evaluate(state).rule is DecisionRule.PENDING_APPROVAL


# ---------------------------------------------------------------------------
# 3. Rule 3 — no runnable step
# ---------------------------------------------------------------------------
class TestRule3NoRunnableStep:
    @pytest.mark.parametrize(
        "state",
        [
            {},
            {"plan": None},
            {"plan": plan_of()},
            {"plan": plan_of(step("s1", status=StepStatus.SUCCEEDED))},
            {"plan": plan_of(step("s1", status=StepStatus.SKIPPED, optional=True))},
            {
                "plan": plan_of(
                    step("s1", status=StepStatus.SUCCEEDED),
                    step("s2", status=StepStatus.SKIPPED, optional=True),
                    step("s3", status=StepStatus.SUCCEEDED),
                )
            },
        ],
        ids=["no-state", "plan-none", "no-steps", "all-succeeded", "all-skipped", "mixed-done"],
    )
    def test_nothing_runnable_completes(self, state: AgentState) -> None:
        d = evaluate(state)
        assert d.route is DecisionRoute.COMPLETE
        assert d.rule is DecisionRule.NO_RUNNABLE_STEP
        assert d.current_step_id is None
        assert d.status_reason is UNCHANGED, "`complete` owns the terminal reason"
        assert d.state_delta() == {"current_step_id": None}

    def test_a_pending_step_is_runnable(self) -> None:
        d = evaluate({"plan": plan_of(step("s1", status=StepStatus.SUCCEEDED), step("s2"))})
        assert d.rule is DecisionRule.READY
        assert d.current_step_id == "s2"

    def test_a_ready_step_is_runnable(self) -> None:
        d = evaluate({"plan": plan_of(step("s1", status=StepStatus.READY))})
        assert d.current_step_id == "s1"

    def test_first_pending_in_plan_order_is_next(self) -> None:
        """Sequential execution (§4.6): plan order is execution order."""
        d = evaluate({"plan": plan_of(step("b"), step("a"))})
        assert d.current_step_id == "b"

    def test_the_active_current_step_keeps_priority(self) -> None:
        """A resumed run continues where it was; it does not skip ahead."""
        plan = plan_of(step("s1"), step("s2", status=StepStatus.RUNNING))
        assert evaluate({"plan": plan, "current_step_id": "s2"}).current_step_id == "s2"
        plan = plan_of(step("s1"), step("s2", status=StepStatus.AWAITING_APPROVAL))
        assert evaluate({"plan": plan, "current_step_id": "s2"}).current_step_id == "s2"

    def test_a_finished_current_step_yields_to_the_next_pending(self) -> None:
        plan = plan_of(step("s1", status=StepStatus.SUCCEEDED), step("s2"))
        assert evaluate({"plan": plan, "current_step_id": "s1"}).current_step_id == "s2"

    def test_a_stale_current_step_id_is_ignored(self) -> None:
        plan = plan_of(step("s1"))
        assert evaluate({"plan": plan, "current_step_id": "gone"}).current_step_id == "s1"

    def test_a_rejected_required_step_ends_the_run_even_with_pending_steps_after_it(
        self,
    ) -> None:
        """§9.6: a required rejected step terminates the run as `rejected`.
        Nothing after it may execute; `complete` computes the terminal status."""
        plan = plan_of(
            step("s6", ToolName.SEND_EMAIL_MOCK, args=SEND_ARGS, status=StepStatus.REJECTED),
            step("s7"),
        )
        d = evaluate({"plan": plan, "approval_state": rejected("s6")})
        assert d.route is DecisionRoute.COMPLETE
        assert d.rule is DecisionRule.NO_RUNNABLE_STEP

    def test_a_reject_decision_alone_blocks_a_required_step_still_marked_pending(
        self,
    ) -> None:
        """Defence in depth: the decision, not only the step status, is authoritative."""
        plan = plan_of(step("s6", ToolName.SEND_EMAIL_MOCK, args=SEND_ARGS), step("s7"))
        d = evaluate({"plan": plan, "approval_state": rejected("s6")})
        assert d.route is DecisionRoute.COMPLETE
        assert d.rule is DecisionRule.NO_RUNNABLE_STEP

    def test_a_rejected_optional_step_is_skipped_and_the_run_continues(self) -> None:
        plan = plan_of(
            step("s6", ToolName.SEND_EMAIL_MOCK, args=SEND_ARGS, optional=True), step("s7")
        )
        d = evaluate({"plan": plan, "approval_state": rejected("s6")})
        assert d.route is DecisionRoute.EXECUTE_TOOL
        assert d.current_step_id == "s7"

    def test_a_rejected_optional_current_step_is_not_re_selected(self) -> None:
        plan = plan_of(
            step("s6", ToolName.SEND_EMAIL_MOCK, args=SEND_ARGS, optional=True), step("s7")
        )
        state: AgentState = {
            "plan": plan,
            "approval_state": rejected("s6"),
            "current_step_id": "s6",
        }
        assert evaluate(state).current_step_id == "s7"

    def test_next_runnable_step_helper_matches_the_router(self) -> None:
        plan = plan_of(step("s1", status=StepStatus.SUCCEEDED), step("s2"), step("s3"))
        chosen = next_runnable_step({"plan": plan}, plan)
        assert chosen is not None and chosen.step_id == "s2"
        assert next_runnable_step({}, None) is None


# ---------------------------------------------------------------------------
# 4. Rule 4 — unsatisfied or unresolvable next step
# ---------------------------------------------------------------------------
class TestRule4Unresolvable:
    @pytest.fixture
    def dependent_plan(self) -> Plan:
        return plan_of(step("s1", status=StepStatus.FAILED), step("s2", depends_on=["s1"]))

    def test_unsatisfied_dependency_replans_while_budget_remains(
        self, dependent_plan: Plan
    ) -> None:
        d = evaluate(
            {"plan": dependent_plan, "replan_count": 0, "metadata": budgets(max_replans=2)}
        )
        assert d.route is DecisionRoute.PLAN
        assert d.rule is DecisionRule.UNRESOLVABLE
        assert d.current_step_id == "s2"
        assert d.status_reason == STATUS_REASON_REPLAN_REQUIRED

    def test_replan_count_one_below_max_still_replans(self, dependent_plan: Plan) -> None:
        d = evaluate(
            {"plan": dependent_plan, "replan_count": 1, "metadata": budgets(max_replans=2)}
        )
        assert d.route is DecisionRoute.PLAN

    def test_replan_count_at_max_fails_unresolvable(self, dependent_plan: Plan) -> None:
        d = evaluate(
            {"plan": dependent_plan, "replan_count": 2, "metadata": budgets(max_replans=2)}
        )
        assert d.route is DecisionRoute.FAIL
        assert d.rule is DecisionRule.UNRESOLVABLE
        assert d.status_reason == STATUS_REASON_UNRESOLVABLE_PLAN
        assert d.current_step_id == "s2", "the failing step is named"

    def test_zero_replan_budget_fails_immediately(self, dependent_plan: Plan) -> None:
        d = evaluate({"plan": dependent_plan, "metadata": budgets(max_replans=0)})
        assert d.route is DecisionRoute.FAIL

    def test_unknown_dependency_is_unresolvable(self) -> None:
        d = evaluate({"plan": plan_of(step("s2", depends_on=["s1"]))})
        assert d.rule is DecisionRule.UNRESOLVABLE

    def test_pending_dependency_is_unsatisfied(self) -> None:
        """Only order-violating plans reach this: `s2` listed before `s1`."""
        d = evaluate({"plan": plan_of(step("s2", depends_on=["s1"]), step("s1"))})
        assert d.rule is DecisionRule.UNRESOLVABLE
        assert d.current_step_id == "s2"

    def test_skipped_optional_dependency_is_unsatisfied(self) -> None:
        plan = plan_of(
            step("s1", optional=True, status=StepStatus.SKIPPED), step("s2", depends_on=["s1"])
        )
        assert evaluate({"plan": plan}).rule is DecisionRule.UNRESOLVABLE

    def test_dependency_satisfied_by_status(self) -> None:
        plan = plan_of(step("s1", status=StepStatus.SUCCEEDED), step("s2", depends_on=["s1"]))
        assert evaluate({"plan": plan}).rule is DecisionRule.READY

    def test_dependency_satisfied_by_artifact(self) -> None:
        """A result in the artifact store proves the step ran, whatever the status says."""
        plan = plan_of(step("s1"), step("s2", depends_on=["s1"]))
        state: AgentState = {
            "plan": plan,
            "tool_results": {"s1": result("s1", {"leads": []})},
            "current_step_id": "s2",
        }
        assert evaluate(state).rule is DecisionRule.READY

    def test_unresolvable_fanout_list_is_a_planning_fault(self) -> None:
        """The referenced step produced no `leads`: retrying cannot help (§4.4)."""
        state = fanout_state(tool_results={"s1": result("s1", {"items": []})})
        d = evaluate(state)
        assert d.route is DecisionRoute.PLAN
        assert d.rule is DecisionRule.UNRESOLVABLE
        assert d.current_step_id == "s2"
        assert d.plan is None, "nothing was expanded"
        assert d.expansions == ()

    def test_unresolvable_fanout_with_replans_spent_fails(self) -> None:
        state = fanout_state(
            tool_results={"s1": result("s1", {"items": []})},
            replan_count=2,
            metadata=budgets(max_replans=2),
        )
        d = evaluate(state)
        assert d.route is DecisionRoute.FAIL
        assert d.status_reason == STATUS_REASON_UNRESOLVABLE_PLAN

    def test_unbindable_fanout_alias_is_a_planning_fault(self) -> None:
        state = fanout_state(tool_results={"s1": result("s1", {"leads": [{"no_company": 1}]})})
        assert evaluate(state).rule is DecisionRule.UNRESOLVABLE

    def test_fanout_dependencies_are_checked_before_its_list_is_resolved(self) -> None:
        plan = plan_of(
            step("s0"),
            step("s1", status=StepStatus.SUCCEEDED),
            step("s2", ToolName.RESEARCH_COMPANY, depends_on=["s0"], fanout=fanout()),
        )
        state: AgentState = {
            "plan": plan,
            "tool_results": {"s1": result("s1", {"leads": leads(2)})},
            "current_step_id": "s2",
        }
        d = evaluate(state)
        assert d.rule is DecisionRule.UNRESOLVABLE
        assert d.expansions == ()

    def test_dependency_on_an_expanded_parent_waits_for_every_child(self) -> None:
        """An expanded parent is `succeeded` to mark the expansion, not the work."""
        plan = plan_of(
            step("s2", ToolName.RESEARCH_COMPANY, fanout=fanout(), status=StepStatus.SUCCEEDED),
            step(
                "s2[0]", ToolName.RESEARCH_COMPANY, parent_step_id="s2", status=StepStatus.SUCCEEDED
            ),
            step("s2[1]", ToolName.RESEARCH_COMPANY, parent_step_id="s2"),
            step("s3", ToolName.SCORE_LEAD, depends_on=["s2"]),
        )
        assert dependencies_satisfied({"plan": plan}, plan, plan.steps[3]) is False
        # Selecting s3 explicitly (an order-violating current step) hits rule 4.
        d = evaluate({"plan": plan, "current_step_id": "s3"})
        assert d.rule is DecisionRule.UNRESOLVABLE
        assert d.current_step_id == "s3"

    def test_dependency_on_an_expanded_parent_is_satisfied_once_children_succeeded(self) -> None:
        plan = plan_of(
            step("s2", ToolName.RESEARCH_COMPANY, fanout=fanout(), status=StepStatus.SUCCEEDED),
            step(
                "s2[0]", ToolName.RESEARCH_COMPANY, parent_step_id="s2", status=StepStatus.SUCCEEDED
            ),
            step("s3", ToolName.SCORE_LEAD, depends_on=["s2"]),
        )
        assert dependencies_satisfied({"plan": plan}, plan, plan.steps[2]) is True
        assert evaluate({"plan": plan}).current_step_id == "s3"

    def test_dependency_on_a_parent_expanded_to_nothing_is_vacuously_satisfied(self) -> None:
        plan = plan_of(
            step("s2", ToolName.RESEARCH_COMPANY, fanout=fanout(), status=StepStatus.SUCCEEDED),
            step("s3", ToolName.SCORE_LEAD, depends_on=["s2"]),
        )
        assert dependencies_satisfied({"plan": plan}, plan, plan.steps[1]) is True


# ---------------------------------------------------------------------------
# 5. Rule 5 — fan-out expansion
# ---------------------------------------------------------------------------
class TestRule5FanOut:
    def test_expands_then_re_evaluates_to_the_first_child(self) -> None:
        d = evaluate(fanout_state(3))

        assert d.route is DecisionRoute.EXECUTE_TOOL
        assert d.rule is DecisionRule.READY, "rule 5 is never final: it re-evaluates"
        assert d.current_step_id == "s2[0]"
        assert d.plan is not None
        assert [s.step_id for s in d.plan.steps] == ["s1", "s2", "s2[0]", "s2[1]", "s2[2]"]
        assert [s.args for s in d.plan.steps[2:]] == [
            {"company_id": "co_0"},
            {"company_id": "co_1"},
            {"company_id": "co_2"},
        ]
        parent = d.plan.step("s2")
        assert parent is not None and is_expanded(parent)
        assert len(d.expansions) == 1
        assert d.expansions[0].parent_step_id == "s2"
        assert d.expansions[0].total_items == 3
        assert d.expansions[0].truncated is False

    def test_the_delta_carries_the_expanded_plan_and_the_child(self) -> None:
        d = evaluate(fanout_state(2))
        delta = d.state_delta()
        assert set(delta) == {"current_step_id", "plan", "status_reason"}
        assert delta["current_step_id"] == "s2[0]"
        assert delta["plan"] is d.plan
        assert delta["status_reason"] is None

    def test_the_input_state_and_plan_are_not_mutated(self) -> None:
        state = fanout_state(2)
        before = {
            k: (v.model_copy(deep=True) if hasattr(v, "model_copy") else v)
            for k, v in state.items()
        }
        evaluate(state)
        assert state["plan"] == before["plan"]
        assert [s.step_id for s in state["plan"].steps] == ["s1", "s2"]

    def test_empty_list_expands_to_nothing_and_completes(self) -> None:
        d = evaluate(fanout_state(0))
        assert d.route is DecisionRoute.COMPLETE
        assert d.rule is DecisionRule.NO_RUNNABLE_STEP
        assert d.plan is not None
        assert [s.step_id for s in d.plan.steps] == ["s1", "s2"]
        parent = d.plan.step("s2")
        assert parent is not None and is_expanded(parent)
        assert d.expansions[0].total_items == 0

    def test_empty_list_then_the_next_step_runs(self) -> None:
        state = fanout_state(0, plan=fanout_plan(trailing=(step("s3", ToolName.SCORE_LEAD),)))
        d = evaluate(state)
        assert d.route is DecisionRoute.EXECUTE_TOOL
        assert d.current_step_id == "s3"

    def test_max_items_boundary_is_respected(self) -> None:
        exact = evaluate(fanout_state(4, plan=fanout_plan(max_items=4)))
        assert exact.plan is not None
        assert len([s for s in exact.plan.steps if s.parent_step_id == "s2"]) == 4
        assert exact.expansions[0].truncated is False

        over = evaluate(fanout_state(5, plan=fanout_plan(max_items=4)))
        assert over.plan is not None
        assert len([s for s in over.plan.steps if s.parent_step_id == "s2"]) == 4
        assert over.expansions[0].truncated is True
        assert over.expansions[0].total_items == 5

    def test_an_expanded_parent_is_never_expanded_again(self) -> None:
        """Re-entry: the second evaluation sees the expanded plan and simply
        picks the next child; no new children, no new expansions."""
        first = evaluate(fanout_state(2))
        assert first.plan is not None
        again = evaluate({**fanout_state(2), **first.state_delta()})  # type: ignore[typeddict-item]
        assert again.rule is DecisionRule.READY
        assert again.current_step_id == "s2[0]"
        assert again.plan is None
        assert again.expansions == ()

    def test_children_of_a_gated_tool_are_gated_individually(self) -> None:
        """Rule 5 then rule 6: the first child pauses for its own approval."""
        plan = fanout_plan(
            child_tool=ToolName.SEND_EMAIL_MOCK,
            child_args={"draft_id": "d_1", "to_email": {REF_KEY: "lead.email"}},
        )
        d = evaluate(fanout_state(2, plan=plan))
        assert d.route is DecisionRoute.REQUEST_APPROVAL
        assert d.rule is DecisionRule.APPROVAL_GATE
        assert d.current_step_id == "s2[0]"
        assert d.plan is not None, "the expansion is still written back"

    def test_a_granted_child_executes_while_the_next_child_still_needs_its_own_grant(
        self,
    ) -> None:
        plan = fanout_plan(
            child_tool=ToolName.SEND_EMAIL_MOCK,
            child_args={"draft_id": "d_1", "to_email": {REF_KEY: "lead.email"}},
        )
        child0_args = {"draft_id": "d_1", "to_email": "lead0@example.com"}
        d = evaluate(fanout_state(2, plan=plan, approval_state=approved("s2[0]", child0_args)))
        assert d.route is DecisionRoute.EXECUTE_TOOL
        assert d.current_step_id == "s2[0]"

        assert d.plan is not None
        done = d.plan.model_copy(
            update={
                "steps": [
                    s.model_copy(update={"status": StepStatus.SUCCEEDED})
                    if s.step_id == "s2[0]"
                    else s
                    for s in d.plan.steps
                ]
            }
        )
        nxt = evaluate(fanout_state(2, plan=done, approval_state=approved("s2[0]", child0_args)))
        assert nxt.route is DecisionRoute.REQUEST_APPROVAL
        assert nxt.current_step_id == "s2[1]"

    def test_two_fanouts_expand_in_one_evaluation_when_the_first_is_empty(self) -> None:
        plan = plan_of(
            step("s1", status=StepStatus.SUCCEEDED),
            step("s2", ToolName.RESEARCH_COMPANY, depends_on=["s1"], fanout=fanout("s1.output.a")),
            step("s3", ToolName.RESEARCH_COMPANY, depends_on=["s1"], fanout=fanout("s1.output.b")),
        )
        state: AgentState = {
            "plan": plan,
            "tool_results": {"s1": result("s1", {"a": [], "b": [{"company_id": "co_9"}]})},
        }
        d = evaluate(state)
        assert [e.parent_step_id for e in d.expansions] == ["s2", "s3"]
        assert d.route is DecisionRoute.EXECUTE_TOOL
        assert d.current_step_id == "s3[0]"
        assert d.plan is not None
        assert [s.step_id for s in d.plan.steps] == ["s1", "s2", "s3", "s3[0]"]

    def test_a_second_fanout_is_not_expanded_early(self) -> None:
        """Sequential execution: after `s2` expands, `s2[0]` is next, so `s3`
        waits — its list might even depend on `s2`'s results."""
        plan = plan_of(
            step("s1", status=StepStatus.SUCCEEDED),
            step("s2", ToolName.RESEARCH_COMPANY, depends_on=["s1"], fanout=fanout("s1.output.a")),
            step("s3", ToolName.RESEARCH_COMPANY, depends_on=["s1"], fanout=fanout("s1.output.b")),
        )
        state: AgentState = {
            "plan": plan,
            "tool_results": {"s1": result("s1", {"a": [{"company_id": "x"}], "b": []})},
        }
        d = evaluate(state)
        assert [e.parent_step_id for e in d.expansions] == ["s2"]
        assert d.current_step_id == "s2[0]"

    def test_expansion_children_are_subject_to_rule_1_on_re_evaluation(self) -> None:
        """The pass after expansion starts again from rule 1; a step budget
        already spent is still spent."""
        d = evaluate(fanout_state(3, step_count=25, metadata=budgets(max_steps=25)))
        assert d.rule is DecisionRule.BUDGET_EXHAUSTED
        assert d.expansions == (), "budgets are checked before any expansion"
        assert d.plan is None


# ---------------------------------------------------------------------------
# 6. Rule 6 — the approval gate
# ---------------------------------------------------------------------------
class TestRule6ApprovalGate:
    @pytest.fixture
    def gated_plan(self) -> Plan:
        return plan_of(step("s6", ToolName.SEND_EMAIL_MOCK, args=SEND_ARGS))

    def test_gated_step_without_a_decision_requests_approval(self, gated_plan: Plan) -> None:
        d = evaluate({"plan": gated_plan, "approval_state": ApprovalState()})
        assert d.route is DecisionRoute.REQUEST_APPROVAL
        assert d.rule is DecisionRule.APPROVAL_GATE
        assert d.current_step_id == "s6"
        assert d.status_reason is None
        assert d.state_delta() == {"current_step_id": "s6", "status_reason": None}

    def test_gated_step_without_any_approval_state_requests_approval(
        self, gated_plan: Plan
    ) -> None:
        assert evaluate({"plan": gated_plan}).rule is DecisionRule.APPROVAL_GATE

    def test_matching_grant_executes(self, gated_plan: Plan) -> None:
        d = evaluate({"plan": gated_plan, "approval_state": approved("s6", SEND_ARGS)})
        assert d.route is DecisionRoute.EXECUTE_TOOL
        assert d.rule is DecisionRule.READY

    def test_grant_for_different_arguments_does_not_apply(self, gated_plan: Plan) -> None:
        """§9.4: approval binds to the arguments the human saw."""
        other = {"draft_id": "d_1", "to_email": "mallory@example.com"}
        d = evaluate({"plan": gated_plan, "approval_state": approved("s6", other)})
        assert d.route is DecisionRoute.REQUEST_APPROVAL
        assert d.rule is DecisionRule.APPROVAL_GATE

    def test_grant_for_a_different_step_does_not_apply(self, gated_plan: Plan) -> None:
        d = evaluate({"plan": gated_plan, "approval_state": approved("s5", SEND_ARGS)})
        assert d.rule is DecisionRule.APPROVAL_GATE

    def test_grant_is_evaluated_against_the_resolved_arguments(self, gated_plan: Plan) -> None:
        """The hash is of the arguments *as they will now be sent*."""
        resolved = {"draft_id": "d_1", "to_email": "resolved@example.com"}

        def resolver(state: AgentState, s: PlanStep) -> dict[str, Any]:
            return resolved

        state: AgentState = {"plan": gated_plan, "approval_state": approved("s6", SEND_ARGS)}
        d = evaluate_decision(state, now=TEST_NOW, contract_for=REGISTRY.get, resolve_args=resolver)
        assert d.rule is DecisionRule.APPROVAL_GATE
        state = {"plan": gated_plan, "approval_state": approved("s6", resolved)}
        d = evaluate_decision(state, now=TEST_NOW, contract_for=REGISTRY.get, resolve_args=resolver)
        assert d.rule is DecisionRule.READY

    def test_every_approval_requiring_tool_is_gated(self) -> None:
        for name, contract in REGISTRY.items():
            if not contract.requires_approval:
                continue
            d = evaluate({"plan": plan_of(step("g", name, args={"x": 1}))})
            assert d.rule is DecisionRule.APPROVAL_GATE, name

    def test_read_only_tools_are_never_gated(self) -> None:
        """P3: approval fatigue is a safety failure."""
        for name, contract in REGISTRY.items():
            if contract.requires_approval:
                continue
            d = evaluate({"plan": plan_of(step("r", name))})
            assert d.rule is DecisionRule.READY, name

    def test_contract_lookup_is_what_decides_gating(self, gated_plan: Plan) -> None:
        """Policy questions go to the registry (§8.5); an unknown contract
        cannot be treated as gated-or-not by guesswork, so `None` means the
        gate does not apply here and barrier 2 in `execute_tool` decides."""
        d = evaluate_decision(
            {"plan": gated_plan},
            now=TEST_NOW,
            contract_for=lambda _: None,
            resolve_args=literal_args,
        )
        assert d.rule is DecisionRule.READY

    def test_a_rejected_required_gated_step_is_terminal_not_re_requested(
        self, gated_plan: Plan
    ) -> None:
        """A reject decision never loops back into a new request."""
        d = evaluate({"plan": gated_plan, "approval_state": rejected("s6")})
        assert d.route is DecisionRoute.COMPLETE


# ---------------------------------------------------------------------------
# 7. Rule 7 — ready
# ---------------------------------------------------------------------------
class TestRule7Ready:
    def test_ready_step_executes(self) -> None:
        d = evaluate({"plan": plan_of(step("s1", args={"industry": "fintech"}))})
        assert d.route is DecisionRoute.EXECUTE_TOOL
        assert d.rule is DecisionRule.READY
        assert d.current_step_id == "s1"
        assert d.plan is None
        assert d.expansions == ()

    def test_a_fresh_dispatch_clears_a_stale_advisory_reason(self) -> None:
        """`optional_step_skipped` / `replan_required` describe a past decision;
        once a new step is dispatched the run is simply running."""
        d = evaluate({"plan": plan_of(step("s1")), "status_reason": "optional_step_skipped"})
        assert d.state_delta() == {"current_step_id": "s1", "status_reason": None}


# ---------------------------------------------------------------------------
# 8. The lifecycle guard (§5.4)
# ---------------------------------------------------------------------------
class TestLifecycleGuard:
    @pytest.mark.parametrize(
        ("status", "route"),
        [
            (RunStatus.COMPLETED, DecisionRoute.COMPLETE),
            (RunStatus.REJECTED, DecisionRoute.COMPLETE),
            (RunStatus.FAILED, DecisionRoute.FAIL),
            (RunStatus.CANCELLED, DecisionRoute.FAIL),
            (RunStatus.EXPIRED, DecisionRoute.FAIL),
        ],
    )
    def test_a_terminal_run_never_re_enters_execution(
        self, status: RunStatus, route: DecisionRoute
    ) -> None:
        """Even with a runnable, ungated step and budget to spare."""
        d = evaluate({"status": status, "plan": plan_of(step("s1")), "current_step_id": "s0"})
        assert d.route is route
        assert d.rule is DecisionRule.LIFECYCLE_GUARD
        assert d.current_step_id == "s0", "nothing is re-selected"
        assert d.plan is None

    def test_guard_covers_every_terminal_status(self) -> None:
        for status in TERMINAL_RUN_STATUSES:
            assert evaluate({"status": status, "plan": plan_of(step("s1"))}).rule is (
                DecisionRule.LIFECYCLE_GUARD
            )

    def test_guard_preserves_an_existing_reason_and_names_the_status_otherwise(self) -> None:
        d = evaluate({"status": RunStatus.FAILED, "status_reason": "verification_failed"})
        assert d.status_reason == "verification_failed"
        d = evaluate({"status": RunStatus.CANCELLED})
        assert d.status_reason == "cancelled"

    def test_a_terminal_run_with_a_pending_approval_is_not_re_paused(self) -> None:
        state: AgentState = {
            "status": RunStatus.CANCELLED,
            "approval_state": ApprovalState(pending=pending_request("s6")),
        }
        assert evaluate(state).route is DecisionRoute.FAIL

    @pytest.mark.parametrize(
        "status",
        [RunStatus.CREATED, RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL],
    )
    def test_non_terminal_statuses_are_routed_normally(self, status: RunStatus) -> None:
        assert evaluate({"status": status, "plan": plan_of(step("s1"))}).rule is DecisionRule.READY

    def test_a_plain_string_status_from_a_checkpoint_is_recognised(self) -> None:
        d = evaluate({"status": "completed", "plan": plan_of(step("s1"))})  # type: ignore[typeddict-item]
        assert d.rule is DecisionRule.LIFECYCLE_GUARD


# ---------------------------------------------------------------------------
# 9. Precedence — several rules true at once
# ---------------------------------------------------------------------------
def _ladder_state() -> AgentState:
    """Every rule from 1 to 7 is *simultaneously* true (where compatible):
    budget spent, an undecided pending approval, the next step has an
    unresolvable fan-out, and its tool requires approval with no grant."""
    plan = plan_of(
        step("s1", status=StepStatus.SUCCEEDED),
        step(
            "s2",
            ToolName.SEND_EMAIL_MOCK,
            args={"draft_id": "d_1", "to_email": {REF_KEY: "lead.email"}},
            depends_on=["s1"],
            fanout=fanout(),
        ),
    )
    return {
        "status": RunStatus.RUNNING,
        "plan": plan,
        "tool_results": {"s1": result("s1", {"items": []})},  # `leads` missing: unresolvable
        "step_count": 25,
        "metadata": budgets(max_steps=25, max_replans=2),
        "replan_count": 0,
        "approval_state": ApprovalState(pending=pending_request("s9")),
    }


class TestPrecedence:
    def test_acceptance_case_budget_exhausted_and_approval_required_fails_not_pauses(
        self,
    ) -> None:
        state: AgentState = {
            "plan": plan_of(step("s6", ToolName.SEND_EMAIL_MOCK, args=SEND_ARGS)),
            "step_count": 25,
            "metadata": budgets(max_steps=25),
            "approval_state": ApprovalState(),
        }
        d = evaluate(state)
        assert d.route is DecisionRoute.FAIL
        assert d.rule is DecisionRule.BUDGET_EXHAUSTED
        assert d.status_reason == STATUS_REASON_BUDGET_EXHAUSTED

    def test_deadline_passed_and_approval_required_fails_not_pauses(self) -> None:
        state: AgentState = {
            "plan": plan_of(step("s6", ToolName.SEND_EMAIL_MOCK, args=SEND_ARGS)),
            "deadline_at": TEST_NOW - timedelta(seconds=1),
        }
        assert evaluate(state).rule is DecisionRule.BUDGET_EXHAUSTED

    def test_ladder_all_rules_true_rule_1_wins(self) -> None:
        d = evaluate(_ladder_state())
        assert d.rule is DecisionRule.BUDGET_EXHAUSTED
        assert d.route is DecisionRoute.FAIL

    def test_ladder_without_rule_1_rule_2_wins(self) -> None:
        state = _ladder_state()
        state["step_count"] = 0
        d = evaluate(state)
        assert d.rule is DecisionRule.PENDING_APPROVAL
        assert d.route is DecisionRoute.REQUEST_APPROVAL
        assert d.current_step_id == "s9"

    def test_ladder_without_rules_1_2_rule_4_wins_over_5_and_6(self) -> None:
        state = _ladder_state()
        state["step_count"] = 0
        state["approval_state"] = ApprovalState()
        d = evaluate(state)
        assert d.rule is DecisionRule.UNRESOLVABLE
        assert d.route is DecisionRoute.PLAN
        assert d.expansions == (), "no expansion before the plan is known to be resolvable"

    def test_ladder_rule_4_fails_when_replans_are_spent(self) -> None:
        state = _ladder_state()
        state["step_count"] = 0
        state["approval_state"] = ApprovalState()
        state["replan_count"] = 2
        d = evaluate(state)
        assert d.rule is DecisionRule.UNRESOLVABLE
        assert d.route is DecisionRoute.FAIL

    def test_ladder_without_rules_1_2_4_rule_5_expands_then_6_gates_the_child(self) -> None:
        state = _ladder_state()
        state["step_count"] = 0
        state["approval_state"] = ApprovalState()
        state["tool_results"] = {"s1": result("s1", {"leads": leads(2)})}
        d = evaluate(state)
        assert len(d.expansions) == 1
        assert d.rule is DecisionRule.APPROVAL_GATE
        assert d.route is DecisionRoute.REQUEST_APPROVAL
        assert d.current_step_id == "s2[0]"

    def test_ladder_without_rules_1_2_4_6_rule_7_executes_the_child(self) -> None:
        state = _ladder_state()
        state["step_count"] = 0
        state["tool_results"] = {"s1": result("s1", {"leads": leads(2)})}
        state["approval_state"] = approved(
            "s2[0]", {"draft_id": "d_1", "to_email": "lead0@example.com"}
        )
        d = evaluate(state)
        assert d.rule is DecisionRule.READY
        assert d.route is DecisionRoute.EXECUTE_TOOL
        assert d.current_step_id == "s2[0]"

    def test_rule_2_beats_rule_3(self) -> None:
        state: AgentState = {"approval_state": ApprovalState(pending=pending_request("s6"))}
        assert evaluate(state).rule is DecisionRule.PENDING_APPROVAL

    def test_rule_1_beats_rule_3(self) -> None:
        assert evaluate({"step_count": 1, "metadata": budgets(max_steps=1)}).rule is (
            DecisionRule.BUDGET_EXHAUSTED
        )

    def test_rule_1_beats_the_terminal_rejection_shortcut(self) -> None:
        state: AgentState = {
            "plan": plan_of(step("s6", ToolName.SEND_EMAIL_MOCK, status=StepStatus.REJECTED)),
            "approval_state": rejected("s6"),
            "step_count": 1,
            "metadata": budgets(max_steps=1),
        }
        assert evaluate(state).rule is DecisionRule.BUDGET_EXHAUSTED

    def test_lifecycle_guard_beats_rule_1(self) -> None:
        state = _ladder_state()
        state["status"] = RunStatus.CANCELLED
        assert evaluate(state).rule is DecisionRule.LIFECYCLE_GUARD


# ---------------------------------------------------------------------------
# 10. Legal routes and edge keys
# ---------------------------------------------------------------------------
class TestLegalRoutes:
    def test_the_router_has_exactly_five_exits_matching_the_graph_edges(self) -> None:
        assert {r.value for r in DecisionRoute} == {
            "execute_tool",
            "request_approval",
            "complete",
            "plan",
            "fail",
        }
        graph = create_agent_graph().get_graph()
        decide_targets = {e.target for e in graph.edges if e.source == "decide"}
        assert decide_targets == {r.value for r in DecisionRoute}

    def test_every_route_is_reachable(self) -> None:
        reached = {
            evaluate({"plan": plan_of(step("s1"))}).route,
            evaluate({"plan": plan_of(step("s6", ToolName.SEND_EMAIL_MOCK, args=SEND_ARGS))}).route,
            evaluate({}).route,
            evaluate({"plan": plan_of(step("s2", depends_on=["s1"]))}).route,
            evaluate({"step_count": 99}).route,
        }
        assert reached == set(DecisionRoute)

    def test_rules_are_numbered_as_in_the_architecture(self) -> None:
        assert [r.value for r in DecisionRule] == [0, 1, 2, 3, 4, 5, 6, 7]
        assert len([r for r in DecisionRule if r.value >= 1]) == 7


# ---------------------------------------------------------------------------
# 11. Determinism, purity, and node/edge agreement
# ---------------------------------------------------------------------------
REPRESENTATIVE_STATES: dict[str, Callable[[], AgentState]] = {
    "ready": lambda: {"plan": plan_of(step("s1"))},
    "gated": lambda: {"plan": plan_of(step("s6", ToolName.SEND_EMAIL_MOCK, args=SEND_ARGS))},
    "granted": lambda: {
        "plan": plan_of(step("s6", ToolName.SEND_EMAIL_MOCK, args=SEND_ARGS)),
        "approval_state": approved("s6", SEND_ARGS),
    },
    "complete": lambda: {"plan": plan_of(step("s1", status=StepStatus.SUCCEEDED))},
    "budget": lambda: {"step_count": 3, "metadata": budgets(max_steps=3)},
    "deadline": lambda: {"deadline_at": TEST_NOW - timedelta(seconds=1)},
    "pending": lambda: {"approval_state": ApprovalState(pending=pending_request("s6"))},
    "replan": lambda: {"plan": plan_of(step("s2", depends_on=["s1"]))},
    "unresolvable": lambda: {
        "plan": plan_of(step("s2", depends_on=["s1"])),
        "replan_count": 2,
        "metadata": budgets(max_replans=2),
    },
    "fanout": lambda: fanout_state(3),
    "fanout-empty": lambda: fanout_state(0),
    "fanout-gated": lambda: fanout_state(
        2,
        plan=fanout_plan(
            child_tool=ToolName.SEND_EMAIL_MOCK,
            child_args={"draft_id": "d_1", "to_email": {REF_KEY: "lead.email"}},
        ),
    ),
    "rejected": lambda: {
        "plan": plan_of(
            step("s6", ToolName.SEND_EMAIL_MOCK, status=StepStatus.REJECTED), step("s7")
        ),
        "approval_state": rejected("s6"),
    },
    "terminal": lambda: {"status": RunStatus.CANCELLED, "plan": plan_of(step("s1"))},
}


class _SpyRegistry:
    """Answers policy questions like the real registry; dispatching is a bug."""

    def __init__(self) -> None:
        self.contract_calls: list[ToolName] = []

    def contract(self, name: ToolName) -> Any:
        self.contract_calls.append(name)
        return REGISTRY[name]

    async def dispatch(self, **kwargs: Any) -> Any:
        raise AssertionError(f"decide must never dispatch a tool: {kwargs}")


class TestDeterminismAndPurity:
    @pytest.mark.parametrize("name", list(REPRESENTATIVE_STATES))
    def test_repeated_evaluation_is_identical(self, name: str) -> None:
        state = REPRESENTATIVE_STATES[name]()
        first = evaluate(state)
        for _ in range(5):
            assert evaluate(state) == first
            assert evaluate(state).state_delta() == first.state_delta()

    @pytest.mark.parametrize("name", list(REPRESENTATIVE_STATES))
    def test_evaluation_does_not_mutate_the_state(self, name: str) -> None:
        state = REPRESENTATIVE_STATES[name]()
        snapshot = {
            k: (v.model_copy(deep=True) if hasattr(v, "model_copy") else v)
            for k, v in state.items()
        }
        evaluate(state)
        assert set(state) == set(snapshot)
        for key, value in snapshot.items():
            assert state[key] == value, key  # type: ignore[literal-required]

    @pytest.mark.parametrize("name", list(REPRESENTATIVE_STATES))
    @pytest.mark.asyncio
    async def test_node_delta_and_conditional_edge_agree(self, name: str) -> None:
        """The node writes its delta; the edge re-evaluates the merged state.
        Both call the same pure function, so they must land on the same route."""
        state = REPRESENTATIVE_STATES[name]()
        handlers = NodeHandlers(clock=FixedClock(TEST_NOW))
        expected = handlers._evaluate_decision(state)
        delta = await handlers.decide(state)
        merged: AgentState = {**state, **delta}  # type: ignore[typeddict-item]
        assert handlers.route_after_decide(merged) == expected.route.value
        # And the merged state is a fixed point: deciding again re-selects the
        # same step, expands nothing further, and routes the same way.
        again = await handlers.decide(merged)
        assert again["current_step_id"] == delta["current_step_id"]
        assert "plan" not in again
        assert handlers.route_after_decide({**merged, **again}) == expected.route.value  # type: ignore[typeddict-item]

    @pytest.mark.parametrize("name", list(REPRESENTATIVE_STATES))
    @pytest.mark.asyncio
    async def test_decide_never_dispatches_a_tool(self, name: str) -> None:
        spy = _SpyRegistry()
        handlers = NodeHandlers(registry=spy, clock=FixedClock(TEST_NOW))  # type: ignore[arg-type]
        state = REPRESENTATIVE_STATES[name]()
        await handlers.decide(state)
        handlers.route_after_decide(state)

    @pytest.mark.asyncio
    async def test_decide_asks_the_registry_for_contracts(self) -> None:
        spy = _SpyRegistry()
        handlers = NodeHandlers(registry=spy, clock=FixedClock(TEST_NOW))  # type: ignore[arg-type]
        await handlers.decide(REPRESENTATIVE_STATES["gated"]())
        assert spy.contract_calls == [ToolName.SEND_EMAIL_MOCK]

    def test_decision_is_immutable(self) -> None:
        d = evaluate({"plan": plan_of(step("s1"))})
        with pytest.raises(AttributeError):
            d.route = DecisionRoute.FAIL  # type: ignore[misc]

    def test_state_delta_writes_only_the_channels_decide_owns(self) -> None:
        """§7: `current_step_id`, expanded `plan`, status — nothing else."""
        for build in REPRESENTATIVE_STATES.values():
            delta = evaluate(build()).state_delta()
            assert set(delta) <= {"current_step_id", "plan", "status_reason"}
            assert "current_step_id" in delta


# ---------------------------------------------------------------------------
# 12. Graph integration (in-memory checkpointer)
# ---------------------------------------------------------------------------
class ScriptedExecutor:
    """Stands in for `execute_tool` (AGENT-005): records every dispatch the
    router asked for, marks the step succeeded and produces a scripted output.
    Counts steps exactly as the real node does."""

    def __init__(self, outputs: dict[ToolName, Callable[[dict[str, Any]], dict[str, Any]]]) -> None:
        self._outputs = outputs
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        plan = state["plan"]
        assert plan is not None
        step_id = state["current_step_id"]
        assert step_id is not None
        current = plan.step(step_id)
        assert current is not None
        self.calls.append((step_id, dict(current.args)))
        output = self._outputs[current.tool](current.args)
        steps = [
            s.model_copy(update={"status": StepStatus.SUCCEEDED}) if s.step_id == step_id else s
            for s in plan.steps
        ]
        return {
            "tool_results": {step_id: result(step_id, output, current.tool)},
            "plan": plan.model_copy(update={"steps": steps}),
            "step_count": state.get("step_count", 0) + 1,
        }


def build_graph(
    executor: ScriptedExecutor, *, checkpointer: Any | None = None
) -> tuple[Any, NodeHandlers]:
    handlers = NodeHandlers(clock=FixedClock(TEST_NOW))
    handlers.execute_tool = executor  # type: ignore[method-assign]
    graph = create_agent_graph(checkpointer=checkpointer or MemorySaver(), node_handlers=handlers)
    return graph, handlers


def fanout_run_plan(
    child_tool: ToolName = ToolName.RESEARCH_COMPANY,
    child_args: dict[str, Any] | None = None,
    *,
    max_items: int = 10,
    optional: bool = False,
) -> Plan:
    return plan_of(
        step("s1", ToolName.SEARCH_LEADS, args={"industry": "fintech", "limit": 3}),
        step(
            "s2",
            child_tool,
            args=child_args or {"company_id": {REF_KEY: "lead.company_id"}},
            depends_on=["s1"],
            optional=optional,
            fanout=fanout(max_items=max_items),
        ),
    )


def search_then_research(n: int) -> dict[ToolName, Callable[[dict[str, Any]], dict[str, Any]]]:
    return {
        ToolName.SEARCH_LEADS: lambda args: {"leads": leads(n), "total_matched": n},
        ToolName.RESEARCH_COMPANY: lambda args: {"profile": {"company_id": args["company_id"]}},
        ToolName.SEND_EMAIL_MOCK: lambda args: {"message_id": f"m_{args['to_email']}"},
    }


class TestGraphIntegration:
    @pytest.mark.asyncio
    async def test_fanout_runs_every_child_in_order_with_bound_arguments(self) -> None:
        executor = ScriptedExecutor(search_then_research(3))
        graph, _ = build_graph(executor)
        initial = create_initial_state(
            run_id=uuid.uuid4(),
            user_request="research each lead",
            plan=fanout_run_plan(),
            clock=FixedClock(TEST_NOW),
        )

        final = await graph.ainvoke(initial, config={"configurable": {"thread_id": "t1"}})

        assert final["status"] == RunStatus.COMPLETED
        assert executor.calls == [
            ("s1", {"industry": "fintech", "limit": 3}),
            ("s2[0]", {"company_id": "co_0"}),
            ("s2[1]", {"company_id": "co_1"}),
            ("s2[2]", {"company_id": "co_2"}),
        ]
        plan = final["plan"]
        assert [s.step_id for s in plan.steps] == ["s1", "s2", "s2[0]", "s2[1]", "s2[2]"]
        assert all(s.status == StepStatus.SUCCEEDED for s in plan.steps)
        assert set(final["tool_results"]) == {"s1", "s2[0]", "s2[1]", "s2[2]"}
        assert final["step_count"] == 4, "children count as executed steps"
        assert final["current_step_id"] is None

    @pytest.mark.asyncio
    async def test_expanded_children_count_against_max_steps(self) -> None:
        """Acceptance: MAX_STEPS=3 admits `s1` and two children; the third
        child is never reached and the run fails `budget_exhausted`."""
        executor = ScriptedExecutor(search_then_research(3))
        graph, _ = build_graph(executor)
        initial = create_initial_state(
            run_id=uuid.uuid4(),
            user_request="research each lead",
            plan=fanout_run_plan(),
            metadata=budgets(max_steps=3),
            clock=FixedClock(TEST_NOW),
        )

        final = await graph.ainvoke(initial, config={"configurable": {"thread_id": "t2"}})

        assert final["status"] == RunStatus.FAILED
        assert final["status_reason"] == STATUS_REASON_BUDGET_EXHAUSTED
        assert [c[0] for c in executor.calls] == ["s1", "s2[0]", "s2[1]"]
        assert final["step_count"] == 3
        third = final["plan"].step("s2[2]")
        assert third is not None and third.status == StepStatus.PENDING

    @pytest.mark.asyncio
    async def test_empty_fanout_completes_without_executing_children(self) -> None:
        executor = ScriptedExecutor(search_then_research(0))
        graph, _ = build_graph(executor)
        initial = create_initial_state(
            run_id=uuid.uuid4(),
            user_request="research each lead",
            plan=fanout_run_plan(),
            clock=FixedClock(TEST_NOW),
        )

        final = await graph.ainvoke(initial, config={"configurable": {"thread_id": "t3"}})

        assert final["status"] == RunStatus.COMPLETED
        assert [c[0] for c in executor.calls] == ["s1"]
        assert [s.step_id for s in final["plan"].steps] == ["s1", "s2"]
        assert is_expanded(final["plan"].step("s2"))

    @pytest.mark.asyncio
    async def test_each_gated_child_pauses_separately_and_re_entry_adds_no_duplicate(
        self,
    ) -> None:
        executor = ScriptedExecutor(search_then_research(2))
        graph, _ = build_graph(executor)
        cfg = {"configurable": {"thread_id": "t4"}}
        plan = fanout_run_plan(
            ToolName.SEND_EMAIL_MOCK, {"draft_id": "d_1", "to_email": {REF_KEY: "lead.email"}}
        )
        initial = create_initial_state(
            run_id=uuid.uuid4(),
            user_request="email each lead",
            plan=plan,
            clock=FixedClock(TEST_NOW),
        )

        # First pause: child 0, with its own bound arguments.
        await graph.ainvoke(initial, config=cfg)
        snapshot = graph.get_state(cfg)
        interrupt = snapshot.tasks[0].interrupts[0].value
        assert interrupt["step_id"] == "s2[0]"
        assert interrupt["payload_preview"] == {"draft_id": "d_1", "to_email": "lead0@example.com"}
        assert [s.step_id for s in snapshot.values["plan"].steps] == ["s1", "s2", "s2[0]", "s2[1]"]
        assert [c[0] for c in executor.calls] == ["s1"], "nothing sent before approval"

        # Second pause: child 1 — the grant for child 0 does not cover it.
        await graph.ainvoke(Command(resume="approve"), config=cfg)
        snapshot = graph.get_state(cfg)
        interrupt = snapshot.tasks[0].interrupts[0].value
        assert interrupt["step_id"] == "s2[1]"
        assert interrupt["payload_preview"]["to_email"] == "lead1@example.com"
        assert [c[0] for c in executor.calls] == ["s1", "s2[0]"]
        assert [s.step_id for s in snapshot.values["plan"].steps] == ["s1", "s2", "s2[0]", "s2[1]"]

        final = await graph.ainvoke(Command(resume="approve"), config=cfg)

        assert final["status"] == RunStatus.COMPLETED
        assert [c[0] for c in executor.calls] == ["s1", "s2[0]", "s2[1]"]
        ids = [s.step_id for s in final["plan"].steps]
        assert ids == ["s1", "s2", "s2[0]", "s2[1]"]
        assert len(ids) == len(set(ids)), "re-entry through request_approval added no duplicate"
        assert set(final["approval_state"].decisions) == {"s2[0]", "s2[1]"}

    @pytest.mark.asyncio
    async def test_rejecting_a_required_child_ends_the_run_rejected_without_running_the_rest(
        self,
    ) -> None:
        executor = ScriptedExecutor(search_then_research(2))
        graph, _ = build_graph(executor)
        cfg = {"configurable": {"thread_id": "t5"}}
        plan = fanout_run_plan(
            ToolName.SEND_EMAIL_MOCK, {"draft_id": "d_1", "to_email": {REF_KEY: "lead.email"}}
        )
        initial = create_initial_state(
            run_id=uuid.uuid4(),
            user_request="email each lead",
            plan=plan,
            clock=FixedClock(TEST_NOW),
        )

        await graph.ainvoke(initial, config=cfg)
        final = await graph.ainvoke(Command(resume="reject"), config=cfg)

        assert final["status"] == RunStatus.REJECTED
        assert final["status_reason"] == "approval_rejected"
        assert [c[0] for c in executor.calls] == ["s1"], "neither child executed"
        second = final["plan"].step("s2[1]")
        assert second is not None and second.status == StepStatus.PENDING

    @pytest.mark.asyncio
    async def test_rejecting_an_optional_child_skips_it_and_continues(self) -> None:
        executor = ScriptedExecutor(search_then_research(2))
        graph, _ = build_graph(executor)
        cfg = {"configurable": {"thread_id": "t6"}}
        plan = fanout_run_plan(
            ToolName.SEND_EMAIL_MOCK,
            {"draft_id": "d_1", "to_email": {REF_KEY: "lead.email"}},
            optional=True,
        )
        initial = create_initial_state(
            run_id=uuid.uuid4(),
            user_request="email each lead",
            plan=plan,
            clock=FixedClock(TEST_NOW),
        )

        await graph.ainvoke(initial, config=cfg)
        await graph.ainvoke(Command(resume="reject"), config=cfg)
        final = await graph.ainvoke(Command(resume="approve"), config=cfg)

        assert final["status"] == RunStatus.COMPLETED
        assert final["final_response"].partial is True
        assert [c[0] for c in executor.calls] == ["s1", "s2[1]"]
        first = final["plan"].step("s2[0]")
        assert first is not None and first.status == StepStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_unresolvable_fanout_replans_a_bounded_number_of_times_then_fails(self) -> None:
        """The search produced no `leads` key: every replan (the placeholder
        planner keeps the plan) hits rule 4 again until MAX_REPLANS is spent."""
        outputs = search_then_research(0)
        outputs[ToolName.SEARCH_LEADS] = lambda args: {"items": []}
        executor = ScriptedExecutor(outputs)
        graph, _ = build_graph(executor)
        initial = create_initial_state(
            run_id=uuid.uuid4(),
            user_request="research each lead",
            plan=fanout_run_plan(),
            metadata=budgets(max_replans=2),
            clock=FixedClock(TEST_NOW),
        )

        final = await graph.ainvoke(initial, config={"configurable": {"thread_id": "t7"}})

        assert final["status"] == RunStatus.FAILED
        assert final["status_reason"] == STATUS_REASON_UNRESOLVABLE_PLAN
        assert final["replan_count"] == 2
        assert len(final["plan_history"]) == 2, "each revision is kept"
        assert [c[0] for c in executor.calls] == ["s1"], "the fan-out never executed"
        assert [s.step_id for s in final["plan"].steps] == ["s1", "s2"]

    @pytest.mark.asyncio
    async def test_the_same_run_twice_is_identical(self) -> None:
        """Determinism across invocations: same plan, same scripted world,
        same trace of dispatches and same final plan."""
        runs = []
        for thread in ("d1", "d2"):
            executor = ScriptedExecutor(search_then_research(3))
            graph, _ = build_graph(executor)
            initial = create_initial_state(
                run_id="00000000-0000-0000-0000-000000000001",
                user_request="research each lead",
                plan=fanout_run_plan(),
                clock=FixedClock(TEST_NOW),
            )
            final = await graph.ainvoke(initial, config={"configurable": {"thread_id": thread}})
            runs.append((executor.calls, final["plan"], final["status"], final["step_count"]))
        assert runs[0] == runs[1]


# ---------------------------------------------------------------------------
# 13. Graph integration on the real Postgres saver with the real registry
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestRealPostgresFanOut:
    @pytest.fixture(autouse=True)
    def _require_db(self) -> None:
        require_database()
        migrate_to_head()

    @pytest.mark.asyncio
    async def test_fanout_children_dispatch_through_the_real_registry(self) -> None:
        """`search_leads` → fan out `get_lead` over `s1.output.leads`: the
        children are real dispatches, bound to real lead ids, checkpointed
        durably, and the expanded plan survives the round trip."""
        from app.integrations.mock import build_mock_adapters
        from app.integrations.mock.seed import seed_database
        from app.persistence.session import create_session_factory
        from app.runtime import SequentialIdGenerator

        engine = await make_engine()
        uow_factory = uow_factory_for(engine)
        settings = get_settings()
        try:
            async with open_checkpointer(settings) as checkpointer:
                run_id = uuid.uuid4()
                async with uow_factory() as uow:
                    await uow.agent_runs.create(
                        id=run_id,
                        user_request="Find 2 technology leads and look up each of them",
                        status=RunStatus.RUNNING,
                        deadline_at=TEST_NOW + timedelta(minutes=5),
                    )
                    await uow.commit()

                session_factory = create_session_factory(engine)
                await seed_database(session_factory, reset=False)
                clock = FixedClock(TEST_NOW)
                adapters = build_mock_adapters(session_factory, clock, SequentialIdGenerator())
                registry = ToolRegistry(adapters=adapters, uow_factory=uow_factory, clock=clock)
                handlers = NodeHandlers(registry=registry, uow_factory=uow_factory, clock=clock)
                graph = create_agent_graph(checkpointer=checkpointer, node_handlers=handlers)

                plan = plan_of(
                    step("s1", ToolName.SEARCH_LEADS, args={"industry": "technology", "limit": 2}),
                    step(
                        "s2",
                        ToolName.GET_LEAD,
                        args={"lead_id": {REF_KEY: "lead.lead_id"}},
                        depends_on=["s1"],
                        fanout=fanout(max_items=2),
                    ),
                    plan_id=f"p_{run_id.hex[:8]}",
                )
                initial = create_initial_state(
                    run_id=run_id,
                    user_request="Find 2 technology leads and look up each of them",
                    plan=plan,
                    clock=FixedClock(TEST_NOW),
                )
                cfg = thread_config(run_id)

                final = await graph.ainvoke(initial, config=cfg, durability=DURABILITY)

                assert final["status"] == RunStatus.COMPLETED
                found = final["tool_results"]["s1"].output["leads"]
                assert 1 <= len(found) <= 2
                child_ids = [f"s2[{i}]" for i in range(len(found))]
                assert [s.step_id for s in final["plan"].steps] == ["s1", "s2", *child_ids]
                for i, child_id in enumerate(child_ids):
                    child = final["plan"].step(child_id)
                    assert child is not None
                    assert child.args == {"lead_id": found[i]["lead_id"]}
                    assert child.status == StepStatus.SUCCEEDED
                    assert (
                        final["tool_results"][child_id].output["lead"]["lead_id"]
                        == (found[i]["lead_id"])
                    )
                assert final["step_count"] == 1 + len(found)

                snapshot = await graph.aget_state(cfg)
                assert [s.step_id for s in snapshot.values["plan"].steps] == [
                    "s1",
                    "s2",
                    *child_ids,
                ]
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 14. Structural safety
# ---------------------------------------------------------------------------
ROUTER_MODULES = ("agent/decide.py", "agent/fanout.py")
#: What the router may import from the application: the state it routes over,
#: the error taxonomy, and the contract types it asks policy questions of.
ALLOWED_APP_IMPORTS = ("app.agent.state", "app.agent.fanout", "app.errors", "app.tools.contracts")
IO_CAPABLE_MODULES = frozenset(
    {
        "anthropic",
        "asyncpg",
        "httpx",
        "langgraph",
        "langgraph_checkpoint",
        "psycopg",
        "requests",
        "socket",
        "sqlalchemy",
        "structlog",
        "urllib",
        "os",
        "sys",
        "subprocess",
        "threading",
        "asyncio",
        "time",
        "random",
    }
)


def _tree(rel: str) -> ast.Module:
    path = APP / rel
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestStructuralSafety:
    @pytest.mark.parametrize("rel", ROUTER_MODULES)
    def test_router_modules_import_nothing_that_can_do_io(self, rel: str) -> None:
        imports = _imports(_tree(rel))
        assert not {i.split(".")[0] for i in imports} & IO_CAPABLE_MODULES, imports
        app_imports = {i for i in imports if i.startswith("app.")}
        assert all(i in ALLOWED_APP_IMPORTS for i in app_imports), app_imports
        assert not any(
            i.startswith(("app.persistence", "app.integrations", "app.tools.impl")) for i in imports
        )
        assert "app.tools.registry" not in imports, "policy questions go through an injected lookup"

    @pytest.mark.parametrize("rel", ROUTER_MODULES)
    def test_router_modules_are_synchronous_and_call_no_tool(self, rel: str) -> None:
        tree = _tree(rel)
        assert not [n for n in ast.walk(tree) if isinstance(n, (ast.AsyncFunctionDef, ast.Await))]
        calls = [
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        ]
        assert "dispatch" not in calls
        assert "send" not in calls and "update" not in calls and "save" not in calls
        names = [
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        assert "open" not in names and "print" not in names

    def test_the_rules_are_encoded_in_exactly_one_ascending_order(self) -> None:
        """`evaluate_decision` constructs decisions citing `DecisionRule.X`;
        in source order those citations must be strictly ascending, so the
        rule order is visible in the code and cannot be silently reordered."""
        tree = _tree("agent/decide.py")
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "evaluate_decision"
        )
        citations = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "DecisionRule"
        ]
        # `ast.walk` is breadth-first; source order is what a reader sees.
        cited = [n.attr for n in sorted(citations, key=lambda n: (n.lineno, n.col_offset))]
        first_seen: list[DecisionRule] = []
        for name in cited:
            rule = DecisionRule[name]
            if rule not in first_seen:
                first_seen.append(rule)
        assert first_seen == [
            DecisionRule.LIFECYCLE_GUARD,
            DecisionRule.BUDGET_EXHAUSTED,
            DecisionRule.PENDING_APPROVAL,
            DecisionRule.NO_RUNNABLE_STEP,
            DecisionRule.UNRESOLVABLE,
            DecisionRule.APPROVAL_GATE,
            DecisionRule.READY,
        ]
        assert [r.value for r in first_seen] == sorted(r.value for r in first_seen)

    def test_the_router_is_the_only_decide_logic_in_the_node_layer(self) -> None:
        """`nodes.py` delegates; it no longer holds a second copy of the rules."""
        tree = _tree("agent/nodes.py")
        handlers = next(
            n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "NodeHandlers"
        )
        method_names = {
            n.name for n in handlers.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert {"decide", "route_after_decide", "_evaluate_decision"} <= method_names
        assert not {"_find_runnable_step", "_dependencies_satisfied"} & method_names
        for name in ("decide", "route_after_decide"):
            fn = next(
                n
                for n in handlers.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
            )
            assert not [n for n in ast.walk(fn) if isinstance(n, (ast.If, ast.For, ast.While))], (
                f"{name} must delegate, not decide"
            )

    def test_no_later_task_implementation_leaked_in(self) -> None:
        """AGENT-006..009, HITL, API and an LLM are out of scope for AGENT-005."""
        agent_files = {p.name for p in (APP / "agent").glob("*.py")}
        assert agent_files == {
            "__init__.py",
            "decide.py",
            "fanout.py",
            "graph.py",
            "nodes.py",
            "normalizer.py",
            "resolver.py",
            "state.py",
        }
        for path in (APP / "agent").glob("*.py"):
            imports = _imports(ast.parse(path.read_text(encoding="utf-8")))
            assert "anthropic" not in {i.split(".")[0] for i in imports}, path.name
        nodes_src = (APP / "agent" / "nodes.py").read_text(encoding="utf-8")
        assert "LLMPlanner" not in nodes_src and "Responder" not in nodes_src

    def test_the_only_new_exception_is_a_reference_resolution_fault(self) -> None:
        assert FanOutResolutionError.error_class.value == "reference_resolution"
