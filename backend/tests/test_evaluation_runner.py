"""EVAL-002: the evaluation runner drives the real service path (§15.2, §15.5).

The integration tests run the canonical `backend/evals/` suite against real
PostgreSQL, the real LangGraph saver, the production graph, the real
`ToolRegistry` and the real approval gate: every approval is a decided
`approvals` row, every send is an outbox row traceable to it, every retry
is a `tool_calls` row, and the whole suite finishes in seconds because the
clock is virtual.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from app.agent.state import ApprovalDecisionKind, ApprovalStatus, RunStatus
from app.errors import ConfigurationError, TransientToolError
from app.evaluation import load_registry
from app.evaluation.registry import EvaluationRegistry
from app.evaluation.runner import (
    ApprovalPolicy,
    CaseResult,
    EvaluationRunner,
    FailureInjector,
)
from app.evaluation.schemas import ApprovalsSpec, EvalCase, FailureInjection
from app.persistence.checkpointing import open_checkpointer
from app.persistence.session import create_session_factory, unit_of_work
from app.tools.contracts import ToolName
from app.tools.registry import ToolTimeoutError, default_implementations
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import migrate_to_head, require_database, settings


# ---------------------------------------------------------------------------
# Unit: the scripted human and the injector
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestApprovalPolicy:
    @pytest.mark.parametrize(
        ("spec", "decisions"),
        [
            (None, [None, None]),
            ({"policy": "never"}, [None, None]),
            ({"policy": "approve"}, [ApprovalDecisionKind.APPROVE] * 2),
            ({"policy": "reject"}, [ApprovalDecisionKind.REJECT] * 2),
            (
                {"policy": "approve_after", "after": 2},
                [
                    ApprovalDecisionKind.REJECT,
                    ApprovalDecisionKind.APPROVE,
                    ApprovalDecisionKind.APPROVE,
                ],
            ),
        ],
    )
    def test_decides_per_ordinal(self, spec: dict[str, Any] | None, decisions: list[Any]) -> None:
        policy = ApprovalPolicy(ApprovalsSpec.model_validate(spec) if spec else None)
        assert [policy.decide(n) for n in range(1, len(decisions) + 1)] == decisions


class _Ctx:
    def __init__(self, tool: ToolName, attempt: int) -> None:
        self.tool, self.attempt, self.step_id = tool, attempt, "s1"
        self.clock = type(
            "C", (), {"now": staticmethod(lambda: datetime(2026, 1, 1, tzinfo=UTC))}
        )()


class _Out(BaseModel):
    ok: bool = True


async def _real(_validated: Any, _ctx: Any) -> BaseModel:
    return _Out()


@pytest.mark.unit
class TestFailureInjector:
    def test_keys_by_tool_and_attempt(self) -> None:
        injector = FailureInjector(
            [
                FailureInjection(tool=ToolName.RESEARCH_COMPANY, kind="transient", attempts=[1, 2]),
                FailureInjection(tool=ToolName.SAVE_DRAFT, kind="lying_success", attempts="all"),
            ]
        )
        assert injector.kind_for(ToolName.RESEARCH_COMPANY, 1) is not None
        assert injector.kind_for(ToolName.RESEARCH_COMPANY, 3) is None
        assert injector.kind_for(ToolName.SAVE_DRAFT, 7) is not None
        assert injector.kind_for(ToolName.GET_LEAD, 1) is None

    async def test_wraps_only_injected_tools_and_only_named_attempts(self) -> None:
        injector = FailureInjector(
            [
                FailureInjection(tool=ToolName.RESEARCH_COMPANY, kind="transient", attempts=[1]),
                FailureInjection(tool=ToolName.GET_LEAD, kind="timeout", attempts=[2]),
            ]
        )
        wrapped = injector.wrap(
            {ToolName.RESEARCH_COMPANY: _real, ToolName.GET_LEAD: _real, ToolName.SCORE_LEAD: _real}
        )
        assert wrapped[ToolName.SCORE_LEAD] is _real
        with pytest.raises(TransientToolError):
            await wrapped[ToolName.RESEARCH_COMPANY](None, _Ctx(ToolName.RESEARCH_COMPANY, 1))
        assert isinstance(
            await wrapped[ToolName.RESEARCH_COMPANY](None, _Ctx(ToolName.RESEARCH_COMPANY, 2)), _Out
        )
        with pytest.raises(ToolTimeoutError):
            await wrapped[ToolName.GET_LEAD](None, _Ctx(ToolName.GET_LEAD, 2))

    def test_lying_success_is_scripted_for_save_draft_only(self) -> None:
        with pytest.raises(ConfigurationError, match="save_draft only"):
            FailureInjector(
                [
                    FailureInjection(
                        tool=ToolName.SEND_EMAIL_MOCK, kind="lying_success", attempts="all"
                    )
                ]
            )

    def test_wraps_the_real_bindings(self) -> None:
        wrapped = FailureInjector([]).wrap(default_implementations())
        assert set(wrapped) == set(default_implementations())


# ---------------------------------------------------------------------------
# Integration: the real path
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


def _failures(result: CaseResult) -> list[str]:
    return [f"{a.name}: {a.detail}" for a in result.failures]


@pytest.mark.integration
class TestTheSuiteDrivesTheRealPath:
    async def test_every_required_case_passes_in_seconds(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        started = time.monotonic()
        results = await runner.run_suite("all")
        elapsed = time.monotonic() - started
        assert [r.case_id for r in results] == list(registry.suites["all"].cases)
        assert all(r.passed for r in results), {r.case_id: _failures(r) for r in results}
        assert elapsed < 60, f"the suite took {elapsed:.1f}s"

        async with unit_of_work(create_session_factory(engine)) as uow:
            for result in results:
                run = await uow.agent_runs.get(result.run_id)
                assert run is not None and run.eval_case_id == result.case_id
                assert run.status is result.final_status
                assert run.seed == registry.case(result.case_id).given.seed
            await uow.commit()

    async def test_the_gate_is_real_and_the_send_traces_to_its_approval(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        result = await runner.run_case(registry.case("approval_required"))
        assert result.passed, _failures(result)
        async with unit_of_work(create_session_factory(engine)) as uow:
            [approval] = await uow.approvals.list_by_run(result.run_id)
            [sent] = await uow.email_outbox.list_by_run(str(result.run_id))
            kinds = [
                e.kind.value for e in await uow.trace_events.list_by_run(result.run_id, limit=1000)
            ]
            await uow.commit()
        assert approval.status is ApprovalStatus.APPROVED
        assert approval.decided_by == "eval:approval_required"
        assert approval.decision_reason == "Approved for the pilot."
        assert sent.approval_id == str(approval.id)
        assert kinds.index("approval_requested") < kinds.index("approval_granted")
        assert kinds.count("tool_started") == 6

    async def test_a_human_who_never_answers_leaves_the_run_paused_and_nothing_sent(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        """`never` is the proof the policy cannot bypass the gate: with no
        decision there is no token, no send and no terminal status."""
        case = registry.case("approval_required")
        paused = EvalCase.model_validate(
            {
                **case.model_dump(),
                "given": {**case.given.model_dump(), "approvals": {"policy": "never"}},
            }
        )
        result = await runner.run_case(paused)
        assert result.final_status is RunStatus.AWAITING_APPROVAL
        assert not result.passed
        assert any(a.name == "final_status" for a in result.failures)
        assert all(a.passed for a in result.assertions if a.name.startswith("while_paused"))
        async with unit_of_work(create_session_factory(engine)) as uow:
            [approval] = await uow.approvals.list_by_run(result.run_id)
            assert (
                await uow.count_rows("mock_crm.email_outbox", {"run_id": str(result.run_id)}) == 0
            )
            await uow.commit()
        assert approval.status is ApprovalStatus.PENDING

    async def test_fixtures_are_reset_before_every_case(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        case = registry.case("approval_required")
        first = await runner.run_case(case)
        second = await runner.run_case(case)
        assert first.passed and second.passed, (_failures(first), _failures(second))
        async with unit_of_work(create_session_factory(engine)) as uow:
            # The first case's send was truncated by the second case's reset.
            assert (
                await uow.count_rows(
                    "mock_crm.email_outbox", {"to_email": "dana@northwind.example"}
                )
                == 1
            )
            assert await uow.count_rows("mock_crm.email_outbox", {"run_id": str(first.run_id)}) == 0
            assert await uow.count_rows("mock_crm.leads", {"lead_id": "L-104"}) == 1
            await uow.commit()

    async def test_injected_failures_are_real_recorded_attempts_with_virtual_backoff(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        result = await runner.run_case(registry.case("retryable_failure"))
        assert result.passed, _failures(result)
        assert result.retry_count == 2
        async with unit_of_work(create_session_factory(engine)) as uow:
            calls = await uow.tool_calls.list_by_run(result.run_id)
            events = await uow.trace_events.list_by_run(result.run_id, limit=1000)
            await uow.commit()
        assert sorted((c.attempt, c.status.value) for c in calls) == [
            (1, "failed"),
            (2, "failed"),
            (3, "succeeded"),
        ]
        assert all(c.error_class == "transient" for c in calls if c.status.value == "failed")
        delays = [e.payload["delay_ms"] for e in events if e.kind.value == "retry_scheduled"]
        assert len(delays) == 2 and 200 <= delays[0] <= 300 and 400 <= delays[1] <= 600
        assert result.duration_ms < 5000  # the backoff was never slept for real

    async def test_a_lying_save_draft_is_caught_by_the_read_back(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        result = await runner.run_case(registry.case("invalid_tool_result"))
        assert result.passed, _failures(result)
        assert (result.final_status, result.status_reason) == (
            RunStatus.FAILED,
            "verification_failed",
        )
        async with unit_of_work(create_session_factory(engine)) as uow:
            assert await uow.count_rows("mock_crm.outreach_drafts", {}) == 0
            assert await uow.count_rows("opspilot.approvals", {"run_id": str(result.run_id)}) == 0
            assert (
                await uow.count_rows(
                    "opspilot.tool_calls", {"run_id": str(result.run_id), "tool": "save_draft"}
                )
                == 3
            )
            await uow.commit()

    async def test_count_rows_refuses_unknown_tables_and_columns(self, engine: AsyncEngine) -> None:
        async with unit_of_work(create_session_factory(engine)) as uow:
            with pytest.raises(KeyError):
                await uow.count_rows("langgraph.checkpoints", {})
            with pytest.raises(KeyError):
                await uow.count_rows("mock_crm.leads", {"nope": 1})
