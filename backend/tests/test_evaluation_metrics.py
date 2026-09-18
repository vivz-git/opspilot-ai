"""EVAL-003: Evaluation metrics computation and persistence (§15.4).

Tests cover:
- all seven current cases
- mixed pass/fail case results
- expected rejection counting as case pass
- expected failure counting as case pass
- task_success_rate excluding non-completion cases from its denominator
- approval wait excluded from agent_duration_ms
- zero/empty-set edge cases where contractually applicable
- persisted metrics round-trip from PostgreSQL
- deterministic metric calculation
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from app.agent.state import ApprovalStatus, PlannerKind, RunStatus
from app.evaluation import (
    CaseMetricInput,
    EvaluationRegistry,
    SuiteRunResult,
    calculate_agent_duration_ms,
    calculate_approval_wait_ms,
    calculate_case_pass_rate,
    calculate_task_success_rate,
    compute_evaluation_metrics,
    load_registry,
)
from app.evaluation.runner import EvaluationRunner
from app.persistence.checkpointing import open_checkpointer
from app.persistence.models import (
    ApprovalRow,
    EvaluationRunStatus,
    TraceEvent,
    TraceEventKind,
)
from app.persistence.session import create_session_factory, unit_of_work
from app.tools.contracts import RiskLevel, ToolName
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import migrate_to_head, require_database, settings


# ---------------------------------------------------------------------------
# Unit: Metric Formulas and Edge Cases
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestMetricFormulas:
    def test_case_pass_rate_formula(self) -> None:
        assert calculate_case_pass_rate(7, 7) == 1.0
        assert calculate_case_pass_rate(0, 7) == 0.0
        assert calculate_case_pass_rate(5, 7) == 5 / 7
        assert calculate_case_pass_rate(0, 0) == 0.0

    def test_task_success_rate_formula(self) -> None:
        assert calculate_task_success_rate(5, 5) == 1.0
        assert calculate_task_success_rate(4, 5) == 0.8
        assert calculate_task_success_rate(0, 5) == 0.0
        assert calculate_task_success_rate(0, 0) == 0.0

    def test_expected_rejection_counts_as_case_pass(self) -> None:
        """A case expecting rejection that terminates in rejected is a PASS (§15.4)."""
        case_input = CaseMetricInput(
            case_id="approval_rejected",
            passed=True,
            expected_final_status=RunStatus.REJECTED,
            actual_final_status=RunStatus.REJECTED,
            duration_ms=1000,
            agent_duration_ms=1000,
        )
        metrics = compute_evaluation_metrics([case_input])
        assert metrics["case_pass_rate"] == 1.0
        assert metrics["passed_cases"] == 1
        assert metrics["failed_cases"] == 0
        # Excluded from completion denominator
        assert metrics["completion_expected_cases"] == 0
        assert metrics["task_success_rate"] == 0.0

    def test_expected_failure_counts_as_case_pass(self) -> None:
        """A case expecting failure that terminates in failed is a PASS (§15.4)."""
        case_input = CaseMetricInput(
            case_id="invalid_tool_result",
            passed=True,
            expected_final_status=RunStatus.FAILED,
            actual_final_status=RunStatus.FAILED,
            duration_ms=800,
            agent_duration_ms=800,
            status_reason="verification_failed",
        )
        metrics = compute_evaluation_metrics([case_input])
        assert metrics["case_pass_rate"] == 1.0
        assert metrics["passed_cases"] == 1
        assert metrics["completion_expected_cases"] == 0
        assert metrics["task_success_rate"] == 0.0

    def test_task_success_rate_excludes_non_completion_cases_from_denominator(self) -> None:
        """Canonical 7-case scenario: 5 completion cases, 1 rejection, 1 failure.

        Rejection and failure cases must NOT reduce task_success_rate merely
        because they ended in rejected/failed.
        """
        cases = [
            CaseMetricInput("c1", True, RunStatus.COMPLETED, RunStatus.COMPLETED, 1000, 1000),
            CaseMetricInput("c2", True, RunStatus.COMPLETED, RunStatus.COMPLETED, 1000, 1000),
            CaseMetricInput("c3", True, RunStatus.COMPLETED, RunStatus.COMPLETED, 1000, 1000),
            CaseMetricInput("c4", True, RunStatus.COMPLETED, RunStatus.COMPLETED, 1000, 1000),
            CaseMetricInput("c5", True, RunStatus.COMPLETED, RunStatus.COMPLETED, 1000, 1000),
            CaseMetricInput(
                "c6",
                True,
                RunStatus.REJECTED,
                RunStatus.REJECTED,
                500,
                500,
                status_reason="rejected",
            ),
            CaseMetricInput(
                "c7",
                True,
                RunStatus.FAILED,
                RunStatus.FAILED,
                500,
                500,
                status_reason="verification_failed",
            ),
        ]
        metrics = compute_evaluation_metrics(cases)
        assert metrics["case_pass_rate"] == 1.0
        assert metrics["total_cases"] == 7
        assert metrics["passed_cases"] == 7
        assert metrics["completion_expected_cases"] == 5
        assert metrics["completed_runs"] == 5
        # Denominator is 5, NOT 7!
        assert metrics["task_success_rate"] == 1.0

    def test_task_success_rate_when_completion_case_fails(self) -> None:
        """When 1 of 5 completion cases fails, task_success_rate is 4/5 (0.8)."""
        cases = [
            CaseMetricInput("c1", True, RunStatus.COMPLETED, RunStatus.COMPLETED, 1000, 1000),
            CaseMetricInput("c2", True, RunStatus.COMPLETED, RunStatus.COMPLETED, 1000, 1000),
            CaseMetricInput("c3", True, RunStatus.COMPLETED, RunStatus.COMPLETED, 1000, 1000),
            CaseMetricInput("c4", True, RunStatus.COMPLETED, RunStatus.COMPLETED, 1000, 1000),
            # c5 failed to complete
            CaseMetricInput(
                "c5",
                False,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                1000,
                1000,
                status_reason="budget_exhausted",
            ),
            # c6 and c7 are intentional rejection / failure
            CaseMetricInput("c6", True, RunStatus.REJECTED, RunStatus.REJECTED, 500, 500),
            CaseMetricInput("c7", True, RunStatus.FAILED, RunStatus.FAILED, 500, 500),
        ]
        metrics = compute_evaluation_metrics(cases)
        assert metrics["case_pass_rate"] == 6 / 7
        assert metrics["completion_expected_cases"] == 5
        assert metrics["completed_runs"] == 4
        assert metrics["task_success_rate"] == 0.8

    def test_mixed_pass_fail_case_results(self) -> None:
        cases = [
            CaseMetricInput("c1", True, RunStatus.COMPLETED, RunStatus.COMPLETED, 1000, 1000),
            CaseMetricInput("c2", False, RunStatus.COMPLETED, RunStatus.FAILED, 1000, 1000),
            CaseMetricInput("c3", True, RunStatus.REJECTED, RunStatus.REJECTED, 500, 500),
            CaseMetricInput("c4", False, RunStatus.FAILED, RunStatus.COMPLETED, 800, 800),
        ]
        metrics = compute_evaluation_metrics(cases)
        assert metrics["total_cases"] == 4
        assert metrics["passed_cases"] == 2
        assert metrics["failed_cases"] == 2
        assert metrics["case_pass_rate"] == 0.5
        assert metrics["completion_expected_cases"] == 2
        assert metrics["completed_runs"] == 1
        assert metrics["task_success_rate"] == 0.5

    def test_zero_empty_set_edge_cases(self) -> None:
        metrics = compute_evaluation_metrics([])
        assert metrics["case_pass_rate"] == 0.0
        assert metrics["task_success_rate"] == 0.0
        assert metrics["total_cases"] == 0
        assert metrics["agent_duration_ms"] == 0.0
        assert metrics["p50_duration_ms"] == 0.0
        assert metrics["p95_duration_ms"] == 0.0

    def test_deterministic_metric_calculation(self) -> None:
        cases = [
            CaseMetricInput("c1", True, RunStatus.COMPLETED, RunStatus.COMPLETED, 1200, 1000, 200),
            CaseMetricInput("c2", True, RunStatus.COMPLETED, RunStatus.COMPLETED, 1500, 1100, 400),
            CaseMetricInput(
                "c3",
                False,
                RunStatus.REJECTED,
                RunStatus.FAILED,
                800,
                800,
                status_reason="unexpected_fail",
            ),
        ]
        first = compute_evaluation_metrics(cases)
        for _ in range(50):
            again = compute_evaluation_metrics(cases)
            assert first == again


# ---------------------------------------------------------------------------
# Unit: Approval Wait and Agent Duration
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestApprovalWaitAndAgentDuration:
    def test_approval_wait_excluded_from_agent_duration_ms(self) -> None:
        assert calculate_agent_duration_ms(10_000, 8_000) == 2_000
        assert calculate_agent_duration_ms(5_000, 0) == 5_000
        assert calculate_agent_duration_ms(None, 1_000) == 0
        # If approval wait exceeds run duration (edge case/clock skew), bounded to 0
        assert calculate_agent_duration_ms(2_000, 5_000) == 0

    def test_approval_wait_calculation_from_approvals(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        run_id = uuid.uuid4()

        a1 = ApprovalRow(
            id=uuid.uuid4(),
            run_id=run_id,
            step_id="s1",
            tool=ToolName.SEND_EMAIL_MOCK,
            risk=RiskLevel.HIGH,
            title="Send email",
            summary="Send email",
            args_hash="h1",
            status=ApprovalStatus.APPROVED,
            requested_at=t0,
            expires_at=t0 + timedelta(minutes=10),
            decided_at=t0 + timedelta(seconds=5),  # 5,000 ms
        )
        a2 = ApprovalRow(
            id=uuid.uuid4(),
            run_id=run_id,
            step_id="s2",
            tool=ToolName.UPDATE_CUSTOMER,
            risk=RiskLevel.HIGH,
            title="Update customer",
            summary="Update customer",
            args_hash="h2",
            status=ApprovalStatus.REJECTED,
            requested_at=t0 + timedelta(seconds=10),
            expires_at=t0 + timedelta(minutes=10),
            decided_at=t0 + timedelta(seconds=13),  # 3,000 ms
        )
        wait_ms = calculate_approval_wait_ms([a1, a2])
        assert wait_ms == 8_000

    def test_approval_wait_calculation_from_expired_approval(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        run_id = uuid.uuid4()
        a = ApprovalRow(
            id=uuid.uuid4(),
            run_id=run_id,
            step_id="s1",
            tool=ToolName.SEND_EMAIL_MOCK,
            risk=RiskLevel.HIGH,
            title="Send email",
            summary="Send email",
            args_hash="h1",
            status=ApprovalStatus.EXPIRED,
            requested_at=t0,
            expires_at=t0 + timedelta(seconds=15),
            decided_at=None,
        )
        wait_ms = calculate_approval_wait_ms([a])
        assert wait_ms == 15_000

    def test_approval_wait_calculation_from_trace_events(self) -> None:
        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        run_id = uuid.uuid4()
        events = [
            TraceEvent(
                id=1,
                run_id=run_id,
                seq=1,
                ts=t0,
                kind=TraceEventKind.APPROVAL_REQUESTED,
                step_id="s1",
                payload={"approval_id": "app-123"},
            ),
            TraceEvent(
                id=2,
                run_id=run_id,
                seq=2,
                ts=t0 + timedelta(seconds=7),
                kind=TraceEventKind.APPROVAL_GRANTED,
                step_id="s1",
                payload={"approval_id": "app-123"},
            ),
        ]
        wait_ms = calculate_approval_wait_ms(events=events)
        assert wait_ms == 7_000


# ---------------------------------------------------------------------------
# Integration: PostgreSQL Round-Trip and Full Suite Metrics
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


@pytest.mark.integration
class TestPostgresPersistenceAndRoundTrip:
    async def test_persisted_metrics_round_trip_from_postgresql(self, engine: AsyncEngine) -> None:
        """Verify evaluation metrics snapshot and results round-trip from PostgreSQL."""
        session_factory = create_session_factory(engine)
        eval_run_id = uuid.uuid4()
        run_id = uuid.uuid4()

        expected_metrics = {
            "case_pass_rate": 0.8571,
            "task_success_rate": 1.0,
            "agent_duration_ms": 1420.5,
            "avg_duration_ms": 1420.5,
            "p50_duration_ms": 1350.0,
            "p95_duration_ms": 2100.0,
            "total_cases": 7,
            "passed_cases": 6,
            "failed_cases": 1,
            "completion_expected_cases": 5,
            "completed_runs": 5,
            "approval_wait_ms": 3200,
            "failed_runs": 1,
            "failure_mix": {"verification_failed": 1},
        }

        async with unit_of_work(session_factory) as uow:
            eval_run = await uow.evaluations.create_run(
                suite="smoke",
                planner_kind=PlannerKind.RULES,
                id=eval_run_id,
            )
            assert eval_run.id == eval_run_id

            # Create an agent_run to satisfy the FK on evaluation_results.run_id
            await uow.agent_runs.create(
                user_request="test request",
                deadline_at=datetime.now(UTC) + timedelta(minutes=5),
                id=run_id,
                evaluation_run_id=eval_run_id,
                eval_case_id="happy_path_multi_step",
            )

            result = await uow.evaluations.record_result(
                evaluation_run_id=eval_run_id,
                case_id="happy_path_multi_step",
                run_id=run_id,
                passed=True,
                assertions=[{"name": "status", "passed": True, "detail": ""}],
                duration_ms=1350,
                retry_count=0,
                tool_calls_count=6,
                approval_outcome="approved",
            )
            assert result.passed is True
            assert result.duration_ms == 1350

            completed = await uow.evaluations.complete_run(
                eval_run_id,
                status=EvaluationRunStatus.COMPLETED,
                finished_at=datetime.now(UTC),
                case_count=7,
                passed=6,
                failed=1,
                metrics=expected_metrics,
            )
            assert completed is not None
            await uow.commit()

        # Read back in a fresh session and verify round-trip
        async with unit_of_work(session_factory) as uow:
            fetched_run = await uow.evaluations.get_run(eval_run_id)
            assert fetched_run is not None
            assert fetched_run.status == EvaluationRunStatus.COMPLETED
            assert fetched_run.case_count == 7
            assert fetched_run.passed == 6
            assert fetched_run.failed == 1
            assert fetched_run.metrics == expected_metrics

            results = await uow.evaluations.list_results(eval_run_id)
            assert len(results) == 1
            assert results[0].case_id == "happy_path_multi_step"
            assert results[0].passed is True
            assert results[0].duration_ms == 1350
            await uow.commit()

    async def test_suite_execution_persists_metrics_and_results_for_all_seven_cases(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        """Run all seven canonical cases, asserting metrics computation and DB persistence."""
        started = time.monotonic()
        suite_res = await runner.run_suite("all")
        elapsed = time.monotonic() - started

        assert isinstance(suite_res, SuiteRunResult)
        assert len(suite_res) == 7
        assert all(r.passed for r in suite_res)
        assert suite_res.evaluation_run_id is not None
        assert elapsed < 60

        metrics = suite_res.metrics
        assert metrics["total_cases"] == 7
        assert metrics["passed_cases"] == 7
        assert metrics["failed_cases"] == 0
        assert metrics["case_pass_rate"] == 1.0

        # Exactly 5 cases expect completion; non-completion cases excluded from denominator
        assert metrics["completion_expected_cases"] == 5
        assert metrics["completed_runs"] == 5
        assert metrics["task_success_rate"] == 1.0

        # agent_duration_ms is present and non-negative
        assert "agent_duration_ms" in metrics
        assert metrics["agent_duration_ms"] >= 0
        assert "avg_duration_ms" in metrics
        assert "p50_duration_ms" in metrics
        assert "p95_duration_ms" in metrics

        # Verify persisted state in PostgreSQL
        async with unit_of_work(create_session_factory(engine)) as uow:
            db_eval_run = await uow.evaluations.get_run(suite_res.evaluation_run_id)
            assert db_eval_run is not None
            assert db_eval_run.status == EvaluationRunStatus.COMPLETED
            assert db_eval_run.case_count == 7
            assert db_eval_run.passed == 7
            assert db_eval_run.failed == 0
            assert db_eval_run.metrics["case_pass_rate"] == 1.0
            assert db_eval_run.metrics["task_success_rate"] == 1.0

            db_results = await uow.evaluations.list_results(suite_res.evaluation_run_id)
            assert len(db_results) == 7
            db_cases = {r.case_id: r for r in db_results}
            assert set(db_cases) == set(registry.suites["all"].cases)
            assert all(r.passed for r in db_results)

            # Check individual case statuses and properties
            assert db_cases["approval_rejected"].approval_outcome == "rejected"
            assert db_cases["approval_required"].approval_outcome == "approved"
            assert db_cases["retryable_failure"].retry_count == 2
            await uow.commit()
