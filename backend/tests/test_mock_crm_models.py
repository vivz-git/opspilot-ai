"""Mock CRM persistence models against a real database (§12.9, DB-004).

Requires Postgres, like `test_persistence_models.py` and `test_evaluation_models.py`
— skips cleanly with no reachable database, migrated to head via the same ad
hoc probe.

Each test runs inside a savepoint nested in one outer transaction so a
`pytest.raises(IntegrityError)` does not poison the rest of the test and
nothing written here survives past it.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from app.config import get_settings
from app.persistence.mock_crm import (
    Company,
    Customer,
    CustomerStatus,
    EmailOutbox,
    EmailOutboxStatus,
    Lead,
    LeadStatus,
    OutreachDraft,
    OutreachDraftStatus,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _sync_engine(**kwargs: object) -> sa.Engine:
    return sa.create_engine(_sync_url(get_settings().database_url.get_secret_value()), **kwargs)


def _require_database() -> None:
    try:
        with sa.create_engine(
            _sync_url(get_settings().database_url.get_secret_value()),
            connect_args={"connect_timeout": 3},
        ).connect():
            pass
    except OperationalError:
        pytest.skip("no reachable Postgres for this session (DB-004 integration test)")


@pytest.fixture(scope="module", autouse=True)
def _migrated_database() -> None:
    _require_database()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url.get_secret_value())
    command.upgrade(config, "head")


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = _sync_engine()
    connection = engine.connect()
    outer_transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer_transaction.rollback()
        connection.close()
        engine.dispose()


def _make_company(**overrides: object) -> Company:
    defaults: dict[str, object] = {
        "company_id": "comp_acme",
        "name": "Acme Corp",
        "domain": "acme.example",
    }
    defaults.update(overrides)
    return Company(**defaults)  # type: ignore[arg-type]


def _make_lead(company_id: str, **overrides: object) -> Lead:
    defaults: dict[str, object] = {
        "lead_id": "lead_john",
        "company_id": company_id,
        "full_name": "John Doe",
        "email": "john@acme.example",
    }
    defaults.update(overrides)
    return Lead(**defaults)  # type: ignore[arg-type]


def _make_customer(**overrides: object) -> Customer:
    defaults: dict[str, object] = {
        "customer_id": "cust_1",
        "account_name": "Acme Corp",
        "primary_contact": "Jane Doe",
        "email": "jane@acme.example",
    }
    defaults.update(overrides)
    return Customer(**defaults)  # type: ignore[arg-type]


def _make_draft(lead_id: str, **overrides: object) -> OutreachDraft:
    defaults: dict[str, object] = {
        "draft_id": "draft_1",
        "lead_id": lead_id,
        "subject": "Quick intro",
        "body": "Hi John, saw your recent funding round.",
        "content_hash": "a" * 64,
    }
    defaults.update(overrides)
    return OutreachDraft(**defaults)  # type: ignore[arg-type]


def _make_outbox(draft_id: str, **overrides: object) -> EmailOutbox:
    defaults: dict[str, object] = {
        "outbox_id": "outbox_1",
        "message_id": "msg_1",
        "draft_id": draft_id,
        "to_email": "john@acme.example",
        "subject": "Quick intro",
        "body": "Hi John, saw your recent funding round.",
        "idempotency_key": "idem_key_1",
    }
    defaults.update(overrides)
    return EmailOutbox(**defaults)  # type: ignore[arg-type]


class TestCompanyModel:
    def test_minimal_company_can_be_created(self, db_session: Session) -> None:
        company = _make_company()
        db_session.add(company)
        db_session.flush()

        assert company.company_id == "comp_acme"
        assert company.name == "Acme Corp"
        assert company.domain == "acme.example"
        assert company.industry is None
        assert company.employee_count is None
        assert company.tech_stack == []
        assert company.signals == []
        assert company.created_at is not None
        assert company.updated_at is not None

    def test_fully_populated_company(self, db_session: Session) -> None:
        company = _make_company(
            company_id="comp_full",
            name="Full Corp",
            domain="full.example",
            industry="Software",
            employee_count=250,
            revenue_band="$10M-$50M",
            hq_location="San Francisco, CA",
            funding_stage="Series B",
            tech_stack=["python", "fastapi", "react"],
            signals=[{"kind": "hiring", "summary": "Hiring 5 engineers"}],
        )
        db_session.add(company)
        db_session.flush()

        assert company.industry == "Software"
        assert company.employee_count == 250
        assert company.tech_stack == ["python", "fastapi", "react"]
        assert len(company.signals) == 1

    def test_duplicate_company_domain_is_rejected(self, db_session: Session) -> None:
        c1 = _make_company(company_id="comp_1", domain="unique.example")
        c2 = _make_company(company_id="comp_2", domain="unique.example")
        db_session.add(c1)
        db_session.flush()

        db_session.add(c2)
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestLeadModel:
    def test_minimal_lead_can_be_created(self, db_session: Session) -> None:
        company = _make_company(company_id="comp_lead_test", domain="leadtest.example")
        db_session.add(company)
        db_session.flush()

        lead = _make_lead(company_id=company.company_id)
        db_session.add(lead)
        db_session.flush()

        assert lead.lead_id == "lead_john"
        assert lead.company_id == "comp_lead_test"
        assert lead.full_name == "John Doe"
        assert lead.email == "john@acme.example"
        assert lead.status == LeadStatus.NEW
        assert lead.tags == []
        assert lead.created_at is not None
        assert lead.updated_at is not None

    def test_fully_populated_lead(self, db_session: Session) -> None:
        company = _make_company(company_id="comp_full_lead", domain="fulllead.example")
        db_session.add(company)
        db_session.flush()

        lead = _make_lead(
            company_id=company.company_id,
            lead_id="lead_full",
            title="VP of Engineering",
            source="outbound_scan",
            owner="alice",
            phone="+1-555-0199",
            timezone="America/New_York",
            tags=["vip", "decision_maker"],
            notes="Met at conference",
        )
        db_session.add(lead)
        db_session.flush()

        assert lead.title == "VP of Engineering"
        assert lead.tags == ["vip", "decision_maker"]
        assert lead.owner == "alice"

    def test_lead_company_relationship(self, db_session: Session) -> None:
        company = _make_company(company_id="comp_rel", domain="rel.example")
        db_session.add(company)
        db_session.flush()

        lead = _make_lead(company_id=company.company_id, lead_id="lead_rel")
        db_session.add(lead)
        db_session.flush()

        assert lead.company.name == "Acme Corp"
        assert len(company.leads) == 1
        assert company.leads[0].lead_id == "lead_rel"

    def test_invalid_company_id_is_rejected(self, db_session: Session) -> None:
        lead = _make_lead(company_id="comp_nonexistent", lead_id="lead_bad_fk")
        db_session.add(lead)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_invalid_lead_status_enum_is_rejected(self, db_session: Session) -> None:
        company = _make_company(company_id="comp_status_test", domain="statustest.example")
        db_session.add(company)
        db_session.flush()

        stmt = sa.text(
            "INSERT INTO mock_crm.leads (lead_id, company_id, full_name, email, status) "
            "VALUES ('lead_bad_status', 'comp_status_test', 'Bad Lead', 'bad@example.com', 'bogus')"
        )
        with pytest.raises(IntegrityError):
            db_session.execute(stmt)
            db_session.flush()


class TestCustomerModel:
    def test_minimal_customer_can_be_created(self, db_session: Session) -> None:
        cust = _make_customer()
        db_session.add(cust)
        db_session.flush()

        assert cust.customer_id == "cust_1"
        assert cust.account_name == "Acme Corp"
        assert cust.primary_contact == "Jane Doe"
        assert cust.email == "jane@acme.example"
        assert cust.status == CustomerStatus.PROSPECT
        assert cust.version == 1
        assert cust.created_at is not None
        assert cust.updated_at is not None

    def test_customer_version_defaults_to_1(self, db_session: Session) -> None:
        """Architecture requires customers.version int not null default 1."""
        cust = _make_customer(customer_id="cust_version_test", email="ver@example.com")
        db_session.add(cust)
        db_session.flush()

        assert cust.version == 1

    def test_customer_version_increments_for_optimistic_concurrency(
        self, db_session: Session
    ) -> None:
        """The version column is the optimistic concurrency token for update_customer."""
        cust = _make_customer(customer_id="cust_occ", email="occ@example.com")
        db_session.add(cust)
        db_session.flush()

        assert cust.version == 1
        cust.version = 2
        cust.plan = "enterprise"
        db_session.flush()

        reloaded = db_session.get(Customer, "cust_occ")
        assert reloaded is not None
        assert reloaded.version == 2
        assert reloaded.plan == "enterprise"

    def test_fully_populated_customer(self, db_session: Session) -> None:
        cust = _make_customer(
            customer_id="cust_full",
            email="full@cust.example",
            phone="+1-555-0142",
            status=CustomerStatus.ACTIVE,
            plan="growth",
            mrr=Decimal("1250.00"),
            owner="carol",
            notes="Onboarded via self-serve",
        )
        db_session.add(cust)
        db_session.flush()

        assert cust.status == CustomerStatus.ACTIVE
        assert cust.plan == "growth"
        assert cust.mrr == Decimal("1250.00")
        assert cust.owner == "carol"
        assert cust.notes == "Onboarded via self-serve"

    def test_duplicate_customer_email_is_rejected(self, db_session: Session) -> None:
        c1 = _make_customer(customer_id="cust_dup1", email="dup@example.com")
        c2 = _make_customer(customer_id="cust_dup2", email="dup@example.com")
        db_session.add(c1)
        db_session.flush()

        db_session.add(c2)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_invalid_customer_status_enum_is_rejected(self, db_session: Session) -> None:
        stmt = sa.text(
            "INSERT INTO mock_crm.customers "
            "(customer_id, account_name, primary_contact, email, status) "
            "VALUES ('cust_bad_status', 'Bad Account', 'Bad Contact', 'bad@ex.com', 'bogus')"
        )
        with pytest.raises(IntegrityError):
            db_session.execute(stmt)
            db_session.flush()


class TestOutreachDraftModel:
    def test_minimal_draft_can_be_created(self, db_session: Session) -> None:
        company = _make_company(company_id="comp_draft", domain="drafttest.example")
        db_session.add(company)
        db_session.flush()

        lead = _make_lead(company_id=company.company_id, lead_id="lead_draft")
        db_session.add(lead)
        db_session.flush()

        draft = _make_draft(lead_id=lead.lead_id)
        db_session.add(draft)
        db_session.flush()

        assert draft.draft_id == "draft_1"
        assert draft.lead_id == "lead_draft"
        assert draft.channel == "email"
        assert draft.subject == "Quick intro"
        assert draft.status == OutreachDraftStatus.SAVED
        assert draft.version == 1
        assert draft.metadata_ == {}
        assert draft.created_at is not None
        assert draft.updated_at is not None

    def test_draft_lead_relationship(self, db_session: Session) -> None:
        company = _make_company(company_id="comp_d_rel", domain="drel.example")
        db_session.add(company)
        db_session.flush()

        lead = _make_lead(company_id=company.company_id, lead_id="lead_d_rel")
        db_session.add(lead)
        db_session.flush()

        draft = _make_draft(lead_id=lead.lead_id, draft_id="draft_rel")
        db_session.add(draft)
        db_session.flush()

        assert draft.lead.full_name == "John Doe"
        assert len(lead.drafts) == 1
        assert lead.drafts[0].draft_id == "draft_rel"

    def test_invalid_lead_id_is_rejected(self, db_session: Session) -> None:
        draft = _make_draft(lead_id="lead_nonexistent", draft_id="draft_bad_fk")
        db_session.add(draft)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_invalid_draft_status_enum_is_rejected(self, db_session: Session) -> None:
        company = _make_company(company_id="comp_d_stat", domain="dstat.example")
        db_session.add(company)
        db_session.flush()

        lead = _make_lead(company_id=company.company_id, lead_id="lead_d_stat")
        db_session.add(lead)
        db_session.flush()

        stmt = sa.text(
            "INSERT INTO mock_crm.outreach_drafts "
            "(draft_id, lead_id, subject, body, content_hash, status) "
            "VALUES ('draft_bad_stat', 'lead_d_stat', 'Sub', 'Body', 'hash', 'bogus')"
        )
        with pytest.raises(IntegrityError):
            db_session.execute(stmt)
            db_session.flush()


class TestEmailOutboxModel:
    def test_minimal_outbox_can_be_created(self, db_session: Session) -> None:
        company = _make_company(company_id="comp_outbox", domain="outboxtest.example")
        db_session.add(company)
        db_session.flush()

        lead = _make_lead(company_id=company.company_id, lead_id="lead_outbox")
        db_session.add(lead)
        db_session.flush()

        draft = _make_draft(lead_id=lead.lead_id, draft_id="draft_outbox")
        db_session.add(draft)
        db_session.flush()

        outbox = _make_outbox(
            draft_id=draft.draft_id,
            run_id="run_123",
            approval_id="appr_456",
        )
        db_session.add(outbox)
        db_session.flush()

        assert outbox.outbox_id == "outbox_1"
        assert outbox.message_id == "msg_1"
        assert outbox.draft_id == "draft_outbox"
        assert outbox.to_email == "john@acme.example"
        assert outbox.status == EmailOutboxStatus.SENT
        assert outbox.provider == "mock"
        assert outbox.idempotency_key == "idem_key_1"
        assert outbox.run_id == "run_123"
        assert outbox.approval_id == "appr_456"
        assert outbox.created_at is not None

    def test_duplicate_idempotency_key_is_rejected(self, db_session: Session) -> None:
        """CRITICAL: email_outbox.idempotency_key must be enforced as a PostgreSQL
        uniqueness constraint so a retried send physically cannot produce a
        second message row."""
        company = _make_company(company_id="comp_idem", domain="idem.example")
        db_session.add(company)
        db_session.flush()

        lead = _make_lead(company_id=company.company_id, lead_id="lead_idem")
        db_session.add(lead)
        db_session.flush()

        draft = _make_draft(lead_id=lead.lead_id, draft_id="draft_idem")
        db_session.add(draft)
        db_session.flush()

        o1 = _make_outbox(
            draft_id=draft.draft_id,
            outbox_id="outbox_idem_1",
            message_id="msg_idem_1",
            idempotency_key="shared_idem_key",
        )
        o2 = _make_outbox(
            draft_id=draft.draft_id,
            outbox_id="outbox_idem_2",
            message_id="msg_idem_2",
            idempotency_key="shared_idem_key",
        )
        db_session.add(o1)
        db_session.flush()

        db_session.add(o2)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_duplicate_message_id_is_rejected(self, db_session: Session) -> None:
        company = _make_company(company_id="comp_msg", domain="msg.example")
        db_session.add(company)
        db_session.flush()

        lead = _make_lead(company_id=company.company_id, lead_id="lead_msg")
        db_session.add(lead)
        db_session.flush()

        draft = _make_draft(lead_id=lead.lead_id, draft_id="draft_msg")
        db_session.add(draft)
        db_session.flush()

        o1 = _make_outbox(
            draft_id=draft.draft_id,
            outbox_id="outbox_msg_1",
            message_id="same_msg_id",
            idempotency_key="key_a",
        )
        o2 = _make_outbox(
            draft_id=draft.draft_id,
            outbox_id="outbox_msg_2",
            message_id="same_msg_id",
            idempotency_key="key_b",
        )
        db_session.add(o1)
        db_session.flush()

        db_session.add(o2)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_invalid_draft_id_is_rejected(self, db_session: Session) -> None:
        outbox = _make_outbox(
            draft_id="draft_nonexistent",
            outbox_id="outbox_bad_fk",
            idempotency_key="key_bad_fk",
        )
        db_session.add(outbox)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_outbox_draft_relationship(self, db_session: Session) -> None:
        company = _make_company(company_id="comp_ob_rel", domain="obrel.example")
        db_session.add(company)
        db_session.flush()

        lead = _make_lead(company_id=company.company_id, lead_id="lead_ob_rel")
        db_session.add(lead)
        db_session.flush()

        draft = _make_draft(lead_id=lead.lead_id, draft_id="draft_ob_rel")
        db_session.add(draft)
        db_session.flush()

        outbox = _make_outbox(
            draft_id=draft.draft_id,
            outbox_id="outbox_ob_rel",
            idempotency_key="key_ob_rel",
        )
        db_session.add(outbox)
        db_session.flush()

        assert outbox.draft.subject == "Quick intro"
        assert len(draft.outbox_entries) == 1
        assert draft.outbox_entries[0].outbox_id == "outbox_ob_rel"

    def test_invalid_outbox_status_enum_is_rejected(self, db_session: Session) -> None:
        company = _make_company(company_id="comp_ob_st", domain="obst.example")
        db_session.add(company)
        db_session.flush()

        lead = _make_lead(company_id=company.company_id, lead_id="lead_ob_st")
        db_session.add(lead)
        db_session.flush()

        draft = _make_draft(lead_id=lead.lead_id, draft_id="draft_ob_st")
        db_session.add(draft)
        db_session.flush()

        stmt = sa.text(
            "INSERT INTO mock_crm.email_outbox "
            "(outbox_id, message_id, draft_id, to_email, subject, body, idempotency_key, status) "
            "VALUES ('outbox_bad_st', 'msg_bad_st', 'draft_ob_st', "
            "'to@example.com', 'Sub', 'Body', 'key_bad_st', 'bogus')"
        )
        with pytest.raises(IntegrityError):
            db_session.execute(stmt)
            db_session.flush()


class TestCascadeAndRestrictBehavior:
    def test_deleting_company_with_referencing_leads_is_restricted(
        self, db_session: Session
    ) -> None:
        """Deleting a company that has leads must be rejected by Postgres."""
        company = _make_company(company_id="comp_restrict", domain="restrict.example")
        db_session.add(company)
        db_session.flush()

        lead = _make_lead(company_id=company.company_id, lead_id="lead_restrict")
        db_session.add(lead)
        db_session.flush()

        with pytest.raises(IntegrityError):
            db_session.execute(
                sa.text("DELETE FROM mock_crm.companies WHERE company_id = 'comp_restrict'")
            )

    def test_deleting_lead_with_referencing_drafts_is_restricted(self, db_session: Session) -> None:
        """Deleting a lead that has drafts must be rejected by Postgres."""
        company = _make_company(company_id="comp_lead_res", domain="leadres.example")
        db_session.add(company)
        db_session.flush()

        lead = _make_lead(company_id=company.company_id, lead_id="lead_res")
        db_session.add(lead)
        db_session.flush()

        draft = _make_draft(lead_id=lead.lead_id, draft_id="draft_res")
        db_session.add(draft)
        db_session.flush()

        with pytest.raises(IntegrityError):
            db_session.execute(sa.text("DELETE FROM mock_crm.leads WHERE lead_id = 'lead_res'"))

    def test_deleting_draft_with_referencing_outbox_is_restricted(
        self, db_session: Session
    ) -> None:
        """Deleting a draft that has outbox entries must be rejected by Postgres."""
        company = _make_company(company_id="comp_draft_res", domain="draftres.example")
        db_session.add(company)
        db_session.flush()

        lead = _make_lead(company_id=company.company_id, lead_id="lead_dres")
        db_session.add(lead)
        db_session.flush()

        draft = _make_draft(lead_id=lead.lead_id, draft_id="draft_dres")
        db_session.add(draft)
        db_session.flush()

        outbox = _make_outbox(
            draft_id=draft.draft_id,
            outbox_id="outbox_dres",
            idempotency_key="key_dres",
        )
        db_session.add(outbox)
        db_session.flush()

        with pytest.raises(IntegrityError):
            db_session.execute(
                sa.text("DELETE FROM mock_crm.outreach_drafts WHERE draft_id = 'draft_dres'")
            )


class TestMockCrmIndexesAndConstraints:
    def test_all_mock_crm_indexes_exist(self, db_session: Session) -> None:
        rows = db_session.execute(
            sa.text("SELECT indexname FROM pg_indexes WHERE schemaname = 'mock_crm'")
        ).fetchall()
        index_names = {r[0] for r in rows}

        expected_indexes = {
            "ix_companies_industry",
            "ix_companies_name",
            "ix_leads_company_id",
            "ix_leads_email",
            "ix_leads_status",
            "ix_customers_email",
            "ix_customers_status",
            "ix_outreach_drafts_lead_id",
            "ix_outreach_drafts_status",
            "ix_email_outbox_draft_id",
            "ix_email_outbox_status",
            "ix_email_outbox_run_id",
            "ix_email_outbox_approval_id",
        }
        assert expected_indexes <= index_names

    def test_all_mock_crm_unique_constraints_exist(self, db_session: Session) -> None:
        rows = db_session.execute(
            sa.text(
                """
                SELECT conname FROM pg_constraint
                JOIN pg_namespace ON pg_constraint.connamespace = pg_namespace.oid
                WHERE pg_namespace.nspname = 'mock_crm' AND contype = 'u'
                """
            )
        ).fetchall()
        constraint_names = {r[0] for r in rows}

        expected_unique = {
            "uq_companies_domain",
            "uq_customers_email",
            "uq_email_outbox_idempotency_key",
            "uq_email_outbox_message_id",
        }
        assert expected_unique <= constraint_names

    def test_all_mock_crm_foreign_keys_exist(self, db_session: Session) -> None:
        rows = db_session.execute(
            sa.text(
                """
                SELECT conname FROM pg_constraint
                JOIN pg_namespace ON pg_constraint.connamespace = pg_namespace.oid
                WHERE pg_namespace.nspname = 'mock_crm' AND contype = 'f'
                """
            )
        ).fetchall()
        fk_names = {r[0] for r in rows}

        expected_fks = {
            "fk_leads_company_id_companies",
            "fk_outreach_drafts_lead_id_leads",
            "fk_email_outbox_draft_id_outreach_drafts",
        }
        assert expected_fks <= fk_names
