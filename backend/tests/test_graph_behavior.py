"""TEST-002: graph behaviour — every terminal path, pause, retry, replan, skip
and verify transition, asserted on the checkpointed state **and** on the trace.

These are graph tests in the §18.1 sense — "pause, retry, replan, verify and
terminal paths actually happen" — driven through the production composition
rather than a harness of their own. Every test here:

* builds the graph with `app.execution.runtime.build_driver`, the one function
  `wire_runtime` and the evaluation runner both call (ADR-024), so the real
  `ToolRegistry`, the real approval gate, the real verifiers, the real planner
  and the real Postgres saver are on the path;
* drives it through `RunService` → `Executor` → `ApprovalService`, so a pause
  is a real `awaiting_approval` row decided by the real service and a terminal
  status is the row the executor settled;
* scripts only the *tools*, through `build_driver`'s own `implementations`
  override (§18.2's `ScriptedTool`), so a scripted failure is a real recorded
  attempt that has already passed the gate, the stored-decision check and the
  token — never a short circuit around them;
* asserts the control path twice: once on the checkpointed `AgentState` the
  run resumes from, and once on the `trace_events` rows an operator reads.

The clock is a `FixedClock`, so backoff is asserted on rather than waited for
(§10.4), and the fixtures are reseeded per test so row counts are exact.

Not re-proven here: each `decide` rule in isolation and their ordering
(`tests/test_decide.py`, `tests/test_agent_graph.py`), the recovery
classification (`tests/test_recover.py`), the `recover → verify` read-back
retry, which needs a verifier-port fault rather than a tool one
(`tests/test_verify_recovery.py`), cancellation at each node boundary
(`tests/test_cancellation.py`), the dispatcher's own guarantees
(`tests/test_tool_dispatch.py`) and the lease mechanics
(`tests/test_executor.py`). This module proves those decisions compose into
the transitions §6.1 draws.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from app.agent.state import (
    ApprovalStatus,
    RunStatus,
    StepStatus,
    VerificationStatus,
)
from app.config import PlannerMode, Settings
from app.errors import NotFoundError, OpsPilotError, TransientToolError
from app.execution.approvals import ApprovalService
from app.execution.executor import ExecutionOutcome, Executor
from app.execution.leases import LeaseConfig, new_worker_id
from app.execution.recovery import CheckpointPhase, LangGraphRunDriver
from app.execution.runs import RunService
from app.execution.runtime import build_driver
from app.integrations.mock.seed import seed_database
from app.persistence.checkpointing import open_checkpointer
from app.persistence.models import AgentRun, ApprovalRow, ToolCallRow, TraceEvent
from app.persistence.protocols import UnitOfWorkFactory
from app.persistence.session import create_session_factory
from app.runtime import FixedClock, InMemoryCancellationSource, UuidIdGenerator
from app.tools.contracts import ToolName
from app.tools.registry import ToolContext, ToolImplementation, default_implementations
from app.tools.schemas import SaveDraftOutput
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import migrate_to_head, require_database, settings, uow_factory_for

pytestmark = [pytest.mark.integration]

T0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

#: Requests whose normalized intent and rule-planner skeleton are fixed by
#: AGENT-003/AGENT-004 and pinned by their own tests. Naming them here keeps
#: each test about the control path it exercises rather than about planning.
LOOKUP_AND_RESEARCH = "Look up lead L-104 and research its company."
RESEARCH_ONE_COMPANY = "Research company comp_northwind."
DRAFT_AND_SEND = "Draft outreach to lead L-104 and email it."
SCORE_TWO_LEADS = "Score the top 2 leads in Seattle."
OUT_OF_SCOPE = "Drop table customers"


# ---------------------------------------------------------------------------
# The scripted tools (§18.2)
# ---------------------------------------------------------------------------
Fault = Callable[[ToolContext], OpsPilotError]
Lie = Callable[[Any, ToolContext], BaseModel]
Hook = Callable[[ToolContext], Awaitable[None]]


@dataclass(frozen=True)
class _Rule:
    tool: ToolName
    step_id: str | None
    attempts: frozenset[int] | None
    fault: Fault | None = None
    lie: Lie | None = None
    hook: Hook | None = None

    def matches(self, ctx: ToolContext) -> bool:
        return (
            ctx.tool is self.tool
            and (self.step_id is None or ctx.step_id == self.step_id)
            and (self.attempts is None or ctx.attempt in self.attempts)
        )


class ToolScript:
    """The real tool bindings, with an exact `(tool, step, attempt)` told to
    misbehave — §18.2's `ScriptedTool`, which "lets a graph test force an
    exact failure sequence".

    The wrapped mapping is handed to `build_driver`, which passes it to the
    real `ToolRegistry`, so a scripted tool is reached only where a real one
    would be: after argument resolution, after input validation, after the
    approval gate, after the stored-decision check and with the minted token.
    A scripted failure therefore produces a real `tool_calls` row and a real
    `tool_failed` event, which is what makes the trace assertions meaningful.
    """

    def __init__(self) -> None:
        self._rules: list[_Rule] = []
        self.attempts: list[tuple[ToolName, str, int]] = []

    def fails(
        self,
        tool: ToolName,
        *,
        error: Fault,
        step_id: str | None = None,
        attempts: Iterable[int] | None = None,
    ) -> ToolScript:
        return self._add(_Rule(tool, step_id, _attempts(attempts), fault=error))

    def lies(
        self,
        tool: ToolName,
        *,
        output: Lie,
        step_id: str | None = None,
        attempts: Iterable[int] | None = None,
    ) -> ToolScript:
        """Return a well-formed success without performing the effect — the
        canonical verification test (§18.4 #3)."""
        return self._add(_Rule(tool, step_id, _attempts(attempts), lie=output))

    def blocks(
        self,
        tool: ToolName,
        *,
        hook: Hook,
        step_id: str | None = None,
        attempts: Iterable[int] | None = None,
    ) -> ToolScript:
        """Await `hook` before the real implementation runs, so a test can act
        while an attempt is genuinely in flight."""
        return self._add(_Rule(tool, step_id, _attempts(attempts), hook=hook))

    def _add(self, rule: _Rule) -> ToolScript:
        self._rules.append(rule)
        return self

    def attempts_for(self, step_id: str) -> int:
        return sum(1 for _, sid, _ in self.attempts if sid == step_id)

    def wrap(
        self, implementations: Mapping[ToolName, ToolImplementation]
    ) -> dict[ToolName, ToolImplementation]:
        return {name: self._wrapped(impl) for name, impl in implementations.items()}

    def _wrapped(self, impl: ToolImplementation) -> ToolImplementation:
        async def scripted(validated: Any, ctx: ToolContext) -> BaseModel:  # noqa: ANN401
            self.attempts.append((ctx.tool, ctx.step_id, ctx.attempt))
            for rule in self._rules:
                if not rule.matches(ctx):
                    continue
                if rule.hook is not None:
                    await rule.hook(ctx)
                    continue
                if rule.fault is not None:
                    raise rule.fault(ctx)
                if rule.lie is not None:
                    return rule.lie(validated, ctx)
            return await impl(validated, ctx)

        return scripted


def _attempts(attempts: Iterable[int] | None) -> frozenset[int] | None:
    return None if attempts is None else frozenset(attempts)


def transient(ctx: ToolContext) -> OpsPilotError:
    return TransientToolError(
        "scripted transient failure",
        detail={"tool": ctx.tool.value, "step_id": ctx.step_id, "attempt": ctx.attempt},
    )


def not_found(ctx: ToolContext) -> OpsPilotError:
    return NotFoundError(
        "scripted missing record",
        detail={"tool": ctx.tool.value, "step_id": ctx.step_id},
    )


def draft_saved_but_not_persisted(validated: Any, ctx: ToolContext) -> BaseModel:  # noqa: ANN401
    """`save_draft` claims success and writes nothing — the lying tool of
    §11.3 that the read-back exists to catch."""
    return SaveDraftOutput(
        draft_id=f"d_unwritten_{ctx.step_id}_{ctx.attempt}",
        version=1,
        status="saved",
        content_hash=str(getattr(validated, "content_hash", "")),
        saved_at=ctx.clock.now(),
    )


# ---------------------------------------------------------------------------
# The production composition under test
# ---------------------------------------------------------------------------
@dataclass
class Harness:
    """One run's worth of the production stack, plus the evidence readers."""

    run_id: uuid.UUID
    driver: LangGraphRunDriver
    executor: Executor
    runs: RunService
    approvals: ApprovalService
    script: ToolScript
    clock: FixedClock
    uow_factory: UnitOfWorkFactory

    # -- evidence: the checkpoint -------------------------------------------
    async def state(self) -> dict[str, Any]:
        return await self.driver.state(self.run_id)

    async def phase(self) -> CheckpointPhase:
        return (await self.driver.inspect(self.run_id)).phase

    async def interrupt_payload(self) -> dict[str, Any]:
        snapshot = await self.driver._graph.aget_state(  # noqa: SLF001 - the pause is the subject
            {"configurable": {"thread_id": str(self.run_id)}}
        )
        interrupts = [i for task in snapshot.tasks for i in task.interrupts]
        assert interrupts, "the graph is not paused"
        value = interrupts[0].value
        assert isinstance(value, dict)
        return value

    # -- evidence: the durable rows -----------------------------------------
    async def run_row(self) -> AgentRun:
        async with self.uow_factory() as uow:
            row = await uow.agent_runs.get(self.run_id)
            await uow.commit()
        assert row is not None
        return row

    async def events(self) -> list[TraceEvent]:
        async with self.uow_factory() as uow:
            rows = await uow.trace_events.list_by_run(self.run_id, limit=1000)
            await uow.commit()
        return rows

    async def kinds(self) -> list[str]:
        return [e.kind.value for e in await self.events()]

    async def tool_calls(self) -> list[ToolCallRow]:
        async with self.uow_factory() as uow:
            rows = await uow.tool_calls.list_by_run(self.run_id)
            await uow.commit()
        return rows

    async def approval_rows(self) -> list[ApprovalRow]:
        async with self.uow_factory() as uow:
            rows = await uow.approvals.list_by_run(self.run_id)
            await uow.commit()
        return rows

    async def count(self, table: str, where: Mapping[str, Any]) -> int:
        async with self.uow_factory() as uow:
            total = await uow.count_rows(table, dict(where))
            await uow.commit()
        return total

    # -- driving -------------------------------------------------------------
    async def launch(self) -> asyncio.Task[ExecutionOutcome]:
        """Start the run and hand back the executor's task, for a test that
        needs to act while the graph is still inside a node."""
        await self.runs.start_run(self.run_id)
        # `schedule` is idempotent: it hands back the task `start_run` made.
        return self.executor.schedule(self.run_id)

    async def start(self) -> ExecutionOutcome:
        return await (await self.launch())

    async def decide(self, decision: str, *, reason: str | None = None) -> None:
        [pending] = [a for a in await self.approval_rows() if a.status is ApprovalStatus.PENDING]
        result = await self.approvals.decide_approval(
            pending.id, decision=decision, decided_by="operator", reason=reason
        )
        assert result.is_winner


def pinned_settings(**budgets: int) -> Settings:
    """The suite's settings: the deterministic planner, no probabilistic tool
    failures, the seed fixed, and whichever budget a test is about."""
    return settings().model_copy(
        update={"planner": PlannerMode.RULES, "seed": 1337, "tool_failure_rate": 0.0, **budgets}
    )


def compose(
    *,
    engine: AsyncEngine,
    checkpointer: AsyncPostgresSaver,
    script: ToolScript,
    clock: FixedClock,
    config: Settings,
    cancellation: InMemoryCancellationSource,
) -> tuple[LangGraphRunDriver, Executor, RunService, ApprovalService]:
    """The production stack, composed exactly as `wire_runtime` composes it —
    `build_driver` for the graph, then the three services over it."""
    session_factory = create_session_factory(engine)
    uow_factory = uow_factory_for(engine)
    ids = UuidIdGenerator()
    driver = build_driver(
        config,
        session_factory=session_factory,
        uow_factory=uow_factory,
        checkpointer=checkpointer,
        clock=clock,
        ids=ids,
        cancellation_source=cancellation,
        implementations=script.wrap(default_implementations()),
    )
    lease = LeaseConfig.from_settings(config)
    executor = Executor(
        uow_factory=uow_factory,
        driver=driver,
        clock=clock,
        lease=lease,
        owner=new_worker_id(ids, label="graph-test"),
        budgets=config.budgets,
    )
    runs = RunService(
        uow_factory=uow_factory,
        settings=config,
        clock=clock,
        ids=ids,
        cancellation_source=cancellation,
        executor=executor,
    )
    approvals = ApprovalService(
        uow_factory=uow_factory, driver=driver, clock=clock, lease=lease, ids=ids
    )
    return driver, executor, runs, approvals


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def _database() -> None:
    require_database()
    migrate_to_head()


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
async def seeded(engine: AsyncEngine) -> None:
    """The mock CRM, reset to its fixtures before every test, so effect counts
    (`email_outbox`, `outreach_drafts`) are exact."""
    await seed_database(create_session_factory(engine), reset=True)


@pytest.fixture
def make_run(
    engine: AsyncEngine, checkpointer: AsyncPostgresSaver, seeded: None
) -> Callable[..., Awaitable[Harness]]:
    async def _make(request: str, *, script: ToolScript | None = None, **budgets: int) -> Harness:
        script = script or ToolScript()
        clock = FixedClock(T0)
        config = pinned_settings(**budgets)
        cancellation = InMemoryCancellationSource()
        driver, executor, runs, approvals = compose(
            engine=engine,
            checkpointer=checkpointer,
            script=script,
            clock=clock,
            config=config,
            cancellation=cancellation,
        )
        created = await runs.create_run(request)
        return Harness(
            run_id=created.run.id,
            driver=driver,
            executor=executor,
            runs=runs,
            approvals=approvals,
            script=script,
            clock=clock,
            uow_factory=uow_factory_for(engine),
        )

    return _make


# ---------------------------------------------------------------------------
# Small readers over the checkpointed state
# ---------------------------------------------------------------------------
# The saver round-trips the channels through JSON, so what comes back out of
# the checkpoint is plain data — which is the point: these assertions read the
# run the way a resuming process reads it, not the way the node wrote it.
def _as_dict(value: object) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value) if isinstance(value, Mapping) else {}


