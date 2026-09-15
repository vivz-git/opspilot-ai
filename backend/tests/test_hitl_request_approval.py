"""HITL-003 — `request_approval`: idempotent durable request, `approval_requested`
on a genuine insert only, then `interrupt()` — never a tool call (§6.3, §7,
§9.7, §9.8, ADR-007).

Three layers, in the order the invariant is built:

1. **The node over the checkpointed state** (unit, `MemorySaver`): the pause
   surfaces one canonical payload, re-entry surfaces the same one, a held
   decision for the exact arguments is reused without pausing, and changed
   arguments pause again rather than loop.
2. **The repository primitive against real Postgres** (integration):
   `ApprovalRepository.upsert_request` under every shape of prior row —
   none, identical pending, changed pending, approved, rejected, elapsed,
   historical — and under concurrency: identical requests, competing
   argument hashes, a decision landing inside the request's window, replay
   after approval, and a deadlock-free mix with decisions and trace writes.
3. **The compiled graph over the Postgres saver** (integration): pausing
   writes the request and nothing else — `mock_crm` is hashed before and
   after — re-entry, crash-window re-entry, replay in a fresh process and
   reconciler recovery all find the one row and the one event; approval
   resumes into exactly one send, rejection into `rejected`, and arguments
   that changed after a grant chain the old approval forward and ask again.

Every test scripts the *human* through the real repository's conditional
`decide`, never the gate.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from functools import partial
from typing import Any

import pytest
import sqlalchemy as sa
from app.agent.graph import create_agent_graph
from app.agent.nodes import NodeHandlers, create_initial_state
from app.agent.state import (
    AgentState,
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalState,
    ApprovalStatus,
    Plan,
    PlanStep,
    RunStatus,
    StepStatus,
    ToolResult,
)
from app.config import get_settings
from app.execution.recovery import LangGraphRunDriver, Reconciler, RecoveryOutcome
from app.integrations.mock import build_mock_adapters, seed_database
from app.integrations.ports import Adapters, DraftInput
from app.persistence.checkpointing import DURABILITY, open_checkpointer, thread_config
from app.persistence.models import ApprovalRow, TraceEventKind
from app.persistence.protocols import ApprovalUpsert, UnitOfWorkFactory
from app.persistence.session import create_session_factory, unit_of_work
from app.runtime import FixedClock, SequentialIdGenerator
from app.security import canonical_args_hash
from app.tools.contracts import RiskLevel, ToolName
from app.tools.registry import ToolRegistry
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from recovery_harness import LEASE, T0, leave_stale_lease, migrate_to_head, require_database

DANA = "dana@northwind.example"  # lead L-104's stored address (fixtures)
STEP = "s6"
TTL = timedelta(hours=24)


def send_plan(args: dict[str, Any], *, optional: bool = False) -> Plan:
    return Plan(
        plan_id="p_hitl3",
        steps=[
            PlanStep(
                step_id=STEP,
                tool=ToolName.SEND_EMAIL_MOCK,
                args=args,
                optional=optional,
                status=StepStatus.PENDING,
            )
        ],
    )


def approve_decision(args: dict[str, Any], *, approval_id: str = "held") -> ApprovalDecision:
    return ApprovalDecision(
        approval_id=approval_id,
        step_id=STEP,
        decision=ApprovalDecisionKind.APPROVE,
        args_hash=canonical_args_hash(args),
        decided_by="operator",
        decided_at=T0,
    )


# ===========================================================================
# 1. The node over the checkpointed state (no database)
# ===========================================================================
class ScriptedExecutor:
    """Stands in for `execute_tool` so the in-memory graph can complete; it
    records every dispatch it is asked for, which must be none while paused."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        step_id = state["current_step_id"]
        self.calls.append(step_id)
        plan = state["plan"]
        step = plan.step(step_id)
        assert step is not None
        return {
            "tool_results": {
                step_id: ToolResult(step_id=step_id, tool=step.tool, output={}, produced_at=T0)
            },
            "plan": plan.model_copy(
                update={
                    "steps": [
                        s.model_copy(update={"status": StepStatus.SUCCEEDED})
                        if s.step_id == step_id
                        else s
                        for s in plan.steps
                    ]
                }
            ),
        }


def in_memory_graph() -> tuple[Any, ScriptedExecutor, dict[str, Any]]:
    executor = ScriptedExecutor()
    handlers = NodeHandlers(clock=FixedClock(T0))
    handlers.execute_tool = executor  # type: ignore[method-assign]
    graph = create_agent_graph(checkpointer=MemorySaver(), node_handlers=handlers)
    return graph, executor, {"configurable": {"thread_id": str(uuid.uuid4())}}


