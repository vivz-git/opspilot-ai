"""Tests for the nine concrete tool implementations and registry bindings (TOOL-003).

Verifies each tool executes through ToolRegistry.dispatch against real Postgres
and mock adapters, honoring contracts, input validation, output validation,
error classification, safety invariants, and determinism.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
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
    NotFoundError,
    PolicyViolation,
    StaleWriteError,
)
from app.integrations.mock import build_mock_adapters, seed_database
from app.integrations.ports import Adapters, DraftInput
from app.persistence.protocols import UnitOfWorkFactory
from app.persistence.session import create_session_factory, unit_of_work
from app.runtime import FixedClock, SequentialIdGenerator
from app.security import ApprovalGate, ApprovalToken, canonical_args_hash
from app.tools.contracts import REGISTRY, RiskLevel, ToolName
from app.tools.impl import (
    TOOL_IMPLEMENTATIONS,
    default_implementations,
    score_lead,
    search_leads,
    update_customer,
)
from app.tools.registry import (
    ApprovalRequiredError,
    DispatchOutcome,
    ToolContext,
    ToolRegistry,
)
from app.tools.schemas import (
    CompanyProfile,
    CustomerStatus,
    DraftOutreachOutput,
    GetCustomerOutput,
    GetLeadOutput,
    ResearchCompanyOutput,
    SaveDraftOutput,
    ScoreBand,
    ScoreLeadOutput,
    SearchLeadsOutput,
    SendEmailMockOutput,
    Signal,
    Tone,
    UpdateCustomerOutput,
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
DANA_EMAIL = "dana@northwind.example"  # lead L-104 / cust_1


# ---------------------------------------------------------------------------
# Database & Test Environment Fixtures
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
        pytest.skip("no reachable Postgres for this session (TOOL-003 integration test)")


@pytest.fixture(scope="module", autouse=True)
def _migrated_database() -> None:
    _require_database()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url.get_secret_value())
    command.upgrade(config, "head")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        get_settings().database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
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


@pytest.fixture
def registry(adapters: Adapters, uow_factory: UnitOfWorkFactory, clock: FixedClock) -> ToolRegistry:
    # Uses default implementations bound by ToolRegistry.__init__
    return ToolRegistry(adapters=adapters, uow_factory=uow_factory, clock=clock)


# ---------------------------------------------------------------------------
# Helpers to setup execution context
# ---------------------------------------------------------------------------
async def create_run(uow_factory: UnitOfWorkFactory) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with uow_factory() as uow:
        await uow.agent_runs.create(
            id=run_id,
            user_request="dispatch TOOL-003 tests",
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
    tool: ToolName,
    risk: RiskLevel = RiskLevel.HIGH,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
) -> uuid.UUID:
    async with uow_factory() as uow:
        row = await uow.approvals.create_request(
            run_id=run_id,
            step_id=step_id,
            tool=tool,
            risk=risk,
            title=f"Approve {tool.value}",
            summary="scripted approval",
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


def mint_token(
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


# ---------------------------------------------------------------------------
# 1. Nine-Tool Completeness & Registry Bindings Tests
# ---------------------------------------------------------------------------
class TestRegistryCompleteness:
    def test_all_nine_tools_are_declared_and_implemented(self) -> None:
        expected_tools = {
            ToolName.SEARCH_LEADS,
            ToolName.GET_LEAD,
            ToolName.RESEARCH_COMPANY,
            ToolName.SCORE_LEAD,
            ToolName.DRAFT_OUTREACH,
            ToolName.SAVE_DRAFT,
            ToolName.SEND_EMAIL_MOCK,
            ToolName.GET_CUSTOMER,
            ToolName.UPDATE_CUSTOMER,
        }
        assert set(REGISTRY.keys()) == expected_tools
        assert set(TOOL_IMPLEMENTATIONS.keys()) == expected_tools
        assert len(TOOL_IMPLEMENTATIONS) == 9

    def test_default_registry_binds_all_nine_tools(self, registry: ToolRegistry) -> None:
        for tool_name in REGISTRY:
            assert registry.is_bound(tool_name), f"Tool {tool_name} must be bound by default"
            contract = registry.contract(tool_name)
            assert contract.name == tool_name

    def test_default_implementations_factory_returns_fresh_copy(self) -> None:
        impls = default_implementations()
        assert len(impls) == 9
        assert impls[ToolName.SEARCH_LEADS] is search_leads
        assert impls[ToolName.UPDATE_CUSTOMER] is update_customer
        # Modifying the returned dict does not mutate the source mapping
        impls.pop(ToolName.SEARCH_LEADS)
        assert ToolName.SEARCH_LEADS in TOOL_IMPLEMENTATIONS


# ---------------------------------------------------------------------------
# 2. Tool-by-Tool Concrete Dispatch Tests
# ---------------------------------------------------------------------------
class TestSearchLeadsTool:
    async def test_search_leads_success(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s_search", ToolName.SEARCH_LEADS)

        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s_search",
            tool_name=ToolName.SEARCH_LEADS,
            arguments={"query": "Dana", "limit": 5},
            attempt=1,
        )
        assert result.outcome is DispatchOutcome.SUCCEEDED
        output = result.output
        assert isinstance(output, SearchLeadsOutput)
        assert output.total_matched >= 1
        assert any(lead.full_name == "Dana Miller" for lead in output.leads)

    async def test_search_leads_zero_matches_is_success(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s_search_zero", ToolName.SEARCH_LEADS)

        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s_search_zero",
            tool_name=ToolName.SEARCH_LEADS,
            arguments={"query": "NonexistentLeadXYZ"},
            attempt=1,
        )
        assert result.outcome is DispatchOutcome.SUCCEEDED
        assert isinstance(result.output, SearchLeadsOutput)
        assert result.output.leads == []
        assert result.output.total_matched == 0

    async def test_search_leads_input_validation(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s_search_val", ToolName.SEARCH_LEADS)

        # Contradictory filters: min_employees > max_employees
        with pytest.raises(InputValidationError):
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s_search_val",
                tool_name=ToolName.SEARCH_LEADS,
                arguments={"min_employees": 100, "max_employees": 10},
                attempt=1,
            )


class TestGetLeadTool:
    async def test_get_lead_success(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s_get_lead", ToolName.GET_LEAD)

        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s_get_lead",
            tool_name=ToolName.GET_LEAD,
            arguments={"lead_id": "L-104"},
            attempt=1,
        )
        assert result.outcome is DispatchOutcome.SUCCEEDED
        output = result.output
        assert isinstance(output, GetLeadOutput)
        assert output.lead.lead_id == "L-104"
        assert output.lead.full_name == "Dana Miller"
        assert output.lead.email == DANA_EMAIL

    async def test_get_lead_not_found(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s_get_lead_missing", ToolName.GET_LEAD)

        with pytest.raises(NotFoundError) as exc:
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s_get_lead_missing",
                tool_name=ToolName.GET_LEAD,
                arguments={"lead_id": "nonexistent_lead_id"},
                attempt=1,
            )
        assert exc.value.error_class is ErrorClass.NOT_FOUND


class TestResearchCompanyTool:
    async def test_research_company_standard_depth(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s_res_comp", ToolName.RESEARCH_COMPANY)

        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s_res_comp",
            tool_name=ToolName.RESEARCH_COMPANY,
            arguments={"company_id": "comp_northwind", "depth": "standard"},
            attempt=1,
        )
        assert result.outcome is DispatchOutcome.SUCCEEDED
        output = result.output
        assert isinstance(output, ResearchCompanyOutput)
        assert output.profile.company_id == "comp_northwind"
        assert output.profile.domain == "northwind.example"
        assert output.profile.confidence == 0.95
        assert len(output.profile.summary) > 0

    async def test_research_company_by_domain_basic_depth(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(
            uow_factory, run_id, "s_res_domain", ToolName.RESEARCH_COMPANY
        )

        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s_res_domain",
            tool_name=ToolName.RESEARCH_COMPANY,
            arguments={"domain": "northwind.example", "depth": "basic"},
            attempt=1,
        )
        assert result.outcome is DispatchOutcome.SUCCEEDED
        assert isinstance(result.output, ResearchCompanyOutput)
        assert result.output.profile.confidence == 0.85

    async def test_research_company_not_found(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(
            uow_factory, run_id, "s_res_missing", ToolName.RESEARCH_COMPANY
        )

        with pytest.raises(NotFoundError):
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s_res_missing",
                tool_name=ToolName.RESEARCH_COMPANY,
                arguments={"company_id": "comp_missing"},
                attempt=1,
            )


class TestScoreLeadTool:
    @pytest.fixture
    def sample_profile(self) -> CompanyProfile:
        return CompanyProfile(
            company_id="comp_northwind",
            name="Northwind Traders",
            domain="northwind.example",
            industry="fintech",
            employee_count=250,
            revenue_band="$10M-$50M",
            hq_location="London, UK",
            funding_stage="Series B",
            tech_stack=["Python", "PostgreSQL", "React"],
            recent_signals=[Signal(kind="expansion", summary="Opened new office in London")],
            summary="Enterprise trade and logistics provider.",
            sources=["https://northwind.example"],
            confidence=0.95,
            retrieved_at=T0,
        )

    async def test_score_lead_deterministic_calculation(
        self,
        registry: ToolRegistry,
        uow_factory: UnitOfWorkFactory,
        sample_profile: CompanyProfile,
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s_score", ToolName.SCORE_LEAD)

        args = {
            "lead_id": "L-104",
            "company": sample_profile.model_dump(mode="json"),
            "weights": {
                "company_fit": 0.4,
                "engagement": 0.3,
                "signal_strength": 0.2,
                "data_quality": 0.1,
            },
        }

        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s_score",
            tool_name=ToolName.SCORE_LEAD,
            arguments=args,
            attempt=1,
        )
        assert result.outcome is DispatchOutcome.SUCCEEDED
        output = result.output
        assert isinstance(output, ScoreLeadOutput)
        assert 0 <= output.score <= 100
        assert output.band in (ScoreBand.HOT, ScoreBand.WARM, ScoreBand.COLD)
        assert len(output.factors) == 4

        # Invariant: sum(factors.contribution) ≈ score (±1)
        factors_sum = sum(f.contribution for f in output.factors)
        assert abs(factors_sum - output.score) <= 1.0

        # Determinism check: re-running gives byte-identical result
        step_uuid2 = await create_step(uow_factory, run_id, "s_score2", ToolName.SCORE_LEAD, seq=2)
        result2 = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid2,
            step_id="s_score2",
            tool_name=ToolName.SCORE_LEAD,
            arguments=args,
            attempt=1,
        )
        assert result2.output.model_dump() == output.model_dump()

    async def test_score_lead_is_pure_tool(
        self, sample_profile: CompanyProfile, clock: FixedClock
    ) -> None:
        # Direct check that score_lead enforces ctx.port is None
        ctx = ToolContext(
            run_id=uuid.uuid4(),
            execution_step_id=uuid.uuid4(),
            step_id="s_pure",
            tool=ToolName.SCORE_LEAD,
            attempt=1,
            args_hash="hash",
            idempotency_key=None,
            port=object(),  # Stray port
            clock=clock,
        )
        from app.tools.schemas import ScoreLeadInput

        with pytest.raises(Exception, match="expects no port"):
            await score_lead(ScoreLeadInput(lead_id="L-1", company=sample_profile), ctx)


class TestDraftOutreachTool:
    @pytest.fixture
    def sample_profile(self) -> CompanyProfile:
        return CompanyProfile(
            company_id="comp_northwind",
            name="Northwind Traders",
            domain="northwind.example",
            industry="fintech",
            employee_count=250,
            summary="Enterprise trade and logistics provider.",
            sources=["https://northwind.example"],
            confidence=0.95,
            retrieved_at=T0,
        )

    async def test_draft_outreach_tones(
        self,
        registry: ToolRegistry,
        uow_factory: UnitOfWorkFactory,
        sample_profile: CompanyProfile,
    ) -> None:
        run_id = await create_run(uow_factory)

        for i, tone in enumerate([Tone.DIRECT, Tone.WARM, Tone.FORMAL], start=1):
            step_id = f"s_draft_{tone.value}"
            step_uuid = await create_step(
                uow_factory, run_id, step_id, ToolName.DRAFT_OUTREACH, seq=i
            )
            result = await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id=step_id,
                tool_name=ToolName.DRAFT_OUTREACH,
                arguments={
                    "lead_id": "L-104",
                    "company": sample_profile.model_dump(mode="json"),
                    "tone": tone.value,
                    "max_words": 150,
                },
                attempt=1,
            )
            assert result.outcome is DispatchOutcome.SUCCEEDED
            output = result.output
            assert isinstance(output, DraftOutreachOutput)
            assert output.word_count <= 150
            assert len(output.subject) > 0
            assert len(output.body) > 0
            assert len(output.content_hash) == 64
            # Assert no unresolved placeholders
            for ph in ("{{", "TODO", "[NAME]"):
                assert ph not in output.body and ph not in output.subject


class TestSaveDraftTool:
    async def test_save_draft_success(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s_save", ToolName.SAVE_DRAFT)

        subject = "Partnership with Northwind"
        body = "Hello Dana, let us connect next week."
        content_hash = hashlib.sha256(f"{subject}\n\n{body}".encode()).hexdigest()

        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s_save",
            tool_name=ToolName.SAVE_DRAFT,
            arguments={
                "lead_id": "L-104",
                "subject": subject,
                "body": body,
                "content_hash": content_hash,
            },
            attempt=1,
        )
        assert result.outcome is DispatchOutcome.SUCCEEDED
        output = result.output
        assert isinstance(output, SaveDraftOutput)
        assert output.status == "saved"
        assert output.draft_id.startswith("drf_")
        assert output.content_hash == content_hash

    async def test_save_draft_hash_mismatch_raises_policy_violation(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s_save_tamper", ToolName.SAVE_DRAFT)

        with pytest.raises(PolicyViolation) as exc:
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s_save_tamper",
                tool_name=ToolName.SAVE_DRAFT,
                arguments={
                    "lead_id": "L-104",
                    "subject": "Original Subject",
                    "body": "Modified body that does not match hash",
                    "content_hash": "a" * 64,
                },
                attempt=1,
            )
        assert exc.value.error_class is ErrorClass.POLICY_VIOLATION
        assert "content_hash does not match" in str(exc.value)

    async def test_save_draft_missing_lead_raises_not_found(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s_save_nolead", ToolName.SAVE_DRAFT)

        subject = "Hello"
        body = "World"
        h = hashlib.sha256(f"{subject}\n\n{body}".encode()).hexdigest()

        with pytest.raises(NotFoundError):
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s_save_nolead",
                tool_name=ToolName.SAVE_DRAFT,
                arguments={
                    "lead_id": "lead_missing_999",
                    "subject": subject,
                    "body": body,
                    "content_hash": h,
                },
                attempt=1,
            )


class TestSendEmailMockTool:
    async def _setup_saved_draft(
        self, adapters: Adapters, lead_id: str = "L-104"
    ) -> tuple[str, str]:
        subject = "Meeting follow up"
        body = "Let us connect soon."
        h = hashlib.sha256(f"{subject}\n\n{body}".encode()).hexdigest()
        rec = await adapters.drafts.save(
            DraftInput(
                lead_id=lead_id,
                subject=subject,
                body=body,
                content_hash=h,
            )
        )
        return rec.draft_id, h

    async def test_send_email_mock_success_with_approval(
        self, registry: ToolRegistry, adapters: Adapters, uow_factory: UnitOfWorkFactory
    ) -> None:
        draft_id, _ = await self._setup_saved_draft(adapters, "L-104")
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s_send", ToolName.SEND_EMAIL_MOCK)

        args = {"draft_id": draft_id, "to_email": DANA_EMAIL}
        app_id = await create_approval(
            uow_factory,
            run_id=run_id,
            step_id="s_send",
            args=args,
            tool=ToolName.SEND_EMAIL_MOCK,
        )
        token = mint_token(app_id, run_id, "s_send", args)

        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s_send",
            tool_name=ToolName.SEND_EMAIL_MOCK,
            arguments=args,
            attempt=1,
            approval_token=token,
        )
        assert result.outcome is DispatchOutcome.SUCCEEDED
        output = result.output
        assert isinstance(output, SendEmailMockOutput)
        assert output.status == "sent"
        assert output.provider == "mock"
        assert output.to_email == DANA_EMAIL
        assert output.draft_id == draft_id
        assert output.message_id.startswith("msg_")

    async def test_send_email_mock_recipient_mismatch_raises_policy_violation(
        self, registry: ToolRegistry, adapters: Adapters, uow_factory: UnitOfWorkFactory
    ) -> None:
        draft_id, _ = await self._setup_saved_draft(adapters, "L-104")
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(
            uow_factory, run_id, "s_send_mismatch", ToolName.SEND_EMAIL_MOCK
        )

        # Recipient is NOT the lead's email (dana@northwind.example)
        wrong_email = "attacker@foreign.example"
        args = {"draft_id": draft_id, "to_email": wrong_email}
        app_id = await create_approval(
            uow_factory,
            run_id=run_id,
            step_id="s_send_mismatch",
            args=args,
            tool=ToolName.SEND_EMAIL_MOCK,
        )
        token = mint_token(app_id, run_id, "s_send_mismatch", args)

        with pytest.raises(PolicyViolation) as exc:
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s_send_mismatch",
                tool_name=ToolName.SEND_EMAIL_MOCK,
                arguments=args,
                attempt=1,
                approval_token=token,
            )
        assert exc.value.error_class is ErrorClass.POLICY_VIOLATION
        assert "does not match lead email" in str(exc.value)

    async def test_send_email_mock_unapproved_raises_approval_required(
        self, registry: ToolRegistry, adapters: Adapters, uow_factory: UnitOfWorkFactory
    ) -> None:
        draft_id, _ = await self._setup_saved_draft(adapters, "L-104")
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s_send_unapp", ToolName.SEND_EMAIL_MOCK)

        with pytest.raises(ApprovalRequiredError):
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s_send_unapp",
                tool_name=ToolName.SEND_EMAIL_MOCK,
                arguments={"draft_id": draft_id, "to_email": DANA_EMAIL},
                attempt=1,
                approval_token=None,
            )


class TestGetCustomerTool:
    async def test_get_customer_by_id(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s_get_cust", ToolName.GET_CUSTOMER)

        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s_get_cust",
            tool_name=ToolName.GET_CUSTOMER,
            arguments={"customer_id": "cust_1"},
            attempt=1,
        )
        assert result.outcome is DispatchOutcome.SUCCEEDED
        output = result.output
        assert isinstance(output, GetCustomerOutput)
        assert output.customer.customer_id == "cust_1"
        assert output.customer.email == DANA_EMAIL
        assert output.customer.version == 1

    async def test_get_customer_by_email(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(
            uow_factory, run_id, "s_get_cust_email", ToolName.GET_CUSTOMER
        )

        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s_get_cust_email",
            tool_name=ToolName.GET_CUSTOMER,
            arguments={"email": DANA_EMAIL},
            attempt=1,
        )
        assert result.outcome is DispatchOutcome.SUCCEEDED
        assert isinstance(result.output, GetCustomerOutput)
        assert result.output.customer.customer_id == "cust_1"

    async def test_get_customer_not_found(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(uow_factory, run_id, "s_cust_missing", ToolName.GET_CUSTOMER)

        with pytest.raises(NotFoundError):
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s_cust_missing",
                tool_name=ToolName.GET_CUSTOMER,
                arguments={"customer_id": "cust_missing_999"},
                attempt=1,
            )


class TestUpdateCustomerTool:
    async def test_update_customer_success_with_approval(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(
            uow_factory, run_id, "s_update_cust", ToolName.UPDATE_CUSTOMER
        )

        args = {
            "customer_id": "cust_1",
            "expected_version": 1,
            "patch": {"status": "active", "notes": "Upgraded tier"},
            "reason": "Customer renewed subscription",
        }
        app_id = await create_approval(
            uow_factory,
            run_id=run_id,
            step_id="s_update_cust",
            args=args,
            tool=ToolName.UPDATE_CUSTOMER,
        )
        token = mint_token(app_id, run_id, "s_update_cust", args)

        result = await registry.dispatch(
            run_id=run_id,
            execution_step_id=step_uuid,
            step_id="s_update_cust",
            tool_name=ToolName.UPDATE_CUSTOMER,
            arguments=args,
            attempt=1,
            approval_token=token,
        )
        assert result.outcome is DispatchOutcome.SUCCEEDED
        output = result.output
        assert isinstance(output, UpdateCustomerOutput)
        assert output.customer_id == "cust_1"
        assert output.version == 2
        assert sorted(output.updated_fields) == ["notes", "status"]
        assert "status" in output.previous
        assert output.previous["status"] == CustomerStatus.ACTIVE

    async def test_update_customer_stale_version_raises_stale_write_error(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(
            uow_factory, run_id, "s_update_stale", ToolName.UPDATE_CUSTOMER
        )

        # Expected version is 99 while actual customer is at version 1
        args = {
            "customer_id": "cust_1",
            "expected_version": 99,
            "patch": {"notes": "Stale update"},
            "reason": "Attempting stale write",
        }
        app_id = await create_approval(
            uow_factory,
            run_id=run_id,
            step_id="s_update_stale",
            args=args,
            tool=ToolName.UPDATE_CUSTOMER,
        )
        token = mint_token(app_id, run_id, "s_update_stale", args)

        with pytest.raises(StaleWriteError) as exc:
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s_update_stale",
                tool_name=ToolName.UPDATE_CUSTOMER,
                arguments=args,
                attempt=1,
                approval_token=token,
            )
        assert exc.value.error_class is ErrorClass.STALE_WRITE

    async def test_update_customer_missing_raises_not_found(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(
            uow_factory, run_id, "s_update_noid", ToolName.UPDATE_CUSTOMER
        )

        args = {
            "customer_id": "cust_missing_999",
            "expected_version": 1,
            "patch": {"status": "active"},
            "reason": "Missing customer update",
        }
        app_id = await create_approval(
            uow_factory,
            run_id=run_id,
            step_id="s_update_noid",
            args=args,
            tool=ToolName.UPDATE_CUSTOMER,
        )
        token = mint_token(app_id, run_id, "s_update_noid", args)

        with pytest.raises(NotFoundError):
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s_update_noid",
                tool_name=ToolName.UPDATE_CUSTOMER,
                arguments=args,
                attempt=1,
                approval_token=token,
            )


# ---------------------------------------------------------------------------
# 3. Regression & Central Dispatch Boundary Protection
# ---------------------------------------------------------------------------
class TestDispatchBoundaryProtection:
    async def test_cannot_invoke_tool_with_wrong_port(self, clock: FixedClock) -> None:
        """A tool implementation immediately rejects an incompatible port."""
        ctx = ToolContext(
            run_id=uuid.uuid4(),
            execution_step_id=uuid.uuid4(),
            step_id="s1",
            tool=ToolName.SEARCH_LEADS,
            attempt=1,
            args_hash="hash",
            idempotency_key=None,
            port=object(),  # Incompatible port
            clock=clock,
        )
        from app.tools.schemas import SearchLeadsInput

        with pytest.raises(Exception, match="requires a LeadPort"):
            await search_leads(SearchLeadsInput(query="test"), ctx)

    async def test_gated_tools_require_approval_through_dispatcher(
        self, registry: ToolRegistry, uow_factory: UnitOfWorkFactory
    ) -> None:
        """Dispatcher enforces that gated tools reject unapproved execution."""
        run_id = await create_run(uow_factory)
        step_uuid = await create_step(
            uow_factory, run_id, "s_update_gate", ToolName.UPDATE_CUSTOMER
        )

        with pytest.raises(ApprovalRequiredError):
            await registry.dispatch(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s_update_gate",
                tool_name=ToolName.UPDATE_CUSTOMER,
                arguments={
                    "customer_id": "cust_1",
                    "expected_version": 1,
                    "patch": {"status": "active"},
                    "reason": "Bypass attempt",
                },
                attempt=1,
                approval_token=None,
            )
