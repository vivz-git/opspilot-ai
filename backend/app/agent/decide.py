"""The `decide` router: seven ordered rules plus fan-out expansion (§6.2, ADR-006).

`decide` is the only router in the graph, and **the order is the safety
property** — the gate is checked before anything can execute, and budgets are
checked before the gate, so a step that is both budget-exhausted and
approval-requiring fails rather than pauses.

The whole router is one pure function, `evaluate_decision`, over the state
plus three injected read-only collaborators (the clock reading, the contract
lookup and the argument resolver). It performs no I/O, holds no state and
calls no tool: the same inputs always produce the same `Decision`. The node
handler and the conditional edge both call it, so the node's delta and the
edge's route cannot disagree.

The seven rules, in the only order they are ever evaluated:

    1. deadline_at passed, or step_count ≥ MAX_STEPS   → fail(budget_exhausted)
    2. a pending approval exists and is undecided      → request_approval
    3. no runnable step remains                        → complete
    4. next step's dependencies unsatisfied/unresolvable
           and replan_count < MAX_REPLANS              → plan
           else                                        → fail(unresolvable_plan)
    5. next step's fanout is unexpanded                → expand, re-evaluate from 1
    6. contract.requires_approval(step) AND NOT approval_state.grants(step)
                                                       → request_approval
    7. otherwise                                       → execute_tool

Ahead of rule 1 sits a lifecycle guard that is not one of the seven: a run
already in a terminal status (§5.4) has no outgoing transition, so it can only
be routed to the terminal node matching its status — never back into
execution or a pause.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, IntEnum, StrEnum
from typing import Any, Final

from app.agent.fanout import (
    FanOutExpansion,
    FanOutResolutionError,
    apply_expansion,
    children_of,
    is_expanded,
    is_unexpanded_fanout,
    plan_fanout_expansion,
)
from app.agent.state import (
    TERMINAL_RUN_STATUSES,
    AgentState,
    ApprovalState,
    Plan,
    PlanStep,
    RunMetadata,
    RunStatus,
    StepStatus,
    ToolResult,
)
from app.tools.contracts import ToolContract, ToolName

__all__ = [
    "Decision",
    "DecisionRoute",
    "DecisionRule",
    "STATUS_REASON_BUDGET_EXHAUSTED",
    "STATUS_REASON_REPLAN_REQUIRED",
    "STATUS_REASON_UNRESOLVABLE_PLAN",
    "UNCHANGED",
    "dependencies_satisfied",
    "evaluate_decision",
    "next_runnable_step",
]

ContractLookup = Callable[[ToolName], ToolContract | None]
ArgResolver = Callable[[AgentState, PlanStep], dict[str, Any]]

STATUS_REASON_BUDGET_EXHAUSTED: Final = "budget_exhausted"
STATUS_REASON_UNRESOLVABLE_PLAN: Final = "unresolvable_plan"
STATUS_REASON_REPLAN_REQUIRED: Final = "replan_required"

#: Step statuses from which the *current* step is still the next step.
_ACTIVE_STEP_STATUSES: Final[frozenset[StepStatus]] = frozenset(
    {StepStatus.PENDING, StepStatus.READY, StepStatus.AWAITING_APPROVAL, StepStatus.RUNNING}
)
#: Step statuses from which any step may be selected as the next step.
_SELECTABLE_STEP_STATUSES: Final[frozenset[StepStatus]] = frozenset(
    {StepStatus.PENDING, StepStatus.READY}
)


class _Unchanged(Enum):
    KEEP = "keep"


#: A `Decision.status_reason` of `UNCHANGED` leaves the channel as it is;
#: `None` clears it. The distinction matters: a fresh dispatch (rules 6 and 7)
#: clears a stale advisory reason, while a re-pause (rule 2) must not.
UNCHANGED: Final = _Unchanged.KEEP


class DecisionRoute(StrEnum):
    """The five legal exits of `decide` (§6.1). Values are the edge keys."""

    EXECUTE_TOOL = "execute_tool"
    REQUEST_APPROVAL = "request_approval"
    COMPLETE = "complete"
    PLAN = "plan"
    FAIL = "fail"


class DecisionRule(IntEnum):
    """Which rule produced a decision. Numbering is §6.2's; ordering is the
    evaluation order, which the structural tests assert."""

    LIFECYCLE_GUARD = 0  # §5.4 — not one of the seven; a terminal run stays terminal
    BUDGET_EXHAUSTED = 1
    PENDING_APPROVAL = 2
    NO_RUNNABLE_STEP = 3
    UNRESOLVABLE = 4
    FANOUT_EXPANSION = 5  # never a final rule: it expands and re-evaluates
    APPROVAL_GATE = 6
    READY = 7


@dataclass(frozen=True)
class Decision:
    """A complete routing decision, and the state delta it implies."""

    route: DecisionRoute
    rule: DecisionRule
    current_step_id: str | None
    #: The plan after the fan-out expansions this evaluation performed, or
    #: `None` when the plan is unchanged.
    plan: Plan | None
    status_reason: str | None | _Unchanged
    expansions: tuple[FanOutExpansion, ...] = ()

    def state_delta(self) -> dict[str, Any]:
        """The partial state the `decide` node returns (§7: `current_step_id`,
        expanded `plan`, status)."""
        delta: dict[str, Any] = {"current_step_id": self.current_step_id}
        if self.plan is not None:
            delta["plan"] = self.plan
        if self.status_reason is not UNCHANGED:
            delta["status_reason"] = self.status_reason
        return delta


# ---------------------------------------------------------------------------
# Step selection and dependency checks
# ---------------------------------------------------------------------------
def _required_step_rejected(plan: Plan, approval_state: ApprovalState) -> bool:
    """A human declined a required step: the run ends as `rejected` and
    nothing after that step may run (§9.6)."""
    return any(
        not s.optional and (s.status == StepStatus.REJECTED or approval_state.rejected(s.step_id))
        for s in plan.steps
    )


def next_runnable_step(state: AgentState, plan: Plan | None) -> PlanStep | None:
    """The step `decide` is deciding about, or `None` when nothing remains.

    The current step keeps priority while it is still active (a resumed run
    must not skip ahead); otherwise the first pending step in plan order is
    next. Sequential execution (§4.6) is what makes "first in plan order"
    sufficient. A step a human has rejected is never runnable.
    """
    if plan is None or not plan.steps:
        return None
    approval_state = state.get("approval_state") or ApprovalState()
    if _required_step_rejected(plan, approval_state):
        return None
    current_id = state.get("current_step_id")
    if current_id is not None:
        current = plan.step(current_id)
        if (
            current is not None
            and current.status in _ACTIVE_STEP_STATUSES
            and not approval_state.rejected(current.step_id)
        ):
            return current
    for step in plan.steps:
        if step.status in _SELECTABLE_STEP_STATUSES and not approval_state.rejected(step.step_id):
            return step
    return None


def _step_settled(step: PlanStep, tool_results: Mapping[str, ToolResult]) -> bool:
    return step.status == StepStatus.SUCCEEDED or step.step_id in tool_results


def dependencies_satisfied(state: AgentState, plan: Plan, step: PlanStep) -> bool:
    """Every dependency has succeeded. A dependency on an expanded fan-out
    parent is satisfied only once every child has succeeded — the parent's
    own `succeeded` marks the expansion, not the work."""
    tool_results = state.get("tool_results") or {}
    for dep_id in step.depends_on:
        dep = plan.step(dep_id)
        if dep is None:
            return False
        if is_expanded(dep):
            if not all(_step_settled(c, tool_results) for c in children_of(plan, dep_id)):
                return False
            continue
        if not _step_settled(dep, tool_results):
            return False
    return True


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------
def _budget_exhausted(state: AgentState, now: datetime, max_steps: int) -> bool:
    deadline = state.get("deadline_at")
    if deadline is not None and now > deadline:
        return True
    return state.get("step_count", 0) >= max_steps


def _undecided_pending_step(approval_state: ApprovalState) -> str | None:
    pending = approval_state.pending
    if pending is not None and pending.step_id not in approval_state.decisions:
        return pending.step_id
    return None


@dataclass(frozen=True)
class _StepReadiness:
    """Rule 4's answer about the next step: either a fault, or — for an
    unexpanded fan-out — the expansion rule 5 will apply, or neither."""

    fault: str | None = None
    expansion: FanOutExpansion | None = None


def _assess_step(state: AgentState, plan: Plan, step: PlanStep) -> _StepReadiness:
    if not dependencies_satisfied(state, plan, step):
        return _StepReadiness(
            fault=f"step {step.step_id!r} has unsatisfied dependencies {step.depends_on!r}"
        )
    if is_unexpanded_fanout(step):
        try:
            expansion = plan_fanout_expansion(step, state.get("tool_results") or {})
        except FanOutResolutionError as exc:
            return _StepReadiness(fault=f"step {step.step_id!r}: {exc}")
        return _StepReadiness(expansion=expansion)
    return _StepReadiness()


def evaluate_decision(
    state: AgentState,
    *,
    now: datetime,
    contract_for: ContractLookup,
    resolve_args: ArgResolver,
) -> Decision:
    """Apply §6.2 to `state` and return the resulting `Decision`.

    Pure: `now` is the injected clock reading, `contract_for` answers policy
    questions from the registry, `resolve_args` produces the arguments a step
    would be sent (rule 6 grants against *those*). Rule 5 mutates only a local
    plan copy and loops back to rule 1; each iteration expands a distinct
    parent, so the loop is bounded by the number of fan-out steps.
    """
    metadata = state.get("metadata") or RunMetadata()
    budgets = metadata.budgets
    approval_state = state.get("approval_state") or ApprovalState()
    plan = state.get("plan")
    expansions: list[FanOutExpansion] = []
    expanded_plan: Plan | None = None

    # Lifecycle guard (§5.4): a terminal run has no outgoing transition.
    status = state.get("status")
    status_reason = state.get("status_reason")
    if (status is not None and status in TERMINAL_RUN_STATUSES) or status_reason in (
        "cancelled",
        "operator_cancelled",
    ):
        return Decision(
            route=(
                DecisionRoute.COMPLETE
                if status in (RunStatus.COMPLETED, RunStatus.REJECTED)
                else DecisionRoute.FAIL
            ),
            rule=DecisionRule.LIFECYCLE_GUARD,
            current_step_id=state.get("current_step_id"),
            plan=None,
            status_reason=status_reason or str(status),
        )

    # Bounded re-evaluation: one pass per fan-out step, plus the final pass.
    max_passes = 1 + (sum(1 for s in plan.steps if s.fanout is not None) if plan else 0)
    for _ in range(max_passes):
        # Rule 1 — budgets first, on every pass, so no cycle can iterate for free.
        if _budget_exhausted(state, now, budgets.max_steps):
            return Decision(
                route=DecisionRoute.FAIL,
                rule=DecisionRule.BUDGET_EXHAUSTED,
                current_step_id=state.get("current_step_id"),
                plan=expanded_plan,
                status_reason=STATUS_REASON_BUDGET_EXHAUSTED,
                expansions=tuple(expansions),
            )

        # Rule 2 — an undecided pause is re-entered, never skipped past.
        pending_step_id = _undecided_pending_step(approval_state)
        if pending_step_id is not None:
            return Decision(
                route=DecisionRoute.REQUEST_APPROVAL,
                rule=DecisionRule.PENDING_APPROVAL,
                current_step_id=pending_step_id,
                plan=expanded_plan,
                status_reason=UNCHANGED,
                expansions=tuple(expansions),
            )

        # Rule 3 — nothing left to run.
        step = next_runnable_step(state, plan)
        if plan is None or step is None:
            return Decision(
                route=DecisionRoute.COMPLETE,
                rule=DecisionRule.NO_RUNNABLE_STEP,
                current_step_id=None,
                plan=expanded_plan,
                status_reason=UNCHANGED,
                expansions=tuple(expansions),
            )

        # Rule 4 — a step that cannot proceed is a planning fault.
        readiness = _assess_step(state, plan, step)
        if readiness.fault is not None:
            if state.get("replan_count", 0) < budgets.max_replans:
                return Decision(
                    route=DecisionRoute.PLAN,
                    rule=DecisionRule.UNRESOLVABLE,
                    current_step_id=step.step_id,
                    plan=expanded_plan,
                    status_reason=STATUS_REASON_REPLAN_REQUIRED,
                    expansions=tuple(expansions),
                )
            return Decision(
                route=DecisionRoute.FAIL,
                rule=DecisionRule.UNRESOLVABLE,
                current_step_id=step.step_id,
                plan=expanded_plan,
                status_reason=STATUS_REASON_UNRESOLVABLE_PLAN,
                expansions=tuple(expansions),
            )

        # Rule 5 — expand, then re-evaluate from rule 1 against the expanded plan.
        if readiness.expansion is not None:
            plan = apply_expansion(plan, readiness.expansion)
            expanded_plan = plan
            expansions.append(readiness.expansion)
            continue

        # Rule 6 — the gate, against the arguments as they will now be sent.
        contract = contract_for(step.tool)
        if (
            contract is not None
            and contract.requires_approval
            and not approval_state.grants(step.step_id, resolve_args(state, step))
        ):
            return Decision(
                route=DecisionRoute.REQUEST_APPROVAL,
                rule=DecisionRule.APPROVAL_GATE,
                current_step_id=step.step_id,
                plan=expanded_plan,
                status_reason=None,
                expansions=tuple(expansions),
            )

        # Rule 7 — ready.
        return Decision(
            route=DecisionRoute.EXECUTE_TOOL,
            rule=DecisionRule.READY,
            current_step_id=step.step_id,
            plan=expanded_plan,
            status_reason=None,
            expansions=tuple(expansions),
        )

    # Unreachable by construction: every pass either returns or expands a
    # distinct parent, and the pass budget is one more than the parent count.
    raise RuntimeError("decide exceeded its bounded re-evaluation passes")
