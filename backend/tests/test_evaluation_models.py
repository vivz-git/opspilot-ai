"""`evaluation_runs` / `evaluation_results` against a real database (§12.8,
DB-003), plus the FK `c6d1db7aa718` deliberately left off
`agent_runs.evaluation_run_id` until this table existed.

Requires Postgres, like `test_migrations.py` and `test_persistence_models.py`
— skips cleanly with no reachable database, migrated to head via the same ad
hoc probe (`TEST-001` will replace this with the project's real fixture).

Each test runs inside a savepoint nested in one outer transaction (the same
pattern `test_persistence_models.py` and `test_trace_events.py` use) so a
`pytest.raises(IntegrityError)` does not poison the rest of the test and
nothing written here survives past it.
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
from app.persistence.models import AgentRun, EvaluationResult, EvaluationRun
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

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
        pytest.skip("no reachable Postgres for this session (DB-003 integration test)")


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


def _make_eval_run(**overrides: object) -> EvaluationRun:
    defaults: dict[str, object] = {
        "suite": "regression",
        "planner_kind": "rules",
    }
    defaults.update(overrides)
    return EvaluationRun(**defaults)  # type: ignore[arg-type]


def _make_eval_result(
    evaluation_run_id: uuid.UUID, run_id: uuid.UUID, **overrides: object
) -> EvaluationResult:
    defaults: dict[str, object] = {
        "evaluation_run_id": evaluation_run_id,
        "case_id": "lead_ranking",
        "run_id": run_id,
        "passed": True,
    }
    defaults.update(overrides)
    return EvaluationResult(**defaults)  # type: ignore[arg-type]


class TestEvaluationRunCanBeCreated:
    def test_minimal_evaluation_run(self, db_session: Session) -> None:
        eval_run = _make_eval_run()
        db_session.add(eval_run)
        db_session.flush()

        assert eval_run.id is not None
        assert eval_run.status == "running"
        assert eval_run.case_count == 0
        assert eval_run.passed == 0
        assert eval_run.failed == 0
        assert eval_run.metrics == {}
        assert eval_run.started_at is not None
        assert eval_run.finished_at is None

    def test_full_evaluation_run_including_reproducibility_fields(
        self, db_session: Session
    ) -> None:
        eval_run = _make_eval_run(
            status="completed",
            git_sha="a1b2c3d4e5f6",
            model_id="claude-sonnet-5",
            prompt_version="2026-09-01",
            seed=42,
            case_count=7,
            passed=6,
            failed=1,
            metrics={"case_pass_rate": 0.857},
            finished_at=datetime.now(UTC),
        )
        db_session.add(eval_run)
        db_session.flush()

        # git_sha persistence
        assert eval_run.git_sha == "a1b2c3d4e5f6"
        # prompt_version persistence
        assert eval_run.prompt_version == "2026-09-01"
        # seed persistence
        assert eval_run.seed == 42
        assert eval_run.metrics == {"case_pass_rate": 0.857}
        assert eval_run.case_count == 7
        assert eval_run.passed == 6
        assert eval_run.failed == 1


class TestEvaluationResultCanBeCreated:
    def test_minimal_evaluation_result(self, db_session: Session) -> None:
        eval_run = _make_eval_run()
        db_session.add(eval_run)
        run = _make_run()
        db_session.add(run)
        db_session.flush()

        result = _make_eval_result(eval_run.id, run.id)
        db_session.add(result)
        db_session.flush()

        assert result.id is not None
        assert result.passed is True
        assert result.assertions == []
        assert result.retry_count == 0
        assert result.tool_calls_count == 0
        assert result.duration_ms is None
        assert result.approval_outcome is None
        assert result.failure_reason is None

    def test_full_evaluation_result(self, db_session: Session) -> None:
        eval_run = _make_eval_run()
        db_session.add(eval_run)
        run = _make_run()
        db_session.add(run)
        db_session.flush()

        result = _make_eval_result(
            eval_run.id,
            run.id,
            passed=False,
            assertions=[
                {
                    "name": "status_equals",
                    "expected": "completed",
                    "observed": "failed",
                    "passed": False,
                }
            ],
            duration_ms=1234,
            retry_count=2,
            tool_calls_count=5,
            approval_outcome="approved",
            failure_reason="verification_failed",
        )
        db_session.add(result)
        db_session.flush()

        assert result.passed is False
        assert result.assertions[0]["name"] == "status_equals"
        assert result.duration_ms == 1234
        assert result.retry_count == 2
        assert result.tool_calls_count == 5
        assert result.approval_outcome == "approved"
        assert result.failure_reason == "verification_failed"


class TestEvaluationRunToResultsRelationship:
    def test_a_result_links_to_its_evaluation_run(self, db_session: Session) -> None:
        eval_run = _make_eval_run()
        db_session.add(eval_run)
        run = _make_run()
        db_session.add(run)
        db_session.flush()

        result = _make_eval_result(eval_run.id, run.id)
        db_session.add(result)
        db_session.flush()

        assert result.evaluation_run_id == eval_run.id

    def test_deleting_the_evaluation_run_cascades_to_its_results(self, db_session: Session) -> None:
        eval_run = _make_eval_run()
        db_session.add(eval_run)
        run = _make_run()
        db_session.add(run)
        db_session.flush()

        result = _make_eval_result(eval_run.id, run.id)
        db_session.add(result)
        db_session.flush()
        result_id = result.id

        db_session.delete(eval_run)
        db_session.flush()

        remaining = db_session.execute(
            sa.text("SELECT count(*) FROM opspilot.evaluation_results WHERE id = :id"),
            {"id": result_id},
        ).scalar_one()
        assert remaining == 0


class TestEvaluationResultToAgentRunRelationship:
    def test_a_result_links_to_the_real_agent_run_it_executed(self, db_session: Session) -> None:
        eval_run = _make_eval_run()
        db_session.add(eval_run)
        run = _make_run()
        db_session.add(run)
        db_session.flush()

        result = _make_eval_result(eval_run.id, run.id)
        db_session.add(result)
        db_session.flush()

        assert result.run_id == run.id

    def test_an_unknown_agent_run_id_is_rejected(self, db_session: Session) -> None:
        eval_run = _make_eval_run()
        db_session.add(eval_run)
        db_session.flush()

        result = _make_eval_result(eval_run.id, uuid.uuid4())
        db_session.add(result)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_an_unknown_evaluation_run_id_is_rejected(self, db_session: Session) -> None:
        run = _make_run()
        db_session.add(run)
        db_session.flush()

        result = _make_eval_result(uuid.uuid4(), run.id)
        db_session.add(result)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_deleting_the_agent_run_is_restricted_while_a_result_references_it(
        self, db_session: Session
    ) -> None:
        """Unlike `execution_steps`/`tool_calls`/`approvals`/`trace_events`
        (owned by the run and cascade-deleted with it), an evaluation result
        is history *about* a run: deleting the run must not silently erase
        whether a case passed. The database blocks the delete instead."""
        eval_run = _make_eval_run()
        db_session.add(eval_run)
        run = _make_run()
        db_session.add(run)
        db_session.flush()

        result = _make_eval_result(eval_run.id, run.id)
        db_session.add(result)
        db_session.flush()

        db_session.delete(run)
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestDeferredAgentRunsForeignKey:
    """The FK `agent_runs.evaluation_run_id -> evaluation_runs.id` that
    DB-001 deliberately left off since `evaluation_runs` did not exist yet."""

    def test_an_agent_run_can_reference_a_real_evaluation_run(self, db_session: Session) -> None:
        eval_run = _make_eval_run()
        db_session.add(eval_run)
        db_session.flush()

        run = _make_run(evaluation_run_id=eval_run.id, eval_case_id="lead_ranking")
        db_session.add(run)
        db_session.flush()

        assert run.evaluation_run_id == eval_run.id

    def test_an_unknown_evaluation_run_id_on_agent_runs_is_rejected(
        self, db_session: Session
    ) -> None:
        run = _make_run(evaluation_run_id=uuid.uuid4())
        db_session.add(run)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_deleting_the_evaluation_run_is_restricted_while_an_agent_run_references_it(
        self, db_session: Session
    ) -> None:
        """The FK must not cascade: deleting an `evaluation_runs` row must
        never delete real execution history."""
        eval_run = _make_eval_run()
        db_session.add(eval_run)
        db_session.flush()

        run = _make_run(evaluation_run_id=eval_run.id)
        db_session.add(run)
        db_session.flush()

        db_session.delete(eval_run)
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestRequiredFieldsAndDefaults:
    def test_evaluation_run_suite_is_required(self, db_session: Session) -> None:
        with pytest.raises(IntegrityError):
            db_session.execute(
                sa.text("INSERT INTO opspilot.evaluation_runs (planner_kind) VALUES ('rules')")
            )
            db_session.flush()

    def test_evaluation_run_planner_kind_is_required(self, db_session: Session) -> None:
        with pytest.raises(IntegrityError):
            db_session.execute(
                sa.text("INSERT INTO opspilot.evaluation_runs (suite) VALUES ('regression')")
            )
            db_session.flush()

    def test_evaluation_result_passed_is_required(self, db_session: Session) -> None:
        eval_run = _make_eval_run()
        db_session.add(eval_run)
        run = _make_run()
        db_session.add(run)
        db_session.flush()
        with pytest.raises(IntegrityError):
            db_session.execute(
                sa.text(
                    "INSERT INTO opspilot.evaluation_results "
                    "(evaluation_run_id, case_id, run_id) "
                    "VALUES (:eval_run_id, 'c1', :run_id)"
                ),
                {"eval_run_id": eval_run.id, "run_id": run.id},
            )
            db_session.flush()

    def test_invalid_evaluation_run_status_is_rejected_at_the_database(
        self, db_session: Session
    ) -> None:
        with pytest.raises(IntegrityError):
            db_session.execute(
                sa.text(
                    "INSERT INTO opspilot.evaluation_runs (suite, planner_kind, status) "
                    "VALUES ('regression', 'rules', 'bogus')"
                )
            )
            db_session.flush()


class TestUniqueEvaluationRunCaseConstraint:
    def test_a_duplicate_case_id_for_the_same_evaluation_run_is_rejected(
        self, db_session: Session
    ) -> None:
        eval_run = _make_eval_run()
        db_session.add(eval_run)
        run_a = _make_run()
        run_b = _make_run()
        db_session.add_all([run_a, run_b])
        db_session.flush()

        db_session.add(_make_eval_result(eval_run.id, run_a.id, case_id="lead_ranking"))
        db_session.flush()

        db_session.add(_make_eval_result(eval_run.id, run_b.id, case_id="lead_ranking"))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_the_same_case_id_is_allowed_across_different_evaluation_runs(
        self, db_session: Session
    ) -> None:
        eval_run_a = _make_eval_run()
        eval_run_b = _make_eval_run()
        run_a = _make_run()
        run_b = _make_run()
        db_session.add_all([eval_run_a, eval_run_b, run_a, run_b])
        db_session.flush()

        db_session.add(_make_eval_result(eval_run_a.id, run_a.id, case_id="lead_ranking"))
        db_session.add(_make_eval_result(eval_run_b.id, run_b.id, case_id="lead_ranking"))
        db_session.flush()  # must not raise


class TestRequiredIndexesExist:
    def test_every_index_named_in_the_architecture_exists(self, db_session: Session) -> None:
        rows = db_session.execute(
            sa.text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'opspilot' "
                "AND tablename IN ('evaluation_runs', 'evaluation_results')"
            )
        ).scalars()
        names = set(rows)
        assert {
            "ix_evaluation_runs_suite_started_at",
            "uq_evaluation_results_evaluation_run_id_case_id",
            "ix_evaluation_results_case_id_passed",
        } <= names

    def test_the_deferred_agent_runs_foreign_key_exists(self, db_session: Session) -> None:
        exists = db_session.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = 'fk_agent_runs_evaluation_run_id_evaluation_runs'"
            )
        ).scalar_one_or_none()
        assert exists == 1
