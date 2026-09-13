"""The nine concrete tool implementations (§8.4, TOOL-003)."""

from __future__ import annotations

from typing import Final

from app.tools.contracts import ToolName
from app.tools.impl.draft_outreach import draft_outreach
from app.tools.impl.get_customer import get_customer
from app.tools.impl.get_lead import get_lead
from app.tools.impl.research_company import research_company
from app.tools.impl.save_draft import save_draft
from app.tools.impl.score_lead import score_lead
from app.tools.impl.search_leads import search_leads
from app.tools.impl.send_email_mock import send_email_mock
from app.tools.impl.update_customer import update_customer
from app.tools.registry import ToolImplementation

__all__ = [
    "TOOL_IMPLEMENTATIONS",
    "default_implementations",
    "draft_outreach",
    "get_customer",
    "get_lead",
    "research_company",
    "save_draft",
    "score_lead",
    "search_leads",
    "send_email_mock",
    "update_customer",
]

TOOL_IMPLEMENTATIONS: Final[dict[ToolName, ToolImplementation]] = {
    ToolName.SEARCH_LEADS: search_leads,
    ToolName.GET_LEAD: get_lead,
    ToolName.RESEARCH_COMPANY: research_company,
    ToolName.SCORE_LEAD: score_lead,
    ToolName.DRAFT_OUTREACH: draft_outreach,
    ToolName.SAVE_DRAFT: save_draft,
    ToolName.SEND_EMAIL_MOCK: send_email_mock,
    ToolName.GET_CUSTOMER: get_customer,
    ToolName.UPDATE_CUSTOMER: update_customer,
}


def default_implementations() -> dict[ToolName, ToolImplementation]:
    """Return a copy of the default implementations mapping for all nine tools."""
    return dict(TOOL_IMPLEMENTATIONS)
