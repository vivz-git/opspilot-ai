"""AGENT-004: the fan-out expansion engine (§4.5, ADR-006).

`app.agent.fanout` is pure: the same parent step and artifact store always
produce the same children, in the same order, and applying an expansion never
touches a step other than the parent and its children. These tests pin the
expansion semantics `decide` relies on (`tests/test_decide.py` covers the
router that invokes it).

Covers:
- normal expansion: `s2` over three items → `s2[0]`, `s2[1]`, `s2[2]`
- empty input: zero children, parent still marked expanded
- the `max_items` boundary: exactly `max_items` expands whole; one more truncates
- deterministic ordering: children mirror list order, repeatably
- idempotency: applying an expansion twice, or over existing children, adds nothing
- alias binding (`{"$ref": "lead.company_id"}`) and what is left untouched
- resolution faults are `reference_resolution` planning faults (§4.4)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from app.agent.fanout import (
    REF_KEY,
    FanOutExpansion,
    FanOutResolutionError,
    apply_expansion,
    bind_fanout_item,
    child_step_id,
    children_of,
    is_expanded,
    is_unexpanded_fanout,
    plan_fanout_expansion,
    resolve_fanout_items,
)
from app.agent.state import FanOut, Plan, PlanStep, StepStatus, ToolResult
from app.errors import ErrorClass, ReferenceResolutionError
from app.tools.contracts import ToolName

pytestmark = [pytest.mark.unit]

T0 = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def leads(n: int) -> list[dict[str, Any]]:
    return [{"lead_id": f"lead_{i}", "company_id": f"co_{i}", "tags": [f"t{i}"]} for i in range(n)]


def search_result(items: list[Any], *, step_id: str = "s1") -> dict[str, ToolResult]:
    return {
        step_id: ToolResult(
            step_id=step_id,
            tool=ToolName.SEARCH_LEADS,
            output={"leads": items, "total_matched": len(items)},
            produced_at=T0,
        )
    }


def fanout_step(
    *,
    step_id: str = "s2",
    over: str = "s1.output.leads",
    alias: str = "lead",
    max_items: int = 10,
    args: dict[str, Any] | None = None,
    optional: bool = False,
    depends_on: list[str] | None = None,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        tool=ToolName.RESEARCH_COMPANY,
        args=args if args is not None else {"company_id": {REF_KEY: f"{alias}.company_id"}},
        depends_on=depends_on if depends_on is not None else ["s1"],
        rationale="research each lead",
        optional=optional,
        fanout=FanOut(over=over, **{"as": alias}, max_items=max_items),
    )


def three_step_plan(parent: PlanStep | None = None) -> Plan:
    parent = parent or fanout_step()
    return Plan(
        plan_id="p1",
        steps=[
            PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS, status=StepStatus.SUCCEEDED),
            parent,
            PlanStep(step_id="s3", tool=ToolName.SCORE_LEAD, depends_on=["s2"]),
        ],
    )


# ---------------------------------------------------------------------------
# 1. Normal expansion
# ---------------------------------------------------------------------------
class TestNormalExpansion:
    def test_three_items_become_three_ordered_children(self) -> None:
        expansion = plan_fanout_expansion(fanout_step(), search_result(leads(3)))

        assert expansion.parent_step_id == "s2"
        assert [c.step_id for c in expansion.children] == ["s2[0]", "s2[1]", "s2[2]"]
        assert [c.args for c in expansion.children] == [
            {"company_id": "co_0"},
            {"company_id": "co_1"},
            {"company_id": "co_2"},
        ]
        assert expansion.total_items == 3
        assert expansion.truncated is False

    def test_children_are_ordinary_steps_inheriting_the_parent(self) -> None:
        parent = fanout_step(optional=True, depends_on=["s0", "s1"])
        expansion = plan_fanout_expansion(parent, search_result(leads(2)))

        for child in expansion.children:
            assert child.tool is parent.tool
            assert child.parent_step_id == "s2"
            assert child.fanout is None, "a child is never itself a fan-out"
            assert child.depends_on == ["s0", "s1"]
            assert child.depends_on is not parent.depends_on, "copied, not shared"
            assert child.optional is True
            assert child.rationale == parent.rationale
            assert child.status == StepStatus.PENDING

    def test_apply_inserts_children_after_parent_and_marks_parent_expanded(self) -> None:
        plan = three_step_plan()
        expansion = plan_fanout_expansion(plan.steps[1], search_result(leads(2)))

        expanded = apply_expansion(plan, expansion)

        assert [s.step_id for s in expanded.steps] == ["s1", "s2", "s2[0]", "s2[1]", "s3"]
        parent = expanded.step("s2")
        assert parent is not None
        assert parent.status == StepStatus.SUCCEEDED
        assert parent.fanout is not None, "the parent keeps its fan-out: the plan stays readable"
        assert is_expanded(parent)
        assert not is_unexpanded_fanout(parent)
        assert children_of(expanded, "s2") == list(expansion.children)

    def test_apply_leaves_every_other_step_untouched(self) -> None:
        plan = three_step_plan()
        expansion = plan_fanout_expansion(plan.steps[1], search_result(leads(2)))

        expanded = apply_expansion(plan, expansion)

        assert expanded.step("s1") is plan.steps[0], "same object, not a copy"
        assert expanded.step("s3") is plan.steps[2]
        assert expanded.plan_id == plan.plan_id
        assert expanded.revision == plan.revision
        assert expanded.created_by == plan.created_by

    def test_apply_does_not_mutate_the_input_plan(self) -> None:
        plan = three_step_plan()
        before = plan.model_copy(deep=True)
        expansion = plan_fanout_expansion(plan.steps[1], search_result(leads(2)))

        apply_expansion(plan, expansion)

        assert plan == before
        assert plan.steps[1].status == StepStatus.PENDING

    def test_child_step_id_format(self) -> None:
        assert child_step_id("s2", 0) == "s2[0]"
        assert child_step_id("s2", 11) == "s2[11]"


# ---------------------------------------------------------------------------
# 2. Empty input
# ---------------------------------------------------------------------------
class TestEmptyInput:
    def test_empty_list_expands_to_no_children(self) -> None:
        expansion = plan_fanout_expansion(fanout_step(), search_result([]))

        assert expansion.children == ()
        assert expansion.total_items == 0
        assert expansion.truncated is False

    def test_empty_expansion_still_marks_the_parent_expanded(self) -> None:
        """Otherwise rule 5 would re-select the parent forever."""
        plan = three_step_plan()
        expanded = apply_expansion(plan, plan_fanout_expansion(plan.steps[1], search_result([])))

        assert [s.step_id for s in expanded.steps] == ["s1", "s2", "s3"]
        parent = expanded.step("s2")
        assert parent is not None
        assert is_expanded(parent)
        assert children_of(expanded, "s2") == []


# ---------------------------------------------------------------------------
# 3. The max_items boundary and over-limit input
# ---------------------------------------------------------------------------
class TestMaxItemsBoundary:
    def test_exactly_max_items_expands_whole(self) -> None:
        expansion = plan_fanout_expansion(fanout_step(max_items=3), search_result(leads(3)))

        assert len(expansion.children) == 3
        assert expansion.truncated is False

    def test_one_over_max_items_truncates_to_max_items(self) -> None:
        expansion = plan_fanout_expansion(fanout_step(max_items=3), search_result(leads(4)))

        assert [c.step_id for c in expansion.children] == ["s2[0]", "s2[1]", "s2[2]"]
        assert [c.args["company_id"] for c in expansion.children] == ["co_0", "co_1", "co_2"]
        assert expansion.total_items == 4
        assert expansion.truncated is True

    def test_far_over_max_items_keeps_the_first_max_items_in_order(self) -> None:
        expansion = plan_fanout_expansion(fanout_step(max_items=10), search_result(leads(50)))

        assert len(expansion.children) == 10
        assert [c.args["company_id"] for c in expansion.children] == [f"co_{i}" for i in range(10)]
        assert expansion.total_items == 50
        assert expansion.truncated is True

    def test_max_items_is_mandatory_and_bounded_by_the_schema(self) -> None:
        with pytest.raises(ValueError):
            FanOut.model_validate({"over": "s1.output.leads", "as": "lead"})
        with pytest.raises(ValueError):
            FanOut.model_validate({"over": "s1.output.leads", "as": "lead", "max_items": 0})
        with pytest.raises(ValueError):
            FanOut.model_validate({"over": "s1.output.leads", "as": "lead", "max_items": 51})


# ---------------------------------------------------------------------------
# 4. Deterministic ordering and idempotency
# ---------------------------------------------------------------------------
class TestDeterminism:
    def test_repeated_expansion_is_identical(self) -> None:
        results = search_result(leads(5))
        first = plan_fanout_expansion(fanout_step(), results)
        for _ in range(5):
            assert plan_fanout_expansion(fanout_step(), results) == first

    def test_children_mirror_list_order_not_a_sort(self) -> None:
        items = [{"company_id": "zeta"}, {"company_id": "alpha"}, {"company_id": "mid"}]
        expansion = plan_fanout_expansion(fanout_step(), search_result(items))

        assert [c.args["company_id"] for c in expansion.children] == ["zeta", "alpha", "mid"]

    def test_applying_the_same_expansion_twice_adds_nothing(self) -> None:
        plan = three_step_plan()
        expansion = plan_fanout_expansion(plan.steps[1], search_result(leads(3)))

        once = apply_expansion(plan, expansion)
        twice = apply_expansion(once, expansion)

        assert twice == once
        assert [s.step_id for s in twice.steps] == ["s1", "s2", "s2[0]", "s2[1]", "s2[2]", "s3"]

    def test_existing_children_are_kept_not_duplicated(self) -> None:
        """A re-entered plan that already holds children (one of them done)
        gains no duplicates and loses no progress."""
        plan = three_step_plan()
        expansion = plan_fanout_expansion(plan.steps[1], search_result(leads(2)))
        done_child = expansion.children[0].model_copy(update={"status": StepStatus.SUCCEEDED})
        inconsistent = plan.model_copy(
            update={"steps": [plan.steps[0], plan.steps[1], done_child, plan.steps[2]]}
        )

        repaired = apply_expansion(inconsistent, expansion)

        assert [s.step_id for s in repaired.steps] == ["s1", "s2", "s2[0]", "s2[1]", "s3"]
        first = repaired.step("s2[0]")
        assert first is not None
        assert first.status == StepStatus.SUCCEEDED, "existing progress kept"

    def test_apply_to_a_plan_without_the_parent_is_an_error(self) -> None:
        plan = Plan(plan_id="p", steps=[PlanStep(step_id="s9", tool=ToolName.GET_LEAD)])
        expansion = FanOutExpansion(
            parent_step_id="s2", children=(), total_items=0, truncated=False
        )
        with pytest.raises(ValueError, match="not in plan"):
            apply_expansion(plan, expansion)

    def test_planning_expansion_of_a_non_fanout_step_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="no fanout"):
            plan_fanout_expansion(PlanStep(step_id="s1", tool=ToolName.GET_LEAD), {})


# ---------------------------------------------------------------------------
# 5. Alias binding
# ---------------------------------------------------------------------------
class TestAliasBinding:
    def test_whole_item_binding(self) -> None:
        item = {"company_id": "co_1", "tags": ["a"]}
        assert bind_fanout_item({"lead": {REF_KEY: "lead"}}, "lead", item) == {"lead": item}

    def test_nested_path_and_index_binding(self) -> None:
        item = {"company": {"id": "co_1"}, "tags": ["first", "second"]}
        bound = bind_fanout_item(
            {"company_id": {REF_KEY: "lead.company.id"}, "tag": {REF_KEY: "lead.tags.1"}},
            "lead",
            item,
        )
        assert bound == {"company_id": "co_1", "tag": "second"}

    def test_binding_recurses_into_containers(self) -> None:
        item = {"company_id": "co_1"}
        bound = bind_fanout_item(
            {"filters": {"ids": [{REF_KEY: "lead.company_id"}], "depth": "basic"}},
            "lead",
            item,
        )
        assert bound == {"filters": {"ids": ["co_1"], "depth": "basic"}}

    def test_step_refs_and_literals_are_left_untouched(self) -> None:
        """Only the alias is bound here; a `$ref` to a step is the child's to
        resolve at execute time (AGENT-005), and literals are literals."""
        args = {
            "template": {REF_KEY: "s0.output.template_id"},
            "depth": "standard",
            "lead.company_id": "a literal string that merely looks like a path",
            "meta": {"$ref": "lead.company_id", "extra": True},
        }
        bound = bind_fanout_item(args, "lead", {"company_id": "co_1"})
        assert bound == {
            "template": {REF_KEY: "s0.output.template_id"},
            "depth": "standard",
            "lead.company_id": "a literal string that merely looks like a path",
            "meta": {"$ref": "lead.company_id", "extra": True},
        }

    def test_binding_does_not_share_containers_between_children(self) -> None:
        expansion = plan_fanout_expansion(
            fanout_step(args={"ids": [{REF_KEY: "lead.company_id"}]}), search_result(leads(2))
        )
        assert expansion.children[0].args is not expansion.children[1].args
        assert expansion.children[0].args["ids"] is not expansion.children[1].args["ids"]

    def test_missing_key_in_item_is_a_resolution_fault(self) -> None:
        with pytest.raises(FanOutResolutionError, match="'domain' not found"):
            bind_fanout_item({"d": {REF_KEY: "lead.domain"}}, "lead", {"company_id": "co_1"})

    def test_path_into_a_scalar_item_is_a_resolution_fault(self) -> None:
        with pytest.raises(FanOutResolutionError, match="cannot descend"):
            bind_fanout_item({"d": {REF_KEY: "lead.company_id"}}, "lead", "co_1")

    def test_a_binding_fault_in_any_item_fails_the_whole_expansion(self) -> None:
        """Planning is all-or-nothing, so applying can never fail half-way."""
        items = [{"company_id": "co_0"}, {"other": "x"}, {"company_id": "co_2"}]
        with pytest.raises(FanOutResolutionError, match=r"item 1"):
            plan_fanout_expansion(fanout_step(), search_result(items))


# ---------------------------------------------------------------------------
# 6. `over` resolution
# ---------------------------------------------------------------------------
class TestOverResolution:
    def test_resolves_a_nested_list_through_keys_and_indices(self) -> None:
        results = {
            "s1": ToolResult(
                step_id="s1",
                tool=ToolName.SEARCH_LEADS,
                output={"pages": [{"leads": leads(2)}]},
                produced_at=T0,
            )
        }
        fanout = FanOut.model_validate(
            {"over": "s1.output.pages.0.leads", "as": "lead", "max_items": 5}
        )
        assert resolve_fanout_items(fanout, results) == leads(2)

    @pytest.mark.parametrize(
        ("over", "message"),
        [
            ("leads", "must have the form"),
            ("s1.leads", "must have the form"),
            (".output.leads", "must have the form"),
            ("s9.output.leads", "has produced no result"),
            ("s1.output.nope", "not found"),
            ("s1.output.leads.7", "out of range"),
            ("s1.output.leads.x", "out of range"),
            ("s1.output.total_matched", "not a list"),
            ("s1.output", "not a list"),
            ("s1.output.leads.0.lead_id.0", "cannot descend"),
        ],
    )
    def test_unresolvable_paths_are_resolution_faults(self, over: str, message: str) -> None:
        fanout = FanOut.model_validate({"over": over, "as": "lead", "max_items": 5})
        with pytest.raises(FanOutResolutionError, match=message):
            resolve_fanout_items(fanout, search_result(leads(2)))

    def test_resolution_faults_are_planning_faults(self) -> None:
        """§4.4: retrying the same expansion cannot succeed, so the class is
        `reference_resolution`, which `recover` and `decide` route to replan."""
        assert issubclass(FanOutResolutionError, ReferenceResolutionError)
        err = FanOutResolutionError("x", detail={"path": "s1.output.leads"})
        assert err.error_class is ErrorClass.REFERENCE_RESOLUTION
        assert err.detail == {"path": "s1.output.leads"}
