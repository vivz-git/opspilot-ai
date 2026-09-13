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
