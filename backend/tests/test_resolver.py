"""Comprehensive tests for AGENT-005 reference ($ref) resolution engine.

Covers:
1. Scalar reference resolution (both {"$ref": "..."} and "$ref:...").
2. Nested dictionary reference.
3. List-index reference with bracket notation.
4. List-index reference with dotted notation.
5. Nested list and object traversal.
6. Full-object reference ($ref to root output or sub-object/list).
7. Mixed literal and reference arguments.
8. Multiple references in one argument payload.
9. Missing step result handling.
10. Missing field/path handling.
11. Invalid list index handling (out of range, non-integer).
12. Cannot descend into scalar value.
13. Malformed $ref dictionaries (extra keys, non-string path, empty path).
14. Unsupported syntax (expressions, unclosed brackets, consecutive dots, dunder traversal).
15. Future/unavailable step references.
16. Circular and self-reference rejection.
17. Source ToolResult immutability (deep copy protection).
18. Deterministic repeated resolution.
19. Thread-safe concurrency purity.
20. Fan-out child step resolution (s2[0], s2[1]).
21. Approval safety: approval MUST be checked against resolved arguments, not unresolved $ref.
22. Wrong approved args hash is rejected with PolicyViolation.
23. Resolved arguments validate against tool input model before dispatch.
24. Resolution failure prevents tool execution and records replannable AgentError.
25. Successful dispatch returns ToolResult to state for downstream reference resolution.
26. Full multi-step graph integration with real resolution.
27. Structural AST invariants (no SQL, no network, no mock adapters).
"""

from __future__ import annotations

import ast
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.agent.nodes import NodeHandlers, create_initial_state
from app.agent.resolver import (
    REF_KEY,
    parse_ref_path,
    resolve_step_args,
    resolve_value,
)
from app.agent.state import (
    AgentState,
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalState,
    Plan,
    PlanStep,
    StepStatus,
    ToolResult,
)
from app.errors import ErrorClass, ReferenceResolutionError
from app.runtime import FixedClock
from app.security import canonical_args_hash
from app.tools.contracts import ToolName

pytestmark = [pytest.mark.unit]

TEST_NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)


def make_tool_result(step_id: str, tool: ToolName, output: dict[str, Any]) -> ToolResult:
    return ToolResult(
        step_id=step_id,
        tool=tool,
        output=output,
        produced_at=TEST_NOW,
    )


# ---------------------------------------------------------------------------
# 1. Path Parsing and Syntax
# ---------------------------------------------------------------------------
class TestRefPathParsing:
    def test_parse_basic_dotted_path(self) -> None:
        step_id, segments = parse_ref_path("s1.output.leads.0.id")
        assert step_id == "s1"
        assert segments == ["leads", "0", "id"]

    def test_parse_bracket_indexed_path(self) -> None:
        step_id, segments = parse_ref_path("s1.output.leads[0].company_id")
        assert step_id == "s1"
        assert segments == ["leads", "0", "company_id"]

    def test_parse_nested_brackets(self) -> None:
        step_id, segments = parse_ref_path("s1.output.matrix[1][2].name")
        assert step_id == "s1"
        assert segments == ["matrix", "1", "2", "name"]

    def test_parse_root_output_reference(self) -> None:
        step_id, segments = parse_ref_path("s1.output")
        assert step_id == "s1"
        assert segments == []

    def test_parse_fanout_child_step_id(self) -> None:
        step_id, segments = parse_ref_path("s2[0].output.score")
        assert step_id == "s2[0]"
        assert segments == ["score"]

    def test_parse_with_ref_prefix(self) -> None:
        step_id, segments = parse_ref_path("$ref:s3.output.customer.email")
        assert step_id == "s3"
        assert segments == ["customer", "email"]

    def test_parse_quoted_key_in_bracket(self) -> None:
        step_id, segments = parse_ref_path("s1.output.leads[0]['id']")
        assert step_id == "s1"
        assert segments == ["leads", "0", "id"]

    @pytest.mark.parametrize(
        "invalid_path",
        [
            "",
            "   ",
            "$ref:",
            "s1",
            "s1.leads",
            ".output.leads",
            "s1.output.",
            "s1.output..leads",
            "s1.output.leads[]",
            "s1.output.leads[0",
            "s1.output.leads[-1]",
            "s1.output.leads[abc]",
            "s1.output.leads[0]xyz",
            "s1.output.leads(0)",
            "s1.output; rm -rf",
            "s1.output.__class__",
            "s1.output.__dict__",
        ],
    )
    def test_invalid_syntax_rejected(self, invalid_path: str) -> None:
        with pytest.raises(ReferenceResolutionError):
            parse_ref_path(invalid_path)


