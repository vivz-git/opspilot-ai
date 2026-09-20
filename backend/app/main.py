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
from fastapi import APIRouter, Depends, FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.middleware.cors import CORSMiddleware

from app.api.approvals import router as approvals_router
from app.api.dependencies import require_authorization, wire_runtime
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
    from app.evaluation.runner import EvaluationRunner
    from app.execution.approvals import ApprovalService
    from app.execution.recovery import RunDriver
    from app.execution.runs import RunService
    from app.runtime import Clock

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def create_app(
    settings: Settings | None = None,
    *,
    approval_service: ApprovalService | None = None,
    run_service: RunService | None = None,
    evaluation_runner: EvaluationRunner | None = None,
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
                app.state.checkpointer = checkpointer
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

    # The schema and its two viewers are registered by hand, below, so that
    # they sit behind `require_authorization` like every other route. FastAPI
    # mounts its built-ins as plain Starlette routes, which no router
    # dependency can reach — so under `auth_mode=proxy` they answered an
    # unauthenticated caller with the full shape of the API while every
    # operational route correctly refused it. Only `/healthz` and `/readyz`
    # belong outside the fence (docs/deployment.md §4).
    app = FastAPI(
        title="OpsPilot AI",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.db_engine = create_async_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True
    )
    app.state.alembic_head_revision = discover_alembic_head(BACKEND_ROOT)
    app.state.approval_service = approval_service
    app.state.run_service = run_service
    app.state.evaluation_runner = evaluation_runner
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

    app.include_router(_schema_router(app))
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


def _schema_router(app: FastAPI) -> APIRouter:
    """`/openapi.json`, `/docs` and `/redoc`, fenced like the rest of the API.

    In the localhost shape `require_authorization` is a no-op, so these behave
    exactly as FastAPI's built-ins did. Under `auth_mode=proxy` they refuse a
    request that did not come through the access proxy, which is the whole
    point of the fuse: a bypassed boundary should not hand out the API's
    shape any more than it hands out a run.
    """
    router = APIRouter(dependencies=[Depends(require_authorization)], include_in_schema=False)
    schema_url = "/openapi.json"

    @router.get(schema_url)
    async def openapi_schema() -> dict[str, object]:
        return app.openapi()

    @router.get("/docs")
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url=schema_url, title=f"{app.title} — Swagger UI")

    @router.get("/redoc")
    async def redoc_ui() -> HTMLResponse:
        return get_redoc_html(openapi_url=schema_url, title=f"{app.title} — ReDoc")

    return router


app = create_app()
