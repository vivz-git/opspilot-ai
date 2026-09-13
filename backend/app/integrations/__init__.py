"""Integration ports and adapter factories (§19, TOOL-001).

Decouples external services (CRM, email, enrichment, generation) from agent
core logic. Supports mock implementations for deterministic testing and local
execution.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import IntegrationMode, Settings
from app.errors import ConfigurationError
from app.integrations.mock import build_mock_adapters
from app.integrations.ports import (
    Adapters,
    CompanyPort,
    ContentPort,
    CustomerPort,
    DraftContent,
    DraftInput,
    DraftPort,
    DraftRecord,
    LeadFilter,
    LeadPage,
    LeadPort,
    MailPort,
    OutboundMessage,
    OutboxRecord,
    OutreachBrief,
    SendReceipt,
)
from app.runtime import Clock, IdGenerator, SeededRandom

__all__ = [
    "Adapters",
    "CompanyPort",
    "ContentPort",
    "CustomerPort",
    "DraftContent",
    "DraftInput",
    "DraftPort",
    "DraftRecord",
    "LeadFilter",
    "LeadPage",
    "LeadPort",
    "MailPort",
    "OutboundMessage",
    "OutboxRecord",
    "OutreachBrief",
    "SendReceipt",
    "build_adapters",
]


def build_adapters(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    id_gen: IdGenerator,
    random_gen: SeededRandom | None = None,
    *,
    failure_rate: float = 0.0,
) -> Adapters:
    """Build the integration adapters configured for this process (§19.3).

    Raises `ConfigurationError` if `settings.integrations == IntegrationMode.REAL`,
    as real external integrations are disallowed in the current phase (§19.1).
    """
    if settings.integrations == IntegrationMode.REAL:
        raise ConfigurationError(
            "Real integrations are disabled in the current phase. "
            "Set OPSPILOT_INTEGRATIONS=mock to run with mock adapters."
        )
    return build_mock_adapters(
        session_factory,
        clock,
        id_gen,
        random_gen,
        failure_rate=failure_rate,
    )
