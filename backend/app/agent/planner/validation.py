"""Deterministic plan validation: the registry allowlist made executable (§4.2, §7, §16.3).

Every plan — rule-built, model-proposed or supplied at run creation — passes
through `validate_plan` before the `plan` node accepts it. The checks are pure
functions over the plan, the normalized task, the contract registry and the
budgets: no network, no database, no clock. A model therefore cannot invent a
capability (unknown tool), reach outside its intent (tool not allowed), plan an
unexecutable graph (missing dependency, cycle, forward reference), reshape a
tool (unknown or ill-typed argument), pre-authorise itself (planned
`approval_token` / `idempotency_key`) or claim work already happened (a step
that is not pending).

Reference *syntax* and DAG consistency are checked here; reference
*resolution* against real artifacts stays with `app.agent.resolver`
(AGENT-005) at execution time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Final

from pydantic import TypeAdapter, ValidationError
from pydantic.fields import FieldInfo

from app.agent.fanout import OUTPUT_SEGMENT, child_step_id
from app.agent.normalizer import CanonicalIntent
from app.agent.resolver import REF_KEY, REF_PREFIX, parse_ref_path
from app.agent.state import Budgets, Model, NormalizedTask, Plan, PlanStep, StepStatus
from app.errors import PlannerError, ReferenceResolutionError
from app.tools.contracts import REGISTRY, ToolContract, ToolName

__all__ = [
    "DISPATCHER_OWNED_ARGS",
    "INTENT_TOOLS",
    "OUTREACH_TOOLS",
    "PlanIssue",
    "PlanValidationError",
    "allowed_tools",
    "assert_valid_plan",
    "planner_visible_fields",
    "validate_plan",
]

#: Arguments the dispatcher alone supplies (§8.5, ADR-020). A plan that
#: carries one is trying to authorise or key its own effect.
DISPATCHER_OWNED_ARGS: Final[frozenset[str]] = frozenset({"idempotency_key", "approval_token"})

#: `s3`, `s2[1]`, `draft-1`. No dots or `$`: both would collide with the
#: `$ref` path language, and no whitespace: ids are cited verbatim everywhere.
STEP_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\[\d+\])?$")
ALIAS_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CHILD_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?P<parent>.+)\[(?P<index>\d+)\]$")

#: Statuses a plan may carry when it is accepted. Anything else encodes an
#: execution outcome, which only `execute_tool`, `request_approval` and
#: `recover` may record. `skipped` and `rejected` are admitted because a
#: revision keeps them: a skipped optional step is not re-attempted and a
#: human's rejection is never forgotten.
ACCEPTED_STEP_STATUSES: Final[frozenset[StepStatus]] = frozenset(
    {StepStatus.PENDING, StepStatus.SKIPPED, StepStatus.REJECTED}
)

_LEAD_READ_TOOLS: Final[frozenset[ToolName]] = frozenset(
    {ToolName.SEARCH_LEADS, ToolName.GET_LEAD, ToolName.RESEARCH_COMPANY, ToolName.SCORE_LEAD}
)
OUTREACH_TOOLS: Final[frozenset[ToolName]] = frozenset(
    {ToolName.DRAFT_OUTREACH, ToolName.SAVE_DRAFT, ToolName.SEND_EMAIL_MOCK}
)

#: The vocabulary each canonical intent may draw on. Customer intents never
#: reach lead tools and vice versa; read-only intents never reach a mutating
#: tool. The send in an outreach intent is still gated by a human (§9): the
#: allowlist bounds what can be *proposed*, the gate bounds what can *happen*.
INTENT_TOOLS: Final[dict[str, frozenset[ToolName]]] = {
    CanonicalIntent.PROSPECT_AND_OUTREACH: _LEAD_READ_TOOLS | OUTREACH_TOOLS,
    CanonicalIntent.LEAD_SEARCH: _LEAD_READ_TOOLS,
    CanonicalIntent.LEAD_LOOKUP: frozenset({ToolName.GET_LEAD, ToolName.RESEARCH_COMPANY}),
    CanonicalIntent.COMPANY_RESEARCH: frozenset(
        {ToolName.RESEARCH_COMPANY, ToolName.GET_LEAD, ToolName.SEARCH_LEADS}
    ),
    CanonicalIntent.LEAD_SCORING: _LEAD_READ_TOOLS,
    CanonicalIntent.DRAFT_OUTREACH: _LEAD_READ_TOOLS | OUTREACH_TOOLS,
    CanonicalIntent.CUSTOMER_LOOKUP: frozenset({ToolName.GET_CUSTOMER}),
    CanonicalIntent.CUSTOMER_UPDATE: frozenset({ToolName.GET_CUSTOMER, ToolName.UPDATE_CUSTOMER}),
}


def allowed_tools(task: NormalizedTask) -> frozenset[ToolName]:
    """The tools a plan for `task` may name. Empty for anything out of scope
    or unrecognised — such a task is not plannable at all."""
    if not task.in_scope:
        return frozenset()
    allowed = INTENT_TOOLS.get(task.intent, frozenset())
    if task.intent == CanonicalIntent.LEAD_SEARCH and task.requires_mutation:
        # "find fintech leads and email them": the normalizer flags the
        # mutation on the search intent (§7) rather than re-classifying it.
        allowed = allowed | OUTREACH_TOOLS
    return allowed


class PlanIssue(Model):
    """One deterministic reason a plan was rejected. Fed back verbatim to the
    repair prompt and recorded on the run's error."""

    code: str
    message: str
    step_id: str | None = None


