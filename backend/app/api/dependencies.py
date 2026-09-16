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
from app.execution.trace import TraceService
from app.persistence.protocols import UnitOfWorkFactory
from app.persistence.repositories import SqlUnitOfWork
from app.persistence.session import create_session_factory
from app.runtime import SystemClock

__all__ = [
    "get_approval_service",
    "get_trace_service",
    "require_authorization",
]


def _get_uow_factory(request: Request) -> UnitOfWorkFactory:
    """The shared, lazily-cached `UnitOfWorkFactory` every API dependency
    builds its repository access from — one session factory per app, never a
    session held across requests."""
    engine: AsyncEngine = request.app.state.db_engine
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        session_factory = create_session_factory(engine)
        request.app.state.session_factory = session_factory

    typed_session_factory = cast(async_sessionmaker[AsyncSession], session_factory)

    def _uow_factory() -> SqlUnitOfWork:
        return SqlUnitOfWork(typed_session_factory)

    return _uow_factory


def get_approval_service(request: Request) -> ApprovalService:
    """Provide the ApprovalService from application state or construct it lazily."""
    service: ApprovalService | None = getattr(request.app.state, "approval_service", None)
    if service is not None:
        return service

    settings: Settings = request.app.state.settings
    uow_factory = _get_uow_factory(request)

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


def get_trace_service(request: Request) -> TraceService:
    """Provide the TraceService (API-003) from application state or construct
    it lazily. Read-only: shares the same `UnitOfWorkFactory` as every other
    dependency, never a driver, checkpointer, or lease."""
    service: TraceService | None = getattr(request.app.state, "trace_service", None)
    if service is not None:
        return service

    settings: Settings = request.app.state.settings
    uow_factory = _get_uow_factory(request)

    service = TraceService(
        uow_factory=uow_factory, payload_max_bytes=settings.trace_payload_max_bytes
    )
    request.app.state.trace_service = service
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
