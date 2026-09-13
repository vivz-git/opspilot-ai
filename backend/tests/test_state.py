"""Agent state: reducers and the approval grant (docs/architecture.md §5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.agent.state import (
    TERMINAL_RUN_STATUSES,
    AgentError,
    AgentState,
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalRequest,
    ApprovalState,
    ApprovalStatus,
    Plan,
    PlanStep,
    RunStatus,
    ToolCall,
    ToolResult,
    VerificationResult,
    VerificationStatus,
    append_list,
    merge_approval_state,
    merge_dict,
)
from app.errors import ErrorClass
from app.security import canonical_args_hash
from app.tools.contracts import RiskLevel, ToolName

pytestmark = [pytest.mark.unit]

NOW = datetime(2026, 9, 12, 18, 0, tzinfo=UTC)
SEND_ARGS = {"draft_id": "d_1", "to_email": "dana@northwind.example"}


def _request(step_id: str = "s6", args: dict | None = None) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id="a_1",
        run_id="r_1",
        step_id=step_id,
        tool=ToolName.SEND_EMAIL_MOCK,
        risk=RiskLevel.HIGH,
        title="Send outreach email to dana@northwind.example",
        summary="Sends saved draft d_1",
        payload_preview=args or SEND_ARGS,
        args_hash=canonical_args_hash(args or SEND_ARGS),
        requested_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )


def _decision(
    kind: ApprovalDecisionKind = ApprovalDecisionKind.APPROVE,
    step_id: str = "s6",
    args: dict | None = None,
) -> ApprovalDecision:
    return ApprovalDecision(
        approval_id="a_1",
        step_id=step_id,
        decision=kind,
        args_hash=canonical_args_hash(args or SEND_ARGS),
        decided_by="operator@example.com",
        decided_at=NOW,
        reason=None if kind is ApprovalDecisionKind.APPROVE else "Wrong segment.",
    )


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------
class TestReducers:
    def test_append_accumulates_and_never_rewrites(self) -> None:
        first = [ToolCall(step_id="s1", tool=ToolName.SEARCH_LEADS, attempt=1, args_hash="h")]
        second = [ToolCall(step_id="s1", tool=ToolName.SEARCH_LEADS, attempt=2, args_hash="h")]
        merged = append_list(first, second)
        assert [c.attempt for c in merged] == [1, 2]

    def test_append_handles_empty_and_none(self) -> None:
        assert append_list(None, None) == []
        assert append_list(None, [1]) == [1]
        assert append_list([1], None) == [1]

    def test_append_is_what_makes_a_resumed_node_additive(self) -> None:
        """A re-executed node re-emitting its error must not erase history."""
        err = AgentError(step_id="s2", error_class=ErrorClass.TRANSIENT, message="timeout")
        assert len(append_list([err], [err])) == 2

    def test_merge_preserves_sibling_keys(self) -> None:
        """A node updating s3 must not drop s1's artifact."""
        current = {
            "s1": ToolResult(step_id="s1", tool=ToolName.GET_LEAD, output={}, produced_at=NOW)
        }
        incoming = {
            "s3": ToolResult(step_id="s3", tool=ToolName.SCORE_LEAD, output={}, produced_at=NOW)
        }
        assert set(merge_dict(current, incoming)) == {"s1", "s3"}

    def test_merge_is_per_step_for_retry_counters(self) -> None:
        assert merge_dict({"s1": 2}, {"s2": 1}) == {"s1": 2, "s2": 1}
        assert merge_dict({"s1": 1}, {"s1": 2}) == {"s1": 2}

    def test_merge_keeps_verification_results_per_step(self) -> None:
        a = {
            "s5": VerificationResult(
                step_id="s5", status=VerificationStatus.PASSED, mode="readback"
            )
        }
        b = {
            "s6": VerificationResult(
                step_id="s6", status=VerificationStatus.FAILED, mode="readback"
            )
        }
        merged = merge_dict(a, b)
        assert merged["s5"].status is VerificationStatus.PASSED
        assert merged["s6"].status is VerificationStatus.FAILED


class TestApprovalStateMerge:
    """§9.7 — request_approval runs at least twice, so the merge is the
    idempotency guard."""

    def test_a_decided_approval_is_never_regressed_to_pending(self) -> None:
        decided = ApprovalState(decisions={"s6": _decision()})
        re_executed = ApprovalState(pending=_request())
        merged = merge_approval_state(decided, re_executed)
        assert merged.pending is None, "a re-executed node re-opened a decided approval"
        assert merged.decisions["s6"].decision is ApprovalDecisionKind.APPROVE

    def test_decisions_accumulate_across_steps(self) -> None:
        first = ApprovalState(decisions={"s6": _decision()})
        second = ApprovalState(decisions={"s9": _decision(step_id="s9")})
        assert set(merge_approval_state(first, second).decisions) == {"s6", "s9"}

    def test_pending_survives_until_it_is_decided(self) -> None:
        merged = merge_approval_state(ApprovalState(), ApprovalState(pending=_request()))
        assert merged.pending is not None
        decided = merge_approval_state(merged, ApprovalState(decisions={"s6": _decision()}))
        assert decided.pending is None

    def test_merge_tolerates_none_on_either_side(self) -> None:
        assert merge_approval_state(None, None).pending is None
        assert merge_approval_state(ApprovalState(pending=_request()), None).pending is not None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