def status_of(state: Mapping[str, Any]) -> RunStatus:
    return RunStatus(str(state["status"]))


def plan_of(state: Mapping[str, Any]) -> dict[str, Any]:
    return _as_dict(state.get("plan"))


def step_statuses(state: Mapping[str, Any]) -> dict[str, str]:
    return {s["step_id"]: s["status"] for s in plan_of(state).get("steps", [])}


def verification_statuses(state: Mapping[str, Any]) -> dict[str, str]:
    return {
        sid: _as_dict(res)["status"]
        for sid, res in (state.get("verification_result") or {}).items()
    }


def error_classes(state: Mapping[str, Any]) -> list[str]:
    return [_as_dict(e)["error_class"] for e in state.get("errors") or []]


def final_response(state: Mapping[str, Any]) -> dict[str, Any]:
    return _as_dict(state.get("final_response"))


def call_signature(rows: Sequence[ToolCallRow], step_id: str) -> list[tuple[int, str]]:
    """`(attempt, status)` for one step's `tool_calls` rows, in attempt order."""
    return sorted((r.attempt, r.status.value) for r in rows if r.step_id == step_id)


# ---------------------------------------------------------------------------
# 1. The successful path: understand → plan → decide → execute → verify → complete
# ---------------------------------------------------------------------------
class TestSuccessfulExecution:
    async def test_a_two_step_run_completes_and_the_trace_records_every_attempt(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """The baseline control path, and the shape every other test departs
        from: both steps dispatched once, both verified, the run terminal and
        the timeline closed by `run_completed`."""
        h = await make_run(LOOKUP_AND_RESEARCH)

        assert await h.start() is ExecutionOutcome.FINISHED

        state = await h.state()
        assert status_of(state) is RunStatus.COMPLETED
        assert state["status_reason"] is None
        assert step_statuses(state) == {"s1": StepStatus.SUCCEEDED, "s2": StepStatus.SUCCEEDED}
        assert verification_statuses(state) == {
            "s1": VerificationStatus.NOT_REQUIRED,  # get_lead declares no postcondition
            "s2": VerificationStatus.PASSED,  # research_company is verified by invariant
        }
        assert state["errors"] == []
        assert state["retry_count"] == {}
        assert state["replan_count"] == 0
        assert state["step_count"] == 2
        assert final_response(state)["done"] == ["s1", "s2"]
        assert final_response(state)["partial"] is False
        assert await h.phase() is CheckpointPhase.FINISHED

        row = await h.run_row()
        assert (row.status, row.status_reason, row.lease_owner) == (RunStatus.COMPLETED, None, None)
        assert row.finished_at is not None
        assert row.final_response is not None and row.final_response["done"] == ["s1", "s2"]

        kinds = await h.kinds()
        assert kinds == [
            "run_created",
            "run_started",
            "tool_started",
            "tool_succeeded",
            "verification_passed",
            "tool_started",
            "tool_succeeded",
            "verification_passed",
            "run_completed",
        ]
        assert call_signature(await h.tool_calls(), "s1") == [(1, "succeeded")]
        assert call_signature(await h.tool_calls(), "s2") == [(1, "succeeded")]

    async def test_the_trace_is_a_gapless_ordered_record_of_the_one_run(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """`seq` is unique and strictly increasing per run, and every event
        belongs to the run that produced it — the property the timeline and
        `since_seq` polling both rest on (§14.2)."""
        h = await make_run(RESEARCH_ONE_COMPANY)
        await h.start()

        events = await h.events()
        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        assert {e.run_id for e in events} == {h.run_id}
        assert [e.step_id for e in events if e.kind.value.startswith("tool_")] == ["s1", "s1"]


# ---------------------------------------------------------------------------
# 2. Pause: the only interrupt in the graph, and the resume that clears it
# ---------------------------------------------------------------------------
class TestApprovalPauseAndResume:
    async def test_the_gate_pauses_before_any_effect_and_the_decision_resumes_the_run(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """`request_approval` is the graph's only `interrupt()` (§6.3): it
        fires *before* the tool, the checkpoint it leaves is what the human's
        decision resumes, and the send happens exactly once afterwards."""
        h = await make_run(DRAFT_AND_SEND)

        assert await h.start() is ExecutionOutcome.PAUSED

        # -- paused: the checkpoint, the row and the CRM ---------------------
        assert await h.phase() is CheckpointPhase.PAUSED
        payload = await h.interrupt_payload()
        assert payload["step_id"] == "s6"
        assert payload["tool"] == ToolName.SEND_EMAIL_MOCK.value
        assert set(payload["payload_preview"]) >= {"to_email", "subject", "body"}

        paused = await h.state()
        assert step_statuses(paused)["s5"] == StepStatus.SUCCEEDED
        assert step_statuses(paused)["s6"] == StepStatus.PENDING
        assert "s6" not in (paused.get("tool_results") or {})

        row = await h.run_row()
        assert (row.status, row.lease_owner, row.lease_expires_at) == (
            RunStatus.AWAITING_APPROVAL,
            None,
            None,
        )
        [approval] = await h.approval_rows()
        assert (approval.status, approval.step_id, approval.tool) == (
            ApprovalStatus.PENDING,
            "s6",
            ToolName.SEND_EMAIL_MOCK,
        )
        assert approval.args_hash == payload["args_hash"]
        # The safety claim (§18.4 #1): a paused run has written nothing.
        assert await h.count("mock_crm.email_outbox", {"run_id": str(h.run_id)}) == 0
        assert await h.count("mock_crm.email_outbox", {"to_email": "dana@northwind.example"}) == 0
        assert h.script.attempts_for("s6") == 0
        assert [e.kind.value for e in await h.events() if e.step_id == "s6"] == [
            "approval_requested"
        ]

        # -- decided: the run resumes from the checkpoint and finishes -------
        await h.decide("approve", reason="Approved for the pilot.")

        state = await h.state()
        assert status_of(state) is RunStatus.COMPLETED
        assert step_statuses(state)["s6"] == StepStatus.SUCCEEDED
        assert verification_statuses(state)["s6"] == VerificationStatus.PASSED
        decisions = _as_dict(state.get("approval_state")).get("decisions") or {}
        decision = _as_dict(decisions.get("s6"))
        assert decision["decision"] == "approve"
        assert decision["args_hash"] == approval.args_hash

        assert (await h.run_row()).status is RunStatus.COMPLETED
        assert await h.count("mock_crm.email_outbox", {"run_id": str(h.run_id)}) == 1
        assert h.script.attempts_for("s6") == 1

        events = await h.events()
        kinds = [e.kind.value for e in events]
        # The pause is requested once and granted once, and the send is the
        # first thing that happens after the grant.
        assert kinds.count("approval_requested") == 1
        assert kinds.count("approval_granted") == 1
        assert kinds[-4:] == [
            "approval_granted",
            "tool_started",
            "tool_succeeded",
            "verification_passed",
        ]
        granted = next(e for e in events if e.kind.value == "approval_granted")
        sends = [e for e in events if e.step_id == "s6"]
        assert [e.kind.value for e in sends] == [
            "approval_requested",
            "approval_granted",
            "tool_started",
            "tool_succeeded",
            "verification_passed",
        ]
        # Everything that touched the mail port happened after the grant.
        assert [e.seq > granted.seq for e in sends] == [False, False, True, True, True]

    async def test_a_rejected_approval_ends_the_run_rejected_with_nothing_sent(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """A decline is the mechanism working, not a failure (§7 `complete`):
        `rejected`, zero effects, and a response that names what was not done."""
        h = await make_run(DRAFT_AND_SEND)
        assert await h.start() is ExecutionOutcome.PAUSED

        await h.decide("reject", reason="Not this quarter.")

        state = await h.state()
        assert status_of(state) is RunStatus.REJECTED
        assert state["status_reason"] == "approval_rejected"
        assert step_statuses(state)["s6"] == StepStatus.REJECTED
        assert "s6" in final_response(state)["not_done"]
        assert final_response(state)["done"] == ["s1", "s2", "s3", "s4", "s5"]

        row = await h.run_row()
        assert (row.status, row.status_reason) == (RunStatus.REJECTED, "approval_rejected")
        assert await h.count("mock_crm.email_outbox", {"run_id": str(h.run_id)}) == 0
        assert h.script.attempts_for("s6") == 0
        [approval] = await h.approval_rows()
        assert approval.status is ApprovalStatus.REJECTED

        kinds = await h.kinds()
        assert kinds[-2:] == ["approval_requested", "approval_rejected"]
        # Nothing was ever dispatched for the declined step.
        assert [e.kind.value for e in await h.events() if e.step_id == "s6"] == [
            "approval_requested",
            "approval_rejected",
        ]


# ---------------------------------------------------------------------------
# 3. Retry: recover → execute_tool, bounded and backed off
# ---------------------------------------------------------------------------
class TestRetryAndTerminalFailure:
    async def test_a_transient_failure_is_retried_with_backoff_and_the_run_completes(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """The retry edge (§6.3): one attempt per invocation, one `tool_calls`
        row and one trace pair per attempt, and a `retry_scheduled` naming the
        node the retry re-enters and the delay it waited (§10.7)."""
        script = ToolScript().fails(
            ToolName.RESEARCH_COMPANY, step_id="s2", attempts=[1, 2], error=transient
        )
        h = await make_run(LOOKUP_AND_RESEARCH, script=script, max_retries=2)

        assert await h.start() is ExecutionOutcome.FINISHED

        state = await h.state()
        assert status_of(state) is RunStatus.COMPLETED
        assert state["retry_count"] == {"s2": 2}
        assert step_statuses(state)["s2"] == StepStatus.SUCCEEDED
        assert error_classes(state) == ["transient", "transient"]
        # Attempts are steps: two retries cost two more of the step budget.
        assert state["step_count"] == 4

        assert call_signature(await h.tool_calls(), "s2") == [
            (1, "failed"),
            (2, "failed"),
            (3, "succeeded"),
        ]
        assert h.script.attempts_for("s2") == 3

        events = await h.events()
        assert [e.kind.value for e in events if e.step_id == "s2"] == [
            "tool_started",
            "tool_failed",
            "retry_scheduled",
            "tool_started",
            "tool_failed",
            "retry_scheduled",
            "tool_started",
            "tool_succeeded",
            "verification_passed",
        ]
        retries = [e for e in events if e.kind.value == "retry_scheduled"]
        assert [e.payload["target"] for e in retries] == ["execute_tool", "execute_tool"]
        assert [e.payload["error_class"] for e in retries] == ["transient", "transient"]
        # §10.4: 250ms then 500ms, each scaled by the seeded jitter in [0.8, 1.2].
        delays = [e.payload["delay_ms"] for e in retries]
        assert 200 <= delays[0] <= 300
        assert 400 <= delays[1] <= 600
        # The backoff was asserted on, not waited for.
        assert [int(s * 1000) for s in h.clock.sleep_calls] == delays

    @pytest.mark.critical
    async def test_a_permanently_failing_tool_stops_at_exactly_one_plus_max_retries(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """The bounded-execution claim (§10.5, §18.4 #5): an exact attempt
        count, a terminal `retry_budget_exhausted`, and the steps behind the
        failure never dispatched."""
        script = ToolScript().fails(ToolName.RESEARCH_COMPANY, step_id="s2", error=transient)
        h = await make_run(LOOKUP_AND_RESEARCH, script=script, max_retries=2)

        assert await h.start() is ExecutionOutcome.FINISHED

        state = await h.state()
        assert status_of(state) is RunStatus.FAILED
        assert state["status_reason"] == "retry_budget_exhausted"
        assert state["retry_count"] == {"s2": 2}
        assert error_classes(state) == ["transient"] * 3
        assert final_response(state)["not_done"] == ["s2"]

        row = await h.run_row()
        assert (row.status, row.status_reason) == (RunStatus.FAILED, "retry_budget_exhausted")
        assert h.script.attempts_for("s2") == 3
        assert call_signature(await h.tool_calls(), "s2") == [
            (1, "failed"),
            (2, "failed"),
            (3, "failed"),
        ]

        kinds = await h.kinds()
        assert kinds.count("tool_started") == 4  # s1 once, s2 three times
        assert kinds.count("tool_failed") == 3
        assert kinds.count("retry_scheduled") == 2  # never a third
        assert kinds[-1] == "run_failed"

    async def test_the_step_budget_stops_a_loop_the_retry_budget_would_have_allowed(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """`decide` checks budgets first on every pass, so `MAX_STEPS` bounds
        the retry loop independently of `MAX_RETRIES` (§10.5)."""
        script = ToolScript().fails(ToolName.RESEARCH_COMPANY, error=transient)
        h = await make_run(RESEARCH_ONE_COMPANY, script=script, max_retries=5, max_steps=2)

        assert await h.start() is ExecutionOutcome.FINISHED

        state = await h.state()
        assert status_of(state) is RunStatus.FAILED
        assert state["status_reason"] == "budget_exhausted"
        assert state["step_count"] == 2
        assert state["retry_count"] == {"s1": 1}  # one retry spent, four still budgeted
        assert h.script.attempts_for("s1") == 2

        kinds = await h.kinds()
        assert kinds.count("tool_started") == 2
        assert kinds.count("retry_scheduled") == 1
        assert kinds[-1] == "run_failed"

    async def test_the_step_budget_is_checked_at_decide_before_the_run_can_complete(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """`decide → fail` on rule 1 (§6.2): budgets are evaluated before
        "nothing left to run", so a run that has spent `MAX_STEPS` terminates
        as `budget_exhausted` even though no tool failed. That ordering is
        what stops any cycle in the graph iterating for free (§10.5)."""
        h = await make_run(LOOKUP_AND_RESEARCH, max_steps=2)

        assert await h.start() is ExecutionOutcome.FINISHED

        state = await h.state()
        assert status_of(state) is RunStatus.FAILED
        assert state["status_reason"] == "budget_exhausted"
        assert state["step_count"] == 2
        # Nothing failed: both attempts succeeded and were verified.
        assert step_statuses(state) == {"s1": StepStatus.SUCCEEDED, "s2": StepStatus.SUCCEEDED}
        assert error_classes(state) == []
        assert final_response(state)["done"] == ["s1", "s2"]

        kinds = await h.kinds()
        assert kinds.count("tool_failed") == 0
        assert kinds.count("retry_scheduled") == 0
        assert kinds[-1] == "run_failed"
        assert (await h.run_row()).status_reason == "budget_exhausted"


# ---------------------------------------------------------------------------
# 4. Skip and replan: the two recoveries that are not a retry
# ---------------------------------------------------------------------------
class TestSkipAndReplan:
    async def test_an_optional_step_that_cannot_run_is_skipped_and_the_run_completes_partial(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """`recover → decide` (§6.1): skipping is preferred to spending replan
        budget (§10.2), the step is never re-attempted, and the response says
        the run was partial rather than claiming success."""
        script = ToolScript().fails(ToolName.SCORE_LEAD, step_id="s4", error=not_found)
        h = await make_run(SCORE_TWO_LEADS, script=script)

        assert await h.start() is ExecutionOutcome.FINISHED

        state = await h.state()
        assert status_of(state) is RunStatus.COMPLETED
        assert step_statuses(state)["s3"] == StepStatus.SUCCEEDED
        assert step_statuses(state)["s4"] == StepStatus.SKIPPED
        assert state["replan_count"] == 0
        assert state["retry_count"] == {}  # NOT_FOUND is a planning fault, never retried
        response = final_response(state)
        assert response["partial"] is True
        assert response["not_done"] == ["s4"]
        assert "s4" not in response["done"]

        assert (await h.run_row()).status is RunStatus.COMPLETED
        assert h.script.attempts_for("s4") == 1
        assert call_signature(await h.tool_calls(), "s4") == [(1, "failed")]

        events = await h.events()
        assert [e.kind.value for e in events if e.step_id == "s4"] == [
            "tool_started",
            "tool_failed",
        ]
        assert "retry_scheduled" not in [e.kind.value for e in events]
        assert (await h.kinds())[-1] == "run_completed"

    async def test_a_replannable_fault_revises_the_plan_and_the_forced_loop_ends_in_budget(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """`recover → plan → decide → …` is a genuine cycle in the graph. It
        cannot spin: every pass consumes replan budget, and the run terminates
        as `replan_budget_exhausted` after exactly `MAX_REPLANS` revisions
        (§10.5). The settled steps are carried over, so the loop re-runs only
        what failed."""
        script = ToolScript().fails(ToolName.SCORE_LEAD, step_id="s3", error=not_found)
        h = await make_run(SCORE_TWO_LEADS, script=script, max_replans=2)

        assert await h.start() is ExecutionOutcome.FINISHED

        state = await h.state()
        assert status_of(state) is RunStatus.FAILED
        assert state["status_reason"] == "replan_budget_exhausted"
        assert state["replan_count"] == 2
        assert len(state["plan_history"]) == 2
        assert plan_of(state)["revision"] == 2
        assert [_as_dict(p)["revision"] for p in state["plan_history"]] == [0, 1]
        assert state["retry_count"] == {}

        # Three attempts at the failing step — one per plan — and the work
        # that already succeeded was never redone.
        assert h.script.attempts_for("s3") == 3
        assert h.script.attempts_for("s1") == 1
        assert [call for call in h.script.attempts if call[0] is ToolName.RESEARCH_COMPANY] == [
            (ToolName.RESEARCH_COMPANY, "s2[0]", 1),
            (ToolName.RESEARCH_COMPANY, "s2[1]", 1),
        ]

        events = await h.events()
        assert [e.kind.value for e in events if e.step_id == "s3"] == [
            "tool_started",
            "tool_failed",
        ] * 3
        assert "retry_scheduled" not in [e.kind.value for e in events]
        assert (await h.kinds())[-1] == "run_failed"
        assert (await h.run_row()).status_reason == "replan_budget_exhausted"


# ---------------------------------------------------------------------------
# 5. Verify: its own node, its own failure, its own recovery
# ---------------------------------------------------------------------------
class TestVerification:
    @pytest.mark.critical
    async def test_a_lying_tool_is_caught_by_the_read_back_and_the_retry_writes_for_real(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """`save_draft` returns ok and persists nothing (§11.3). Verification
        is a node, so the lie is a first-class `verification_failed` event and
        a recoverable error — the absent write is safe to repeat on an
        idempotent tool, and attempt 2 leaves exactly one row."""
        script = ToolScript().lies(
            ToolName.SAVE_DRAFT, step_id="s5", attempts=[1], output=draft_saved_but_not_persisted
        )
        h = await make_run(DRAFT_AND_SEND, script=script, max_retries=2)

        assert await h.start() is ExecutionOutcome.PAUSED

        state = await h.state()
        assert state["retry_count"] == {"s5": 1}
        assert verification_statuses(state)["s5"] == VerificationStatus.PASSED
        assert step_statuses(state)["s5"] == StepStatus.SUCCEEDED
        assert error_classes(state) == ["verification_failed"]
        assert h.script.attempts_for("s5") == 2
        assert await h.count("mock_crm.outreach_drafts", {}) == 1

        events = await h.events()
        assert [e.kind.value for e in events if e.step_id == "s5"] == [
            "tool_started",
            "tool_succeeded",  # the tool reported success …
            "verification_failed",  # … and the read-back disagreed
            "retry_scheduled",
            "tool_started",
            "tool_succeeded",
            "verification_passed",
        ]
        [retry] = [e for e in events if e.kind.value == "retry_scheduled"]
        assert retry.payload["target"] == "execute_tool"
        assert retry.payload["error_class"] == "verification_failed"

        # The recovered run still meets the gate before it sends.
        await h.decide("approve")
        assert status_of(await h.state()) is RunStatus.COMPLETED
        assert await h.count("mock_crm.email_outbox", {"run_id": str(h.run_id)}) == 1

    async def test_a_tool_that_never_persists_ends_the_run_as_verification_failed(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """When every attempt lies, the run fails on the verification verdict
        — not on a tool error — the effect never happened, and the gated step
        behind it is never even requested."""
        script = ToolScript().lies(
            ToolName.SAVE_DRAFT, step_id="s5", output=draft_saved_but_not_persisted
        )
        h = await make_run(DRAFT_AND_SEND, script=script, max_retries=2)

        assert await h.start() is ExecutionOutcome.FINISHED

        state = await h.state()
        assert status_of(state) is RunStatus.FAILED
        assert state["status_reason"] == "verification_failed"
        assert verification_statuses(state)["s5"] == VerificationStatus.FAILED
        assert state["retry_count"] == {"s5": 2}
        assert error_classes(state) == ["verification_failed"] * 3

        assert h.script.attempts_for("s5") == 3
        assert h.script.attempts_for("s6") == 0
        assert await h.count("mock_crm.outreach_drafts", {}) == 0
        assert await h.count("mock_crm.email_outbox", {"run_id": str(h.run_id)}) == 0
        assert await h.approval_rows() == []

        kinds = await h.kinds()
        assert kinds.count("verification_failed") == 3
        assert kinds.count("tool_succeeded") == 7  # every attempt "succeeded"; none verified
        assert "approval_requested" not in kinds
        assert kinds[-1] == "run_failed"
        assert (await h.run_row()).status_reason == "verification_failed"


# ---------------------------------------------------------------------------
# 6. Cancellation: cooperative, at a node boundary, never mid-effect
# ---------------------------------------------------------------------------
class TestCancellation:
    async def test_cancelling_a_running_graph_finishes_the_attempt_then_terminalizes(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """The operator cancels while a tool is genuinely in flight (§13.2):
        that attempt completes and is recorded, the graph exits through
        `fail(cancelled)` at the next node boundary, nothing after it is
        dispatched, and the executor's settle never overwrites the operator's
        verdict."""
        in_flight = asyncio.Event()
        release = asyncio.Event()

        async def hold(_ctx: ToolContext) -> None:
            in_flight.set()
            await release.wait()

        script = ToolScript().blocks(ToolName.SCORE_LEAD, step_id="s3", hook=hold)
        h = await make_run(SCORE_TWO_LEADS, script=script)

        task = await h.launch()
        await asyncio.wait_for(in_flight.wait(), timeout=10)
        assert (await h.run_row()).status is RunStatus.RUNNING

        await h.runs.cancel_run(h.run_id, reason="cancelled")
        release.set()
        await task

        state = await h.state()
        assert status_of(state) is RunStatus.FAILED
        assert state["status_reason"] == "cancelled"
        # The in-flight attempt was never interrupted; the next one never ran.
        assert h.script.attempts_for("s3") == 1
        assert h.script.attempts_for("s4") == 0
        assert call_signature(await h.tool_calls(), "s3") == [(1, "succeeded")]
        assert [r for r in await h.tool_calls() if r.step_id == "s4"] == []

        # The database, not the executor, has the last word on the row.
        row = await h.run_row()
        assert (row.status, row.status_reason) == (RunStatus.CANCELLED, "cancelled")
        kinds = await h.kinds()
        assert "run_cancelled" in kinds
        assert "run_failed" not in kinds
        assert [e.kind.value for e in await h.events() if e.step_id == "s4"] == []


# ---------------------------------------------------------------------------
# 7. Terminal paths that never reach a tool
# ---------------------------------------------------------------------------
class TestEarlyTerminalPaths:
    async def test_an_out_of_scope_request_fails_at_understand_without_planning(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """`understand → fail` (§6.1): refusing early is cheaper than planning
        something we cannot do, and nothing downstream runs."""
        h = await make_run(OUT_OF_SCOPE)

        assert await h.start() is ExecutionOutcome.FINISHED

        state = await h.state()
        assert status_of(state) is RunStatus.FAILED
        assert state["status_reason"] == "out_of_scope"
        assert state["plan"] is None
        assert state["tool_calls"] == []
        assert state["step_count"] == 0
        assert "out of scope" in final_response(state)["summary"].lower()

        assert (await h.run_row()).status_reason == "out_of_scope"
        assert await h.tool_calls() == []
        assert await h.kinds() == ["run_created", "run_started", "run_failed"]

    async def test_a_plan_that_cannot_be_validated_fails_before_any_tool_runs(
        self, make_run: Callable[..., Awaitable[Harness]]
    ) -> None:
        """`plan → fail` (§7 `plan`): the six-step outreach plan does not fit
        a three-step budget, deterministic validation refuses it, and the run
        ends without a dispatch, an approval or an effect."""
        h = await make_run(DRAFT_AND_SEND, max_steps=3)

        assert await h.start() is ExecutionOutcome.FINISHED

        state = await h.state()
        assert status_of(state) is RunStatus.FAILED
        assert state["status_reason"] == "invalid_plan"
        assert error_classes(state) == ["planner_error"]
        assert state["replan_count"] == 0
        assert state["step_count"] == 0

        assert await h.tool_calls() == []
        assert await h.approval_rows() == []
        assert await h.count("mock_crm.email_outbox", {"run_id": str(h.run_id)}) == 0
        assert await h.kinds() == ["run_created", "run_started", "run_failed"]


# ---------------------------------------------------------------------------
# 8. Durability: the checkpoint, not the process, carries the run
# ---------------------------------------------------------------------------
class TestDurableStateAndResume:
    async def test_a_paused_run_resumes_in_a_fresh_composition_and_redoes_nothing(
        self,
        make_run: Callable[..., Awaitable[Harness]],
        engine: AsyncEngine,
        checkpointer: AsyncPostgresSaver,
    ) -> None:
        """`thread_id` is the run id and every invocation is `durability="sync"`
        (§6.4), so a pause survives the process that produced it. A second
        composition — its own graph, registry, tool bindings and services over
        the same saver — reads the run's whole history from the checkpoint and
        resumes it, re-executing only the step the human decided."""
        h = await make_run(DRAFT_AND_SEND)
        assert await h.start() is ExecutionOutcome.PAUSED

        successor = ToolScript()
        driver, _executor, _runs, approvals = compose(
            engine=engine,
            checkpointer=checkpointer,
            script=successor,
            clock=FixedClock(T0),
            config=pinned_settings(),
            cancellation=InMemoryCancellationSource(),
        )

        # What the successor can see before it does anything: the plan, the
        # five settled steps and the pause — none of it in this process.
        inspection = await driver.inspect(h.run_id)
        assert inspection.phase is CheckpointPhase.PAUSED
        assert inspection.step_id == "s6"
        carried = await driver.state(h.run_id)
        assert [s["status"] for s in plan_of(carried)["steps"]] == [StepStatus.SUCCEEDED] * 5 + [
            StepStatus.PENDING
        ]
        assert set(carried["tool_results"]) == {"s1", "s2", "s3", "s4", "s5"}

        [pending] = [a for a in await h.approval_rows() if a.status is ApprovalStatus.PENDING]
        result = await approvals.decide_approval(
            pending.id, decision="approve", decided_by="operator"
        )
        assert result.is_winner

        state = await driver.state(h.run_id)
        assert status_of(state) is RunStatus.COMPLETED
        assert step_statuses(state)["s6"] == StepStatus.SUCCEEDED
        # The successor ran the gated step and nothing else: the five settled
        # steps came back from the checkpoint, not from a re-execution.
        assert [step_id for _tool, step_id, _attempt in successor.attempts] == ["s6"]
        assert h.script.attempts_for("s6") == 0
        assert await h.count("mock_crm.email_outbox", {"run_id": str(h.run_id)}) == 1
        assert (await h.run_row()).status is RunStatus.COMPLETED
        # One timeline, across both processes.
        assert (await h.kinds())[-4:] == [
            "approval_granted",
            "tool_started",
            "tool_succeeded",
            "verification_passed",
        ]
