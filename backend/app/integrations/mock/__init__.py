"""Mock integration package (§19.2, TOOL-001).

Simulated third-party adapters providing CRM, firmographic research,
content generation, and email dispatch with zero network calls and full
contract readback verification.
"""

from typing import TYPE_CHECKING

from app.integrations.mock.adapters import (
    MockCompanyAdapter,
    MockContentAdapter,
    MockCustomerAdapter,
    MockDraftAdapter,
    MockLeadAdapter,
    MockMailAdapter,
    build_mock_adapters,
)
from app.integrations.mock.fixtures import (
    COMPANY_FIXTURES,
    CUSTOMER_FIXTURES,
    FIXTURE_BASE_TIME,
    LEAD_FIXTURES,
)

if TYPE_CHECKING:
    from app.integrations.mock.seed import seed_database

__all__ = [
    "COMPANY_FIXTURES",
    "CUSTOMER_FIXTURES",
    "FIXTURE_BASE_TIME",
    "LEAD_FIXTURES",
    "MockCompanyAdapter",
    "MockContentAdapter",
    "MockCustomerAdapter",
    "MockDraftAdapter",
    "MockLeadAdapter",
    "MockMailAdapter",
    "build_mock_adapters",
    "seed_database",
]


def __getattr__(name: str) -> object:
    if name == "seed_database":
        from app.integrations.mock.seed import seed_database

        return seed_database
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
