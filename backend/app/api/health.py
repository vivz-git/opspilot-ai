"""Health and readiness endpoints (§13.7).

`/healthz` is liveness: the process is running, nothing else. `/readyz` is
readiness: the database must be reachable and, once Alembic is wired up
(FOUND-003), at the migration head — otherwise a container with a stale
schema never receives traffic. Until then `expected_head` is `None` and
readiness falls back to a pure connectivity check, so this endpoint needs no
further change when FOUND-003 lands.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])

READINESS_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    reason: str | None = None
    detail: str | None = None
    head_revision: str | None = None
    current_revision: str | None = None


def discover_alembic_head(backend_root: Path) -> str | None:
    """The migration head this codebase declares, if Alembic is wired up.

    Returns `None` before FOUND-003 adds `alembic.ini`, in which case
    readiness cannot assert a head and falls back to connectivity only.
    """
    ini_path = backend_root / "alembic.ini"
    if not ini_path.exists():
        return None

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ini_path))
    script = ScriptDirectory.from_config(config)
    return script.get_current_head()


def get_db_engine(request: Request) -> AsyncEngine:
    return request.app.state.db_engine  # type: ignore[no-any-return]


def get_alembic_head(request: Request) -> str | None:
    return request.app.state.alembic_head_revision  # type: ignore[no-any-return]


async def _current_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        try:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            return result.scalar_one_or_none()
        except SQLAlchemyError:
            # No alembic_version table: no migration has ever been applied.
            return None


async def check_readiness(
    engine: AsyncEngine,
    expected_head: str | None,
    *,
    timeout_seconds: float = READINESS_TIMEOUT_SECONDS,
) -> ReadinessResult:
    try:
        async with asyncio.timeout(timeout_seconds):
            current = await _current_revision(engine)
    except (TimeoutError, SQLAlchemyError, OSError) as exc:
        logger.warning("readiness_check_failed", reason="database_unreachable", error=str(exc))
        return ReadinessResult(
            ready=False, reason="database_unreachable", detail="database is unreachable"
        )

    if expected_head is not None and current != expected_head:
        logger.warning(
            "readiness_check_failed",
            reason="migrations_pending",
            head_revision=expected_head,
            current_revision=current,
        )
        return ReadinessResult(
            ready=False,
            reason="migrations_pending",
            detail="database migrations have not been applied",
            head_revision=expected_head,
            current_revision=current,
        )

    return ReadinessResult(ready=True, head_revision=expected_head, current_revision=current)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness. Process only, no dependencies (§13.7)."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(
    engine: AsyncEngine = Depends(get_db_engine),
    expected_head: str | None = Depends(get_alembic_head),
) -> JSONResponse:
    result = await check_readiness(engine, expected_head)
    if not result.ready:
        return JSONResponse(
            status_code=503,
            media_type="application/problem+json",
            content={
                "type": "https://opspilot.dev/errors/integration-unavailable",
                "title": "Integration unavailable",
                "status": 503,
                "detail": result.detail,
                "code": "integration_unavailable",
            },
        )
    return JSONResponse(
        status_code=200,
        content={"status": "ready", "revision": result.current_revision},
    )