class PlanValidationError(PlannerError):
    """The plan failed deterministic validation (a `planner_error`, §10.1)."""

    def __init__(self, issues: list[PlanIssue], *, message: str | None = None) -> None:
        self.issues = list(issues)
        summary = "; ".join(f"{i.code}" + (f"[{i.step_id}]" if i.step_id else "") for i in issues)
        super().__init__(
            message or f"plan failed validation: {summary}",
            detail={"issues": [i.model_dump() for i in issues]},
        )


def planner_visible_fields(contract: ToolContract) -> dict[str, FieldInfo]:
    """The input fields a plan may set: the contract's, minus the dispatcher's."""
    return {
        name: field
        for name, field in contract.input_model.model_fields.items()
        if name not in DISPATCHER_OWNED_ARGS
    }


# ---------------------------------------------------------------------------
# Reference collection
# ---------------------------------------------------------------------------
def _collect_refs(value: Any, issues: list[PlanIssue], step_id: str) -> list[str]:  # noqa: ANN401 - walks arbitrary JSON-like data
    """Every `$ref` path inside an argument value, flagging malformed markers."""
    refs: list[str] = []
    if isinstance(value, dict):
        if REF_KEY in value:
            if len(value) != 1 or not isinstance(value[REF_KEY], str):
                issues.append(
                    PlanIssue(
                        code="malformed_reference",
                        step_id=step_id,
                        message=(
                            f"step {step_id!r}: a reference is exactly {{'$ref': '<path>'}}, "
                            f"got keys {sorted(value)}"
                        ),
                    )
                )
                return refs
            refs.append(value[REF_KEY])
            return refs
        for v in value.values():
            refs.extend(_collect_refs(v, issues, step_id))
    elif isinstance(value, list):
        for v in value:
            refs.extend(_collect_refs(v, issues, step_id))
    elif isinstance(value, str) and value.startswith(REF_PREFIX):
        refs.append(value[len(REF_PREFIX) :])
    return refs


