"""DB-007 — LangGraph Postgres checkpointer wiring (§2.2, §6.4, §12.1, ADR-011).

Against a real Postgres and the real `AsyncPostgresSaver`:

1. the saver's tables live in the `langgraph` schema and nowhere else;
2. a checkpoint is created by running a graph and can be loaded back;
3. run/checkpoint identity is stable — `thread_id` *is* `agent_runs.id`;
4. bootstrap is idempotent and safe to run concurrently.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from app.persistence.checkpointing import (
    LANGGRAPH_SCHEMA,
    libpq_conninfo,
    open_checkpointer,
    run_id_from_config,
    thread_config,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import (
    Harness,
    migrate_to_head,
    require_database,
    settings,
)

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
def _database() -> None:
    """Integration classes opt in via `usefixtures`; the pure-function classes
    below run everywhere, database or not."""
    require_database()
    migrate_to_head()


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(settings().database_url.get_secret_value(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    async with open_checkpointer(settings()) as saver:
        yield saver


class TestConfigIdentity:
    """Pure functions; no database."""

    def test_thread_id_is_the_run_id(self) -> None:
        run_id = uuid.uuid4()
        config = thread_config(run_id)
        assert config["configurable"]["thread_id"] == str(run_id)
        assert run_id_from_config(config) == run_id

    def test_conninfo_drops_the_sqlalchemy_driver_suffix_and_keeps_the_target(self) -> None:
        conninfo = libpq_conninfo("postgresql+asyncpg://user:s3cret@db.example:5433/opspilot")
        assert conninfo == "postgresql://user:s3cret@db.example:5433/opspilot"


@pytest.mark.usefixtures("_database")
class TestSchemaOwnership:
    async def test_saver_tables_live_in_the_langgraph_schema_only(
        self, checkpointer: AsyncPostgresSaver, engine: AsyncEngine
    ) -> None:
        async with engine.connect() as conn:
            rows = await conn.execute(
                sa.text(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE table_name LIKE 'checkpoint%'"
                )
            )
            placement = {name: schema for schema, name in rows}
        assert set(placement) >= {
            "checkpoints",
            "checkpoint_blobs",
            "checkpoint_writes",
            "checkpoint_migrations",
        }
        assert set(placement.values()) == {LANGGRAPH_SCHEMA}

    async def test_bootstrap_is_idempotent_and_concurrency_safe(self) -> None:
        # Two processes starting at once must not race the saver's own
        # `CREATE TABLE IF NOT EXISTS` + versions insert; the advisory lock in
        # `open_checkpointer` serializes them. Three concurrent opens, then
        # one more, must all succeed and leave exactly one migrations ledger.
        async def open_and_close() -> None:
            async with open_checkpointer(settings()):
                pass

        await asyncio.gather(open_and_close(), open_and_close(), open_and_close())
        async with open_checkpointer(settings()) as saver:
            async with saver.conn.connection() as conn:  # type: ignore[union-attr]
                cur = await conn.execute("SELECT count(*) AS n FROM checkpoint_migrations")
                row = await cur.fetchone()
            assert row is not None
            assert row["n"] == len(saver.MIGRATIONS)


@pytest.mark.usefixtures("_database")
class TestCheckpointLifecycle:
    async def test_running_a_graph_creates_a_loadable_checkpoint_under_the_run_id(
        self, checkpointer: AsyncPostgresSaver
    ) -> None:
        run_id = uuid.uuid4()
        config = thread_config(run_id)
        assert await checkpointer.aget_tuple(config) is None

        graph = Harness().build(checkpointer)
        await graph.ainvoke({"run_id": str(run_id), "log": []}, config)

        stored = await checkpointer.aget_tuple(config)
        assert stored is not None
        assert stored.config["configurable"]["thread_id"] == str(run_id)
        assert stored.checkpoint["channel_values"]["log"] == ["prepare", "work", "finish"]

        # Loading through the graph, as the reconciler does, sees the same thing.
        snapshot = await graph.aget_state(config)
        assert snapshot.values["log"] == ["prepare", "work", "finish"]
        assert snapshot.values["status"] == "completed"
        assert snapshot.next == ()

    async def test_checkpoint_identity_is_stable_across_saver_instances(
        self, checkpointer: AsyncPostgresSaver
    ) -> None:
        run_id = uuid.uuid4()
        config = thread_config(run_id)
        graph = Harness().build(checkpointer)
        await graph.ainvoke({"run_id": str(run_id), "log": []}, config)
        first = await checkpointer.aget_tuple(config)
        assert first is not None

        # A second process opens its own saver and finds the same checkpoint
        # by the same key — the run id — with the same checkpoint id.
        async with open_checkpointer(settings()) as other:
            second = await other.aget_tuple(thread_config(run_id))
        assert second is not None
        assert (
            second.config["configurable"]["checkpoint_id"]
            == (first.config["configurable"]["checkpoint_id"])
        )
        assert second.checkpoint["channel_values"] == first.checkpoint["channel_values"]

    async def test_different_runs_never_share_a_checkpoint(
        self, checkpointer: AsyncPostgresSaver
    ) -> None:
        graph = Harness().build(checkpointer)
        a, b = uuid.uuid4(), uuid.uuid4()
        await graph.ainvoke({"run_id": str(a), "log": ["a"]}, thread_config(a))
        assert await checkpointer.aget_tuple(thread_config(b)) is None
        await graph.ainvoke({"run_id": str(b), "log": ["b"]}, thread_config(b))
        snap_a = await graph.aget_state(thread_config(a))
        snap_b = await graph.aget_state(thread_config(b))
        assert snap_a.values["log"][0] == "a"
        assert snap_b.values["log"][0] == "b"
