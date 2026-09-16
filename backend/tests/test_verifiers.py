"""Unit tests for the verification framework and verifiers (§11, VERIFY-001, VERIFY-002)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.agent.state import VerificationStatus
from app.agent.verifiers.base import VerificationContext, Verifier
from app.agent.verifiers.invariants import (
    DraftOutreachVerifier,
    ResearchCompanyVerifier,
    ScoreLeadVerifier,
    SearchLeadsVerifier,
)
from app.agent.verifiers.readback import (
    SaveDraftVerifier,
    SendEmailMockVerifier,
    UpdateCustomerVerifier,
)
from app.agent.verifiers.registry import NullVerifier, VerifierRegistry, default_verifiers
from app.errors import ConfigurationError, NotFoundError, TransientToolError
from app.integrations.ports import (
    Adapters,
    Customer,
    CustomerPatch,
    CustomerPort,
    DraftPort,
    DraftRecord,
    MailPort,
    OutboxRecord,
)
from app.runtime import SystemClock
from app.tools.contracts import REGISTRY, ToolName, VerificationMode

pytestmark = [pytest.mark.unit]

NOW = datetime.now(UTC)


def make_ctx(
    tool: ToolName,
    *,
    input_args: dict[str, Any] | None = None,
    output_data: dict[str, Any] | None = None,
    step_id: str = "s1",
    attempt: int = 1,
    idempotency_key: str | None = "idem_key_123",
    adapters: Adapters | None = None,
    baseline_customer: Customer | None = None,
) -> VerificationContext:
    contract = REGISTRY[tool]
    return VerificationContext(
        run_id=uuid.uuid4(),
        step_id=step_id,
        tool=tool,
        attempt=attempt,
        contract=contract,
        input_args=input_args or {},
        output_data=output_data or {},
        idempotency_key=idempotency_key,
        adapters=adapters,
        clock=SystemClock(),
        baseline_customer=baseline_customer,
    )


# ---------------------------------------------------------------------------
# 1. Framework & Registry Tests
# ---------------------------------------------------------------------------
class TestVerificationFrameworkAndRegistry:
    def test_verifiers_conform_to_protocol(self) -> None:
        for tool, verifier in default_verifiers().items():
            assert isinstance(verifier, Verifier), f"{tool} verifier does not implement Verifier"

    def test_registry_resolves_all_tools(self) -> None:
        registry = VerifierRegistry()
        for contract in REGISTRY.values():
            verifier = registry.get_verifier(contract)
            assert verifier is not None
            if contract.verification == VerificationMode.NONE:
                assert isinstance(verifier, NullVerifier)

    def test_registry_raises_for_unregistered_non_none_contract(self) -> None:
        empty_registry = VerifierRegistry(verifiers={})
        mutating_contract = REGISTRY[ToolName.SEND_EMAIL_MOCK]
        with pytest.raises(ConfigurationError):
            empty_registry.get_verifier(mutating_contract)

    @pytest.mark.asyncio
    async def test_null_verifier_records_not_required(self) -> None:
        verifier = NullVerifier()
        ctx = make_ctx(ToolName.GET_LEAD, input_args={"lead_id": "l_1"})
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.NOT_REQUIRED
        assert res.mode == "none"
        assert res.checks == []


# ---------------------------------------------------------------------------
# 2. Invariant Verifiers Tests (§8.4)
# ---------------------------------------------------------------------------
class TestSearchLeadsVerifier:
    @pytest.mark.asyncio
    async def test_valid_search_results_pass(self) -> None:
        verifier = SearchLeadsVerifier()
        ctx = make_ctx(
            ToolName.SEARCH_LEADS,
            input_args={"status": "new", "limit": 10},
            output_data={
                "leads": [
                    {"lead_id": "l_1", "status": "new", "company_id": "c_1"},
                    {"lead_id": "l_2", "status": "new", "company_id": "c_2"},
                ],
                "total_matched": 5,
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.PASSED
        assert all(c.passed for c in res.checks)

    @pytest.mark.asyncio
    async def test_lead_count_exceeding_limit_fails(self) -> None:
        verifier = SearchLeadsVerifier()
        ctx = make_ctx(
            ToolName.SEARCH_LEADS,
            input_args={"limit": 2},
            output_data={
                "leads": [{"lead_id": f"l_{i}"} for i in range(5)],
                "total_matched": 5,
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        failed = next(c for c in res.checks if not c.passed)
        assert failed.name == "count_within_limit"

    @pytest.mark.asyncio
    async def test_total_matched_less_than_count_fails(self) -> None:
        verifier = SearchLeadsVerifier()
        ctx = make_ctx(
            ToolName.SEARCH_LEADS,
            input_args={"limit": 10},
            output_data={
                "leads": [{"lead_id": "l_1"}, {"lead_id": "l_2"}],
                "total_matched": 1,  # Inconsistent with 2 returned leads
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        failed = next(c for c in res.checks if not c.passed)
        assert failed.name == "total_matched_valid"

    @pytest.mark.asyncio
    async def test_negative_total_matched_fails(self) -> None:
        verifier = SearchLeadsVerifier()
        ctx = make_ctx(
            ToolName.SEARCH_LEADS,
            input_args={"limit": 10},
            output_data={
                "leads": [],
                "total_matched": -1,
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED

    @pytest.mark.asyncio
    async def test_duplicate_lead_ids_fail_verification(self) -> None:
        verifier = SearchLeadsVerifier()
        ctx = make_ctx(
            ToolName.SEARCH_LEADS,
            input_args={"limit": 10},
            output_data={
                "leads": [
                    {"lead_id": "l_duplicate", "status": "new"},
                    {"lead_id": "l_duplicate", "status": "new"},
                ],
                "total_matched": 2,
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        failed = next(c for c in res.checks if not c.passed)
        assert failed.name == "lead_ids_unique"

    @pytest.mark.asyncio
    async def test_filter_mismatch_fails(self) -> None:
        verifier = SearchLeadsVerifier()
        ctx = make_ctx(
            ToolName.SEARCH_LEADS,
            input_args={"status": "qualified"},
            output_data={
                "leads": [
                    {"lead_id": "l_1", "status": "qualified"},
                    {"lead_id": "l_2", "status": "new"},  # Mismatch!
                ],
                "total_matched": 2,
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        failed = next(c for c in res.checks if not c.passed)
        assert failed.name == "filter_compliance"


class TestResearchCompanyVerifier:
    @pytest.mark.asyncio
    async def test_valid_profile_passes(self) -> None:
        verifier = ResearchCompanyVerifier()
        ctx = make_ctx(
            ToolName.RESEARCH_COMPANY,
            input_args={"company_id": "comp_1"},
            output_data={
                "profile": {
                    "company_id": "comp_1",
                    "domain": "comp1.com",
                    "confidence": 0.85,
                    "summary": "Fast-growing fintech startup.",
                    "tech_stack": ["Python", "PostgreSQL"],
                    "recent_signals": ["Hiring VP Sales"],
                    "sources": ["LinkedIn"],
                }
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.PASSED

    @pytest.mark.asyncio
    async def test_confidence_out_of_bounds_fails(self) -> None:
        verifier = ResearchCompanyVerifier()
        ctx = make_ctx(
            ToolName.RESEARCH_COMPANY,
            output_data={"profile": {"confidence": 1.25, "summary": "Valid summary."}},
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "confidence_in_bounds" and not c.passed for c in res.checks)

    @pytest.mark.asyncio
    async def test_empty_summary_fails(self) -> None:
        verifier = ResearchCompanyVerifier()
        ctx = make_ctx(
            ToolName.RESEARCH_COMPANY,
            output_data={"profile": {"confidence": 0.5, "summary": "   "}},
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "summary_non_empty" and not c.passed for c in res.checks)

    @pytest.mark.asyncio
    async def test_company_id_mismatch_fails(self) -> None:
        verifier = ResearchCompanyVerifier()
        ctx = make_ctx(
            ToolName.RESEARCH_COMPANY,
            input_args={"company_id": "expected_comp"},
            output_data={
                "profile": {
                    "company_id": "different_comp",
                    "confidence": 0.7,
                    "summary": "Valid summary.",
                }
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "company_id_matches_intent" and not c.passed for c in res.checks)

    @pytest.mark.asyncio
    async def test_domain_matches_intent_passes_and_mismatch_fails(self) -> None:
        verifier = ResearchCompanyVerifier()
        # 1. Matching domain
        ctx_ok = make_ctx(
            ToolName.RESEARCH_COMPANY,
            input_args={"domain": "acme.corp"},
            output_data={
                "profile": {
                    "domain": "ACME.CORP",
                    "confidence": 0.9,
                    "summary": "Global logistics leader.",
                }
            },
        )
        res_ok = await verifier.verify(ctx_ok)
        assert res_ok.status == VerificationStatus.PASSED

        # 2. Mismatched domain
        ctx_bad = make_ctx(
            ToolName.RESEARCH_COMPANY,
            input_args={"domain": "acme.corp"},
            output_data={
                "profile": {
                    "domain": "other.corp",
                    "confidence": 0.9,
                    "summary": "Global logistics leader.",
                }
            },
        )
        res_bad = await verifier.verify(ctx_bad)
        assert res_bad.status == VerificationStatus.FAILED
        assert any(c.name == "domain_matches_intent" and not c.passed for c in res_bad.checks)


class TestScoreLeadVerifier:
    @pytest.mark.asyncio
    async def test_valid_scoring_passes(self) -> None:
        verifier = ScoreLeadVerifier()
        ctx = make_ctx(
            ToolName.SCORE_LEAD,
            input_args={"lead_id": "l_1"},
            output_data={
                "lead_id": "l_1",
                "score": 85,
                "band": "hot",
                "factors": [
                    {"name": "company_fit", "contribution": 40.0},
                    {"name": "engagement", "contribution": 20.0},
                    {"name": "signal_strength", "contribution": 15.0},
                    {"name": "data_quality", "contribution": 10.0},
                ],
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.PASSED

    @pytest.mark.asyncio
    async def test_score_between_75_and_79_is_hot_passes(self) -> None:
        """VERIFY-002 Bug Fix: score_lead rule engine assigns HOT at >= 75 (not 80)."""
        verifier = ScoreLeadVerifier()
        ctx = make_ctx(
            ToolName.SCORE_LEAD,
            input_args={"lead_id": "l_76"},
            output_data={
                "lead_id": "l_76",
                "score": 76,
                "band": "hot",
                "factors": [
                    {"name": "company_fit", "contribution": 35.0},
                    {"name": "engagement", "contribution": 20.0},
                    {"name": "signal_strength", "contribution": 11.0},
                    {"name": "data_quality", "contribution": 10.0},
                ],
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.PASSED

    @pytest.mark.asyncio
    async def test_score_between_50_and_74_is_warm_passes(self) -> None:
        verifier = ScoreLeadVerifier()
        ctx = make_ctx(
            ToolName.SCORE_LEAD,
            input_args={"lead_id": "l_60"},
            output_data={
                "lead_id": "l_60",
                "score": 60,
                "band": "warm",
                "factors": [
                    {"name": "company_fit", "contribution": 30.0},
                    {"name": "engagement", "contribution": 10.0},
                    {"name": "signal_strength", "contribution": 10.0},
                    {"name": "data_quality", "contribution": 10.0},
                ],
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.PASSED

    @pytest.mark.asyncio
    async def test_score_factor_sum_mismatch_fails(self) -> None:
        verifier = ScoreLeadVerifier()
        ctx = make_ctx(
            ToolName.SCORE_LEAD,
            output_data={
                "score": 90,
                "band": "hot",
                "factors": [
                    {"name": "company_fit", "contribution": 40.0},
                    {"name": "engagement", "contribution": 20.0},
                    {"name": "signal_strength", "contribution": 10.0},
                    {"name": "data_quality", "contribution": 10.0},  # sum is 80, not 90
                ],
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "factor_sum_consistent" and not c.passed for c in res.checks)

    @pytest.mark.asyncio
    async def test_missing_required_factors_fails(self) -> None:
        verifier = ScoreLeadVerifier()
        ctx = make_ctx(
            ToolName.SCORE_LEAD,
            output_data={
                "score": 60,
                "band": "warm",
                "factors": [
                    {"name": "company_fit", "contribution": 60.0},
                    # missing engagement, signal_strength, data_quality
                ],
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "required_factors_present" and not c.passed for c in res.checks)

    @pytest.mark.asyncio
    async def test_band_inconsistent_with_score_fails(self) -> None:
        verifier = ScoreLeadVerifier()
        ctx = make_ctx(
            ToolName.SCORE_LEAD,
            output_data={
                "score": 35,
                "band": "hot",  # 35 must be "cold"
                "factors": [
                    {"name": "company_fit", "contribution": 20.0},
                    {"name": "engagement", "contribution": 5.0},
                    {"name": "signal_strength", "contribution": 5.0},
                    {"name": "data_quality", "contribution": 5.0},
                ],
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "band_consistent" and not c.passed for c in res.checks)


class TestDraftOutreachVerifier:
    @pytest.mark.asyncio
    async def test_clean_draft_passes(self) -> None:
        verifier = DraftOutreachVerifier()
        subject = "Quick question about your ops"
        body = "Hi Alex, noticed OpsPilot could optimize your workflows."
        expected_hash = hashlib.sha256(f"{subject}\n\n{body}".encode()).hexdigest()

        ctx = make_ctx(
            ToolName.DRAFT_OUTREACH,
            input_args={"max_words": 100},
            output_data={
                "subject": subject,
                "body": body,
                "word_count": len(body.split()),
                "content_hash": expected_hash,
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.PASSED

    @pytest.mark.asyncio
    async def test_actual_body_words_exceeding_max_words_fails(self) -> None:
        """VERIFY-002: Catches when tool lies about word_count but body is oversized."""
        verifier = DraftOutreachVerifier()
        subject = "Hello"
        body = "word " * 150  # 150 words
        expected_hash = hashlib.sha256(f"{subject}\n\n{body}".encode()).hexdigest()

        ctx = make_ctx(
            ToolName.DRAFT_OUTREACH,
            input_args={"max_words": 50},
            output_data={
                "subject": subject,
                "body": body,
                "word_count": 30,  # Tool claims 30 words!
                "content_hash": expected_hash,
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "word_count_bounded" and not c.passed for c in res.checks)

    @pytest.mark.asyncio
    async def test_subject_too_long_fails(self) -> None:
        """VERIFY-002: Subject must not exceed 120 chars (§8.4)."""
        verifier = DraftOutreachVerifier()
        subject = "A" * 125
        body = "Short body."
        expected_hash = hashlib.sha256(f"{subject}\n\n{body}".encode()).hexdigest()

        ctx = make_ctx(
            ToolName.DRAFT_OUTREACH,
            output_data={
                "subject": subject,
                "body": body,
                "word_count": 2,
                "content_hash": expected_hash,
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "subject_length_bounded" and not c.passed for c in res.checks)

    @pytest.mark.asyncio
    async def test_unresolved_placeholders_fail(self) -> None:
        verifier = DraftOutreachVerifier()
        subject = "Hello {{first_name}}"
        body = "We noticed [COMPANY] needs help with <METRIC>. TODO: add details."
        expected_hash = hashlib.sha256(f"{subject}\n\n{body}".encode()).hexdigest()

        ctx = make_ctx(
            ToolName.DRAFT_OUTREACH,
            output_data={
                "subject": subject,
                "body": body,
                "word_count": 10,
                "content_hash": expected_hash,
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        failed = next(c for c in res.checks if not c.passed)
        assert failed.name == "no_unresolved_placeholders"

    @pytest.mark.asyncio
    async def test_content_hash_mismatch_fails(self) -> None:
        verifier = DraftOutreachVerifier()
        ctx = make_ctx(
            ToolName.DRAFT_OUTREACH,
            output_data={
                "subject": "Hello",
                "body": "World",
                "word_count": 2,
                "content_hash": "corrupted_hash_value",
            },
        )
        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "content_hash_integrity" and not c.passed for c in res.checks)


# ---------------------------------------------------------------------------
# 3. Readback Verifiers Tests (§8.4, §11.3)
# ---------------------------------------------------------------------------
class TestSendEmailMockVerifier:
    @pytest.mark.asyncio
    async def test_valid_outbox_readback_passes(self) -> None:
        verifier = SendEmailMockVerifier()
        mock_mail = AsyncMock(spec=MailPort)
        mock_mail.get_outbox.return_value = OutboxRecord(
            outbox_id="out_1",
            message_id="msg_1",
            draft_id="drf_100",
            to_email="lead@company.com",
            subject="Test Subject",
            body="Test Body",
            status="sent",
            provider="mock",
            idempotency_key="idem_1",
            sent_at=NOW,
        )
        mock_mail.count_outbox.return_value = 1
        adapters = AsyncMock(spec=Adapters)
        adapters.mail = mock_mail

        ctx = make_ctx(
            ToolName.SEND_EMAIL_MOCK,
            input_args={"to_email": "lead@company.com", "draft_id": "drf_100"},
            output_data={"message_id": "msg_1"},
            idempotency_key="idem_1",
            adapters=adapters,
        )

        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.PASSED
        mock_mail.get_outbox.assert_awaited_once_with("msg_1")
        mock_mail.count_outbox.assert_awaited_once_with("idem_1")

    @pytest.mark.asyncio
    async def test_duplicate_outbox_rows_fails_verification(self) -> None:
        """VERIFY-002: Assert exactly one row exists for the idempotency key."""
        verifier = SendEmailMockVerifier()
        mock_mail = AsyncMock(spec=MailPort)
        mock_mail.get_outbox.return_value = OutboxRecord(
            outbox_id="out_1",
            message_id="msg_1",
            draft_id="drf_100",
            to_email="lead@company.com",
            subject="Test Subject",
            body="Test Body",
            status="sent",
            provider="mock",
            idempotency_key="idem_1",
            sent_at=NOW,
        )
        # Duplicate row detected! (count == 2)
        mock_mail.count_outbox.return_value = 2
        adapters = AsyncMock(spec=Adapters)
        adapters.mail = mock_mail

        ctx = make_ctx(
            ToolName.SEND_EMAIL_MOCK,
            input_args={"to_email": "lead@company.com", "draft_id": "drf_100"},
            output_data={"message_id": "msg_1"},
            idempotency_key="idem_1",
            adapters=adapters,
        )

        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        failed = next(c for c in res.checks if not c.passed)
        assert failed.name == "idempotency_key_single_row"
        assert failed.observed == 2

    @pytest.mark.asyncio
    async def test_recipient_mismatch_against_requested_intent_fails(self) -> None:
        verifier = SendEmailMockVerifier()
        mock_mail = AsyncMock(spec=MailPort)
        mock_mail.get_outbox.return_value = OutboxRecord(
            outbox_id="out_1",
            message_id="msg_1",
            draft_id="drf_100",
            to_email="attacker@external.com",
            subject="Test Subject",
            body="Test Body",
            status="sent",
            provider="mock",
            idempotency_key="idem_1",
            sent_at=NOW,
        )
        mock_mail.count_outbox.return_value = 1
        adapters = AsyncMock(spec=Adapters)
        adapters.mail = mock_mail

        ctx = make_ctx(
            ToolName.SEND_EMAIL_MOCK,
            input_args={"to_email": "intended@company.com", "draft_id": "drf_100"},
            output_data={"message_id": "msg_1"},
            idempotency_key="idem_1",
            adapters=adapters,
        )

        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "recipient_matches_intent" and not c.passed for c in res.checks)

    @pytest.mark.asyncio
    async def test_missing_outbox_record_fails_verification(self) -> None:
        verifier = SendEmailMockVerifier()
        mock_mail = AsyncMock(spec=MailPort)
        mock_mail.get_outbox.side_effect = NotFoundError("Outbox entry not found: msg_1")
        adapters = AsyncMock(spec=Adapters)
        adapters.mail = mock_mail

        ctx = make_ctx(
            ToolName.SEND_EMAIL_MOCK,
            input_args={"to_email": "lead@company.com", "draft_id": "drf_100"},
            output_data={"message_id": "msg_1"},
            adapters=adapters,
        )

        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "outbox_record_exists" and not c.passed for c in res.checks)

    @pytest.mark.asyncio
    async def test_transient_port_error_propagates_as_transient(self) -> None:
        verifier = SendEmailMockVerifier()
        mock_mail = AsyncMock(spec=MailPort)
        mock_mail.get_outbox.side_effect = ConnectionError("DB connection failed")
        adapters = AsyncMock(spec=Adapters)
        adapters.mail = mock_mail

        ctx = make_ctx(
            ToolName.SEND_EMAIL_MOCK,
            output_data={"message_id": "msg_1"},
            adapters=adapters,
        )

        with pytest.raises(TransientToolError):
            await verifier.verify(ctx)


class TestUpdateCustomerVerifier:
    @pytest.mark.asyncio
    async def test_valid_customer_readback_passes(self) -> None:
        verifier = UpdateCustomerVerifier()
        mock_cust = AsyncMock(spec=CustomerPort)
        mock_cust.get.return_value = Customer(
            customer_id="cust_99",
            account_name="Acme Corp",
            primary_contact="Jane Doe",
            email="jane@acme.com",
            status="active",
            plan="enterprise",
            version=4,  # expected_version (3) + 1
            updated_at=NOW,
        )
        adapters = AsyncMock(spec=Adapters)
        adapters.customers = mock_cust

        ctx = make_ctx(
            ToolName.UPDATE_CUSTOMER,
            input_args={
                "customer_id": "cust_99",
                "expected_version": 3,
                "patch": CustomerPatch(plan="enterprise"),
            },
            output_data={"customer_id": "cust_99", "version": 4},
            adapters=adapters,
        )

        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.PASSED
        mock_cust.get.assert_awaited_once_with(customer_id="cust_99")

    @pytest.mark.asyncio
    async def test_untouched_field_tampered_fails_verification(self) -> None:
        """VERIFY-002: Assert no field outside the patch changed."""
        verifier = UpdateCustomerVerifier()
        mock_cust = AsyncMock(spec=CustomerPort)
        # Port returns customer where plan was updated, BUT account_name was tampered with!
        mock_cust.get.return_value = Customer(
            customer_id="cust_99",
            account_name="Compromised Corp",  # Tampered! Was "Acme Corp"
            primary_contact="Jane Doe",
            email="jane@acme.com",
            status="active",
            plan="enterprise",
            version=4,
            updated_at=NOW,
        )
        adapters = AsyncMock(spec=Adapters)
        adapters.customers = mock_cust

        baseline = Customer(
            customer_id="cust_99",
            account_name="Acme Corp",
            primary_contact="Jane Doe",
            email="jane@acme.com",
            status="active",
            plan="starter",
            version=3,
            updated_at=NOW,
        )

        ctx = make_ctx(
            ToolName.UPDATE_CUSTOMER,
            input_args={
                "customer_id": "cust_99",
                "expected_version": 3,
                "patch": CustomerPatch(plan="enterprise"),
            },
            output_data={"customer_id": "cust_99", "version": 4},
            adapters=adapters,
            baseline_customer=baseline,
        )

        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        failed = next(c for c in res.checks if not c.passed)
        assert failed.name == "untouched_fields_intact"
        assert any("account_name" in str(m) for m in failed.observed)

    @pytest.mark.asyncio
    async def test_untouched_field_intact_with_baseline_passes(self) -> None:
        """VERIFY-002: Confirms untouched fields match baseline perfectly."""
        verifier = UpdateCustomerVerifier()
        mock_cust = AsyncMock(spec=CustomerPort)
        mock_cust.get.return_value = Customer(
            customer_id="cust_99",
            account_name="Acme Corp",
            primary_contact="Jane Doe",
            email="jane@acme.com",
            status="active",
            plan="enterprise",
            version=4,
            updated_at=NOW,
        )
        adapters = AsyncMock(spec=Adapters)
        adapters.customers = mock_cust

        baseline = Customer(
            customer_id="cust_99",
            account_name="Acme Corp",
            primary_contact="Jane Doe",
            email="jane@acme.com",
            status="active",
            plan="starter",
            version=3,
            updated_at=NOW,
        )

        ctx = make_ctx(
            ToolName.UPDATE_CUSTOMER,
            input_args={
                "customer_id": "cust_99",
                "expected_version": 3,
                "patch": CustomerPatch(plan="enterprise"),
            },
            output_data={"customer_id": "cust_99", "version": 4},
            adapters=adapters,
            baseline_customer=baseline,
        )

        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.PASSED
        assert any(c.name == "untouched_fields_intact" and c.passed for c in res.checks)

    @pytest.mark.asyncio
    async def test_version_not_incremented_fails(self) -> None:
        verifier = UpdateCustomerVerifier()
        mock_cust = AsyncMock(spec=CustomerPort)
        mock_cust.get.return_value = Customer(
            customer_id="cust_99",
            account_name="Acme Corp",
            primary_contact="Jane Doe",
            email="jane@acme.com",
            status="active",
            plan="enterprise",
            version=3,  # Stale! Expected 4
            updated_at=NOW,
        )
        adapters = AsyncMock(spec=Adapters)
        adapters.customers = mock_cust

        ctx = make_ctx(
            ToolName.UPDATE_CUSTOMER,
            input_args={
                "customer_id": "cust_99",
                "expected_version": 3,
                "patch": {"plan": "enterprise"},
            },
            adapters=adapters,
        )

        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "version_advanced_by_one" and not c.passed for c in res.checks)

    @pytest.mark.asyncio
    async def test_patch_field_not_applied_fails(self) -> None:
        verifier = UpdateCustomerVerifier()
        mock_cust = AsyncMock(spec=CustomerPort)
        mock_cust.get.return_value = Customer(
            customer_id="cust_99",
            account_name="Acme Corp",
            primary_contact="Jane Doe",
            email="jane@acme.com",
            status="active",
            plan="starter",  # Was supposed to be updated to "enterprise"!
            version=4,
            updated_at=NOW,
        )
        adapters = AsyncMock(spec=Adapters)
        adapters.customers = mock_cust

        ctx = make_ctx(
            ToolName.UPDATE_CUSTOMER,
            input_args={
                "customer_id": "cust_99",
                "expected_version": 3,
                "patch": {"plan": "enterprise"},
            },
            adapters=adapters,
        )

        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "patched_fields_match_intent" and not c.passed for c in res.checks)


class TestSaveDraftVerifier:
    @pytest.mark.asyncio
    async def test_valid_save_draft_passes(self) -> None:
        verifier = SaveDraftVerifier()
        subject = "Collaboration Opportunity"
        body = "Let's work together."
        expected_hash = hashlib.sha256(f"{subject}\n\n{body}".encode()).hexdigest()

        mock_draft = AsyncMock(spec=DraftPort)
        mock_draft.get.return_value = DraftRecord(
            draft_id="drf_50",
            lead_id="l_7",
            subject=subject,
            body=body,
            channel="email",
            status="saved",
            content_hash=expected_hash,
            saved_at=NOW,
        )
        adapters = AsyncMock(spec=Adapters)
        adapters.drafts = mock_draft

        ctx = make_ctx(
            ToolName.SAVE_DRAFT,
            input_args={"lead_id": "l_7", "subject": subject, "body": body, "channel": "email"},
            output_data={"draft_id": "drf_50"},
            adapters=adapters,
        )

        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.PASSED
        mock_draft.get.assert_awaited_once_with("drf_50")

    @pytest.mark.asyncio
    async def test_channel_mismatch_fails(self) -> None:
        """VERIFY-002: Assert channel matches requested intent."""
        verifier = SaveDraftVerifier()
        subject = "Hello"
        body = "World"
        expected_hash = hashlib.sha256(f"{subject}\n\n{body}".encode()).hexdigest()

        mock_draft = AsyncMock(spec=DraftPort)
        mock_draft.get.return_value = DraftRecord(
            draft_id="drf_50",
            lead_id="l_7",
            subject=subject,
            body=body,
            channel="slack",  # Mismatch! Requested email
            status="saved",
            content_hash=expected_hash,
            saved_at=NOW,
        )
        adapters = AsyncMock(spec=Adapters)
        adapters.drafts = mock_draft

        ctx = make_ctx(
            ToolName.SAVE_DRAFT,
            input_args={"lead_id": "l_7", "subject": subject, "body": body, "channel": "email"},
            output_data={"draft_id": "drf_50"},
            adapters=adapters,
        )

        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "channel_matches_intent" and not c.passed for c in res.checks)

    @pytest.mark.asyncio
    async def test_draft_content_differs_from_requested_intent_fails(self) -> None:
        verifier = SaveDraftVerifier()
        req_subject = "Important Update"
        req_body = "The real content."

        # Mock adapter returns a draft with truncated or modified content
        saved_subject = "Important Update"
        saved_body = "Truncated..."
        tampered_hash = hashlib.sha256(f"{saved_subject}\n\n{saved_body}".encode()).hexdigest()

        mock_draft = AsyncMock(spec=DraftPort)
        mock_draft.get.return_value = DraftRecord(
            draft_id="drf_50",
            lead_id="l_7",
            subject=saved_subject,
            body=saved_body,
            status="saved",
            content_hash=tampered_hash,
            saved_at=NOW,
        )
        adapters = AsyncMock(spec=Adapters)
        adapters.drafts = mock_draft

        ctx = make_ctx(
            ToolName.SAVE_DRAFT,
            input_args={"lead_id": "l_7", "subject": req_subject, "body": req_body},
            output_data={"draft_id": "drf_50"},
            adapters=adapters,
        )

        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(
            c.name == "content_hash_matches_requested_content" and not c.passed for c in res.checks
        )

    @pytest.mark.asyncio
    async def test_draft_not_found_fails_verification(self) -> None:
        verifier = SaveDraftVerifier()
        mock_draft = AsyncMock(spec=DraftPort)
        mock_draft.get.side_effect = NotFoundError("Draft not found")
        adapters = AsyncMock(spec=Adapters)
        adapters.drafts = mock_draft

        ctx = make_ctx(
            ToolName.SAVE_DRAFT,
            output_data={"draft_id": "drf_nonexistent"},
            adapters=adapters,
        )

        res = await verifier.verify(ctx)
        assert res.status == VerificationStatus.FAILED
        assert any(c.name == "draft_record_exists" and not c.passed for c in res.checks)
