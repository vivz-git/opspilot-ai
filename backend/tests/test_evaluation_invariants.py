"""EVAL-004: the seven global invariants of §15.6, asserted after every case.

Unit: `evaluate_invariants` is pure, so each invariant gets one in-memory
evidence set that violates exactly it. Integration: a real case is run
through the real path, then its persisted evidence is tampered in
PostgreSQL the way a bug would leave it — an approval flipped, a customer
edited without a gate, an attempt past the budget, a run left running, a
trace gap, a policy violation — and `EvaluationRunner.check_invariants`
re-read from the database must catch precisely that invariant. Finally the
whole suite proves the seven valid cases satisfy all seven invariants and
that the verdicts persist in `evaluation_results`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from app.agent.state import ApprovalStatus, RunStatus
from app.evaluation import load_registry
from app.evaluation.invariants import (
    INVARIANT_NAMES,
    InvariantEvidence,
    InvariantOutcome,
    evaluate_invariants,
)
from app.evaluation.registry import EvaluationRegistry
from app.evaluation.runner import CaseResult, EvaluationRunner
from app.evaluation.schemas import CustomerFixture, EvalCase
from app.persistence.checkpointing import open_checkpointer
from app.persistence.mock_crm import Customer, EmailOutbox, EmailOutboxStatus
from app.persistence.models import (
    AgentRun,
    ApprovalRow,
    ExecutionStep,
    ToolCallRow,
    ToolCallStatus,
    TraceEvent,
    TraceEventKind,
    TraceEventSeverity,
)
from app.persistence.session import create_session_factory, unit_of_work
from app.tools.contracts import RiskLevel, ToolName
from app.tools.schemas import CustomerStatus
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import migrate_to_head, require_database, settings

# ---------------------------------------------------------------------------
# Unit: one violating evidence set per invariant
# ---------------------------------------------------------------------------
_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_STEP_UUID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_APPROVAL_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
_HASH = "sha256:send"
_KEY = "idem:run:s6:send"


def _fixture_customer() -> CustomerFixture:
    return CustomerFixture(
        customer_id="cust_1",
        account_name="Northwind",
        primary_contact="Dana",
        email="dana@northwind.example",
        status=CustomerStatus.ACTIVE,
        mrr=Decimal("100.00"),
        version=1,
        created_at=_T0,
        updated_at=_T0,
    )


def _customer_row(fixture: CustomerFixture, **overrides: object) -> Customer:
    values = fixture.model_dump()
    values.update(overrides)
    return Customer(**values)


def _call(step_id: str, attempt: int, tool: ToolName, **overrides: object) -> ToolCallRow:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "run_id": _RUN_ID,
        "execution_step_id": _STEP_UUID,
        "step_id": step_id,
        "attempt": attempt,
        "tool": tool.value,
        "input": {},
        "input_hash": _HASH,
        "status": ToolCallStatus.SUCCEEDED,
        "idempotency_key": _KEY,
        "started_at": _T0,
    }
    values.update(overrides)
    return ToolCallRow(**values)


def _event(seq: int, kind: TraceEventKind = TraceEventKind.NODE_ENTERED) -> TraceEvent:
    return TraceEvent(id=seq, run_id=_RUN_ID, seq=seq, kind=kind, ts=_T0, payload={})


def _valid() -> InvariantEvidence:
    """A completed run that sent one approved email: every invariant holds."""
    approval = ApprovalRow(
        id=_APPROVAL_ID,
        run_id=_RUN_ID,
        step_id="s6",
        tool=ToolName.SEND_EMAIL_MOCK.value,
        risk=RiskLevel.HIGH,
        title="t",
        summary="s",
        payload_preview={},
        args_hash=_HASH,
        status=ApprovalStatus.APPROVED,
        requested_at=_T0,
        expires_at=_T0 + timedelta(hours=1),
    )
    outbox = EmailOutbox(
        outbox_id="out_1",
        message_id="msg_1",
        draft_id="d_1",
        to_email="dana@northwind.example",
        subject="s",
        body="b",
        status=EmailOutboxStatus.SENT,
        idempotency_key=_KEY,
        run_id=str(_RUN_ID),
        approval_id=str(_APPROVAL_ID),
    )
    fixture = _fixture_customer()
    return InvariantEvidence(
        run=AgentRun(
            id=_RUN_ID,
            status=RunStatus.COMPLETED,
            status_reason=None,
            deadline_at=_T0 + timedelta(minutes=5),
            finished_at=_T0 + timedelta(seconds=3),
            lease_owner=None,
        ),
        tool_calls=[_call("s6", 1, ToolName.SEND_EMAIL_MOCK)],
        approvals=[approval],
        events=[_event(1), _event(2), _event(3)],
        steps=[ExecutionStep(step_id="s6", attempts=0)],
        outbox=[outbox],
        outbox_total=1,
        customer_rows=[_customer_row(fixture)],
        customers_total=1,
        seeded_customers=[fixture],
        max_retries=2,
        max_steps=25,
        policy_violation_expected=False,
    )


def _only_violated(outcomes: tuple[InvariantOutcome, ...], number: int) -> InvariantOutcome:
    """Exactly invariant `number` failed; the report is well-formed."""
    by_number = {o.invariant: o for o in outcomes}
    assert [o.invariant for o in outcomes] == [1, 2, 3, 4, 5, 6, 7]
    assert {n for n, o in by_number.items() if not o.passed} == {number}, [
        (o.invariant, o.detail) for o in outcomes if not o.passed
    ]
    failed = by_number[number]
    assert failed.name == INVARIANT_NAMES[number]
    assert failed.evidence["violations"], "a failure must carry its violating evidence"
    assert failed.evidence["run_id"] == str(_RUN_ID)
    assert "violation" in failed.detail
    return failed


@pytest.mark.unit
class TestEachInvariantCatchesItsViolation:
    def test_valid_evidence_satisfies_all_seven(self) -> None:
        outcomes = evaluate_invariants(_valid())
        assert [(o.invariant, o.passed) for o in outcomes] == [(n, True) for n in range(1, 8)]
        assert all(o.evidence["violations"] == [] for o in outcomes)

    def test_1_outbox_row_whose_approval_was_not_approved(self) -> None:
        ev = _valid()
        ev.approvals[0].status = ApprovalStatus.REJECTED
        failed = _only_violated(evaluate_invariants(ev), 1)
        assert failed.evidence["violations"][0]["problems"] == ["approval status is rejected"]

    def test_1_outbox_row_whose_args_hash_differs_from_the_producing_attempt(self) -> None:
        ev = _valid()
        ev.approvals[0].args_hash = "sha256:something-else"
        failed = _only_violated(evaluate_invariants(ev), 1)
        assert "args_hash" in failed.evidence["violations"][0]["problems"][0]

    def test_1_outbox_row_no_approval_can_account_for(self) -> None:
        ev = replace(_valid(), outbox_total=2)  # one row not attributable to the run
        ev.outbox[0].approval_id = None
        failed = _only_violated(evaluate_invariants(ev), 1)
        problems = failed.evidence["violations"]
        assert problems[0]["problems"] == ["no approval of this run with that approval_id"]
        assert problems[1] == {"outbox_rows_not_attributed_to_run": 1}

    def test_2_customer_modified_without_an_approved_approval(self) -> None:
        ev = _valid()
        ev.customer_rows[0].version = 2
        failed = _only_violated(evaluate_invariants(ev), 2)
        assert failed.evidence["modified_customers"] == ["cust_1"]
        assert failed.evidence["violations"][0]["customer_id"] == "cust_1"

    def test_2_customer_modified_under_an_approved_update_is_allowed(self) -> None:
        ev = _valid()
        ev.customer_rows[0].version = 2
        update = _call(
            "s7",
            1,
            ToolName.UPDATE_CUSTOMER,
            input={"customer_id": "cust_1"},
            input_hash="sha256:update",
            idempotency_key="idem:run:s7:update",
        )
        approval = ApprovalRow(
            id=uuid.uuid4(),
            run_id=_RUN_ID,
            step_id="s7",
            tool=ToolName.UPDATE_CUSTOMER.value,
            args_hash="sha256:update",
            status=ApprovalStatus.APPROVED,
        )
        ev = replace(ev, tool_calls=[*ev.tool_calls, update], approvals=[*ev.approvals, approval])
        outcome = evaluate_invariants(ev)[1]
        assert outcome.passed and outcome.evidence["modified_customers"] == ["cust_1"]

    def test_3_step_with_more_attempts_than_one_plus_max_retries(self) -> None:
        ev = _valid()
        extra = [
            _call("s6", n, ToolName.SEND_EMAIL_MOCK, status=ToolCallStatus.FAILED)
            for n in (2, 3, 4)
        ]
        ev = replace(ev, tool_calls=[*ev.tool_calls, *extra])
        failed = _only_violated(evaluate_invariants(ev), 3)
        assert failed.evidence["violations"][0]["attempts"] == [1, 2, 3, 4]
        assert failed.evidence["violations"][0]["limit"] == 3

    def test_3_execution_steps_attempts_column_is_also_bounded(self) -> None:
        ev = _valid()
        ev.steps[0].attempts = 4
        failed = _only_violated(evaluate_invariants(ev), 3)
        assert failed.evidence["violations"][0]["execution_steps.attempts"] == 4

    def test_4_run_past_its_deadline_or_step_budget(self) -> None:
        ev = _valid()
        ev.run.deadline_at = _T0 - timedelta(seconds=1)
        failed = _only_violated(evaluate_invariants(ev), 4)
        kinds = [next(iter(v)) for v in failed.evidence["violations"]]
        assert kinds == ["attempts_started_after_deadline", "finished_at"]

        failed = _only_violated(evaluate_invariants(replace(_valid(), max_steps=0)), 4)
        assert failed.evidence["violations"] == [{"dispatched_attempts": 1, "max_steps": 0}]

    def test_5_run_left_non_terminal(self) -> None:
        ev = _valid()
        ev.run.status = RunStatus.AWAITING_APPROVAL
        failed = _only_violated(evaluate_invariants(ev), 5)
        assert failed.evidence["violations"][0]["problems"] == ["not terminal"]

        ev = _valid()
        ev.run.lease_owner = "worker-1"
        failed = _only_violated(evaluate_invariants(ev), 5)
        assert failed.evidence["violations"][0]["problems"] == ["lease still held"]

    def test_6_trace_seq_with_a_gap_or_out_of_order(self) -> None:
        ev = replace(_valid(), events=[_event(1), _event(3)])
        failed = _only_violated(evaluate_invariants(ev), 6)
        assert failed.evidence["violations"][0]["missing"] == [2]

        swapped = [_event(1), _event(2), _event(3)]
        swapped[1].seq, swapped[2].seq = 3, 2  # inserted out of order
        failed = _only_violated(evaluate_invariants(replace(_valid(), events=swapped)), 6)
        assert failed.evidence["violations"][0]["out_of_order"] == [[1, 3], [3, 2]]

    def test_7_policy_violation_the_case_did_not_expect(self) -> None:
        events = [*_valid().events, _event(4, TraceEventKind.POLICY_VIOLATION)]
        failed = _only_violated(evaluate_invariants(replace(_valid(), events=events)), 7)
        assert failed.evidence["policy_violations"][0]["seq"] == 4

        expected = replace(_valid(), events=events, policy_violation_expected=True)
        assert evaluate_invariants(expected)[6].passed

    def test_outcomes_are_deterministic(self) -> None:
        assert evaluate_invariants(_valid()) == evaluate_invariants(_valid())


# ---------------------------------------------------------------------------
# Integration: real runs, tampered evidence, re-read from PostgreSQL
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def _database() -> None:
    require_database()
    migrate_to_head()


@pytest.fixture(scope="module")
def registry() -> EvaluationRegistry:
    return load_registry()


@pytest.fixture
async def engine(_database: None) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        settings().database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=20,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    async with open_checkpointer(settings()) as saver:
        yield saver


@pytest.fixture
def runner(
    engine: AsyncEngine, checkpointer: AsyncPostgresSaver, registry: EvaluationRegistry
) -> EvaluationRunner:
    return EvaluationRunner(
        settings=settings(),
        session_factory=create_session_factory(engine),
        checkpointer=checkpointer,
        registry=registry,
    )


def _violated(outcomes: tuple[InvariantOutcome, ...]) -> set[int]:
    return {o.invariant for o in outcomes if not o.passed}


async def _run_valid(
    runner: EvaluationRunner, registry: EvaluationRegistry, case_id: str
) -> CaseResult:
    result = await runner.run_case(registry.case(case_id))
    assert result.passed and not result.violations, [
        (i.invariant, i.detail) for i in result.violations
    ]
    return result


async def _tamper(engine: AsyncEngine, sql: str, **params: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.text(sql), params)


@pytest.mark.integration
class TestInvariantsOverPersistedEvidence:
    async def test_1_an_outbox_row_whose_approval_is_no_longer_approved(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        case = registry.case("approval_required")
        result = await _run_valid(runner, registry, case.id)
        await _tamper(
            engine,
            "UPDATE opspilot.approvals SET status = 'rejected' WHERE run_id = :run_id",
            run_id=result.run_id,
        )
        outcomes = await runner.check_invariants(case, result.run_id)
        assert _violated(outcomes) == {1}
        violation = outcomes[0].evidence["violations"][0]
        assert violation["problems"] == ["approval status is rejected"]
        assert violation["to_email"] == "dana@northwind.example"
        assert violation["producing_tool_call_id"] is not None

    async def test_2_a_customer_edited_behind_the_gate(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        case = registry.case("company_research")
        result = await _run_valid(runner, registry, case.id)
        await _tamper(
            engine,
            "UPDATE mock_crm.customers SET version = version + 1, notes = 'tampered' "
            "WHERE customer_id = 'cust_2'",
        )
        outcomes = await runner.check_invariants(case, result.run_id)
        assert _violated(outcomes) == {2}
        assert outcomes[1].evidence["modified_customers"] == ["cust_2"]
        assert outcomes[1].evidence["violations"][0]["version"] == 2

    async def test_3_an_attempt_beyond_one_plus_max_retries(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        case = registry.case("company_research")
        result = await _run_valid(runner, registry, case.id)
        limit = 1 + (case.given.budgets or settings().budgets).max_retries
        await _tamper(
            engine,
            "INSERT INTO opspilot.tool_calls "
            "(run_id, execution_step_id, step_id, attempt, tool, status, started_at) "
            "SELECT run_id, execution_step_id, step_id, :attempt, tool, 'failed', started_at "
            "FROM opspilot.tool_calls WHERE run_id = :run_id AND step_id = 's1' AND attempt = 1",
            run_id=result.run_id,
            attempt=limit + 1,
        )
        outcomes = await runner.check_invariants(case, result.run_id)
        assert _violated(outcomes) == {3}
        assert outcomes[2].evidence["violations"][0]["attempts"] == [1, limit + 1]

    async def test_4_work_past_the_deadline(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        case = registry.case("company_research")
        result = await _run_valid(runner, registry, case.id)
        await _tamper(
            engine,
            "UPDATE opspilot.agent_runs SET deadline_at = deadline_at - interval '1 hour' "
            "WHERE id = :run_id",
            run_id=result.run_id,
        )
        outcomes = await runner.check_invariants(case, result.run_id)
        assert _violated(outcomes) == {4}
        assert outcomes[3].evidence["violations"][0]["attempts_started_after_deadline"]

    async def test_5_a_run_left_running(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        case = registry.case("company_research")
        result = await _run_valid(runner, registry, case.id)
        await _tamper(
            engine,
            "UPDATE opspilot.agent_runs SET status = 'running', finished_at = NULL "
            "WHERE id = :run_id",
            run_id=result.run_id,
        )
        outcomes = await runner.check_invariants(case, result.run_id)
        assert _violated(outcomes) == {5}
        assert outcomes[4].evidence["status"] == "running"

    async def test_6_a_gap_in_the_trace(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        case = registry.case("company_research")
        result = await _run_valid(runner, registry, case.id)
        await _tamper(
            engine,
            "DELETE FROM opspilot.trace_events WHERE run_id = :run_id AND seq = 2",
            run_id=result.run_id,
        )
        outcomes = await runner.check_invariants(case, result.run_id)
        assert _violated(outcomes) == {6}
        assert outcomes[5].evidence["violations"][0]["missing"] == [2]

    async def test_7_a_policy_violation_the_case_did_not_expect(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        case = registry.case("company_research")
        result = await _run_valid(runner, registry, case.id)
        async with unit_of_work(create_session_factory(engine)) as uow:
            await uow.trace_events.append(
                run_id=result.run_id,
                kind=TraceEventKind.POLICY_VIOLATION,
                severity=TraceEventSeverity.ERROR,
                node="execute_tool",
                tool=ToolName.SEND_EMAIL_MOCK,
                step_id="s9",
                error={"class": "policy_violation", "message": "planted"},
            )
            await uow.commit()
        outcomes = await runner.check_invariants(case, result.run_id)
        assert _violated(outcomes) == {7}
        planted = outcomes[6].evidence["policy_violations"][0]
        assert (planted["step_id"], planted["tool"]) == ("s9", "send_email_mock")

    async def test_a_violated_invariant_fails_the_case_whatever_its_assertions_say(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        """The scripted human never answers: the case's own `final_status`
        assertion fails *and* invariant 5 reports the run left paused —
        the two verdicts are recorded side by side, neither replaces the other."""
        case = registry.case("approval_required")
        paused = EvalCase.model_validate(
            {
                **case.model_dump(),
                "given": {**case.given.model_dump(), "approvals": {"policy": "never"}},
            }
        )
        result = await runner.run_case(paused)
        assert not result.passed
        assert result.final_status is RunStatus.AWAITING_APPROVAL
        assert {a.name for a in result.failures} >= {"final_status", "terminal"}
        assert [i.invariant for i in result.violations] == [5]
        assert result.violations[0].evidence["status"] == "awaiting_approval"

    async def test_the_seven_valid_cases_satisfy_all_seven_invariants_and_persist_them(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        suite = await runner.run_suite("all")
        assert [r.case_id for r in suite] == list(registry.suites["all"].cases)
        for result in suite:
            assert result.passed, [(a.name, a.detail) for a in result.failures]
            assert [i.invariant for i in result.invariants] == [1, 2, 3, 4, 5, 6, 7]
            assert not result.violations, [(i.invariant, i.detail) for i in result.violations]
        # The sends are the invariant-1 evidence: exactly the two approved cases.
        sends = {r.case_id: r.invariants[0].evidence["outbox_rows"] for r in suite}
        assert {k for k, n in sends.items() if n} == {"happy_path_multi_step", "approval_required"}
        assert suite.metrics["case_pass_rate"] == 1.0

        assert suite.evaluation_run_id is not None
        async with unit_of_work(create_session_factory(engine)) as uow:
            persisted = await uow.evaluations.list_results(suite.evaluation_run_id)
            await uow.commit()
        assert len(persisted) == 7
        for row in persisted:
            recorded = [a for a in row.assertions if "invariant" in a]
            assert [a["invariant"] for a in recorded] == [1, 2, 3, 4, 5, 6, 7]
            assert all(a["passed"] for a in recorded)
            assert all(a["evidence"]["run_id"] == str(row.run_id) for a in recorded)
            assert all(a["name"].startswith(f"invariant[{a['invariant']}] ") for a in recorded)
