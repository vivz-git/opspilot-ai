"""Persistence layer (§12). ORM models and repositories live here only.

Services and agent logic depend on repository protocols (DB-005), never on
`sqlalchemy` directly — this package is where that boundary is drawn.
"""

from app.persistence.mock_crm import (
    Company,
    Customer,
    EmailOutbox,
    EmailOutboxStatus,
    Lead,
    LeadStatus,
    OutreachDraft,
    OutreachDraftStatus,
)

__all__ = [
    "Company",
    "Customer",
    "EmailOutbox",
    "EmailOutboxStatus",
    "Lead",
    "LeadStatus",
    "OutreachDraft",
    "OutreachDraftStatus",
]
