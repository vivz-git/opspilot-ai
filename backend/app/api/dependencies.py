"""FastAPI dependency injection wiring for the API layer (§13.7, §16.6).

Provides:
- `wire_runtime`: the composition root (§2.1) the lifespan calls once the
  checkpointer is open — one graph, one driver, one executor, one
  reconciler, and the two services that share them.
- `get_approval_service` / `get_run_service`: yield the wired services, or
  construct control-plane-only ones lazily when the lifespan could not wire
  the runtime (no database at start-up; `/readyz` reports that).
- `require_authorization`: enforces the v1 authentication boundary.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI, Request
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent.graph import create_agent_graph
from app.config import Settings
from app.errors import PolicyViolation
from app.execution.approvals import ApprovalService
from app.execution.executor import Executor
from app.execution.leases import LeaseConfig, new_worker_id
from app.execution.recovery import LangGraphRunDriver, Reconciler, RunDriver
from app.execution.runs import RunService
from app.execution.runtime import build_driver
from app.persistence.protocols import UnitOfWorkFactory
from app.persistence.repositories import SqlUnitOfWork
from app.persistence.session import create_session_factory
from app.runtime import (
    CancellationSource,
    Clock,
    IdGenerator,
    InMemoryCancellationSource,
    SystemClock,
    UuidIdGenerator,
)

__all__ = [
    "get_approval_service",
    "get_run_service",
    "require_authorization",
    "wire_runtime",
]


def _session_factory(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    session_factory = getattr(app.state, "session_factory", None)
    if session_factory is None:
        engine: AsyncEngine = app.state.db_engine
        session_factory = create_session_factory(engine)
        app.state.session_factory = session_factory
    return cast(async_sessionmaker[AsyncSession], session_factory)


def _uow_factory(app: FastAPI) -> UnitOfWorkFactory:
    typed_session_factory = _session_factory(app)

    def factory() -> SqlUnitOfWork:
        return SqlUnitOfWork(typed_session_factory)

    return factory


def _runtime_primitives(app: FastAPI) -> tuple[Clock, IdGenerator, CancellationSource]:
    """The injected clock, id generator and cancellation source (FOUND-004),
    created once and pinned on `app.state` so every collaborator — service,
    graph nodes, executor — shares the same instances."""
    clock: Clock = getattr(app.state, "clock", None) or SystemClock()
    ids: IdGenerator = getattr(app.state, "id_gen", None) or UuidIdGenerator()
    cancellation_source: CancellationSource = (
        getattr(app.state, "cancellation_source", None) or InMemoryCancellationSource()
    )
    app.state.clock, app.state.id_gen = clock, ids
    app.state.cancellation_source = cancellation_source
    return clock, ids, cancellation_source


def wire_runtime(app: FastAPI, *, checkpointer: BaseCheckpointSaver[Any]) -> None:
    """Compose the production graph with its collaborators (§2.1, §6.4) and
    the execution ownership around it (§2.4). Anything injected through
    `create_app` (a service, a driver, a clock) is kept; the rest is built
    here, once, and pinned on `app.state`."""
    settings: Settings = app.state.settings
    uow_factory = _uow_factory(app)
    clock, ids, cancellation_source = _runtime_primitives(app)
    lease = LeaseConfig.from_settings(settings)

    driver: RunDriver | None = getattr(app.state, "run_driver", None)
    if driver is None:
        driver = build_driver(
            settings,
            session_factory=_session_factory(app),
            uow_factory=uow_factory,
            checkpointer=checkpointer,
            clock=clock,
            ids=ids,
            cancellation_source=cancellation_source,
        )
        app.state.run_driver = driver

    executor: Executor | None = getattr(app.state, "executor", None)
    if executor is None:
        executor = Executor(
            uow_factory=uow_factory,
            driver=driver,
            clock=clock,
            lease=lease,
            owner=new_worker_id(ids, label="executor"),
            budgets=settings.budgets,
        )
        app.state.executor = executor

    if getattr(app.state, "run_service", None) is None:
        app.state.run_service = RunService(
            uow_factory=uow_factory,
            settings=settings,
            clock=clock,
            ids=ids,
            cancellation_source=cancellation_source,
            executor=executor,
        )
    if getattr(app.state, "approval_service", None) is None:
        app.state.approval_service = ApprovalService(
            uow_factory=uow_factory, driver=driver, clock=clock, lease=lease, ids=ids
        )
    if getattr(app.state, "reconciler", None) is None:
        app.state.reconciler = Reconciler(
            uow_factory=uow_factory,
            driver=driver,
            clock=clock,
            owner=new_worker_id(ids, label="reconciler"),
            lease=lease,
        )


def get_approval_service(request: Request) -> ApprovalService:
    """Provide the ApprovalService from application state or construct it lazily."""
    service: ApprovalService | None = getattr(request.app.state, "approval_service", None)
    if service is not None:
        return service

    app = request.app
    settings: Settings = app.state.settings
    uow_factory = _uow_factory(app)
    clock, ids, _ = _runtime_primitives(app)

    driver: RunDriver | None = getattr(app.state, "run_driver", None)
    if driver is None:
        graph = create_agent_graph(
            checkpointer=getattr(app.state, "checkpointer", None),
            uow_factory=uow_factory,
            approval_ttl=settings.approval_ttl,
        )
        driver = LangGraphRunDriver(graph)
        app.state.run_driver = driver

    service = ApprovalService(
        uow_factory=uow_factory,
        driver=driver,
        clock=clock,
        lease=LeaseConfig.from_settings(settings),
        ids=ids,
    )
    app.state.approval_service = service
    return service


def get_run_service(request: Request) -> RunService:
    """Provide the RunService from application state or construct it lazily."""
    service: RunService | None = getattr(request.app.state, "run_service", None)
    if service is not None:
        return service

    app = request.app
    clock, ids, cancellation_source = _runtime_primitives(app)
    service = RunService(
        uow_factory=_uow_factory(app),
        settings=app.state.settings,
        clock=clock,
        ids=ids,
        cancellation_source=cancellation_source,
        executor=getattr(app.state, "executor", None),
    )
    app.state.run_service = service
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
