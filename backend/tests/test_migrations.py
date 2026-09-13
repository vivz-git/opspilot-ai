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

    def test_langgraph_schema_is_never_created_or_dropped_by_our_migrations(self) -> None:
        """§12.1 — langgraph is the checkpointer's own schema; we must never
        create or touch it (ADR-011). Since DB-007 the saver creates it at
        checkpointer start-up (`app.persistence.checkpointing`), so it may
        legitimately exist in a database the suite has already used — what
        must hold is that a full downgrade/upgrade cycle neither removes nor
        adds it."""
        config = _alembic_config()
        command.upgrade(config, "head")
        before = "langgraph" in _schema_names()
        command.downgrade(config, "base")
        assert ("langgraph" in _schema_names()) is before
        command.upgrade(config, "head")
        assert ("langgraph" in _schema_names()) is before


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


class TestMockCrmTablesMigration:
    """DB-004 — `companies`, `leads`, `customers`, `outreach_drafts`, `email_outbox`
    (§12.9). The migration must build on DB-003 ('62fe5fff7640') and be reversible
    without touching the schemas or control-plane tables DB-001..003 own."""

    _MOCK_CRM_TABLES = {
        "companies",
        "leads",
        "customers",
        "outreach_drafts",
        "email_outbox",
    }

    def test_upgrade_head_creates_mock_crm_tables(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        assert _table_names("mock_crm") >= self._MOCK_CRM_TABLES
        assert _current_revision() == _script_head(config)

    def test_a_second_upgrade_head_is_a_no_op(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        before = _current_revision()
        command.upgrade(config, "head")
        assert _current_revision() == before

    def test_downgrade_to_db003_drops_only_mock_crm_tables(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        command.downgrade(config, "62fe5fff7640")

        # mock_crm tables dropped
        assert not (_table_names("mock_crm") & self._MOCK_CRM_TABLES)
        # schemas still exist
        assert {"opspilot", "mock_crm"} <= _schema_names()
        # DB-001..003 tables in opspilot still exist
        assert _table_names("opspilot") >= {
            "agent_runs",
            "execution_steps",
            "tool_calls",
            "approvals",
            "trace_events",
            "evaluation_runs",
            "evaluation_results",
        }
        assert _current_revision() == "62fe5fff7640"
        command.upgrade(config, "head")

    def test_downgrade_then_reupgrade_reproduces_the_same_schema(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        command.downgrade(config, "62fe5fff7640")
        command.upgrade(config, "head")
        assert _table_names("mock_crm") >= self._MOCK_CRM_TABLES
        assert _current_revision() == _script_head(config)


def _column_names(schema: str, table: str) -> set[str]:
    with _sync_engine().connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = :table"
            ),
            {"schema": schema, "table": table},
        )
        return {row[0] for row in rows}


def _check_constraint(schema: str, table: str, name: str) -> str | None:
    with _sync_engine().connect() as conn:
        return conn.execute(
            sa.text(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = :schema AND t.relname = :table AND c.conname = :name"
            ),
            {"schema": schema, "table": table, "name": name},
        ).scalar_one_or_none()


class TestLeaseOwnerMigration:
    """DB-007 — `agent_runs.lease_owner`, its pairing `CHECK`, and the
    `run_recovered` trace kind. Builds on DB-004 ('e35beebb06d0'); the
    `langgraph` schema is still never touched by any of our revisions
    (ADR-011) — the saver creates it at checkpointer start-up."""

    _PREVIOUS = "e35beebb06d0"
    _LEASE_CHECK = "ck_agent_runs_lease_owner_and_expiry_together"
    _KIND_CHECK = "ck_trace_events_trace_event_kind"

    def test_upgrade_head_adds_lease_owner_and_its_pairing_check(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        assert "lease_owner" in _column_names("opspilot", "agent_runs")
        check = _check_constraint("opspilot", "agent_runs", self._LEASE_CHECK)
        assert check is not None
        assert "lease_owner IS NULL" in check and "lease_expires_at IS NULL" in check
        assert _current_revision() == _script_head(config)

    def test_upgrade_head_admits_run_recovered_and_nothing_else_new(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        check = _check_constraint("opspilot", "trace_events", self._KIND_CHECK)
        assert check is not None
        assert "'run_recovered'" in check
        assert "'policy_violation'" in check

    def test_a_second_upgrade_head_is_a_no_op(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        before = _current_revision()
        command.upgrade(config, "head")
        assert _current_revision() == before

    def test_downgrade_to_db004_removes_exactly_what_was_added(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        command.downgrade(config, self._PREVIOUS)
        assert "lease_owner" not in _column_names("opspilot", "agent_runs")
        assert "lease_expires_at" in _column_names("opspilot", "agent_runs")
        assert _check_constraint("opspilot", "agent_runs", self._LEASE_CHECK) is None
        check = _check_constraint("opspilot", "trace_events", self._KIND_CHECK)
        assert check is not None and "'run_recovered'" not in check
        assert _table_names("opspilot") >= {"agent_runs", "trace_events"}
        assert _table_names("mock_crm") >= {"companies", "email_outbox"}
        assert _current_revision() == self._PREVIOUS
        command.upgrade(config, "head")

    def test_downgrade_then_reupgrade_reproduces_the_same_schema(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        command.downgrade(config, self._PREVIOUS)
        command.upgrade(config, "head")
        assert "lease_owner" in _column_names("opspilot", "agent_runs")
        assert _check_constraint("opspilot", "agent_runs", self._LEASE_CHECK) is not None
        check = _check_constraint("opspilot", "trace_events", self._KIND_CHECK)
        assert check is not None and "'run_recovered'" in check
        assert _current_revision() == _script_head(config)


class TestNoAutogenerateDrift:
    """`alembic check` must report zero drift between `target_metadata` and
    the live schema — the same check CI/a developer would run by hand.

    This is a regression test for a real bug found while adding DB-003, not
    a generic sanity check: the default Postgres `search_path`
    (`"$user", public`) makes a role named `opspilot` resolve `opspilot` as
    the connection's *ambient default* schema — the same name as our real
    schema (every model in `docker-compose.yml`/`.env.example` uses
    `POSTGRES_USER=opspilot`). Unqualified reflection of that ambient
    default then reports `schema=None` for objects that are actually in
    `opspilot`, which `alembic`'s autogenerate comparator treats as a
    *different* table/FK identity than the metadata's explicit
    `schema="opspilot"` — so every single foreign key in the schema showed
    up as simultaneously removed and re-added, even though nothing was
    actually wrong. `alembic/env.py` fixes this by pinning the migration
    connection's `search_path` to `public` (a schema with no ORM tables, so
    it can never collide with a real one) and passing
    `include_schemas=True` so `opspilot`/`mock_crm` are compared at all.
    This test exists so a future change to `env.py`, the connection URL's
    role name, or the schema layout that reintroduces the ambiguity fails
    loudly here instead of being noticed only as "cosmetic" noise in local
    `alembic check` output and silently tolerated.
    """

    def test_alembic_check_reports_no_drift_at_head(self) -> None:
        config = _alembic_config()
        command.upgrade(config, "head")
        command.check(config)  # raises AutogenerateDiffsDetected on any drift
