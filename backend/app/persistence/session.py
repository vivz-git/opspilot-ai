"""Session and transaction lifecycle management (§12, DB-005).

Provides async session factory creation and the `unit_of_work()` context manager.
Ensures:
- No global mutable session object exists.
- Sessions are strictly scoped to context blocks and closed upon exit.
- Unit of Work enforces explicit commit semantics and automatic rollback on failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings
from app.persistence.protocols import UnitOfWork
from app.persistence.repositories import SqlUnitOfWork

__all__ = [
    "create_session_factory",
    "unit_of_work",
]


def create_session_factory(
    engine: AsyncEngine | None = None,
    *,
    settings: Settings | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Create a configured async_sessionmaker with expire_on_commit=False.

    Does not create a global mutable session; callers obtain fresh sessions from
    the returned factory.
    """
    if engine is None:
        cfg = settings or get_settings()
        engine = create_async_engine(
            cfg.database_url.get_secret_value(),
            pool_pre_ping=True,
        )
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@asynccontextmanager
async def unit_of_work(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[UnitOfWork]:
    """Async context manager providing a Unit of Work across all aggregate repositories.

    Transaction boundaries are explicit:
    - Call `await uow.commit()` to persist changes.
    - Exiting without an explicit commit or upon exception triggers automatic rollback.
    - Session is guaranteed to be closed and connection released to the pool on exit.
    """
    factory = session_factory or create_session_factory()
    uow = SqlUnitOfWork(factory)
    async with uow:
        yield uow
