"""`trace_events` against a real database (§12.7, DB-002).

Requires Postgres, like `test_migrations.py` and `test_persistence_models.py`
— skips cleanly with no reachable database, migrated to head via the same ad
hoc probe (`TEST-001` will replace this with the project's real fixture).

Most tests run inside a savepoint nested in one outer transaction (the same
pattern `test_persistence_models.py` uses) so a `pytest.raises(IntegrityError)`
does not poison the rest of the test and nothing written here survives past
it. The concurrency tests are the deliberate exception: they need genuinely
separate, concurrently-committing transactions — the property under test is
what happens *across* transaction boundaries — so they open their own
connections from a dedicated engine instead of sharing the savepoint session.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from app.config import get_settings
from app.persistence.models import AgentRun, TraceEvent, TraceEventKind, TraceEventSeverity
from app.persistence.trace_events import append_trace_event
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _sync_engine(**kwargs: object) -> sa.Engine:
    return sa.create_engine(_sync_url(get_settings().database_url.get_secret_value()), **kwargs)


def _require_database() -> None:
    try:
        with sa.create_engine(
            _sync_url(get_settings().database_url.get_secret_value()),
            connect_args={"connect_timeout": 3},
        ).connect():
            pass
    except OperationalError:
        pytest.skip("no reachable Postgres for this session (DB-002 integration test)")


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


class TestTraceEventCanBeCreated:
    def test_minimal_event(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()

        event = append_trace_event(db_session, run_id=run.id, kind=TraceEventKind.NODE_ENTERED)

        assert event.id is not None
        assert event.seq == 1
        assert event.severity == TraceEventSeverity.INFO
        assert event.payload == {}
        assert event.ts is not None

    def test_full_event(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()

        event = append_trace_event(
            db_session,
            run_id=run.id,
            kind=TraceEventKind.TOOL_FAILED,
            severity=TraceEventSeverity.ERROR,
            node="execute_tool",
            tool="send_email_mock",
            step_id="s2",
            attempt=2,
            input={"to": "jane@example.com"},
            output=None,
            status="failed",
            duration_ms=120,
            retry_count=1,
            error={"class": "TRANSIENT", "message": "timeout"},
            payload={"idempotency_key": "abc"},
        )

        assert event.kind == TraceEventKind.TOOL_FAILED
        assert event.severity == TraceEventSeverity.ERROR
        assert event.error == {"class": "TRANSIENT", "message": "timeout"}


class TestRequiredFieldsAndConstraints:
    def test_run_id_is_required(self, db_session: Session) -> None:
        with pytest.raises(IntegrityError):
            db_session.execute(
                sa.text("INSERT INTO opspilot.trace_events (seq, kind) VALUES (1, 'node_entered')")
            )
            db_session.flush()

    def test_seq_is_required(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        with pytest.raises(IntegrityError):
            db_session.execute(
                sa.text(
                    "INSERT INTO opspilot.trace_events (run_id, kind) "
                    "VALUES (:run_id, 'node_entered')"
                ),
                {"run_id": run.id},
            )
            db_session.flush()

    def test_unknown_run_id_is_rejected(self, db_session: Session) -> None:
        event = TraceEvent(run_id=uuid.uuid4(), seq=1, kind=TraceEventKind.NODE_ENTERED)
        db_session.add(event)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_invalid_kind_is_rejected_at_the_database(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        with pytest.raises(IntegrityError):
            db_session.execute(
                sa.text(
                    "INSERT INTO opspilot.trace_events (run_id, seq, kind) "
                    "VALUES (:run_id, 1, 'bogus')"
                ),
                {"run_id": run.id},
            )
            db_session.flush()

    def test_invalid_severity_is_rejected_at_the_database(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        with pytest.raises(IntegrityError):
            db_session.execute(
                sa.text(
                    "INSERT INTO opspilot.trace_events (run_id, seq, kind, severity) "
                    "VALUES (:run_id, 1, 'node_entered', 'bogus')"
                ),
                {"run_id": run.id},
            )
            db_session.flush()

    def test_deleting_a_run_cascades_to_its_trace_events(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        event = append_trace_event(db_session, run_id=run.id, kind=TraceEventKind.RUN_CREATED)
        event_id = event.id

        db_session.delete(run)
        db_session.flush()

        remaining = db_session.execute(
            sa.text("SELECT count(*) FROM opspilot.trace_events WHERE id = :id"),
            {"id": event_id},
        ).scalar_one()
        assert remaining == 0


class TestUniqueRunIdSeqConstraint:
    def test_a_duplicate_explicit_run_id_seq_is_rejected(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()

        db_session.add(TraceEvent(run_id=run.id, seq=1, kind=TraceEventKind.NODE_ENTERED))
        db_session.flush()

        db_session.add(TraceEvent(run_id=run.id, seq=1, kind=TraceEventKind.NODE_EXITED))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_the_same_seq_is_allowed_across_different_runs(self, db_session: Session) -> None:
        run_a = _make_run()
        run_b = _make_run()
        db_session.add_all([run_a, run_b])
        db_session.flush()

        db_session.add(TraceEvent(run_id=run_a.id, seq=1, kind=TraceEventKind.NODE_ENTERED))
        db_session.add(TraceEvent(run_id=run_b.id, seq=1, kind=TraceEventKind.NODE_ENTERED))
        db_session.flush()  # must not raise


class TestRequiredIndexesExist:
    def test_every_index_named_in_the_architecture_exists(self, db_session: Session) -> None:
        rows = db_session.execute(
            sa.text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'opspilot' AND tablename = 'trace_events'"
            )
        ).scalars()
        names = set(rows)
        assert {
            "uq_trace_events_run_id_seq",
            "ix_trace_events_run_id_id",
            "ix_trace_events_kind_ts",
            "ix_trace_events_ts_brin",
        } <= names

    def test_the_ts_index_uses_brin(self, db_session: Session) -> None:
        method = db_session.execute(
            sa.text(
                "SELECT am.amname FROM pg_class c "
                "JOIN pg_am am ON am.oid = c.relam "
                "WHERE c.relname = 'ix_trace_events_ts_brin'"
            )
        ).scalar_one()
        assert method == "brin"

    def test_the_run_id_seq_index_is_unique(self, db_session: Session) -> None:
        is_unique = db_session.execute(
            sa.text(
                "SELECT indisunique FROM pg_index "
                "WHERE indexrelid = 'opspilot.uq_trace_events_run_id_seq'::regclass"
            )
        ).scalar_one()
        assert is_unique is True


class TestSequenceAllocationIsMonotonicAndSequential:
    def test_sequential_appends_produce_1_2_3(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()

        first = append_trace_event(db_session, run_id=run.id, kind=TraceEventKind.RUN_CREATED)
        second = append_trace_event(db_session, run_id=run.id, kind=TraceEventKind.RUN_STARTED)
        third = append_trace_event(db_session, run_id=run.id, kind=TraceEventKind.NODE_ENTERED)

        assert (first.seq, second.seq, third.seq) == (1, 2, 3)

    def test_seq_is_independent_per_run(self, db_session: Session) -> None:
        run_a = _make_run()
        run_b = _make_run()
        db_session.add_all([run_a, run_b])
        db_session.flush()

        a1 = append_trace_event(db_session, run_id=run_a.id, kind=TraceEventKind.RUN_CREATED)
        b1 = append_trace_event(db_session, run_id=run_b.id, kind=TraceEventKind.RUN_CREATED)
        a2 = append_trace_event(db_session, run_id=run_a.id, kind=TraceEventKind.RUN_STARTED)

        assert (a1.seq, b1.seq, a2.seq) == (1, 1, 2)


class TestConcurrentSequenceAllocation:
    """The critical DB-002 property: concurrent writers for the SAME run
    must never produce a duplicate or a gapped `seq`.

    Each worker opens its own connection and commits independently — this is
    what makes the test a genuine test of `pg_advisory_xact_lock`'s
    transaction-scoped serialization rather than of a single session's
    ordering, which would prove nothing about real concurrency.
    """

    CONCURRENCY = 25

    def _committed_run_id(self, engine: sa.Engine) -> uuid.UUID:
        with Session(bind=engine) as session:
            run = _make_run()
            session.add(run)
            session.commit()
            return run.id

    def test_concurrent_appends_yield_1_through_n_with_no_duplicates_or_gaps(self) -> None:
        engine = _sync_engine(poolclass=NullPool)
        try:
            run_id = self._committed_run_id(engine)

            def append_one(i: int) -> int:
                with Session(bind=engine) as session:
                    event = append_trace_event(
                        session,
                        run_id=run_id,
                        kind=TraceEventKind.NODE_ENTERED,
                        node=f"worker-{i}",
                    )
                    session.commit()
                    return event.seq

            with ThreadPoolExecutor(max_workers=self.CONCURRENCY) as executor:
                seqs = list(executor.map(append_one, range(self.CONCURRENCY)))

            assert len(seqs) == self.CONCURRENCY
            assert len(set(seqs)) == self.CONCURRENCY, "duplicate seq allocated under concurrency"
            assert sorted(seqs) == list(range(1, self.CONCURRENCY + 1)), (
                "gap in allocated sequence under concurrency"
            )

            with engine.connect() as conn:
                db_seqs = (
                    conn.execute(
                        sa.text(
                            "SELECT seq FROM opspilot.trace_events "
                            "WHERE run_id = :run_id ORDER BY seq"
                        ),
                        {"run_id": run_id},
                    )
                    .scalars()
                    .all()
                )
            assert list(db_seqs) == list(range(1, self.CONCURRENCY + 1))
        finally:
            engine.dispose()

    def test_two_concurrent_runs_do_not_interfere_with_each_others_sequence(self) -> None:
        engine = _sync_engine(poolclass=NullPool)
        try:
            run_a = self._committed_run_id(engine)
            run_b = self._committed_run_id(engine)

            def append_for(run_id: uuid.UUID, i: int) -> tuple[uuid.UUID, int]:
                with Session(bind=engine) as session:
                    event = append_trace_event(
                        session, run_id=run_id, kind=TraceEventKind.NODE_ENTERED
                    )
                    session.commit()
                    return run_id, event.seq

            n_per_run = 15
            jobs = [(run_a, i) for i in range(n_per_run)] + [(run_b, i) for i in range(n_per_run)]

            with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
                results = list(executor.map(lambda job: append_for(*job), jobs))

            seqs_a = sorted(seq for run_id, seq in results if run_id == run_a)
            seqs_b = sorted(seq for run_id, seq in results if run_id == run_b)
            assert seqs_a == list(range(1, n_per_run + 1))
            assert seqs_b == list(range(1, n_per_run + 1))
        finally:
            engine.dispose()

    def test_an_explicit_duplicate_run_id_seq_is_rejected_even_under_concurrency(self) -> None:
        """The database backstop: two transactions racing to insert the same
        explicit `(run_id, seq)` (bypassing the allocator) must have exactly
        one winner, never both succeeding."""
        engine = _sync_engine(poolclass=NullPool)
        try:
            run_id = self._committed_run_id(engine)

            def insert_seq_1() -> bool:
                with Session(bind=engine) as session:
                    session.add(TraceEvent(run_id=run_id, seq=1, kind=TraceEventKind.NODE_ENTERED))
                    try:
                        session.commit()
                        return True
                    except IntegrityError:
                        session.rollback()
                        return False

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: insert_seq_1(), range(2)))

            assert sorted(results) == [False, True]
        finally:
            engine.dispose()
