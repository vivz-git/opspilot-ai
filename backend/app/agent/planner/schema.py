"""The structured-output contract between the model and the planner (§4.2, §16.3).

A model never emits a `Plan`. It emits a `ProposedPlan`: the same shape minus
everything a plan is not allowed to decide — no step status, no parent, no
approval or verification flags, no ids for the plan itself. `extra="forbid"`
on every level means a response that tries to carry any of those (or a
`requires_approval: false`, or a free-form `command`) is rejected as
schema-invalid before it is ever read as a plan.

The provider is asked for JSON conforming to `response_json_schema()`; the
response is parsed by `json.loads` and `ProposedPlan.model_validate` and
nothing else. There is no lenient parsing, no bracket balancing, no regex
extraction: malformed output is a rejected proposal, not a puzzle.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.planner.validation import PlanIssue
from app.agent.state import FanOut, Plan, PlannerKind, PlanStep, StepStatus
from app.tools.contracts import REGISTRY, ToolContract, ToolName

__all__ = [
    "PLAN_SCHEMA_NAME",
    "ProposedFanOut",
    "ProposedPlan",
    "ProposedStep",
    "parse_proposal",
    "proposal_to_plan",
    "response_json_schema",
]

PLAN_SCHEMA_NAME = "opspilot_plan"

#: Bounds on the raw response, independent of any budget: a plan for nine
#: tools never needs more, and an unbounded document is a cost, not a plan.
MAX_RESPONSE_BYTES = 64 * 1024


class _Proposed(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ProposedFanOut(_Proposed):
    over: str = Field(
        description="Path to a list in an earlier step's output: '<step_id>.output.<key>'"
    )
    as_: str = Field(
        alias="as", description="Alias bound to each item, used as {'$ref': '<alias>.<field>'}"
    )
    max_items: int = Field(ge=1, le=50, description="Upper bound on the number of items expanded")


class ProposedStep(_Proposed):
    step_id: str = Field(description="Stable id such as 's1', 's2'; cited by depends_on and $ref")
    tool: str = Field(description="Exactly one registered tool name from the catalog")
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Tool arguments: literals and/or {'$ref': '<step_id>.output.<path>'} bindings",
    )
    depends_on: list[str] = Field(
        default_factory=list, description="Ids of earlier steps whose output this step uses"
    )
    rationale: str = Field(default="", description="Why this step exists; shown to the operator")
    optional: bool = Field(
        default=False, description="True if the run may continue without this step's result"
    )
    fanout: ProposedFanOut | None = Field(
        default=None, description="Expand this step once per item of an earlier list output"
    )


class ProposedPlan(_Proposed):
    steps: list[ProposedStep] = Field(description="Steps in execution order")


def _inline_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace `$ref`s to `$defs` with the definitions themselves, and refuse
    unknown keys on every object except a step's free-form `args`. The
    provider then receives one self-contained document."""
    defs: dict[str, Any] = schema.pop("$defs", {})

    def walk(node: Any, *, key: str | None = None) -> Any:  # noqa: ANN401 - walks a JSON schema
        if isinstance(node, dict):
            if "$ref" in node and isinstance(node["$ref"], str):
                name = node["$ref"].rsplit("/", 1)[-1]
                return walk(dict(defs[name]))
            out = {k: walk(v, key=k) for k, v in node.items() if k != "title"}
            if out.get("type") == "object" and key != "args":
                out.setdefault("additionalProperties", False)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result = walk(schema)
    if not isinstance(result, dict):  # pragma: no cover - a model schema is always an object
        raise TypeError("schema root must be an object")
    return result


def response_json_schema() -> dict[str, Any]:
    """The JSON Schema the provider is asked to conform to."""
    return _inline_defs(ProposedPlan.model_json_schema(by_alias=True))


def parse_proposal(raw: str) -> tuple[ProposedPlan | None, list[PlanIssue]]:
    """Strict parse of a raw completion. Any deviation is an issue, never a fix."""
    if len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return None, [
            PlanIssue(code="response_too_large", message="the response exceeds the size bound")
        ]
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [
            PlanIssue(code="malformed_json", message=f"the response is not valid JSON: {exc.msg}")
        ]
    if not isinstance(document, dict):
        return None, [
            PlanIssue(code="malformed_json", message="the response must be a JSON object")
        ]
    try:
        return ProposedPlan.model_validate(document), []
    except ValidationError as exc:
        return None, [
            PlanIssue(
                code="schema_violation",
                message=(
                    f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', '')}".strip(": ")
                ),
            )
            for e in exc.errors()[:10]
        ]


def proposal_to_plan(
    proposal: ProposedPlan,
    *,
    plan_id: str,
    revision: int,
    created_by: PlannerKind,
    contracts: Mapping[ToolName, ToolContract] = REGISTRY,
) -> tuple[Plan | None, list[PlanIssue]]:
    """Deterministic conversion into the execution contract. Every step
    starts `pending`; an unknown tool name is an issue rather than a guess."""
    issues: list[PlanIssue] = []
    steps: list[PlanStep] = []
    for proposed in proposal.steps:
        try:
            tool = ToolName(proposed.tool)
        except ValueError:
            tool = None
        if tool is None or tool not in contracts:
            issues.append(
                PlanIssue(
                    code="unknown_tool",
                    step_id=proposed.step_id,
                    message=(
                        f"step {proposed.step_id!r} names {proposed.tool!r}, which is not a "
                        f"registered tool (registered: {sorted(t.value for t in contracts)})"
                    ),
                )
            )
            continue
        try:
            fanout = (
                FanOut(
                    over=proposed.fanout.over,
                    as_=proposed.fanout.as_,
                    max_items=proposed.fanout.max_items,
                )
                if proposed.fanout is not None
                else None
            )
            steps.append(
                PlanStep(
                    step_id=proposed.step_id,
                    tool=tool,
                    args=dict(proposed.args),
                    depends_on=list(proposed.depends_on),
                    rationale=proposed.rationale,
                    optional=proposed.optional,
                    fanout=fanout,
                    parent_step_id=None,
                    status=StepStatus.PENDING,
                )
            )
        except ValidationError as exc:
            issues.append(
                PlanIssue(
                    code="schema_violation",
                    step_id=proposed.step_id,
                    message=f"step {proposed.step_id!r}: {exc.errors()[0].get('msg', '')}",
                )
            )
    if issues:
        return None, issues
    return Plan(plan_id=plan_id, revision=revision, created_by=created_by, steps=steps), []
