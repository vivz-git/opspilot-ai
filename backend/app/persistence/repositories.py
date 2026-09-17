"""Asynchronous repository implementations (§12, DB-005).

All SQLAlchemy queries (`select()`, `update()`, `insert()`), advisory locks,
and transaction-specific operations are encapsulated here. No ORM session or query
construction escapes this module.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Any, Final, Self

import sqlalchemy as sa
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from app.agent.state import ApprovalStatus, PlannerKind, RunStatus, StepStatus, VerificationStatus
from app.persistence.mock_crm import (
    Company,
    Customer,
    CustomerStatus,
    EmailOutbox,
    Lead,
    LeadStatus,
    OutreachDraft,
    OutreachDraftStatus,
)
from app.persistence.models import (
    AgentRun,
    ApprovalRow,
    EvaluationResult,
    EvaluationRun,
    EvaluationRunStatus,
    ExecutionStep,
    ToolCallRow,
    ToolCallStatus,
    TraceEvent,
    TraceEventKind,
    TraceEventSeverity,
)
from app.persistence.protocols import ApprovalUpsert, RunListResult
from app.tools.contracts import RiskLevel, ToolName

__all__ = [
    "SqlAgentRunRepository",
    "SqlApprovalRepository",
    "SqlCompanyRepository",
    "SqlCustomerRepository",
    "SqlEmailOutboxRepository",
    "SqlEvaluationRepository",
    "SqlExecutionStepRepository",
    "SqlLeadRepository",
    "SqlOutreachDraftRepository",
    "SqlToolCallRepository",
    "SqlTraceEventRepository",
    "SqlUnitOfWork",
]


class SqlAgentRunRepository:
    """Async SQLAlchemy implementation of AgentRunRepository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, run_id: uuid.UUID) -> AgentRun | None:
        return await self._session.get(AgentRun, run_id)

    async def get_by_idempotency_key(self, idempotency_key: str) -> AgentRun | None:
        stmt = select(AgentRun).where(AgentRun.idempotency_key == idempotency_key)
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()

    async def create(
        self,
        *,
        user_request: str,
        planner_kind: PlannerKind = PlannerKind.RULES,
        deadline_at: datetime,
        id: uuid.UUID | None = None,
        parent_run_id: uuid.UUID | None = None,
        status: RunStatus = RunStatus.CREATED,
        status_reason: str | None = None,
        normalized_task: dict[str, Any] | None = None,
        plan: dict[str, Any] | None = None,
        plan_history: list[Any] | None = None,
        plan_revision: int = 0,
        final_response: dict[str, Any] | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        seed: int | None = None,
        idempotency_key: str | None = None,
        actor_id: str | None = None,
        evaluation_run_id: uuid.UUID | None = None,
        eval_case_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRun:
        run = AgentRun(
            user_request=user_request,
            planner_kind=planner_kind,
            deadline_at=deadline_at,
            parent_run_id=parent_run_id,
            status=status,
            status_reason=status_reason,
            normalized_task=normalized_task,
            plan=plan,
            plan_history=plan_history if plan_history is not None else [],
            plan_revision=plan_revision,
            final_response=final_response,
            model_id=model_id,
            prompt_version=prompt_version,
            seed=seed,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            evaluation_run_id=evaluation_run_id,
            eval_case_id=eval_case_id,
            metadata_=metadata if metadata is not None else {},
        )
        if id is not None:
            run.id = id
        self._session.add(run)
        await self._session.flush()
        return run

    async def update_status(
        self,
        run_id: uuid.UUID,
        *,
        status: RunStatus,
        status_reason: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
    ) -> AgentRun | None:
        run = await self.get(run_id)
        if run is None:
            return None
        run.status = status
        if status_reason is not None:
            run.status_reason = status_reason
        if started_at is not None:
            run.started_at = started_at
        if finished_at is not None:
            run.finished_at = finished_at
        if duration_ms is not None:
            run.duration_ms = duration_ms
        await self._session.flush()
        return run

    async def update_plan(
        self,
        run_id: uuid.UUID,
        *,
        plan: dict[str, Any],
        plan_revision: int,
        plan_history: list[Any] | None = None,
    ) -> AgentRun | None:
        run = await self.get(run_id)
        if run is None:
            return None
        run.plan = plan
        run.plan_revision = plan_revision
        if plan_history is not None:
            run.plan_history = plan_history
        await self._session.flush()
        return run

    async def update_final_response(
        self,
        run_id: uuid.UUID,
        *,
        final_response: dict[str, Any],
        status: RunStatus = RunStatus.COMPLETED,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
    ) -> AgentRun | None:
        run = await self.get(run_id)
        if run is None:
            return None
        run.final_response = final_response
        run.status = status
        if finished_at is not None:
            run.finished_at = finished_at
        if duration_ms is not None:
            run.duration_ms = duration_ms
        await self._session.flush()
        return run

    async def increment_counters(
        self,
        run_id: uuid.UUID,
        *,
        step_count_delta: int = 0,
        retry_delta: int = 0,
        replan_delta: int = 0,
    ) -> AgentRun | None:
        run = await self.get(run_id)
        if run is None:
            return None
        run.step_count += step_count_delta
        run.retry_total += retry_delta
        run.replan_count += replan_delta
        await self._session.flush()
        return run

    # -- Ownership and lifecycle (DB-007, ADR-023) ---------------------------
    #
    # Every method below is exactly one conditional `UPDATE`. Under Postgres's
    # default READ COMMITTED isolation, two concurrent `UPDATE`s of the same
    # row serialize on the row lock: the second waits for the first to commit,
    # then re-evaluates its `WHERE` clause against the *new* row version
    # (EvalPlanQual) and matches zero rows if the first one changed the lease
    # or status out from under it. That is what makes "check the lease and
    # take it" atomic without `SELECT … FOR UPDATE`, without SERIALIZABLE, and
    # without any Python-side read-then-write — the ownership decision is the
    # database's, and exactly one caller ever gets a row back.
    #
    # The expiry boundary is exact and complementary: a lease is *live* while
    # `lease_expires_at > now` (heartbeat allowed) and *expired* once
    # `lease_expires_at <= now` (claimable, and an orphan candidate). There is
    # no instant at which the old owner can still renew and a new owner can
    # already claim.

    async def transition_status(
        self,
        run_id: uuid.UUID,
        *,
        expected: Iterable[RunStatus],
        status: RunStatus,
        owner: str | None = None,
        status_reason: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
        release_lease: bool = False,
    ) -> AgentRun | None:
        values: dict[str, Any] = {"status": status}
        if status_reason is not None:
            values["status_reason"] = status_reason
        if started_at is not None:
            values["started_at"] = started_at
        if finished_at is not None:
            values["finished_at"] = finished_at
        if duration_ms is not None:
            values["duration_ms"] = duration_ms
        if release_lease:
            values["lease_owner"] = None
            values["lease_expires_at"] = None
        conditions = [AgentRun.id == run_id, AgentRun.status.in_(list(expected))]
        if owner is not None:
            conditions.append(AgentRun.lease_owner == owner)
        stmt = update(AgentRun).where(*conditions).values(**values).returning(AgentRun)
        res = await self._session.execute(stmt)
        await self._session.flush()
        return res.scalar_one_or_none()

    async def acquire_lease(
        self,
        run_id: uuid.UUID,
        *,
        owner: str,
        now: datetime,
        ttl: timedelta,
        expected: Iterable[RunStatus] = (RunStatus.QUEUED, RunStatus.RUNNING),
        status: RunStatus | None = None,
    ) -> AgentRun | None:
        values: dict[str, Any] = {"lease_owner": owner, "lease_expires_at": now + ttl}
        if status is not None:
            values["status"] = status
        stmt = (
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_(list(expected)),
                sa.or_(
                    AgentRun.lease_owner.is_(None),
                    AgentRun.lease_owner == owner,
                    AgentRun.lease_expires_at <= now,
                ),
            )
            .values(**values)
            .returning(AgentRun)
        )
        res = await self._session.execute(stmt)
        await self._session.flush()
        return res.scalar_one_or_none()

    async def heartbeat_lease(
        self, run_id: uuid.UUID, *, owner: str, now: datetime, ttl: timedelta
    ) -> bool:
        stmt = (
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
                AgentRun.lease_owner == owner,
                AgentRun.lease_expires_at > now,
            )
            .values(lease_expires_at=now + ttl)
        )
        res = await self._session.execute(stmt)
        await self._session.flush()
        return bool(getattr(res, "rowcount", 0) > 0)

    async def release_lease(self, run_id: uuid.UUID, *, owner: str) -> bool:
        stmt = (
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.lease_owner == owner)
            .values(lease_owner=None, lease_expires_at=None)
        )
        res = await self._session.execute(stmt)
        await self._session.flush()
        return bool(getattr(res, "rowcount", 0) > 0)

    async def list_runs(
        self,
        *,
        status: RunStatus | None = None,
        statuses: list[RunStatus] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        parent_run_id: uuid.UUID | None = None,
        query: str | None = None,
        cursor_created_at: datetime | None = None,
        cursor_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> RunListResult:
        conditions: list[Any] = []
        if status is not None:
            conditions.append(AgentRun.status == status)
        elif statuses is not None and len(statuses) > 0:
            conditions.append(AgentRun.status.in_(statuses))

        if since is not None:
            conditions.append(AgentRun.created_at >= since)
        if until is not None:
            conditions.append(AgentRun.created_at <= until)
        if parent_run_id is not None:
            conditions.append(AgentRun.parent_run_id == parent_run_id)
        if query is not None and query.strip():
            clean_q = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(AgentRun.user_request.ilike(f"%{clean_q}%"))

        # 1. Total estimate count under current filters
        count_stmt = select(sa.func.count()).select_from(AgentRun)
        if conditions:
            count_stmt = count_stmt.where(sa.and_(*conditions))
        total_res = await self._session.execute(count_stmt)
        total_estimate = int(total_res.scalar_one() or 0)

        # 2. Main query
        stmt = select(AgentRun)
        if conditions:
            stmt = stmt.where(sa.and_(*conditions))

        # Keyset pagination condition (§13.2)
        if cursor_created_at is not None and cursor_id is not None:
            keyset_cond = sa.or_(
                AgentRun.created_at < cursor_created_at,
                sa.and_(
                    AgentRun.created_at == cursor_created_at,
                    AgentRun.id < cursor_id,
                ),
            )
            stmt = stmt.where(keyset_cond)
            stmt = stmt.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit + 1)
            res = await self._session.execute(stmt)
            rows = list(res.scalars().all())
            has_more = len(rows) > limit
            items = rows[:limit]
            return RunListResult(items=items, has_more=has_more, total_estimate=total_estimate)

        # Offset-based or initial un-cursored query
        stmt = (
            stmt.order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .offset(offset)
            .limit(limit + 1)
        )
        res = await self._session.execute(stmt)
        rows = list(res.scalars().all())
        has_more = len(rows) > limit
        items = rows[:limit]
        return RunListResult(items=items, has_more=has_more, total_estimate=total_estimate)

    async def list_orphaned_runs(self, *, now: datetime, limit: int = 50) -> list[AgentRun]:
        stmt = (
            select(AgentRun)
            .where(
                AgentRun.status.in_([RunStatus.RUNNING, RunStatus.QUEUED]),
                sa.or_(AgentRun.lease_expires_at.is_(None), AgentRun.lease_expires_at <= now),
            )
            .order_by(AgentRun.lease_expires_at.asc().nulls_first())
            .limit(limit)
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())


