"""HITL-002 — `ApprovalGate.issue_from_persisted`: the durable approved row
becomes the execution-time capability, and nothing else does (§9.4-§9.5,
§12.6, ADR-010, ADR-024).

Three layers, in the order the invariant is built:

1. **The gate as a pure function** (unit): a plain record double satisfies
   `ApprovalRecordProtocol`, so every refusal — missing, not approved, wrong
   run/step/tool, superseded, expired, wrong arguments — is asserted without
   a database, and so is the token's provenance guarantee (no constructor,
   no `dataclasses.replace`, no mutation).
2. **The lookup and the wiring against real Postgres** (integration): the
   repository's `get_approved` picks the *current* decision among historical
   rows; `execute_tool` mints from it and hands the token to the dispatcher;
   expiry and supersession are asserted from persisted state moved by the
   injected clock and by real rows, never by a mocked answer.
3. **Defense in depth** (integration): a gate-minted token replayed across
   runs, steps and tools, or after the row moved, is refused by the
   dispatcher too; two concurrent, individually valid authorisations produce
   one effect; and the whole agent graph pauses, resumes and sends exactly
   once through `execute_tool`.

Every test scripts the *human* (a stored decision made through the real
repository) and never the *gate*.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
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
)
from app.config import get_settings
from app.errors import ApprovalInvalidError, ApprovalRequiredError, ErrorClass, PolicyViolation
from app.integrations.mock import build_mock_adapters, seed_database
from app.integrations.ports import Adapters, DraftInput
from app.persistence.checkpointing import DURABILITY, open_checkpointer, thread_config
from app.persistence.models import ApprovalRow, ToolCallStatus
from app.persistence.protocols import UnitOfWorkFactory
from app.persistence.session import create_session_factory, unit_of_work
from app.runtime import FixedClock, SequentialIdGenerator
from app.security import (
    APPROVED_STATUS,
    ApprovalGate,
    ApprovalRecordProtocol,
    ApprovalToken,
    canonical_args_hash,
)
from app.tools.contracts import RiskLevel, ToolName
from app.tools.registry import ToolRegistry
from langgraph.types import Command
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from recovery_harness import T0, migrate_to_head, require_database

DANA = "dana@northwind.example"  # lead L-104's stored address (fixtures)
RUN = uuid.UUID("00000000-0000-0000-0000-00000000a001")
OTHER_RUN = uuid.UUID("00000000-0000-0000-0000-00000000b002")
APPROVAL = uuid.UUID("00000000-0000-0000-0000-00000000c003")
ARGS: dict[str, Any] = {"draft_id": "d_1", "to_email": DANA}
TTL = timedelta(minutes=30)


# ===========================================================================
# 1. The gate as a pure function over a record
# ===========================================================================
@dataclass(frozen=True)
class Record:
    """A plain object that satisfies `ApprovalRecordProtocol` — the gate
    needs these columns of §12.6 and nothing about how they were loaded."""

    id: uuid.UUID
    run_id: uuid.UUID
    step_id: str
    tool: str
    status: str
    args_hash: str
    superseded_by: uuid.UUID | None
    expires_at: datetime


def approved(**over: Any) -> Record:
    fields: dict[str, Any] = {
        "id": APPROVAL,
        "run_id": RUN,
        "step_id": "s6",
        "tool": ToolName.SEND_EMAIL_MOCK.value,
        "status": ApprovalStatus.APPROVED.value,
        "args_hash": canonical_args_hash(ARGS),
        "superseded_by": None,
        "expires_at": T0 + TTL,
    }
    fields.update(over)
    return Record(**fields)


def issue(record: ApprovalRecordProtocol | None, **over: Any) -> ApprovalToken:
    call: dict[str, Any] = {
        "run_id": str(RUN),
        "step_id": "s6",
        "tool": ToolName.SEND_EMAIL_MOCK.value,
        "args": ARGS,
        "now": T0,
    }
    call.update(over)
    return ApprovalGate.issue_from_persisted(record, **call)


@pytest.mark.unit
class TestGateIssuesFromAnApprovedRecord:
    def test_a_valid_approved_record_mints_a_token_bound_to_the_row(self) -> None:
        token = issue(approved())
        assert isinstance(token, ApprovalToken)
        assert token.approval_id == str(APPROVAL)
        assert token.run_id == str(RUN)
        assert token.step_id == "s6"
        assert token.args_hash == canonical_args_hash(ARGS)
        assert token.authorises(run_id=str(RUN), step_id="s6", args=ARGS)

    def test_the_token_hash_equals_the_row_hash_equals_the_argument_hash(self) -> None:
        """The invariant HITL-002 exists to hold:
        canonical_args_hash(resolved) == approval.args_hash == token.args_hash."""
        record = approved()
        token = issue(record)
        assert canonical_args_hash(ARGS) == record.args_hash == token.args_hash

    def test_a_retry_with_a_new_idempotency_key_still_mints(self) -> None:
        """Volatile keys are outside the binding (§9.4): the grant survives
        a retry, which carries a fresh key but the same operation."""
        token = issue(approved(), args={**ARGS, "idempotency_key": "attempt-2"})
        assert token.args_hash == canonical_args_hash(ARGS)

    def test_no_stated_tool_skips_only_the_tool_check(self) -> None:
        assert issue(approved(), tool=None).approval_id == str(APPROVAL)
        with pytest.raises(ApprovalInvalidError):
            issue(approved(args_hash="other"), tool=None)

    def test_the_approved_status_literal_is_pinned_to_the_enum(self) -> None:
        """security.py is a leaf and cannot import `ApprovalStatus`; this is
        the one place the two spellings are tied together."""
        assert ApprovalStatus.APPROVED.value == APPROVED_STATUS
        assert ApprovalStatus.APPROVED == APPROVED_STATUS  # StrEnum equality

    def test_an_orm_row_satisfies_the_record_protocol_at_runtime(self) -> None:
        """A transient `ApprovalRow` (never added to a session) is what the
        repository hands the node; the gate reads it like any record."""
        row = ApprovalRow(
            id=APPROVAL,
            run_id=RUN,
            step_id="s6",
            tool=ToolName.SEND_EMAIL_MOCK,
            risk=RiskLevel.HIGH,
            title="t",
            summary="s",
            payload_preview={},
            args_hash=canonical_args_hash(ARGS),
            status=ApprovalStatus.APPROVED,
            requested_at=T0,
            expires_at=T0 + TTL,
        )
        assert issue(row).approval_id == str(APPROVAL)


@pytest.mark.unit
class TestGateRefusals:
    """Each refusal is a `PolicyViolation` (terminal, never retried) and the
    detail names every binding that failed — never a fallback, never the
    "closest" approval."""

    def test_no_record_is_approval_required(self) -> None:
        with pytest.raises(ApprovalRequiredError) as exc:
            issue(None)
        assert isinstance(exc.value, PolicyViolation)
        assert exc.value.error_class is ErrorClass.POLICY_VIOLATION
        assert exc.value.detail == {
            "run_id": str(RUN),
            "step_id": "s6",
            "tool": ToolName.SEND_EMAIL_MOCK.value,
        }

    @pytest.mark.parametrize(
        "status",
        [
            ApprovalStatus.PENDING,
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.SUPERSEDED,
            ApprovalStatus.CANCELLED,
        ],
    )
    def test_only_an_approved_status_mints(self, status: ApprovalStatus) -> None:
        with pytest.raises(ApprovalInvalidError) as exc:
            issue(approved(status=status.value))
        assert exc.value.detail["mismatch"] == [f"status={status.value}"]
        assert exc.value.detail["approval_id"] == str(APPROVAL)

    def test_a_record_for_another_run_is_refused(self) -> None:
        with pytest.raises(ApprovalInvalidError) as exc:
            issue(approved(run_id=OTHER_RUN))
        assert exc.value.detail["mismatch"] == ["run_id"]

    def test_a_record_for_another_step_is_refused(self) -> None:
        with pytest.raises(ApprovalInvalidError) as exc:
            issue(approved(step_id="s9"))
        assert exc.value.detail["mismatch"] == ["step_id"]

    def test_a_record_for_another_tool_is_refused(self) -> None:
        """Same step, same arguments, approved for a different tool: no."""
        with pytest.raises(ApprovalInvalidError) as exc:
            issue(approved(tool=ToolName.UPDATE_CUSTOMER.value))
        assert exc.value.detail["mismatch"] == ["tool"]

    def test_a_superseded_record_is_refused_even_if_still_marked_approved(self) -> None:
        """`superseded_by` is checked independently of `status`: a row that a
        later mechanism chains forward without moving its status is dead."""
        with pytest.raises(ApprovalInvalidError) as exc:
            issue(approved(superseded_by=uuid.uuid4()))
        assert exc.value.detail["mismatch"] == ["superseded"]

    def test_an_approved_record_past_its_ttl_is_refused(self) -> None:
        with pytest.raises(ApprovalInvalidError) as exc:
            issue(approved(), now=T0 + TTL + timedelta(seconds=1))
        assert exc.value.detail["mismatch"] == ["expired"]

    def test_the_expiry_boundary_is_closed(self) -> None:
        """`now == expires_at` is expired (the same comparison the approval
        service uses); one microsecond earlier is still inside the window."""
        with pytest.raises(ApprovalInvalidError):
            issue(approved(), now=T0 + TTL)
        assert issue(approved(), now=T0 + TTL - timedelta(microseconds=1))

    def test_changed_arguments_are_refused(self) -> None:
        """§9.9 (5): a grant for hash A does not authorise arguments hashing to B."""
        with pytest.raises(ApprovalInvalidError) as exc:
            issue(approved(), args={**ARGS, "to_email": "ceo@acme.example"})
        assert exc.value.detail["mismatch"] == ["args_hash"]

    def test_every_failed_binding_is_reported_and_none_is_forgiven(self) -> None:
        wrong = approved(
            run_id=OTHER_RUN,
            step_id="s9",
            tool=ToolName.UPDATE_CUSTOMER.value,
            status=ApprovalStatus.REJECTED.value,
            superseded_by=uuid.uuid4(),
            args_hash="other",
        )
        with pytest.raises(ApprovalInvalidError) as exc:
            issue(wrong, now=T0 + TTL)
        assert exc.value.detail["mismatch"] == [
            "status=rejected",
            "run_id",
            "step_id",
            "tool",
            "superseded",
            "expired",
            "args_hash",
        ]


@pytest.mark.unit
class TestTokenProvenance:
    """The token is an in-process capability: only the gate can produce one,
    and an instance cannot be turned into a different one."""

    def test_direct_construction_is_refused(self) -> None:
        with pytest.raises(PolicyViolation):
            ApprovalToken(
                approval_id=str(APPROVAL),
                run_id=str(RUN),
                step_id="s6",
                args_hash=canonical_args_hash(ARGS),
            )

    def test_a_guessed_sentinel_is_refused(self) -> None:
        """The sentinel is compared by identity; an equal-looking object is
        not it."""
        with pytest.raises(PolicyViolation):
            ApprovalToken(
                approval_id=str(APPROVAL),
                run_id=str(RUN),
                step_id="s6",
                args_hash=canonical_args_hash(ARGS),
                mint=object(),
            )

    def test_a_minted_token_cannot_be_mutated(self) -> None:
        token = issue(approved())
        for field in ("approval_id", "run_id", "step_id", "args_hash"):
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(token, field, "forged")

    def test_a_minted_token_cannot_be_copied_with_changes(self) -> None:
        """`dataclasses.replace` re-runs `__init__` without the sentinel, so a
        valid token cannot be rebound to other arguments, a step or a run."""
        token = issue(approved())
        for change in (
            {"args_hash": canonical_args_hash({**ARGS, "to_email": "x@acme.example"})},
            {"step_id": "s9"},
            {"run_id": str(OTHER_RUN)},
        ):
            with pytest.raises(PolicyViolation):
                dataclasses.replace(token, **change)

    def test_the_sentinel_never_appears_on_the_instance(self) -> None:
        from app.security import _MINT

        token = issue(approved())
        assert set(vars(token)) == {"approval_id", "run_id", "step_id", "args_hash"}
        assert _MINT not in vars(token).values()


# ===========================================================================
# 2. Real Postgres: lookup, wiring, expiry and supersession
# ===========================================================================
@pytest.fixture(scope="module", autouse=True)
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
async def _seeded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await seed_database(session_factory, reset=True)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def adapters(session_factory: async_sessionmaker[AsyncSession], clock: FixedClock) -> Adapters:
    return build_mock_adapters(session_factory, clock, SequentialIdGenerator())


@pytest.fixture
def registry(adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock) -> ToolRegistry:
    """The production registry: the nine real implementations over the real
    mock adapters. Nothing on the path to the outbox is doubled."""
    return ToolRegistry(adapters=adapters, uow_factory=uow_factory, clock=clock)


@pytest.fixture
def handlers(
    registry: ToolRegistry, uow_factory: UnitOfWorkFactory, clock: FixedClock
) -> NodeHandlers:
    return NodeHandlers(registry=registry, uow_factory=uow_factory, clock=clock)


async def create_run(uow_factory: UnitOfWorkFactory) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with uow_factory() as uow:
        await uow.agent_runs.create(
            id=run_id,
            user_request="send the approved email",
            deadline_at=T0 + timedelta(minutes=5),
            status=RunStatus.RUNNING,
        )
        await uow.commit()
    return run_id


async def create_step(
    uow_factory: UnitOfWorkFactory, run_id: uuid.UUID, step_id: str, tool: ToolName
) -> uuid.UUID:
    async with uow_factory() as uow:
        step = await uow.execution_steps.create(
            run_id=run_id, step_id=step_id, plan_revision=0, seq=1, tool=tool
        )
        await uow.commit()
        return step.id


async def script_decision(
    uow_factory: UnitOfWorkFactory,
    *,
    run_id: uuid.UUID,
    step_id: str,
    args: dict[str, Any],
    tool: ToolName = ToolName.SEND_EMAIL_MOCK,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    requested_at: datetime = T0,
    expires_at: datetime | None = None,
    decided_at: datetime = T0,
) -> uuid.UUID:
    """Script the human through the real repository: a request, then (unless
    it is to stay pending) the conditional `decide` the API performs."""
    async with uow_factory() as uow:
        row = await uow.approvals.create_request(
            run_id=run_id,
            step_id=step_id,
            tool=tool,
            risk=RiskLevel.HIGH,
            title="Send outreach email",
            summary="scripted",
            payload_preview={},
            args_hash=canonical_args_hash(args),
            requested_at=requested_at,
            expires_at=expires_at or (requested_at + TTL),
        )
        if status is not ApprovalStatus.PENDING:
            decided = await uow.approvals.decide(
                row.id, status=status, decided_by="operator", decided_at=decided_at
            )
            assert decided is not None
        await uow.commit()
        return row.id


async def get_approved(
    uow_factory: UnitOfWorkFactory, run_id: uuid.UUID, step_id: str
) -> ApprovalRow | None:
    async with uow_factory() as uow:
        row = await uow.approvals.get_approved(run_id, step_id)
        await uow.commit()
        return row


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


async def tool_call_statuses(
    uow_factory: UnitOfWorkFactory, step_uuid: uuid.UUID
) -> list[ToolCallStatus]:
    async with uow_factory() as uow:
        rows = await uow.tool_calls.list_by_step(step_uuid)
        await uow.commit()
        return [r.status for r in rows]


def _sf(uow_factory: UnitOfWorkFactory) -> async_sessionmaker[AsyncSession]:
    return uow_factory.args[0]  # type: ignore[attr-defined]


def gated_state(
    run_id: uuid.UUID,
    *,
    args: dict[str, Any],
    granted_for: dict[str, Any] | None = None,
    step_id: str = "s6",
    tool: ToolName = ToolName.SEND_EMAIL_MOCK,
    attempt: int = 1,
) -> AgentState:
    """A run positioned at `execute_tool` for a gated step, with barrier 2
    (`approval_state`) satisfied for `granted_for` (defaults to `args`) — so
    what is exercised is barrier 3, the durable row."""
    granted = granted_for if granted_for is not None else args
    plan = Plan(
        plan_id=f"p_{run_id.hex[:8]}",
        steps=[PlanStep(step_id=step_id, tool=tool, args=args, status=StepStatus.PENDING)],
    )
    state = create_initial_state(
        run_id=run_id, user_request="send", plan=plan, clock=FixedClock(T0)
    )
    state["current_step_id"] = step_id
    state["status"] = RunStatus.RUNNING
    state["retry_count"] = {step_id: attempt - 1}
    state["approval_state"] = ApprovalState(
        decisions={
            step_id: ApprovalDecision(
                approval_id="scripted",
                step_id=step_id,
                decision=ApprovalDecisionKind.APPROVE,
                args_hash=canonical_args_hash(granted),
                decided_by="operator",
                decided_at=T0,
            )
        }
    )
    return state


def refusal(delta: dict[str, Any]) -> Any:
    assert "tool_results" not in delta, "a refused step must produce no result"
    (err,) = delta["errors"]
    assert err.error_class is ErrorClass.POLICY_VIOLATION
    assert err.recovery is not None and err.recovery.value == "fail"
    (call,) = delta["tool_calls"]
    assert call.status == "failed" and call.error_class is ErrorClass.POLICY_VIOLATION
    return err


class Sendable:
    """A run with a planned `send_email_mock` step and a saved draft."""

    def __init__(self, run_id: uuid.UUID, step_uuid: uuid.UUID, draft_id: str) -> None:
        self.run_id = run_id
        self.step_id = "s6"
        self.step_uuid = step_uuid
        self.draft_id = draft_id
        self.args: dict[str, Any] = {"draft_id": draft_id, "to_email": DANA}

    @classmethod
    async def create(cls, uow_factory: UnitOfWorkFactory, adapters: Adapters) -> Sendable:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s6", ToolName.SEND_EMAIL_MOCK)
        return cls(run_id, step_uuid, await save_draft(adapters))

    async def approve(self, uow_factory: UnitOfWorkFactory, **over: Any) -> uuid.UUID:
        return await script_decision(
            uow_factory, run_id=self.run_id, step_id=self.step_id, args=self.args, **over
        )

    def state(self, **over: Any) -> AgentState:
        return gated_state(self.run_id, args=self.args, **over)


@pytest.mark.integration
@pytest.mark.usefixtures("_seeded")
class TestApprovedRowLookup:
    """`ApprovalRepository.get_approved` — the SQL lives in persistence; the
    gate only ever sees the row it returns."""

    async def test_returns_only_an_approved_row_for_exactly_this_run_and_step(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        other = await create_run(uow_factory)
        assert await get_approved(uow_factory, run_id, "s6") is None

        await script_decision(uow_factory, run_id=other, step_id="s6", args=ARGS)
        await script_decision(uow_factory, run_id=run_id, step_id="s7", args=ARGS)
        assert await get_approved(uow_factory, run_id, "s6") is None, (
            "another run's / another step's approval must not surface"
        )

        approval_id = await script_decision(uow_factory, run_id=run_id, step_id="s6", args=ARGS)
        row = await get_approved(uow_factory, run_id, "s6")
        assert row is not None and row.id == approval_id
        assert row.status is ApprovalStatus.APPROVED

    @pytest.mark.parametrize(
        "status",
        [
            ApprovalStatus.PENDING,
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.CANCELLED,
        ],
    )
    async def test_a_non_approved_row_is_not_found(
        self, uow_factory: UnitOfWorkFactory, status: ApprovalStatus
    ) -> None:
        run_id = await create_run(uow_factory)
        await script_decision(uow_factory, run_id=run_id, step_id="s6", args=ARGS, status=status)
        assert await get_approved(uow_factory, run_id, "s6") is None

    async def test_a_superseded_row_is_not_found(self, uow_factory: UnitOfWorkFactory) -> None:
        run_id = await create_run(uow_factory)
        old_id = await script_decision(
            uow_factory, run_id=run_id, step_id="s6", args=ARGS, status=ApprovalStatus.PENDING
        )
        async with uow_factory() as uow:
            new = await uow.approvals.create_request(
                run_id=run_id,
                step_id="s7",
                tool=ToolName.SEND_EMAIL_MOCK,
                risk=RiskLevel.HIGH,
                title="t",
                summary="s",
                payload_preview={},
                args_hash="other",
                requested_at=T0,
                expires_at=T0 + TTL,
            )
            assert await uow.approvals.supersede(old_id, new.id) is not None
            await uow.commit()
        assert await get_approved(uow_factory, run_id, "s6") is None

    async def test_the_latest_decision_wins_among_historical_approvals(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        """A replan re-requests with new arguments (§9.3); once that is
        approved too, the step has two approved rows. The newer decision is
        the current one — an older grant never outranks it."""
        run_id = await create_run(uow_factory)
        older = await script_decision(
            uow_factory, run_id=run_id, step_id="s6", args=ARGS, decided_at=T0
        )
        newer_args = {**ARGS, "draft_id": "d_2"}
        newer = await script_decision(
            uow_factory,
            run_id=run_id,
            step_id="s6",
            args=newer_args,
            requested_at=T0 + timedelta(minutes=1),
            decided_at=T0 + timedelta(minutes=2),
        )
        row = await get_approved(uow_factory, run_id, "s6")
        assert row is not None and row.id == newer and row.id != older
        assert row.args_hash == canonical_args_hash(newer_args)

        # And the gate binds that one row: the old arguments no longer mint.
        with pytest.raises(ApprovalInvalidError) as exc:
            ApprovalGate.issue_from_persisted(
                row,
                run_id=str(run_id),
                step_id="s6",
                tool=ToolName.SEND_EMAIL_MOCK.value,
                args=ARGS,
                now=T0 + timedelta(minutes=3),
            )
        assert exc.value.detail["mismatch"] == ["args_hash"]
        token = ApprovalGate.issue_from_persisted(
            row,
            run_id=str(run_id),
            step_id="s6",
            tool=ToolName.SEND_EMAIL_MOCK.value,
            args=newer_args,
            now=T0 + timedelta(minutes=3),
        )
        assert token.approval_id == str(newer)

    async def test_the_lookup_reads_the_row_as_it_is_now(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        """Populate-existing: a session that loaded the row before another
        transaction decided it must not answer from its stale copy."""
        run_id = await create_run(uow_factory)
        approval_id = await script_decision(
            uow_factory, run_id=run_id, step_id="s6", args=ARGS, status=ApprovalStatus.PENDING
        )
        async with uow_factory() as uow:
            stale = await uow.approvals.get(approval_id)
            assert stale is not None and stale.status is ApprovalStatus.PENDING
            async with uow_factory() as other:
                decided = await other.approvals.decide(
                    approval_id, status=ApprovalStatus.APPROVED, decided_at=T0
                )
                assert decided is not None
                await other.commit()
            row = await uow.approvals.get_approved(run_id, "s6")
            assert row is not None and row.status is ApprovalStatus.APPROVED
            await uow.commit()


@pytest.mark.integration
@pytest.mark.usefixtures("_seeded")
class TestExecuteToolMintsFromTheRow:
    """`execute_tool`: resolved args → barrier 2 → gate → token →
    `ToolRegistry.dispatch` → the effect. The node never fabricates a token
    and never reaches the port around the dispatcher."""

    async def test_approved_row_to_gate_to_token_to_dispatch_to_mutation(
        self, handlers: NodeHandlers, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        s = await Sendable.create(uow_factory, adapters)
        approval_id = await s.approve(uow_factory)

        delta = await handlers.execute_tool(s.state())

        assert "errors" not in delta
        result = delta["tool_results"][s.step_id]
        assert result.tool is ToolName.SEND_EMAIL_MOCK
        assert result.output["to_email"] == DANA
        (call,) = delta["tool_calls"]
        assert call.status == "succeeded"
        assert call.args_hash == canonical_args_hash(s.args)

        (sent,) = await outbox_rows(_sf(uow_factory))
        assert sent["to_email"] == DANA
        assert sent["run_id"] == str(s.run_id)
        assert sent["approval_id"] == str(approval_id), (
            "the effect is attributed to the row the token was minted from"
        )
        assert await tool_call_statuses(uow_factory, s.step_uuid) == [ToolCallStatus.SUCCEEDED]

    async def test_no_approved_row_is_refused_before_any_attempt_is_made(
        self, handlers: NodeHandlers, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """Barrier 2 is satisfied by state; the durable prerequisite is not.
        Nothing reaches the dispatcher, so nothing is recorded or sent."""
        s = await Sendable.create(uow_factory, adapters)

        err = refusal(await handlers.execute_tool(s.state()))

        assert "human approval is required" in err.message
        assert await outbox_rows(_sf(uow_factory)) == []
        assert await tool_call_statuses(uow_factory, s.step_uuid) == []

    @pytest.mark.parametrize(
        "status",
        [ApprovalStatus.PENDING, ApprovalStatus.REJECTED, ApprovalStatus.CANCELLED],
    )
    async def test_a_row_that_is_not_approved_is_refused(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        status: ApprovalStatus,
    ) -> None:
        s = await Sendable.create(uow_factory, adapters)
        await s.approve(uow_factory, status=status)
        refusal(await handlers.execute_tool(s.state()))
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_an_expired_approval_is_refused_from_persisted_state(
        self,
        handlers: NodeHandlers,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        clock: FixedClock,
    ) -> None:
        """The row is `approved`; only the injected clock has moved past its
        `expires_at`. The gate reads both from the row and the clock."""
        s = await Sendable.create(uow_factory, adapters)
        await s.approve(uow_factory)

        clock.set(T0 + TTL)
        err = refusal(await handlers.execute_tool(s.state()))
        assert err.detail["mismatch"] == ["expired"]
        assert await outbox_rows(_sf(uow_factory)) == []

        # Inside the window it mints and sends — same row, same state.
        clock.set(T0 + TTL - timedelta(seconds=1))
        delta = await handlers.execute_tool(s.state(attempt=2))
        assert "errors" not in delta
        assert len(await outbox_rows(_sf(uow_factory))) == 1

    async def test_a_swept_expired_row_is_refused(
        self, handlers: NodeHandlers, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        s = await Sendable.create(uow_factory, adapters)
        await s.approve(uow_factory, status=ApprovalStatus.EXPIRED)
        refusal(await handlers.execute_tool(s.state()))
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_a_superseded_approval_is_refused(
        self, handlers: NodeHandlers, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """§9.3: the arguments changed and a fresh request replaced the old
        row through the repository's `supersede`. The old grant is dead."""
        s = await Sendable.create(uow_factory, adapters)
        old_id = await s.approve(uow_factory, status=ApprovalStatus.PENDING)
        async with uow_factory() as uow:
            new = await uow.approvals.create_request(
                run_id=s.run_id,
                step_id="s7",
                tool=ToolName.SEND_EMAIL_MOCK,
                risk=RiskLevel.HIGH,
                title="t",
                summary="s",
                payload_preview={},
                args_hash="other",
                requested_at=T0,
                expires_at=T0 + TTL,
            )
            assert await uow.approvals.supersede(old_id, new.id) is not None
            await uow.commit()
        refusal(await handlers.execute_tool(s.state()))
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_an_approved_row_chained_forward_is_refused(
        self, handlers: NodeHandlers, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """Persisted `superseded_by` on a row still marked `approved` — no
        repository method produces this today, which is exactly why the gate
        checks the column rather than trusting the status alone."""
        s = await Sendable.create(uow_factory, adapters)
        approval_id = await s.approve(uow_factory)
        successor = await script_decision(
            uow_factory, run_id=s.run_id, step_id="s7", args={**s.args, "draft_id": "d_x"}
        )
        async with _sf(uow_factory)() as session:
            await session.execute(
                sa.text("UPDATE opspilot.approvals SET superseded_by = :new WHERE id = :old"),
                {"new": successor, "old": approval_id},
            )
            await session.commit()
        err = refusal(await handlers.execute_tool(s.state()))
        assert err.detail["mismatch"] == ["superseded"]
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_changed_arguments_are_refused_against_the_row(
        self, handlers: NodeHandlers, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """The human approved a send to Dana. The state's grant claims the
        redirected arguments (barrier 2 satisfied, as a compromised state
        would); the durable row says otherwise, and the row wins."""
        s = await Sendable.create(uow_factory, adapters)
        await s.approve(uow_factory)
        redirected = {**s.args, "to_email": "ceo@acme.example"}

        err = refusal(
            await handlers.execute_tool(
                gated_state(s.run_id, args=redirected, granted_for=redirected)
            )
        )
        assert err.detail["mismatch"] == ["args_hash"]
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_an_idempotency_key_in_the_plan_is_a_planning_fault_not_a_hash_change(
        self, handlers: NodeHandlers, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """Volatile keys are excluded from the hash, so a plan carrying one
        still matches the row — and is then refused by the dispatcher's
        hygiene check as `INPUT_VALIDATION`, not accepted and not a policy
        violation. The gate does not decide that; the dispatcher does."""
        s = await Sendable.create(uow_factory, adapters)
        await s.approve(uow_factory)
        args = {**s.args, "idempotency_key": "planner-chosen"}
        assert canonical_args_hash(args) == canonical_args_hash(s.args)

        delta = await handlers.execute_tool(gated_state(s.run_id, args=args))
        (err,) = delta["errors"]
        assert err.error_class is ErrorClass.INPUT_VALIDATION
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_a_row_for_another_step_is_not_consumed(
        self, handlers: NodeHandlers, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        s = await Sendable.create(uow_factory, adapters)
        await script_decision(uow_factory, run_id=s.run_id, step_id="s9", args=s.args)
        refusal(await handlers.execute_tool(s.state()))
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_a_row_for_another_run_is_not_consumed(
        self, handlers: NodeHandlers, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        s = await Sendable.create(uow_factory, adapters)
        other = await Sendable.create(uow_factory, adapters)
        other.args = s.args
        await other.approve(uow_factory)
        refusal(await handlers.execute_tool(s.state()))
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_a_row_for_another_tool_on_the_same_step_is_refused(
        self, handlers: NodeHandlers, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        s = await Sendable.create(uow_factory, adapters)
        await s.approve(uow_factory, tool=ToolName.UPDATE_CUSTOMER)
        err = refusal(await handlers.execute_tool(s.state()))
        assert err.detail["mismatch"] == ["tool"]
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_an_ungated_tool_receives_no_token_and_needs_no_row(
        self, handlers: NodeHandlers, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        plan = Plan(
            plan_id="p_read",
            steps=[
                PlanStep(
                    step_id="s1",
                    tool=ToolName.SEARCH_LEADS,
                    args={"industry": "technology", "limit": 3},
                    status=StepStatus.PENDING,
                )
            ],
        )
        state = create_initial_state(run_id=run_id, user_request="find", plan=plan)
        state["current_step_id"] = "s1"
        state["status"] = RunStatus.RUNNING
        delta = await handlers.execute_tool(state)
        assert "errors" not in delta
        assert "s1" in delta["tool_results"]

    async def test_without_a_durable_store_a_gated_step_fails_closed(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """No `uow_factory` means no row to mint from: the node does not
        guess, does not fabricate, and does not dispatch."""
        s = await Sendable.create(uow_factory, adapters)
        await s.approve(uow_factory)
        lonely = NodeHandlers(registry=registry, clock=FixedClock(T0))
        err = refusal(await lonely.execute_tool(s.state()))
        assert "no durable approval store" in err.message
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_retrying_an_approved_step_replays_rather_than_resends(
        self, handlers: NodeHandlers, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """Each attempt re-reads the row and mints afresh; the key derived
        from `(run, step, args_hash)` makes the second application a replay
        (ADR-020) — one outbox row, two recorded attempts."""
        s = await Sendable.create(uow_factory, adapters)
        await s.approve(uow_factory)
        first = await handlers.execute_tool(s.state(attempt=1))
        second = await handlers.execute_tool(s.state(attempt=2))
        assert "errors" not in first and "errors" not in second
        assert len(await outbox_rows(_sf(uow_factory))) == 1
        assert await tool_call_statuses(uow_factory, s.step_uuid) == [
            ToolCallStatus.SUCCEEDED,
            ToolCallStatus.DUPLICATE_SUPPRESSED,
        ]


# ===========================================================================
# 3. Defense in depth: the dispatcher re-checks a gate-minted token
# ===========================================================================
async def mint_from_row(
    uow_factory: UnitOfWorkFactory,
    *,
    run_id: uuid.UUID,
    step_id: str,
    args: dict[str, Any],
    now: datetime = T0,
) -> ApprovalToken:
    """What `execute_tool` does, exposed so a token can be carried elsewhere."""
    row = await get_approved(uow_factory, run_id, step_id)
    return ApprovalGate.issue_from_persisted(
        row,
        run_id=str(run_id),
        step_id=step_id,
        tool=ToolName.SEND_EMAIL_MOCK.value,
        args=args,
        now=now,
    )


@pytest.mark.integration
@pytest.mark.usefixtures("_seeded")
class TestDispatcherRechecksAGateMintedToken:
    """TOCTOU and replay: a token that was valid when minted is re-verified
    against the call and against the row *at dispatch time*."""

    async def _dispatch(
        self, registry: ToolRegistry, s: Sendable, token: ApprovalToken, **over: Any
    ) -> Any:
        kwargs: dict[str, Any] = {
            "run_id": s.run_id,
            "execution_step_id": s.step_uuid,
            "step_id": s.step_id,
            "tool_name": ToolName.SEND_EMAIL_MOCK,
            "arguments": s.args,
            "attempt": 1,
            "approval_token": token,
        }
        kwargs.update(over)
        return await registry.dispatch(**kwargs)

    async def test_a_token_replayed_into_another_run_is_refused(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        s = await Sendable.create(uow_factory, adapters)
        victim = await Sendable.create(uow_factory, adapters)
        victim.args = s.args
        await s.approve(uow_factory)
        token = await mint_from_row(uow_factory, run_id=s.run_id, step_id="s6", args=s.args)
        with pytest.raises(ApprovalInvalidError) as exc:
            await self._dispatch(registry, victim, token)
        assert exc.value.detail["mismatch"] == ["run_id"]
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_a_token_replayed_into_another_step_is_refused(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        s = await Sendable.create(uow_factory, adapters)
        await s.approve(uow_factory)
        other_step = await create_step(uow_factory, s.run_id, "s9", ToolName.SEND_EMAIL_MOCK)
        token = await mint_from_row(uow_factory, run_id=s.run_id, step_id="s6", args=s.args)
        with pytest.raises(ApprovalInvalidError) as exc:
            await self._dispatch(registry, s, token, step_id="s9", execution_step_id=other_step)
        assert exc.value.detail["mismatch"] == ["step_id"]
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_a_token_replayed_into_another_tool_is_refused(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """The token carries no tool; the step row and the approval row do.
        The dispatcher refuses before the stored decision is even read."""
        s = await Sendable.create(uow_factory, adapters)
        await s.approve(uow_factory)
        token = await mint_from_row(uow_factory, run_id=s.run_id, step_id="s6", args=s.args)
        with pytest.raises(PolicyViolation, match="does not match the dispatch"):
            await self._dispatch(registry, s, token, tool_name=ToolName.UPDATE_CUSTOMER)
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_a_token_presented_with_different_arguments_is_refused(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        s = await Sendable.create(uow_factory, adapters)
        await s.approve(uow_factory)
        token = await mint_from_row(uow_factory, run_id=s.run_id, step_id="s6", args=s.args)
        with pytest.raises(ApprovalInvalidError) as exc:
            await self._dispatch(
                registry, s, token, arguments={**s.args, "to_email": "ceo@acme.example"}
            )
        assert exc.value.detail["mismatch"] == ["args_hash"]
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_an_approval_that_expires_between_minting_and_dispatch_is_refused(
        self,
        registry: ToolRegistry,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        clock: FixedClock,
    ) -> None:
        """The gate said yes at T0; by dispatch the TTL has elapsed. The
        dispatcher's stored-decision check reads the clock too."""
        s = await Sendable.create(uow_factory, adapters)
        await s.approve(uow_factory)
        token = await mint_from_row(uow_factory, run_id=s.run_id, step_id="s6", args=s.args)
        clock.set(T0 + TTL)
        with pytest.raises(ApprovalInvalidError) as exc:
            await self._dispatch(registry, s, token)
        assert exc.value.detail["mismatch"] == ["expired"]
        assert await outbox_rows(_sf(uow_factory)) == []
        assert await tool_call_statuses(uow_factory, s.step_uuid) == [ToolCallStatus.FAILED]

    async def test_an_approval_chained_forward_between_minting_and_dispatch_is_refused(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        s = await Sendable.create(uow_factory, adapters)
        approval_id = await s.approve(uow_factory)
        token = await mint_from_row(uow_factory, run_id=s.run_id, step_id="s6", args=s.args)
        successor = await script_decision(
            uow_factory, run_id=s.run_id, step_id="s7", args={**s.args, "draft_id": "d_x"}
        )
        async with _sf(uow_factory)() as session:
            await session.execute(
                sa.text("UPDATE opspilot.approvals SET superseded_by = :new WHERE id = :old"),
                {"new": successor, "old": approval_id},
            )
            await session.commit()
        with pytest.raises(ApprovalInvalidError) as exc:
            await self._dispatch(registry, s, token)
        assert exc.value.detail["mismatch"] == ["superseded"]
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_concurrent_dispatches_with_identical_valid_authorisation_send_once(
        self, handlers: NodeHandlers, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """Two workers (a double resume, a worker racing the reconciler) each
        read the row, each mint a valid token, each dispatch. The per-key
        advisory lock serialises them and the outbox constraint is the
        effect-level guard: one `succeeded`, one `duplicate_suppressed`, one
        email."""
        s = await Sendable.create(uow_factory, adapters)
        await s.approve(uow_factory)

        first, second = await asyncio.gather(
            handlers.execute_tool(s.state(attempt=1)),
            handlers.execute_tool(s.state(attempt=2)),
        )

        assert "errors" not in first and "errors" not in second
        assert len(await outbox_rows(_sf(uow_factory))) == 1
        statuses = await tool_call_statuses(uow_factory, s.step_uuid)
        assert sorted(statuses) == sorted(
            [ToolCallStatus.SUCCEEDED, ToolCallStatus.DUPLICATE_SUPPRESSED]
        )

    async def test_the_same_attempt_slot_cannot_be_taken_twice(
        self, handlers: NodeHandlers, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """Two drivers of the *same* attempt — which the leases should make
        impossible — still produce exactly one effect: the loser is refused
        as `DuplicateAttemptError`, recorded as internal, never executed."""
        s = await Sendable.create(uow_factory, adapters)
        await s.approve(uow_factory)
        left, right = await asyncio.gather(
            handlers.execute_tool(s.state(attempt=1)),
            handlers.execute_tool(s.state(attempt=1)),
        )
        outcomes = sorted("errors" in d for d in (left, right))
        assert outcomes == [False, True]
        assert len(await outbox_rows(_sf(uow_factory))) == 1
        loser = left if "errors" in left else right
        assert loser["errors"][0].error_class is ErrorClass.INTERNAL


# ===========================================================================
# 4. End to end: the agent graph pauses, is approved, and sends once
# ===========================================================================
@pytest.mark.integration
@pytest.mark.usefixtures("_seeded")
class TestAgentGraphEndToEnd:
    async def test_pause_approve_resume_sends_exactly_once_through_execute_tool(
        self,
        registry: ToolRegistry,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        clock: FixedClock,
    ) -> None:
        """§9.9 (1), (6): no `mock_crm` write while paused; after the human's
        stored approval the graph resumes through `request_approval` →
        `decide` → `execute_tool` → gate → dispatcher → one outbox row."""
        run_id = await create_run(uow_factory)
        draft_id = await save_draft(adapters)
        args = {"draft_id": draft_id, "to_email": DANA}
        plan = Plan(
            plan_id=f"p_{run_id.hex[:8]}",
            steps=[
                PlanStep(
                    step_id="s6",
                    tool=ToolName.SEND_EMAIL_MOCK,
                    args=args,
                    status=StepStatus.PENDING,
                )
            ],
        )
        handlers = NodeHandlers(registry=registry, uow_factory=uow_factory, clock=clock)

        async with open_checkpointer(get_settings()) as checkpointer:
            graph = create_agent_graph(checkpointer=checkpointer, node_handlers=handlers)
            cfg = thread_config(run_id)
            initial = create_initial_state(
                run_id=run_id, user_request="Send outreach email", plan=plan, clock=clock
            )

            # 1. Pause at the gate: nothing written, nothing sent.
            await graph.ainvoke(initial, config=cfg, durability=DURABILITY)
            snapshot = await graph.aget_state(cfg)
            (task,) = snapshot.tasks
            (pause,) = task.interrupts
            assert pause.value["step_id"] == "s6"
            assert pause.value["args_hash"] == canonical_args_hash(args)
            assert await outbox_rows(_sf(uow_factory)) == []

            # 2. The human decides — durably, through the repository, for the
            #    exact hash the interrupt showed.
            approval_id = await script_decision(uow_factory, run_id=run_id, step_id="s6", args=args)

            # 3. Resume: the token is minted from that row inside execute_tool.
            final = await graph.ainvoke(
                Command(resume="approve"), config=cfg, durability=DURABILITY
            )

        assert final["status"] is RunStatus.COMPLETED
        assert final["approval_state"].grants("s6", args)
        assert "s6" in final["tool_results"]
        assert [c.status for c in final["tool_calls"]] == ["succeeded"]
        (sent,) = await outbox_rows(_sf(uow_factory))
        assert sent["to_email"] == DANA
        assert sent["approval_id"] == str(approval_id)
        assert sent["run_id"] == str(run_id)

    async def test_resume_with_no_stored_decision_fails_closed(
        self,
        registry: ToolRegistry,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        clock: FixedClock,
    ) -> None:
        """An "approve" resume value alone is state, not proof: with no
        durable approved row the step is refused and nothing is sent."""
        run_id = await create_run(uow_factory)
        draft_id = await save_draft(adapters)
        plan = Plan(
            plan_id=f"p_{run_id.hex[:8]}",
            steps=[
                PlanStep(
                    step_id="s6",
                    tool=ToolName.SEND_EMAIL_MOCK,
                    args={"draft_id": draft_id, "to_email": DANA},
                    status=StepStatus.PENDING,
                )
            ],
        )
        handlers = NodeHandlers(registry=registry, uow_factory=uow_factory, clock=clock)
        async with open_checkpointer(get_settings()) as checkpointer:
            graph = create_agent_graph(checkpointer=checkpointer, node_handlers=handlers)
            cfg = thread_config(run_id)
            initial = create_initial_state(
                run_id=run_id, user_request="Send outreach email", plan=plan, clock=clock
            )
            await graph.ainvoke(initial, config=cfg, durability=DURABILITY)
            final = await graph.ainvoke(
                Command(resume="approve"), config=cfg, durability=DURABILITY
            )

        assert final["status"] is RunStatus.FAILED
        assert final["errors"][-1].error_class is ErrorClass.POLICY_VIOLATION
        assert await outbox_rows(_sf(uow_factory)) == []
