"""LangGraph Postgres checkpointer wiring (§2.2, §6.4, §12.1, ADR-001, ADR-011).

The checkpoint is the **resumable execution state** (ADR-012): whatever the
graph had committed when the process died is what a new worker resumes
from. This module is the only place that knows how that state reaches
Postgres.

**Which saver.** `langgraph-checkpoint-postgres` ships `AsyncPostgresSaver`
over psycopg 3 (`AsyncConnection` / `AsyncConnectionPool`). It is not
pluggable onto SQLAlchemy or asyncpg, which is why `psycopg[binary,pool]` is
a dependency alongside `asyncpg`: the control plane talks to Postgres
through SQLAlchemy + asyncpg, the checkpointer through psycopg, and both
derive their connection target from the one `Settings.database_url`
(§17.1). Two drivers, one database, one configuration surface.

**Which schema.** The saver's DDL is unqualified (`CREATE TABLE IF NOT
EXISTS checkpoints …`), so its tables land wherever the connection's
`search_path` points. Every checkpointer connection is opened with
`search_path=langgraph`, which is what makes ADR-011 true in practice: the
saver owns the `langgraph` schema, Alembic never touches it, and a LangGraph
upgrade cannot collide with one of our revisions. `CREATE SCHEMA IF NOT
EXISTS langgraph` is issued here, at checkpointer start-up, for the same
reason — FOUND-003's first migration deliberately creates only `opspilot`
and `mock_crm`.

**Setup.** `AsyncPostgresSaver.setup()` runs the library's own versioned
migrations (`checkpoint_migrations`) and must be called before first use.
It is idempotent for a single caller but not concurrency-safe across
processes (`CREATE TABLE IF NOT EXISTS` followed by an `INSERT` into the
versions table), so it runs under a session-level advisory lock taken on a
dedicated connection — two API processes starting simultaneously serialize
their bootstrap instead of racing it.

**Identity.** `thread_id == agent_runs.id` (§12.3): one identifier for the
run everywhere. `thread_config(run_id)` is the only way to build a graph
config, so no call site can spell the mapping differently.

**Durability.** LangGraph's default (`"async"`) submits each step's
checkpoint write in the background and starts the next step immediately,
so a process can die having executed a node whose *predecessor's*
checkpoint never landed. That is survivable — recovery would re-execute one
node more, and re-execution is already idempotent by design (§9.7, §10.4) —
but it makes "the checkpoint is authoritative for resumption" (ADR-012)
only approximately true. Every OpsPilot invocation therefore passes
`DURABILITY = "sync"`: the checkpoint is committed before the next step
starts, at the cost of one round-trip per step. That is the right trade for
a system whose whole claim is that a run can be interrupted anywhere.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, Final

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Durability
from psycopg import AsyncConnection
from psycopg import errors as psycopg_errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.engine import make_url

from app.config import Settings

__all__ = [
    "DURABILITY",
    "LANGGRAPH_SCHEMA",
    "libpq_conninfo",
    "open_checkpointer",
    "run_id_from_config",
    "thread_config",
]

LANGGRAPH_SCHEMA = "langgraph"

#: See the module docstring. Pass as `graph.ainvoke(..., durability=DURABILITY)`.
DURABILITY: Final[Durability] = "sync"

#: Arbitrary but fixed: the advisory-lock key that serializes checkpointer
#: bootstrap across processes. Session-level (not transaction-level) because
#: the saver's `setup()` runs in autocommit mode on its own connections.
_BOOTSTRAP_LOCK_KEY = 0x0D_B0_07

_log = structlog.get_logger("opspilot.checkpointing")


def libpq_conninfo(database_url: str) -> str:
    """Turn the SQLAlchemy URL in `Settings.database_url` into the libpq
    connection string psycopg expects — same host, database and
    credentials, no `+asyncpg` driver suffix."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def thread_config(run_id: uuid.UUID | str) -> RunnableConfig:
    """The graph config for a run. `thread_id` is the run id (§12.3)."""
    return {"configurable": {"thread_id": str(run_id)}}


def run_id_from_config(config: RunnableConfig) -> uuid.UUID:
    """The inverse of `thread_config`, for code handed a config by LangGraph."""
    configurable: dict[str, Any] = config.get("configurable") or {}
    return uuid.UUID(str(configurable["thread_id"]))


@asynccontextmanager
async def open_checkpointer(
    settings: Settings,
    *,
    min_size: int = 1,
    max_size: int = 4,
) -> AsyncIterator[AsyncPostgresSaver]:
    """Open a pooled `AsyncPostgresSaver` bound to the `langgraph` schema.

    Creates the schema and runs the saver's own setup on entry; closes the
    pool on exit. Must be entered inside a running event loop (the saver
    binds to it), so the intended home is the application lifespan
    (API-007) or a test fixture — never module import time.
    """
    conninfo = libpq_conninfo(settings.database_url.get_secret_value())
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        conninfo,
        open=False,
        min_size=min_size,
        max_size=max_size,
        kwargs={
            # The saver's documented connection requirements (`from_conn_string`
            # uses exactly these), plus the schema pin that makes ADR-011 hold.
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": f"-c search_path={LANGGRAPH_SCHEMA}",
        },
    )
    await pool.open()
    try:
        await _bootstrap(conninfo, pool)
        yield AsyncPostgresSaver(pool)
    finally:
        await pool.close()


async def _bootstrap(conninfo: str, pool: AsyncConnectionPool[Any]) -> None:
    # The lock lives on its own connection, outside the pool, so bootstrap
    # can never deadlock against a pool sized at one — and so the lock is
    # released by the connection closing even if `setup()` raises.
    async with await AsyncConnection.connect(conninfo, autocommit=True) as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock(%s)", (_BOOTSTRAP_LOCK_KEY,))
        try:
            await _ensure_schema(pool)
            await AsyncPostgresSaver(pool).setup()
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (_BOOTSTRAP_LOCK_KEY,))
    _log.info("checkpointer_ready", schema=LANGGRAPH_SCHEMA)


async def _ensure_schema(pool: AsyncConnectionPool[Any]) -> None:
    # `IF NOT EXISTS` is not race-free in Postgres: two sessions can both
    # pass the existence check and one loses on `pg_namespace`'s unique
    # index. The schema exists either way. (The advisory lock in
    # `_bootstrap` already prevents this within OpsPilot; this covers an
    # operator creating the schema by hand at the same moment.)
    async with pool.connection() as conn:
        with suppress(psycopg_errors.UniqueViolation):
            await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {LANGGRAPH_SCHEMA}")