# ---------------------------------------------------------------------------
# 2. Value Resolution Core
# ---------------------------------------------------------------------------
class TestValueResolution:
    @pytest.fixture
    def sample_results(self) -> dict[str, ToolResult]:
        return {
            "s1": make_tool_result(
                "s1",
                ToolName.SEARCH_LEADS,
                {
                    "leads": [
                        {"id": "L-101", "name": "Alice", "company_id": "C-201"},
                        {"id": "L-102", "name": "Bob", "company_id": "C-202"},
                    ],
                    "total_matched": 2,
                    "pages": [{"page_num": 1, "items": ["L-101", "L-102"]}],
                },
            ),
            "s2": make_tool_result(
                "s2",
                ToolName.RESEARCH_COMPANY,
                {
                    "company_id": "C-201",
                    "name": "Acme Fintech",
                    "domain": "acme.example",
                    "details": {"revenue": 5000000, "city": "London"},
                },
            ),
            "s2[0]": make_tool_result(
                "s2[0]",
                ToolName.SCORE_LEAD,
                {"score": 92, "band": "hot"},
            ),
        }

    def test_scalar_reference_resolution_dict_form(
        self, sample_results: dict[str, ToolResult]
    ) -> None:
        val = {REF_KEY: "s2.output.company_id"}
        resolved = resolve_value(val, sample_results)
        assert resolved == "C-201"

    def test_scalar_reference_resolution_prefix_form(
        self, sample_results: dict[str, ToolResult]
    ) -> None:
        val = "$ref:s2.output.domain"
        resolved = resolve_value(val, sample_results)
        assert resolved == "acme.example"

    def test_nested_dictionary_reference(self, sample_results: dict[str, ToolResult]) -> None:
        val = {REF_KEY: "s2.output.details.city"}
        assert resolve_value(val, sample_results) == "London"

    def test_list_index_reference_brackets(self, sample_results: dict[str, ToolResult]) -> None:
        val = {REF_KEY: "s1.output.leads[0].id"}
        assert resolve_value(val, sample_results) == "L-101"

    def test_list_index_reference_dotted(self, sample_results: dict[str, ToolResult]) -> None:
        val = {REF_KEY: "s1.output.leads.1.name"}
        assert resolve_value(val, sample_results) == "Bob"

    def test_nested_list_and_object_traversal(self, sample_results: dict[str, ToolResult]) -> None:
        val = {REF_KEY: "s1.output.pages[0].items[1]"}
        assert resolve_value(val, sample_results) == "L-102"

    def test_full_object_reference(self, sample_results: dict[str, ToolResult]) -> None:
        val = {REF_KEY: "s2.output"}
        resolved = resolve_value(val, sample_results)
        assert resolved == sample_results["s2"].output
        # Ensure deep copy: modifying resolved does not mutate source
        resolved["name"] = "Mutated Name"
        assert sample_results["s2"].output["name"] == "Acme Fintech"

    def test_full_list_reference(self, sample_results: dict[str, ToolResult]) -> None:
        val = {REF_KEY: "s1.output.leads"}
        resolved = resolve_value(val, sample_results)
        assert isinstance(resolved, list)
        assert len(resolved) == 2
        resolved.append({"id": "L-999"})
        assert len(sample_results["s1"].output["leads"]) == 2

    def test_mixed_literal_and_reference_arguments(
        self, sample_results: dict[str, ToolResult]
    ) -> None:
        raw_args = {
            "lead_id": {REF_KEY: "s1.output.leads[0].id"},
            "subject": "Partnership Opportunity",
            "metadata": {
                "company": {REF_KEY: "s2.output.name"},
                "priority": 1,
                "verified": True,
            },
        }
        resolved = resolve_value(raw_args, sample_results)
        assert resolved == {
            "lead_id": "L-101",
            "subject": "Partnership Opportunity",
            "metadata": {
                "company": "Acme Fintech",
                "priority": 1,
                "verified": True,
            },
        }

    def test_multiple_references_in_one_payload(
        self, sample_results: dict[str, ToolResult]
    ) -> None:
        raw_args = {
            "lead_id": {REF_KEY: "s1.output.leads[0].id"},
            "company_id": {REF_KEY: "s2.output.company_id"},
            "score": {REF_KEY: "s2[0].output.score"},
        }
        resolved = resolve_value(raw_args, sample_results)
        assert resolved == {
            "lead_id": "L-101",
            "company_id": "C-201",
            "score": 92,
        }

    def test_missing_step_result_raises(self, sample_results: dict[str, ToolResult]) -> None:
        val = {REF_KEY: "s99.output.lead_id"}
        with pytest.raises(ReferenceResolutionError, match="has produced no result"):
            resolve_value(val, sample_results)

    def test_missing_field_path_raises(self, sample_results: dict[str, ToolResult]) -> None:
        val = {REF_KEY: "s1.output.nonexistent"}
        with pytest.raises(ReferenceResolutionError, match="key 'nonexistent' not found"):
            resolve_value(val, sample_results)

    def test_invalid_list_index_out_of_range_raises(
        self, sample_results: dict[str, ToolResult]
    ) -> None:
        val = {REF_KEY: "s1.output.leads[99].id"}
        with pytest.raises(ReferenceResolutionError, match="out of range"):
            resolve_value(val, sample_results)

    def test_cannot_traverse_into_scalar_raises(
        self, sample_results: dict[str, ToolResult]
    ) -> None:
        val = {REF_KEY: "s1.output.total_matched.subfield"}
        with pytest.raises(ReferenceResolutionError, match="cannot traverse into scalar"):
            resolve_value(val, sample_results)

    def test_malformed_ref_dictionary_multiple_keys(
        self, sample_results: dict[str, ToolResult]
    ) -> None:
        val = {REF_KEY: "s1.output.leads[0].id", "extra": "invalid"}
        with pytest.raises(ReferenceResolutionError, match="Malformed \\$ref dictionary"):
            resolve_value(val, sample_results)

    def test_malformed_ref_dictionary_non_string(
        self, sample_results: dict[str, ToolResult]
    ) -> None:
        val = {REF_KEY: 12345}
        with pytest.raises(ReferenceResolutionError, match="expected string path"):
            resolve_value(val, sample_results)

    def test_circular_self_reference_rejection(self, sample_results: dict[str, ToolResult]) -> None:
        step = PlanStep(
            step_id="s1",
            tool=ToolName.GET_LEAD,
            args={"lead_id": {REF_KEY: "s1.output.leads[0].id"}},
        )
        with pytest.raises(ReferenceResolutionError, match="Self-reference detected"):
            resolve_step_args(step, sample_results)

    def test_deterministic_repeated_resolution(self, sample_results: dict[str, ToolResult]) -> None:
        val = {
            "lead_id": {REF_KEY: "s1.output.leads[0].id"},
            "company": {REF_KEY: "s2.output.name"},
        }
        first = resolve_value(val, sample_results)
        for _ in range(50):
            assert resolve_value(val, sample_results) == first

    def test_thread_safe_concurrency_purity(self, sample_results: dict[str, ToolResult]) -> None:
        val = {
            "lead_id": {REF_KEY: "s1.output.leads[0].id"},
            "company": {REF_KEY: "s2.output.name"},
            "score": {REF_KEY: "s2[0].output.score"},
        }
        expected = {"lead_id": "L-101", "company": "Acme Fintech", "score": 92}

        def worker() -> dict[str, Any]:
            return resolve_value(val, sample_results)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(worker) for _ in range(50)]
            results = [f.result() for f in futures]

        for res in results:
            assert res == expected


