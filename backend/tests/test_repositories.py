"""Integration tests for the async repository layer against real PostgreSQL (§12, DB-005).

Validates:
1. Repository creation (standalone and via UnitOfWork)
2. Retrieval by identifier (all 11 aggregates)
3. Missing record behavior (returns None)
4. List and query behavior (filtering, pagination, orphan detection)
5. Update behavior
6. Transaction rollback behavior on error
7. Explicit commit behavior (rollback-by-default on uncommitted exit)
8. Session cleanup and release
9. Repository isolation
10. Constraint enforcement at the database level
11. State transitions (atomic approval decision, optimistic customer versioning)
12. No ORM session escapes repository boundaries
13. Repository protocols are satisfied by their implementations
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from app.agent.state import ApprovalStatus, PlannerKind, RunStatus, StepStatus, VerificationStatus
from app.config import get_settings
from app.persistence.mock_crm import (
    Company,
    Customer,
    CustomerStatus,
    EmailOutbox,
    EmailOutboxStatus,
    Lead,
    LeadStatus,
    OutreachDraft,
    OutreachDraftStatus,
)
from app.persistence.models import (
    AgentRun,
    EvaluationRunStatus,
    ToolCallStatus,
    TraceEventKind,
)
from app.persistence.protocols import (
    AgentRunRepository,
    ApprovalRepository,
    CompanyRepository,
    CustomerRepository,
    EmailOutboxRepository,
    EvaluationRepository,
    ExecutionStepRepository,
    LeadRepository,
    OutreachDraftRepository,
    ToolCallRepository,
    TraceEventRepository,
    UnitOfWork,
)
from app.persistence.repositories import (
    SqlAgentRunRepository,
    SqlApprovalRepository,
    SqlCompanyRepository,
    SqlCustomerRepository,
    SqlEmailOutboxRepository,
    SqlEvaluationRepository,
    SqlExecutionStepRepository,
    SqlLeadRepository,
    SqlOutreachDraftRepository,
    SqlToolCallRepository,
    SqlTraceEventRepository,
    SqlUnitOfWork,
)
from app.persistence.session import create_session_factory, unit_of_work
from app.tools.contracts import RiskLevel
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
        pytest.skip("no reachable Postgres for this session (DB-005 integration test)")


@pytest.fixture(scope="module", autouse=True)
def _migrated_database() -> None:
    _require_database()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url.get_secret_value())
    command.upgrade(config, "head")


@pytest.fixture
async def async_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url.get_secret_value(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(async_engine)


class TestRepositoryProtocols:
    """13. Repository protocols are satisfied by their implementations."""

    def test_sql_repositories_satisfy_protocols(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async def check() -> None:
            async with session_factory() as session:
                agent_repo = SqlAgentRunRepository(session)
                step_repo = SqlExecutionStepRepository(session)
                call_repo = SqlToolCallRepository(session)
                appr_repo = SqlApprovalRepository(session)
                trace_repo = SqlTraceEventRepository(session)
                eval_repo = SqlEvaluationRepository(session)
                comp_repo = SqlCompanyRepository(session)
                lead_repo = SqlLeadRepository(session)
                cust_repo = SqlCustomerRepository(session)
                draft_repo = SqlOutreachDraftRepository(session)
                outbox_repo = SqlEmailOutboxRepository(session)
                uow = SqlUnitOfWork(session_factory)

                assert isinstance(agent_repo, AgentRunRepository)
                assert isinstance(step_repo, ExecutionStepRepository)
                assert isinstance(call_repo, ToolCallRepository)
                assert isinstance(appr_repo, ApprovalRepository)
                assert isinstance(trace_repo, TraceEventRepository)
                assert isinstance(eval_repo, EvaluationRepository)
                assert isinstance(comp_repo, CompanyRepository)
                assert isinstance(lead_repo, LeadRepository)
                assert isinstance(cust_repo, CustomerRepository)
                assert isinstance(draft_repo, OutreachDraftRepository)
                assert isinstance(outbox_repo, EmailOutboxRepository)
                assert isinstance(uow, UnitOfWork)

        asyncio.run(check())

    def test_protocols_do_not_expose_orm_sessions_or_queries(self) -> None:
        protocols = [
            AgentRunRepository,
            ExecutionStepRepository,
            ToolCallRepository,
            ApprovalRepository,
            TraceEventRepository,
            EvaluationRepository,
            CompanyRepository,
            LeadRepository,
            CustomerRepository,
            OutreachDraftRepository,
            EmailOutboxRepository,
            UnitOfWork,
        ]
        forbidden_names = {"session", "query", "select", "execute", "raw_connection"}
        for proto in protocols:
            members = [name for name, _ in inspect.getmembers(proto)]
            leaks = set(members) & forbidden_names
            assert not leaks, f"Protocol {proto.__name__} leaked internal ORM members: {leaks}"


class TestUnitOfWorkTransactionSemantics:
    """6, 7, 8, 9. Explicit commit, rollback on error, rollback on uncommitted exit, cleanup."""

    async def test_explicit_commit_persists_changes(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        deadline = datetime.now(UTC) + timedelta(minutes=5)

        async with unit_of_work(session_factory) as uow:
            run = await uow.agent_runs.create(
                id=run_id,
                user_request="Commit test run",
                deadline_at=deadline,
            )
            assert run.id == run_id
            await uow.commit()

        # Read back in a new transaction
        async with unit_of_work(session_factory) as uow:
            found = await uow.agent_runs.get(run_id)
            assert found is not None
            assert found.user_request == "Commit test run"

    async def test_rollback_on_uncommitted_exit(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Architecture requires explicit commit: exiting context without commit rolls back."""
        run_id = uuid.uuid4()
        deadline = datetime.now(UTC) + timedelta(minutes=5)

        async with unit_of_work(session_factory) as uow:
            await uow.agent_runs.create(
                id=run_id,
                user_request="Uncommitted run",
                deadline_at=deadline,
            )
            # Deliberately NOT calling await uow.commit()

        # Must not exist in DB
        async with unit_of_work(session_factory) as uow:
            found = await uow.agent_runs.get(run_id)
            assert found is None

    async def test_rollback_on_exception(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        deadline = datetime.now(UTC) + timedelta(minutes=5)

        with pytest.raises(RuntimeError, match="Simulated error"):
            async with unit_of_work(session_factory) as uow:
                await uow.agent_runs.create(
                    id=run_id,
                    user_request="Failed run",
                    deadline_at=deadline,
                )
                raise RuntimeError("Simulated error")

        async with unit_of_work(session_factory) as uow:
            found = await uow.agent_runs.get(run_id)
            assert found is None

    async def test_session_cleanup_and_closed_safety(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        uow = SqlUnitOfWork(session_factory)
        async with uow:
            assert uow._session is not None
            await uow.commit()

        # After exiting, session must be closed and set to None
        assert uow._session is None
        with pytest.raises(RuntimeError, match="not active"):
            await uow.commit()

    async def test_repository_isolation_under_concurrency(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        deadline = datetime.now(UTC) + timedelta(minutes=5)

        async with unit_of_work(session_factory) as uow1:
            await uow1.agent_runs.create(
                id=run_id,
                user_request="Isolated run",
                deadline_at=deadline,
            )

            # Inside uow2 (different transaction), uncommitted run1 must not be visible
            async with unit_of_work(session_factory) as uow2:
                uncommitted = await uow2.agent_runs.get(run_id)
                assert uncommitted is None

            await uow1.commit()

        # Now committed: visible to uow3
        async with unit_of_work(session_factory) as uow3:
            committed = await uow3.agent_runs.get(run_id)
            assert committed is not None


class TestAgentRunRepository:
    """1, 2, 3, 4, 5. AgentRun CRUD, missing records, update, counters, lease heartbeat, orphans."""

    async def test_agent_run_lifecycle_and_queries(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        idem_key = f"idem-{uuid.uuid4()}"
        deadline = datetime.now(UTC) + timedelta(minutes=5)

        async with unit_of_work(session_factory) as uow:
            # 1. Create
            run = await uow.agent_runs.create(
                id=run_id,
                user_request="Find fintech leads",
                deadline_at=deadline,
                idempotency_key=idem_key,
            )
            assert run.id == run_id
            assert run.status == RunStatus.CREATED
            assert run.step_count == 0

            # 2. Get by ID and idempotency key
            by_id = await uow.agent_runs.get(run_id)
            assert by_id is not None
            assert by_id.id == run_id

            by_key = await uow.agent_runs.get_by_idempotency_key(idem_key)
            assert by_key is not None
            assert by_key.id == run_id

            # 3. Missing record returns None
            missing = await uow.agent_runs.get(uuid.uuid4())
            assert missing is None

            # 4. Updates
            now = datetime.now(UTC)
            updated = await uow.agent_runs.update_status(
                run_id, status=RunStatus.RUNNING, started_at=now
            )
            assert updated is not None
            assert updated.status == RunStatus.RUNNING

            plan_updated = await uow.agent_runs.update_plan(
                run_id, plan={"steps": ["s1", "s2"]}, plan_revision=1
            )
            assert plan_updated is not None
            assert plan_updated.plan_revision == 1

            counters = await uow.agent_runs.increment_counters(
                run_id, step_count_delta=2, retry_delta=1, replan_delta=1
            )
            assert counters is not None
            assert counters.step_count == 2
            assert counters.retry_total == 1
            assert counters.replan_count == 1

            # 5. Lease heartbeat and orphan query (DB-007: leases are owned
            # and fenced — a stale lease is one acquired at a `now` in the past)
            ttl = timedelta(seconds=30)
            long_ago = datetime.now(UTC) - timedelta(minutes=5)
            claimed = await uow.agent_runs.acquire_lease(
                run_id, owner="worker-a", now=long_ago, ttl=ttl
            )
            assert claimed is not None
            assert claimed.lease_owner == "worker-a"
            assert claimed.lease_expires_at == long_ago + ttl

            # The owner cannot revive a lease that has already expired.
            revived = await uow.agent_runs.heartbeat_lease(
                run_id, owner="worker-a", now=datetime.now(UTC), ttl=ttl
            )
            assert revived is False

            orphans = await uow.agent_runs.list_orphaned_runs(now=datetime.now(UTC))
            orphan_ids = [o.id for o in orphans]
            assert run_id in orphan_ids

            # Update final response
            completed = await uow.agent_runs.update_final_response(
                run_id,
                final_response={"summary": "done"},
                status=RunStatus.COMPLETED,
                finished_at=datetime.now(UTC),
                duration_ms=500,
            )
            assert completed is not None
            assert completed.status == RunStatus.COMPLETED

            # List runs with filter
            runs = await uow.agent_runs.list_runs(status=RunStatus.COMPLETED)
            assert any(r.id == run_id for r in runs)

            await uow.commit()


class TestExecutionStepAndToolCallRepositories:
    """ExecutionStep and ToolCallRow aggregate persistence and attempt tracking."""

    async def test_step_and_tool_call_lifecycle(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        step_uuid = uuid.uuid4()
        deadline = datetime.now(UTC) + timedelta(minutes=5)

        async with unit_of_work(session_factory) as uow:
            await uow.agent_runs.create(id=run_id, user_request="Step test", deadline_at=deadline)

            step = await uow.execution_steps.create(
                id=step_uuid,
                run_id=run_id,
                step_id="s1",
                plan_revision=0,
                seq=1,
                tool="search_leads",
                args={"query": "fintech"},
            )
            assert step.id == step_uuid
            assert step.status == StepStatus.PENDING

            # Query by plan step_id
            by_step = await uow.execution_steps.get_by_step_id(run_id, "s1", 0)
            assert by_step is not None
            assert by_step.id == step_uuid

            # Record attempts & verification
            await uow.execution_steps.increment_attempts(step_uuid, retry_count_delta=0)
            await uow.execution_steps.record_verification(
                step_uuid,
                verification_status=VerificationStatus.PASSED,
                verification={"passed": True},
            )
            verified_step = await uow.execution_steps.record_result(
                step_uuid, result={"leads": ["lead_1"]}
            )
            assert verified_step is not None
            assert verified_step.status == StepStatus.SUCCEEDED
            assert verified_step.verification_status == VerificationStatus.PASSED

            # ToolCall attempt
            call_key = f"call-key-{uuid.uuid4()}"
            call = await uow.tool_calls.record_call(
                run_id=run_id,
                execution_step_id=step_uuid,
                step_id="s1",
                attempt=1,
                tool="search_leads",
                input={"query": "fintech"},
                status=ToolCallStatus.SUCCEEDED,
                output={"leads": ["lead_1"]},
                idempotency_key=call_key,
            )
            assert call.attempt == 1

            by_call_key = await uow.tool_calls.get_by_idempotency_key(call_key)
            assert by_call_key is not None
            assert by_call_key.id == call.id

            step_calls = await uow.tool_calls.list_by_step(step_uuid)
            assert len(step_calls) == 1

            await uow.commit()


class TestApprovalRepository:
    """10, 11. Approval state machine, atomic single-flight decide, constraint enforcement."""

    async def test_atomic_approval_decision_state_transition(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        appr_id = uuid.uuid4()
        now = datetime.now(UTC)
        deadline = now + timedelta(minutes=5)
        expires = now + timedelta(hours=1)

        async with unit_of_work(session_factory) as uow:
            await uow.agent_runs.create(
                id=run_id, user_request="Approval test", deadline_at=deadline
            )
            appr = await uow.approvals.create_request(
                id=appr_id,
                run_id=run_id,
                step_id="s3",
                tool="send_email_mock",
                risk=RiskLevel.HIGH,
                title="Send outreach",
                summary="Send email to lead",
                payload_preview={"to": "lead@example.com"},
                args_hash="sha256-hash-abc",
                requested_at=now,
                expires_at=expires,
            )
            assert appr.status == ApprovalStatus.PENDING

            # 1st decision: wins and returns row
            decided = await uow.approvals.decide(
                appr_id,
                status=ApprovalStatus.APPROVED,
                decided_by="operator-1",
                decision_reason="Looks good",
                decided_at=datetime.now(UTC),
            )
            assert decided is not None
            assert decided.status == ApprovalStatus.APPROVED
            assert decided.decided_by == "operator-1"

            # 2nd decision on already decided approval: returns None (409 conflict simulation)
            conflict = await uow.approvals.decide(
                appr_id,
                status=ApprovalStatus.REJECTED,
                decided_by="operator-2",
                decided_at=datetime.now(UTC),
            )
            assert conflict is None

            await uow.commit()

    async def test_duplicate_pending_approval_constraint_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        now = datetime.now(UTC)
        deadline = now + timedelta(minutes=5)
        expires = now + timedelta(hours=1)

        async with unit_of_work(session_factory) as uow:
            await uow.agent_runs.create(
                id=run_id, user_request="Pending constraint test", deadline_at=deadline
            )
            await uow.approvals.create_request(
                run_id=run_id,
                step_id="s2",
                tool="update_customer",
                risk=RiskLevel.HIGH,
                title="Update customer",
                summary="Patch record",
                payload_preview={},
                args_hash="hash-1",
                requested_at=now,
                expires_at=expires,
            )

            # Second pending approval on same (run_id, step_id) must be rejected
            # by PostgreSQL partial unique index
            with pytest.raises(IntegrityError):
                await uow.approvals.create_request(
                    run_id=run_id,
                    step_id="s2",
                    tool="update_customer",
                    risk=RiskLevel.HIGH,
                    title="Update customer 2",
                    summary="Patch record duplicate",
                    payload_preview={},
                    args_hash="hash-2",
                    requested_at=now,
                    expires_at=expires,
                )


class TestTraceEventRepository:
    """Atomic monotonic seq append with advisory lock and cursor listing."""

    async def test_trace_event_monotonic_sequence_and_cursor(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        deadline = datetime.now(UTC) + timedelta(minutes=5)

        async with unit_of_work(session_factory) as uow:
            await uow.agent_runs.create(id=run_id, user_request="Trace test", deadline_at=deadline)

            e1 = await uow.trace_events.append(run_id=run_id, kind=TraceEventKind.RUN_CREATED)
            e2 = await uow.trace_events.append(run_id=run_id, kind=TraceEventKind.RUN_STARTED)
            e3 = await uow.trace_events.append(run_id=run_id, kind=TraceEventKind.NODE_ENTERED)

            assert e1.seq == 1
            assert e2.seq == 2
            assert e3.seq == 3

            # Test cursor pagination (after_seq=1 -> returns e2, e3)
            cursor_events = await uow.trace_events.list_by_run(run_id, after_seq=1)
            assert len(cursor_events) == 2
            assert cursor_events[0].seq == 2
            assert cursor_events[1].seq == 3

            latest = await uow.trace_events.get_latest(run_id)
            assert latest is not None
            assert latest.seq == 3

            await uow.commit()

    async def test_concurrent_async_trace_event_appends_are_gapless_and_unique(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        deadline = datetime.now(UTC) + timedelta(minutes=5)

        async with unit_of_work(session_factory) as uow:
            await uow.agent_runs.create(
                id=run_id, user_request="Concurrent trace test", deadline_at=deadline
            )
            await uow.commit()

        total_events = 15

        async def worker(index: int) -> int:
            async with unit_of_work(session_factory) as uow:
                event = await uow.trace_events.append(
                    run_id=run_id,
                    kind=TraceEventKind.TOOL_STARTED,
                    step_id=f"step_{index}",
                )
                await uow.commit()
                return event.seq

        seqs = await asyncio.gather(*(worker(i) for i in range(total_events)))
        assert sorted(seqs) == list(range(1, total_events + 1))


class TestEvaluationRepository:
    """Evaluation suite runs and per-case results persistence."""

    async def test_evaluation_run_and_results(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        eval_run_id = uuid.uuid4()
        agent_run_id = uuid.uuid4()
        deadline = datetime.now(UTC) + timedelta(minutes=5)

        async with unit_of_work(session_factory) as uow:
            await uow.agent_runs.create(
                id=agent_run_id, user_request="Eval run test", deadline_at=deadline
            )

            eval_run = await uow.evaluations.create_run(
                id=eval_run_id,
                suite="e2e_canonical",
                planner_kind=PlannerKind.RULES,
                git_sha="abcdef123456",
            )
            assert eval_run.id == eval_run_id
            assert eval_run.status == EvaluationRunStatus.RUNNING

            res = await uow.evaluations.record_result(
                evaluation_run_id=eval_run_id,
                case_id="case_1_lead_search",
                run_id=agent_run_id,
                passed=True,
                assertions=[{"name": "found_leads", "passed": True}],
                duration_ms=120,
            )
            assert res.passed is True

            completed = await uow.evaluations.complete_run(
                eval_run_id,
                status=EvaluationRunStatus.COMPLETED,
                finished_at=datetime.now(UTC),
                case_count=1,
                passed=1,
                failed=0,
                metrics={"case_pass_rate": 1.0},
            )
            assert completed is not None
            assert completed.status == EvaluationRunStatus.COMPLETED

            all_results = await uow.evaluations.list_results(eval_run_id)
            assert len(all_results) == 1

            await uow.commit()


class TestMockCrmRepositories:
    """Mock CRM aggregates: Company, Lead, Customer, OutreachDraft, EmailOutbox."""

    async def test_mock_crm_aggregates_and_optimistic_concurrency(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        comp_id = f"comp_{uuid.uuid4().hex[:8]}"
        lead_id = f"lead_{uuid.uuid4().hex[:8]}"
        cust_id = f"cust_{uuid.uuid4().hex[:8]}"
        draft_id = f"draft_{uuid.uuid4().hex[:8]}"
        outbox_id = f"outbox_{uuid.uuid4().hex[:8]}"
        idem_key = f"outbox_idem_{uuid.uuid4()}"

        async with unit_of_work(session_factory) as uow:
            # 1. Company
            comp = Company(
                company_id=comp_id,
                name="Acme Fintech",
                domain=f"{comp_id}.example.com",
                industry="Fintech",
            )
            await uow.companies.create(comp)
            by_comp = await uow.companies.get(comp_id)
            assert by_comp is not None
            assert by_comp.name == "Acme Fintech"

            # 2. Lead
            lead = Lead(
                lead_id=lead_id,
                company_id=comp_id,
                full_name="Alice Smith",
                email=f"alice@{comp_id}.example.com",
                status=LeadStatus.NEW,
            )
            await uow.leads.create(lead)
            by_lead = await uow.leads.get(lead_id)
            assert by_lead is not None
            assert by_lead.company_id == comp_id

            await uow.leads.update_status(lead_id, LeadStatus.QUALIFIED)
            updated_lead = await uow.leads.get(lead_id)
            assert updated_lead is not None
            assert updated_lead.status == LeadStatus.QUALIFIED

            # 3. Customer & optimistic concurrency version check
            cust = Customer(
                customer_id=cust_id,
                account_name="Acme Corp",
                primary_contact="Bob",
                email=f"bob@{cust_id}.example.com",
                status=CustomerStatus.PROSPECT,
                version=1,
            )
            await uow.customers.create(cust)

            # Optimistic update with correct version 1 -> increments to 2
            opt_success = await uow.customers.update_optimistic(
                cust_id, expected_version=1, status=CustomerStatus.ACTIVE, plan="Enterprise"
            )
            assert opt_success is not None
            assert opt_success.version == 2
            assert opt_success.status == CustomerStatus.ACTIVE

            # Optimistic update with stale version 1 -> returns None (concurrency conflict)
            opt_conflict = await uow.customers.update_optimistic(
                cust_id, expected_version=1, notes="Stale update"
            )
            assert opt_conflict is None

            # 4. OutreachDraft
            draft = OutreachDraft(
                draft_id=draft_id,
                lead_id=lead_id,
                subject="Intro",
                body="Hello Alice",
                content_hash="hash123",
                status=OutreachDraftStatus.SAVED,
            )
            await uow.outreach_drafts.create(draft)
            by_draft = await uow.outreach_drafts.get(draft_id)
            assert by_draft is not None
            assert by_draft.subject == "Intro"

            # 5. EmailOutbox
            outbox = EmailOutbox(
                outbox_id=outbox_id,
                message_id=f"msg_{uuid.uuid4().hex[:8]}",
                draft_id=draft_id,
                to_email="alice@example.com",
                subject="Intro",
                body="Hello Alice",
                status=EmailOutboxStatus.SENT,
                idempotency_key=idem_key,
                run_id="run-1",
                approval_id="appr-1",
            )
            await uow.email_outbox.create(outbox)
            by_outbox = await uow.email_outbox.get_by_idempotency_key(idem_key)
            assert by_outbox is not None
            assert by_outbox.outbox_id == outbox_id

            await uow.commit()

        # Duplicate idempotency_key rejected by PostgreSQL database constraint
        with pytest.raises(IntegrityError):
            async with unit_of_work(session_factory) as err_uow:
                dup = EmailOutbox(
                    outbox_id=f"outbox_dup_{uuid.uuid4().hex[:8]}",
                    message_id=f"msg_dup_{uuid.uuid4().hex[:8]}",
                    draft_id=draft_id,
                    to_email="alice@example.com",
                    subject="Intro",
                    body="Hello Alice",
                    status=EmailOutboxStatus.SENT,
                    idempotency_key=idem_key,  # duplicate!
                )
                await err_uow.email_outbox.create(dup)


class TestNoOrmSessionEscapes:
    """12. No ORM session escapes repository boundaries."""

    async def test_repository_results_are_plain_entities(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        run_id = uuid.uuid4()
        deadline = datetime.now(UTC) + timedelta(minutes=5)

        async with unit_of_work(session_factory) as uow:
            run = await uow.agent_runs.create(
                id=run_id, user_request="Boundary test", deadline_at=deadline
            )
            await uow.commit()

        # The returned run instance should not expose an open session
        assert isinstance(run, AgentRun)
        assert run.id == run_id
        assert run.user_request == "Boundary test"
