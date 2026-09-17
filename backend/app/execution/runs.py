"""Run application service (§2.1, §13.2, §13.3).

Coordinates durable run creation, idempotency validation, trace recording,
and run detail retrieval without bypassing DB-007 persistence or worker leases.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import structlog
from fastapi import Request
from sse_starlette.event import ServerSentEvent

from app.agent.state import TERMINAL_RUN_STATUSES, ApprovalStatus, PlannerKind, RunStatus
from app.api.schemas import TraceEventResource, decode_cursor, encode_cursor
from app.config import Settings
from app.errors import (
    IdempotencyConflictError,
    InputValidationError,
    NotFoundError,
    RunActiveError,
    RunNotCancellableError,
    RunNotStartableError,
)
from app.persistence.models import (
    AgentRun,
    ApprovalRow,
    ExecutionStep,
    TraceEvent,
    TraceEventKind,
    TraceEventSeverity,
)
from app.persistence.protocols import UnitOfWorkFactory
from app.runtime import (
    CancellationSource,
    Clock,
    IdGenerator,
    InMemoryCancellationSource,
    SystemClock,
    UuidIdGenerator,
)

__all__ = [
    "RunCreateResult",
    "RunDetails",
    "RunService",
    "RunTraceResult",
]

TERMINAL_TRACE_KINDS: Final[frozenset[TraceEventKind]] = frozenset(
    {
        TraceEventKind.RUN_COMPLETED,
        TraceEventKind.RUN_FAILED,
        TraceEventKind.RUN_REJECTED,
        TraceEventKind.RUN_EXPIRED,
        TraceEventKind.RUN_CANCELLED,
    }
)


@dataclass(frozen=True)
class RunTraceResult:
    run_id: uuid.UUID
    events: list[TraceEvent]
    next_seq: int | None
    complete: bool


_log = structlog.get_logger("opspilot.execution.runs")


@dataclass(frozen=True)
class RunCreateResult:
    run: AgentRun
    is_duplicate: bool


@dataclass(frozen=True)
class RunDetails:
    run: AgentRun
    steps: list[ExecutionStep]
    pending_approval: ApprovalRow | None


class RunService:
    """Control-plane service for managing agent run lifecycle and queries."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        settings: Settings,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        cancellation_source: CancellationSource | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._settings = settings
        self._clock = clock or SystemClock()
        self._ids = ids or UuidIdGenerator()
        self._cancellation_source = cancellation_source or InMemoryCancellationSource()

    async def create_run(
        self,
        user_request: str,
        *,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        auto_start: bool = False,
        planner_kind: PlannerKind | None = None,
        actor_id: str | None = None,
    ) -> RunCreateResult:
        """Create a new durable run or return the existing run on idempotent replay."""
        clean_request = user_request.strip()
        if not clean_request:
            raise InputValidationError("user_request cannot be empty")
        if len(clean_request) > 4000:
            raise InputValidationError("user_request cannot exceed 4000 characters")

        now = self._clock.now()
        deadline_at = now + timedelta(seconds=self._settings.run_deadline_seconds)
        effective_planner = planner_kind or self._settings.effective_planner

        async with self._uow_factory() as uow:
            # 1. Check idempotency if key provided
            if idempotency_key is not None:
                existing = await uow.agent_runs.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    # Validate body matches
                    if existing.user_request.strip() != clean_request:
                        raise IdempotencyConflictError(
                            f"Idempotency-Key '{idempotency_key}' reused with a different body",
                            detail={"idempotency_key": idempotency_key},
                        )
                    await uow.commit()
                    return RunCreateResult(run=existing, is_duplicate=True)

            # 2. Persist new durable run
            run_uuid: uuid.UUID | None = None
            if self._ids is not None:
                raw_id = self._ids.new_id()
                try:
                    run_uuid = uuid.UUID(hex=raw_id) if len(raw_id) == 32 else uuid.UUID(raw_id)
                except ValueError:
                    try:
                        run_uuid = uuid.UUID(int=int(raw_id))
                    except ValueError:
                        run_uuid = None

            initial_status = RunStatus.QUEUED if auto_start else RunStatus.CREATED
            run = await uow.agent_runs.create(
                id=run_uuid,
                user_request=clean_request,
                planner_kind=effective_planner,
                deadline_at=deadline_at,
                status=initial_status,
                idempotency_key=idempotency_key,
                actor_id=actor_id,
                metadata=metadata or {},
            )

            # 3. Canonical trace event
            planner_str = (
                effective_planner.value
                if hasattr(effective_planner, "value")
                else str(effective_planner)
            )
            await uow.trace_events.append(
                run_id=run.id,
                kind=TraceEventKind.RUN_CREATED,
                status=initial_status.value,
                payload={
                    "user_request": clean_request,
                    "auto_start": auto_start,
                    "planner_kind": planner_str,
                    "idempotency_key": idempotency_key,
                },
            )

            await uow.commit()

        _log.info(
            "run_created",
            run_id=str(run.id),
            status=initial_status.value,
            idempotency_key=idempotency_key,
        )
        return RunCreateResult(run=run, is_duplicate=False)

    async def get_run(self, run_id: uuid.UUID) -> AgentRun | None:
        """Retrieve the raw AgentRun persistence model."""
        async with self._uow_factory() as uow:
            run = await uow.agent_runs.get(run_id)
            await uow.commit()
            return run

    async def get_run_details(self, run_id: uuid.UUID) -> RunDetails | None:
        """Retrieve full details needed to assemble a public RunResource."""
        async with self._uow_factory() as uow:
            run = await uow.agent_runs.get(run_id)
            if run is None:
                await uow.commit()
                return None

            steps = await uow.execution_steps.list_by_run(run_id)
            pending_approval: ApprovalRow | None = None
            if run.status == RunStatus.AWAITING_APPROVAL:
                approvals = await uow.approvals.list_by_run(run_id)
                pending_approval = next(
                    (a for a in approvals if a.status == ApprovalStatus.PENDING),
                    None,
                )

            await uow.commit()
            return RunDetails(run=run, steps=steps, pending_approval=pending_approval)

    async def list_runs(
        self,
        *,
        statuses: list[RunStatus] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        parent_run_id: uuid.UUID | None = None,
        query: str | None = None,
        cursor_str: str | None = None,
        limit: int = 25,
    ) -> tuple[list[AgentRun], str | None, int]:
        """List runs with optional filtering and keyset cursor pagination (§13.2).

        Returns:
            (items, next_cursor, total_estimate)
        """
        cursor_created_at: datetime | None = None
        cursor_id: uuid.UUID | None = None
        if cursor_str:
            cursor_created_at, cursor_id = decode_cursor(cursor_str)

        async with self._uow_factory() as uow:
            result = await uow.agent_runs.list_runs(
                statuses=statuses,
                since=since,
                until=until,
                parent_run_id=parent_run_id,
                query=query,
                cursor_created_at=cursor_created_at,
                cursor_id=cursor_id,
                limit=limit,
            )
            await uow.commit()

        next_cursor: str | None = None
        if result.has_more and len(result.items) > 0:
            last_item = result.items[-1]
            next_cursor = encode_cursor(last_item.created_at, last_item.id)

        return result.items, next_cursor, result.total_estimate

    async def start_run(self, run_id: uuid.UUID) -> AgentRun:
        """Transition a run from created to queued and schedule execution (§13.2)."""
        async with self._uow_factory() as uow:
            run = await uow.agent_runs.get(run_id)
            if run is None:
                raise NotFoundError(f"Run {run_id} not found")
            if run.status != RunStatus.CREATED:
                raise RunNotStartableError(
                    f"Run {run_id} is in status '{run.status.value}', expected 'created'",
                    detail={"run_id": str(run_id), "status": run.status.value},
                )

            now = self._clock.now()
            updated = await uow.agent_runs.transition_status(
                run_id,
                expected=[RunStatus.CREATED],
                status=RunStatus.QUEUED,
                started_at=now,
            )
            if updated is None:
                current = await uow.agent_runs.get(run_id)
                curr_status = current.status.value if current else "unknown"
                raise RunNotStartableError(
                    f"Run {run_id} is in status '{curr_status}', expected 'created'",
                    detail={"run_id": str(run_id), "status": curr_status},
                )

            await uow.trace_events.append(
                run_id=run_id,
                kind=TraceEventKind.RUN_STARTED,
                status=RunStatus.QUEUED.value,
                payload={"started_at": now.isoformat()},
            )
            await uow.commit()
            _log.info("run_started", run_id=str(run_id))
            return updated

    async def cancel_run(self, run_id: uuid.UUID, *, reason: str | None = None) -> AgentRun:
        """Cooperatively cancel an active or non-terminal run (§13.2)."""
        async with self._uow_factory() as uow:
            run = await uow.agent_runs.get(run_id)
            if run is None:
                raise NotFoundError(f"Run {run_id} not found")

            if run.status == RunStatus.CANCELLED:
                # Idempotent cancel on already cancelled run
                await uow.commit()
                return run

            if run.status in TERMINAL_RUN_STATUSES:
                raise RunNotCancellableError(
                    f"Run {run_id} is already terminal with status '{run.status.value}'",
                    detail={"run_id": str(run_id), "status": run.status.value},
                )

            now = self._clock.now()
            status_reason = reason or "cancelled"

            # 1. Flag cooperative cancellation (Invariant P5: tools in flight finish)
            self._cancellation_source.cancel(str(run_id))

            # 2. If awaiting approval, cancel open pending approvals atomically
            if run.status == RunStatus.AWAITING_APPROVAL:
                approvals = await uow.approvals.list_by_run(run_id)
                for app in approvals:
                    if app.status == ApprovalStatus.PENDING:
                        await uow.approvals.decide(
                            app.id,
                            status=ApprovalStatus.CANCELLED,
                            decided_at=now,
                            decided_by="operator_cancellation",
                            decision_reason=status_reason,
                        )

            # 3. Transition run to cancelled in database
            updated = await uow.agent_runs.update_status(
                run_id,
                status=RunStatus.CANCELLED,
                status_reason=status_reason,
                finished_at=now,
            )

            # 4. Canonical trace event
            await uow.trace_events.append(
                run_id=run_id,
                kind=TraceEventKind.RUN_CANCELLED,
                status=RunStatus.CANCELLED.value,
                payload={"reason": status_reason},
            )

            await uow.commit()
            _log.info("run_cancelled", run_id=str(run_id), reason=status_reason)
            return updated or run

    async def retry_run(
        self,
        run_id: uuid.UUID,
        *,
        idempotency_key: str | None = None,
        auto_start: bool = False,
        metadata: dict[str, Any] | None = None,
        actor_id: str | None = None,
    ) -> RunCreateResult:
        """Create a new run linked to parent run_id, copying user_request (§10.6, §13.2)."""
        async with self._uow_factory() as uow:
            original = await uow.agent_runs.get(run_id)
            if original is None:
                raise NotFoundError(f"Run {run_id} not found")

            if original.status not in TERMINAL_RUN_STATUSES:
                raise RunActiveError(
                    f"Run {run_id} is active ({original.status.value}); cannot retry",
                    detail={"run_id": str(run_id), "status": original.status.value},
                )
            await uow.commit()

        # Compose merged metadata
        combined_meta = dict(original.metadata_ or {})
        combined_meta["retried_from"] = str(original.id)
        if metadata:
            combined_meta.update(metadata)

        # Create fresh child run with parent_run_id set
        now = self._clock.now()
        deadline_at = now + timedelta(seconds=self._settings.run_deadline_seconds)
        effective_planner = original.planner_kind or self._settings.effective_planner

        async with self._uow_factory() as uow:
            if idempotency_key is not None:
                existing = await uow.agent_runs.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    if existing.user_request.strip() != original.user_request.strip():
                        raise IdempotencyConflictError(
                            f"Idempotency-Key '{idempotency_key}' reused with a different body",
                            detail={"idempotency_key": idempotency_key},
                        )
                    await uow.commit()
                    return RunCreateResult(run=existing, is_duplicate=True)

            run_uuid: uuid.UUID | None = None
            if self._ids is not None:
                raw_id = self._ids.new_id()
                try:
                    run_uuid = uuid.UUID(hex=raw_id) if len(raw_id) == 32 else uuid.UUID(raw_id)
                except ValueError:
                    try:
                        run_uuid = uuid.UUID(int=int(raw_id))
                    except ValueError:
                        run_uuid = None

            initial_status = RunStatus.QUEUED if auto_start else RunStatus.CREATED
            new_run = await uow.agent_runs.create(
                id=run_uuid,
                parent_run_id=original.id,
                user_request=original.user_request,
                planner_kind=effective_planner,
                deadline_at=deadline_at,
                status=initial_status,
                idempotency_key=idempotency_key,
                actor_id=actor_id or original.actor_id,
                metadata=combined_meta,
            )

            planner_str = (
                effective_planner.value
                if hasattr(effective_planner, "value")
                else str(effective_planner)
            )
            await uow.trace_events.append(
                run_id=new_run.id,
                kind=TraceEventKind.RUN_CREATED,
                status=initial_status.value,
                payload={
                    "user_request": original.user_request,
                    "parent_run_id": str(original.id),
                    "auto_start": auto_start,
                    "planner_kind": planner_str,
                    "idempotency_key": idempotency_key,
                    "retry": True,
                },
            )
            await uow.commit()

        _log.info(
            "run_retried",
            parent_run_id=str(original.id),
            new_run_id=str(new_run.id),
            status=initial_status.value,
        )
        return RunCreateResult(run=new_run, is_duplicate=False)

    async def verify_run_exists(self, run_id: uuid.UUID) -> None:
        """Ensure run exists in database or raise NotFoundError."""
        async with self._uow_factory() as uow:
            run = await uow.agent_runs.get(run_id)
            if run is None:
                raise NotFoundError(f"Run {run_id} not found")
            await uow.commit()

    async def get_run_trace(
        self,
        run_id: uuid.UUID,
        *,
        since_seq: int = 1,
        limit: int = 100,
        kinds: list[TraceEventKind] | None = None,
        severity_min: TraceEventSeverity | None = None,
    ) -> RunTraceResult:
        """Retrieve paginated execution trace events ordered strictly by monotonic seq (§13.4)."""
        async with self._uow_factory() as uow:
            run = await uow.agent_runs.get(run_id)
            if run is None:
                raise NotFoundError(f"Run {run_id} not found")

            is_terminal = run.status in TERMINAL_RUN_STATUSES
            fetch_limit = limit + 1
            raw_events = await uow.trace_events.list_by_run(
                run_id,
                since_seq=since_seq,
                limit=fetch_limit,
                kinds=kinds,
                severity_min=severity_min,
            )

            if len(raw_events) > limit:
                page_events = raw_events[:limit]
                next_seq = page_events[-1].seq + 1
                complete = False
            else:
                page_events = raw_events
                if page_events:
                    if is_terminal:
                        complete = True
                        next_seq = None
                    else:
                        complete = False
                        next_seq = page_events[-1].seq + 1
                else:
                    if is_terminal:
                        complete = True
                        next_seq = None
                    else:
                        complete = False
                        next_seq = since_seq

            await uow.commit()

        return RunTraceResult(
            run_id=run_id,
            events=page_events,
            next_seq=next_seq,
            complete=complete,
        )

    async def stream_run_events(
        self,
        run_id: uuid.UUID,
        *,
        start_seq: int = 1,
        request: Request | None = None,
        poll_interval: float = 0.5,
    ) -> AsyncIterator[ServerSentEvent]:
        """Stream live trace events via SSE with Last-Event-ID replay (§13.4)."""
        cursor_seq = start_seq
        while True:
            if request is not None and await request.is_disconnected():
                break

            resources: list[TraceEventResource] = []
            run_status: RunStatus | None = None
            try:
                async with self._uow_factory() as uow:
                    events = await uow.trace_events.list_by_run(
                        run_id,
                        since_seq=cursor_seq,
                        limit=100,
                    )
                    run = await uow.agent_runs.get(run_id)
                    run_status = run.status if run is not None else None
                    resources = [TraceEventResource.from_row(e) for e in events]
                    await uow.commit()
            except Exception as exc:
                _log.exception("sse_trace_stream_error", run_id=str(run_id), error=str(exc))
                break

            if resources:
                for res in resources:
                    yield ServerSentEvent(
                        id=str(res.seq),
                        event=res.kind.value,
                        data=res.model_dump_json(),
                    )
                    cursor_seq = res.seq + 1
                    if res.kind in TERMINAL_TRACE_KINDS:
                        return

            if not resources:
                if run_status in TERMINAL_RUN_STATUSES:
                    final_resources: list[TraceEventResource] = []
                    try:
                        async with self._uow_factory() as uow:
                            final_events = await uow.trace_events.list_by_run(
                                run_id,
                                since_seq=cursor_seq,
                                limit=100,
                            )
                            final_resources = [TraceEventResource.from_row(e) for e in final_events]
                            await uow.commit()
                    except Exception:
                        break

                    for res in final_resources:
                        yield ServerSentEvent(
                            id=str(res.seq),
                            event=res.kind.value,
                            data=res.model_dump_json(),
                        )
                        cursor_seq = res.seq + 1
                        if res.kind in TERMINAL_TRACE_KINDS:
                            return
                    return

                await asyncio.sleep(poll_interval)