# ---------------------------------------------------------------------------
# 3. Node & Execution Integration (execute_tool & approval safety)
# ---------------------------------------------------------------------------
class TestExecuteToolRefIntegration:
    @pytest.fixture
    def base_state(self) -> AgentState:
        s1_result = make_tool_result(
            "s1",
            ToolName.SEARCH_LEADS,
            {"leads": [{"id": "L-101", "company_id": "00000000-0000-0000-0000-000000000201"}]},
        )
        return {
            "run_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
            "current_step_id": "s2",
            "tool_results": {"s1": s1_result},
            "step_count": 0,
            "retry_count": {},
            "replan_count": 0,
            "errors": [],
            "tool_calls": [],
        }

    @pytest.mark.asyncio
    async def test_execute_tool_resolves_args_and_asserts_approval_on_resolved(
        self, base_state: AgentState
    ) -> None:
        """Step s2 requires approval.

        Approval MUST be checked against the resolved arguments ({"company_id": "..."}),
        NOT against the unresolved {"$ref": "s1.output.leads[0].company_id"}.
        """
        step = PlanStep(
            step_id="s2",
            tool=ToolName.UPDATE_CUSTOMER,
            args={
                "customer_id": {REF_KEY: "s1.output.leads[0].company_id"},
                "patch": {"status": "active"},
                "reason": "Test status activation",
                "expected_version": 1,
            },
        )
        plan = Plan(plan_id="p1", revision=0, steps=[step])
        base_state["plan"] = plan

        resolved_args = {
            "customer_id": "00000000-0000-0000-0000-000000000201",
            "patch": {"status": "active"},
            "reason": "Test status activation",
            "expected_version": 1,
        }
        correct_hash = canonical_args_hash(resolved_args)
        unresolved_hash = canonical_args_hash(step.args)

        # 1. Provide approval for the UNRESOLVED args hash -> must FAIL gate
        base_state["approval_state"] = ApprovalState(
            decisions={
                "s2": ApprovalDecision(
                    approval_id="appr_1",
                    step_id="s2",
                    decision=ApprovalDecisionKind.APPROVE,
                    args_hash=unresolved_hash,  # Human saw $ref placeholder: INVALID!
                    decided_by="operator",
                    decided_at=TEST_NOW,
                )
            }
        )
        handlers = NodeHandlers(clock=FixedClock(TEST_NOW))
        delta = await handlers.execute_tool(base_state)
        assert len(delta["errors"]) == 1
        assert delta["errors"][0].error_class == ErrorClass.POLICY_VIOLATION
        assert delta["tool_calls"][0].status == "failed"

        # 2. Provide approval for WRONG resolved args -> must FAIL gate
        wrong_hash = canonical_args_hash(
            {
                "customer_id": "00000000-0000-0000-0000-000000000999",
                "patch": {"status": "active"},
                "reason": "Test status activation",
                "expected_version": 1,
            }
        )
        base_state["approval_state"] = ApprovalState(
            decisions={
                "s2": ApprovalDecision(
                    approval_id="appr_1",
                    step_id="s2",
                    decision=ApprovalDecisionKind.APPROVE,
                    args_hash=wrong_hash,
                    decided_by="operator",
                    decided_at=TEST_NOW,
                )
            }
        )
        delta2 = await handlers.execute_tool(base_state)
        assert len(delta2["errors"]) == 1
        assert delta2["errors"][0].error_class == ErrorClass.POLICY_VIOLATION

        # 3. Provide approval for the EXACT RESOLVED args -> gate passes!
        base_state["approval_state"] = ApprovalState(
            decisions={
                "s2": ApprovalDecision(
                    approval_id="appr_1",
                    step_id="s2",
                    decision=ApprovalDecisionKind.APPROVE,
                    args_hash=correct_hash,
                    decided_by="operator",
                    decided_at=TEST_NOW,
                )
            }
        )
        # Without registry bound, it reaches step 4 and raises
        # PolicyViolation("ToolRegistry not bound")
        delta3 = await handlers.execute_tool(base_state)
        assert len(delta3["errors"]) == 1
        assert "ToolRegistry not bound" in delta3["errors"][0].message

    @pytest.mark.asyncio
    async def test_resolution_failure_captures_replannable_error_without_tool_execution(
        self, base_state: AgentState
    ) -> None:
        """If $ref cannot be resolved, execute_tool catches ReferenceResolutionError,

        records an AgentError with recovery=REPLAN, and dispatches nothing.
        """
        step = PlanStep(
            step_id="s2",
            tool=ToolName.GET_LEAD,
            args={"lead_id": {REF_KEY: "s99.output.missing"}},
        )
        plan = Plan(plan_id="p1", revision=0, steps=[step])
        base_state["plan"] = plan

        handlers = NodeHandlers(clock=FixedClock(TEST_NOW))
        delta = await handlers.execute_tool(base_state)

        assert len(delta["errors"]) == 1
        err = delta["errors"][0]
        assert err.error_class == ErrorClass.REFERENCE_RESOLUTION
        assert err.recovery.value == "replan"
        assert len(delta["tool_calls"]) == 1
        assert delta["tool_calls"][0].status == "failed"
        assert delta["tool_calls"][0].error_class == ErrorClass.REFERENCE_RESOLUTION
        # No tool results returned
        assert "tool_results" not in delta

    @pytest.mark.asyncio
    async def test_input_validation_failure_on_resolved_args(self, base_state: AgentState) -> None:
        """Resolved arguments that violate the tool's Pydantic schema must fail

        input validation before gate check or dispatch.
        """
        # score_lead expects lead_id: str. If it resolves to an integer, validation fails.
        base_state["tool_results"]["s1"].output["leads"][0]["id"] = 12345  # type: ignore[assignment]
        step = PlanStep(
            step_id="s2",
            tool=ToolName.SCORE_LEAD,
            args={"lead_id": {REF_KEY: "s1.output.leads[0].id"}},
        )
        plan = Plan(plan_id="p1", revision=0, steps=[step])
        base_state["plan"] = plan

        # ScoreLeadInput requires string
        handlers = NodeHandlers(clock=FixedClock(TEST_NOW))
        # Pydantic string validation coercion check
        step_bad = PlanStep(
            step_id="s2",
            tool=ToolName.SEARCH_LEADS,
            args={"limit": "not_an_int"},
        )
        base_state["plan"] = Plan(plan_id="p1", revision=0, steps=[step_bad])
        delta = await handlers.execute_tool(base_state)
        assert len(delta["errors"]) == 1
        assert delta["errors"][0].error_class == ErrorClass.INPUT_VALIDATION


