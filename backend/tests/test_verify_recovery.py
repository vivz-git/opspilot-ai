"""VERIFY-003 — verification failure recovery, retry, replan and terminal semantics.

The control loop this module closes:

    execute_tool → verify → passed      → decide
                         → failed       → recover → retry execute_tool / skip / fail
                         → unconfirmed  → recover → retry VERIFY → verify

The safety property under test, stated once:

    A mutating tool that SUCCEEDED and whose verifier came back UNCONFIRMED
    is re-verified, never re-executed. `unconfirmed` means "we cannot tell",
    and you do not answer "did the email send?" by sending another email.

Every test that touches a mutation counts the mutation. `CountingExecutor`
stands in for `execute_tool` precisely so that "how many times did the effect
happen?" is an assertion and not a reading of the code.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import textwrap
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app.agent.graph import create_agent_graph
from app.agent.nodes import NodeHandlers, create_initial_state
from app.agent.normalizer import CanonicalIntent
from app.agent.state import (
    AgentError,
    AgentState,
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalState,
    Budgets,
    NormalizedTask,
    Plan,
    PlanStep,
    RunMetadata,
    RunStatus,
    StepStatus,
    ToolCall,
    ToolResult,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
)
from app.agent.verification_recovery import (
    PROVEN_ABSENCE_CHECKS,
    RetryTarget,
    VerificationSafety,
    classify_verification_failure,
    is_safe_to_retry_mutation,
    is_verification_error,
    retry_target_for,
    tool_effect_succeeded,
    verify_retry_key,
)
from app.errors import ErrorClass, NotFoundError, TransientToolError
from app.integrations.ports import (
    Adapters,
    Customer,
    DraftRecord,
    OutboxRecord,
)
from app.persistence.models import TraceEventKind, TraceEventSeverity
from app.runtime import FixedClock
from app.security import canonical_args_hash
from app.tools.contracts import REGISTRY, ToolName, VerificationMode
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import NodeCancelledError

pytestmark = [pytest.mark.unit]

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
APP = Path(__file__).resolve().parents[1] / "app"


# ---------------------------------------------------------------------------
# Scripted ports: each read-back answer is chosen by the test, in order.
# ---------------------------------------------------------------------------
class _Scripted:
    """Replays a scripted sequence of read-back answers and counts reads.

    An entry that is an exception is raised (the port is unreachable →
    `unconfirmed`); anything else is returned.
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.reads = 0

    def _next(self) -> Any:
        self.reads += 1
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, BaseException):
            raise item
        return item


class ScriptedDraftPort(_Scripted):
    async def save(self, draft: Any) -> DraftRecord:  # pragma: no cover - never called
        raise AssertionError("a verifier must never write")

    async def get(self, draft_id: str) -> DraftRecord:
        return self._next()  # type: ignore[no-any-return]


