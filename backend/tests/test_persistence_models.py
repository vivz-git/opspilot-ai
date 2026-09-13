"""Control-plane persistence models against a real database (§12.3-12.6, DB-001).

Requires Postgres, like `test_migrations.py` — skips cleanly with no reachable
database, and the schema is migrated to head via the same ad hoc probe
(`TEST-001` will replace this with the project's real fixture, per the note
in `test_migrations.py`).

Each test runs inside a savepoint nested in one outer transaction per test
(SQLAlchemy's documented pattern for joining a `Session` to an external
transaction) so a `pytest.raises(IntegrityError)` — which aborts the current
savepoint — does not poison the rest of the test, and nothing written here
survives past the test.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from app.config import get_settings
from app.persistence.models import AgentRun, ApprovalRow, ExecutionStep, ToolCallRow
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _sync_engine() -> sa.Engine:
    return sa.create_engine(_sync_url(get_settings().database_url.get_secret_value()))


def _require_database() -> None:
    try:
        with sa.create_engine(
            _sync_url(get_settings().database_url.get_secret_value()),
            connect_args={"connect_timeout": 3},
        ).connect():
            pass
    except OperationalError:
        pytest.skip("no reachable Postgres for this session (DB-001 integration test)")


@pytest.fixture(scope="module", autouse=True)
def _migrated_database() -> None:
    _require_database()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url.get_secret_value())
    command.upgrade(config, "head")


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = _sync_engine()
    connection = engine.connect()
    outer_transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer_transaction.rollback()
        connection.close()
        engine.dispose()


def _make_run(**overrides: object) -> AgentRun:
    now = datetime.now(UTC)
    defaults: dict[str, object] = {
        "user_request": "find and score enterprise leads",
        "planner_kind": "rules",
        "deadline_at": now + timedelta(minutes=5),
    }
    defaults.update(overrides)
    return AgentRun(**defaults)  # type: ignore[arg-type]


def _make_step(run_id: uuid.UUID, **overrides: object) -> ExecutionStep:
    defaults: dict[str, object] = {
        "run_id": run_id,
        "step_id": "s1",
        "plan_revision": 0,
        "seq": 1,
        "tool": "search_leads",
    }
    defaults.update(overrides)
    return ExecutionStep(**defaults)  # type: ignore[arg-type]


class TestEveryModelCanBeCreated:
    def test_agent_run(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        assert run.id is not None
        assert run.status == "created"
        assert run.plan_history == []
        assert run.metadata_ == {}

    def test_execution_step(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        step = _make_step(run.id)
        db_session.add(step)
        db_session.flush()
        assert step.id is not None
        assert step.status == "pending"
        assert step.verification_status == "not_required"

    def test_tool_call(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        step = _make_step(run.id)
        db_session.add(step)
        db_session.flush()
        call = ToolCallRow(
            run_id=run.id,
            execution_step_id=step.id,
            step_id=step.step_id,
            attempt=1,
            tool="search_leads",
            status="succeeded",
        )
        db_session.add(call)
        db_session.flush()
        assert call.id is not None

    def test_approval(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        now = datetime.now(UTC)
        approval = ApprovalRow(
            run_id=run.id,
            step_id="s2",
            tool="send_email_mock",
            risk="high",
            title="Send outreach email",
            summary="Send the drafted email to jane@example.com",
            args_hash="deadbeef",
            requested_at=now,
            expires_at=now + timedelta(hours=24),
        )
        db_session.add(approval)
        db_session.flush()
        assert approval.id is not None
        assert approval.status == "pending"


class TestEnumsAreEnforced:
    @pytest.mark.parametrize(
        ("table", "column", "value"),
        [
            # Short enough to fit every column's varchar length (the
            # smallest is `approval_risk`, sized to `medium` at 6 chars) so
            # this actually exercises the `CHECK` constraint rather than a
            # column-width `DataError`.
            ("agent_runs", "status", "bogus"),
            ("execution_steps", "status", "bogus"),
            ("execution_steps", "verification_status", "bogus"),
            ("tool_calls", "status", "bogus"),
            ("approvals", "status", "bogus"),
            ("approvals", "risk", "bogus"),
        ],
    )
    def test_invalid_enum_value_is_rejected_at_the_database(
        self, db_session: Session, table: str, column: str, value: str
    ) -> None:
        with pytest.raises(IntegrityError):
            db_session.execute(_bad_enum_insert(table, column, value))
            db_session.flush()


def _bad_enum_insert(table: str, column: str, value: str) -> sa.TextClause:
    """The minimal `INSERT` that can violate exactly one enum `CHECK`.

    Every not-null column besides the enum under test needs a value, so this
    fills the required columns per table with an innocuous constant.
    """
    required: dict[str, dict[str, str]] = {
        "agent_runs": {
            "user_request": "'x'",
            "planner_kind": "'rules'",
            "deadline_at": "now()",
        },
        "execution_steps": {
            "run_id": "gen_random_uuid()",
            "step_id": "'s1'",
            "plan_revision": "0",
            "seq": "1",
            "tool": "'search_leads'",
        },
        "tool_calls": {
            "run_id": "gen_random_uuid()",
            "execution_step_id": "gen_random_uuid()",
            "step_id": "'s1'",
            "attempt": "1",
            "tool": "'search_leads'",
        },
        "approvals": {
            "run_id": "gen_random_uuid()",
            "step_id": "'s1'",
            "tool": "'send_email_mock'",
            "risk": "'high'",
            "title": "'t'",
            "summary": "'s'",
            "args_hash": "'h'",
            "requested_at": "now()",
            "expires_at": "now()",
        },
    }
    columns = dict(required[table])
    columns[column] = f"'{value}'"
    col_list = ", ".join(columns)
    val_list = ", ".join(columns.values())
    return sa.text(f"INSERT INTO opspilot.{table} ({col_list}) VALUES ({val_list})")  # noqa: S608


class TestForeignKeysAreEnforced:
    def test_execution_step_rejects_an_unknown_run_id(self, db_session: Session) -> None:
        step = _make_step(uuid.uuid4())
        db_session.add(step)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_tool_call_rejects_an_unknown_execution_step_id(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        call = ToolCallRow(
            run_id=run.id,
            execution_step_id=uuid.uuid4(),
            step_id="s1",
            attempt=1,
            tool="search_leads",
            status="succeeded",
        )
        db_session.add(call)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_deleting_a_run_cascades_to_its_steps_calls_and_approvals(
        self, db_session: Session
    ) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        step = _make_step(run.id)
        db_session.add(step)
        db_session.flush()
        call = ToolCallRow(
            run_id=run.id,
            execution_step_id=step.id,
            step_id=step.step_id,
            attempt=1,
            tool="search_leads",
            status="succeeded",
        )
        now = datetime.now(UTC)
        approval = ApprovalRow(
            run_id=run.id,
            step_id=step.step_id,
            tool="send_email_mock",
            risk="high",
            title="t",
            summary="s",
            args_hash="h",
            requested_at=now,
            expires_at=now + timedelta(hours=1),
        )
        db_session.add_all([call, approval])
        db_session.flush()

        step_id, call_id, approval_id = step.id, call.id, approval.id

        # A plain `DELETE FROM agent_runs`, bypassing the ORM's own
        # relationship cascades entirely (none are declared — §12.1's
        # `ON DELETE CASCADE` is what must do this work). The session's
        # identity map has no way to know rows vanished underneath it, so
        # the follow-up checks query the database directly rather than
        # `Session.get`, which would just return the cached, stale objects.
        db_session.delete(run)
        db_session.flush()

        remaining = db_session.execute(
            sa.text(
                "SELECT "
                "(SELECT count(*) FROM opspilot.execution_steps WHERE id = :step_id) AS steps, "
                "(SELECT count(*) FROM opspilot.tool_calls WHERE id = :call_id) AS calls, "
                "(SELECT count(*) FROM opspilot.approvals WHERE id = :approval_id) AS approvals"
            ),
            {"step_id": step_id, "call_id": call_id, "approval_id": approval_id},
        ).one()
        assert tuple(remaining) == (0, 0, 0)


class TestRequiredIndexesExist:
    def test_every_index_named_in_the_architecture_exists(self, db_session: Session) -> None:
        rows = db_session.execute(
            sa.text("SELECT indexname FROM pg_indexes WHERE schemaname = 'opspilot'")
        ).scalars()
        names = set(rows)
        expected = {
            "ix_agent_runs_status_created_at",
            "ix_agent_runs_created_at",
            "ix_agent_runs_parent_run_id",
            "ix_agent_runs_evaluation_run_id",
            "ix_agent_runs_lease_expires_at_active",
            "uq_agent_runs_idempotency_key",
            "uq_execution_steps_run_step_revision",
            "ix_execution_steps_run_id_seq",
            "ix_execution_steps_run_id_status",
            "ix_execution_steps_verification_failed",
            "uq_tool_calls_execution_step_id_attempt",
            "ix_tool_calls_run_id_started_at",
            "ix_tool_calls_tool_status",
            "ix_tool_calls_idempotency_key",
            "ix_tool_calls_error_class_failed",
            "uq_approvals_run_id_step_id_pending",
            "ix_approvals_status_expires_at",
            "ix_approvals_status_requested_at",
            "ix_approvals_run_id",
        }
        assert expected <= names


class TestDuplicateApprovalConstraint:
    def test_a_second_pending_approval_for_the_same_step_is_rejected(
        self, db_session: Session
    ) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        now = datetime.now(UTC)

        def approval() -> ApprovalRow:
            return ApprovalRow(
                run_id=run.id,
                step_id="s1",
                tool="send_email_mock",
                risk="high",
                title="t",
                summary="s",
                args_hash="h",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
            )

        db_session.add(approval())
        db_session.flush()

        db_session.add(approval())
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_a_new_pending_approval_is_allowed_once_the_first_is_decided(
        self, db_session: Session
    ) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        now = datetime.now(UTC)

        first = ApprovalRow(
            run_id=run.id,
            step_id="s1",
            tool="send_email_mock",
            risk="high",
            title="t",
            summary="s",
            args_hash="h",
            requested_at=now,
            expires_at=now + timedelta(hours=1),
            status="approved",
            decided_at=now,
            decided_by="operator@example.com",
        )
        db_session.add(first)
        db_session.flush()

        second = ApprovalRow(
            run_id=run.id,
            step_id="s1",
            tool="send_email_mock",
            risk="high",
            title="t",
            summary="s",
            args_hash="h2",
            requested_at=now,
            expires_at=now + timedelta(hours=1),
        )
        db_session.add(second)
        db_session.flush()  # must not raise
        assert second.id is not None


class TestDuplicateToolCallAttemptConstraint:
    def test_a_second_tool_call_with_the_same_attempt_number_is_rejected(
        self, db_session: Session
    ) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        step = _make_step(run.id)
        db_session.add(step)
        db_session.flush()

        def call() -> ToolCallRow:
            return ToolCallRow(
                run_id=run.id,
                execution_step_id=step.id,
                step_id=step.step_id,
                attempt=1,
                tool="search_leads",
                status="succeeded",
            )

        db_session.add(call())
        db_session.flush()

        db_session.add(call())
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_a_second_attempt_with_a_different_number_is_allowed(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        step = _make_step(run.id)
        db_session.add(step)
        db_session.flush()

        db_session.add(
            ToolCallRow(
                run_id=run.id,
                execution_step_id=step.id,
                step_id=step.step_id,
                attempt=1,
                tool="search_leads",
                status="failed",
            )
        )
        db_session.flush()
        db_session.add(
            ToolCallRow(
                run_id=run.id,
                execution_step_id=step.id,
                step_id=step.step_id,
                attempt=2,
                tool="search_leads",
                status="succeeded",
            )
        )
        db_session.flush()  # must not raise
