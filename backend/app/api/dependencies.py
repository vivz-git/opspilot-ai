"""FastAPI dependency injection wiring for the API layer (§13.7, §16.6).

Provides:
- `get_approval_service`: yields the singleton/configured `ApprovalService` without
  duplicating persistence, driver, or lease setup.
- `require_authorization`: enforces the v1 authentication boundary.
"""

from __future__ import annotations

from typing import cast

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent.graph import create_agent_graph
from app.config import Settings
from app.errors import PolicyViolation
from app.execution.approvals import ApprovalService
from app.execution.leases import LeaseConfig
from app.execution.recovery import LangGraphRunDriver, RunDriver
from app.persistence.protocols import UnitOfWorkFactory
from app.persistence.repositories import SqlUnitOfWork
from app.persistence.session import create_session_factory
from app.runtime import SystemClock

__all__ = [
    "get_approval_service",
    "require_authorization",
]


def get_approval_service(request: Request) -> ApprovalService:
    """Provide the ApprovalService from application state or construct it lazily."""
    service: ApprovalService | None = getattr(request.app.state, "approval_service", None)
    if service is not None:
        return service

    engine: AsyncEngine = request.app.state.db_engine
    settings: Settings = request.app.state.settings

    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        session_factory = create_session_factory(engine)
        request.app.state.session_factory = session_factory

    typed_session_factory = cast(async_sessionmaker[AsyncSession], session_factory)

    def _uow_factory() -> SqlUnitOfWork:
        return SqlUnitOfWork(typed_session_factory)

    uow_factory: UnitOfWorkFactory = _uow_factory

    driver: RunDriver | None = getattr(request.app.state, "run_driver", None)
    if driver is None:
        checkpointer = getattr(request.app.state, "checkpointer", None)
        graph = create_agent_graph(
            checkpointer=checkpointer,
            uow_factory=uow_factory,
            approval_ttl=settings.approval_ttl,
        )
        driver = LangGraphRunDriver(graph)
        request.app.state.run_driver = driver

    clock = getattr(request.app.state, "clock", None) or SystemClock()
    lease = LeaseConfig.from_settings(settings)

    service = ApprovalService(
        uow_factory=uow_factory,
        driver=driver,
        clock=clock,
        lease=lease,
    )
    request.app.state.approval_service = service
    return service


def require_authorization(request: Request) -> None:
    """Enforce the existing authorization boundary (§16.6).

    In v1 development mode (`auth_mode` is None), access is permitted for local operation.
    When `auth_mode` is configured, an `Authorization` header is required.
    """
    settings: Settings = request.app.state.settings
    if settings.auth_mode is not None:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.strip():
            raise PolicyViolation("Authorization header required under configured auth_mode")
