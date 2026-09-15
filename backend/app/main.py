"""The FastAPI app factory (§2, §13.7, §17).

`create_app` wires settings (with fail-fast `validate_runtime`), structured
logging, CORS from configuration, the database engine `/readyz` probes, and
the health routes. Every other router lands here as its task builds it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.middleware.cors import CORSMiddleware

from app.api.approvals import router as approvals_router
from app.api.errors import register_error_handlers
from app.api.health import discover_alembic_head
from app.api.health import router as health_router
from app.config import Settings, get_settings
from app.logging_config import configure_logging

if TYPE_CHECKING:
    from app.execution.approvals import ApprovalService
    from app.execution.recovery import RunDriver
    from app.runtime import Clock

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def create_app(
    settings: Settings | None = None,
    *,
    approval_service: ApprovalService | None = None,
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
        try:
            yield
        finally:
            await app.state.db_engine.dispose()

    app = FastAPI(title="OpsPilot AI", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db_engine = create_async_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True
    )
    app.state.alembic_head_revision = discover_alembic_head(BACKEND_ROOT)
    app.state.approval_service = approval_service
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

    return app


app = create_app()
