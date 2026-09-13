"""Mock adapters implementing integration ports against mock_crm (§19.2, TOOL-001).

Zero-network mock adapters conforming to the ports defined in `app.integrations.ports`.
All mutations are recorded durably into the `mock_crm` schema and are verifiable
via contract-driven readback queries.
"""

from __future__ import annotations

import hashlib

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import (
    InputValidationError,
    NotFoundError,
    PolicyViolation,
    StaleWriteError,
    TransientToolError,
)
from app.integrations.ports import (
    Adapters,
    DraftContent,
    DraftInput,
    DraftRecord,
    LeadFilter,
    LeadPage,
    OutboundMessage,
    OutboxRecord,
    OutreachBrief,
    SendReceipt,
)
from app.persistence.mock_crm import (
    EmailOutbox,
    EmailOutboxStatus,
    OutreachDraft,
    OutreachDraftStatus,
)
from app.persistence.session import unit_of_work
from app.runtime import Clock, IdGenerator, SeededRandom
from app.security import ApprovalToken
from app.tools.schemas import (
    CompanyProfile,
    Customer,
    CustomerPatch,
    LeadDetail,
    LeadSummary,
    ResearchDepth,
    Signal,
)

__all__ = [
    "MockCompanyAdapter",
    "MockContentAdapter",
    "MockCustomerAdapter",
    "MockDraftAdapter",
    "MockLeadAdapter",
    "MockMailAdapter",
    "build_mock_adapters",
]


class MockLeadAdapter:
    """Mock implementation of LeadPort against mock_crm database tables."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        id_gen: IdGenerator,
        random_gen: SeededRandom | None = None,
        *,
        failure_rate: float = 0.0,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._id_gen = id_gen
        self._random_gen = random_gen
        self._failure_rate = failure_rate

    def _maybe_fail(self, op: str) -> None:
        if (
            self._failure_rate > 0.0
            and self._random_gen is not None
            and self._random_gen.random() < self._failure_rate
        ):
            raise TransientToolError(f"Injected upstream transient failure in LeadPort.{op}")

    async def search(self, f: LeadFilter) -> LeadPage:
        self._maybe_fail("search")
        async with unit_of_work(self._session_factory) as uow:
            leads, total = await uow.leads.search(
                industry=f.industry,
                location=f.location,
                min_employees=f.min_employees,
                max_employees=f.max_employees,
                status=f.status,
                query=f.query,
                limit=f.limit,
                offset=f.offset,
            )
            summaries: list[LeadSummary] = []
            for lead in leads:
                comp_name = lead.company.name if lead.company is not None else ""
                summaries.append(
                    LeadSummary(
                        lead_id=lead.lead_id,
                        full_name=lead.full_name,
                        title=lead.title,
                        email=lead.email,
                        company_id=lead.company_id,
                        company_name=comp_name,
                        status=lead.status,
                        source=lead.source,
                        created_at=lead.created_at,
                    )
                )
            return LeadPage(
                leads=summaries,
                total_matched=total,
                truncated=(f.offset + len(summaries) < total),
            )

    async def get(self, lead_id: str) -> LeadDetail:
        self._maybe_fail("get")
        async with unit_of_work(self._session_factory) as uow:
            lead = await uow.leads.get(lead_id)
            if lead is None:
                raise NotFoundError(f"Lead not found: {lead_id}")
            company = await uow.companies.get(lead.company_id)
            comp_name = company.name if company is not None else ""
            return LeadDetail(
                lead_id=lead.lead_id,
                full_name=lead.full_name,
                title=lead.title,
                email=lead.email,
                company_id=lead.company_id,
                company_name=comp_name,
                status=lead.status,
                source=lead.source,
                created_at=lead.created_at,
                phone=lead.phone,
                timezone=lead.timezone,
                tags=list(lead.tags or []),
                owner=lead.owner,
                last_contacted_at=lead.last_contacted_at,
                notes=lead.notes,
            )


class MockCompanyAdapter:
    """Mock implementation of CompanyPort against mock_crm database tables."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        id_gen: IdGenerator,
        random_gen: SeededRandom | None = None,
        *,
        failure_rate: float = 0.0,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._id_gen = id_gen
        self._random_gen = random_gen
        self._failure_rate = failure_rate

    def _maybe_fail(self, op: str) -> None:
        if (
            self._failure_rate > 0.0
            and self._random_gen is not None
            and self._random_gen.random() < self._failure_rate
        ):
            raise TransientToolError(f"Injected upstream transient failure in CompanyPort.{op}")

    async def profile(
        self,
        *,
        company_id: str | None = None,
        domain: str | None = None,
        depth: ResearchDepth = ResearchDepth.STANDARD,
    ) -> CompanyProfile:
        self._maybe_fail("profile")
        if (company_id is None) == (domain is None):
            raise InputValidationError("Exactly one of company_id or domain is required")

        async with unit_of_work(self._session_factory) as uow:
            if company_id is not None:
                company = await uow.companies.get(company_id)
            elif domain is not None:
                company = await uow.companies.get_by_domain(domain)
            else:
                raise InputValidationError("company_id or domain required")

            if company is None:
                raise NotFoundError(
                    f"Company not found for company_id={company_id}, domain={domain}"
                )

            signals_list: list[Signal] = []
            for s in company.signals or []:
                if isinstance(s, dict):
                    signals_list.append(Signal.model_validate(s))
                elif isinstance(s, Signal):
                    signals_list.append(s)

            if depth == ResearchDepth.BASIC:
                signals_to_return = signals_list[:1]
                summary = (
                    f"{company.name} operates in the {company.industry or 'technology'} sector "
                    f"with approximately {company.employee_count or 0} employees."
                )
                confidence = 0.85
            else:
                signals_to_return = signals_list
                summary = (
                    f"{company.name} is an enterprise organization in the "
                    f"{company.industry or 'technology'} sector, headquartered in "
                    f"{company.hq_location or 'unknown'}. Funding stage: "
                    f"{company.funding_stage or 'N/A'}. Estimated revenue: "
                    f"{company.revenue_band or 'N/A'}."
                )
                confidence = 0.95

            return CompanyProfile(
                company_id=company.company_id,
                name=company.name,
                domain=company.domain,
                industry=company.industry,
                employee_count=company.employee_count,
                revenue_band=company.revenue_band,
                hq_location=company.hq_location,
                funding_stage=company.funding_stage,
                tech_stack=list(company.tech_stack or []),
                recent_signals=signals_to_return,
                summary=summary,
                sources=[f"https://{company.domain}", "https://registry.example/firms"],
                confidence=confidence,
                retrieved_at=self._clock.now(),
            )


