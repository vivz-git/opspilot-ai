"""Integration tests for mock tool ports and adapters (§19.1, §19.2, TOOL-001).

Validates:
1. Port protocol conformance for all 6 ports (Lead, Company, Customer, Draft, Mail, Content).
2. Deterministic seed dataset loading and RFC 2606 compliance (*.example / example.com).
3. Lead search filtering, pagination, and retrieval.
4. Company firmographic profiling and depth handling.
5. Customer retrieval, optimistic concurrency updates, and conflict rejection.
6. Outreach draft persistence and readback verification.
7. Outbound email dispatch, approval enforcement, recipient validation,
   outbox readback, and idempotent replay.
8. Deterministic content templating and hash verification.
9. Seeded failure injection behavior.
10. Adapter factory validation and configuration safety.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from app.config import IntegrationMode, Settings, get_settings
from app.errors import (
    ConfigurationError,
    InputValidationError,
    NotFoundError,
    PolicyViolation,
    StaleWriteError,
    TransientToolError,
)
from app.integrations import build_adapters
from app.integrations.mock import (
    COMPANY_FIXTURES,
    CUSTOMER_FIXTURES,
    FIXTURE_BASE_TIME,
    LEAD_FIXTURES,
    build_mock_adapters,
    seed_database,
)
from app.integrations.ports import (
    Adapters,
    CompanyPort,
    ContentPort,
    CustomerPort,
    DraftInput,
    DraftPort,
    LeadFilter,
    LeadPort,
    MailPort,
    OutboundMessage,
    OutreachBrief,
)
from app.persistence.session import create_session_factory
from app.runtime import DeterministicRandom, FixedClock, SequentialIdGenerator
from app.security import ApprovalGate, ApprovalToken, canonical_args_hash
from app.tools.schemas import CustomerPatch, CustomerStatus, LeadStatus, ResearchDepth
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).resolve().parent.parent


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
        pytest.skip("no reachable Postgres for this session (TOOL-001 integration test)")


@pytest.fixture(scope="module", autouse=True)
def _migrated_database() -> None:
    _require_database()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url.get_secret_value())
    command.upgrade(config, "head")


@pytest.fixture
async def async_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url.get_secret_value(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(async_engine)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXTURE_BASE_TIME)


@pytest.fixture
def id_gen() -> SequentialIdGenerator:
    return SequentialIdGenerator()


@pytest.fixture
def random_gen() -> DeterministicRandom:
    return DeterministicRandom(seed=42)


@pytest.fixture
def adapters(
    session_factory: async_sessionmaker[AsyncSession],
    clock: FixedClock,
    id_gen: SequentialIdGenerator,
    random_gen: DeterministicRandom,
) -> Adapters:
    return build_mock_adapters(session_factory, clock, id_gen, random_gen)


@pytest.fixture(autouse=True)
async def _seed_data(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Ensure database has fresh deterministic fixtures before each test."""
    await seed_database(session_factory, reset=True)


def _mint_token(
    *, run_id: str = "run_test_001", step_id: str = "step_01", args: dict[str, str] | None = None
) -> ApprovalToken:
    actual_args = args or {"action": "execute"}
    h = canonical_args_hash(actual_args)
    return ApprovalGate.issue(
        approval_id="appr_001",
        run_id=run_id,
        step_id=step_id,
        args=actual_args,
        approved_args_hash=h,
        decision="approve",
    )


# ---------------------------------------------------------------------------
# 1. Protocol Conformance
# ---------------------------------------------------------------------------
class TestProtocolConformance:
    def test_mock_adapters_satisfy_protocols(self, adapters: Adapters) -> None:
        assert isinstance(adapters.leads, LeadPort)
        assert isinstance(adapters.companies, CompanyPort)
        assert isinstance(adapters.customers, CustomerPort)
        assert isinstance(adapters.drafts, DraftPort)
        assert isinstance(adapters.mail, MailPort)
        assert isinstance(adapters.content, ContentPort)