class ScriptedMailPort(_Scripted):
    def __init__(self, script: list[Any], *, outbox_count: int = 1) -> None:
        super().__init__(script)
        self.outbox_count = outbox_count

    async def send(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("a verifier must never send")

    async def get_outbox(self, message_id: str) -> OutboxRecord:
        return self._next()  # type: ignore[no-any-return]

    async def count_outbox(self, idempotency_key: str) -> int:
        return self.outbox_count


class ScriptedCustomerPort(_Scripted):
    async def update(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("a verifier must never write")

    async def get(self, customer_id: str) -> Customer:
        return self._next()  # type: ignore[no-any-return]


class _Unused:
    def __getattr__(self, name: str) -> Any:  # pragma: no cover - defensive
        raise AssertionError(f"port {name} must not be touched by this test")


def adapters_with(
    *,
    drafts: Any = None,
    mail: Any = None,
    customers: Any = None,
) -> Adapters:
    return Adapters(
        leads=_Unused(),  # type: ignore[arg-type]
        companies=_Unused(),  # type: ignore[arg-type]
        customers=customers or _Unused(),  # type: ignore[arg-type]
        drafts=drafts or _Unused(),  # type: ignore[arg-type]
        mail=mail or _Unused(),  # type: ignore[arg-type]
        content=_Unused(),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------
SUBJECT = "Quick question about your rollout"
BODY = "Hi Ada, noticed your team shipped a new billing flow."
CONTENT_HASH = hashlib.sha256(f"{SUBJECT}\n\n{BODY}".encode()).hexdigest()

SAVE_DRAFT_ARGS: dict[str, Any] = {
    "lead_id": "l_1",
    "subject": SUBJECT,
    "body": BODY,
    "channel": "email",
    "content_hash": CONTENT_HASH,
}
SEND_EMAIL_ARGS: dict[str, Any] = {"draft_id": "drf_1", "to_email": "ada@example.com"}
UPDATE_CUSTOMER_ARGS: dict[str, Any] = {
    "customer_id": "cus_1",
    "expected_version": 1,
    "patch": {"plan": "enterprise"},
    "reason": "Signed the annual enterprise contract",
}


def good_draft(draft_id: str = "drf_1") -> DraftRecord:
    return DraftRecord(
        draft_id=draft_id,
        lead_id="l_1",
        subject=SUBJECT,
        body=BODY,
        channel="email",
        status="saved",
        version=1,
        content_hash=CONTENT_HASH,
        saved_at=NOW,
    )


def good_outbox(*, to_email: str = "ada@example.com") -> OutboxRecord:
    return OutboxRecord(
        outbox_id="obx_1",
        message_id="msg_1",
        draft_id="drf_1",
        to_email=to_email,
        subject=SUBJECT,
        body=BODY,
        status="sent",
        provider="mock",
        idempotency_key="idem_1",
        sent_at=NOW,
    )


def good_customer(*, account_name: str = "Acme Corp", plan: str = "enterprise") -> Customer:
    return Customer(
        customer_id="cus_1",
        account_name=account_name,
        primary_contact="Ada Lovelace",
        email="ada@example.com",
        status="active",
        plan=plan,
        version=2,
        updated_at=NOW,
    )


BASELINE_CUSTOMER_OUTPUT: dict[str, Any] = {
    "customer": {
        "customer_id": "cus_1",
        "account_name": "Acme Corp",
        "primary_contact": "Ada Lovelace",
        "email": "ada@example.com",
        "status": "active",
        "plan": "starter",
        "version": 1,
        "updated_at": NOW.isoformat(),
    }
}


# ---------------------------------------------------------------------------
# State builders
# ---------------------------------------------------------------------------
def make_plan(
    step_id: str,
    tool: ToolName,
    args: dict[str, Any],
    *,
    optional: bool = False,
    status: StepStatus = StepStatus.RUNNING,
    extra: list[PlanStep] | None = None,
) -> Plan:
    steps = list(extra or [])
    steps.append(PlanStep(step_id=step_id, tool=tool, args=args, optional=optional, status=status))
    return Plan(plan_id="p_verify", steps=steps)


def executed_state(
    *,
    step_id: str = "s1",
    tool: ToolName = ToolName.SAVE_DRAFT,
    args: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    plan: Plan | None = None,
    attempt: int = 1,
    retry_count: dict[str, int] | None = None,
    max_retries: int = 2,
    tool_status: str = "succeeded",
    errors: list[AgentError] | None = None,
    verification: VerificationResult | None = None,
    prior_results: dict[str, ToolResult] | None = None,
) -> AgentState:
    """State as it stands the instant `execute_tool` has returned."""
    step_args = SAVE_DRAFT_ARGS if args is None else args
    results: dict[str, ToolResult] = dict(prior_results or {})
    results[step_id] = ToolResult(
        step_id=step_id,
        tool=tool,
        output=output if output is not None else {"draft_id": "drf_1"},
        produced_at=NOW,
    )
    state: AgentState = {
        "run_id": str(uuid.uuid4()),
        "current_step_id": step_id,
        "plan": plan or make_plan(step_id, tool, step_args),
        "tool_results": results,
        "tool_calls": [
            ToolCall(
                step_id=step_id,
                tool=tool,
                attempt=attempt,
                args_hash="h_1",
                idempotency_key="idem_1",
                status=tool_status,
            )
        ],
        "errors": list(errors or []),
        "retry_count": dict(retry_count or {}),
        "replan_count": 0,
        "step_count": 1,
        "verification_result": {step_id: verification} if verification else {},
        "metadata": RunMetadata(budgets=Budgets(max_retries=max_retries)),
        "deadline_at": NOW + timedelta(minutes=5),
        "status": RunStatus.RUNNING,
    }
    return state


# ---------------------------------------------------------------------------
# A trace-collecting unit of work
# ---------------------------------------------------------------------------
class FakeTraceEvents:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append(self, **kwargs: Any) -> None:
        self.events.append(kwargs)

    def of_kind(self, kind: TraceEventKind) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("kind") == kind]


class FakeExecutionSteps:
    def __init__(self) -> None:
        self.verifications: list[tuple[VerificationStatus, dict[str, Any] | None]] = []

    async def get_by_step_id(self, *a: Any, **k: Any) -> None:
        return None

    async def record_verification(self, *a: Any, **k: Any) -> None:  # pragma: no cover
        self.verifications.append((k["verification_status"], k.get("verification")))


class FakeUnitOfWork:
    def __init__(self, traces: FakeTraceEvents) -> None:
        self.trace_events = traces
        self.execution_steps = FakeExecutionSteps()

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


def trace_collector() -> tuple[FakeTraceEvents, Callable[[], FakeUnitOfWork]]:
    traces = FakeTraceEvents()

    def factory() -> FakeUnitOfWork:
        return FakeUnitOfWork(traces)

    return traces, factory


async def no_sleep(_seconds: float) -> None:
    return None


def handlers_for(
    adapters: Adapters | None = None,
    *,
    uow_factory: Any = None,
    max_retries: int = 2,
) -> NodeHandlers:
    return NodeHandlers(
        clock=FixedClock(NOW),
        adapters=adapters,
        uow_factory=uow_factory,
        sleep=no_sleep,
    )


# ---------------------------------------------------------------------------
# A counting stand-in for execute_tool
# ---------------------------------------------------------------------------
class CountingExecutor:
    """Mirrors `execute_tool`'s delta while counting real effects.

    Each call is one mutation. The whole point of VERIFY-003 is that
    `self.calls` stays at 1 when a verifier cannot answer, so this counter is
    the assertion in every unconfirmed test.
    """

    def __init__(self, *, output: dict[str, Any], effect: Callable[[], None] | None = None) -> None:
        self.calls = 0
        self._output = output
        self._effect = effect

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        step_id = state["current_step_id"]
        assert step_id is not None
        plan = state["plan"]
        assert plan is not None
        step = plan.step(step_id)
        assert step is not None
        attempt = state.get("retry_count", {}).get(step_id, 0) + 1
        self.calls += 1
        if self._effect is not None:
            self._effect()
        contract = REGISTRY[step.tool]
        settled = contract.verification is VerificationMode.NONE
        return {
            "tool_calls": [
                ToolCall(
                    step_id=step_id,
                    tool=step.tool,
                    attempt=attempt,
                    args_hash="h_1",
                    idempotency_key="idem_1",
                    status="succeeded",
                )
            ],
            "tool_results": {
                step_id: ToolResult(
                    step_id=step_id, tool=step.tool, output=self._output, produced_at=NOW
                )
            },
            "plan": plan.model_copy(
                update={
                    "steps": [
                        s.model_copy(
                            update={
                                "status": (StepStatus.SUCCEEDED if settled else StepStatus.RUNNING)
                            }
                        )
                        if s.step_id == step_id
                        else s
                        for s in plan.steps
                    ]
                }
            ),
            "step_count": state.get("step_count", 0) + 1,
        }


def build_graph(
    *,
    executor: CountingExecutor,
    adapters: Adapters,
    checkpointer: MemorySaver | None = None,
    max_retries: int = 2,
    uow_factory: Any = None,
) -> tuple[Any, NodeHandlers, dict[str, Any]]:
    handlers = handlers_for(adapters, uow_factory=uow_factory, max_retries=max_retries)
    handlers.execute_tool = executor  # type: ignore[assignment]
    graph = create_agent_graph(checkpointer=checkpointer or MemorySaver(), node_handlers=handlers)
    cfg: dict[str, Any] = {"configurable": {"thread_id": str(uuid.uuid4())}}
    return graph, handlers, cfg


def granted(plan: Plan) -> ApprovalState:
    """A standing operator decision for every gated step in `plan`.

    VERIFY-003 changes nothing about the gate, so these tests carry a real
    grant — bound to the real argument hash — rather than routing around it.
    An approval that did not match these arguments would not authorise them.
    """
    decisions = {
        step.step_id: ApprovalDecision(
            approval_id=f"ap_{step.step_id}",
            step_id=step.step_id,
            decision=ApprovalDecisionKind.APPROVE,
            args_hash=canonical_args_hash(step.args),
            decided_by="ops@example.com",
            decided_at=NOW,
        )
        for step in plan.steps
        if REGISTRY[step.tool].requires_approval
    }
    return ApprovalState(decisions=decisions)


def seeded_state(
    plan: Plan,
    *,
    run_id: str | None = None,
    max_retries: int = 2,
    intent: CanonicalIntent = CanonicalIntent.DRAFT_OUTREACH,
) -> AgentState:
    """A run that starts with the plan already settled.

    The normalized task is supplied so `understand` keeps it and `plan`
    validates the seeded plan rather than replacing it: these tests are about
    the verification loop, not about planning.
    """
    state = create_initial_state(
        run_id or str(uuid.uuid4()),
        "Draft and save outreach for lead l_1",
        metadata=RunMetadata(budgets=Budgets(max_retries=max_retries)),
        clock=FixedClock(NOW),
        plan=plan,
    )
    state["normalized_task"] = NormalizedTask(intent=intent, requires_mutation=True)
    state["approval_state"] = granted(plan)
    return state


# ===========================================================================
# 1. PASSED
# ===========================================================================
class TestVerificationPassed:
    @pytest.mark.asyncio
    async def test_passed_routes_to_decide_and_records_no_error(self) -> None:
        handlers = handlers_for(adapters_with(drafts=ScriptedDraftPort([good_draft()])))
        state = executed_state()

        delta = await handlers.verify(state)

        assert delta["verification_result"]["s1"].status is VerificationStatus.PASSED
        assert "errors" not in delta
        merged = {**state, **delta}
        assert handlers.route_after_verify(merged) == "decide"

    @pytest.mark.asyncio
    async def test_step_becomes_succeeded_only_after_verification(self) -> None:
        """A tool that returned is not yet a step that succeeded."""
        drafts = ScriptedDraftPort([good_draft()])
        handlers = handlers_for(adapters_with(drafts=drafts))
        state = executed_state()

        # Post-execution, pre-verification: the step is still running.
        assert state["plan"].step("s1").status is StepStatus.RUNNING

        delta = await handlers.verify(state)

        assert delta["plan"].step("s1").status is StepStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_execute_tool_leaves_verified_tools_running(self) -> None:
        """The status transition lives in `verify`, not in `execute_tool`."""
        executor = CountingExecutor(output={"draft_id": "drf_1"})
        plan = make_plan("s1", ToolName.SAVE_DRAFT, SAVE_DRAFT_ARGS, status=StepStatus.PENDING)
        state = executed_state(plan=plan)
        state["current_step_id"] = "s1"

        delta = await executor(state)
        assert delta["plan"].step("s1").status is StepStatus.RUNNING

    @pytest.mark.asyncio
    async def test_not_required_settles_the_step_without_a_verifier(self) -> None:
        handlers = handlers_for(adapters_with())
        plan = make_plan("s1", ToolName.GET_LEAD, {"lead_id": "l_1"})
        state = executed_state(
            tool=ToolName.GET_LEAD,
            args={"lead_id": "l_1"},
            output={"lead": {"lead_id": "l_1"}},
            plan=plan,
        )

        delta = await handlers.verify(state)

        res = delta["verification_result"]["s1"]
        assert res.status is VerificationStatus.NOT_REQUIRED
        assert delta["plan"].step("s1").status is StepStatus.SUCCEEDED
        assert "errors" not in delta

    @pytest.mark.asyncio
    async def test_passed_schedules_no_retry(self) -> None:
        traces, factory = trace_collector()
        handlers = handlers_for(
            adapters_with(drafts=ScriptedDraftPort([good_draft()])), uow_factory=factory
        )
        state = executed_state()

        delta = await handlers.verify(state)

        assert "retry_count" not in delta
        assert traces.of_kind(TraceEventKind.RETRY_SCHEDULED) == []
        assert traces.of_kind(TraceEventKind.VERIFICATION_PASSED)


# ===========================================================================
# 2. FAILED — confirmed, and classified on the evidence
# ===========================================================================
class TestVerificationFailedEvidence:
    def test_absent_write_is_the_only_safe_evidence(self) -> None:
        absent = VerificationResult(
            step_id="s1",
            status=VerificationStatus.FAILED,
            mode="readback",
            checks=[VerificationCheck(name="draft_record_exists", passed=False)],
        )
        assert classify_verification_failure(absent) is VerificationSafety.ABSENT
        assert is_safe_to_retry_mutation(absent, REGISTRY[ToolName.SAVE_DRAFT])

    @pytest.mark.parametrize(
        "check_name",
        [
            "recipient_matches_intent",
            "idempotency_key_single_row",
            "untouched_fields_intact",
            "patched_fields_match_intent",
            "content_hash_matches_requested_content",
            "status_is_sent",
            "version_advanced_by_one",
        ],
    )
    def test_contradicting_evidence_is_never_safe(self, check_name: str) -> None:
        result = VerificationResult(
            step_id="s1",
            status=VerificationStatus.FAILED,
            mode="readback",
            checks=[
                VerificationCheck(name="outbox_record_exists", passed=True),
                VerificationCheck(name=check_name, passed=False),
            ],
        )
        assert classify_verification_failure(result) is VerificationSafety.CONTRADICTED
        assert not is_safe_to_retry_mutation(result, REGISTRY[ToolName.SEND_EMAIL_MOCK])

    def test_an_unknown_check_is_unsafe_until_someone_decides_otherwise(self) -> None:
        result = VerificationResult(
            step_id="s1",
            status=VerificationStatus.FAILED,
            mode="readback",
            checks=[VerificationCheck(name="a_check_added_next_year", passed=False)],
        )
        assert classify_verification_failure(result) is VerificationSafety.CONTRADICTED

    def test_failure_with_no_failing_check_is_not_evidence_of_absence(self) -> None:
        result = VerificationResult(
            step_id="s1", status=VerificationStatus.FAILED, mode="readback", checks=[]
        )
        assert classify_verification_failure(result) is VerificationSafety.CONTRADICTED

    def test_a_non_idempotent_contract_overrides_absent_evidence(self) -> None:
        """Invariant P5: the contract has a veto the evidence cannot overturn."""
        absent = VerificationResult(
            step_id="s1",
            status=VerificationStatus.FAILED,
            mode="readback",
            checks=[VerificationCheck(name="outbox_record_exists", passed=False)],
        )
        non_idempotent = REGISTRY[ToolName.SEND_EMAIL_MOCK].model_copy(update={"idempotent": False})
        assert not is_safe_to_retry_mutation(absent, non_idempotent)

    def test_absence_checks_are_exactly_the_read_back_existence_checks(self) -> None:
        assert {
            "outbox_record_exists",
            "draft_record_exists",
            "customer_record_exists",
        } == PROVEN_ABSENCE_CHECKS


class TestFailedVerificationRecovery:
    @pytest.mark.asyncio
    async def test_missing_write_on_idempotent_tool_retries_execute_tool(self) -> None:
        """Case A — the draft is simply not there. Redoing it duplicates nothing."""
        drafts = ScriptedDraftPort([NotFoundError("Draft not found: drf_1")])
        handlers = handlers_for(adapters_with(drafts=drafts))
        state = executed_state()

        verify_delta = await handlers.verify(state)
        state = {**state, **_merge(state, verify_delta)}
        assert handlers.route_after_verify(state) == "recover"

        recover_delta = await handlers.recover(state)
        state = {**state, **_merge(state, recover_delta)}

        assert recover_delta["status_reason"] == "retry_attempt_1"
        assert recover_delta["retry_count"] == {"s1": 1}
        assert handlers.route_after_recover(state) == "execute_tool"

    @pytest.mark.asyncio
    async def test_retry_then_success_verifies_and_reaches_decide(self) -> None:
        """execute → verify(failed, absent) → recover → execute → verify(passed) → decide."""
        drafts = ScriptedDraftPort([NotFoundError("Draft not found"), good_draft()])
        executor = CountingExecutor(output={"draft_id": "drf_1"})
        graph, handlers, cfg = build_graph(executor=executor, adapters=adapters_with(drafts=drafts))

        final = await graph.ainvoke(
            seeded_state(
                make_plan("s1", ToolName.SAVE_DRAFT, SAVE_DRAFT_ARGS, status=StepStatus.PENDING)
            ),
            config=cfg,
        )

        assert executor.calls == 2
        assert final["status"] is RunStatus.COMPLETED
        assert final["verification_result"]["s1"].status is VerificationStatus.PASSED
        assert final["plan"].step("s1").status is StepStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_retry_budget_exhaustion_fails_as_verification_failed(self) -> None:
        drafts = ScriptedDraftPort([NotFoundError("Draft not found")])
        executor = CountingExecutor(output={"draft_id": "drf_1"})
        graph, handlers, cfg = build_graph(
            executor=executor, adapters=adapters_with(drafts=drafts), max_retries=2
        )

        final = await graph.ainvoke(
            seeded_state(
                make_plan("s1", ToolName.SAVE_DRAFT, SAVE_DRAFT_ARGS, status=StepStatus.PENDING),
                max_retries=2,
            ),
            config=cfg,
        )

        assert executor.calls == 3  # the first attempt plus max_retries
        assert final["status"] is RunStatus.FAILED
        assert final["status_reason"] == "verification_failed"
        assert final["status_reason"] != "recovery_exhausted"
        assert final["final_response"].unconfirmed == ["s1"]
        assert final["final_response"].done == []

    @pytest.mark.asyncio
    async def test_non_idempotent_verification_failure_fails_immediately(self) -> None:
        """Invariant P5 — no retry, and the run never claims the effect happened."""
        handlers = handlers_for(adapters_with())
        result = VerificationResult(
            step_id="s1",
            status=VerificationStatus.FAILED,
            mode="readback",
            checks=[VerificationCheck(name="outbox_record_exists", passed=False)],
        )
        state = executed_state(
            tool=ToolName.SEND_EMAIL_MOCK,
            args=SEND_EMAIL_ARGS,
            output={"message_id": "msg_1"},
            verification=result,
            errors=[_verify_error("s1", ErrorClass.VERIFICATION_FAILED, result)],
        )
        handlers._get_contract = lambda _t: REGISTRY[  # type: ignore[method-assign]
            ToolName.SEND_EMAIL_MOCK
        ].model_copy(update={"idempotent": False})

        delta = await handlers.recover(state)

        assert delta["status_reason"] == "verification_failed"
        assert "retry_count" not in delta
        assert handlers.route_after_recover({**state, **delta}) == "fail"

    @pytest.mark.asyncio
    async def test_duplicate_outbox_rows_terminate_without_a_second_send(self) -> None:
        """Adversarial C — two rows for one idempotency key. Never send again."""
        mail = ScriptedMailPort([good_outbox()], outbox_count=2)
        executor = CountingExecutor(output={"message_id": "msg_1"})
        handlers = handlers_for(adapters_with(mail=mail))
        state = executed_state(
            tool=ToolName.SEND_EMAIL_MOCK,
            args=SEND_EMAIL_ARGS,
            output={"message_id": "msg_1"},
            plan=make_plan("s1", ToolName.SEND_EMAIL_MOCK, SEND_EMAIL_ARGS),
        )

        verify_delta = await handlers.verify(state)
        res = verify_delta["verification_result"]["s1"]
        assert res.status is VerificationStatus.FAILED
        assert "idempotency_key_single_row" in {c.name for c in res.checks if not c.passed}

        state = {**state, **_merge(state, verify_delta)}
        recover_delta = await handlers.recover(state)

        assert recover_delta["status_reason"] == "verification_failed"
        assert handlers.route_after_recover({**state, **recover_delta}) == "fail"
        assert executor.calls == 0

    @pytest.mark.asyncio
    async def test_wrong_recipient_terminates_without_a_second_send(self) -> None:
        """Adversarial B — the mail went somewhere nobody approved."""
        mail = ScriptedMailPort([good_outbox(to_email="attacker@example.com")])
        handlers = handlers_for(adapters_with(mail=mail))
        state = executed_state(
            tool=ToolName.SEND_EMAIL_MOCK,
            args=SEND_EMAIL_ARGS,
            output={"message_id": "msg_1"},
            plan=make_plan("s1", ToolName.SEND_EMAIL_MOCK, SEND_EMAIL_ARGS),
        )

        verify_delta = await handlers.verify(state)
        res = verify_delta["verification_result"]["s1"]
        assert not next(c for c in res.checks if c.name == "recipient_matches_intent").passed

        state = {**state, **_merge(state, verify_delta)}
        assert (await handlers.recover(state))["status_reason"] == "verification_failed"

    @pytest.mark.asyncio
    async def test_customer_untouched_field_tampering_terminates(self) -> None:
        """Adversarial D — a field nobody asked to touch changed under us."""
        customers = ScriptedCustomerPort([good_customer(account_name="Compromised Corp")])
        handlers = handlers_for(adapters_with(customers=customers))
        state = executed_state(
            tool=ToolName.UPDATE_CUSTOMER,
            args=UPDATE_CUSTOMER_ARGS,
            output={"customer_id": "cus_1", "version": 2},
            plan=make_plan("s1", ToolName.UPDATE_CUSTOMER, UPDATE_CUSTOMER_ARGS),
            prior_results={
                "s0": ToolResult(
                    step_id="s0",
                    tool=ToolName.GET_CUSTOMER,
                    output=BASELINE_CUSTOMER_OUTPUT,
                    produced_at=NOW,
                )
            },
        )

        verify_delta = await handlers.verify(state)
        res = verify_delta["verification_result"]["s1"]
        assert not next(c for c in res.checks if c.name == "untouched_fields_intact").passed

        state = {**state, **_merge(state, verify_delta)}
        recover_delta = await handlers.recover(state)

        assert recover_delta["status_reason"] == "verification_failed"
        assert customers.reads == 1  # read once, never written

    @pytest.mark.asyncio
    async def test_optional_step_keeps_existing_skip_semantics(self) -> None:
        """An optional step skips rather than ending the run; skipping compounds nothing."""
        mail = ScriptedMailPort([good_outbox()], outbox_count=2)
        handlers = handlers_for(adapters_with(mail=mail))
        plan = make_plan("s1", ToolName.SEND_EMAIL_MOCK, SEND_EMAIL_ARGS, optional=True)
        state = executed_state(
            tool=ToolName.SEND_EMAIL_MOCK,
            args=SEND_EMAIL_ARGS,
            output={"message_id": "msg_1"},
            plan=plan,
        )

        verify_delta = await handlers.verify(state)
        state = {**state, **_merge(state, verify_delta)}
        recover_delta = await handlers.recover(state)

        assert recover_delta["status_reason"] == "optional_step_skipped"
        assert recover_delta["plan"].step("s1").status is StepStatus.SKIPPED
        assert handlers.route_after_recover({**state, **recover_delta}) == "decide"


# ===========================================================================
# 3. UNCONFIRMED — the tool succeeded; only the read-back is missing
# ===========================================================================
class TestUnconfirmedIsNotFailed:
    @pytest.mark.asyncio
    async def test_port_blip_yields_unconfirmed_and_a_transient_error(self) -> None:
        drafts = ScriptedDraftPort([ConnectionError("draft store unreachable")])
        handlers = handlers_for(adapters_with(drafts=drafts))
        state = executed_state()

        delta = await handlers.verify(state)

        res = delta["verification_result"]["s1"]
        assert res.status is VerificationStatus.UNCONFIRMED
        assert res.status is not VerificationStatus.FAILED
        err = delta["errors"][0]
        assert err.error_class is ErrorClass.TRANSIENT
        assert err.error_class is not ErrorClass.VERIFICATION_FAILED
        assert is_verification_error(err)

    @pytest.mark.asyncio
    async def test_unconfirmed_trace_names_the_distinction_at_warning_severity(self) -> None:
        traces, factory = trace_collector()
        mail = ScriptedMailPort([TransientToolError("outbox unreachable")])
        handlers = handlers_for(adapters_with(mail=mail), uow_factory=factory)
        state = executed_state(
            tool=ToolName.SEND_EMAIL_MOCK,
            args=SEND_EMAIL_ARGS,
            output={"message_id": "msg_1"},
            plan=make_plan("s1", ToolName.SEND_EMAIL_MOCK, SEND_EMAIL_ARGS),
        )

        await handlers.verify(state)

        [event] = traces.of_kind(TraceEventKind.VERIFICATION_FAILED)
        assert event["severity"] is TraceEventSeverity.WARNING
        assert event["status"] == "unconfirmed"
        payload = event["payload"]
        assert payload["unconfirmed"] is True
        assert payload["classification"] == ErrorClass.TRANSIENT.value
        assert payload["recovery"] == "retry_readback"
        assert payload["tool_effect_succeeded"] is True

    @pytest.mark.asyncio
    async def test_a_proven_failure_on_a_mutating_tool_stays_at_error_severity(self) -> None:
        traces, factory = trace_collector()
        mail = ScriptedMailPort([good_outbox(to_email="wrong@example.com")])
        handlers = handlers_for(adapters_with(mail=mail), uow_factory=factory)
        state = executed_state(
            tool=ToolName.SEND_EMAIL_MOCK,
            args=SEND_EMAIL_ARGS,
            output={"message_id": "msg_1"},
            plan=make_plan("s1", ToolName.SEND_EMAIL_MOCK, SEND_EMAIL_ARGS),
        )

        await handlers.verify(state)

        [event] = traces.of_kind(TraceEventKind.VERIFICATION_FAILED)
        assert event["severity"] is TraceEventSeverity.ERROR
        assert event["status"] == "failed"

    def test_retry_target_requires_all_three_facts(self) -> None:
        unconfirmed = VerificationResult(
            step_id="s1", status=VerificationStatus.UNCONFIRMED, mode="readback"
        )
        ok_call = [
            ToolCall(
                step_id="s1", tool=ToolName.SAVE_DRAFT, attempt=1, args_hash="h", status="succeeded"
            )
        ]
        bad_call = [
            ToolCall(
                step_id="s1", tool=ToolName.SAVE_DRAFT, attempt=1, args_hash="h", status="failed"
            )
        ]
        verify_err = _verify_error("s1", ErrorClass.TRANSIENT, unconfirmed)
        tool_err = AgentError(step_id="s1", error_class=ErrorClass.TRANSIENT, message="boom")

        assert (
            retry_target_for(error=verify_err, result=unconfirmed, tool_calls=ok_call, step_id="s1")
            is RetryTarget.VERIFY
        )
        # A dispatcher-side transient failure is not a verification retry.
        assert (
            retry_target_for(error=tool_err, result=unconfirmed, tool_calls=ok_call, step_id="s1")
            is RetryTarget.EXECUTE_TOOL
        )
        # The tool did not succeed: there is no effect to protect.
        assert (
            retry_target_for(
                error=verify_err, result=unconfirmed, tool_calls=bad_call, step_id="s1"
            )
            is RetryTarget.EXECUTE_TOOL
        )
        # A proven failure is not an unconfirmed one.
        failed = unconfirmed.model_copy(update={"status": VerificationStatus.FAILED})
        assert (
            retry_target_for(error=verify_err, result=failed, tool_calls=ok_call, step_id="s1")
            is RetryTarget.EXECUTE_TOOL
        )

    def test_tool_effect_success_is_read_from_the_latest_attempt(self) -> None:
        calls = [
            ToolCall(
                step_id="s1", tool=ToolName.SAVE_DRAFT, attempt=1, args_hash="h", status="failed"
            ),
            ToolCall(
                step_id="s1", tool=ToolName.SAVE_DRAFT, attempt=2, args_hash="h", status="succeeded"
            ),
        ]
        assert tool_effect_succeeded(calls, "s1") is True
        assert tool_effect_succeeded(calls, "s2") is False
        assert tool_effect_succeeded([], "s1") is False


class TestUnconfirmedRecovery:
    @pytest.mark.asyncio
    async def test_recover_targets_verify_and_counts_once(self) -> None:
        drafts = ScriptedDraftPort([TransientToolError("draft store unreachable")])
        handlers = handlers_for(adapters_with(drafts=drafts))
        state = executed_state()

        verify_delta = await handlers.verify(state)
        state = {**state, **_merge(state, verify_delta)}

        recover_delta = await handlers.recover(state)

        assert recover_delta["status_reason"] == "retry_verify_attempt_1"
        assert recover_delta["retry_count"] == {verify_retry_key("s1"): 1}
        # The tool's own retry budget is untouched: nothing about the tool failed.
        assert "s1" not in recover_delta["retry_count"]
        assert handlers.route_after_recover({**state, **recover_delta}) == "verify"

    @pytest.mark.asyncio
    async def test_save_draft_unconfirmed_then_confirmed_without_a_second_save(self) -> None:
        """Requirement 11 — verifier timeout, then a successful read. One save."""
        drafts = ScriptedDraftPort([TransientToolError("timeout"), good_draft()])
        executor = CountingExecutor(output={"draft_id": "drf_1"})
        graph, _handlers, cfg = build_graph(
            executor=executor, adapters=adapters_with(drafts=drafts)
        )

        final = await graph.ainvoke(
            seeded_state(
                make_plan("s1", ToolName.SAVE_DRAFT, SAVE_DRAFT_ARGS, status=StepStatus.PENDING)
            ),
            config=cfg,
        )

        assert executor.calls == 1, "the draft must be saved exactly once"
        assert drafts.reads == 2, "the read-back, not the write, is what was retried"
        assert final["status"] is RunStatus.COMPLETED
        assert final["verification_result"]["s1"].status is VerificationStatus.PASSED
        assert final["retry_count"] == {verify_retry_key("s1"): 1}

    @pytest.mark.asyncio
    async def test_update_customer_unconfirmed_never_updates_twice(self) -> None:
        """Requirement 12 — the customer record is written once, read twice."""
        customers = ScriptedCustomerPort([TransientToolError("db timeout"), good_customer()])
        updates: list[int] = []
        executor = CountingExecutor(
            output={"customer_id": "cus_1", "version": 2},
            effect=lambda: updates.append(1),
        )
        plan = make_plan(
            "s1", ToolName.UPDATE_CUSTOMER, UPDATE_CUSTOMER_ARGS, status=StepStatus.PENDING
        )
        graph, handlers, cfg = build_graph(
            executor=executor, adapters=adapters_with(customers=customers)
        )
        state = seeded_state(plan, intent=CanonicalIntent.CUSTOMER_UPDATE)
        # The pre-image the untouched-fields check compares against, as a
        # completed read from earlier in the run.
        state["tool_results"] = {
            "s0": ToolResult(
                step_id="s0",
                tool=ToolName.GET_CUSTOMER,
                output=BASELINE_CUSTOMER_OUTPUT,
                produced_at=NOW,
            )
        }

        final = await graph.ainvoke(state, config=cfg)

        assert len(updates) == 1, "exactly one customer mutation"
        assert customers.reads == 2
        assert final["verification_result"]["s1"].status is VerificationStatus.PASSED
        assert final["status"] is RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_send_email_unconfirmed_leaves_exactly_one_outbox_row(self) -> None:
        """Requirement 13 / adversarial A — one send, however many reads."""
        outbox: list[str] = []
        mail = ScriptedMailPort([TransientToolError("outbox unreachable"), good_outbox()])
        executor = CountingExecutor(
            output={"message_id": "msg_1"}, effect=lambda: outbox.append("msg_1")
        )
        graph, _handlers, cfg = build_graph(executor=executor, adapters=adapters_with(mail=mail))

        final = await graph.ainvoke(
            seeded_state(
                make_plan(
                    "s1", ToolName.SEND_EMAIL_MOCK, SEND_EMAIL_ARGS, status=StepStatus.PENDING
                )
            ),
            config=cfg,
        )

        assert len(outbox) == 1, "no second message may leave the system"
        assert executor.calls == 1
        assert final["verification_result"]["s1"].status is VerificationStatus.PASSED

    @pytest.mark.asyncio
    async def test_persistent_verifier_outage_ends_as_verification_unconfirmed(self) -> None:
        """Requirement 14 / adversarial E — absence of evidence is not evidence."""
        drafts = ScriptedDraftPort([TransientToolError("store down")])
        executor = CountingExecutor(output={"draft_id": "drf_1"})
        graph, _handlers, cfg = build_graph(
            executor=executor, adapters=adapters_with(drafts=drafts), max_retries=2
        )

        final = await graph.ainvoke(
            seeded_state(
                make_plan("s1", ToolName.SAVE_DRAFT, SAVE_DRAFT_ARGS, status=StepStatus.PENDING),
                max_retries=2,
            ),
            config=cfg,
        )

        assert executor.calls == 1, "not one extra write while the reader was down"
        assert drafts.reads == 3  # 1 + max_retries
        assert final["status"] is RunStatus.FAILED
        assert final["status_reason"] == "verification_unconfirmed"
        assert final["status_reason"] != "verification_failed"
        assert final["final_response"].unconfirmed == ["s1"]
        assert "unconfirmed" in final["final_response"].summary

    @pytest.mark.asyncio
    async def test_unconfirmed_then_proven_failure_enters_failed_semantics(self) -> None:
        """A later read that *does* answer moves the run onto the failed path."""
        mail = ScriptedMailPort(
            [TransientToolError("blip"), good_outbox(to_email="wrong@example.com")]
        )
        executor = CountingExecutor(output={"message_id": "msg_1"})
        graph, _handlers, cfg = build_graph(executor=executor, adapters=adapters_with(mail=mail))

        final = await graph.ainvoke(
            seeded_state(
                make_plan(
                    "s1", ToolName.SEND_EMAIL_MOCK, SEND_EMAIL_ARGS, status=StepStatus.PENDING
                )
            ),
            config=cfg,
        )

        assert executor.calls == 1
        assert final["status_reason"] == "verification_failed"
        assert final["verification_result"]["s1"].status is VerificationStatus.FAILED

    @pytest.mark.asyncio
    async def test_retry_counter_increments_exactly_once_per_attempt(self) -> None:
        """Requirement 15 — re-entering `recover` on the same evidence is a no-op."""
        handlers = handlers_for(adapters_with())
        result = VerificationResult(
            step_id="s1",
            status=VerificationStatus.UNCONFIRMED,
            mode="readback",
            attempt=1,
            checks=[VerificationCheck(name="verifier_execution", passed=False)],
        )
        state = executed_state(
            verification=result,
            errors=[_verify_error("s1", ErrorClass.TRANSIENT, result)],
        )

        first = await handlers.recover(state)
        assert first["retry_count"] == {verify_retry_key("s1"): 1}

        # The crash/resume case: the counter is already at 1 for attempt 1.
        resumed = {**state, "retry_count": dict(first["retry_count"])}
        second = await handlers.recover(resumed)

        assert second["retry_count"] == {verify_retry_key("s1"): 1}
        assert second["status_reason"] == "retry_verify_attempt_1"
        assert handlers.route_after_recover({**resumed, **second}) == "verify"

    @pytest.mark.asyncio
    async def test_a_second_unconfirmed_attempt_does_advance_the_counter(self) -> None:
        handlers = handlers_for(adapters_with())
        result = VerificationResult(
            step_id="s1", status=VerificationStatus.UNCONFIRMED, mode="readback", attempt=2
        )
        state = executed_state(
            verification=result,
            retry_count={verify_retry_key("s1"): 1},
            errors=[_verify_error("s1", ErrorClass.TRANSIENT, result)],
        )

        delta = await handlers.recover(state)

        assert delta["retry_count"] == {verify_retry_key("s1"): 2}
        assert delta["status_reason"] == "retry_verify_attempt_2"

    @pytest.mark.asyncio
    async def test_verify_stamps_the_attempt_from_checkpointed_state(self) -> None:
        """Requirement 16 — the retry target survives a checkpoint because it
        is derived from the checkpoint."""
        drafts = ScriptedDraftPort([TransientToolError("still down")])
        handlers = handlers_for(adapters_with(drafts=drafts))
        state = executed_state(retry_count={verify_retry_key("s1"): 1})

        delta = await handlers.verify(state)

        assert delta["verification_result"]["s1"].attempt == 2
        assert delta["errors"][0].detail["verification_attempt"] == 2

    @pytest.mark.asyncio
    async def test_unconfirmed_optional_step_skips_when_reads_are_exhausted(self) -> None:
        handlers = handlers_for(adapters_with())
        result = VerificationResult(
            step_id="s1", status=VerificationStatus.UNCONFIRMED, mode="readback", attempt=3
        )
        plan = make_plan("s1", ToolName.SAVE_DRAFT, SAVE_DRAFT_ARGS, optional=True)
        state = executed_state(
            plan=plan,
            verification=result,
            retry_count={verify_retry_key("s1"): 2},
            errors=[_verify_error("s1", ErrorClass.TRANSIENT, result)],
        )

        delta = await handlers.recover(state)

        assert delta["status_reason"] == "optional_step_skipped"
        assert delta["plan"].step("s1").status is StepStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_retry_trace_names_the_target_node(self) -> None:
        traces, factory = trace_collector()
        handlers = handlers_for(adapters_with(), uow_factory=factory)
        unconfirmed = VerificationResult(
            step_id="s1", status=VerificationStatus.UNCONFIRMED, mode="readback", attempt=1
        )
        await handlers.recover(
            executed_state(
                verification=unconfirmed,
                errors=[_verify_error("s1", ErrorClass.TRANSIENT, unconfirmed)],
            )
        )
        absent = VerificationResult(
            step_id="s1",
            status=VerificationStatus.FAILED,
            mode="readback",
            checks=[VerificationCheck(name="draft_record_exists", passed=False)],
        )
        await handlers.recover(
            executed_state(
                verification=absent,
                errors=[_verify_error("s1", ErrorClass.VERIFICATION_FAILED, absent)],
            )
        )

        targets = [e["payload"]["target"] for e in traces.of_kind(TraceEventKind.RETRY_SCHEDULED)]
        assert targets == ["verify", "execute_tool"]
        assert all(
            "approval_token" not in str(e) and "payload_preview" not in str(e)
            for e in traces.events
        )


# ===========================================================================
# 4. CRASH / CHECKPOINT SAFETY
# ===========================================================================
class _CrashOnce:
    """Wraps a node handler and dies on the nth call, as a worker would."""

    def __init__(self, inner: Any, *, die_on: int = 1) -> None:
        self._inner = inner
        self._die_on = die_on
        self.calls = 0

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        self.calls += 1
        if self.calls == self._die_on:
            raise asyncio.CancelledError("worker died")
        return await self._inner(state)


class TestCrashSafety:
    @pytest.mark.asyncio
    async def test_crash_after_execute_before_verify_resumes_into_verify(self) -> None:
        """Requirement 17 — resume re-enters `verify`; the tool is not re-run."""
        drafts = ScriptedDraftPort([good_draft()])
        executor = CountingExecutor(output={"draft_id": "drf_1"})
        saver = MemorySaver()
        handlers = handlers_for(adapters_with(drafts=drafts))
        handlers.execute_tool = executor  # type: ignore[assignment]
        crashing = _CrashOnce(handlers.verify, die_on=1)
        handlers.verify = crashing  # type: ignore[assignment]
        graph = create_agent_graph(checkpointer=saver, node_handlers=handlers)
        cfg: dict[str, Any] = {"configurable": {"thread_id": str(uuid.uuid4())}}

        with pytest.raises(NodeCancelledError):
            await graph.ainvoke(
                seeded_state(
                    make_plan("s1", ToolName.SAVE_DRAFT, SAVE_DRAFT_ARGS, status=StepStatus.PENDING)
                ),
                config=cfg,
            )

        assert executor.calls == 1
        snapshot = graph.get_state(cfg)
        assert [t.name for t in snapshot.tasks] == ["verify"]

        final = await graph.ainvoke(None, config=cfg)

        assert executor.calls == 1, "resume must not re-execute the tool"
        assert crashing.calls == 2
        assert final["status"] is RunStatus.COMPLETED
        assert final["verification_result"]["s1"].status is VerificationStatus.PASSED

    @pytest.mark.asyncio
    async def test_crash_inside_the_verifier_re_enters_verify(self) -> None:
        """Requirement 18 — the read-back itself dies mid-flight."""
        calls = {"n": 0}

        class DyingDraftPort:
            reads = 0

            async def get(self, draft_id: str) -> DraftRecord:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise asyncio.CancelledError("worker died mid-read")
                return good_draft()

        executor = CountingExecutor(output={"draft_id": "drf_1"})
        saver = MemorySaver()
        graph, _handlers, cfg = build_graph(
            executor=executor,
            adapters=adapters_with(drafts=DyingDraftPort()),
            checkpointer=saver,
        )

        with pytest.raises(NodeCancelledError):
            await graph.ainvoke(
                seeded_state(
                    make_plan("s1", ToolName.SAVE_DRAFT, SAVE_DRAFT_ARGS, status=StepStatus.PENDING)
                ),
                config=cfg,
            )
        assert [t.name for t in graph.get_state(cfg).tasks] == ["verify"]

        final = await graph.ainvoke(None, config=cfg)

        assert executor.calls == 1
        assert calls["n"] == 2
        assert final["status"] is RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_crash_after_unconfirmed_preserves_that_the_tool_succeeded(self) -> None:
        """Requirement 19 — the resumed run still knows not to re-execute."""
        drafts = ScriptedDraftPort([TransientToolError("down"), good_draft()])
        executor = CountingExecutor(output={"draft_id": "drf_1"})
        saver = MemorySaver()
        handlers = handlers_for(adapters_with(drafts=drafts))
        handlers.execute_tool = executor  # type: ignore[assignment]
        real_recover = handlers.recover
        handlers.recover = _CrashOnce(real_recover, die_on=1)  # type: ignore[assignment]
        graph = create_agent_graph(checkpointer=saver, node_handlers=handlers)
        cfg: dict[str, Any] = {"configurable": {"thread_id": str(uuid.uuid4())}}

        with pytest.raises(NodeCancelledError):
            await graph.ainvoke(
                seeded_state(
                    make_plan("s1", ToolName.SAVE_DRAFT, SAVE_DRAFT_ARGS, status=StepStatus.PENDING)
                ),
                config=cfg,
            )

        mid = graph.get_state(cfg).values
        assert [t.name for t in graph.get_state(cfg).tasks] == ["recover"]
        assert mid["verification_result"]["s1"].status is VerificationStatus.UNCONFIRMED
        assert tool_effect_succeeded(mid["tool_calls"], "s1") is True

        final = await graph.ainvoke(None, config=cfg)

        assert executor.calls == 1, "a resumed unconfirmed step is re-read, not re-written"
        assert final["retry_count"] == {verify_retry_key("s1"): 1}
        assert final["status"] is RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_crash_during_retry_backoff_counts_the_retry_once(self) -> None:
        """Requirement 20 — a lost backoff must not buy a second increment."""
        drafts = ScriptedDraftPort([TransientToolError("down"), good_draft()])
        executor = CountingExecutor(output={"draft_id": "drf_1"})
        saver = MemorySaver()
        sleeps = {"n": 0}

        async def dying_sleep(_seconds: float) -> None:
            sleeps["n"] += 1
            if sleeps["n"] == 1:
                raise asyncio.CancelledError("worker died in backoff")

        handlers = NodeHandlers(
            clock=FixedClock(NOW),
            adapters=adapters_with(drafts=drafts),
            sleep=dying_sleep,
        )
        handlers.execute_tool = executor  # type: ignore[assignment]
        graph = create_agent_graph(checkpointer=saver, node_handlers=handlers)
        cfg: dict[str, Any] = {"configurable": {"thread_id": str(uuid.uuid4())}}

        with pytest.raises(NodeCancelledError):
            await graph.ainvoke(
                seeded_state(
                    make_plan("s1", ToolName.SAVE_DRAFT, SAVE_DRAFT_ARGS, status=StepStatus.PENDING)
                ),
                config=cfg,
            )
        assert graph.get_state(cfg).values.get("retry_count", {}) == {}

        final = await graph.ainvoke(None, config=cfg)

        assert sleeps["n"] == 2
        assert final["retry_count"] == {verify_retry_key("s1"): 1}
        assert executor.calls == 1
        assert final["status"] is RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_crash_after_retry_before_verification_rebuilds_the_route(self) -> None:
        """Requirement 21 — the next node is reconstructed from the checkpoint."""
        drafts = ScriptedDraftPort([TransientToolError("down"), good_draft()])
        executor = CountingExecutor(output={"draft_id": "drf_1"})
        saver = MemorySaver()
        handlers = handlers_for(adapters_with(drafts=drafts))
        handlers.execute_tool = executor  # type: ignore[assignment]
        real_verify = handlers.verify
        handlers.verify = _CrashOnce(real_verify, die_on=2)  # type: ignore[assignment]
        graph = create_agent_graph(checkpointer=saver, node_handlers=handlers)
        cfg: dict[str, Any] = {"configurable": {"thread_id": str(uuid.uuid4())}}

        with pytest.raises(NodeCancelledError):
            await graph.ainvoke(
                seeded_state(
                    make_plan("s1", ToolName.SAVE_DRAFT, SAVE_DRAFT_ARGS, status=StepStatus.PENDING)
                ),
                config=cfg,
            )

        snapshot = graph.get_state(cfg)
        assert [t.name for t in snapshot.tasks] == ["verify"]
        assert snapshot.values["retry_count"] == {verify_retry_key("s1"): 1}
        assert snapshot.values["status_reason"] == "retry_verify_attempt_1"

        final = await graph.ainvoke(None, config=cfg)

        assert executor.calls == 1
        assert final["retry_count"] == {verify_retry_key("s1"): 1}
        assert final["status"] is RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_tool_exception_keeps_existing_execute_retry_semantics(self) -> None:
        """Adversarial F — a failed dispatch still retries `execute_tool`."""
        handlers = handlers_for(adapters_with())
        state = executed_state(
            tool_status="failed",
            errors=[
                AgentError(
                    step_id="s1",
                    error_class=ErrorClass.TRANSIENT,
                    message="adapter unavailable",
                    attempt=1,
                )
            ],
        )

        delta = await handlers.recover(state)

        assert delta["status_reason"] == "retry_attempt_1"
        assert delta["retry_count"] == {"s1": 1}
        assert handlers.route_after_recover({**state, **delta}) == "execute_tool"


# ===========================================================================
# 5. REPLAN — untouched by verification
# ===========================================================================
class TestReplanSemanticsArePreserved:
    @pytest.mark.asyncio
    async def test_stale_write_still_replans(self) -> None:
        """Requirement 22 — a moved record re-reads and re-plans, as before."""
        handlers = handlers_for(adapters_with())
        state = executed_state(
            tool=ToolName.UPDATE_CUSTOMER,
            args=UPDATE_CUSTOMER_ARGS,
            output={},
            plan=make_plan("s1", ToolName.UPDATE_CUSTOMER, UPDATE_CUSTOMER_ARGS),
            tool_status="failed",
            errors=[
                AgentError(
                    step_id="s1",
                    error_class=ErrorClass.STALE_WRITE,
                    message="version mismatch: expected 1, current 4",
                    attempt=1,
                )
            ],
        )

        delta = await handlers.recover(state)

        assert delta["status_reason"] == "replannable_fault"
        assert handlers.route_after_recover({**state, **delta}) == "plan"

    @pytest.mark.asyncio
    async def test_a_verification_failure_is_never_silently_promoted_to_a_replan(
        self,
    ) -> None:
        handlers = handlers_for(adapters_with())
        result = VerificationResult(
            step_id="s1",
            status=VerificationStatus.FAILED,
            mode="readback",
            checks=[VerificationCheck(name="untouched_fields_intact", passed=False)],
        )
        state = executed_state(
            tool=ToolName.UPDATE_CUSTOMER,
            args=UPDATE_CUSTOMER_ARGS,
            output={"customer_id": "cus_1"},
            plan=make_plan("s1", ToolName.UPDATE_CUSTOMER, UPDATE_CUSTOMER_ARGS),
            verification=result,
            errors=[_verify_error("s1", ErrorClass.VERIFICATION_FAILED, result)],
        )

        delta = await handlers.recover(state)

        assert delta["status_reason"] == "verification_failed"
        assert handlers.route_after_recover({**state, **delta}) != "plan"

    def test_a_replanned_mutation_cannot_inherit_the_old_grant(self) -> None:
        """Requirements 23 and 24 — new arguments, new hash, no standing grant."""
        old_args = dict(UPDATE_CUSTOMER_ARGS)
        approvals = ApprovalState(
            decisions={
                "s1": ApprovalDecision(
                    approval_id="ap_1",
                    step_id="s1",
                    decision=ApprovalDecisionKind.APPROVE,
                    args_hash=canonical_args_hash(old_args),
                    decided_by="ops@example.com",
                    decided_at=NOW,
                )
            }
        )
        assert approvals.grants("s1", old_args) is True

        # The replan re-read the customer: a different expected_version, so a
        # different hash, so the old approval authorises nothing.
        new_args = {**old_args, "expected_version": 4}
        assert canonical_args_hash(new_args) != canonical_args_hash(old_args)
        assert approvals.grants("s1", new_args) is False


# ===========================================================================
# 6. END TO END
# ===========================================================================
class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_execute_verify_recover_verify_decide_next_step(self) -> None:
        """Requirement 25 — the loop closes and the run moves on."""
        drafts = ScriptedDraftPort([TransientToolError("blip"), good_draft()])
        mail = ScriptedMailPort([good_outbox()])
        outputs = {"s1": {"draft_id": "drf_1"}, "s2": {"message_id": "msg_1"}}

        class TwoStepExecutor(CountingExecutor):
            async def __call__(self, state: AgentState) -> dict[str, Any]:
                self._output = outputs[state["current_step_id"]]  # type: ignore[index]
                return await super().__call__(state)

        executor = TwoStepExecutor(output={})
        plan = Plan(
            plan_id="p_e2e",
            steps=[
                PlanStep(step_id="s1", tool=ToolName.SAVE_DRAFT, args=SAVE_DRAFT_ARGS),
                PlanStep(
                    step_id="s2",
                    tool=ToolName.SEND_EMAIL_MOCK,
                    args=SEND_EMAIL_ARGS,
                    depends_on=["s1"],
                ),
            ],
        )
        graph, _handlers, cfg = build_graph(
            executor=executor, adapters=adapters_with(drafts=drafts, mail=mail)
        )

        final = await graph.ainvoke(seeded_state(plan), config=cfg)

        assert executor.calls == 2, "one execution per step, despite the read-back retry"
        assert drafts.reads == 2
        assert final["status"] is RunStatus.COMPLETED
        assert final["final_response"].done == ["s1", "s2"]
        assert final["final_response"].unconfirmed == []
        assert final["plan"].step("s2").status is StepStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_no_false_successful_terminal_response(self) -> None:
        """Requirement 26 — an unconfirmed effect is never folded into `done`."""
        drafts = ScriptedDraftPort([TransientToolError("store down")])
        executor = CountingExecutor(output={"draft_id": "drf_1"})
        graph, _handlers, cfg = build_graph(
            executor=executor, adapters=adapters_with(drafts=drafts)
        )

        final = await graph.ainvoke(
            seeded_state(
                make_plan("s1", ToolName.SAVE_DRAFT, SAVE_DRAFT_ARGS, status=StepStatus.PENDING)
            ),
            config=cfg,
        )

        response = final["final_response"]
        assert final["status"] is RunStatus.FAILED
        assert final["status"] is not RunStatus.COMPLETED
        assert "s1" not in response.done
        assert response.unconfirmed == ["s1"]
        assert "could not be confirmed" in response.summary or "unconfirmed" in response.summary

    @pytest.mark.asyncio
    async def test_outbound_unconfirmed_tells_the_operator_to_check_the_outbox(self) -> None:
        mail = ScriptedMailPort([TransientToolError("outbox unreachable")])
        executor = CountingExecutor(output={"message_id": "msg_1"})
        graph, _handlers, cfg = build_graph(executor=executor, adapters=adapters_with(mail=mail))

        final = await graph.ainvoke(
            seeded_state(
                make_plan(
                    "s1", ToolName.SEND_EMAIL_MOCK, SEND_EMAIL_ARGS, status=StepStatus.PENDING
                )
            ),
            config=cfg,
        )

        assert final["status_reason"] == "verification_unconfirmed"
        assert "check the outbox" in final["final_response"].summary


# ===========================================================================
# 7. STRUCTURAL
# ===========================================================================
class TestStructuralInvariants:
    def test_the_policy_module_is_pure(self) -> None:
        """No ports, no registry, no security, no I/O: it only reads state."""
        src = (APP / "agent" / "verification_recovery.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        modules = {
            n.module
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("app.")
        }
        assert modules == {"app.agent.state", "app.tools.contracts"}
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
        for banned in (".dispatch(", "ApprovalGate", "uow_factory", "adapters", "select("):
            assert banned not in src

    def test_recovery_introduces_no_second_state_machine(self) -> None:
        """The policy module is functions over the existing channels — no
        class holding recovery state, and no new graph."""
        tree = ast.parse((APP / "agent" / "verification_recovery.py").read_text(encoding="utf-8"))
        classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        assert {c.name for c in classes} == {"RetryTarget", "VerificationSafety"}
        assert all(any(getattr(b, "id", None) == "StrEnum" for b in c.bases) for c in classes)

    def test_recovery_keeps_no_process_local_state(self) -> None:
        """Every recovery input is read from `state`; nothing is stashed on
        the handler, or a resumed worker would decide differently."""
        for name in (
            "recover",
            "_recover_unconfirmed",
            "_skip_optional_step",
            "route_after_recover",
            "verify",
        ):
            src = inspect.getsource(getattr(NodeHandlers, name))
            tree = ast.parse(textwrap.dedent(src))
            assigns = [
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id == "self"
                and isinstance(n.ctx, ast.Store)
            ]
            assert not assigns, f"{name} writes process-local state"

    def test_verify_is_the_only_writer_of_verification_results(self) -> None:
        """`recover` reads the evidence; it never rewrites it. A recovery node
        that could edit a verification result could launder a failure."""
        tree = ast.parse((APP / "agent" / "nodes.py").read_text(encoding="utf-8"))
        writers: set[str] = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                # A write is the key of a returned dict literal, or the slice
                # of a subscript being stored into. A `state.get(...)` read is
                # neither.
                if isinstance(node, ast.Dict):
                    keys = {
                        k.value
                        for k in node.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    }
                    if "verification_result" in keys:
                        writers.add(fn.name)
                elif (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.ctx, ast.Store)
                    and isinstance(node.slice, ast.Constant)
                    and node.slice.value == "verification_result"
                ):
                    writers.add(fn.name)
        assert writers == {"verify", "execute_tool", "create_initial_state"}
        assert "recover" not in writers
        assert "_recover_unconfirmed" not in writers

    def test_recover_routes_only_into_declared_edges(self) -> None:
        src = inspect.getsource(NodeHandlers.route_after_recover)
        returned = {
            n.value.value
            for n in ast.walk(ast.parse(textwrap.dedent(src)))
            if isinstance(n, ast.Return)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        }
        assert returned == {"execute_tool", "verify", "plan", "decide", "fail"}

    def test_the_graph_declares_the_verify_retry_edge(self) -> None:
        graph = create_agent_graph(checkpointer=MemorySaver())
        assert "verify" in graph.get_graph().nodes
        edges = {(e.source, e.target) for e in graph.get_graph().edges}
        assert ("recover", "verify") in edges
        assert ("recover", "execute_tool") in edges

    def test_no_static_interrupt_lists_survive(self) -> None:
        graph = create_agent_graph(checkpointer=MemorySaver())
        assert not graph.interrupt_before_nodes
        assert not graph.interrupt_after_nodes

    def test_verifiers_still_never_mutate_or_mint(self) -> None:
        for path in (APP / "agent" / "verifiers").glob("*.py"):
            src = path.read_text(encoding="utf-8")
            for banned in ("ApprovalGate", ".send(", ".update(", ".save(", "dispatch("):
                assert banned not in src, f"{path.name} contains {banned}"


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------
def _verify_error(step_id: str, error_class: ErrorClass, result: VerificationResult) -> AgentError:
    """An `AgentError` shaped exactly as the `verify` node emits it."""
    return AgentError(
        step_id=step_id,
        error_class=error_class,
        message=result.detail or "verification outcome",
        attempt=1,
        detail={
            "source": "verify",
            "verification_attempt": result.attempt,
            "verification_status": result.status.value,
            "checks": [c.model_dump() for c in result.checks],
        },
    )


def _merge(state: AgentState, delta: dict[str, Any]) -> dict[str, Any]:
    """Apply a node delta the way the declared reducers would."""
    merged = dict(delta)
    if "errors" in delta:
        merged["errors"] = [*state.get("errors", []), *delta["errors"]]
    if "verification_result" in delta:
        merged["verification_result"] = {
            **state.get("verification_result", {}),
            **delta["verification_result"],
        }
    if "retry_count" in delta:
        merged["retry_count"] = {**state.get("retry_count", {}), **delta["retry_count"]}
    return merged