def paused_payload(graph: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    snapshot = graph.get_state(cfg)
    (task,) = snapshot.tasks
    (pause,) = task.interrupts
    assert isinstance(pause.value, dict)
    return pause.value


@pytest.mark.unit
class TestNodeOverCheckpointedState:
    ARGS: dict[str, Any] = {"draft_id": "d_100", "to_email": DANA}

    async def test_the_first_request_pauses_with_a_canonical_payload(self) -> None:
        graph, executor, cfg = in_memory_graph()
        run_id = uuid.uuid4()
        initial = create_initial_state(
            run_id=run_id, user_request="send", plan=send_plan(self.ARGS), clock=FixedClock(T0)
        )

        paused = await graph.ainvoke(initial, config=cfg)
        payload = paused_payload(graph, cfg)

        assert set(payload) == {
            "approval_id",
            "run_id",
            "step_id",
            "tool",
            "args_hash",
            "payload_preview",
        }
        assert payload["run_id"] == str(run_id)
        assert payload["step_id"] == STEP
        assert payload["tool"] == ToolName.SEND_EMAIL_MOCK.value
        assert payload["args_hash"] == canonical_args_hash(self.ARGS)
        assert payload["payload_preview"] == self.ARGS
        assert payload["approval_id"].startswith(f"appr_{str(run_id)[:8]}_{STEP}_")
        assert paused["status"] is RunStatus.RUNNING
        assert paused["approval_state"].decisions == {}
        assert executor.calls == [], "nothing dispatched while paused"

    async def test_re_entry_without_a_decision_surfaces_the_same_request(self) -> None:
        graph, executor, cfg = in_memory_graph()
        initial = create_initial_state(
            run_id=uuid.uuid4(), user_request="send", plan=send_plan(self.ARGS)
        )
        await graph.ainvoke(initial, config=cfg)
        first = paused_payload(graph, cfg)

        # A plain re-entry (no `Command(resume=...)`): the node re-executes
        # from the top and must pause on the very same request.
        await graph.ainvoke(None, config=cfg)

        assert paused_payload(graph, cfg) == first
        assert executor.calls == []

    async def test_approve_resumes_through_decide_into_execute_tool(self) -> None:
        graph, executor, cfg = in_memory_graph()
        initial = create_initial_state(
            run_id=uuid.uuid4(), user_request="send", plan=send_plan(self.ARGS)
        )
        await graph.ainvoke(initial, config=cfg)
        payload = paused_payload(graph, cfg)

        final = await graph.ainvoke(Command(resume="approve"), config=cfg)

        assert final["status"] is RunStatus.COMPLETED
        assert executor.calls == [STEP]
        decision = final["approval_state"].decisions[STEP]
        assert decision.decision is ApprovalDecisionKind.APPROVE
        assert decision.args_hash == payload["args_hash"], "bound to the hash the human saw"
        assert decision.approval_id == payload["approval_id"]
        assert final["approval_state"].grants(STEP, self.ARGS)

    async def test_reject_ends_the_run_rejected_not_failed(self) -> None:
        graph, executor, cfg = in_memory_graph()
        initial = create_initial_state(
            run_id=uuid.uuid4(), user_request="send", plan=send_plan(self.ARGS)
        )
        await graph.ainvoke(initial, config=cfg)

        final = await graph.ainvoke(
            Command(resume={"decision": "reject", "decided_by": "ops", "reason": "wrong lead"}),
            config=cfg,
        )

        assert final["status"] is RunStatus.REJECTED
        assert final["status_reason"] == "approval_rejected"
        assert executor.calls == []
        decision = final["approval_state"].decisions[STEP]
        assert decision.decision is ApprovalDecisionKind.REJECT
        assert decision.decided_by == "ops" and decision.reason == "wrong lead"
        assert final["plan"].step(STEP).status is StepStatus.REJECTED

    async def test_rejecting_an_optional_step_skips_it_and_completes(self) -> None:
        graph, executor, cfg = in_memory_graph()
        initial = create_initial_state(
            run_id=uuid.uuid4(), user_request="send", plan=send_plan(self.ARGS, optional=True)
        )
        await graph.ainvoke(initial, config=cfg)

        final = await graph.ainvoke(Command(resume="reject"), config=cfg)

        assert final["status"] is RunStatus.COMPLETED
        assert final["plan"].step(STEP).status is StepStatus.SKIPPED
        assert executor.calls == []

    async def test_a_held_decision_for_these_arguments_is_reused_without_pausing(self) -> None:
        """Outside a graph `interrupt()` cannot be called at all, so a delta
        coming back proves the node did not try to pause: the decision it
        already holds for exactly these arguments is the answer."""
        handlers = NodeHandlers(clock=FixedClock(T0))
        state = create_initial_state(
            run_id="run_held", user_request="send", plan=send_plan(self.ARGS)
        )
        state["current_step_id"] = STEP
        state["approval_state"] = ApprovalState(decisions={STEP: approve_decision(self.ARGS)})

        delta = await handlers.request_approval(state)

        assert delta["approval_state"].decisions[STEP] == approve_decision(self.ARGS)
        assert delta["status"] is RunStatus.RUNNING

    async def test_changed_arguments_after_a_grant_pause_again_rather_than_loop(self) -> None:
        """AGENT-004 left this path to HITL-003: a grant for other arguments
        does not authorise these, and the node must ask afresh — a new
        request for the new hash — not short-circuit back into `decide`."""
        graph, executor, cfg = in_memory_graph()
        old_args = {"draft_id": "d_100", "to_email": "old@northwind.example"}
        initial = create_initial_state(
            run_id=uuid.uuid4(), user_request="send", plan=send_plan(self.ARGS)
        )
        initial["approval_state"] = ApprovalState(
            decisions={STEP: approve_decision(old_args, approval_id="stale")}
        )

        await graph.ainvoke(initial, config=cfg)
        payload = paused_payload(graph, cfg)

        assert payload["args_hash"] == canonical_args_hash(self.ARGS)
        assert payload["approval_id"] != "stale"
        assert executor.calls == [], "the stale grant sent nothing"

        final = await graph.ainvoke(Command(resume="approve"), config=cfg)
        assert final["status"] is RunStatus.COMPLETED
        assert final["approval_state"].decisions[STEP].args_hash == payload["args_hash"]
        assert executor.calls == [STEP]


# ===========================================================================
# 2. Real Postgres: the repository primitive
# ===========================================================================
@pytest.fixture(scope="module")
def _database() -> None:
    require_database()
    migrate_to_head()


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        get_settings().database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=20,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
def uow_factory(session_factory: async_sessionmaker[AsyncSession]) -> UnitOfWorkFactory:
    return partial(unit_of_work, session_factory)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


#: Runs this module created, deleted again after each test: the cascade takes
#: their approvals and trace events with them, so a shared database does not
#: accumulate `pending` rows that would crowd the operator queue.
_CREATED_RUNS: list[uuid.UUID] = []


@pytest.fixture(autouse=True)
async def _cleanup(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    if not _CREATED_RUNS:
        return
    async with session_factory() as session:
        await session.execute(
            sa.text("DELETE FROM opspilot.agent_runs WHERE id = ANY(:ids)"),
            {"ids": list(_CREATED_RUNS)},
        )
        await session.commit()
    _CREATED_RUNS.clear()


async def create_run(
    uow_factory: UnitOfWorkFactory, *, status: RunStatus = RunStatus.RUNNING
) -> uuid.UUID:
    run_id = uuid.uuid4()
    _CREATED_RUNS.append(run_id)
    async with uow_factory() as uow:
        await uow.agent_runs.create(
            id=run_id,
            user_request="send the approved email",
            deadline_at=T0 + timedelta(minutes=5),
            status=status,
        )
        await uow.commit()
    return run_id


async def request(
    uow_factory: UnitOfWorkFactory,
    run_id: uuid.UUID,
    args: dict[str, Any],
    *,
    step_id: str = STEP,
    now: datetime = T0,
) -> ApprovalUpsert:
    async with uow_factory() as uow:
        result = await uow.approvals.upsert_request(
            run_id=run_id,
            step_id=step_id,
            tool=ToolName.SEND_EMAIL_MOCK,
            risk=RiskLevel.HIGH,
            title="Send outreach email",
            summary="scripted",
            payload_preview=args,
            args_hash=canonical_args_hash(args),
            requested_at=now,
            expires_at=now + TTL,
        )
        await uow.commit()
        return result


async def decide(
    uow_factory: UnitOfWorkFactory,
    approval_id: uuid.UUID,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    *,
    at: datetime = T0,
) -> None:
    async with uow_factory() as uow:
        row = await uow.approvals.decide(
            approval_id, status=status, decided_by="operator", decided_at=at
        )
        assert row is not None, "the human's conditional decision must win"
        await uow.commit()


async def rows_for(
    uow_factory: UnitOfWorkFactory, run_id: uuid.UUID, step_id: str = STEP
) -> list[ApprovalRow]:
    async with uow_factory() as uow:
        rows = await uow.approvals.list_by_run(run_id)
        await uow.commit()
    return [r for r in rows if r.step_id == step_id]


def by_status(rows: list[ApprovalRow], status: ApprovalStatus) -> list[ApprovalRow]:
    return [r for r in rows if r.status is status]


@pytest.mark.integration
@pytest.mark.usefixtures("_database")
class TestUpsertRequest:
    A: dict[str, Any] = {"draft_id": "d_1", "to_email": DANA}
    B: dict[str, Any] = {"draft_id": "d_1", "to_email": "someone-else@northwind.example"}

    async def test_the_first_request_inserts_a_pending_row(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)

        first = await request(uow_factory, run_id, self.A)

        assert first.created is True and first.superseded == ()
        assert first.row.status is ApprovalStatus.PENDING
        assert first.row.args_hash == canonical_args_hash(self.A)
        assert first.row.run_id == run_id and first.row.step_id == STEP
        assert first.row.expires_at == T0 + TTL
        assert [r.id for r in await rows_for(uow_factory, run_id)] == [first.row.id]

    async def test_an_identical_request_finds_its_own_row(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        first = await request(uow_factory, run_id, self.A)

        again = await request(uow_factory, run_id, self.A, now=T0 + timedelta(minutes=1))

        assert again.created is False and again.superseded == ()
        assert again.row.id == first.row.id
        assert again.row.status is ApprovalStatus.PENDING
        assert len(await rows_for(uow_factory, run_id)) == 1

    async def test_changed_arguments_supersede_the_open_request(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        first = await request(uow_factory, run_id, self.A)

        second = await request(uow_factory, run_id, self.B)

        assert second.created is True
        assert second.row.id != first.row.id
        assert second.row.args_hash == canonical_args_hash(self.B)
        assert [r.id for r in second.superseded] == [first.row.id]
        rows = {r.id: r for r in await rows_for(uow_factory, run_id)}
        assert rows[first.row.id].status is ApprovalStatus.SUPERSEDED
        assert rows[first.row.id].superseded_by == second.row.id
        assert rows[second.row.id].status is ApprovalStatus.PENDING
        assert len(by_status(list(rows.values()), ApprovalStatus.PENDING)) == 1

    @pytest.mark.parametrize("status", [ApprovalStatus.APPROVED, ApprovalStatus.REJECTED])
    async def test_a_decision_for_these_arguments_is_reused_never_re_asked(
        self, uow_factory: UnitOfWorkFactory, status: ApprovalStatus
    ) -> None:
        run_id = await create_run(uow_factory)
        first = await request(uow_factory, run_id, self.A)
        await decide(uow_factory, first.row.id, status)

        again = await request(uow_factory, run_id, self.A, now=T0 + timedelta(hours=1))

        assert again.created is False
        assert again.row.id == first.row.id and again.row.status is status
        assert again.row.decided_by == "operator"
        rows = await rows_for(uow_factory, run_id)
        assert len(rows) == 1 and by_status(rows, ApprovalStatus.PENDING) == []

    async def test_changed_arguments_after_a_grant_chain_it_forward(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        """§9.8: arguments changed since approval → the old approval is
        `superseded`, a new request is issued, and the old grant can no
        longer be found by the gate's lookup."""
        run_id = await create_run(uow_factory)
        first = await request(uow_factory, run_id, self.A)
        await decide(uow_factory, first.row.id)

        second = await request(uow_factory, run_id, self.B)

        assert second.created is True and second.row.status is ApprovalStatus.PENDING
        assert [r.id for r in second.superseded] == [first.row.id]
        rows = {r.id: r for r in await rows_for(uow_factory, run_id)}
        assert rows[first.row.id].status is ApprovalStatus.SUPERSEDED
        assert rows[first.row.id].superseded_by == second.row.id
        async with uow_factory() as uow:
            assert await uow.approvals.get_approved(run_id, STEP) is None
            await uow.commit()

    async def test_an_elapsed_approval_is_asked_afresh(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        """An approval is a grant bounded by its TTL — the gate refuses it
        past `expires_at` — so a request made after that asks again rather
        than handing back a grant nothing can mint from."""
        run_id = await create_run(uow_factory)
        first = await request(uow_factory, run_id, self.A)
        await decide(uow_factory, first.row.id)

        later = await request(uow_factory, run_id, self.A, now=T0 + TTL)

        assert later.created is True and later.row.id != first.row.id
        assert later.row.status is ApprovalStatus.PENDING
        rows = {r.id: r for r in await rows_for(uow_factory, run_id)}
        assert rows[first.row.id].status is ApprovalStatus.APPROVED, "history is not rewritten"

    @pytest.mark.parametrize(
        "status", [ApprovalStatus.EXPIRED, ApprovalStatus.CANCELLED, ApprovalStatus.SUPERSEDED]
    )
    async def test_historical_rows_neither_answer_nor_block_a_request(
        self, uow_factory: UnitOfWorkFactory, status: ApprovalStatus, session_factory: Any
    ) -> None:
        run_id = await create_run(uow_factory)
        first = await request(uow_factory, run_id, self.A)
        async with session_factory() as session:
            await session.execute(
                sa.text("UPDATE opspilot.approvals SET status = :status WHERE id = :id"),
                {"status": status.value, "id": first.row.id},
            )
            await session.commit()

        again = await request(uow_factory, run_id, self.A)

        assert again.created is True and again.row.id != first.row.id
        assert again.superseded == ()
        rows = {r.id: r for r in await rows_for(uow_factory, run_id)}
        assert rows[first.row.id].status is status and rows[first.row.id].superseded_by is None

    async def test_requests_for_different_steps_do_not_interfere(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        s6 = await request(uow_factory, run_id, self.A, step_id="s6")
        s7 = await request(uow_factory, run_id, self.A, step_id="s7")

        assert s6.created and s7.created and s6.row.id != s7.row.id
        assert s6.superseded == () and s7.superseded == ()


@pytest.mark.integration
@pytest.mark.usefixtures("_database")
class TestUpsertRequestUnderConcurrency:
    A: dict[str, Any] = {"draft_id": "d_1", "to_email": DANA}

    async def test_identical_concurrent_requests_produce_one_row_and_one_insert(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)

        results = await asyncio.gather(*(request(uow_factory, run_id, self.A) for _ in range(8)))

        assert sum(r.created for r in results) == 1, "exactly one winner"
        assert len({r.row.id for r in results}) == 1, "every loser reuses the winner's row"
        rows = await rows_for(uow_factory, run_id)
        assert len(rows) == 1 and rows[0].status is ApprovalStatus.PENDING

    async def test_competing_argument_hashes_leave_exactly_one_open_request(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        """Concurrent replans racing to request different arguments for the
        same step: each distinct request is inserted once, every earlier one
        is chained forward, and the partial index's promise — at most one
        `pending` per step — holds at the end and at every point between."""
        run_id = await create_run(uow_factory)
        variants = [{"draft_id": "d_1", "to_email": f"lead{i}@northwind.example"} for i in range(4)]

        results = await asyncio.gather(*(request(uow_factory, run_id, args) for args in variants))

        assert all(r.created for r in results), "each distinct hash is a genuine request"
        rows = await rows_for(uow_factory, run_id)
        assert len(rows) == 4
        (open_request,) = by_status(rows, ApprovalStatus.PENDING)
        chained = by_status(rows, ApprovalStatus.SUPERSEDED)
        assert len(chained) == 3
        ids = {r.id for r in rows}
        assert all(r.superseded_by in ids and r.superseded_by != r.id for r in chained)
        # The chain terminates at the one open request.
        for r in chained:
            head = r
            seen = set()
            while head.status is ApprovalStatus.SUPERSEDED:
                assert head.id not in seen
                seen.add(head.id)
                head = next(x for x in rows if x.id == head.superseded_by)
            assert head.id == open_request.id

    async def test_a_decision_landing_inside_the_request_window_is_observed(
        self, uow_factory: UnitOfWorkFactory, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The race the naive `SELECT → mark superseded → INSERT` gets wrong:
        a request for changed arguments reads the open request, then the
        human's `UPDATE … WHERE status = 'pending'` commits first. Here the
        decision's transaction holds the row lock until the request is
        blocked on its conditional close; the close then matches nothing,
        the request re-reads, finds an *approval* for other arguments, and
        chains that forward — the human's decision is never overwritten and
        the new arguments are still asked."""
        run_id = await create_run(uow_factory)
        first = await request(uow_factory, run_id, self.A)
        changed = {"draft_id": "d_1", "to_email": "changed@northwind.example"}

        async with session_factory() as human:
            # The human decides but has not committed: the row is locked.
            await human.execute(
                sa.text(
                    "UPDATE opspilot.approvals SET status = 'approved', decided_by = 'operator',"
                    " decided_at = :at WHERE id = :id AND status = 'pending'"
                ),
                {"at": T0, "id": first.row.id},
            )
            worker = asyncio.create_task(request(uow_factory, run_id, changed))
            await asyncio.sleep(0.3)  # the request is now blocked on the row lock
            assert not worker.done(), "the request must wait for the decision, not race it"
            await human.commit()
            second = await worker

        assert second.created is True and second.row.args_hash == canonical_args_hash(changed)
        assert [r.id for r in second.superseded] == [first.row.id]
        rows = {r.id: r for r in await rows_for(uow_factory, run_id)}
        assert rows[first.row.id].status is ApprovalStatus.SUPERSEDED
        assert rows[first.row.id].decided_by == "operator", "the decision was kept, then chained"
        assert rows[first.row.id].superseded_by == second.row.id
        assert rows[second.row.id].status is ApprovalStatus.PENDING

    async def test_replay_after_approval_never_regresses_to_pending(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        first = await request(uow_factory, run_id, self.A)
        await decide(uow_factory, first.row.id)

        replays = await asyncio.gather(*(request(uow_factory, run_id, self.A) for _ in range(8)))

        assert all(not r.created for r in replays)
        assert all(
            r.row.id == first.row.id and r.row.status is ApprovalStatus.APPROVED for r in replays
        )
        rows = await rows_for(uow_factory, run_id)
        assert len(rows) == 1 and by_status(rows, ApprovalStatus.PENDING) == []

    async def test_a_decision_racing_identical_replays_ends_decided_once(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        """Replays of the same request while the human decides it: whatever
        the interleaving, the step ends with one row, decided, and no
        replay re-opened it."""
        run_id = await create_run(uow_factory)
        first = await request(uow_factory, run_id, self.A)

        async def human() -> None:
            await asyncio.sleep(0.005)
            await decide(uow_factory, first.row.id)

        replays = [request(uow_factory, run_id, self.A) for _ in range(6)]
        results = await asyncio.gather(human(), *replays)

        assert all(not r.created and r.row.id == first.row.id for r in results[1:])
        rows = await rows_for(uow_factory, run_id)
        assert len(rows) == 1 and rows[0].status is ApprovalStatus.APPROVED

    async def test_requests_decisions_and_trace_writes_interleave_without_deadlock(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        """The node's transaction (request lock → approval row locks → run
        trace lock) and the approval service's (approval row lock → run
        trace lock) acquire in one consistent order. Mixed at volume they
        must all finish — Postgres would abort a cycle with a deadlock error
        — and leave the run's `seq` gapless."""
        run_id = await create_run(uow_factory)
        variants = [{"draft_id": "d_1", "to_email": f"lead{i}@northwind.example"} for i in range(3)]

        async def node(args: dict[str, Any]) -> None:
            async with uow_factory() as uow:
                result = await uow.approvals.upsert_request(
                    run_id=run_id,
                    step_id=STEP,
                    tool=ToolName.SEND_EMAIL_MOCK,
                    risk=RiskLevel.HIGH,
                    title="t",
                    summary="s",
                    payload_preview=args,
                    args_hash=canonical_args_hash(args),
                    requested_at=T0,
                    expires_at=T0 + TTL,
                )
                if result.created:
                    await uow.trace_events.append(
                        run_id=run_id,
                        kind=TraceEventKind.APPROVAL_REQUESTED,
                        step_id=STEP,
                        payload={"approval_id": str(result.row.id)},
                    )
                await uow.commit()

        async def human() -> None:
            for _ in range(10):
                async with uow_factory() as uow:
                    pending = await uow.approvals.get_pending(run_id, STEP)
                    if pending is not None:
                        decided = await uow.approvals.decide(
                            pending.id, status=ApprovalStatus.APPROVED, decided_at=T0
                        )
                        if decided is not None:
                            await uow.trace_events.append(
                                run_id=run_id,
                                kind=TraceEventKind.APPROVAL_GRANTED,
                                step_id=STEP,
                                payload={"approval_id": str(decided.id)},
                            )
                    await uow.commit()
                await asyncio.sleep(0)

        await asyncio.gather(
            *(node(args) for args in variants * 3), human(), human()
        )  # raises on any deadlock or constraint violation

        rows = await rows_for(uow_factory, run_id)
        assert len(by_status(rows, ApprovalStatus.PENDING)) <= 1
        async with uow_factory() as uow:
            events = await uow.trace_events.list_by_run(run_id, limit=1000)
            await uow.commit()
        assert [e.seq for e in events] == list(range(1, len(events) + 1)), "gapless"
        requested = [e for e in events if e.kind is TraceEventKind.APPROVAL_REQUESTED]
        assert len(requested) == len(rows), "one request event per inserted row"


# ===========================================================================
# 3. The compiled graph over the Postgres saver and the real registry
# ===========================================================================
@pytest.fixture
async def _seeded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await seed_database(session_factory, reset=True)


@pytest.fixture
def adapters(session_factory: async_sessionmaker[AsyncSession], clock: FixedClock) -> Adapters:
    return build_mock_adapters(session_factory, clock, SequentialIdGenerator())


@pytest.fixture
def registry(adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock) -> ToolRegistry:
    return ToolRegistry(adapters=adapters, uow_factory=uow_factory, clock=clock)


@pytest.fixture
def handlers(
    registry: ToolRegistry, uow_factory: UnitOfWorkFactory, clock: FixedClock
) -> NodeHandlers:
    return NodeHandlers(registry=registry, uow_factory=uow_factory, clock=clock, approval_ttl=TTL)


async def save_draft(adapters: Adapters, lead_id: str = "L-104") -> str:
    saved = await adapters.drafts.save(
        DraftInput(
            lead_id=lead_id,
            subject="Hello",
            body="A short note.",
            content_hash="0123456789abcdef0123",
        )
    )
    return saved.draft_id


async def outbox_rows(session_factory: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    async with session_factory() as session:
        res = await session.execute(
            sa.text(
                "SELECT idempotency_key, message_id, to_email, approval_id, run_id "
                "FROM mock_crm.email_outbox ORDER BY created_at"
            )
        )
        return [dict(r._mapping) for r in res]


async def crm_fingerprint(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    """A content hash of every `mock_crm` table: equal before and after means
    not one row was inserted, updated or deleted."""
    async with session_factory() as session:
        tables = [
            r[0]
            for r in await session.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'mock_crm' ORDER BY table_name"
                )
            )
        ]
        fingerprint: dict[str, str] = {}
        for table in tables:
            res = await session.execute(
                sa.text(
                    f"SELECT count(*)::text || ':' || coalesce(md5(string_agg(t::text, '|' "  # noqa: S608 - schema-listed identifier
                    f"ORDER BY t::text)), '') FROM mock_crm.{table} t"
                )
            )
            fingerprint[table] = res.scalar_one()
        return fingerprint


async def approval_events(uow_factory: UnitOfWorkFactory, run_id: uuid.UUID) -> list[Any]:
    async with uow_factory() as uow:
        events = await uow.trace_events.list_by_run(run_id, limit=1000)
        await uow.commit()
    return [e for e in events if e.kind.value.startswith("approval_")]


class Sendable:
    """A run with a planned `send_email_mock` step and a saved draft."""

    def __init__(self, run_id: uuid.UUID, draft_id: str) -> None:
        self.run_id = run_id
        self.args: dict[str, Any] = {"draft_id": draft_id, "to_email": DANA}
        self.cfg = thread_config(run_id)

    @classmethod
    async def create(
        cls,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        *,
        status: RunStatus = RunStatus.RUNNING,
    ) -> Sendable:
        run_id = await create_run(uow_factory, status=status)
        return cls(run_id, await save_draft(adapters))

    def initial(self, clock: FixedClock, **over: Any) -> AgentState:
        state = create_initial_state(
            run_id=self.run_id,
            user_request="Send outreach email",
            plan=send_plan(self.args),
            clock=clock,
        )
        state.update(over)
        return state


async def paused_value(graph: Any, cfg: Any) -> dict[str, Any]:
    snapshot = await graph.aget_state(cfg)
    (task,) = snapshot.tasks
    (pause,) = task.interrupts
    assert isinstance(pause.value, dict)
    return pause.value


@pytest.mark.integration
@pytest.mark.usefixtures("_database", "_seeded")
class TestGraphPausesDurably:
    async def test_pausing_persists_the_request_and_writes_nothing_to_the_crm(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        clock: FixedClock,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The acceptance case: zero `mock_crm` writes (whole-schema
        fingerprint before and after), one pending row, one
        `approval_requested`, and the interrupt names that row."""
        run = await Sendable.create(uow_factory, adapters)
        before = await crm_fingerprint(session_factory)

        async with open_checkpointer(get_settings()) as checkpointer:
            graph = create_agent_graph(checkpointer=checkpointer, node_handlers=handlers)
            paused = await graph.ainvoke(run.initial(clock), config=run.cfg, durability=DURABILITY)
            payload = await paused_value(graph, run.cfg)

        assert await crm_fingerprint(session_factory) == before
        assert paused["status"] is RunStatus.RUNNING and "s6" not in paused["tool_results"]

        (row,) = await rows_for(uow_factory, run.run_id)
        assert row.status is ApprovalStatus.PENDING
        assert row.args_hash == canonical_args_hash(run.args) == payload["args_hash"]
        assert row.tool == ToolName.SEND_EMAIL_MOCK.value and row.risk is RiskLevel.HIGH
        assert row.payload_preview == run.args
        assert row.requested_at == T0 and row.expires_at == T0 + TTL
        assert payload["approval_id"] == str(row.id)
        assert payload["run_id"] == str(run.run_id) and payload["step_id"] == STEP

        (event,) = await approval_events(uow_factory, run.run_id)
        assert event.kind is TraceEventKind.APPROVAL_REQUESTED
        assert event.step_id == STEP and event.node == "request_approval"
        assert event.status == "pending"
        assert event.payload["approval_id"] == str(row.id)
        assert event.payload["args_hash"] == row.args_hash
        assert event.payload["expires_at"] == row.expires_at.isoformat()

    async def test_re_entry_without_a_decision_adds_no_row_and_no_event(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        clock: FixedClock,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        run = await Sendable.create(uow_factory, adapters)
        async with open_checkpointer(get_settings()) as checkpointer:
            graph = create_agent_graph(checkpointer=checkpointer, node_handlers=handlers)
            await graph.ainvoke(run.initial(clock), config=run.cfg, durability=DURABILITY)
            first = await paused_value(graph, run.cfg)
            before = await crm_fingerprint(session_factory)

            # Re-enter twice with no decision (what a resume without a
            # stored decision, or a reconciler R4 pass, does).
            await graph.ainvoke(None, config=run.cfg, durability=DURABILITY)
            await graph.ainvoke(None, config=run.cfg, durability=DURABILITY)
            again = await paused_value(graph, run.cfg)

        assert again == first
        assert await crm_fingerprint(session_factory) == before
        assert len(await rows_for(uow_factory, run.run_id)) == 1
        assert [e.kind for e in await approval_events(uow_factory, run.run_id)] == [
            TraceEventKind.APPROVAL_REQUESTED
        ]

    async def test_a_crash_between_the_request_commit_and_the_checkpoint_is_idempotent(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        clock: FixedClock,
    ) -> None:
        """The worker committed the request transaction and died before
        `interrupt()` was checkpointed. Re-entry runs the node from the top:
        it must find the committed row and event, not add a second pair."""
        run = await Sendable.create(uow_factory, adapters)
        step = send_plan(run.args).step(STEP)
        assert step is not None
        # Exactly the node's transaction, committed; then no checkpoint.
        committed = await handlers._persist_approval_request(
            uow_factory,
            run_id=run.run_id,
            step=step,
            args_hash=canonical_args_hash(run.args),
            preview=run.args,
        )
        assert committed.decision is None

        async with open_checkpointer(get_settings()) as checkpointer:
            graph = create_agent_graph(checkpointer=checkpointer, node_handlers=handlers)
            await graph.ainvoke(run.initial(clock), config=run.cfg, durability=DURABILITY)
            payload = await paused_value(graph, run.cfg)

        assert payload["approval_id"] == committed.approval_id
        (row,) = await rows_for(uow_factory, run.run_id)
        assert str(row.id) == committed.approval_id
        assert [e.kind for e in await approval_events(uow_factory, run.run_id)] == [
            TraceEventKind.APPROVAL_REQUESTED
        ]


@pytest.mark.integration
@pytest.mark.usefixtures("_database", "_seeded")
class TestGraphResumes:
    async def test_approve_resumes_into_exactly_one_send(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        clock: FixedClock,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """approve → resume → `request_approval` (finds the approved row,
        no second request) → `decide` (rule 6 grants) → `execute_tool`
        (mints from that row) → one outbox row traceable to it."""
        run = await Sendable.create(uow_factory, adapters)
        async with open_checkpointer(get_settings()) as checkpointer:
            graph = create_agent_graph(checkpointer=checkpointer, node_handlers=handlers)
            await graph.ainvoke(run.initial(clock), config=run.cfg, durability=DURABILITY)
            payload = await paused_value(graph, run.cfg)
            approval_id = uuid.UUID(payload["approval_id"])
            await decide(uow_factory, approval_id)

            final = await graph.ainvoke(
                Command(resume="approve"), config=run.cfg, durability=DURABILITY
            )

        assert final["status"] is RunStatus.COMPLETED
        decision = final["approval_state"].decisions[STEP]
        assert decision.approval_id == str(approval_id), "the durable row, not the resume value"
        assert decision.args_hash == payload["args_hash"]
        assert decision.decided_by == "operator" and decision.decided_at == T0
        assert [c.status for c in final["tool_calls"]] == ["succeeded"]
        (sent,) = await outbox_rows(session_factory)
        assert sent["approval_id"] == str(approval_id) and sent["to_email"] == DANA
        assert len(await rows_for(uow_factory, run.run_id)) == 1, "resume added no request"
        assert [e.kind for e in await approval_events(uow_factory, run.run_id)] == [
            TraceEventKind.APPROVAL_REQUESTED
        ], "resume emitted no duplicate request event"

    async def test_reject_resumes_into_rejected_with_no_effect(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        clock: FixedClock,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        run = await Sendable.create(uow_factory, adapters)
        before = await crm_fingerprint(session_factory)
        async with open_checkpointer(get_settings()) as checkpointer:
            graph = create_agent_graph(checkpointer=checkpointer, node_handlers=handlers)
            await graph.ainvoke(run.initial(clock), config=run.cfg, durability=DURABILITY)
            payload = await paused_value(graph, run.cfg)
            await decide(uow_factory, uuid.UUID(payload["approval_id"]), ApprovalStatus.REJECTED)

            final = await graph.ainvoke(
                Command(resume="reject"), config=run.cfg, durability=DURABILITY
            )

        assert final["status"] is RunStatus.REJECTED
        assert final["status_reason"] == "approval_rejected"
        assert final["tool_calls"] == [] and final["errors"] == []
        assert final["plan"].step(STEP).status is StepStatus.REJECTED
        assert await crm_fingerprint(session_factory) == before
        assert len(await rows_for(uow_factory, run.run_id)) == 1
        assert [e.kind for e in await approval_events(uow_factory, run.run_id)] == [
            TraceEventKind.APPROVAL_REQUESTED
        ]

    async def test_replay_from_the_durable_checkpoint_in_a_fresh_process(
        self,
        registry: ToolRegistry,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        clock: FixedClock,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The process that paused is gone; a new saver, graph and handlers
        resume from the checkpoint alone."""
        run = await Sendable.create(uow_factory, adapters)
        async with open_checkpointer(get_settings()) as first_saver:
            graph = create_agent_graph(
                checkpointer=first_saver,
                node_handlers=NodeHandlers(
                    registry=registry, uow_factory=uow_factory, clock=clock, approval_ttl=TTL
                ),
            )
            await graph.ainvoke(run.initial(clock), config=run.cfg, durability=DURABILITY)
            payload = await paused_value(graph, run.cfg)
        await decide(uow_factory, uuid.UUID(payload["approval_id"]))

        async with open_checkpointer(get_settings()) as second_saver:
            replay = create_agent_graph(
                checkpointer=second_saver,
                node_handlers=NodeHandlers(
                    registry=registry, uow_factory=uow_factory, clock=clock, approval_ttl=TTL
                ),
            )
            final = await replay.ainvoke(
                Command(resume="approve"), config=run.cfg, durability=DURABILITY
            )

        assert final["status"] is RunStatus.COMPLETED
        (sent,) = await outbox_rows(session_factory)
        assert sent["approval_id"] == payload["approval_id"]
        assert len(await rows_for(uow_factory, run.run_id)) == 1

    async def test_a_decision_made_while_the_worker_was_away_is_found_on_re_entry(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        clock: FixedClock,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Approval arrives during replay: the graph is re-entered with *no*
        resume value after the human approved. The node finds the approved
        row, records it, does not pause again, does not regress the row,
        and the run completes with one send."""
        run = await Sendable.create(uow_factory, adapters)
        async with open_checkpointer(get_settings()) as checkpointer:
            graph = create_agent_graph(checkpointer=checkpointer, node_handlers=handlers)
            await graph.ainvoke(run.initial(clock), config=run.cfg, durability=DURABILITY)
            payload = await paused_value(graph, run.cfg)
            await decide(uow_factory, uuid.UUID(payload["approval_id"]))

            final = await graph.ainvoke(None, config=run.cfg, durability=DURABILITY)

        assert final["status"] is RunStatus.COMPLETED
        assert final["approval_state"].decisions[STEP].approval_id == payload["approval_id"]
        (row,) = await rows_for(uow_factory, run.run_id)
        assert row.status is ApprovalStatus.APPROVED, "never regressed to pending"
        (sent,) = await outbox_rows(session_factory)
        assert sent["approval_id"] == payload["approval_id"]
        assert [e.kind for e in await approval_events(uow_factory, run.run_id)] == [
            TraceEventKind.APPROVAL_REQUESTED
        ]

    async def test_the_reconciler_recovers_a_paused_run_whose_decision_landed(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        clock: FixedClock,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Crash around resume (ADR-023): the decision committed, the worker
        died before resuming, the run row was left `running` under a lease
        that then expired. The reconciler resumes the production graph with
        the stored decision; `request_approval` re-executes and finds the
        approved row; one send."""
        run = await Sendable.create(uow_factory, adapters)
        await leave_stale_lease(uow_factory, run.run_id, owner="worker-dead", acquired_at=T0)
        async with open_checkpointer(get_settings()) as checkpointer:
            graph = create_agent_graph(checkpointer=checkpointer, node_handlers=handlers)
            await graph.ainvoke(run.initial(clock), config=run.cfg, durability=DURABILITY)
            payload = await paused_value(graph, run.cfg)
            await decide(uow_factory, uuid.UUID(payload["approval_id"]))

            clock.set(T0 + LEASE.ttl + timedelta(seconds=1))

            async def instant(_seconds: float) -> None:
                await asyncio.sleep(0)

            reconciler = Reconciler(
                uow_factory=uow_factory,
                driver=LangGraphRunDriver(graph),
                clock=clock,
                owner="reconciler-1",
                lease=LEASE,
                sleep=instant,
            )
            outcome = await reconciler.recover_run(run.run_id)

        assert outcome is RecoveryOutcome.RESUMED
        async with uow_factory() as uow:
            run_row = await uow.agent_runs.get(run.run_id)
            await uow.commit()
        assert run_row is not None and run_row.status is RunStatus.COMPLETED
        (sent,) = await outbox_rows(session_factory)
        assert sent["approval_id"] == payload["approval_id"]
        assert len(await rows_for(uow_factory, run.run_id)) == 1
        assert [e.kind for e in await approval_events(uow_factory, run.run_id)] == [
            TraceEventKind.APPROVAL_REQUESTED
        ]


@pytest.mark.integration
@pytest.mark.usefixtures("_database", "_seeded")
class TestChangedArgumentsAfterAGrant:
    async def test_changed_arguments_chain_the_grant_forward_and_ask_again(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        clock: FixedClock,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A human approved the step for other arguments (row and state
        agree). The plan now sends different ones: rule 6 does not grant,
        the node supersedes the old approval, requests afresh, emits
        `approval_superseded` then `approval_requested`, and pauses. The
        old grant can mint nothing; approving the new request sends once,
        traceable to the new row."""
        run = await Sendable.create(uow_factory, adapters)
        old_args = {"draft_id": run.args["draft_id"], "to_email": "old@northwind.example"}
        old = await request(uow_factory, run.run_id, old_args)
        await decide(uow_factory, old.row.id)
        initial = run.initial(
            clock,
            approval_state=ApprovalState(
                decisions={STEP: approve_decision(old_args, approval_id=str(old.row.id))}
            ),
        )
        before = await crm_fingerprint(session_factory)

        async with open_checkpointer(get_settings()) as checkpointer:
            graph = create_agent_graph(checkpointer=checkpointer, node_handlers=handlers)
            await graph.ainvoke(initial, config=run.cfg, durability=DURABILITY)
            payload = await paused_value(graph, run.cfg)

            assert await crm_fingerprint(session_factory) == before, "the stale grant sent nothing"
            assert payload["args_hash"] == canonical_args_hash(run.args)
            new_id = uuid.UUID(payload["approval_id"])
            assert new_id != old.row.id
            rows = {r.id: r for r in await rows_for(uow_factory, run.run_id)}
            assert rows[old.row.id].status is ApprovalStatus.SUPERSEDED
            assert rows[old.row.id].superseded_by == new_id
            assert rows[new_id].status is ApprovalStatus.PENDING
            events = await approval_events(uow_factory, run.run_id)
            assert [e.kind for e in events] == [
                TraceEventKind.APPROVAL_SUPERSEDED,
                TraceEventKind.APPROVAL_REQUESTED,
            ]
            assert events[0].payload == {
                "approval_id": str(old.row.id),
                "superseded_by": str(new_id),
                "args_hash": canonical_args_hash(old_args),
            }
            assert events[1].payload["approval_id"] == str(new_id)
            async with uow_factory() as uow:
                assert await uow.approvals.get_approved(run.run_id, STEP) is None
                await uow.commit()

            await decide(uow_factory, new_id)
            final = await graph.ainvoke(
                Command(resume="approve"), config=run.cfg, durability=DURABILITY
            )

        assert final["status"] is RunStatus.COMPLETED
        assert final["approval_state"].decisions[STEP].approval_id == str(new_id)
        assert final["approval_state"].decisions[STEP].args_hash == payload["args_hash"]
        (sent,) = await outbox_rows(session_factory)
        assert sent["approval_id"] == str(new_id)
        assert len(await rows_for(uow_factory, run.run_id)) == 2