class MockCustomerAdapter:
    """Mock implementation of CustomerPort against mock_crm database tables."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        id_gen: IdGenerator,
        random_gen: SeededRandom | None = None,
        *,
        failure_rate: float = 0.0,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._id_gen = id_gen
        self._random_gen = random_gen
        self._failure_rate = failure_rate

    def _maybe_fail(self, op: str) -> None:
        if (
            self._failure_rate > 0.0
            and self._random_gen is not None
            and self._random_gen.random() < self._failure_rate
        ):
            raise TransientToolError(f"Injected upstream transient failure in CustomerPort.{op}")

    async def get(
        self,
        *,
        customer_id: str | None = None,
        email: str | None = None,
    ) -> Customer:
        self._maybe_fail("get")
        if (customer_id is None) == (email is None):
            raise InputValidationError("Exactly one of customer_id or email is required")

        async with unit_of_work(self._session_factory) as uow:
            if customer_id is not None:
                cust = await uow.customers.get(customer_id)
            elif email is not None:
                cust = await uow.customers.get_by_email(email)
            else:
                raise InputValidationError("customer_id or email required")

            if cust is None:
                raise NotFoundError(f"Customer not found for id={customer_id}, email={email}")

            return Customer(
                customer_id=cust.customer_id,
                account_name=cust.account_name,
                primary_contact=cust.primary_contact,
                email=cust.email,
                phone=cust.phone,
                status=cust.status,
                plan=cust.plan,
                mrr=float(cust.mrr) if cust.mrr is not None else None,
                owner=cust.owner,
                version=cust.version,
                updated_at=cust.updated_at,
            )

    async def update(
        self,
        customer_id: str,
        patch: CustomerPatch,
        *,
        expected_version: int,
        token: ApprovalToken,
        idempotency_key: str,
    ) -> Customer:
        self._maybe_fail("update")
        if token is None or not isinstance(token, ApprovalToken):
            raise PolicyViolation("Approval token is required for customer update")

        patch_dict = patch.model_dump(exclude_none=True)
        if not patch_dict:
            raise InputValidationError("CustomerPatch must change at least one field")

        async with unit_of_work(self._session_factory) as uow:
            updated = await uow.customers.update_optimistic(
                customer_id,
                expected_version=expected_version,
                **patch_dict,
            )
            if updated is None:
                existing = await uow.customers.get(customer_id)
                if existing is None:
                    raise NotFoundError(f"Customer not found: {customer_id}")
                raise StaleWriteError(
                    f"Customer {customer_id} version mismatch: "
                    f"expected {expected_version}, current {existing.version}"
                )
            await uow.commit()
            return Customer(
                customer_id=updated.customer_id,
                account_name=updated.account_name,
                primary_contact=updated.primary_contact,
                email=updated.email,
                phone=updated.phone,
                status=updated.status,
                plan=updated.plan,
                mrr=float(updated.mrr) if updated.mrr is not None else None,
                owner=updated.owner,
                version=updated.version,
                updated_at=updated.updated_at,
            )


class MockDraftAdapter:
    """Mock implementation of DraftPort against mock_crm database tables."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        id_gen: IdGenerator,
        random_gen: SeededRandom | None = None,
        *,
        failure_rate: float = 0.0,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._id_gen = id_gen
        self._random_gen = random_gen
        self._failure_rate = failure_rate

    def _maybe_fail(self, op: str) -> None:
        if (
            self._failure_rate > 0.0
            and self._random_gen is not None
            and self._random_gen.random() < self._failure_rate
        ):
            raise TransientToolError(f"Injected upstream transient failure in DraftPort.{op}")

    async def save(self, draft: DraftInput) -> DraftRecord:
        self._maybe_fail("save")
        draft_id = self._id_gen.new_id(prefix="drf_")
        now = self._clock.now()
        record = OutreachDraft(
            draft_id=draft_id,
            lead_id=draft.lead_id,
            channel=draft.channel,
            subject=draft.subject,
            body=draft.body,
            content_hash=draft.content_hash,
            status=OutreachDraftStatus.SAVED,
            version=1,
            metadata_=draft.metadata,
            created_at=now,
            updated_at=now,
        )
        async with unit_of_work(self._session_factory) as uow:
            lead = await uow.leads.get(draft.lead_id)
            if lead is None:
                raise NotFoundError(f"Lead not found: {draft.lead_id}")
            saved = await uow.outreach_drafts.create(record)
            await uow.commit()
            return DraftRecord(
                draft_id=saved.draft_id,
                lead_id=saved.lead_id,
                subject=saved.subject,
                body=saved.body,
                channel=saved.channel,
                status=saved.status.value,
                version=saved.version,
                content_hash=saved.content_hash,
                saved_at=saved.created_at,
                metadata=saved.metadata_,
            )

    async def get(self, draft_id: str) -> DraftRecord:
        self._maybe_fail("get")
        async with unit_of_work(self._session_factory) as uow:
            saved = await uow.outreach_drafts.get(draft_id)
            if saved is None:
                raise NotFoundError(f"Draft not found: {draft_id}")
            return DraftRecord(
                draft_id=saved.draft_id,
                lead_id=saved.lead_id,
                subject=saved.subject,
                body=saved.body,
                channel=saved.channel,
                status=saved.status.value,
                version=saved.version,
                content_hash=saved.content_hash,
                saved_at=saved.created_at,
                metadata=saved.metadata_,
            )