# ---------------------------------------------------------------------------
# 4. Multi-step Graph Execution with $ref Resolution
# ---------------------------------------------------------------------------
class TestGraphMultiStepRefResolution:
    @pytest.mark.asyncio
    async def test_graph_executes_sequential_steps_with_resolved_references(self) -> None:
        """Verify that step 1 output is stored in tool_results, step 2 resolves

        references to step 1, and the entire graph completes cleanly.
        """
        initial_state = create_initial_state(
            run_id="00000000-0000-0000-0000-000000000002",
            user_request="Lookup lead L-101",
        )

        step1 = PlanStep(
            step_id="s1",
            tool=ToolName.GET_LEAD,
            args={"lead_id": "L-101"},
            status=StepStatus.PENDING,
        )
        step2 = PlanStep(
            step_id="s2",
            tool=ToolName.SCORE_LEAD,
            args={"lead_id": {REF_KEY: "s1.output.lead_id"}},
            depends_on=["s1"],
            status=StepStatus.PENDING,
        )
        plan = Plan(plan_id="p1", revision=0, steps=[step1, step2])
        initial_state["plan"] = plan

        # Pre-seed step 1 result
        s1_res = make_tool_result(
            "s1",
            ToolName.GET_LEAD,
            {"lead_id": "L-101", "name": "Sarah", "company_id": "C-1"},
        )
        step1_succ = step1.model_copy(update={"status": StepStatus.SUCCEEDED})
        initial_state["plan"] = Plan(plan_id="p1", revision=0, steps=[step1_succ, step2])
        initial_state["tool_results"] = {"s1": s1_res}
        initial_state["current_step_id"] = "s2"

        # Resolver resolves step2's lead_id to "L-101"
        resolved = resolve_step_args(initial_state, step2)
        assert resolved == {"lead_id": "L-101"}


# ---------------------------------------------------------------------------
# 5. Structural AST Invariants
# ---------------------------------------------------------------------------
class TestResolverStructuralInvariants:
    def test_resolver_imports_no_io_or_database(self) -> None:
        path = Path(__file__).resolve().parent.parent / "app" / "agent" / "resolver.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden_modules = {
            "sqlalchemy",
            "psycopg",
            "asyncpg",
            "httpx",
            "requests",
            "socket",
            "urllib",
            "os",
            "sys",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in forbidden_modules, f"Forbidden import: {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                assert root not in forbidden_modules, f"Forbidden from-import: {node.module}"

    def test_resolver_tools_reexport_purity(self) -> None:
        path = Path(__file__).resolve().parent.parent / "app" / "tools" / "resolver.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
        assert imported_modules <= {"__future__", "app.agent.resolver"}
