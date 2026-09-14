"""The `Planner` protocol and what a planner is handed (§4.2, §7, ADR-002).

A planner turns a `NormalizedTask` into a `Plan`: a DAG of `PlanStep`s naming
registered tools, literal arguments and `$ref` bindings (§4.3, §4.4). It is
responsible for *planning only*: it never executes a tool, never grants an
approval and never sees a tool's raw output. Two implementations exist behind
this one protocol — the deterministic `RulePlanner` and the model-backed
`LLMPlanner` — and the `plan` node cannot tell them apart.

`normalize` is not part of this protocol: AGENT-003 landed understanding as
its own `TaskNormalizer` protocol (`app.agent.normalizer`), so the `plan`
signature here is the one §4.2 specifies for planning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import Field

from app.agent.state import AgentError, Budgets, Model, NormalizedTask, Plan, PlannerKind

__all__ = [
    "PlanRevisionContext",
    "Planner",
    "PlannerIdentity",
]


class PlanRevisionContext(Model):
    """What a planner is told when it is asked to *revise* a plan (§7 `plan`:
    "reads `plan` + `errors` when revising").

    Deliberately narrow: the previous plan, the classified errors and the ids
    of steps whose results already exist. No tool output travels here — a
    planner reasons over the structure of what happened, never over untrusted
    third-party text (§16.3).
    """

    previous: Plan
    #: Revisions made before this one; the new plan is revision `replan_count + 1`.
    replan_count: int = Field(default=0, ge=0)
    #: The `status_reason` that routed the run back to `plan`.
    reason: str | None = None
    #: The step the run was on when the fault occurred, if any.
    failed_step_id: str | None = None
    errors: list[AgentError] = Field(default_factory=list)
    #: Steps whose `tool_results` entry is still trustworthy. A revision that
    #: re-emits one of these unchanged (same id, tool and arguments) does not
    #: re-execute it (`carry_over_settled_steps`).
    settled_step_ids: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class PlannerIdentity:
    """Recorded on the run for reproducibility (§5.2 `metadata`, ADR-002)."""

    kind: PlannerKind
    model_id: str | None = None
    prompt_version: str | None = None


class Planner(Protocol):
    """One protocol, two implementations (ADR-002)."""

    @property
    def identity(self) -> PlannerIdentity: ...

    async def plan(
        self,
        task: NormalizedTask,
        prior: PlanRevisionContext | None = None,
        *,
        budgets: Budgets | None = None,
    ) -> Plan:
        """Produce a plan for `task`, or revise `prior.previous` when given.

        The returned plan must already satisfy `validate_plan`; a planner that
        cannot produce one raises `PlannerError` (or its subclass
        `PlanValidationError`). `budgets` bounds the plan's size.
        """
        ...