class SqlExecutionStepRepository:
    """Async SQLAlchemy implementation of ExecutionStepRepository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, step_uuid: uuid.UUID) -> ExecutionStep | None:
        return await self._session.get(ExecutionStep, step_uuid)

    async def get_by_step_id(
        self, run_id: uuid.UUID, step_id: str, plan_revision: int
    ) -> ExecutionStep | None:
        stmt = select(ExecutionStep).where(
            ExecutionStep.run_id == run_id,
            ExecutionStep.step_id == step_id,
            ExecutionStep.plan_revision == plan_revision,
        )
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()

    async def list_by_run(self, run_id: uuid.UUID) -> list[ExecutionStep]:
        stmt = (
            select(ExecutionStep)
            .where(ExecutionStep.run_id == run_id)
            .order_by(ExecutionStep.seq.asc())
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def create(
        self,
        *,
        run_id: uuid.UUID,
        step_id: str,
        plan_revision: int,
        seq: int,
        tool: ToolName,
        tool_version: str = "1.0.0",
        parent_step_id: str | None = None,
        status: StepStatus = StepStatus.PENDING,
        args: dict[str, Any] | None = None,
        args_hash: str | None = None,
        depends_on: list[str] | None = None,
        optional: bool = False,
        id: uuid.UUID | None = None,
    ) -> ExecutionStep:
        step = ExecutionStep(
            run_id=run_id,
            step_id=step_id,
            plan_revision=plan_revision,
            seq=seq,
            tool=tool,
            tool_version=tool_version,
            parent_step_id=parent_step_id,
            status=status,
            args=args if args is not None else {},
            args_hash=args_hash,
            depends_on=depends_on if depends_on is not None else [],
            optional=optional,
        )
        if id is not None:
            step.id = id
        self._session.add(step)
        await self._session.flush()
        return step

    async def update_status(
        self,
        step_uuid: uuid.UUID,
        *,
        status: StepStatus,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
        error: dict[str, Any] | None = None,
    ) -> ExecutionStep | None:
        step = await self.get(step_uuid)
        if step is None:
            return None
        step.status = status
        if started_at is not None:
            step.started_at = started_at
        if finished_at is not None:
            step.finished_at = finished_at
        if duration_ms is not None:
            step.duration_ms = duration_ms
        if error is not None:
            step.error = error
        await self._session.flush()
        return step

    async def record_result(
        self,
        step_uuid: uuid.UUID,
        *,
        result: dict[str, Any],
        status: StepStatus = StepStatus.SUCCEEDED,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
    ) -> ExecutionStep | None:
        step = await self.get(step_uuid)
        if step is None:
            return None
        step.result = result
        step.status = status
        if finished_at is not None:
            step.finished_at = finished_at
        if duration_ms is not None:
            step.duration_ms = duration_ms
        await self._session.flush()
        return step

    async def record_verification(
        self,
        step_uuid: uuid.UUID,
        *,
        verification_status: VerificationStatus,
        verification: dict[str, Any] | None = None,
    ) -> ExecutionStep | None:
        step = await self.get(step_uuid)
        if step is None:
            return None
        step.verification_status = verification_status
        step.verification = verification
        await self._session.flush()
        return step

    async def increment_attempts(
        self, step_uuid: uuid.UUID, *, retry_count_delta: int = 0
    ) -> ExecutionStep | None:
        step = await self.get(step_uuid)
        if step is None:
            return None
        step.attempts += 1
        step.retry_count += retry_count_delta
        await self._session.flush()
        return step


class SqlToolCallRepository:
    """Async SQLAlchemy implementation of ToolCallRepository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, tool_call_id: uuid.UUID) -> ToolCallRow | None:
        return await self._session.get(ToolCallRow, tool_call_id)

    async def get_by_attempt(
        self, execution_step_id: uuid.UUID, attempt: int
    ) -> ToolCallRow | None:
        stmt = select(ToolCallRow).where(
            ToolCallRow.execution_step_id == execution_step_id,
            ToolCallRow.attempt == attempt,
        )
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_idempotency_key(self, idempotency_key: str) -> ToolCallRow | None:
        stmt = (
            select(ToolCallRow)
            .where(ToolCallRow.idempotency_key == idempotency_key)
            .order_by(ToolCallRow.started_at.desc())
            .limit(1)
        )
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()

    async def list_by_idempotency_key(self, idempotency_key: str) -> list[ToolCallRow]:
        stmt = (
            select(ToolCallRow)
            .where(ToolCallRow.idempotency_key == idempotency_key)
            .order_by(ToolCallRow.started_at.asc(), ToolCallRow.attempt.asc())
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def lock_idempotency_key(self, idempotency_key: str) -> None:
        # Same shape as the per-run trace lock: hashtextextended folds the key
        # into the 64-bit advisory-lock space; the lock is released with the
        # transaction, so a failed attempt can never leak it.
        lock_key = sa.func.hashtextextended(sa.cast(idempotency_key, sa.Text), 0)
        await self._session.execute(sa.select(sa.func.pg_advisory_xact_lock(lock_key)))

    async def list_by_step(self, execution_step_id: uuid.UUID) -> list[ToolCallRow]:
        stmt = (
            select(ToolCallRow)
            .where(ToolCallRow.execution_step_id == execution_step_id)
            .order_by(ToolCallRow.attempt.asc())
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def list_by_run(self, run_id: uuid.UUID) -> list[ToolCallRow]:
        stmt = (
            select(ToolCallRow)
            .where(ToolCallRow.run_id == run_id)
            .order_by(ToolCallRow.started_at.asc())
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def record_call(
        self,
        *,
        run_id: uuid.UUID,
        execution_step_id: uuid.UUID,
        step_id: str,
        attempt: int,
        tool: ToolName,
        input: dict[str, Any],
        status: ToolCallStatus,
        tool_version: str = "1.0.0",
        output: dict[str, Any] | None = None,
        input_hash: str | None = None,
        error_class: str | None = None,
        error_message: str | None = None,
        idempotency_key: str | None = None,
        port: str | None = None,
        adapter: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
        id: uuid.UUID | None = None,
    ) -> ToolCallRow:
        row = ToolCallRow(
            run_id=run_id,
            execution_step_id=execution_step_id,
            step_id=step_id,
            attempt=attempt,
            tool=tool,
            tool_version=tool_version,
            input=input,
            status=status,
            output=output,
            input_hash=input_hash,
            error_class=error_class,
            error_message=error_message,
            idempotency_key=idempotency_key,
            port=port,
            adapter=adapter,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
        )
        if id is not None:
            row.id = id
        self._session.add(row)
        await self._session.flush()
        return row


class SqlApprovalRepository:
    """Async SQLAlchemy implementation of ApprovalRepository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, approval_id: uuid.UUID, *, fresh: bool = False) -> ApprovalRow | None:
        if fresh:
            stmt = (
                select(ApprovalRow)
                .where(ApprovalRow.id == approval_id)
                .execution_options(populate_existing=True)
            )
            res = await self._session.execute(stmt)
            return res.scalar_one_or_none()
        return await self._session.get(ApprovalRow, approval_id)

    async def get_pending(self, run_id: uuid.UUID, step_id: str) -> ApprovalRow | None:
        stmt = select(ApprovalRow).where(
            ApprovalRow.run_id == run_id,
            ApprovalRow.step_id == step_id,
            ApprovalRow.status == ApprovalStatus.PENDING,
        )
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_approved(self, run_id: uuid.UUID, step_id: str) -> ApprovalRow | None:
        # Only `approved`, only this run and step. The partial unique index
        # bounds *pending* rows to one per step; approved rows accumulate
        # across replans, so the latest decision wins deterministically
        # (`decided_at`, then `requested_at`, then the id as a total order).
        # Fresh: the gate must see the row as it is now, not as this
        # session last loaded it.
        stmt = (
            select(ApprovalRow)
            .where(
                ApprovalRow.run_id == run_id,
                ApprovalRow.step_id == step_id,
                ApprovalRow.status == ApprovalStatus.APPROVED,
            )
            .order_by(
                ApprovalRow.decided_at.desc().nulls_last(),
                ApprovalRow.requested_at.desc(),
                ApprovalRow.id.desc(),
            )
            .limit(1)
            .execution_options(populate_existing=True)
        )
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()

    async def create_request(
        self,
        *,
        run_id: uuid.UUID,
        step_id: str,
        tool: ToolName,
        risk: RiskLevel,
        title: str,
        summary: str,
        payload_preview: dict[str, Any],
        args_hash: str,
        requested_at: datetime,
        expires_at: datetime,
        id: uuid.UUID | None = None,
    ) -> ApprovalRow:
        row = ApprovalRow(
            run_id=run_id,
            step_id=step_id,
            tool=tool,
            risk=risk,
            title=title,
            summary=summary,
            payload_preview=payload_preview,
            args_hash=args_hash,
            requested_at=requested_at,
            expires_at=expires_at,
        )
        if id is not None:
            row.id = id
        self._session.add(row)
        await self._session.flush()
        return row

    #: Statuses a request can find "current" for its step: an open request or
    #: a decision that has not been chained forward. Expired, cancelled and
    #: superseded rows are history and never reused.
    _CURRENT_STATUSES = (ApprovalStatus.PENDING, ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)
    _DECIDED_STATUSES = (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)
    #: Bound on the read → conditional-write loop below. A retry means a
    #: *decision* committed between our read and our write (decisions do not
    #: take the request lock), which cannot repeat indefinitely: a decided row
    #: is never pending again.
    _UPSERT_ATTEMPTS = 4

    async def upsert_request(
        self,
        *,
        run_id: uuid.UUID,
        step_id: str,
        tool: ToolName,
        risk: RiskLevel,
        title: str,
        summary: str,
        payload_preview: dict[str, Any],
        args_hash: str,
        requested_at: datetime,
        expires_at: datetime,
    ) -> ApprovalUpsert:
        # Serialise every request for this (run, step) with a transaction-
        # scoped advisory lock — the same mechanism `trace_events` uses
        # (`app.persistence.trace_events`), for the same reason: before the
        # first request there is no row to lock, and the partial unique index
        # alone cannot stop a second `pending` row from being inserted after
        # the first one was *approved* (it is no longer in the index). Under
        # the lock, the read below sees every committed request, so a decided
        # row is found rather than re-requested. The key is distinct from the
        # per-run trace key, and the lock is taken before any approval row
        # lock — the order `ApprovalService` also follows (row, then trace),
        # so there is no ordering cycle and no deadlock.
        lock_key = sa.func.hashtextextended(
            sa.cast(f"approval-request:{run_id}:{step_id}", sa.Text), 0
        )
        await self._session.execute(sa.select(sa.func.pg_advisory_xact_lock(lock_key)))

        for _ in range(self._UPSERT_ATTEMPTS):
            current = await self._current_rows(run_id, step_id)

            # 1. A decision for these exact arguments is final: hand it back.
            #    An approval is a grant bounded by its TTL (the gate refuses it
            #    past `expires_at`), so an elapsed one is asked afresh; a
            #    rejection is final regardless.
            decided = [
                r
                for r in current
                if r.status in self._DECIDED_STATUSES
                and r.args_hash == args_hash
                and (r.status is ApprovalStatus.REJECTED or requested_at < r.expires_at)
            ]
            if decided:
                return ApprovalUpsert(row=decided[0], created=False)

            # 2. The open request for these exact arguments: the re-executed
            #    node finding its own row.
            pending = next((r for r in current if r.status is ApprovalStatus.PENDING), None)
            if pending is not None and pending.args_hash == args_hash:
                return ApprovalUpsert(row=pending, created=False)

            # 3. Nothing current matches: request afresh, chaining forward
            #    whatever the changed arguments invalidated. The open request
            #    must be closed *before* the insert (the partial index admits
            #    one `pending` per step) and conditionally — if the human
            #    decided it meanwhile, the row is no longer ours to close and
            #    the loop re-reads it as a decision.
            stale = [r for r in current if r.args_hash != args_hash]
            if pending is not None:
                closed = await self._session.execute(
                    update(ApprovalRow)
                    .where(
                        ApprovalRow.id == pending.id,
                        ApprovalRow.status == ApprovalStatus.PENDING,
                    )
                    .values(status=ApprovalStatus.SUPERSEDED)
                    .returning(ApprovalRow.id)
                    .execution_options(synchronize_session=False)
                )
                if closed.scalar_one_or_none() is None:
                    continue

            inserted = await self._insert_pending(
                run_id=run_id,
                step_id=step_id,
                tool=tool,
                risk=risk,
                title=title,
                summary=summary,
                payload_preview=payload_preview,
                args_hash=args_hash,
                requested_at=requested_at,
                expires_at=expires_at,
            )
            if inserted is None:
                # `ON CONFLICT DO NOTHING` on the partial index: a writer that
                # bypassed the request lock inserted a `pending` row for this
                # step. Re-read and treat it like any other current row.
                continue

            superseded: list[ApprovalRow] = []
            if stale:
                # `superseded_by` is a foreign key to the new row, so it can
                # only be set once that row exists; the status flip for the
                # open request above and this pointer commit as one unit.
                res = await self._session.execute(
                    update(ApprovalRow)
                    .where(
                        ApprovalRow.id.in_([r.id for r in stale]),
                        ApprovalRow.superseded_by.is_(None),
                        ApprovalRow.status.in_(
                            (*self._DECIDED_STATUSES, ApprovalStatus.SUPERSEDED)
                        ),
                    )
                    .values(status=ApprovalStatus.SUPERSEDED, superseded_by=inserted.id)
                    .returning(ApprovalRow.id)
                    .execution_options(synchronize_session=False)
                )
                touched = set(res.scalars().all())
                for r in stale:
                    if r.id in touched:
                        await self._session.refresh(r)
                        superseded.append(r)
            await self._session.flush()
            return ApprovalUpsert(row=inserted, created=True, superseded=tuple(superseded))

        raise RuntimeError(
            f"approval request for run {run_id} step {step_id!r} did not settle "
            f"in {self._UPSERT_ATTEMPTS} attempts"
        )

    async def _current_rows(self, run_id: uuid.UUID, step_id: str) -> list[ApprovalRow]:
        """This step's open request and un-chained decisions, as they are
        now (`populate_existing`), latest decision first."""
        stmt = (
            select(ApprovalRow)
            .where(
                ApprovalRow.run_id == run_id,
                ApprovalRow.step_id == step_id,
                ApprovalRow.superseded_by.is_(None),
                ApprovalRow.status.in_(self._CURRENT_STATUSES),
            )
            .order_by(
                ApprovalRow.decided_at.desc().nulls_last(),
                ApprovalRow.requested_at.desc(),
                ApprovalRow.id.desc(),
            )
            .execution_options(populate_existing=True)
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def _insert_pending(
        self,
        *,
        run_id: uuid.UUID,
        step_id: str,
        tool: ToolName,
        risk: RiskLevel,
        title: str,
        summary: str,
        payload_preview: dict[str, Any],
        args_hash: str,
        requested_at: datetime,
        expires_at: datetime,
    ) -> ApprovalRow | None:
        """`INSERT … ON CONFLICT DO NOTHING RETURNING *` against the partial
        unique index: the row, or `None` when another `pending` row for the
        step already exists."""
        stmt = (
            pg_insert(ApprovalRow)
            .values(
                run_id=run_id,
                step_id=step_id,
                tool=tool,
                risk=risk,
                title=title,
                summary=summary,
                payload_preview=payload_preview,
                args_hash=args_hash,
                status=ApprovalStatus.PENDING,
                requested_at=requested_at,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(
                index_elements=[ApprovalRow.run_id, ApprovalRow.step_id],
                index_where=sa.text("status = 'pending'"),
            )
            .returning(ApprovalRow)
        )
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()

    async def decide(
        self,
        approval_id: uuid.UUID,
        *,
        status: ApprovalStatus,
        decided_by: str | None = None,
        decision_reason: str | None = None,
        decided_at: datetime,
    ) -> ApprovalRow | None:
        # Atomic conditional update: only transitions if current status is pending
        stmt = (
            update(ApprovalRow)
            .where(ApprovalRow.id == approval_id, ApprovalRow.status == ApprovalStatus.PENDING)
            .values(
                status=status,
                decided_by=decided_by,
                decision_reason=decision_reason,
                decided_at=decided_at,
            )
            .execution_options(synchronize_session=False)
            .returning(ApprovalRow)
        )
        res = await self._session.execute(stmt)
        row = res.scalar_one_or_none()
        if row is not None:
            await self._session.refresh(row)
        await self._session.flush()
        return row

    async def supersede(
        self, approval_id: uuid.UUID, superseded_by: uuid.UUID
    ) -> ApprovalRow | None:
        stmt = (
            update(ApprovalRow)
            .where(ApprovalRow.id == approval_id, ApprovalRow.status == ApprovalStatus.PENDING)
            .values(status=ApprovalStatus.SUPERSEDED, superseded_by=superseded_by)
            .returning(ApprovalRow)
        )
        res = await self._session.execute(stmt)
        await self._session.flush()
        return res.scalar_one_or_none()

    async def list_pending(self, *, limit: int = 50) -> list[ApprovalRow]:
        stmt = (
            select(ApprovalRow)
            .where(ApprovalRow.status == ApprovalStatus.PENDING)
            .order_by(ApprovalRow.requested_at.desc())
            .limit(limit)
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def list_expired(self, *, now: datetime, limit: int = 50) -> list[ApprovalRow]:
        stmt = (
            select(ApprovalRow)
            .where(ApprovalRow.status == ApprovalStatus.PENDING, ApprovalRow.expires_at < now)
            .order_by(ApprovalRow.expires_at.asc())
            .limit(limit)
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def list_by_run(self, run_id: uuid.UUID) -> list[ApprovalRow]:
        stmt = (
            select(ApprovalRow)
            .where(ApprovalRow.run_id == run_id)
            .order_by(ApprovalRow.requested_at.asc())
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())


_SEVERITY_RANK: Final[dict[TraceEventSeverity, int]] = {
    TraceEventSeverity.DEBUG: 1,
    TraceEventSeverity.INFO: 2,
    TraceEventSeverity.WARNING: 3,
    TraceEventSeverity.ERROR: 4,
}


class SqlTraceEventRepository:
    """Async SQLAlchemy implementation of TraceEventRepository (§12.7, DB-002, DB-005)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        run_id: uuid.UUID,
        kind: TraceEventKind,
        severity: TraceEventSeverity = TraceEventSeverity.INFO,
        node: str | None = None,
        tool: ToolName | None = None,
        step_id: str | None = None,
        attempt: int | None = None,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        retry_count: int | None = None,
        error: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> TraceEvent:
        # Atomic per-run sequence allocation protected by transaction advisory lock
        lock_key = sa.func.hashtextextended(sa.cast(str(run_id), sa.Text), 0)
        await self._session.execute(sa.select(sa.func.pg_advisory_xact_lock(lock_key)))

        seq_stmt = sa.select(sa.func.coalesce(sa.func.max(TraceEvent.seq), 0) + 1).where(
            TraceEvent.run_id == run_id
        )
        res = await self._session.execute(seq_stmt)
        next_seq = res.scalar_one()

        event = TraceEvent(
            run_id=run_id,
            seq=next_seq,
            kind=kind,
            severity=severity,
            node=node,
            tool=tool,
            step_id=step_id,
            attempt=attempt,
            input=input,
            output=output,
            status=status,
            duration_ms=duration_ms,
            retry_count=retry_count,
            error=error,
            payload=payload if payload is not None else {},
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def list_by_run(
        self,
        run_id: uuid.UUID,
        *,
        after_seq: int = 0,
        since_seq: int | None = None,
        limit: int = 100,
        kinds: list[TraceEventKind] | None = None,
        severity_min: TraceEventSeverity | None = None,
    ) -> list[TraceEvent]:
        start_after = (since_seq - 1) if since_seq is not None else after_seq
        stmt = select(TraceEvent).where(TraceEvent.run_id == run_id, TraceEvent.seq > start_after)
        if kinds:
            stmt = stmt.where(TraceEvent.kind.in_(kinds))
        if severity_min is not None:
            min_rank = _SEVERITY_RANK.get(severity_min, 1)
            allowed_severities = [s for s, rank in _SEVERITY_RANK.items() if rank >= min_rank]
            stmt = stmt.where(TraceEvent.severity.in_(allowed_severities))
        stmt = stmt.order_by(TraceEvent.seq.asc()).limit(limit)
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def get_latest(self, run_id: uuid.UUID) -> TraceEvent | None:
        stmt = (
            select(TraceEvent)
            .where(TraceEvent.run_id == run_id)
            .order_by(TraceEvent.seq.desc())
            .limit(1)
        )
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()


class SqlEvaluationRepository:
    """Async SQLAlchemy implementation of EvaluationRepository (§12.8, DB-003, DB-005)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_run(self, evaluation_run_id: uuid.UUID) -> EvaluationRun | None:
        return await self._session.get(EvaluationRun, evaluation_run_id)

    async def create_run(
        self,
        *,
        suite: str,
        planner_kind: PlannerKind = PlannerKind.RULES,
        git_sha: str | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        seed: int | None = None,
        id: uuid.UUID | None = None,
    ) -> EvaluationRun:
        run = EvaluationRun(
            suite=suite,
            planner_kind=planner_kind,
            git_sha=git_sha,
            model_id=model_id,
            prompt_version=prompt_version,
            seed=seed,
        )
        if id is not None:
            run.id = id
        self._session.add(run)
        await self._session.flush()
        return run

    async def complete_run(
        self,
        evaluation_run_id: uuid.UUID,
        *,
        status: EvaluationRunStatus,
        finished_at: datetime,
        case_count: int,
        passed: int,
        failed: int,
        metrics: dict[str, Any],
    ) -> EvaluationRun | None:
        run = await self.get_run(evaluation_run_id)
        if run is None:
            return None
        run.status = status
        run.finished_at = finished_at
        run.case_count = case_count
        run.passed = passed
        run.failed = failed
        run.metrics = metrics
        await self._session.flush()
        return run

    async def record_result(
        self,
        *,
        evaluation_run_id: uuid.UUID,
        case_id: str,
        run_id: uuid.UUID,
        passed: bool,
        assertions: list[Any],
        duration_ms: int | None = None,
        retry_count: int = 0,
        tool_calls_count: int = 0,
        approval_outcome: str | None = None,
        failure_reason: str | None = None,
        id: uuid.UUID | None = None,
    ) -> EvaluationResult:
        result = EvaluationResult(
            evaluation_run_id=evaluation_run_id,
            case_id=case_id,
            run_id=run_id,
            passed=passed,
            assertions=assertions,
            duration_ms=duration_ms,
            retry_count=retry_count,
            tool_calls_count=tool_calls_count,
            approval_outcome=approval_outcome,
            failure_reason=failure_reason,
        )
        if id is not None:
            result.id = id
        self._session.add(result)
        await self._session.flush()
        return result

    async def list_results(self, evaluation_run_id: uuid.UUID) -> list[EvaluationResult]:
        stmt = (
            select(EvaluationResult)
            .where(EvaluationResult.evaluation_run_id == evaluation_run_id)
            .order_by(EvaluationResult.case_id.asc())
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def list_runs(self, *, suite: str | None = None, limit: int = 50) -> list[EvaluationRun]:
        stmt = select(EvaluationRun)
        if suite is not None:
            stmt = stmt.where(EvaluationRun.suite == suite)
        stmt = stmt.order_by(EvaluationRun.started_at.desc()).limit(limit)
        res = await self._session.execute(stmt)
        return list(res.scalars().all())


class SqlCompanyRepository:
    """Async SQLAlchemy implementation of CompanyRepository (§12.9)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, company_id: str) -> Company | None:
        return await self._session.get(Company, company_id)

    async def get_by_domain(self, domain: str) -> Company | None:
        stmt = select(Company).where(Company.domain == domain)
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()

    async def list_all(self, *, industry: str | None = None, limit: int = 100) -> list[Company]:
        stmt = select(Company)
        if industry is not None:
            stmt = stmt.where(Company.industry == industry)
        stmt = stmt.order_by(Company.company_id.asc()).limit(limit)
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def create(self, company: Company) -> Company:
        self._session.add(company)
        await self._session.flush()
        return company


class SqlLeadRepository:
    """Async SQLAlchemy implementation of LeadRepository (§12.9)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, lead_id: str) -> Lead | None:
        return await self._session.get(Lead, lead_id)

    async def get_by_email(self, email: str) -> Lead | None:
        stmt = select(Lead).where(Lead.email == email)
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()

    async def list_by_company(self, company_id: str) -> list[Lead]:
        stmt = select(Lead).where(Lead.company_id == company_id).order_by(Lead.lead_id.asc())
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def list_by_status(self, status: LeadStatus, *, limit: int = 100) -> list[Lead]:
        stmt = select(Lead).where(Lead.status == status).order_by(Lead.lead_id.asc()).limit(limit)
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def update_status(
        self,
        lead_id: str,
        status: LeadStatus,
        *,
        last_contacted_at: datetime | None = None,
    ) -> Lead | None:
        lead = await self.get(lead_id)
        if lead is None:
            return None
        lead.status = status
        if last_contacted_at is not None:
            lead.last_contacted_at = last_contacted_at
        await self._session.flush()
        return lead

    async def create(self, lead: Lead) -> Lead:
        self._session.add(lead)
        await self._session.flush()
        return lead

    async def search(
        self,
        *,
        industry: str | None = None,
        location: str | None = None,
        min_employees: int | None = None,
        max_employees: int | None = None,
        status: LeadStatus | None = None,
        query: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[Lead], int]:
        stmt = (
            select(Lead)
            .join(Company, Lead.company_id == Company.company_id)
            .options(joinedload(Lead.company))
        )
        count_stmt = (
            select(sa.func.count(Lead.lead_id))
            .select_from(Lead)
            .join(Company, Lead.company_id == Company.company_id)
        )

        filters = []
        if status is not None:
            filters.append(Lead.status == status)
        if industry is not None:
            filters.append(Company.industry == industry)
        if location is not None:
            filters.append(Company.hq_location.ilike(f"%{location}%"))
        if min_employees is not None:
            filters.append(Company.employee_count >= min_employees)
        if max_employees is not None:
            filters.append(Company.employee_count <= max_employees)
        if query is not None and query.strip():
            q = f"%{query.strip()}%"
            filters.append(sa.or_(Lead.full_name.ilike(q), Company.name.ilike(q)))

        if filters:
            cond = sa.and_(*filters)
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)

        stmt = stmt.order_by(Lead.created_at.asc(), Lead.lead_id.asc()).limit(limit).offset(offset)

        total_res = await self._session.execute(count_stmt)
        total_count = int(total_res.scalar_one() or 0)

        items_res = await self._session.execute(stmt)
        leads = list(items_res.scalars().unique().all())
        return leads, total_count


class SqlCustomerRepository:
    """Async SQLAlchemy implementation of CustomerRepository (§12.9, §8.5)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, customer_id: str) -> Customer | None:
        return await self._session.get(Customer, customer_id)

    async def get_by_email(self, email: str) -> Customer | None:
        stmt = select(Customer).where(Customer.email == email)
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()

    async def create(self, customer: Customer) -> Customer:
        self._session.add(customer)
        await self._session.flush()
        return customer

    async def update_optimistic(
        self,
        customer_id: str,
        *,
        expected_version: int,
        account_name: str | None = None,
        primary_contact: str | None = None,
        phone: str | None = None,
        status: CustomerStatus | None = None,
        plan: str | None = None,
        mrr: Decimal | None = None,
        owner: str | None = None,
        notes: str | None = None,
    ) -> Customer | None:
        values: dict[str, Any] = {"version": expected_version + 1}
        if account_name is not None:
            values["account_name"] = account_name
        if primary_contact is not None:
            values["primary_contact"] = primary_contact
        if phone is not None:
            values["phone"] = phone
        if status is not None:
            values["status"] = status
        if plan is not None:
            values["plan"] = plan
        if mrr is not None:
            values["mrr"] = mrr
        if owner is not None:
            values["owner"] = owner
        if notes is not None:
            values["notes"] = notes

        stmt = (
            update(Customer)
            .where(Customer.customer_id == customer_id, Customer.version == expected_version)
            .values(**values)
            .returning(Customer)
        )
        res = await self._session.execute(stmt)
        await self._session.flush()
        return res.scalar_one_or_none()


class SqlOutreachDraftRepository:
    """Async SQLAlchemy implementation of OutreachDraftRepository (§12.9)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, draft_id: str) -> OutreachDraft | None:
        return await self._session.get(OutreachDraft, draft_id)

    async def list_by_lead(self, lead_id: str) -> list[OutreachDraft]:
        stmt = (
            select(OutreachDraft)
            .where(OutreachDraft.lead_id == lead_id)
            .order_by(OutreachDraft.created_at.desc())
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def create(self, draft: OutreachDraft) -> OutreachDraft:
        self._session.add(draft)
        await self._session.flush()
        return draft

    async def update_status(
        self, draft_id: str, status: OutreachDraftStatus
    ) -> OutreachDraft | None:
        draft = await self.get(draft_id)
        if draft is None:
            return None
        draft.status = status
        await self._session.flush()
        return draft


class SqlEmailOutboxRepository:
    """Async SQLAlchemy implementation of EmailOutboxRepository (§12.9)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, outbox_id: str) -> EmailOutbox | None:
        return await self._session.get(EmailOutbox, outbox_id)

    async def get_by_message_id(self, message_id: str) -> EmailOutbox | None:
        stmt = select(EmailOutbox).where(EmailOutbox.message_id == message_id)
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_idempotency_key(self, idempotency_key: str) -> EmailOutbox | None:
        stmt = select(EmailOutbox).where(EmailOutbox.idempotency_key == idempotency_key)
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()

    async def count_by_idempotency_key(self, idempotency_key: str) -> int:
        stmt = (
            select(sa.func.count())
            .select_from(EmailOutbox)
            .where(EmailOutbox.idempotency_key == idempotency_key)
        )
        res = await self._session.execute(stmt)
        return int(res.scalar_one() or 0)

    async def list_by_run(self, run_id: str) -> list[EmailOutbox]:
        stmt = (
            select(EmailOutbox)
            .where(EmailOutbox.run_id == run_id)
            .order_by(EmailOutbox.created_at.asc())
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def create(self, entry: EmailOutbox) -> EmailOutbox:
        self._session.add(entry)
        await self._session.flush()
        return entry


class SqlUnitOfWork:
    """Async SQLAlchemy Unit of Work implementation with explicit transaction boundaries.

    Ensures:
    - Session is acquired only on enter.
    - Explicit `await uow.commit()` required to persist changes.
    - Exiting without commit or on error automatically rolls back.
    - Session is always closed and released back to pool in finally.
    - Callers cannot reuse a closed session.
    """

    agent_runs: SqlAgentRunRepository = None  # type: ignore[assignment]
    execution_steps: SqlExecutionStepRepository = None  # type: ignore[assignment]
    tool_calls: SqlToolCallRepository = None  # type: ignore[assignment]
    approvals: SqlApprovalRepository = None  # type: ignore[assignment]
    trace_events: SqlTraceEventRepository = None  # type: ignore[assignment]
    evaluations: SqlEvaluationRepository = None  # type: ignore[assignment]
    companies: SqlCompanyRepository = None  # type: ignore[assignment]
    leads: SqlLeadRepository = None  # type: ignore[assignment]
    customers: SqlCustomerRepository = None  # type: ignore[assignment]
    outreach_drafts: SqlOutreachDraftRepository = None  # type: ignore[assignment]
    email_outbox: SqlEmailOutboxRepository = None  # type: ignore[assignment]

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._committed = False

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        self._committed = False
        self.agent_runs = SqlAgentRunRepository(self._session)
        self.execution_steps = SqlExecutionStepRepository(self._session)
        self.tool_calls = SqlToolCallRepository(self._session)
        self.approvals = SqlApprovalRepository(self._session)
        self.trace_events = SqlTraceEventRepository(self._session)
        self.evaluations = SqlEvaluationRepository(self._session)
        self.companies = SqlCompanyRepository(self._session)
        self.leads = SqlLeadRepository(self._session)
        self.customers = SqlCustomerRepository(self._session)
        self.outreach_drafts = SqlOutreachDraftRepository(self._session)
        self.email_outbox = SqlEmailOutboxRepository(self._session)
        return self

    async def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("UnitOfWork transaction is not active")
        await self._session.commit()
        self._committed = True

    async def rollback(self) -> None:
        if self._session is not None and self._session.is_active:
            await self._session.rollback()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None:
                await self.rollback()
            elif not self._committed:
                # Explicit commit required: exiting context without commit rolls back.
                await self.rollback()
        finally:
            if self._session is not None:
                await self._session.close()
                self._session = None
