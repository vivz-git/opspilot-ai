"""Deterministic seed dataset loader for the mock CRM (§19.2, TOOL-001).

Loads reproducible firmographic, lead, and customer data bound to RFC 2606
domains into the `mock_crm` database schema.

Callable programmatically via `seed_database(session_factory, reset=True)` or
via the CLI: `python -m app.integrations.mock.seed` (`make seed`).
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.integrations.mock.fixtures import (
    COMPANY_FIXTURES,
    CUSTOMER_FIXTURES,
    LEAD_FIXTURES,
)
from app.persistence.mock_crm import Company, Customer, Lead
from app.persistence.session import create_session_factory, unit_of_work

__all__ = ["seed_database"]


async def seed_database(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reset: bool = True,
) -> dict[str, int]:
    """Seed the mock_crm schema with deterministic test fixtures.

    Args:
        session_factory: SQLAlchemy async session factory.
        reset: If True, truncates all mock_crm tables prior to insertion.
            If False, inserts only records that do not already exist.

    Returns:
        Dictionary containing counts of inserted/loaded records by table.
    """
    if reset:
        async with unit_of_work(session_factory) as uow:
            await uow.reset_mock_crm(
                companies=COMPANY_FIXTURES, leads=LEAD_FIXTURES, customers=CUSTOMER_FIXTURES
            )
            await uow.commit()
        return {
            "companies": len(COMPANY_FIXTURES),
            "leads": len(LEAD_FIXTURES),
            "customers": len(CUSTOMER_FIXTURES),
        }

    companies_seeded = 0
    leads_seeded = 0
    customers_seeded = 0

    async with unit_of_work(session_factory) as uow:
        # 1. Companies
        for comp_data in COMPANY_FIXTURES:
            comp_id = str(comp_data["company_id"])
            existing_comp = await uow.companies.get(comp_id)
            if existing_comp is not None:
                continue
            comp = Company(**comp_data)
            await uow.companies.create(comp)
            companies_seeded += 1

        # 2. Leads (depend on companies)
        for lead_data in LEAD_FIXTURES:
            lead_id = str(lead_data["lead_id"])
            existing_lead = await uow.leads.get(lead_id)
            if existing_lead is not None:
                continue
            lead = Lead(**lead_data)
            await uow.leads.create(lead)
            leads_seeded += 1

        # 3. Customers
        for cust_data in CUSTOMER_FIXTURES:
            cust_id = str(cust_data["customer_id"])
            existing_cust = await uow.customers.get(cust_id)
            if existing_cust is not None:
                continue
            cust = Customer(**cust_data)
            await uow.customers.create(cust)
            customers_seeded += 1

        await uow.commit()

    return {
        "companies": companies_seeded,
        "leads": leads_seeded,
        "customers": customers_seeded,
    }


async def main(argv: Sequence[str] | None = None) -> None:
    """CLI entrypoint for `make seed` / `python -m app.integrations.mock.seed`.

    Defaults to `--reset`, which is what a local developer wants: the mock CRM
    goes back to the exact fixture dataset. A hosted deployment that seeds on
    boot passes `--no-reset` instead, so restarting the container never
    truncates rows an operator was looking at (docs/deployment.md)."""
    parser = argparse.ArgumentParser(prog="python -m app.integrations.mock.seed")
    parser.add_argument(
        "--no-reset",
        dest="reset",
        action="store_false",
        help="insert only missing rows instead of truncating the mock CRM first",
    )
    parser.set_defaults(reset=True)
    args = parser.parse_args(argv)

    settings = get_settings()
    engine = create_async_engine(settings.database_url.get_secret_value())
    session_factory = create_session_factory(engine)
    try:
        counts = await seed_database(session_factory, reset=args.reset)
        verb = "seeded" if args.reset else "topped up"
        print(  # noqa: T201
            f"Successfully {verb} mock CRM: "
            f"{counts['companies']} companies, "
            f"{counts['leads']} leads, "
            f"{counts['customers']} customers."
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
