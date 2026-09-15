"""`ToolRegistry.dispatch` — the single choke point — against real Postgres
and the real mock adapters (§8.5, §9.5, §10.4, §14.4, TOOL-002).

The tool implementations here are scripted doubles (`ScriptedTool`, §18.2):
TOOL-003 lands the nine production implementations. What is *not* doubled is
everything the safety claims rest on — the `tool_calls` and `trace_events`
repositories, the `approvals` row, the outbox unique constraint, the
advisory lock — because a fake would not have the constraints that are the
mechanism.

Every test that touches a gated tool scripts the *human* (a stored decision)
and never the *gate*.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from app.agent.state import ApprovalStatus, RunStatus
from app.config import get_settings
from app.errors import (
    ErrorClass,
    InputValidationError,
    InternalError,
    NotFoundError,
    OutputValidationError,
    PolicyViolation,
    TransientToolError,
)
from app.integrations.mock import build_mock_adapters, seed_database
from app.integrations.ports import (
    Adapters,
    DraftInput,
    LeadFilter,
    LeadPort,
    MailPort,
    OutboundMessage,
)
from app.observability.redaction import REDACTED
from app.persistence.models import ToolCallRow, ToolCallStatus, TraceEvent, TraceEventKind
from app.persistence.protocols import UnitOfWorkFactory
from app.persistence.session import create_session_factory, unit_of_work
from app.runtime import FixedClock, SequentialIdGenerator
from app.security import ApprovalGate, ApprovalToken, canonical_args_hash, idempotency_key_for
from app.tools.contracts import REGISTRY, RiskLevel, ToolName
from app.tools.registry import (
    ApprovalInvalidError,
    ApprovalRequiredError,
    DispatchOutcome,
    DispatchResult,
    DuplicateAttemptError,
    ToolContext,
    ToolNotBoundError,
    ToolRegistry,
    ToolTimeoutError,
    UnknownToolError,
)
from app.tools.schemas import (
    GetLeadInput,
    GetLeadOutput,
    ScoreBand,
    ScoreLeadInput,
    ScoreLeadOutput,
    SearchLeadsInput,
    SearchLeadsOutput,
    SendEmailMockInput,
    SendEmailMockOutput,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from tests.recovery_harness import T0

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DANA = "dana@northwind.example"  # lead L-104's stored address (fixtures)


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------
def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _require_database() -> None:
    try:
        with sa.create_engine(
            _sync_url(get_settings().database_url.get_secret_value()),
            connect_args={"connect_timeout": 3},
        ).connect():
            pass
    except OperationalError:
        pytest.skip("no reachable Postgres for this session (TOOL-002 integration test)")


@pytest.fixture(scope="module", autouse=True)
def _migrated_database() -> None:
    _require_database()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url.get_secret_value())
    command.upgrade(config, "head")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    # The concurrency tests hold one connection per waiting dispatcher plus
    # one for the executing adapter (module docstring of app.tools.registry).
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


@pytest.fixture(autouse=True)
async def _seeded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await seed_database(session_factory, reset=True)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def adapters(session_factory: async_sessionmaker[AsyncSession], clock: FixedClock) -> Adapters:
    return build_mock_adapters(session_factory, clock, SequentialIdGenerator())


# ---------------------------------------------------------------------------
# Scripted tool implementations (§18.2) — real ports, scripted glue
# ---------------------------------------------------------------------------
async def search_leads(args: SearchLeadsInput, ctx: ToolContext) -> SearchLeadsOutput:
    assert isinstance(ctx.port, LeadPort)
    page = await ctx.port.search(LeadFilter(**args.model_dump()))
    return SearchLeadsOutput(**page.model_dump())


async def send_email_mock(args: SendEmailMockInput, ctx: ToolContext) -> SendEmailMockOutput:
    assert isinstance(ctx.port, MailPort)
    receipt = await ctx.port.send(
        OutboundMessage(draft_id=args.draft_id, to_email=args.to_email),
        token=args.approval_token,
        idempotency_key=args.idempotency_key,
    )
    return SendEmailMockOutput(**receipt.model_dump())


async def score_lead(args: ScoreLeadInput, ctx: ToolContext) -> ScoreLeadOutput:
    assert ctx.port is None  # a pure tool sees no port at all
    return ScoreLeadOutput(
        lead_id=args.lead_id,
        score=72,
        band=ScoreBand.WARM,
        factors=[],
        rationale="scripted",
        model_version="test",
    )


def failing_with(exc: Exception) -> Callable[[Any, ToolContext], Any]:
    async def impl(args: Any, ctx: ToolContext) -> Any:
        raise exc

    return impl


async def lying(args: GetLeadInput, ctx: ToolContext) -> Any:
    return ScoreLeadOutput(  # the wrong model, and internally inconsistent for get_lead
        lead_id=args.lead_id,
        score=1,
        band=ScoreBand.COLD,
        factors=[],
        rationale="",
        model_version="x",
    )


async def hanging(args: Any, ctx: ToolContext) -> Any:
    await asyncio.Event().wait()


async def get_lead(args: GetLeadInput, ctx: ToolContext) -> GetLeadOutput:
    assert isinstance(ctx.port, LeadPort)
    return GetLeadOutput(lead=await ctx.port.get(args.lead_id))


def make_registry(
    adapters: Adapters,
    uow_factory: UnitOfWorkFactory,
    clock: FixedClock,
    implementations: dict[ToolName, Any] | None = None,
    **kwargs: Any,
) -> ToolRegistry:
    impls = {
        ToolName.SEARCH_LEADS: search_leads,
        ToolName.SEND_EMAIL_MOCK: send_email_mock,
        ToolName.SCORE_LEAD: score_lead,
        ToolName.GET_LEAD: get_lead,
    }
    if implementations:
        impls.update(implementations)
    return ToolRegistry(
        adapters=adapters, uow_factory=uow_factory, clock=clock, implementations=impls, **kwargs
    )


@pytest.fixture
def registry(adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock) -> ToolRegistry:
    return make_registry(adapters, uow_factory, clock)


# ---------------------------------------------------------------------------
# Control-plane rows and readbacks
# ---------------------------------------------------------------------------
async def create_run(uow_factory: UnitOfWorkFactory) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with uow_factory() as uow:
        await uow.agent_runs.create(
            id=run_id,
            user_request="dispatch me",
            deadline_at=T0 + timedelta(minutes=5),
            status=RunStatus.RUNNING,
        )
        await uow.commit()
    return run_id


async def create_step(
    uow_factory: UnitOfWorkFactory, run_id: uuid.UUID, step_id: str, tool: ToolName, seq: int = 1
) -> uuid.UUID:
    async with uow_factory() as uow:
        step = await uow.execution_steps.create(
            run_id=run_id, step_id=step_id, plan_revision=0, seq=seq, tool=tool
        )
        await uow.commit()
        return step.id


async def create_approval(
    uow_factory: UnitOfWorkFactory,
    *,
    run_id: uuid.UUID,
    step_id: str,
    args: dict[str, Any],
    tool: ToolName = ToolName.SEND_EMAIL_MOCK,
    risk: RiskLevel = RiskLevel.HIGH,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
) -> uuid.UUID:
    """Script the human: a stored decision, made through the real repository."""
    async with uow_factory() as uow:
        row = await uow.approvals.create_request(
            run_id=run_id,
            step_id=step_id,
            tool=tool,
            risk=risk,
            title="Send outreach email",
            summary="scripted",
            payload_preview={},
            args_hash=canonical_args_hash(args),
            requested_at=T0,
            expires_at=T0 + timedelta(minutes=30),
        )
        if status is not ApprovalStatus.PENDING:
            decided = await uow.approvals.decide(
                row.id, status=status, decided_by="operator", decided_at=T0
            )
            assert decided is not None
        await uow.commit()
        return row.id


def mint(
    approval_id: uuid.UUID, run_id: uuid.UUID, step_id: str, args: dict[str, Any]
) -> ApprovalToken:
    return ApprovalGate.issue(
        approval_id=str(approval_id),
        run_id=str(run_id),
        step_id=step_id,
        args=args,
        approved_args_hash=canonical_args_hash(args),
        decision="approve",
    )


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


async def rows_for(uow_factory: UnitOfWorkFactory, step_uuid: uuid.UUID) -> list[ToolCallRow]:
    # Commit the read-only transaction: exiting uncommitted rolls back, which
    # expires every loaded attribute on the detached rows (DB-005).
    async with uow_factory() as uow:
        rows = await uow.tool_calls.list_by_step(step_uuid)
        await uow.commit()
        return rows


async def events_for(uow_factory: UnitOfWorkFactory, run_id: uuid.UUID) -> list[TraceEvent]:
    async with uow_factory() as uow:
        events = await uow.trace_events.list_by_run(run_id, limit=1000)
        await uow.commit()
        return events


async def outbox_rows(session_factory: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    async with session_factory() as session:
        res = await session.execute(
            sa.text("SELECT idempotency_key, message_id, to_email FROM mock_crm.email_outbox")
        )
        return [dict(r._mapping) for r in res]


def kinds(events: list[TraceEvent], attempt: int | None = None) -> list[str]:
    return [e.kind.value for e in events if attempt is None or e.attempt == attempt]


class Gated:
    """A run with a planned `send_email_mock` step and a saved draft."""

    def __init__(self, run_id: uuid.UUID, step_uuid: uuid.UUID, draft_id: str) -> None:
        self.run_id = run_id
        self.step_id = "s6"
        self.step_uuid = step_uuid
        self.draft_id = draft_id
        self.args = {"draft_id": draft_id, "to_email": DANA}

    @classmethod
    async def create(cls, uow_factory: UnitOfWorkFactory, adapters: Adapters) -> Gated:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s6", ToolName.SEND_EMAIL_MOCK)
        return cls(run_id, step_uuid, await save_draft(adapters))

    async def approve(
        self, uow_factory: UnitOfWorkFactory, status: ApprovalStatus = ApprovalStatus.APPROVED
    ) -> ApprovalToken:
        approval_id = await create_approval(
            uow_factory, run_id=self.run_id, step_id=self.step_id, args=self.args, status=status
        )
        return mint(approval_id, self.run_id, self.step_id, self.args)

    def dispatch(
        self,
        registry: ToolRegistry,
        *,
        attempt: int = 1,
        token: ApprovalToken | None = None,
        **over: Any,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "run_id": self.run_id,
            "execution_step_id": self.step_uuid,
            "step_id": self.step_id,
            "tool_name": ToolName.SEND_EMAIL_MOCK,
            "arguments": self.args,
            "attempt": attempt,
            "approval_token": token,
        }
        kwargs.update(over)
        return registry.dispatch(**kwargs)


# ===========================================================================
# 1. Read tools
# ===========================================================================
class TestReadDispatch:
    async def test_a_registered_read_tool_dispatches_without_approval(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s1", ToolName.SEARCH_LEADS)

        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s1",
            tool_name="search_leads",
            arguments={"industry": "technology", "limit": 5},
            attempt=1,
        )

        assert isinstance(result, DispatchResult)
        assert result.outcome is DispatchOutcome.SUCCEEDED
        assert isinstance(result.output, SearchLeadsOutput)
        assert result.output.leads and all(lead.company_name for lead in result.output.leads)
        assert result.output_data["total_matched"] == result.output.total_matched
        assert result.idempotency_key is None  # reads are not keyed effects
        assert result.port == "LeadPort" and result.adapter == "mock"
        assert result.args_hash == canonical_args_hash({"industry": "technology", "limit": 5})

        (row,) = await rows_for(uow_factory, step_uuid)
        assert row.status is ToolCallStatus.SUCCEEDED
        assert row.id == result.tool_call_id
        assert row.attempt == 1 and row.tool == "search_leads" and row.tool_version == "1.0.0"
        assert row.input == {"industry": "technology", "limit": 5, **_search_defaults()}
        assert row.input_hash == result.args_hash
        assert row.output is not None and row.output["total_matched"] == result.output.total_matched
        assert row.error_class is None and row.error_message is None
        assert row.idempotency_key is None
        assert row.port == "LeadPort" and row.adapter == "mock"
        assert row.started_at == T0 and row.finished_at == T0 and row.duration_ms == 0

        events = await events_for(uow_factory, run_id)
        assert kinds(events) == ["tool_started", "tool_succeeded"]
        assert all(e.node == "execute_tool" and e.tool == "search_leads" for e in events)
        assert events[1].payload["tool_call_id"] == str(row.id)
        assert events[1].status == "succeeded" and events[1].retry_count == 0

    async def test_a_pure_tool_sees_no_port(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s4", ToolName.SCORE_LEAD)
        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s4",
            tool_name=ToolName.SCORE_LEAD,
            arguments={"lead_id": "L-104", "company": _company()},
            attempt=1,
        )
        assert result.outcome is DispatchOutcome.SUCCEEDED
        assert result.port is None and result.adapter is None
        (row,) = await rows_for(uow_factory, step_uuid)
        assert row.port is None and row.adapter is None

    async def test_duration_is_measured_on_the_injected_clock(
        self, adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock
    ) -> None:
        async def slow(args: GetLeadInput, ctx: ToolContext) -> GetLeadOutput:
            clock.advance(ms=250)
            assert isinstance(ctx.port, LeadPort)
            return GetLeadOutput(lead=await ctx.port.get(args.lead_id))

        registry = make_registry(adapters, uow_factory, clock, {ToolName.GET_LEAD: slow})
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s2", ToolName.GET_LEAD)
        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s2",
            tool_name=ToolName.GET_LEAD,
            arguments={"lead_id": "L-104"},
            attempt=1,
        )
        assert result.duration_ms == 250
        assert result.started_at == T0 and result.finished_at == T0 + timedelta(milliseconds=250)


def _search_defaults() -> dict[str, Any]:
    return {
        "location": None,
        "min_employees": None,
        "max_employees": None,
        "status": None,
        "query": None,
        "offset": 0,
    }


def _company() -> dict[str, Any]:
    return {
        "company_id": "C-1",
        "name": "Northwind",
        "domain": "northwind.example",
        "summary": "A company.",
        "confidence": 0.9,
        "retrieved_at": T0.isoformat(),
    }


# ===========================================================================
# 2. Fail-closed rejections before the port
# ===========================================================================
class TestRejections:
    async def test_unknown_tool_fails_closed_without_touching_the_database(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s1", ToolName.SEARCH_LEADS)
        with pytest.raises(UnknownToolError):
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s1",
                tool_name="wipe_crm",
                arguments={},
                attempt=1,
            )
        assert await rows_for(uow_factory, step_uuid) == []
        assert await events_for(uow_factory, run_id) == []

    @pytest.mark.parametrize(
        ("arguments", "loc"),
        [
            ({"industry": "technology", "unexpected": 1}, "unexpected"),  # extra="forbid"
            ({}, "at least one filter"),  # semantic rule
            ({"industry": "technology", "limit": "ten"}, "limit"),  # wrong type
            ({"industry": "technology", "limit": 500}, "limit"),  # out of range
            ({"min_employees": 50, "max_employees": 10}, "max_employees"),  # contradictory
        ],
    )
    async def test_malformed_input_is_rejected_and_recorded(
        self,
        registry: ToolRegistry,
        uow_factory: UnitOfWorkFactory,
        arguments: dict[str, Any],
        loc: str,
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s1", ToolName.SEARCH_LEADS)
        with pytest.raises(InputValidationError) as exc:
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s1",
                tool_name=ToolName.SEARCH_LEADS,
                arguments=arguments,
                attempt=1,
            )
        assert loc in json.dumps(exc.value.detail)
        assert exc.value.detail["dispatch"]["outcome"] == "rejected"

        (row,) = await rows_for(uow_factory, step_uuid)
        assert row.status is ToolCallStatus.FAILED
        assert row.error_class == ErrorClass.INPUT_VALIDATION.value
        assert row.adapter is None, "the port was never reached"
        assert row.output is None
        assert str(row.id) == exc.value.detail["dispatch"]["tool_call_id"]
        events = await events_for(uow_factory, run_id)
        assert kinds(events) == ["tool_started", "tool_failed"]
        assert events[1].status == "rejected"
        assert events[1].error is not None and events[1].error["class"] == "input_validation"

    async def test_missing_required_field_is_rejected(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s2", ToolName.GET_LEAD)
        with pytest.raises(InputValidationError) as exc:
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s2",
                tool_name=ToolName.GET_LEAD,
                arguments={},
                attempt=1,
            )
        assert exc.value.detail["errors"][0]["loc"] == ["lead_id"]

    async def test_a_plan_may_not_supply_the_idempotency_key(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        g = await Gated.create(uow_factory, adapters)
        token = await g.approve(uow_factory)
        with pytest.raises(InputValidationError, match="derives it"):
            await g.dispatch(
                registry, token=token, arguments={**g.args, "idempotency_key": "chosen-by-plan"}
            )
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_a_plan_may_not_supply_an_approval_token(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        g = await Gated.create(uow_factory, adapters)
        with pytest.raises(PolicyViolation, match="never planned"):
            await g.dispatch(registry, arguments={**g.args, "approval_token": "sk-ant-forged"})
        (row,) = await rows_for(uow_factory, g.step_uuid)
        assert row.error_class == "policy_violation"
        assert row.input["approval_token"] == REDACTED  # never persisted in clear
        events = await events_for(uow_factory, g.run_id)
        assert kinds(events) == ["tool_started", "tool_failed", "policy_violation"]
        assert events[2].severity.value == "error"

    async def test_a_token_presented_for_an_ungated_tool_is_refused(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        g = await Gated.create(uow_factory, adapters)
        token = await g.approve(uow_factory)
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s1", ToolName.SEARCH_LEADS)
        with pytest.raises(PolicyViolation, match="not gated"):
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s1",
                tool_name=ToolName.SEARCH_LEADS,
                arguments={"industry": "technology"},
                attempt=1,
                approval_token=token,
            )

    async def test_a_contract_without_an_implementation_fails_closed(
        self, adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock
    ) -> None:
        registry = ToolRegistry(
            adapters=adapters, uow_factory=uow_factory, clock=clock, implementations={}
        )
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s1", ToolName.SEARCH_LEADS)
        with pytest.raises(ToolNotBoundError) as exc:
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s1",
                tool_name=ToolName.SEARCH_LEADS,
                arguments={"industry": "technology"},
                attempt=1,
            )
        assert exc.value.error_class is ErrorClass.INTERNAL
        (row,) = await rows_for(uow_factory, step_uuid)
        assert row.status is ToolCallStatus.FAILED and row.error_class == "internal"

    async def test_an_execution_step_of_another_tool_cannot_host_the_attempt(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s1", ToolName.GET_LEAD)
        with pytest.raises(PolicyViolation, match="does not match"):
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s1",
                tool_name=ToolName.SEARCH_LEADS,
                arguments={"industry": "technology"},
                attempt=1,
            )
        assert await rows_for(uow_factory, step_uuid) == []
        assert kinds(await events_for(uow_factory, run_id)) == ["policy_violation"]

    async def test_an_execution_step_of_another_run_cannot_host_the_attempt(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_a = await create_run(uow_factory)
        run_b = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_a, "s1", ToolName.SEARCH_LEADS)
        with pytest.raises(PolicyViolation, match="does not match"):
            await registry.dispatch(
                run_id=run_b,
                execution_step_id=step_uuid,
                step_id="s1",
                tool_name=ToolName.SEARCH_LEADS,
                arguments={"industry": "technology"},
                attempt=1,
            )

    async def test_an_unknown_execution_step_is_an_internal_error(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        with pytest.raises(InternalError, match="does not exist"):
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=uuid.uuid4(),
                step_id="s1",
                tool_name=ToolName.SEARCH_LEADS,
                arguments={"industry": "technology"},
                attempt=1,
            )


def _sf(uow_factory: UnitOfWorkFactory) -> async_sessionmaker[AsyncSession]:
    return uow_factory.args[0]  # type: ignore[attr-defined]


# ===========================================================================
# 3. The approval gate — barrier 2 and the stored decision
# ===========================================================================
class TestApprovalGate:
    async def test_a_mutation_without_a_grant_raises_policy_violation_and_writes_nothing(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """TOOL-002's acceptance criterion, and §9.9 (2)."""
        g = await Gated.create(uow_factory, adapters)
        with pytest.raises(ApprovalRequiredError) as exc:
            await g.dispatch(registry)
        assert isinstance(exc.value, PolicyViolation)
        assert exc.value.error_class is ErrorClass.POLICY_VIOLATION

        assert await outbox_rows(_sf(uow_factory)) == []
        (row,) = await rows_for(uow_factory, g.step_uuid)
        assert row.status is ToolCallStatus.FAILED
        assert row.error_class == "policy_violation" and row.adapter is None
        assert row.idempotency_key == idempotency_key_for(
            run_id=str(g.run_id), step_id="s6", args_hash=canonical_args_hash(g.args)
        )
        events = await events_for(uow_factory, g.run_id)
        assert kinds(events) == ["tool_started", "tool_failed", "policy_violation"]
        assert events[1].status == "rejected"

    async def test_a_valid_grant_permits_exactly_the_approved_mutation(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        g = await Gated.create(uow_factory, adapters)
        token = await g.approve(uow_factory)

        result = await g.dispatch(registry, token=token)

        assert result.outcome is DispatchOutcome.SUCCEEDED
        assert isinstance(result.output, SendEmailMockOutput)
        assert result.output.to_email == DANA and result.output.draft_id == g.draft_id
        outbox = await outbox_rows(_sf(uow_factory))
        assert len(outbox) == 1
        assert outbox[0]["idempotency_key"] == result.idempotency_key
        assert outbox[0]["message_id"] == result.output.message_id

        (row,) = await rows_for(uow_factory, g.step_uuid)
        assert row.status is ToolCallStatus.SUCCEEDED
        assert row.idempotency_key == result.idempotency_key
        assert row.port == "MailPort" and row.adapter == "mock"
        assert row.input == {
            "draft_id": g.draft_id,
            "to_email": DANA,
            "idempotency_key": result.idempotency_key,
        }
        assert "approval_token" not in row.input
        events = await events_for(uow_factory, g.run_id)
        assert kinds(events) == ["tool_started", "tool_succeeded"]
        assert events[0].payload["approval_id"] == token.approval_id

    async def test_a_grant_for_another_run_is_rejected(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        g = await Gated.create(uow_factory, adapters)
        other = await Gated.create(uow_factory, adapters)
        other.args = g.args  # same effect, approved in a different run
        token = await other.approve(uow_factory)
        with pytest.raises(ApprovalInvalidError) as exc:
            await g.dispatch(registry, token=token)
        assert exc.value.detail["mismatch"] == ["run_id"]
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_a_grant_for_another_step_is_rejected(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        g = await Gated.create(uow_factory, adapters)
        approval_id = await create_approval(uow_factory, run_id=g.run_id, step_id="s9", args=g.args)
        token = mint(approval_id, g.run_id, "s9", g.args)
        with pytest.raises(ApprovalInvalidError) as exc:
            await g.dispatch(registry, token=token)
        assert exc.value.detail["mismatch"] == ["step_id"]
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_a_grant_for_another_tool_is_rejected(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """The token binds run, step and hash; the stored row binds the tool.
        An approval recorded for `update_customer` cannot authorise a send
        even if a step id and argument hash happen to coincide."""
        g = await Gated.create(uow_factory, adapters)
        approval_id = await create_approval(
            uow_factory, run_id=g.run_id, step_id="s6", args=g.args, tool=ToolName.UPDATE_CUSTOMER
        )
        token = mint(approval_id, g.run_id, "s6", g.args)
        with pytest.raises(ApprovalInvalidError) as exc:
            await g.dispatch(registry, token=token)
        assert exc.value.detail["mismatch"] == ["tool"]
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_a_grant_for_a_different_risk_context_is_rejected(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        g = await Gated.create(uow_factory, adapters)
        approval_id = await create_approval(
            uow_factory, run_id=g.run_id, step_id="s6", args=g.args, risk=RiskLevel.LOW
        )
        token = mint(approval_id, g.run_id, "s6", g.args)
        with pytest.raises(ApprovalInvalidError) as exc:
            await g.dispatch(registry, token=token)
        assert exc.value.detail["mismatch"] == ["risk"]

    async def test_modified_arguments_are_rejected_by_the_token_itself(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """§9.9 (5): a grant for hash A does not authorise arguments hashing to B."""
        g = await Gated.create(uow_factory, adapters)
        token = await g.approve(uow_factory)
        redirected = {**g.args, "to_email": "ceo@acme.example"}
        with pytest.raises(ApprovalInvalidError) as exc:
            await g.dispatch(registry, token=token, arguments=redirected)
        assert exc.value.detail["mismatch"] == ["args_hash"]
        assert await outbox_rows(_sf(uow_factory)) == []
        (row,) = await rows_for(uow_factory, g.step_uuid)
        assert row.input_hash == canonical_args_hash(redirected)

    async def test_modified_arguments_with_a_forged_matching_token_are_rejected_by_the_row(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """A token whose hash matches the *new* arguments but whose stored
        approval was for the *old* ones: the row check catches it."""
        g = await Gated.create(uow_factory, adapters)
        approval_id = await create_approval(uow_factory, run_id=g.run_id, step_id="s6", args=g.args)
        redirected = {**g.args, "to_email": "ceo@acme.example"}
        token = mint(approval_id, g.run_id, "s6", redirected)
        with pytest.raises(ApprovalInvalidError) as exc:
            await g.dispatch(registry, token=token, arguments=redirected)
        assert exc.value.detail["mismatch"] == ["args_hash"]
        assert await outbox_rows(_sf(uow_factory)) == []

    @pytest.mark.parametrize(
        "status",
        [
            ApprovalStatus.PENDING,
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.CANCELLED,
        ],
    )
    async def test_a_token_whose_stored_decision_is_not_approved_is_stale(
        self,
        registry: ToolRegistry,
        uow_factory: UnitOfWorkFactory,
        adapters: Adapters,
        status: ApprovalStatus,
    ) -> None:
        g = await Gated.create(uow_factory, adapters)
        token = await g.approve(uow_factory, status=status)
        with pytest.raises(ApprovalInvalidError) as exc:
            await g.dispatch(registry, token=token)
        assert exc.value.detail["mismatch"] == [f"status={status.value}"]
        assert await outbox_rows(_sf(uow_factory)) == []
        (row,) = await rows_for(uow_factory, g.step_uuid)
        assert row.error_class == "policy_violation" and row.adapter is None

    async def test_a_superseded_approval_no_longer_authorises(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """§9.3: a replan changed the arguments; the old grant is dead."""
        g = await Gated.create(uow_factory, adapters)
        old_id = await create_approval(
            uow_factory, run_id=g.run_id, step_id="s6", args=g.args, status=ApprovalStatus.PENDING
        )
        token = mint(old_id, g.run_id, "s6", g.args)
        async with uow_factory() as uow:
            new = await uow.approvals.create_request(
                run_id=g.run_id,
                step_id="s7",
                tool=ToolName.SEND_EMAIL_MOCK,
                risk=RiskLevel.HIGH,
                title="t",
                summary="s",
                payload_preview={},
                args_hash="other",
                requested_at=T0,
                expires_at=T0 + timedelta(minutes=30),
            )
            assert await uow.approvals.supersede(old_id, new.id) is not None
            await uow.commit()
        with pytest.raises(ApprovalInvalidError) as exc:
            await g.dispatch(registry, token=token)
        # Both the status and the `superseded_by` chain are named (HITL-002
        # added the column check so an approved row chained forward is dead
        # even before its status moves).
        assert exc.value.detail["mismatch"] == ["status=superseded", "superseded"]

    async def test_a_token_naming_no_stored_approval_is_rejected(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        g = await Gated.create(uow_factory, adapters)
        phantom = mint(uuid.uuid4(), g.run_id, "s6", g.args)
        with pytest.raises(ApprovalInvalidError, match="no stored approval"):
            await g.dispatch(registry, token=phantom)
        malformed = ApprovalGate.issue(
            approval_id="not-a-uuid",
            run_id=str(g.run_id),
            step_id="s6",
            args=g.args,
            approved_args_hash=canonical_args_hash(g.args),
            decision="approve",
        )
        with pytest.raises(ApprovalInvalidError, match="malformed"):
            await g.dispatch(registry, token=malformed, attempt=2)
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_reusing_the_grant_for_a_retry_replays_rather_than_resends(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """A retry after an approved attempt reuses the same token and the same
        key (§10.4); the effect is applied once and the retry is recorded as
        `duplicate_suppressed` (ADR-020)."""
        g = await Gated.create(uow_factory, adapters)
        token = await g.approve(uow_factory)
        first = await g.dispatch(registry, token=token, attempt=1)
        second = await g.dispatch(registry, token=token, attempt=2)

        assert first.outcome is DispatchOutcome.SUCCEEDED
        assert second.outcome is DispatchOutcome.DUPLICATE_SUPPRESSED
        assert second.output.message_id == first.output.message_id  # type: ignore[attr-defined]
        assert second.idempotency_key == first.idempotency_key
        assert len(await outbox_rows(_sf(uow_factory))) == 1

        rows = await rows_for(uow_factory, g.step_uuid)
        assert [r.status for r in rows] == [
            ToolCallStatus.SUCCEEDED,
            ToolCallStatus.DUPLICATE_SUPPRESSED,
        ]
        events = await events_for(uow_factory, g.run_id)
        assert kinds(events, attempt=2) == ["tool_started", "tool_duplicate_suppressed"]

    async def test_the_grant_survives_a_replanned_step_with_identical_arguments(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """A new plan revision re-creates the step row; the key is derived from
        (run, step, args), not the row, so the effect is still applied once."""
        g = await Gated.create(uow_factory, adapters)
        token = await g.approve(uow_factory)
        await g.dispatch(registry, token=token)
        async with uow_factory() as uow:
            revised = await uow.execution_steps.create(
                run_id=g.run_id, step_id="s6", plan_revision=1, seq=1, tool=ToolName.SEND_EMAIL_MOCK
            )
            await uow.commit()
        replay = await g.dispatch(registry, token=token, execution_step_id=revised.id)
        assert replay.outcome is DispatchOutcome.DUPLICATE_SUPPRESSED
        assert len(await outbox_rows(_sf(uow_factory))) == 1


# ===========================================================================
# 4. Execution failures — recorded accurately, classified faithfully
# ===========================================================================
class TestExecutionFailures:
    async def _dispatch_get_lead(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> tuple[uuid.UUID, uuid.UUID, Any]:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s2", ToolName.GET_LEAD)
        coro = registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s2",
            tool_name=ToolName.GET_LEAD,
            arguments={"lead_id": "L-104"},
            attempt=1,
        )
        return run_id, step_uuid, coro

    async def test_a_transient_failure_is_recorded_as_failed_with_its_class(
        self, adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock
    ) -> None:
        registry = make_registry(
            adapters,
            uow_factory,
            clock,
            {ToolName.GET_LEAD: failing_with(TransientToolError("store unavailable"))},
        )
        run_id, step_uuid, coro = await self._dispatch_get_lead(registry, uow_factory)
        with pytest.raises(TransientToolError) as exc:
            await coro
        assert exc.value.detail["dispatch"]["outcome"] == "failed"
        (row,) = await rows_for(uow_factory, step_uuid)
        assert row.status is ToolCallStatus.FAILED
        assert row.error_class == "transient" and row.error_message == "store unavailable"
        assert row.adapter == "mock", "the port was reached; the failure is the integration's"
        assert str(row.id) == exc.value.detail["dispatch"]["tool_call_id"]
        events = await events_for(uow_factory, run_id)
        assert kinds(events) == ["tool_started", "tool_failed"]
        assert events[1].status == "failed"
        assert events[1].error == {
            "class": "transient",
            "message": "store unavailable",
            "detail": {},
        }

    async def test_a_not_found_from_the_real_adapter_is_classified_not_swallowed(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s2", ToolName.GET_LEAD)
        with pytest.raises(NotFoundError):
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s2",
                tool_name=ToolName.GET_LEAD,
                arguments={"lead_id": "L-999"},
                attempt=1,
            )
        (row,) = await rows_for(uow_factory, step_uuid)
        assert row.error_class == "not_found"

    async def test_a_timeout_is_recorded_as_timeout_and_classified_transient(
        self, adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock
    ) -> None:
        quick = REGISTRY[ToolName.GET_LEAD].model_copy(update={"timeout_ms": 100})
        registry = make_registry(
            adapters,
            uow_factory,
            clock,
            {ToolName.GET_LEAD: hanging},
            contracts={**REGISTRY, ToolName.GET_LEAD: quick},
        )
        run_id, step_uuid, coro = await self._dispatch_get_lead(registry, uow_factory)
        with pytest.raises(ToolTimeoutError) as exc:
            await coro
        assert exc.value.error_class is ErrorClass.TRANSIENT
        assert exc.value.detail["dispatch"]["outcome"] == "timeout"
        (row,) = await rows_for(uow_factory, step_uuid)
        assert row.status is ToolCallStatus.TIMEOUT and row.error_class == "transient"
        assert kinds(await events_for(uow_factory, run_id)) == ["tool_started", "tool_timeout"]

    async def test_malformed_output_is_rejected_by_the_contract(
        self, adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock
    ) -> None:
        registry = make_registry(adapters, uow_factory, clock, {ToolName.GET_LEAD: lying})
        run_id, step_uuid, coro = await self._dispatch_get_lead(registry, uow_factory)
        with pytest.raises(OutputValidationError):
            await coro
        (row,) = await rows_for(uow_factory, step_uuid)
        assert row.status is ToolCallStatus.FAILED
        assert row.error_class == "output_validation" and row.output is None

    async def test_a_non_model_return_is_malformed_output(
        self, adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock
    ) -> None:
        async def returns_none(args: Any, ctx: ToolContext) -> Any:
            return None

        registry = make_registry(adapters, uow_factory, clock, {ToolName.GET_LEAD: returns_none})
        _, step_uuid, coro = await self._dispatch_get_lead(registry, uow_factory)
        with pytest.raises(OutputValidationError, match="not its output model"):
            await coro
        (row,) = await rows_for(uow_factory, step_uuid)
        assert row.error_class == "output_validation"

    async def test_an_unexpected_exception_is_an_internal_failure_with_the_cause_chained(
        self, adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock
    ) -> None:
        registry = make_registry(
            adapters, uow_factory, clock, {ToolName.GET_LEAD: failing_with(RuntimeError("boom"))}
        )
        _, step_uuid, coro = await self._dispatch_get_lead(registry, uow_factory)
        with pytest.raises(InternalError) as exc:
            await coro
        assert isinstance(exc.value.__cause__, RuntimeError)
        (row,) = await rows_for(uow_factory, step_uuid)
        assert row.error_class == "internal" and "RuntimeError" in (row.error_message or "")

    async def test_store_unavailability_is_transient_not_internal(
        self, adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock
    ) -> None:
        registry = make_registry(
            adapters,
            uow_factory,
            clock,
            {ToolName.GET_LEAD: failing_with(OperationalError("SELECT 1", {}, ConnectionError()))},
        )
        _, step_uuid, coro = await self._dispatch_get_lead(registry, uow_factory)
        with pytest.raises(TransientToolError):
            await coro
        (row,) = await rows_for(uow_factory, step_uuid)
        assert row.error_class == "transient"

    async def test_a_policy_violation_raised_by_the_adapter_is_terminal_and_loud(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """Recipient pinning (§16.3 #3) lives in the adapter; the dispatcher
        records it as an executed attempt that failed with POLICY_VIOLATION."""
        g = await Gated.create(uow_factory, adapters)
        g.args = {"draft_id": g.draft_id, "to_email": "ceo@acme.example"}
        token = await g.approve(uow_factory)  # the human approved the wrong address
        with pytest.raises(PolicyViolation, match="does not match lead email"):
            await g.dispatch(registry, token=token)
        assert await outbox_rows(_sf(uow_factory)) == []
        (row,) = await rows_for(uow_factory, g.step_uuid)
        assert row.status is ToolCallStatus.FAILED
        assert row.error_class == "policy_violation" and row.adapter == "mock"
        events = await events_for(uow_factory, g.run_id)
        assert kinds(events) == ["tool_started", "tool_failed", "policy_violation"]

    async def test_a_cancelled_attempt_is_recorded_and_the_cancellation_propagates(
        self, adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock
    ) -> None:
        started = asyncio.Event()

        async def blocks(args: Any, ctx: ToolContext) -> Any:
            started.set()
            await asyncio.Event().wait()

        registry = make_registry(adapters, uow_factory, clock, {ToolName.GET_LEAD: blocks})
        run_id, step_uuid, coro = await self._dispatch_get_lead(registry, uow_factory)
        task = asyncio.create_task(coro)
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        (row,) = await rows_for(uow_factory, step_uuid)
        assert row.status is ToolCallStatus.FAILED and row.error_message == (
            "attempt cancelled while executing"
        )
        events = await events_for(uow_factory, run_id)
        assert kinds(events) == ["tool_started", "tool_failed"]
        assert events[1].status == "cancelled"

    async def test_policy_rejection_and_integration_failure_are_distinguishable(
        self, adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock
    ) -> None:
        """Same tool, same step shape: one never reached the port, one did."""
        registry = make_registry(adapters, uow_factory, clock)
        g = await Gated.create(uow_factory, adapters)
        with pytest.raises(PolicyViolation) as rejected:
            await g.dispatch(registry, attempt=1)
        failing = make_registry(
            adapters,
            uow_factory,
            clock,
            {ToolName.SEND_EMAIL_MOCK: failing_with(TransientToolError("provider 503"))},
        )
        token = await g.approve(uow_factory)
        with pytest.raises(TransientToolError) as failed:
            await g.dispatch(failing, attempt=2, token=token)

        assert rejected.value.detail["dispatch"]["outcome"] == "rejected"
        assert failed.value.detail["dispatch"]["outcome"] == "failed"
        one, two = await rows_for(uow_factory, g.step_uuid)
        assert (one.error_class, one.adapter) == ("policy_violation", None)
        assert (two.error_class, two.adapter) == ("transient", "mock")
        events = await events_for(uow_factory, g.run_id)
        assert [e.status for e in events if e.kind is TraceEventKind.TOOL_FAILED] == [
            "rejected",
            "failed",
        ]


# ===========================================================================
# 5. Attempt slots and the one-row-per-attempt invariant
# ===========================================================================
class TestAttemptSlots:
    async def test_the_same_attempt_cannot_be_dispatched_twice(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        g = await Gated.create(uow_factory, adapters)
        token = await g.approve(uow_factory)
        await g.dispatch(registry, token=token, attempt=1)
        with pytest.raises(DuplicateAttemptError):
            await g.dispatch(registry, token=token, attempt=1)
        assert len(await rows_for(uow_factory, g.step_uuid)) == 1
        assert len(await outbox_rows(_sf(uow_factory))) == 1

    async def test_a_rejected_attempt_still_consumes_its_slot(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        g = await Gated.create(uow_factory, adapters)
        with pytest.raises(ApprovalRequiredError):
            await g.dispatch(registry, attempt=1)
        with pytest.raises(DuplicateAttemptError):
            await g.dispatch(registry, attempt=1)
        assert len(await rows_for(uow_factory, g.step_uuid)) == 1


# ===========================================================================
# 6. Races — real concurrency against real Postgres
# ===========================================================================
class TestRaces:
    async def test_concurrent_duplicate_mutations_apply_exactly_once(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """Six retries of one approved send race: one applies, five replay,
        one outbox row, and the records say exactly that."""
        g = await Gated.create(uow_factory, adapters)
        token = await g.approve(uow_factory)
        results: list[DispatchResult] = await asyncio.gather(
            *(g.dispatch(registry, token=token, attempt=n) for n in range(1, 7))
        )
        outcomes = sorted(r.outcome.value for r in results)
        assert outcomes == ["duplicate_suppressed"] * 5 + ["succeeded"]
        assert len({r.output.message_id for r in results}) == 1  # type: ignore[attr-defined]
        assert len(await outbox_rows(_sf(uow_factory))) == 1
        rows = await rows_for(uow_factory, g.step_uuid)
        assert sorted(r.status.value for r in rows) == outcomes
        events = await events_for(uow_factory, g.run_id)
        assert sorted(kinds(events)) == sorted(
            ["tool_started"] * 6 + ["tool_succeeded"] + ["tool_duplicate_suppressed"] * 5
        )

    async def test_concurrent_dispatch_of_one_attempt_executes_it_once(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        g = await Gated.create(uow_factory, adapters)
        token = await g.approve(uow_factory)
        results = await asyncio.gather(
            *(g.dispatch(registry, token=token, attempt=1) for _ in range(4)),
            return_exceptions=True,
        )
        winners = [r for r in results if isinstance(r, DispatchResult)]
        losers = [r for r in results if not isinstance(r, DispatchResult)]
        assert len(winners) == 1 and winners[0].outcome is DispatchOutcome.SUCCEEDED
        assert len(losers) == 3 and all(isinstance(e, DuplicateAttemptError) for e in losers)
        assert len(await rows_for(uow_factory, g.step_uuid)) == 1
        assert len(await outbox_rows(_sf(uow_factory))) == 1

    async def test_a_rejection_racing_execution_never_mutates(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """The token was minted while the row was still pending (a bug or a
        forgery); the operator rejects concurrently. Whichever order the
        database serialises, the row is never `approved`, so nothing sends."""
        g = await Gated.create(uow_factory, adapters)
        pending_id = await create_approval(
            uow_factory, run_id=g.run_id, step_id="s6", args=g.args, status=ApprovalStatus.PENDING
        )
        token = mint(pending_id, g.run_id, "s6", g.args)

        async def reject() -> None:
            async with uow_factory() as uow:
                await uow.approvals.decide(
                    pending_id, status=ApprovalStatus.REJECTED, decided_by="op", decided_at=T0
                )
                await uow.commit()

        dispatched, _ = await asyncio.gather(
            g.dispatch(registry, token=token), reject(), return_exceptions=True
        )
        assert isinstance(dispatched, ApprovalInvalidError)
        assert dispatched.detail["mismatch"][0].startswith("status=")
        assert await outbox_rows(_sf(uow_factory)) == []

    async def test_a_grant_racing_execution_is_consistent_with_the_effect(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        """Approve and dispatch race. Either the dispatcher saw `approved`
        and one email exists, or it saw `pending` and none does — never a
        send without a stored approval, and a retry afterwards converges."""
        g = await Gated.create(uow_factory, adapters)
        pending_id = await create_approval(
            uow_factory, run_id=g.run_id, step_id="s6", args=g.args, status=ApprovalStatus.PENDING
        )
        token = mint(pending_id, g.run_id, "s6", g.args)

        async def approve() -> None:
            async with uow_factory() as uow:
                await uow.approvals.decide(
                    pending_id, status=ApprovalStatus.APPROVED, decided_by="op", decided_at=T0
                )
                await uow.commit()

        dispatched, _ = await asyncio.gather(
            g.dispatch(registry, token=token), approve(), return_exceptions=True
        )
        outbox = await outbox_rows(_sf(uow_factory))
        if isinstance(dispatched, DispatchResult):
            assert dispatched.outcome is DispatchOutcome.SUCCEEDED and len(outbox) == 1
        else:
            assert isinstance(dispatched, ApprovalInvalidError) and outbox == []

        retry = await g.dispatch(registry, token=token, attempt=2)
        assert retry.outcome in (DispatchOutcome.SUCCEEDED, DispatchOutcome.DUPLICATE_SUPPRESSED)
        assert len(await outbox_rows(_sf(uow_factory))) == 1

    async def test_replay_is_detected_across_separate_execution_contexts(
        self, adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock
    ) -> None:
        """Two registries (two workers) share nothing in memory; the second's
        replay classification comes from the database."""
        g = await Gated.create(uow_factory, adapters)
        token = await g.approve(uow_factory)
        first = make_registry(adapters, uow_factory, clock)
        second = make_registry(adapters, uow_factory, clock)
        a = await g.dispatch(first, token=token, attempt=1)
        b = await g.dispatch(second, token=token, attempt=2)
        assert (a.outcome, b.outcome) == (
            DispatchOutcome.SUCCEEDED,
            DispatchOutcome.DUPLICATE_SUPPRESSED,
        )
        assert len(await outbox_rows(_sf(uow_factory))) == 1


# ===========================================================================
# 7. Nothing secret is persisted
# ===========================================================================
class TestNoSecretsPersisted:
    async def test_the_token_never_reaches_a_row_or_an_event(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory, adapters: Adapters
    ) -> None:
        g = await Gated.create(uow_factory, adapters)
        token = await g.approve(uow_factory)
        await g.dispatch(registry, token=token)
        with pytest.raises(ApprovalInvalidError):
            await g.dispatch(
                registry, token=token, attempt=2, arguments={**g.args, "to_email": "x@y.example"}
            )

        rows = await rows_for(uow_factory, g.step_uuid)
        events = await events_for(uow_factory, g.run_id)
        persisted = json.dumps(
            [r.input for r in rows]
            + [r.output for r in rows]
            + [e.input for e in events]
            + [e.output for e in events]
            + [e.payload for e in events]
            + [e.error for e in events],
            default=str,
        )
        assert "approval_token" not in persisted
        assert repr(token) not in persisted
        assert "mint" not in persisted

    async def test_denylisted_argument_keys_are_redacted_before_persistence(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s1", ToolName.SEARCH_LEADS)
        with pytest.raises(InputValidationError):
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s1",
                tool_name=ToolName.SEARCH_LEADS,
                arguments={
                    "industry": "technology",
                    "password": "hunter2",
                    "api_key": "sk-ant-abcdefghijkl",
                },
                attempt=1,
            )
        (row,) = await rows_for(uow_factory, step_uuid)
        assert row.input == {
            "industry": "technology",
            "password": "[redacted]",
            "api_key": "[redacted]",
        }
        events = await events_for(uow_factory, run_id)
        assert "hunter2" not in json.dumps([e.input for e in events] + [e.error for e in events])

    async def test_oversized_payloads_are_truncated_with_a_marker(
        self, adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock
    ) -> None:
        registry = make_registry(adapters, uow_factory, clock, trace_payload_max_bytes=256)
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s4", ToolName.SCORE_LEAD)
        company = {**_company(), "summary": "x" * 2000}
        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s4",
            tool_name=ToolName.SCORE_LEAD,
            arguments={"lead_id": "L-104", "company": company},
            attempt=1,
        )
        assert result.output_data["score"] == 72  # the artifact itself is complete
        (row,) = await rows_for(uow_factory, step_uuid)
        assert row.input["_truncated"] is True and row.input["_original_bytes"] > 256
