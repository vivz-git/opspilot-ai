"""Database constraint and invariant verification against real PostgreSQL (§12, DB-006).

Proves at the database level that safety-critical indexes, unique constraints,
foreign keys, cascade/restrict rules, optimistic concurrency version checks,
and enum CHECK constraints are enforced by PostgreSQL itself — not merely by
application or ORM validation.

Every concurrency test uses genuinely independent connections/transactions to
create real races and reports:
- Number of competing transactions
- Number of successful commits
- Number of rejected operations
- Final database state
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from app.config import get_settings
from app.persistence.models import (
    TraceEventKind,
)
from app.persistence.repositories import SqlTraceEventRepository
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _require_database() -> None:
    try:
        with sa.create_engine(
            _sync_url(get_settings().database_url.get_secret_value()),
            connect_args={"connect_timeout": 3},
        ).connect():
            pass
    except OperationalError:
        pytest.skip("no reachable Postgres for this session (DB-006 integration test)")


@pytest.fixture(scope="module", autouse=True)
def _migrated_database() -> None:
    _require_database()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url.get_secret_value())
    command.upgrade(config, "head")


@pytest.fixture
async def async_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        get_settings().database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=25,
        max_overflow=10,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=async_engine, class_=AsyncSession, expire_on_commit=False)


class TestApprovalPendingConstraint:
    """Invariant: At most one pending approval per (run_id, step_id) at the database level."""

    async def test_partial_unique_index_exists_in_postgres(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            stmt = text("""
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'opspilot'
                  AND tablename = 'approvals'
                  AND indexname = 'uq_approvals_run_id_step_id_pending';
            """)
            res = await session.execute(stmt)
            row = res.fetchone()
            assert row is not None
            assert "UNIQUE" in row.indexdef
            assert "status" in row.indexdef and "pending" in row.indexdef

    async def test_re_approval_allowed_once_first_approval_decided(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        now = datetime.now(UTC)
        deadline = now + timedelta(minutes=5)
        expires = now + timedelta(hours=1)

        async with session_factory() as session:
            # Create run
            await session.execute(
                text("""
                    INSERT INTO opspilot.agent_runs (id, user_request, planner_kind, deadline_at)
                    VALUES (:id, 'Approval flow', 'rules', :deadline);
                """),
                {"id": run_id, "deadline": deadline},
            )
            # Insert first pending approval
            appr_1 = uuid.uuid4()
            await session.execute(
                text("""
                    INSERT INTO opspilot.approvals
                    (id, run_id, step_id, tool, risk, title, summary,
                     args_hash, status, requested_at, expires_at)
                    VALUES (:id, :run_id, 's1', 'send_email_mock', 'high',
                            'Title', 'Summary', 'h1', 'pending', :req, :exp);
                """),
                {"id": appr_1, "run_id": run_id, "req": now, "exp": expires},
            )
            await session.commit()

        # Decide first approval: transition to 'approved'
        async with session_factory() as session:
            await session.execute(
                text("UPDATE opspilot.approvals SET status = 'approved' WHERE id = :id;"),
                {"id": appr_1},
            )
            await session.commit()

        # Second pending approval for the SAME (run_id, step_id) is now allowed
        appr_2 = uuid.uuid4()
        async with session_factory() as session:
            await session.execute(
                text("""
                    INSERT INTO opspilot.approvals
                    (id, run_id, step_id, tool, risk, title, summary,
                     args_hash, status, requested_at, expires_at)
                    VALUES (:id, :run_id, 's1', 'send_email_mock', 'high',
                            'Title 2', 'Summary 2', 'h2', 'pending', :req, :exp);
                """),
                {"id": appr_2, "run_id": run_id, "req": now, "exp": expires},
            )
            await session.commit()

        # Verify both exist in DB
        async with session_factory() as session:
            res = await session.execute(
                text(
                    "SELECT count(*) FROM opspilot.approvals "
                    "WHERE run_id = :run_id AND step_id = 's1';"
                ),
                {"run_id": run_id},
            )
            assert res.scalar_one() == 2

    async def test_concurrent_pending_approval_race(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        now = datetime.now(UTC)
        deadline = now + timedelta(minutes=5)
        expires = now + timedelta(hours=1)

        async with session_factory() as session:
            await session.execute(
                text("""
                    INSERT INTO opspilot.agent_runs (id, user_request, planner_kind, deadline_at)
                    VALUES (:id, 'Race run', 'rules', :deadline);
                """),
                {"id": run_id, "deadline": deadline},
            )
            await session.commit()

        competing_transactions = 10

        async def worker(idx: int) -> str:
            async with session_factory() as session:
                appr_id = uuid.uuid4()
                await session.execute(
                    text("""
                        INSERT INTO opspilot.approvals
                        (id, run_id, step_id, tool, risk, title, summary,
                         args_hash, status, requested_at, expires_at)
                        VALUES (:id, :run_id, 'step_race', 'send_email_mock', 'high',
                                :title, 'Summary', :h, 'pending', :req, :exp);
                    """),
                    {
                        "id": appr_id,
                        "run_id": run_id,
                        "title": f"Attempt {idx}",
                        "h": f"hash_{idx}",
                        "req": now,
                        "exp": expires,
                    },
                )
                await session.commit()
                return str(appr_id)

        results = await asyncio.gather(
            *(worker(i) for i in range(competing_transactions)), return_exceptions=True
        )

        successful_commits = [r for r in results if not isinstance(r, Exception)]
        rejected_operations = [r for r in results if isinstance(r, IntegrityError)]

        # Verify race invariants
        assert len(successful_commits) == 1, f"Expected 1 winner, got: {successful_commits}"
        assert len(rejected_operations) == competing_transactions - 1

        # Check final DB state
        async with session_factory() as session:
            res = await session.execute(
                text(
                    "SELECT count(*) FROM opspilot.approvals "
                    "WHERE run_id = :run_id AND step_id = 'step_race';"
                ),
                {"run_id": run_id},
            )
            final_db_count = res.scalar_one()

        assert final_db_count == 1


class TestOutboxIdempotencyConstraint:
    """Invariant: UNIQUE(idempotency_key) on mock_crm.email_outbox prevents duplicate sends."""

    async def test_outbox_idempotency_constraint_exists(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            stmt = text("""
                SELECT conname
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
                WHERE nsp.nspname = 'mock_crm'
                  AND rel.relname = 'email_outbox'
                  AND con.conname = 'uq_email_outbox_idempotency_key'
                  AND con.contype = 'u';
            """)
            res = await session.execute(stmt)
            assert res.scalar_one_or_none() == "uq_email_outbox_idempotency_key"

    async def test_concurrent_outbox_idempotency_key_race(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        comp_id = f"c_outbox_{uuid.uuid4().hex[:8]}"
        lead_id = f"l_outbox_{uuid.uuid4().hex[:8]}"
        draft_id = f"d_outbox_{uuid.uuid4().hex[:8]}"
        idem_key = f"race_idem_{uuid.uuid4()}"

        async with session_factory() as session:
            await session.execute(
                text("""
                    INSERT INTO mock_crm.companies (company_id, name, domain)
                    VALUES (:cid, 'Outbox Co', :domain);
                """),
                {"cid": comp_id, "domain": f"{comp_id}.example"},
            )
            await session.execute(
                text("""
                    INSERT INTO mock_crm.leads (lead_id, company_id, full_name, email)
                    VALUES (:lid, :cid, 'Lead 1', 'lead@example.com');
                """),
                {"lid": lead_id, "cid": comp_id},
            )
            await session.execute(
                text("""
                    INSERT INTO mock_crm.outreach_drafts
                    (draft_id, lead_id, subject, body, content_hash)
                    VALUES (:did, :lid, 'Subject', 'Body', 'hash');
                """),
                {"did": draft_id, "lid": lead_id},
            )
            await session.commit()

        competing_transactions = 10

        async def worker(idx: int) -> str:
            async with session_factory() as session:
                outbox_id = f"out_{uuid.uuid4().hex[:8]}"
                msg_id = f"msg_{uuid.uuid4().hex[:8]}"
                await session.execute(
                    text("""
                        INSERT INTO mock_crm.email_outbox
                        (outbox_id, message_id, draft_id, to_email, subject,
                         body, status, provider, idempotency_key)
                        VALUES (:oid, :mid, :did, 'lead@example.com', 'Sub',
                                'Body', 'sent', 'mock', :idem);
                    """),
                    {"oid": outbox_id, "mid": msg_id, "did": draft_id, "idem": idem_key},
                )
                await session.commit()
                return outbox_id

        results = await asyncio.gather(
            *(worker(i) for i in range(competing_transactions)), return_exceptions=True
        )

        successful_commits = [r for r in results if not isinstance(r, Exception)]
        rejected_operations = [r for r in results if isinstance(r, IntegrityError)]

        assert len(successful_commits) == 1
        assert len(rejected_operations) == competing_transactions - 1

        async with session_factory() as session:
            res = await session.execute(
                text("SELECT count(*) FROM mock_crm.email_outbox WHERE idempotency_key = :idem;"),
                {"idem": idem_key},
            )
            final_db_count = res.scalar_one()

        assert final_db_count == 1


class TestTraceEventSequenceConstraint:
    """Invariant: UNIQUE(run_id, seq) on opspilot.trace_events ensures total ordering."""

    async def test_trace_events_unique_constraint_exists(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            stmt = text("""
                SELECT conname
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
                WHERE nsp.nspname = 'opspilot'
                  AND rel.relname = 'trace_events'
                  AND con.conname = 'uq_trace_events_run_id_seq'
                  AND con.contype = 'u';
            """)
            res = await session.execute(stmt)
            assert res.scalar_one_or_none() == "uq_trace_events_run_id_seq"

    async def test_concurrent_raw_duplicate_seq_race(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        now = datetime.now(UTC)
        deadline = now + timedelta(minutes=5)

        async with session_factory() as session:
            await session.execute(
                text("""
                    INSERT INTO opspilot.agent_runs (id, user_request, planner_kind, deadline_at)
                    VALUES (:id, 'Trace race', 'rules', :deadline);
                """),
                {"id": run_id, "deadline": deadline},
            )
            await session.commit()

        competing_transactions = 10

        # Race to insert the exact same seq=1
        async def worker(idx: int) -> int:
            async with session_factory() as session:
                await session.execute(
                    text("""
                        INSERT INTO opspilot.trace_events (run_id, seq, kind, severity)
                        VALUES (:run_id, 1, 'run_started', 'info');
                    """),
                    {"run_id": run_id},
                )
                await session.commit()
                return idx

        results = await asyncio.gather(
            *(worker(i) for i in range(competing_transactions)), return_exceptions=True
        )

        successful_commits = [r for r in results if not isinstance(r, Exception)]
        rejected_operations = [r for r in results if isinstance(r, IntegrityError)]

        assert len(successful_commits) == 1
        assert len(rejected_operations) == competing_transactions - 1

        async with session_factory() as session:
            res = await session.execute(
                text(
                    "SELECT count(*) FROM opspilot.trace_events WHERE run_id = :run_id AND seq = 1;"
                ),
                {"run_id": run_id},
            )
            assert res.scalar_one() == 1

    async def test_advisory_lock_allocation_yields_gapless_unique_sequence(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        now = datetime.now(UTC)
        deadline = now + timedelta(minutes=5)

        async with session_factory() as session:
            await session.execute(
                text("""
                    INSERT INTO opspilot.agent_runs (id, user_request, planner_kind, deadline_at)
                    VALUES (:id, 'Advisory lock trace race', 'rules', :deadline);
                """),
                {"id": run_id, "deadline": deadline},
            )
            await session.commit()

        competing_transactions = 20

        async def worker(idx: int) -> int:
            async with session_factory() as session:
                repo = SqlTraceEventRepository(session)
                event = await repo.append(
                    run_id=run_id,
                    kind=TraceEventKind.TOOL_STARTED,
                    step_id=f"step_{idx}",
                )
                await session.commit()
                return event.seq

        results = await asyncio.gather(
            *(worker(i) for i in range(competing_transactions)), return_exceptions=True
        )

        successful_commits = [r for r in results if isinstance(r, int)]
        rejected_operations = [r for r in results if isinstance(r, Exception)]

        assert len(successful_commits) == competing_transactions
        assert len(rejected_operations) == 0
        assert sorted(successful_commits) == list(range(1, competing_transactions + 1))


class TestAgentRunIdempotencyConstraint:
    """Invariant: UNIQUE(idempotency_key) on opspilot.agent_runs prevents duplicate runs."""

    async def test_run_idempotency_constraint_exists(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            stmt = text("""
                SELECT conname
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
                WHERE nsp.nspname = 'opspilot'
                  AND rel.relname = 'agent_runs'
                  AND con.conname = 'uq_agent_runs_idempotency_key'
                  AND con.contype = 'u';
            """)
            res = await session.execute(stmt)
            assert res.scalar_one_or_none() == "uq_agent_runs_idempotency_key"

    async def test_concurrent_agent_run_idempotency_race(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        idem_key = f"run_race_{uuid.uuid4()}"
        deadline = datetime.now(UTC) + timedelta(minutes=5)
        competing_transactions = 10

        async def worker(idx: int) -> str:
            async with session_factory() as session:
                run_id = uuid.uuid4()
                await session.execute(
                    text("""
                        INSERT INTO opspilot.agent_runs
                        (id, user_request, planner_kind, deadline_at, idempotency_key)
                        VALUES (:id, :req, 'rules', :deadline, :idem);
                    """),
                    {"id": run_id, "req": f"Req {idx}", "deadline": deadline, "idem": idem_key},
                )
                await session.commit()
                return str(run_id)

        results = await asyncio.gather(
            *(worker(i) for i in range(competing_transactions)), return_exceptions=True
        )

        successful_commits = [r for r in results if not isinstance(r, Exception)]
        rejected_operations = [r for r in results if isinstance(r, IntegrityError)]

        assert len(successful_commits) == 1
        assert len(rejected_operations) == competing_transactions - 1

        async with session_factory() as session:
            res = await session.execute(
                text("SELECT count(*) FROM opspilot.agent_runs WHERE idempotency_key = :idem;"),
                {"idem": idem_key},
            )
            assert res.scalar_one() == 1


class TestStepAndToolCallConstraints:
    """Invariants: Step revision uniqueness and ToolCall attempt uniqueness."""

    async def test_step_revision_constraint_exists(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            stmt = text("""
                SELECT conname
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
                WHERE nsp.nspname = 'opspilot'
                  AND rel.relname = 'execution_steps'
                  AND con.conname = 'uq_execution_steps_run_step_revision'
                  AND con.contype = 'u';
            """)
            res = await session.execute(stmt)
            assert res.scalar_one_or_none() == "uq_execution_steps_run_step_revision"

    async def test_tool_call_attempt_constraint_exists(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            stmt = text("""
                SELECT conname
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
                WHERE nsp.nspname = 'opspilot'
                  AND rel.relname = 'tool_calls'
                  AND con.conname = 'uq_tool_calls_execution_step_id_attempt'
                  AND con.contype = 'u';
            """)
            res = await session.execute(stmt)
            assert res.scalar_one_or_none() == "uq_tool_calls_execution_step_id_attempt"

    async def test_concurrent_tool_call_attempt_race(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        step_id = uuid.uuid4()
        deadline = datetime.now(UTC) + timedelta(minutes=5)

        async with session_factory() as session:
            await session.execute(
                text("""
                    INSERT INTO opspilot.agent_runs (id, user_request, planner_kind, deadline_at)
                    VALUES (:rid, 'Attempt race', 'rules', :deadline);
                """),
                {"rid": run_id, "deadline": deadline},
            )
            await session.execute(
                text("""
                    INSERT INTO opspilot.execution_steps
                    (id, run_id, step_id, plan_revision, seq, tool)
                    VALUES (:sid, :rid, 's1', 0, 1, 'search_leads');
                """),
                {"sid": step_id, "rid": run_id},
            )
            await session.commit()

        competing_transactions = 10

        # Race to insert attempt=1
        async def worker(idx: int) -> str:
            async with session_factory() as session:
                call_id = uuid.uuid4()
                await session.execute(
                    text("""
                        INSERT INTO opspilot.tool_calls
                        (id, run_id, execution_step_id, step_id, attempt, tool, status)
                        VALUES (:cid, :rid, :sid, 's1', 1, 'search_leads', 'succeeded');
                    """),
                    {"cid": call_id, "rid": run_id, "sid": step_id},
                )
                await session.commit()
                return str(call_id)

        results = await asyncio.gather(
            *(worker(i) for i in range(competing_transactions)), return_exceptions=True
        )

        successful_commits = [r for r in results if not isinstance(r, Exception)]
        rejected_operations = [r for r in results if isinstance(r, IntegrityError)]

        assert len(successful_commits) == 1
        assert len(rejected_operations) == competing_transactions - 1


class TestEvaluationResultConstraint:
    """Invariant: UNIQUE(evaluation_run_id, case_id) on opspilot.evaluation_results."""

    async def test_evaluation_results_unique_constraint_exists(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            stmt = text("""
                SELECT conname
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
                WHERE nsp.nspname = 'opspilot'
                  AND rel.relname = 'evaluation_results'
                  AND con.conname = 'uq_evaluation_results_evaluation_run_id_case_id'
                  AND con.contype = 'u';
            """)
            res = await session.execute(stmt)
            assert res.scalar_one_or_none() == "uq_evaluation_results_evaluation_run_id_case_id"

    async def test_concurrent_evaluation_result_case_race(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        eval_run_id = uuid.uuid4()
        agent_run_id = uuid.uuid4()
        deadline = datetime.now(UTC) + timedelta(minutes=5)

        async with session_factory() as session:
            await session.execute(
                text("""
                    INSERT INTO opspilot.agent_runs (id, user_request, planner_kind, deadline_at)
                    VALUES (:arid, 'Eval race', 'rules', :deadline);
                """),
                {"arid": agent_run_id, "deadline": deadline},
            )
            await session.execute(
                text("""
                    INSERT INTO opspilot.evaluation_runs (id, suite, planner_kind)
                    VALUES (:erid, 'race_suite', 'rules');
                """),
                {"erid": eval_run_id},
            )
            await session.commit()

        competing_transactions = 10

        async def worker(idx: int) -> str:
            async with session_factory() as session:
                res_id = uuid.uuid4()
                await session.execute(
                    text("""
                        INSERT INTO opspilot.evaluation_results
                        (id, evaluation_run_id, case_id, run_id, passed)
                        VALUES (:id, :erid, 'canonical_case_1', :arid, true);
                    """),
                    {"id": res_id, "erid": eval_run_id, "arid": agent_run_id},
                )
                await session.commit()
                return str(res_id)

        results = await asyncio.gather(
            *(worker(i) for i in range(competing_transactions)), return_exceptions=True
        )

        successful_commits = [r for r in results if not isinstance(r, Exception)]
        rejected_operations = [r for r in results if isinstance(r, IntegrityError)]

        assert len(successful_commits) == 1
        assert len(rejected_operations) == competing_transactions - 1


class TestMockCrmUniquenessConstraints:
    """Invariants: UNIQUE domain, email, message_id in mock_crm."""

    async def test_unique_company_domain_race(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        domain = f"unique-{uuid.uuid4().hex[:8]}.example"
        competing_transactions = 5

        async def worker(idx: int) -> str:
            async with session_factory() as session:
                cid = f"c_{uuid.uuid4().hex[:8]}"
                await session.execute(
                    text("""
                        INSERT INTO mock_crm.companies (company_id, name, domain)
                        VALUES (:cid, 'Co', :domain);
                    """),
                    {"cid": cid, "domain": domain},
                )
                await session.commit()
                return cid

        results = await asyncio.gather(
            *(worker(i) for i in range(competing_transactions)), return_exceptions=True
        )

        successful_commits = [r for r in results if not isinstance(r, Exception)]
        rejected_operations = [r for r in results if isinstance(r, IntegrityError)]

        assert len(successful_commits) == 1
        assert len(rejected_operations) == competing_transactions - 1

    async def test_unique_customer_email_race(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        email = f"cust-{uuid.uuid4().hex[:8]}@example.com"
        competing_transactions = 5

        async def worker(idx: int) -> str:
            async with session_factory() as session:
                cid = f"cust_{uuid.uuid4().hex[:8]}"
                await session.execute(
                    text("""
                        INSERT INTO mock_crm.customers
                        (customer_id, account_name, primary_contact, email)
                        VALUES (:cid, 'Acc', 'Cont', :email);
                    """),
                    {"cid": cid, "email": email},
                )
                await session.commit()
                return cid

        results = await asyncio.gather(
            *(worker(i) for i in range(competing_transactions)), return_exceptions=True
        )

        successful_commits = [r for r in results if not isinstance(r, Exception)]
        rejected_operations = [r for r in results if isinstance(r, IntegrityError)]

        assert len(successful_commits) == 1
        assert len(rejected_operations) == competing_transactions - 1


class TestCustomerOptimisticConcurrency:
    """Invariant: Lost update prevention via `customers.version`."""

    async def test_customer_version_default_is_1(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        cust_id = f"cust_def_{uuid.uuid4().hex[:8]}"
        async with session_factory() as session:
            await session.execute(
                text("""
                    INSERT INTO mock_crm.customers
                    (customer_id, account_name, primary_contact, email)
                    VALUES (:cid, 'Acc', 'Cont', :email);
                """),
                {"cid": cust_id, "email": f"{cust_id}@example.com"},
            )
            await session.commit()

            res = await session.execute(
                text("SELECT version FROM mock_crm.customers WHERE customer_id = :cid;"),
                {"cid": cust_id},
            )
            assert res.scalar_one() == 1

    async def test_concurrent_optimistic_update_race_prevents_lost_updates(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        cust_id = f"cust_race_{uuid.uuid4().hex[:8]}"
        async with session_factory() as session:
            await session.execute(
                text("""
                    INSERT INTO mock_crm.customers
                    (customer_id, account_name, primary_contact, email, version)
                    VALUES (:cid, 'Acc', 'Cont', :email, 1);
                """),
                {"cid": cust_id, "email": f"{cust_id}@example.com"},
            )
            await session.commit()

        competing_transactions = 10

        # 10 concurrent transactions all attempt to update version 1 -> 2
        async def worker(idx: int) -> int:
            async with session_factory() as session:
                res = await session.execute(
                    text("""
                        UPDATE mock_crm.customers
                        SET plan = :plan, version = version + 1, updated_at = now()
                        WHERE customer_id = :cid AND version = 1
                        RETURNING version;
                    """),
                    {"cid": cust_id, "plan": f"Plan-{idx}"},
                )
                await session.commit()
                row = res.fetchone()
                return 1 if row is not None else 0

        results = await asyncio.gather(*(worker(i) for i in range(competing_transactions)))

        successful_updates = sum(results)
        stale_write_rejections = competing_transactions - successful_updates

        # Exactly 1 update must succeed; 9 must return 0 rows
        assert successful_updates == 1
        assert stale_write_rejections == competing_transactions - 1

        # Final DB state: version is exactly 2
        async with session_factory() as session:
            res = await session.execute(
                text("SELECT version FROM mock_crm.customers WHERE customer_id = :cid;"),
                {"cid": cust_id},
            )
            assert res.scalar_one() == 2


class TestReferentialIntegrity:
    """Invariants: Cascades delete owned history; Restrict blocks deleting referenced parents."""

    async def test_agent_run_cascade_deletes_owned_history(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        step_id = uuid.uuid4()
        call_id = uuid.uuid4()
        appr_id = uuid.uuid4()
        deadline = datetime.now(UTC) + timedelta(minutes=5)
        now = datetime.now(UTC)

        async with session_factory() as session:
            await session.execute(
                text("""
                    INSERT INTO opspilot.agent_runs (id, user_request, planner_kind, deadline_at)
                    VALUES (:rid, 'Cascade run', 'rules', :deadline);
                """),
                {"rid": run_id, "deadline": deadline},
            )
            await session.execute(
                text("""
                    INSERT INTO opspilot.execution_steps
                    (id, run_id, step_id, plan_revision, seq, tool)
                    VALUES (:sid, :rid, 's1', 0, 1, 'search_leads');
                """),
                {"sid": step_id, "rid": run_id},
            )
            await session.execute(
                text("""
                    INSERT INTO opspilot.tool_calls
                    (id, run_id, execution_step_id, step_id, attempt, tool, status)
                    VALUES (:cid, :rid, :sid, 's1', 1, 'search_leads', 'succeeded');
                """),
                {"cid": call_id, "rid": run_id, "sid": step_id},
            )
            await session.execute(
                text("""
                    INSERT INTO opspilot.approvals
                    (id, run_id, step_id, tool, risk, title, summary,
                     args_hash, requested_at, expires_at)
                    VALUES (:aid, :rid, 's1', 'send_email_mock', 'high',
                            'T', 'S', 'h', :now, :now);
                """),
                {"aid": appr_id, "rid": run_id, "now": now},
            )
            await session.execute(
                text("""
                    INSERT INTO opspilot.trace_events (run_id, seq, kind)
                    VALUES (:rid, 1, 'run_started');
                """),
                {"rid": run_id},
            )
            await session.commit()

        # Delete run directly in database
        async with session_factory() as session:
            await session.execute(
                text("DELETE FROM opspilot.agent_runs WHERE id = :rid;"),
                {"rid": run_id},
            )
            await session.commit()

        # Verify all children were cascaded
        async with session_factory() as session:
            res_steps = await session.execute(
                text("SELECT count(*) FROM opspilot.execution_steps WHERE run_id = :rid;"),
                {"rid": run_id},
            )
            res_calls = await session.execute(
                text("SELECT count(*) FROM opspilot.tool_calls WHERE run_id = :rid;"),
                {"rid": run_id},
            )
            res_appr = await session.execute(
                text("SELECT count(*) FROM opspilot.approvals WHERE run_id = :rid;"),
                {"rid": run_id},
            )
            res_trace = await session.execute(
                text("SELECT count(*) FROM opspilot.trace_events WHERE run_id = :rid;"),
                {"rid": run_id},
            )

            assert res_steps.scalar_one() == 0
            assert res_calls.scalar_one() == 0
            assert res_appr.scalar_one() == 0
            assert res_trace.scalar_one() == 0

    async def test_restrict_rules_prevent_history_deletion(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        comp_id = f"c_res_{uuid.uuid4().hex[:8]}"
        lead_id = f"l_res_{uuid.uuid4().hex[:8]}"
        draft_id = f"d_res_{uuid.uuid4().hex[:8]}"
        outbox_id = f"o_res_{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        eval_run_id = uuid.uuid4()
        eval_res_id = uuid.uuid4()
        now = datetime.now(UTC)
        deadline = now + timedelta(minutes=5)

        async with session_factory() as session:
            await session.execute(
                text("""
                    INSERT INTO mock_crm.companies (company_id, name, domain)
                    VALUES (:cid, 'Co', :domain);
                """),
                {"cid": comp_id, "domain": f"{comp_id}.example"},
            )
            await session.execute(
                text("""
                    INSERT INTO mock_crm.leads (lead_id, company_id, full_name, email)
                    VALUES (:lid, :cid, 'Lead', 'lead@example.com');
                """),
                {"lid": lead_id, "cid": comp_id},
            )
            await session.execute(
                text("""
                    INSERT INTO mock_crm.outreach_drafts
                    (draft_id, lead_id, subject, body, content_hash)
                    VALUES (:did, :lid, 'Sub', 'Body', 'hash');
                """),
                {"did": draft_id, "lid": lead_id},
            )
            await session.execute(
                text("""
                    INSERT INTO mock_crm.email_outbox
                    (outbox_id, message_id, draft_id, to_email, subject, body, idempotency_key)
                    VALUES (:oid, :mid, :did, 'lead@example.com', 'Sub', 'Body', :idem);
                """),
                {
                    "oid": outbox_id,
                    "mid": f"m_{uuid.uuid4().hex[:8]}",
                    "did": draft_id,
                    "idem": f"idem_{uuid.uuid4().hex[:8]}",
                },
            )
            await session.execute(
                text("""
                    INSERT INTO opspilot.agent_runs (id, user_request, planner_kind, deadline_at)
                    VALUES (:rid, 'Eval run', 'rules', :deadline);
                """),
                {"rid": run_id, "deadline": deadline},
            )
            await session.execute(
                text("""
                    INSERT INTO opspilot.evaluation_runs (id, suite, planner_kind)
                    VALUES (:erid, 'suite', 'rules');
                """),
                {"erid": eval_run_id},
            )
            await session.execute(
                text("""
                    INSERT INTO opspilot.evaluation_results
                    (id, evaluation_run_id, case_id, run_id, passed)
                    VALUES (:ersid, :erid, 'case1', :rid, true);
                """),
                {"ersid": eval_res_id, "erid": eval_run_id, "rid": run_id},
            )
            await session.commit()

        # 1. Deleting company with leads must be blocked by PostgreSQL RESTRICT/NO ACTION
        async with session_factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text("DELETE FROM mock_crm.companies WHERE company_id = :cid;"),
                    {"cid": comp_id},
                )

        # 2. Deleting lead with drafts must be blocked
        async with session_factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text("DELETE FROM mock_crm.leads WHERE lead_id = :lid;"),
                    {"lid": lead_id},
                )

        # 3. Deleting draft with outbox entries must be blocked
        async with session_factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text("DELETE FROM mock_crm.outreach_drafts WHERE draft_id = :did;"),
                    {"did": draft_id},
                )

        # 4. Deleting AgentRun referenced by EvaluationResult must be blocked (FK RESTRICT)
        async with session_factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text("DELETE FROM opspilot.agent_runs WHERE id = :rid;"),
                    {"rid": run_id},
                )


class TestEnumCheckConstraints:
    """Invariant: PostgreSQL CHECK constraints enforce vocabulary for all enum columns."""

    @pytest.mark.parametrize(
        ("table", "column", "raw_sql", "params"),
        [
            (
                "opspilot.agent_runs",
                "status",
                "INSERT INTO opspilot.agent_runs "
                "(id, user_request, planner_kind, deadline_at, status) "
                "VALUES (:id, 'req', 'rules', now(), 'bogus');",
                {"id": uuid.uuid4()},
            ),
            (
                "opspilot.execution_steps",
                "status",
                "INSERT INTO opspilot.execution_steps "
                "(id, run_id, step_id, plan_revision, seq, tool, status) "
                "VALUES (:id, :rid, 's1', 0, 1, 'search_leads', 'bogus');",
                {"id": uuid.uuid4(), "rid": uuid.uuid4()},
            ),
            (
                "opspilot.execution_steps",
                "verification_status",
                "INSERT INTO opspilot.execution_steps "
                "(id, run_id, step_id, plan_revision, seq, tool, verification_status) "
                "VALUES (:id, :rid, 's1', 0, 1, 'search_leads', 'bogus');",
                {"id": uuid.uuid4(), "rid": uuid.uuid4()},
            ),
            (
                "opspilot.tool_calls",
                "status",
                "INSERT INTO opspilot.tool_calls "
                "(id, run_id, execution_step_id, step_id, attempt, tool, status) "
                "VALUES (:id, :rid, :sid, 's1', 1, 'search_leads', 'bogus');",
                {"id": uuid.uuid4(), "rid": uuid.uuid4(), "sid": uuid.uuid4()},
            ),
            (
                "opspilot.approvals",
                "status",
                "INSERT INTO opspilot.approvals "
                "(id, run_id, step_id, tool, risk, title, summary, "
                "args_hash, requested_at, expires_at, status) "
                "VALUES (:id, :rid, 's1', 'send_email_mock', 'high', "
                "'T', 'S', 'h', now(), now(), 'bogus');",
                {"id": uuid.uuid4(), "rid": uuid.uuid4()},
            ),
            (
                "opspilot.approvals",
                "risk",
                "INSERT INTO opspilot.approvals "
                "(id, run_id, step_id, tool, risk, title, summary, "
                "args_hash, requested_at, expires_at) "
                "VALUES (:id, :rid, 's1', 'send_email_mock', 'bogus', "
                "'T', 'S', 'h', now(), now());",
                {"id": uuid.uuid4(), "rid": uuid.uuid4()},
            ),
            (
                "opspilot.trace_events",
                "kind",
                "INSERT INTO opspilot.trace_events (run_id, seq, kind) VALUES (:rid, 1, 'bogus');",
                {"rid": uuid.uuid4()},
            ),
            (
                "opspilot.trace_events",
                "severity",
                "INSERT INTO opspilot.trace_events (run_id, seq, kind, severity) "
                "VALUES (:rid, 1, 'run_started', 'bogus');",
                {"rid": uuid.uuid4()},
            ),
            (
                "opspilot.evaluation_runs",
                "status",
                "INSERT INTO opspilot.evaluation_runs (id, suite, planner_kind, status) "
                "VALUES (:id, 'suite', 'rules', 'bogus');",
                {"id": uuid.uuid4()},
            ),
            (
                "mock_crm.leads",
                "status",
                "INSERT INTO mock_crm.leads (lead_id, company_id, full_name, email, status) "
                "VALUES (:lid, 'cid', 'Name', 'a@b.com', 'bogus');",
                {"lid": f"l_{uuid.uuid4().hex[:8]}"},
            ),
            (
                "mock_crm.customers",
                "status",
                "INSERT INTO mock_crm.customers "
                "(customer_id, account_name, primary_contact, email, status) "
                "VALUES (:cid, 'Acc', 'Cont', 'c@b.com', 'bogus');",
                {"cid": f"c_{uuid.uuid4().hex[:8]}"},
            ),
            (
                "mock_crm.outreach_drafts",
                "status",
                "INSERT INTO mock_crm.outreach_drafts "
                "(draft_id, lead_id, subject, body, content_hash, status) "
                "VALUES (:did, 'lid', 'Sub', 'Body', 'hash', 'bogus');",
                {"did": f"d_{uuid.uuid4().hex[:8]}"},
            ),
            (
                "mock_crm.email_outbox",
                "status",
                "INSERT INTO mock_crm.email_outbox "
                "(outbox_id, message_id, draft_id, to_email, subject, body, "
                "idempotency_key, status) "
                "VALUES (:oid, 'mid', 'did', 'a@b.com', 'Sub', 'Body', 'idem', 'bogus');",
                {"oid": f"o_{uuid.uuid4().hex[:8]}"},
            ),
        ],
    )
    async def test_enum_check_constraint_rejects_raw_invalid_value(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        table: str,
        column: str,
        raw_sql: str,
        params: dict[str, Any],
    ) -> None:
        async with session_factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(text(raw_sql), params)
