"""Replanning glue: what the `plan` node derives from state before and after
it asks a planner to revise (§7 `plan`, §10.2, §10.3).

Pure functions over `AgentState`. They decide nothing about *whether* a
replan is allowed — `decide` (rule 4) and `recover` own that, and both
announce their decision in `status_reason` — only what the planner is told and
which finished work the revised plan may keep.
"""

from __future__ import annotations

from typing import Final

from app.agent.decide import STATUS_REASON_REPLAN_REQUIRED
from app.agent.fanout import children_of
from app.agent.planner.protocol import PlanRevisionContext
from app.agent.state import AgentError, AgentState, Plan, PlanStep, StepStatus
from app.errors import ErrorClass

__all__ = [
    "REVISION_REASONS",
    "STATUS_REASON_REPLANNABLE_FAULT",
    "build_revision_context",
    "carry_over_settled_steps",
    "revision_requested",
]

#: What `recover` writes when it routes a replannable fault back to `plan`.
STATUS_REASON_REPLANNABLE_FAULT: Final = "replannable_fault"

#: The two ways the graph re-enters `plan` with a plan already in state. Any
#: other entry with a plan present means the plan was supplied at run
#: creation (`create_initial_state(plan=...)`) and is validated, not replaced.
REVISION_REASONS: Final[frozenset[str]] = frozenset(
    {STATUS_REASON_REPLAN_REQUIRED, STATUS_REASON_REPLANNABLE_FAULT}
)


def revision_requested(state: AgentState) -> bool:
    return state.get("plan") is not None and state.get("status_reason") in REVISION_REASONS


def _transitive_dependencies(plan: Plan, step_id: str) -> set[str]:
    seen: set[str] = set()
    frontier = [step_id]
    while frontier:
        step = plan.step(frontier.pop())
        if step is None:
            continue
        for dep in step.depends_on:
            if dep not in seen:
                seen.add(dep)
                frontier.append(dep)
    return seen


def build_revision_context(state: AgentState, previous: Plan) -> PlanRevisionContext:
    """Everything a planner may know about why it is revising.

    Settled steps are those with a result in the artifact store, minus the
    step that faulted and — for a `stale_write` — everything it read through
    (§10.3: the record moved, so the re-read must actually happen, which
    produces new arguments and therefore a fresh approval).
    """
    failed_step_id = state.get("current_step_id")
    errors: list[AgentError] = list(state.get("errors") or [])
    relevant = [e for e in errors if failed_step_id is not None and e.step_id == failed_step_id]
    if not relevant and errors:
        relevant = [errors[-1]]
    relevant = relevant[-3:]

    invalidated: set[str] = set()
    if failed_step_id is not None:
        invalidated.add(failed_step_id)
        if any(e.error_class is ErrorClass.STALE_WRITE for e in relevant):
            invalidated |= _transitive_dependencies(previous, failed_step_id)

    results = state.get("tool_results") or {}
    settled = [
        s.step_id
        for s in previous.steps
        if s.step_id in results
        and s.status == StepStatus.SUCCEEDED
        and s.step_id not in invalidated
    ]
    return PlanRevisionContext(
        previous=previous,
        replan_count=state.get("replan_count", 0),
        reason=state.get("status_reason"),
        failed_step_id=failed_step_id,
        errors=relevant,
        settled_step_ids=settled,
    )


def _expanded(step: PlanStep) -> bool:
    return step.fanout is not None and step.status == StepStatus.SUCCEEDED


def carry_over_settled_steps(plan: Plan, prior: PlanRevisionContext) -> Plan:
    """Mark a revised plan's steps `succeeded` where the previous plan already
    produced their result — same id, same tool, same arguments — so a
    revision never re-executes finished work and never re-asks for an
    approval a human already gave. Anything that differs runs again.

    A fan-out parent that was already expanded is carried over the same way
    when the revision keeps it and every child it produced (§4.5): its
    `succeeded` records the expansion, not work, and re-expanding would only
    reproduce the children the plan already lists.
    """
    previous = {s.step_id: s for s in prior.previous.steps}
    settled = set(prior.settled_step_ids)
    new_ids = {s.step_id for s in plan.steps}
    steps = []
    for step in plan.steps:
        before = previous.get(step.step_id)
        if before is None or step.status != StepStatus.PENDING:
            steps.append(step)
            continue
        unchanged = before.tool == step.tool and before.args == step.args
        result_kept = step.step_id in settled
        expansion_kept = (
            _expanded(before)
            and step.fanout == before.fanout
            and all(c.step_id in new_ids for c in children_of(prior.previous, step.step_id))
        )
        if unchanged and (result_kept or expansion_kept):
            steps.append(step.model_copy(update={"status": StepStatus.SUCCEEDED}))
        else:
            steps.append(step)
    return plan.model_copy(update={"steps": steps})
