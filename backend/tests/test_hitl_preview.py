"""Unit tests for HITL-005 rich approval preview builder.

Tests:
1. send_email_mock preview with persisted draft (de-references subject, body, lead_id, content_hash)
2. send_email_mock preview from in-memory tool_results (fallback when uow is None)
3. send_email_mock unresolved draft graceful fallback (safe None fields, no crash)
4. send_email_mock secret pattern masking in subject and body (Groq, Anthropic, Bearer)
5. send_email_mock oversized body truncation with content_hash preservation
6. update_customer preview with persisted customer (diff before/after, account_name, version_match)
7. update_customer preview detects stale version (expected_version != current_version)
8. update_customer preview unresolved customer fallback (diff with before=None)
9. update_customer secret pattern masking in patch notes and reason
10. volatile fields (approval_token, idempotency_key) never appear in preview
11. deterministic serialization across invocations
12. preview size budget enforced (<= 4096 bytes)
13. zero CRM mutations during preview generation
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from app.agent.preview import build_approval_preview
from app.agent.state import PlanStep, ToolResult
from app.config import get_settings
from app.persistence.mock_crm import Customer, CustomerStatus, OutreachDraft
from app.security import VOLATILE_ARG_KEYS
from app.tools.contracts import RiskLevel, ToolName
from app.tools.schemas import CustomerPatch
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from recovery_harness import seed_draft, uow_factory_for


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(
        get_settings().database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=20,
    )
    try:
        yield eng
    finally:
        await eng.dispose()


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


T0 = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)


def _make_step(tool: ToolName, step_id: str = "s_test", rationale: str = "Test step") -> PlanStep:
    return PlanStep(
        step_id=step_id,
        tool=tool,
        args={},
        rationale=rationale,
    )


@pytest.mark.unit
class TestHitlSendEmailPreview:
    @pytest.mark.asyncio
    async def test_send_email_preview_with_persisted_draft(self) -> None:
        """HITL-005: send_email_mock de-references draft subject, body, lead_id from UoW."""
        mock_uow = AsyncMock()
        draft_row = OutreachDraft(
            draft_id="drf_123",
            lead_id="l_456",
            subject="Introducing OpsPilot",
            body="Hello Dana,\nWe are excited to share our latest product.",
            content_hash="hash_abc123",
        )
        mock_uow.outreach_drafts.get.return_value = draft_row

        step = _make_step(ToolName.SEND_EMAIL_MOCK, rationale="Send outreach to Dana")
        resolved_args = {"draft_id": "drf_123", "to_email": "dana@northwind.example"}

        preview, title, summary = await build_approval_preview(
            step, resolved_args, uow=mock_uow, risk=RiskLevel.HIGH
        )

        assert preview["action"] == "send_email_mock"
        assert preview["tool"] == "send_email_mock"
        assert preview["risk"] == "high"
        assert preview["draft_id"] == "drf_123"
        assert preview["to_email"] == "dana@northwind.example"
        assert preview["to"] == "dana@northwind.example"
        assert preview["subject"] == "Introducing OpsPilot"
        assert preview["body"] == "Hello Dana,\nWe are excited to share our latest product."
        assert preview["lead_id"] == "l_456"
        assert preview["content_hash"] == "hash_abc123"
        assert preview["args"] == resolved_args
        assert title == "Send outreach email to dana@northwind.example"
        assert summary == "Send outreach to Dana"

    @pytest.mark.asyncio
    async def test_send_email_preview_from_tool_results(self) -> None:
        """HITL-005: send_email_mock de-references from tool_results when uow is None."""
        tool_results = {
            "s_draft": ToolResult(
                step_id="s_draft",
                tool=ToolName.DRAFT_OUTREACH,
                output={
                    "subject": "AI Operations at Scale",
                    "body": "Dear Partner,\nHere is the proposal.",
                    "content_hash": "hash_xyz789",
                },
                produced_at=T0,
            ),
            "s_save": ToolResult(
                step_id="s_save",
                tool=ToolName.SAVE_DRAFT,
                output={
                    "draft_id": "drf_mem_1",
                    "lead_id": "lead_99",
                },
                produced_at=T0,
            ),
        }

        step = _make_step(ToolName.SEND_EMAIL_MOCK)
        resolved_args = {"draft_id": "drf_mem_1", "to_email": "lead@northwind.example"}

        preview, title, summary = await build_approval_preview(
            step, resolved_args, tool_results=tool_results, uow=None, risk=RiskLevel.HIGH
        )

        assert preview["draft_id"] == "drf_mem_1"
        assert preview["to_email"] == "lead@northwind.example"
        assert preview["subject"] == "AI Operations at Scale"
        assert preview["body"] == "Dear Partner,\nHere is the proposal."
        assert preview["lead_id"] == "lead_99"
        assert preview["content_hash"] == "hash_xyz789"

    @pytest.mark.asyncio
    async def test_send_email_preview_unresolved_draft_graceful_fallback(self) -> None:
        """HITL-005: Unresolved draft ID does not crash, returns safe None fields."""
        mock_uow = AsyncMock()
        mock_uow.outreach_drafts.get.return_value = None

        step = _make_step(ToolName.SEND_EMAIL_MOCK)
        resolved_args = {"draft_id": "drf_missing", "to_email": "missing@example.com"}

        preview, title, summary = await build_approval_preview(
            step, resolved_args, uow=mock_uow, risk=RiskLevel.HIGH
        )

        assert preview["draft_id"] == "drf_missing"
        assert preview["to_email"] == "missing@example.com"
        assert preview["subject"] is None
        assert preview["body"] is None
        assert preview["lead_id"] is None
        assert title == "Send outreach email to missing@example.com"

    @pytest.mark.asyncio
    async def test_send_email_preview_masks_embedded_secrets(self) -> None:
        """HITL-005: Embedded API keys or tokens in email body/subject are masked."""
        mock_uow = AsyncMock()
        draft_row = OutreachDraft(
            draft_id="drf_secret",
            lead_id="l_sec",
            subject="Key sk-ant-secret12345",
            body="Use this key: gsk_supersecret123 and Authorization: Bearer token12345678",
            content_hash="hash_sec",
        )
        mock_uow.outreach_drafts.get.return_value = draft_row

        step = _make_step(ToolName.SEND_EMAIL_MOCK)
        resolved_args = {"draft_id": "drf_secret", "to_email": "sec@example.com"}

        preview, _, _ = await build_approval_preview(step, resolved_args, uow=mock_uow)

        assert "[redacted]" in preview["subject"]
        assert "sk-ant-secret12345" not in preview["subject"]
        assert "[redacted]" in preview["body"]
        assert "gsk_supersecret123" not in preview["body"]
        assert "token12345678" not in preview["body"]

    @pytest.mark.asyncio
    async def test_send_email_preview_truncates_oversized_body(self) -> None:
        """HITL-005: Oversized body is truncated at 1500 chars with content_hash preserved."""
        mock_uow = AsyncMock()
        large_body = "A" * 5000
        draft_row = OutreachDraft(
            draft_id="drf_big",
            lead_id="l_big",
            subject="Big Email",
            body=large_body,
            content_hash="hash_big_999",
        )
        mock_uow.outreach_drafts.get.return_value = draft_row

        step = _make_step(ToolName.SEND_EMAIL_MOCK)
        resolved_args = {"draft_id": "drf_big", "to_email": "big@example.com"}

        preview, _, _ = await build_approval_preview(step, resolved_args, uow=mock_uow)

        assert len(preview["body"]) < 2000
        assert (
            "[truncated: original length 5000 chars, content_hash: hash_big_999]" in preview["body"]
        )
        assert preview["content_hash"] == "hash_big_999"


@pytest.mark.unit
class TestHitlUpdateCustomerPreview:
    @pytest.mark.asyncio
    async def test_update_customer_preview_with_persisted_customer(self) -> None:
        """HITL-005: update_customer produces field-by-field before/after diff."""
        mock_uow = AsyncMock()
        customer = Customer(
            customer_id="cust_001",
            account_name="Acme Corp",
            primary_contact="Alice Smith",
            email="alice@acme.example",
            status=CustomerStatus.PROSPECT,
            plan="starter",
            version=3,
        )
        mock_uow.customers.get.return_value = customer

        step = _make_step(ToolName.UPDATE_CUSTOMER, rationale="Promote Acme to enterprise")
        patch = CustomerPatch(status=CustomerStatus.ACTIVE, plan="enterprise")
        resolved_args = {
            "customer_id": "cust_001",
            "expected_version": 3,
            "patch": patch,
            "reason": "Signed contract",
        }

        preview, title, summary = await build_approval_preview(
            step, resolved_args, uow=mock_uow, risk=RiskLevel.HIGH
        )

        assert preview["action"] == "update_customer"
        assert preview["tool"] == "update_customer"
        assert preview["risk"] == "high"
        assert preview["customer_id"] == "cust_001"
        assert preview["account_name"] == "Acme Corp"
        assert preview["primary_contact"] == "Alice Smith"
        assert preview["email"] == "alice@acme.example"
        assert preview["expected_version"] == 3
        assert preview["current_version"] == 3
        assert preview["version_match"] is True

        diff = preview["diff"]
        assert diff["status"] == {"before": "prospect", "after": "active"}
        assert diff["plan"] == {"before": "starter", "after": "enterprise"}
        assert title == "Update customer Acme Corp"
        assert summary == "Promote Acme to enterprise"

    @pytest.mark.asyncio
    async def test_update_customer_preview_detects_stale_version(self) -> None:
        """HITL-005: Stale version mismatch flags version_match=False for operator alert."""
        mock_uow = AsyncMock()
        customer = Customer(
            customer_id="cust_002",
            account_name="Beta LLC",
            primary_contact="Bob",
            email="bob@beta.example",
            status=CustomerStatus.ACTIVE,
            version=5,
        )
        mock_uow.customers.get.return_value = customer

        step = _make_step(ToolName.UPDATE_CUSTOMER)
        resolved_args = {
            "customer_id": "cust_002",
            "expected_version": 4,  # Stale! DB is at version 5
            "patch": {"status": "churned"},
            "reason": "Inactivity",
        }

        preview, _, _ = await build_approval_preview(step, resolved_args, uow=mock_uow)

        assert preview["expected_version"] == 4
        assert preview["current_version"] == 5
        assert preview["version_match"] is False

    @pytest.mark.asyncio
    async def test_update_customer_preview_unresolved_customer_fallback(self) -> None:
        """HITL-005: Missing customer ID produces diff with before=None without error."""
        mock_uow = AsyncMock()
        mock_uow.customers.get.return_value = None

        step = _make_step(ToolName.UPDATE_CUSTOMER)
        resolved_args = {
            "customer_id": "cust_missing",
            "expected_version": 1,
            "patch": {"status": "qualified"},
            "reason": "New lead",
        }

        preview, title, _ = await build_approval_preview(step, resolved_args, uow=mock_uow)

        assert preview["customer_id"] == "cust_missing"
        assert preview["account_name"] is None
        assert preview["current_version"] is None
        assert preview["version_match"] is None
        assert preview["diff"]["status"] == {"before": None, "after": "qualified"}
        assert title == "Update customer cust_missing"

    @pytest.mark.asyncio
    async def test_update_customer_preview_masks_secrets_in_patch(self) -> None:
        """HITL-005: Sensitive keys or values in patch/notes/reason are masked."""
        mock_uow = AsyncMock()
        mock_uow.customers.get.return_value = None

        step = _make_step(ToolName.UPDATE_CUSTOMER)
        resolved_args = {
            "customer_id": "cust_sec",
            "expected_version": 1,
            "patch": {"notes": "Customer API secret is gsk_sensitivekey123"},
            "reason": "Account token: sk-ant-token9999",
            "api_key": "my-secret-api-key",
        }

        preview, _, _ = await build_approval_preview(step, resolved_args, uow=mock_uow)

        assert "[redacted]" in preview["diff"]["notes"]["after"]
        assert "gsk_sensitivekey123" not in preview["diff"]["notes"]["after"]
        assert "[redacted]" in preview["reason"]
        assert "sk-ant-token9999" not in preview["reason"]
        assert preview["args"]["api_key"] == "[redacted]"


@pytest.mark.unit
class TestHitlPreviewInvariants:
    @pytest.mark.asyncio
    async def test_preview_volatile_fields_never_appear(self) -> None:
        """HITL-005: approval_token, idempotency_key never appear in preview."""
        step = _make_step(ToolName.SEND_EMAIL_MOCK)
        resolved_args = {
            "draft_id": "drf_1",
            "to_email": "test@example.com",
            "approval_token": "FORGED_TOKEN",
            "idempotency_key": "idemp-key-12345",
            "trace_id": "tr-abc",
            "requested_at": "2026-09-15T00:00:00Z",
        }

        preview, _, _ = await build_approval_preview(step, resolved_args)

        for key in VOLATILE_ARG_KEYS:
            assert key not in preview
            assert key not in preview["args"]

    @pytest.mark.asyncio
    async def test_preview_deterministic_ordering(self) -> None:
        """HITL-005: Preview output is deterministic across repeated calls."""
        step = _make_step(ToolName.SEND_EMAIL_MOCK)
        resolved_args = {"to_email": "zeta@example.com", "draft_id": "drf_alpha"}

        p1, _, _ = await build_approval_preview(step, resolved_args)
        p2, _, _ = await build_approval_preview(step, resolved_args)

        json1 = json.dumps(p1, sort_keys=True)
        json2 = json.dumps(p2, sort_keys=True)
        assert json1 == json2

    @pytest.mark.asyncio
    async def test_preview_size_budget_enforced(self) -> None:
        """HITL-005: Preview size never exceeds 4096 bytes."""
        step = _make_step(ToolName.SEND_EMAIL_MOCK)
        huge_args = {
            "draft_id": "drf_huge",
            "to_email": "huge@example.com",
            "extra_large_metadata": {"key_" + str(i): "val_" + ("x" * 50) for i in range(100)},
        }

        preview, _, _ = await build_approval_preview(step, huge_args)

        encoded = json.dumps(preview, ensure_ascii=False)
        assert len(encoded.encode("utf-8")) <= 4096

    @pytest.mark.asyncio
    async def test_generic_fallback_tool_preview(self) -> None:
        """HITL-005: Arbitrary/fallback tools generate safe generic preview."""
        step = _make_step(ToolName.SEARCH_LEADS, rationale="Search UK fintech leads")
        resolved_args = {"industry": "fintech", "location": "London"}

        preview, title, summary = await build_approval_preview(
            step, resolved_args, risk=RiskLevel.LOW
        )

        assert preview["action"] == "search_leads"
        assert preview["tool"] == "search_leads"
        assert preview["risk"] == "low"
        assert preview["args"] == resolved_args
        assert title == "search_leads: approve step s_test"
        assert summary == "Search UK fintech leads"


@pytest.mark.integration
class TestHitlPreviewZeroMutation:
    @pytest.mark.asyncio
    async def test_preview_generation_does_not_mutate_crm(self, engine: AsyncEngine) -> None:
        """HITL-005: Preview generation performs zero CRM mutations."""
        uow_factory = uow_factory_for(engine)
        draft_id, to_email = await seed_draft(uow_factory)

        session_factory = async_sessionmaker(engine, autoflush=True, expire_on_commit=False)
        before_fingerprint = await crm_fingerprint(session_factory)

        step = _make_step(ToolName.SEND_EMAIL_MOCK)
        resolved_args = {"draft_id": draft_id, "to_email": to_email}

        async with uow_factory() as uow:
            preview, _, _ = await build_approval_preview(step, resolved_args, uow=uow)

        after_fingerprint = await crm_fingerprint(session_factory)

        assert before_fingerprint == after_fingerprint
        assert preview["draft_id"] == draft_id
        assert preview["to_email"] == to_email
        assert preview["subject"] == "Hello"
        assert preview["body"] == "Body"
