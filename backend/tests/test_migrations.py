"""Alembic migrations against a real database (§12.1, ADR-011; FOUND-003).

Requires Postgres (`pytest.ini_options` marks it `integration`). Skips
cleanly when no database is reachable, the same way
`test_structure.py::test_mock_integrations_cannot_reach_the_network` skips
until its prerequisite lands — TEST-001 will replace this ad hoc connectivity
probe with the project's real Postgres test fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from app.config import get_settings
from sqlalchemy.exc import OperationalError, SQLAlchemyError

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _sync_engine(*, connect_timeout: int | None = None) -> sa.Engine:
    connect_args: dict[str, int] = {"connect_timeout": connect_timeout} if connect_timeout else {}
    return sa.create_engine(
        _sync_url(get_settings().database_url.get_secret_value()), connect_args=connect_args
    )


def _require_database() -> None:
    try:
        with _sync_engine(connect_timeout=3).connect():
            pass
    except OperationalError:
        pytest.skip("no reachable Postgres for this session (FOUND-003 integration test)")


def _alembic_config() -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url.get_secret_value())
    return config


def _schema_names() -> set[str]:
    with _sync_engine().connect() as conn:
        rows = conn.execute(sa.text("SELECT schema_name FROM information_schema.schemata"))
        return {row[0] for row in rows}


def _script_head(config: Config) -> str | None:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(config).get_current_head()


def _current_revision() -> str | None:
    with _sync_engine().connect() as conn:
        try:
            result = conn.execute(sa.text("SELECT version_num FROM alembic_version"))
            return result.scalar_one_or_none()
        except SQLAlchemyError:
            return None


@pytest.fixture(scope="module", autouse=True)
def _skip_without_database() -> None:
    """One connectivity probe for the whole module, not one per test — a
    dual-stack (IPv6+IPv4) connection failure alone takes several seconds."""
    _require_database()


class TestSchemaMigrations:
    def test_upgrade_head_creates_both_owned_schemas(self) -> None:
        config = _alembic_config()
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        assert {"opspilot", "mock_crm"} <= _schema_names()

    def test_a_second_upgrade_head_is_a_no_op(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        before = _current_revision()
        command.upgrade(config, "head")
        assert _current_revision() == before

    def test_downgrade_base_drops_both_schemas_and_is_clean(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        schemas = _schema_names()
        assert "opspilot" not in schemas
        assert "mock_crm" not in schemas
        command.upgrade(config, "head")  # leave the database migrated

    def test_langgraph_schema_is_never_created_by_our_migrations(self) -> None:
        """§12.1 — langgraph is the checkpointer's own schema; we must never
        create or touch it (ADR-011)."""
        config = _alembic_config()
        command.upgrade(config, "head")
        assert "langgraph" not in _schema_names()


def _table_names(schema: str) -> set[str]:
    with _sync_engine().connect() as conn:
        inspector = sa.inspect(conn)
        return set(inspector.get_table_names(schema=schema))


class TestControlPlaneTablesMigration:
    """DB-001 — `agent_runs`, `execution_steps`, `tool_calls`, `approvals`
    (§12.3-12.6). The migration must build on FOUND-003 and be reversible
    without touching the schemas that revision owns."""

    _DB001_TABLES = frozenset({"agent_runs", "execution_steps", "tool_calls", "approvals"})

    def test_upgrade_head_creates_all_four_control_plane_tables(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        assert _table_names("opspilot") >= self._DB001_TABLES

    def test_downgrade_to_the_previous_revision_drops_only_the_new_tables(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        command.downgrade(config, "19463f144188")
        tables = _table_names("opspilot")
        assert not (self._DB001_TABLES & tables)
        assert "opspilot" in _schema_names()  # FOUND-003's schema itself is untouched
        command.upgrade(config, "head")  # leave the database migrated

    def test_a_second_upgrade_head_after_downgrade_reproduces_the_same_schema(self) -> None:
        """Determinism: downgrade then re-upgrade must not leave stray state
        (e.g. a duplicate constraint name) behind."""
        config = _alembic_config()
        command.upgrade(config, "head")
        command.downgrade(config, "19463f144188")
        command.upgrade(config, "head")
        assert _table_names("opspilot") >= self._DB001_TABLES
        # Not the literal head id: DB-002 added a revision on top of this
        # one, so "head" has moved on. What this test actually pins is that
        # re-upgrading through this revision is deterministic, which
        # `_table_names` already established; re-check against Alembic's own
        # notion of head instead of a hardcoded id that would go stale again
        # each time a later migration lands.
        config = _alembic_config()
        assert _current_revision() == _script_head(config)


class TestTraceEventsMigration:
    """DB-002 — `trace_events` (§12.7). Must build on DB-001's head and be
    reversible without touching the four control-plane tables it revises."""

    _DB001_TABLES = frozenset({"agent_runs", "execution_steps", "tool_calls", "approvals"})

    def test_upgrade_head_creates_trace_events(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        assert "trace_events" in _table_names("opspilot")

    def test_downgrade_to_the_previous_revision_drops_only_trace_events(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        command.downgrade(config, "c6d1db7aa718")
        tables = _table_names("opspilot")
        assert "trace_events" not in tables
        assert tables >= self._DB001_TABLES  # DB-001's tables are untouched
        command.upgrade(config, "head")  # leave the database migrated

    def test_a_second_upgrade_head_is_a_true_no_op(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        before = _current_revision()
        command.upgrade(config, "head")
        assert _current_revision() == before

    def test_downgrade_then_reupgrade_reproduces_the_same_schema(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        command.downgrade(config, "c6d1db7aa718")
        command.upgrade(config, "head")
        assert "trace_events" in _table_names("opspilot")
        assert _current_revision() == _script_head(config)


def _foreign_key_names(schema: str, table: str) -> set[str]:
    with _sync_engine().connect() as conn:
        inspector = sa.inspect(conn)
        return {
            name
            for fk in inspector.get_foreign_keys(table, schema=schema)
            if (name := fk["name"]) is not None
        }


class TestEvaluationTablesMigration:
    """DB-003 — `evaluation_runs`, `evaluation_results` (§12.8), plus the
    deferred `agent_runs.evaluation_run_id` FK that DB-001 left off because
    `evaluation_runs` did not exist yet. Must build on DB-002's head and be
    reversible without touching the tables it revises."""

    _PRIOR_TABLES = frozenset({"agent_runs", "execution_steps", "tool_calls", "approvals"})
    _DB003_TABLES = frozenset({"evaluation_runs", "evaluation_results"})

    def test_upgrade_head_creates_both_evaluation_tables(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        assert _table_names("opspilot") >= self._DB003_TABLES

    def test_upgrade_head_adds_the_deferred_agent_runs_foreign_key(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        assert "fk_agent_runs_evaluation_run_id_evaluation_runs" in _foreign_key_names(
            "opspilot", "agent_runs"
        )

    def test_downgrade_to_the_previous_revision_drops_only_the_new_tables_and_fk(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        command.downgrade(config, "21765d8fa136")
        tables = _table_names("opspilot")
        assert not (self._DB003_TABLES & tables)
        assert tables >= self._PRIOR_TABLES  # earlier revisions' tables are untouched
        assert "trace_events" in tables  # DB-002's table is untouched
        assert "fk_agent_runs_evaluation_run_id_evaluation_runs" not in _foreign_key_names(
            "opspilot", "agent_runs"
        )
        command.upgrade(config, "head")  # leave the database migrated

    def test_a_second_upgrade_head_is_a_true_no_op(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        before = _current_revision()
        command.upgrade(config, "head")
        assert _current_revision() == before

    def test_downgrade_then_reupgrade_reproduces_the_same_schema(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        command.downgrade(config, "21765d8fa136")
        command.upgrade(config, "head")
        assert _table_names("opspilot") >= self._DB003_TABLES
        assert "fk_agent_runs_evaluation_run_id_evaluation_runs" in _foreign_key_names(
            "opspilot", "agent_runs"
        )
        assert _current_revision() == _script_head(config)