# ---------------------------------------------------------------------------
# 2. Seed Dataset & RFC 2606 Compliance
# ---------------------------------------------------------------------------
class TestSeedFixturesAndRFC2606:
    def test_all_fixtures_use_rfc2606_domains(self) -> None:
        """Every fixture email and domain must use *.example or example.com."""
        for comp in COMPANY_FIXTURES:
            domain = str(comp["domain"])
            assert domain.endswith(".example") or domain == "example.com"
        for lead in LEAD_FIXTURES:
            domain = str(lead["email"]).split("@")[1]
            assert domain.endswith(".example") or domain == "example.com"
        for cust in CUSTOMER_FIXTURES:
            domain = str(cust["email"]).split("@")[1]
            assert domain.endswith(".example") or domain == "example.com"

    def test_canonical_lead_l104_exists(self) -> None:
        """L-104 'Dana Miller' is the canonical test lead per architecture."""
        l104 = next((item for item in LEAD_FIXTURES if item["lead_id"] == "L-104"), None)
        assert l104 is not None
        assert l104["full_name"] == "Dana Miller"
        assert l104["email"] == "dana@northwind.example"
        assert l104["company_id"] == "comp_northwind"

    async def test_seeding_is_idempotent(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Calling seed_database with reset=False should not duplicate rows."""
        counts = await seed_database(session_factory, reset=False)
        assert counts["companies"] == 0
        assert counts["leads"] == 0
        assert counts["customers"] == 0


# ---------------------------------------------------------------------------
# 3. LeadPort Tests
# ---------------------------------------------------------------------------
class TestLeadPort:
    async def test_search_by_industry(self, adapters: Adapters) -> None:
        page = await adapters.leads.search(LeadFilter(industry="technology", limit=10))
        assert page.total_matched >= 1
        assert any(item.company_id == "comp_northwind" for item in page.leads)

    async def test_search_by_status(self, adapters: Adapters) -> None:
        page = await adapters.leads.search(LeadFilter(status=LeadStatus.NEW, limit=20))
        assert page.total_matched >= 1
        assert all(item.status == LeadStatus.NEW for item in page.leads)

    async def test_search_by_employee_bounds(self, adapters: Adapters) -> None:
        page = await adapters.leads.search(
            LeadFilter(min_employees=1000, max_employees=5000, limit=20)
        )
        assert page.total_matched >= 1
        # Hooli has 4500 employees, Globex has 1200
        comp_ids = {item.company_id for item in page.leads}
        assert "comp_hooli" in comp_ids or "comp_globex" in comp_ids

    async def test_search_by_query(self, adapters: Adapters) -> None:
        page = await adapters.leads.search(LeadFilter(query="Dana", limit=10))
        assert page.total_matched == 1
        assert page.leads[0].lead_id == "L-104"
        assert page.leads[0].full_name == "Dana Miller"
        assert page.leads[0].company_name == "Northwind Traders"

    async def test_search_pagination(self, adapters: Adapters) -> None:
        page_1 = await adapters.leads.search(LeadFilter(limit=3, offset=0))
        page_2 = await adapters.leads.search(LeadFilter(limit=3, offset=3))
        assert len(page_1.leads) == 3
        assert len(page_2.leads) == 3
        assert page_1.truncated is True
        # Sets of lead IDs across pages must be disjoint
        p1_ids = {item.lead_id for item in page_1.leads}
        p2_ids = {item.lead_id for item in page_2.leads}
        assert p1_ids.isdisjoint(p2_ids)

    async def test_get_lead_detail(self, adapters: Adapters) -> None:
        lead = await adapters.leads.get("L-104")
        assert lead.lead_id == "L-104"
        assert lead.full_name == "Dana Miller"
        assert lead.email == "dana@northwind.example"
        assert lead.company_name == "Northwind Traders"
        assert "operations" in lead.tags

    async def test_get_nonexistent_lead_raises_not_found(self, adapters: Adapters) -> None:
        with pytest.raises(NotFoundError):
            await adapters.leads.get("nonexistent_lead_id")


# ---------------------------------------------------------------------------
# 4. CompanyPort Tests
# ---------------------------------------------------------------------------
class TestCompanyPort:
    async def test_profile_by_company_id(self, adapters: Adapters) -> None:
        prof = await adapters.companies.profile(company_id="comp_northwind")
        assert prof.company_id == "comp_northwind"
        assert prof.name == "Northwind Traders"
        assert prof.domain == "northwind.example"
        assert len(prof.recent_signals) >= 1
        assert prof.confidence >= 0.9

    async def test_profile_by_domain(self, adapters: Adapters) -> None:
        prof = await adapters.companies.profile(domain="northwind.example")
        assert prof.company_id == "comp_northwind"
        assert prof.name == "Northwind Traders"

    async def test_profile_basic_depth(self, adapters: Adapters) -> None:
        prof = await adapters.companies.profile(
            company_id="comp_northwind", depth=ResearchDepth.BASIC
        )
        assert len(prof.recent_signals) <= 1
        assert prof.confidence == 0.85

    async def test_profile_missing_company_raises_not_found(self, adapters: Adapters) -> None:
        with pytest.raises(NotFoundError):
            await adapters.companies.profile(company_id="comp_nonexistent")

    async def test_profile_invalid_input_raises_validation_error(self, adapters: Adapters) -> None:
        # Both company_id and domain provided
        with pytest.raises(InputValidationError):
            await adapters.companies.profile(
                company_id="comp_northwind", domain="northwind.example"
            )
        # Neither provided
        with pytest.raises(InputValidationError):
            await adapters.companies.profile()


# ---------------------------------------------------------------------------
# 5. CustomerPort Tests
# ---------------------------------------------------------------------------
class TestCustomerPort:
    async def test_get_customer_by_id(self, adapters: Adapters) -> None:
        cust = await adapters.customers.get(customer_id="cust_1")
        assert cust.customer_id == "cust_1"
        assert cust.account_name == "Northwind Traders"
        assert cust.version == 1

    async def test_get_customer_by_email(self, adapters: Adapters) -> None:
        cust = await adapters.customers.get(email="dana@northwind.example")
        assert cust.customer_id == "cust_1"

    async def test_get_customer_missing_raises_not_found(self, adapters: Adapters) -> None:
        with pytest.raises(NotFoundError):
            await adapters.customers.get(customer_id="cust_nonexistent")

    async def test_update_requires_approval_token(self, adapters: Adapters) -> None:
        with pytest.raises(PolicyViolation):
            await adapters.customers.update(
                "cust_1",
                CustomerPatch(status=CustomerStatus.ACTIVE),
                expected_version=1,
                token=None,  # type: ignore[arg-type]
                idempotency_key="idemp_key_001",
            )

    async def test_update_successful_with_optimistic_concurrency(self, adapters: Adapters) -> None:
        token = _mint_token()
        updated = await adapters.customers.update(
            "cust_1",
            CustomerPatch(notes="Upgraded enterprise tier"),
            expected_version=1,
            token=token,
            idempotency_key="idemp_key_update_1",
        )
        assert updated.customer_id == "cust_1"
        assert updated.version == 2
        # Read back to verify database state
        readback = await adapters.customers.get(customer_id="cust_1")
        assert readback.version == 2

    async def test_update_with_stale_version_raises_stale_write_error(
        self, adapters: Adapters
    ) -> None:
        token = _mint_token()
        # Stale expected_version=99
        with pytest.raises(StaleWriteError):
            await adapters.customers.update(
                "cust_1",
                CustomerPatch(status=CustomerStatus.ACTIVE),
                expected_version=99,
                token=token,
                idempotency_key="idemp_key_conflict",
            )

    async def test_update_missing_customer_raises_not_found(self, adapters: Adapters) -> None:
        token = _mint_token()
        with pytest.raises(NotFoundError):
            await adapters.customers.update(
                "cust_missing",
                CustomerPatch(status=CustomerStatus.ACTIVE),
                expected_version=1,
                token=token,
                idempotency_key="idemp_key_missing",
            )


# ---------------------------------------------------------------------------
# 6. DraftPort Tests
# ---------------------------------------------------------------------------
class TestDraftPort:
    async def test_save_and_get_draft(self, adapters: Adapters) -> None:
        draft_in = DraftInput(
            lead_id="L-104",
            subject="Partnership discussion",
            body="Hi Dana, would love to connect.",
            channel="email",
            content_hash="abc123def4567890",
            metadata={"source": "test"},
        )
        saved = await adapters.drafts.save(draft_in)
        assert saved.draft_id.startswith("drf_")
        assert saved.status == "saved"
        assert saved.lead_id == "L-104"
        assert saved.subject == "Partnership discussion"

        # Readback verification
        retrieved = await adapters.drafts.get(saved.draft_id)
        assert retrieved.draft_id == saved.draft_id
        assert retrieved.body == "Hi Dana, would love to connect."
        assert retrieved.content_hash == "abc123def4567890"

    async def test_save_draft_invalid_lead_raises_not_found(self, adapters: Adapters) -> None:
        draft_in = DraftInput(
            lead_id="L-INVALID",
            subject="Hello",
            body="Body content",
            content_hash="hash0123456789abcdef",
        )
        with pytest.raises(NotFoundError):
            await adapters.drafts.save(draft_in)

    async def test_get_nonexistent_draft_raises_not_found(self, adapters: Adapters) -> None:
        with pytest.raises(NotFoundError):
            await adapters.drafts.get("drf_nonexistent")


# ---------------------------------------------------------------------------
# 7. MailPort Tests
# ---------------------------------------------------------------------------
class TestMailPort:
    async def _create_draft(self, adapters: Adapters) -> str:
        saved = await adapters.drafts.save(
            DraftInput(
                lead_id="L-104",
                subject="Test Mail",
                body="Test email body",
                content_hash="hash0123456789abcdef",
            )
        )
        return saved.draft_id

    async def test_send_requires_approval_token(self, adapters: Adapters) -> None:
        draft_id = await self._create_draft(adapters)
        with pytest.raises(PolicyViolation):
            await adapters.mail.send(
                OutboundMessage(draft_id=draft_id, to_email="dana@northwind.example"),
                token=None,  # type: ignore[arg-type]
                idempotency_key="idemp_mail_01",
            )

    async def test_send_recipient_mismatch_raises_policy_violation(
        self, adapters: Adapters
    ) -> None:
        draft_id = await self._create_draft(adapters)
        token = _mint_token()
        # Lead L-104's email is dana@northwind.example, not someone_else@example.com
        with pytest.raises(PolicyViolation):
            await adapters.mail.send(
                OutboundMessage(draft_id=draft_id, to_email="someone_else@other.example"),
                token=token,
                idempotency_key="idemp_mail_mismatch",
            )

    async def test_send_and_get_outbox(self, adapters: Adapters) -> None:
        draft_id = await self._create_draft(adapters)
        token = _mint_token()
        receipt = await adapters.mail.send(
            OutboundMessage(draft_id=draft_id, to_email="dana@northwind.example"),
            token=token,
            idempotency_key="idemp_mail_success",
        )
        assert receipt.status == "sent"
        assert receipt.provider == "mock"
        assert receipt.to_email == "dana@northwind.example"

        # Readback verification
        outbox = await adapters.mail.get_outbox(receipt.message_id)
        assert outbox.message_id == receipt.message_id
        assert outbox.draft_id == draft_id
        assert outbox.to_email == "dana@northwind.example"
        assert outbox.status == "sent"

    async def test_send_duplicate_idempotency_key_returns_existing(
        self, adapters: Adapters
    ) -> None:
        draft_id = await self._create_draft(adapters)
        token = _mint_token()
        key = "idemp_mail_replay_test"

        receipt_1 = await adapters.mail.send(
            OutboundMessage(draft_id=draft_id, to_email="dana@northwind.example"),
            token=token,
            idempotency_key=key,
        )
        receipt_2 = await adapters.mail.send(
            OutboundMessage(draft_id=draft_id, to_email="dana@northwind.example"),
            token=token,
            idempotency_key=key,
        )
        # Idempotent replay must yield identical message and outbox IDs
        assert receipt_1.message_id == receipt_2.message_id
        assert receipt_1.outbox_id == receipt_2.outbox_id


# ---------------------------------------------------------------------------
# 8. ContentPort Tests
# ---------------------------------------------------------------------------
class TestContentPort:
    async def test_draft_direct_tone(self, adapters: Adapters) -> None:
        brief = OutreachBrief(
            lead_id="L-104",
            lead_name="Dana Miller",
            company_name="Northwind Traders",
            title="VP Operations",
            tone="direct",
            max_words=100,
        )
        content = await adapters.content.draft(brief)
        assert "Dana Miller" in content.body
        assert "Northwind Traders" in content.body
        assert content.word_count <= 100
        assert len(content.content_hash) == 64
        # Verify no un-rendered curly braces
        assert "{" not in content.body and "}" not in content.body
        assert "{" not in content.subject and "}" not in content.subject

    async def test_draft_warm_tone(self, adapters: Adapters) -> None:
        brief = OutreachBrief(
            lead_id="L-104",
            lead_name="Dana Miller",
            company_name="Northwind Traders",
            title="VP Operations",
            tone="warm",
            company_summary="enterprise logistics software",
        )
        content = await adapters.content.draft(brief)
        assert "Dana Miller" in content.body
        assert "logistics" in content.body

    async def test_draft_formal_tone(self, adapters: Adapters) -> None:
        brief = OutreachBrief(
            lead_id="L-104",
            lead_name="Dana Miller",
            company_name="Northwind Traders",
            tone="formal",
        )
        content = await adapters.content.draft(brief)
        assert "Dear Dana Miller" in content.body
        assert "Sincerely" in content.body


# ---------------------------------------------------------------------------
# 9. Failure Injection Tests
# ---------------------------------------------------------------------------
class TestFailureInjection:
    async def test_forced_failure_raises_transient_error(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: FixedClock,
        id_gen: SequentialIdGenerator,
        random_gen: DeterministicRandom,
    ) -> None:
        flaky_adapters = build_mock_adapters(
            session_factory, clock, id_gen, random_gen, failure_rate=1.0
        )
        with pytest.raises(TransientToolError) as exc_info:
            await flaky_adapters.leads.search(LeadFilter(industry="Software"))
        assert "Injected upstream transient failure" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 10. Adapter Factory Tests
# ---------------------------------------------------------------------------
class TestBuildAdapters:
    def test_mock_mode_builds_adapters(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: FixedClock,
        id_gen: SequentialIdGenerator,
        random_gen: DeterministicRandom,
    ) -> None:
        settings = Settings(
            _env_file=None,
            OPSPILOT_DATABASE_URL=get_settings().database_url,
            OPSPILOT_INTEGRATIONS=IntegrationMode.MOCK,
        )
        bundle = build_adapters(settings, session_factory, clock, id_gen, random_gen)
        assert isinstance(bundle, Adapters)

    def test_real_mode_raises_configuration_error(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: FixedClock,
        id_gen: SequentialIdGenerator,
        random_gen: DeterministicRandom,
    ) -> None:
        settings = Settings(
            _env_file=None,
            OPSPILOT_DATABASE_URL=get_settings().database_url,
            OPSPILOT_INTEGRATIONS=IntegrationMode.REAL,
        )
        with pytest.raises(ConfigurationError):
            build_adapters(settings, session_factory, clock, id_gen, random_gen)