class MockMailAdapter:
    """Mock implementation of MailPort against mock_crm database tables.

    Guarantees:
    - Zero network calls (persists to simulated mock_crm.email_outbox table).
    - Idempotency via unique idempotency_key (returns existing SendReceipt on duplicate).
    - Policy check: to_email must match lead email associated with draft.
    - Approval token verification.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        id_gen: IdGenerator,
        random_gen: SeededRandom | None = None,
        *,
        failure_rate: float = 0.0,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._id_gen = id_gen
        self._random_gen = random_gen
        self._failure_rate = failure_rate

    def _maybe_fail(self, op: str) -> None:
        if (
            self._failure_rate > 0.0
            and self._random_gen is not None
            and self._random_gen.random() < self._failure_rate
        ):
            raise TransientToolError(f"Injected upstream transient failure in MailPort.{op}")

    async def send(
        self,
        msg: OutboundMessage,
        *,
        token: ApprovalToken,
        idempotency_key: str,
    ) -> SendReceipt:
        self._maybe_fail("send")
        if token is None or not isinstance(token, ApprovalToken):
            raise PolicyViolation("Approval token is required to send email")

        async with unit_of_work(self._session_factory) as uow:
            existing = await uow.email_outbox.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return SendReceipt(
                    message_id=existing.message_id,
                    outbox_id=existing.outbox_id,
                    status=existing.status.value,
                    provider=existing.provider,
                    to_email=existing.to_email,
                    draft_id=existing.draft_id,
                    sent_at=existing.sent_at or existing.created_at,
                )

            draft = await uow.outreach_drafts.get(msg.draft_id)
            if draft is None:
                raise NotFoundError(f"Draft not found: {msg.draft_id}")

            lead = await uow.leads.get(draft.lead_id)
            if lead is not None and lead.email.lower() != msg.to_email.lower():
                raise PolicyViolation(
                    f"Recipient {msg.to_email} does not match lead email {lead.email}"
                )

            outbox_id = self._id_gen.new_id(prefix="out_")
            message_id = self._id_gen.new_id(prefix="msg_")
            now = self._clock.now()

            run_id = str(token.run_id) if hasattr(token, "run_id") and token.run_id else None
            approval_id = (
                str(token.approval_id)
                if hasattr(token, "approval_id") and token.approval_id
                else None
            )

            entry = EmailOutbox(
                outbox_id=outbox_id,
                message_id=message_id,
                draft_id=draft.draft_id,
                to_email=str(msg.to_email),
                subject=draft.subject,
                body=draft.body,
                status=EmailOutboxStatus.SENT,
                provider="mock",
                idempotency_key=idempotency_key,
                run_id=run_id,
                approval_id=approval_id,
                created_at=now,
                sent_at=now,
            )
            await uow.email_outbox.create(entry)
            await uow.outreach_drafts.update_status(draft.draft_id, OutreachDraftStatus.SENT)
            await uow.commit()

            return SendReceipt(
                message_id=message_id,
                outbox_id=outbox_id,
                status="sent",
                provider="mock",
                to_email=msg.to_email,
                draft_id=draft.draft_id,
                sent_at=now,
            )

    async def get_outbox(self, message_id: str) -> OutboxRecord:
        self._maybe_fail("get_outbox")
        async with unit_of_work(self._session_factory) as uow:
            entry = await uow.email_outbox.get_by_message_id(message_id)
            if entry is None:
                raise NotFoundError(f"Outbox entry not found: {message_id}")
            return OutboxRecord(
                outbox_id=entry.outbox_id,
                message_id=entry.message_id,
                draft_id=entry.draft_id,
                to_email=entry.to_email,
                subject=entry.subject,
                body=entry.body,
                status=entry.status.value,
                provider=entry.provider,
                idempotency_key=entry.idempotency_key,
                sent_at=entry.sent_at,
            )


class MockContentAdapter:
    """Mock implementation of ContentPort generating deterministic templated copy."""

    def __init__(
        self,
        clock: Clock,
        id_gen: IdGenerator,
        random_gen: SeededRandom | None = None,
        *,
        failure_rate: float = 0.0,
    ) -> None:
        self._clock = clock
        self._id_gen = id_gen
        self._random_gen = random_gen
        self._failure_rate = failure_rate

    def _maybe_fail(self, op: str) -> None:
        if (
            self._failure_rate > 0.0
            and self._random_gen is not None
            and self._random_gen.random() < self._failure_rate
        ):
            raise TransientToolError(f"Injected upstream transient failure in ContentPort.{op}")

    async def draft(self, brief: OutreachBrief) -> DraftContent:
        self._maybe_fail("draft")
        tone = (brief.tone or "direct").lower()
        title_str = brief.title or "leader"

        if tone == "warm":
            subject = f"Connecting with {brief.company_name} — operational efficiency"
            signal_context = (
                f" We've been following {brief.company_name}'s exciting trajectory in "
                f"{brief.company_summary}."
                if brief.company_summary
                else ""
            )
            body = (
                f"Hi {brief.lead_name},\n\n"
                f"Hope your week is off to a great start.{signal_context} "
                f"As {title_str} at {brief.company_name}, you likely see first-hand the balance "
                f"between scaling operations and maintaining high quality.\n\n"
                f"We build human-in-the-loop autonomous AI agents that handle repetitive "
                f"operational tasks safely and reliably. Would you be open to a quick 15-minute "
                f"chat next week to see how teams like yours are accelerating their workflows?\n\n"
                f"Best regards,\n"
                f"OpsPilot Team"
            )
        elif tone == "formal":
            subject = f"Inquiry regarding operational optimization at {brief.company_name}"
            body = (
                f"Dear {brief.lead_name},\n\n"
                f"I am writing to introduce OpsPilot AI and explore potential alignment with\n"
                f"{brief.company_name}'s strategic initiatives. Given your role as {title_str},\n"
                f"our verified orchestration platform may be of strategic value in optimizing\n"
                f"your operational pipeline.\n\n"
                f"Our system provides deterministic, audit-trailed workflow automation with strict "
                f"verification boundaries. We would appreciate an opportunity to present an "
                f"executive briefing at your convenience.\n\n"
                f"Sincerely,\n"
                f"OpsPilot AI Team"
            )
        else:  # "direct" or default
            subject = f"Partnership inquiry for {brief.company_name}"
            signals_text = (
                f" following {brief.company_name}'s recent milestones"
                if brief.recent_signals
                else ""
            )
            body = (
                f"Hi {brief.lead_name},\n\n"
                f"I noticed your role as {title_str} at {brief.company_name}{signals_text}. "
                f"OpsPilot provides production-grade operational AI agents that automate "
                f"multi-step processes with human-in-the-loop governance.\n\n"
                f"Do you have 10 minutes this Thursday or Friday to connect?\n\n"
                f"Best,\n"
                f"OpsPilot AI Team"
            )

        words = body.split()
        if len(words) > brief.max_words:
            body = " ".join(words[: brief.max_words])
            words = body.split()

        word_count = len(words)
        payload = f"{subject}\n\n{body}"
        content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

        return DraftContent(
            subject=subject,
            body=body,
            word_count=word_count,
            personalization_notes=[
                f"Targeted for {brief.lead_name} at {brief.company_name}",
                f"Tone: {brief.tone}",
            ],
            content_hash=content_hash,
            model_version="mock-v1",
        )


def build_mock_adapters(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    id_gen: IdGenerator,
    random_gen: SeededRandom | None = None,
    *,
    failure_rate: float = 0.0,
) -> Adapters:
    """Build the bundle of all mock adapters."""
    return Adapters(
        leads=MockLeadAdapter(
            session_factory, clock, id_gen, random_gen, failure_rate=failure_rate
        ),
        companies=MockCompanyAdapter(
            session_factory, clock, id_gen, random_gen, failure_rate=failure_rate
        ),
        customers=MockCustomerAdapter(
            session_factory, clock, id_gen, random_gen, failure_rate=failure_rate
        ),
        drafts=MockDraftAdapter(
            session_factory, clock, id_gen, random_gen, failure_rate=failure_rate
        ),
        mail=MockMailAdapter(session_factory, clock, id_gen, random_gen, failure_rate=failure_rate),
        content=MockContentAdapter(clock, id_gen, random_gen, failure_rate=failure_rate),
    )
