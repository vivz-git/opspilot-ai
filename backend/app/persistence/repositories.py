"""Asynchronous repository implementations (§12, DB-005).

All SQLAlchemy queries (`select()`, `update()`, `insert()`), advisory locks,
and transaction-specific operations are encapsulated here. No ORM session or query
construction escapes this module.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

import sqlalchemy as sa
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

    async def heartbeat_lease(self, run_id: uuid.UUID, lease_expires_at: datetime) -> bool:
        stmt = (
            update(AgentRun).where(AgentRun.id == run_id).values(lease_expires_at=lease_expires_at)
        )
        res = await self._session.execute(stmt)
        await self._session.flush()
        rowcount = getattr(res, "rowcount", 0)
        return bool(rowcount > 0)

    async def list_runs(
        self,
        *,
        status: RunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentRun]:
        stmt = select(AgentRun)
        if status is not None:
            stmt = stmt.where(AgentRun.status == status)
        stmt = stmt.order_by(AgentRun.created_at.desc()).offset(offset).limit(limit)
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def list_orphaned_runs(self, *, now: datetime, limit: int = 50) -> list[AgentRun]:
        stmt = (
            select(AgentRun)
            .where(
                AgentRun.status.in_([RunStatus.RUNNING, RunStatus.QUEUED]),
                AgentRun.lease_expires_at < now,
            )
            .order_by(AgentRun.lease_expires_at.asc())
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

    async def get(self, approval_id: uuid.UUID) -> ApprovalRow | None:
        return await self._session.get(ApprovalRow, approval_id)

    async def get_pending(self, run_id: uuid.UUID, step_id: str) -> ApprovalRow | None:
        stmt = select(ApprovalRow).where(
            ApprovalRow.run_id == run_id,
            ApprovalRow.step_id == step_id,
            ApprovalRow.status == ApprovalStatus.PENDING,
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
            .returning(ApprovalRow)
        )
        res = await self._session.execute(stmt)
        await self._session.flush()
        return res.scalar_one_or_none()

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
        self, run_id: uuid.UUID, *, after_seq: int = 0, limit: int = 100
    ) -> list[TraceEvent]:
        stmt = (
            select(TraceEvent)
            .where(TraceEvent.run_id == run_id, TraceEvent.seq > after_seq)
            .order_by(TraceEvent.seq.asc())
            .limit(limit)
        )
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