class TestApprovalGrant:
    def test_no_decision_means_no_grant(self) -> None:
        assert ApprovalState(pending=_request()).grants("s6", SEND_ARGS) is False

    def test_approval_grants_the_exact_arguments(self) -> None:
        state = ApprovalState(decisions={"s6": _decision()})
        assert state.grants("s6", SEND_ARGS) is True

    def test_approval_does_not_grant_changed_arguments(self) -> None:
        """The time-of-check/time-of-use gap: approving draft d_1 must not
        authorise sending d_2 (§9.4)."""
        state = ApprovalState(decisions={"s6": _decision()})
        assert state.grants("s6", {**SEND_ARGS, "draft_id": "d_2"}) is False
        assert state.grants("s6", {**SEND_ARGS, "to_email": "ceo@acme.example"}) is False

    def test_grant_is_unaffected_by_volatile_arguments(self) -> None:
        """A retry changes the idempotency key but not the operation, so it
        must not invalidate the human's grant."""
        state = ApprovalState(decisions={"s6": _decision()})
        assert state.grants("s6", {**SEND_ARGS, "idempotency_key": "k_attempt_2"}) is True

    def test_a_grant_for_one_step_does_not_authorise_another(self) -> None:
        state = ApprovalState(decisions={"s6": _decision()})
        assert state.grants("s9", SEND_ARGS) is False

    def test_rejection_is_not_a_grant(self) -> None:
        state = ApprovalState(decisions={"s6": _decision(ApprovalDecisionKind.REJECT)})
        assert state.grants("s6", SEND_ARGS) is False
        assert state.rejected("s6") is True


# ---------------------------------------------------------------------------
# Lifecycle and plan
# ---------------------------------------------------------------------------
class TestLifecycle:
    def test_rejected_is_terminal_but_distinct_from_failed(self) -> None:
        """Folding rejection into failure would corrupt task success rate and
        would tell the operator the system broke when it worked (§5.4)."""
        assert RunStatus.REJECTED in TERMINAL_RUN_STATUSES
        assert RunStatus.REJECTED is not RunStatus.FAILED

    def test_non_terminal_statuses_are_not_terminal(self) -> None:
        for status in (
            RunStatus.CREATED,
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.AWAITING_APPROVAL,
        ):
            assert status not in TERMINAL_RUN_STATUSES

    def test_all_approval_statuses_are_representable(self) -> None:
        assert {s.value for s in ApprovalStatus} == {
            "pending",
            "approved",
            "rejected",
            "expired",
            "superseded",
            "cancelled",
        }


class TestPlan:
    def test_steps_are_addressable_by_stable_id(self) -> None:
        plan = Plan(
            plan_id="p_1",
            steps=[
                PlanStep(step_id="s1", tool=ToolName.SEARCH_LEADS, args={"industry": "fintech"}),
                PlanStep(step_id="s2", tool=ToolName.RESEARCH_COMPANY, depends_on=["s1"]),
            ],
        )
        assert plan.step("s2").tool is ToolName.RESEARCH_COMPANY
        assert plan.step("nope") is None

    def test_plan_round_trips_through_json(self) -> None:
        """The plan is persisted as JSONB and returned by the API."""
        plan = Plan(plan_id="p_1", steps=[PlanStep(step_id="s1", tool=ToolName.GET_LEAD)])
        assert Plan.model_validate_json(plan.model_dump_json()) == plan

    def test_fanout_requires_a_cap(self) -> None:
        from app.agent.state import FanOut
        from pydantic import ValidationError

        FanOut(over="s1.output.leads", **{"as": "lead"}, max_items=3)
        with pytest.raises(ValidationError):
            FanOut(over="s1.output.leads", **{"as": "lead"}, max_items=0)


def test_state_declares_every_documented_channel() -> None:
    expected = {
        "run_id",
        "user_request",
        "normalized_task",
        "plan",
        "plan_history",
        "current_step_id",
        "tool_calls",
        "tool_results",
        "approval_state",
        "errors",
        "retry_count",
        "replan_count",
        "step_count",
        "verification_result",
        "final_response",
        "status",
        "status_reason",
        "created_at",
        "updated_at",
        "deadline_at",
        "metadata",
    }
    assert set(AgentState.__annotations__) == expected
