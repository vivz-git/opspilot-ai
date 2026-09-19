"""The FastAPI app factory (§2, §13.7, §17).

`create_app` wires settings (with fail-fast `validate_runtime`), structured
logging, CORS from configuration, the database engine `/readyz` probes, and
the health routes. Every other router lands here as its task builds it.

The lifespan is where execution ownership begins and ends (§2.4, API-007):
it opens the LangGraph saver, composes the graph runtime (`wire_runtime`),
runs the startup reconciler over orphaned runs, and on shutdown stops the
executor so no run is left driven by a process that is going away.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.middleware.cors import CORSMiddleware

from app.api.approvals import router as approvals_router
from app.api.dependencies import wire_runtime
from app.api.errors import register_error_handlers
from app.api.evaluations import router as evaluations_router
from app.api.health import discover_alembic_head
from app.api.health import router as health_router
from app.api.runs import router as runs_router
from app.api.tools import router as tools_router
from app.config import Settings, get_settings
from app.execution.recovery import RecoveryOutcome
from app.logging_config import configure_logging
from app.persistence.checkpointing import open_checkpointer

if TYPE_CHECKING:
    from app.execution.approvals import ApprovalService
    from app.execution.evaluations import EvaluationService
    from app.execution.recovery import RunDriver
    from app.execution.runs import RunService
    from app.runtime import Clock

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def create_app(
    settings: Settings | None = None,
    *,
    approval_service: ApprovalService | None = None,
    run_service: RunService | None = None,
    evaluation_service: EvaluationService | None = None,
    driver: RunDriver | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_runtime()
    configure_logging(settings)
    logger = structlog.get_logger("opspilot.startup")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("startup", **settings.safe_dump())
        async with AsyncExitStack() as stack:
            # API-007: open the saver, compose the runtime, reconcile orphans
            # (§2.4). A database that is not there yet is `/readyz`'s to
            # report — the process still boots, control plane only.
            try:
                checkpointer = await stack.enter_async_context(open_checkpointer(settings))
                wire_runtime(app, checkpointer=checkpointer)
                report = await app.state.reconciler.reconcile_all()
            except Exception as exc:  # noqa: BLE001 - degraded start-up is logged, not fatal
                logger.warning("runtime_unavailable", error=repr(exc))
            else:
                logger.info(
                    "reconciled_on_startup",
                    candidates=report.candidates,
                    **{o.value: report.count(o) for o in RecoveryOutcome},
                )
            try:
                yield
            finally:
                executor = getattr(app.state, "executor", None)
                if executor is not None:
                    await executor.shutdown()
                await app.state.db_engine.dispose()

    app = FastAPI(title="OpsPilot AI", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db_engine = create_async_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True
    )
    app.state.alembic_head_revision = discover_alembic_head(BACKEND_ROOT)
    app.state.approval_service = approval_service
    app.state.run_service = run_service
    app.state.evaluation_service = evaluation_service
    app.state.run_driver = driver
    app.state.clock = clock

    register_error_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(approvals_router)
    app.include_router(approvals_router, prefix="/api/v1")
    app.include_router(runs_router)
    app.include_router(runs_router, prefix="/api/v1")
    app.include_router(evaluations_router)
    app.include_router(evaluations_router, prefix="/api/v1")
    app.include_router(tools_router)
    app.include_router(tools_router, prefix="/api/v1")

    return app


app = create_app()
