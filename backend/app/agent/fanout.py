"""Fan-out expansion: one planned step becomes N executed steps (§4.5, ADR-006).

`decide` expands a `fanout` step **at execution time**, once the list it
iterates over exists in the artifact store, into concrete children `s2[0]`,
`s2[1]`, … Each child is an ordinary step: separately traced, separately
retried, separately (if applicable) approved.

Everything here is a pure function over immutable inputs. Expansion never
touches a step other than the parent it expands and the children it creates,
and it is idempotent: applying the same expansion twice yields the same plan,
so a graph re-entry cannot double a fan-out.

What is deliberately *not* here: a general `$ref` resolver (AGENT-005) or a
planner. The only binding performed is of the fan-out alias (`fanout.as`)
inside the parent's arguments; any other argument value is copied verbatim to
every child and resolved, if at all, when the child executes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.agent.state import FanOut, Plan, PlanStep, StepStatus, ToolResult
from app.errors import ReferenceResolutionError

__all__ = [
    "FanOutExpansion",
    "FanOutResolutionError",
    "REF_KEY",
    "apply_expansion",
    "bind_fanout_item",
    "child_step_id",
    "children_of",
    "is_expanded",
    "is_unexpanded_fanout",
    "plan_fanout_expansion",
    "resolve_fanout_items",
]

#: The reference marker of §4.4: `{"$ref": "lead.company_id"}`.
REF_KEY = "$ref"
#: The artifact-store segment every `over` path must cross (`s1.output.leads`).
OUTPUT_SEGMENT = "output"


class FanOutResolutionError(ReferenceResolutionError):
    """The fan-out's list or an alias binding could not be resolved.

    Classified as a `reference_resolution` **planning** fault (§4.4): the
    same expansion against the same artifacts cannot succeed, so `decide`
    routes it to replan (rule 4), never to retry.
    """


@dataclass(frozen=True)
class FanOutExpansion:
    """The outcome of planning an expansion, before it is applied to a plan."""

    parent_step_id: str
    children: tuple[PlanStep, ...]
    #: How many items the `over` path produced *before* `max_items` applied.
    total_items: int
    #: `max_items` bounded the expansion: only the first `max_items` items
    #: became children, in list order.
    truncated: bool


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------
def child_step_id(parent_step_id: str, index: int) -> str:
    """`s2` + `1` → `s2[1]` (§12.3)."""
    return f"{parent_step_id}[{index}]"


def children_of(plan: Plan, parent_step_id: str) -> list[PlanStep]:
    return [s for s in plan.steps if s.parent_step_id == parent_step_id]


def is_unexpanded_fanout(step: PlanStep) -> bool:
    """Rule 5's predicate: a fan-out parent that has not yet been expanded.

    An expanded parent is marked `succeeded` (its job — producing children —
    is done; execution outcomes live on the children), so the predicate is
    false for it and a second `decide` pass cannot expand it again.
    """
    return step.fanout is not None and step.status in (StepStatus.PENDING, StepStatus.READY)


def is_expanded(step: PlanStep) -> bool:
    return step.fanout is not None and step.status == StepStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def _walk(root: Any, segments: Sequence[str], *, path: str) -> Any:  # noqa: ANN401 - walks arbitrary JSON-like data
    """Dot-separated keys and numeric indices — nothing else (§4.4)."""
    current = root
    for segment in segments:
        if isinstance(current, Mapping):
            if segment not in current:
                raise FanOutResolutionError(
                    f"fan-out path {path!r}: key {segment!r} not found",
                    detail={"path": path, "segment": segment},
                )
            current = current[segment]
        elif isinstance(current, list):
            if not segment.isdigit() or int(segment) >= len(current):
                raise FanOutResolutionError(
                    f"fan-out path {path!r}: index {segment!r} out of range",
                    detail={"path": path, "segment": segment},
                )
            current = current[int(segment)]
        else:
            raise FanOutResolutionError(
                f"fan-out path {path!r}: cannot descend into {type(current).__name__}"
                f" at {segment!r}",
                detail={"path": path, "segment": segment},
            )
    return current


def resolve_fanout_items(fanout: FanOut, tool_results: Mapping[str, ToolResult]) -> list[Any]:
    """Resolve `fanout.over` (`s1.output.leads`) against the artifact store.

    The path must name an earlier step that has produced a result, cross its
    `output`, and land on a list. Anything else is a planning fault.
    """
    path = fanout.over
    segments = path.split(".")
    if len(segments) < 2 or not segments[0] or segments[1] != OUTPUT_SEGMENT:
        raise FanOutResolutionError(
            f"fan-out path {path!r} must have the form '<step_id>.output[.<key>...]'",
            detail={"path": path},
        )
    step_id = segments[0]
    result = tool_results.get(step_id)
    if result is None:
        raise FanOutResolutionError(
            f"fan-out path {path!r}: step {step_id!r} has produced no result",
            detail={"path": path, "step_id": step_id},
        )
    items = _walk(result.output, segments[2:], path=path)
    if not isinstance(items, list):
        raise FanOutResolutionError(
            f"fan-out path {path!r} resolved to {type(items).__name__}, not a list",
            detail={"path": path},
        )
    return items


def _bind_value(value: Any, alias: str, item: Any, *, index: int) -> Any:  # noqa: ANN401 - walks arbitrary JSON-like data
    """Replace `{"$ref": "<alias>[.path]"}` with the item (or a part of it).

    Any other `$ref` — one that names a step, not the alias — is left exactly
    as written for the child's own resolution at execute time.
    """
    if isinstance(value, dict):
        ref = value.get(REF_KEY)
        if isinstance(ref, str) and len(value) == 1:
            segments = ref.split(".")
            if segments[0] == alias:
                return _walk(item, segments[1:], path=f"{ref} (item {index})")
            return value
        return {k: _bind_value(v, alias, item, index=index) for k, v in value.items()}
    if isinstance(value, list):
        return [_bind_value(v, alias, item, index=index) for v in value]
    return value


def bind_fanout_item(
    args: Mapping[str, Any],
    alias: str,
    item: Any,  # noqa: ANN401 - an item is arbitrary JSON-like data
    *,
    index: int = 0,
) -> dict[str, Any]:
    """Bind one item into the parent's arguments, producing a child's literal
    arguments. Deterministic: the same item and arguments always bind to the
    same result."""
    return {k: _bind_value(v, alias, item, index=index) for k, v in args.items()}


# ---------------------------------------------------------------------------
# Planning and applying an expansion
# ---------------------------------------------------------------------------
def plan_fanout_expansion(
    parent: PlanStep, tool_results: Mapping[str, ToolResult]
) -> FanOutExpansion:
    """Compute the children a fan-out parent expands into, without touching
    any plan. Raises `FanOutResolutionError` if the list or any binding is
    unresolvable, so that applying an expansion can never fail half-way.

    `max_items` is mandatory and bounds the expansion (§4.5): items beyond it
    are dropped, in list order, and the result records that it happened.
    """
    if parent.fanout is None:
        raise ValueError(f"step {parent.step_id!r} has no fanout to expand")
    fanout = parent.fanout
    items = resolve_fanout_items(fanout, tool_results)
    selected = items[: fanout.max_items]
    children = tuple(
        PlanStep(
            step_id=child_step_id(parent.step_id, index),
            tool=parent.tool,
            args=bind_fanout_item(parent.args, fanout.as_, item, index=index),
            depends_on=list(parent.depends_on),
            rationale=parent.rationale,
            optional=parent.optional,
            fanout=None,
            parent_step_id=parent.step_id,
            status=StepStatus.PENDING,
        )
        for index, item in enumerate(selected)
    )
    return FanOutExpansion(
        parent_step_id=parent.step_id,
        children=children,
        total_items=len(items),
        truncated=len(items) > len(selected),
    )


def apply_expansion(plan: Plan, expansion: FanOutExpansion) -> Plan:
    """Return a new plan with the children placed immediately after their
    parent, in index order, and the parent marked expanded.

    Every other step is carried over untouched (the same objects). A child
    whose `step_id` already exists in the plan keeps its existing state and
    is moved into canonical position rather than inserted again, so the
    operation is idempotent under re-entry and never duplicates a child.
    """
    child_ids = {c.step_id for c in expansion.children}
    existing = {s.step_id: s for s in plan.steps if s.step_id in child_ids}
    steps: list[PlanStep] = []
    found = False
    for step in plan.steps:
        if step.step_id in existing:
            continue  # re-emitted in canonical position below
        if step.step_id != expansion.parent_step_id:
            steps.append(step)
            continue
        found = True
        steps.append(step.model_copy(update={"status": StepStatus.SUCCEEDED}))
        steps.extend(existing.get(c.step_id, c) for c in expansion.children)
    if not found:
        raise ValueError(f"step {expansion.parent_step_id!r} is not in plan {plan.plan_id!r}")
    return plan.model_copy(update={"steps": steps})