def _contains_ref(value: Any) -> bool:  # noqa: ANN401 - walks arbitrary JSON-like data
    if isinstance(value, dict):
        return REF_KEY in value or any(_contains_ref(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_ref(v) for v in value)
    return isinstance(value, str) and value.startswith(REF_PREFIX)


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------
def _dependency_closure(step_id: str, deps: Mapping[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    frontier = list(deps.get(step_id, []))
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(deps.get(current, []))
    return seen


def _has_cycle(deps: Mapping[str, list[str]]) -> bool:
    """Iterative DFS over declared dependencies (bounded by the plan's size)."""
    white, grey, black = 0, 1, 2
    colour = dict.fromkeys(deps, white)
    for root in deps:
        if colour[root] != white:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        colour[root] = grey
        while stack:
            node, index = stack[-1]
            children = [d for d in deps.get(node, []) if d in colour]
            if index < len(children):
                stack[-1] = (node, index + 1)
                child = children[index]
                if colour[child] == grey:
                    return True
                if colour[child] == white:
                    colour[child] = grey
                    stack.append((child, 0))
            else:
                colour[node] = black
                stack.pop()
    return False


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------
def validate_plan(
    plan: Plan,
    *,
    task: NormalizedTask,
    contracts: Mapping[ToolName, ToolContract] = REGISTRY,
    budgets: Budgets | None = None,
) -> list[PlanIssue]:
    """Every reason `plan` may not be executed for `task`; empty when it may."""
    budgets = budgets or Budgets()
    issues: list[PlanIssue] = []
    allowed = allowed_tools(task)
    if not allowed:
        issues.append(
            PlanIssue(
                code="intent_not_plannable",
                message=f"no tools are available for intent {task.intent!r}",
            )
        )

    steps = plan.steps
    if not steps:
        issues.append(PlanIssue(code="empty_plan", message="a plan needs at least one step"))
    if len(steps) > budgets.max_steps:
        issues.append(
            PlanIssue(
                code="too_many_steps",
                message=f"{len(steps)} steps exceed the run's MAX_STEPS of {budgets.max_steps}",
            )
        )

    order: dict[str, int] = {}
    by_id: dict[str, PlanStep] = {}
    for index, step in enumerate(steps):
        if not STEP_ID_PATTERN.match(step.step_id):
            issues.append(
                PlanIssue(
                    code="invalid_step_id",
                    step_id=step.step_id,
                    message=f"step id {step.step_id!r} must match {STEP_ID_PATTERN.pattern}",
                )
            )
        if step.step_id in by_id:
            issues.append(
                PlanIssue(
                    code="duplicate_step_id",
                    step_id=step.step_id,
                    message=f"step id {step.step_id!r} is used more than once",
                )
            )
            continue
        by_id[step.step_id] = step
        order[step.step_id] = index

    deps: dict[str, list[str]] = {sid: list(s.depends_on) for sid, s in by_id.items()}

    for step in by_id.values():
        sid = step.step_id
        contract = contracts.get(step.tool)
        if contract is None:
            issues.append(
                PlanIssue(
                    code="unknown_tool",
                    step_id=sid,
                    message=f"step {sid!r} names {step.tool!r}, which is not a registered tool",
                )
            )
        elif step.tool not in allowed:
            issues.append(
                PlanIssue(
                    code="tool_not_allowed",
                    step_id=sid,
                    message=(
                        f"step {sid!r}: tool {step.tool.value!r} is not allowed for intent "
                        f"{task.intent!r} (allowed: {sorted(t.value for t in allowed)})"
                    ),
                )
            )
        if step.status not in ACCEPTED_STEP_STATUSES:
            issues.append(
                PlanIssue(
                    code="status_encodes_execution",
                    step_id=sid,
                    message=(
                        f"step {sid!r} is {step.status.value!r}; a plan describes work to do, "
                        "never work already done"
                    ),
                )
            )
        _check_dependencies(step, deps, order, issues)
        _check_fanout(step, by_id, order, deps, issues)
        _check_parent(step, by_id, order, issues)
        if contract is not None:
            _check_args(step, contract, by_id, order, deps, issues)

    if not any(i.code in {"missing_dependency", "self_dependency"} for i in issues) and _has_cycle(
        deps
    ):
        issues.append(
            PlanIssue(code="dependency_cycle", message="the dependency graph contains a cycle")
        )
    return issues


def assert_valid_plan(
    plan: Plan,
    *,
    task: NormalizedTask,
    contracts: Mapping[ToolName, ToolContract] = REGISTRY,
    budgets: Budgets | None = None,
) -> Plan:
    issues = validate_plan(plan, task=task, contracts=contracts, budgets=budgets)
    if issues:
        raise PlanValidationError(issues)
    return plan


# ---------------------------------------------------------------------------
# Per-step checks
# ---------------------------------------------------------------------------
def _check_dependencies(
    step: PlanStep,
    deps: Mapping[str, list[str]],
    order: Mapping[str, int],
    issues: list[PlanIssue],
) -> None:
    sid = step.step_id
    for dep in step.depends_on:
        if dep == sid:
            issues.append(
                PlanIssue(
                    code="self_dependency",
                    step_id=sid,
                    message=f"step {sid!r} depends on itself",
                )
            )
        elif dep not in order:
            issues.append(
                PlanIssue(
                    code="missing_dependency",
                    step_id=sid,
                    message=f"step {sid!r} depends on {dep!r}, which is not in the plan",
                )
            )
        elif order[dep] > order[sid]:
            # Steps execute one at a time in plan order (§4.6): a dependency
            # listed later can never be satisfied when this step is reached.
            issues.append(
                PlanIssue(
                    code="dependency_after_step",
                    step_id=sid,
                    message=f"step {sid!r} depends on {dep!r}, which appears later in the plan",
                )
            )


def _check_fanout(
    step: PlanStep,
    by_id: Mapping[str, PlanStep],
    order: Mapping[str, int],
    deps: Mapping[str, list[str]],
    issues: list[PlanIssue],
) -> None:
    """`max_items` is bounded by the `FanOut` model (1..50); MAX_STEPS is
    enforced as children execute, on every `decide` pass, never by refusing
    the expansion (§4.5, AGENT-004)."""
    fanout = step.fanout
    sid = step.step_id
    if fanout is None:
        return
    if step.parent_step_id is not None:
        issues.append(
            PlanIssue(
                code="nested_fanout",
                step_id=sid,
                message=f"step {sid!r} is a fan-out child and cannot fan out itself",
            )
        )
    if not ALIAS_PATTERN.match(fanout.as_):
        issues.append(
            PlanIssue(
                code="invalid_fanout_alias",
                step_id=sid,
                message=f"step {sid!r}: fan-out alias {fanout.as_!r} is not an identifier",
            )
        )
    if fanout.as_ in by_id:
        issues.append(
            PlanIssue(
                code="ambiguous_fanout_alias",
                step_id=sid,
                message=f"step {sid!r}: fan-out alias {fanout.as_!r} is also a step id",
            )
        )
    segments = fanout.over.split(".")
    if len(segments) < 2 or not segments[0] or segments[1] != OUTPUT_SEGMENT:
        issues.append(
            PlanIssue(
                code="invalid_fanout_path",
                step_id=sid,
                message=(
                    f"step {sid!r}: fan-out path {fanout.over!r} must have the form "
                    f"'<step_id>.{OUTPUT_SEGMENT}[.<key>...]'"
                ),
            )
        )
        return
    _check_target(
        step,
        target=segments[0],
        path=fanout.over,
        by_id=by_id,
        order=order,
        deps=deps,
        issues=issues,
        what="fan-out path",
    )


def _check_parent(
    step: PlanStep,
    by_id: Mapping[str, PlanStep],
    order: Mapping[str, int],
    issues: list[PlanIssue],
) -> None:
    """A step may claim a parent only if it really is that parent's child."""
    parent_id = step.parent_step_id
    sid = step.step_id
    if parent_id is None:
        return
    parent = by_id.get(parent_id)
    match = CHILD_ID_PATTERN.match(sid)
    legitimate = (
        parent is not None
        and parent.fanout is not None
        and match is not None
        and match.group("parent") == parent_id
        and sid == child_step_id(parent_id, int(match.group("index")))
        and int(match.group("index")) < parent.fanout.max_items
        and order[parent_id] < order[sid]
        and parent.tool == step.tool
    )
    if not legitimate:
        issues.append(
            PlanIssue(
                code="invalid_parent",
                step_id=sid,
                message=(
                    f"step {sid!r} claims parent {parent_id!r} but is not one of its fan-out "
                    "children"
                ),
            )
        )


def _check_target(
    step: PlanStep,
    *,
    target: str,
    path: str,
    by_id: Mapping[str, PlanStep],
    order: Mapping[str, int],
    deps: Mapping[str, list[str]],
    issues: list[PlanIssue],
    what: str,
) -> None:
    """A referenced step must exist (or be a prospective child of an earlier
    fan-out), precede this step and be one of its (transitive) dependencies —
    the artifact must exist by the time the reference is resolved (§4.4)."""
    sid = step.step_id
    if target == sid:
        issues.append(
            PlanIssue(
                code="self_reference",
                step_id=sid,
                message=f"step {sid!r}: {what} {path!r} refers to the step itself",
            )
        )
        return
    resolved = target
    if target not in by_id:
        match = CHILD_ID_PATTERN.match(target)
        parent = by_id.get(match.group("parent")) if match else None
        if (
            match is None
            or parent is None
            or parent.fanout is None
            or int(match.group("index")) >= parent.fanout.max_items
        ):
            issues.append(
                PlanIssue(
                    code="unknown_reference_target",
                    step_id=sid,
                    message=f"step {sid!r}: {what} {path!r} refers to a step not in the plan",
                )
            )
            return
        resolved = parent.step_id
    if order[resolved] > order[sid]:
        issues.append(
            PlanIssue(
                code="reference_to_later_step",
                step_id=sid,
                message=f"step {sid!r}: {what} {path!r} refers to a step that runs later",
            )
        )
        return
    closure = _dependency_closure(sid, deps)
    parent_id = by_id[resolved].parent_step_id if resolved in by_id else None
    if resolved not in closure and (parent_id is None or parent_id not in closure):
        # Depending on a fan-out parent covers every child it expands into
        # (`dependencies_satisfied`, §4.5), whether or not the child already
        # appears in the plan — a revision lists expanded children.
        issues.append(
            PlanIssue(
                code="reference_outside_dependencies",
                step_id=sid,
                message=(
                    f"step {sid!r}: {what} {path!r} refers to {resolved!r}, which is not among "
                    "its dependencies (add it to depends_on)"
                ),
            )
        )


def _check_args(
    step: PlanStep,
    contract: ToolContract,
    by_id: Mapping[str, PlanStep],
    order: Mapping[str, int],
    deps: Mapping[str, list[str]],
    issues: list[PlanIssue],
) -> None:
    sid = step.step_id
    visible = planner_visible_fields(contract)
    for key in step.args:
        if key in DISPATCHER_OWNED_ARGS:
            issues.append(
                PlanIssue(
                    code="dispatcher_owned_argument",
                    step_id=sid,
                    message=(
                        f"step {sid!r} plans {key!r}; the dispatcher alone supplies it, a plan "
                        "cannot authorise or key its own effect"
                    ),
                )
            )
        elif key not in visible:
            issues.append(
                PlanIssue(
                    code="unknown_argument",
                    step_id=sid,
                    message=(
                        f"step {sid!r}: {contract.name.value} has no argument {key!r} "
                        f"(accepts {sorted(visible)})"
                    ),
                )
            )
    for name, field in visible.items():
        if field.is_required() and name not in step.args:
            issues.append(
                PlanIssue(
                    code="missing_argument",
                    step_id=sid,
                    message=f"step {sid!r}: {contract.name.value} requires argument {name!r}",
                )
            )

    # References: syntax, then DAG consistency.
    alias = step.fanout.as_ if step.fanout is not None else None
    for ref in _collect_refs(step.args, issues, sid):
        head = ref.split(".", 1)[0]
        if alias is not None and head == alias:
            continue
        if head in {s.fanout.as_ for s in by_id.values() if s.fanout is not None}:
            issues.append(
                PlanIssue(
                    code="unbound_alias",
                    step_id=sid,
                    message=(
                        f"step {sid!r}: reference {ref!r} uses fan-out alias {head!r}, but "
                        "this step does not fan out with that alias"
                    ),
                )
            )
            continue
        try:
            target, _ = parse_ref_path(ref)
        except ReferenceResolutionError as exc:
            issues.append(
                PlanIssue(
                    code="invalid_reference",
                    step_id=sid,
                    message=f"step {sid!r}: reference {ref!r} is malformed: {exc}",
                )
            )
            continue
        _check_target(
            step,
            target=target,
            path=ref,
            by_id=by_id,
            order=order,
            deps=deps,
            issues=issues,
            what="reference",
        )

    # Literal values are typed against the contract now; referenced values
    # are typed at execution, once they exist.
    has_any_ref = False
    for name, value in step.args.items():
        if name not in visible:
            continue
        field = visible[name]
        if _contains_ref(value):
            has_any_ref = True
            continue
        try:
            _adapter_for(field).validate_python(value)
        except ValidationError as exc:
            issues.append(
                PlanIssue(
                    code="invalid_argument",
                    step_id=sid,
                    message=(
                        f"step {sid!r}: argument {name!r} does not satisfy "
                        f"{contract.name.value}: {_first_error(exc)}"
                    ),
                )
            )
    if (
        not has_any_ref
        and not contract.requires_approval
        and not any(i.step_id == sid and i.code.endswith("argument") for i in issues)
    ):
        # Whole-model validators ("at least one filter", "exactly one of") run
        # only when every argument is literal. Gated tools are excluded because
        # their models require the dispatcher-owned fields; the dispatcher
        # validates them in full before any effect (§8.5).
        try:
            contract.input_model.model_validate(step.args)
        except ValidationError as exc:
            issues.append(
                PlanIssue(
                    code="invalid_arguments",
                    step_id=sid,
                    message=(
                        f"step {sid!r}: arguments do not satisfy {contract.name.value}: "
                        f"{_first_error(exc)}"
                    ),
                )
            )


def _adapter_for(field: FieldInfo) -> TypeAdapter[Any]:
    """A validator for one field carrying the field's own constraints
    (`ge`, `max_length`, …), which live in `FieldInfo.metadata`."""
    annotation = field.annotation
    metadata = list(field.metadata)
    if metadata:
        annotation = Annotated.__class_getitem__((annotation, *metadata))  # type: ignore[attr-defined]
    return TypeAdapter(annotation)


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    location = ".".join(str(p) for p in first.get("loc", ()))
    return f"{location}: {first.get('msg', '')}".strip(": ")
