"""Human-in-the-loop approval service (§9.6, §12.6, ADR-007, ADR-023).

Coordinates the durable approval decision and graph resumption lifecycle:
1. Validates caller-supplied `args_hash` matches persisted `approval.args_hash`.
2. In a single atomic database transaction:
   - Conditionally transitions approval from PENDING to APPROVED/REJECTED.
   - Claims/transitions the agent run from AWAITING_APPROVAL to RUNNING under worker lease.
   - Records approval trace event (APPROVAL_GRANTED or APPROVAL_REJECTED).
   - Commits.
3. Dispatches graph resume with `Command(resume=stored_decision)` only for the winner.
4. Settles the run state on terminal completion or subsequent pause, releasing the lease.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from app.agent.state import ApprovalDecisionKind, ApprovalStatus, RunStatus
from app.errors import (
    ApprovalExpiredError,
    ApprovalNotPendingError,
    ApprovalSupersededError,
    InputValidationError,
    NotFoundError,
    PolicyViolation,
    RunNotResumableError,
)
from app.execution.leases import LeaseConfig, LeaseHeartbeat, UnitOfWorkFactory, new_worker_id
from app.execution.recovery import CheckpointInspection, CheckpointPhase, RunDriver
from app.persistence.models import ApprovalRow, TraceEventKind, TraceEventSeverity
from app.runtime import Clock, IdGenerator, UuidIdGenerator

__all__ = [
    "ApprovalService",
    "DecideApprovalResult",
]

_log = structlog.get_logger("opspilot.approvals")


@dataclass(frozen=True)
class DecideApprovalResult:
    approval: ApprovalRow
    is_winner: bool
    inspection: CheckpointInspection | None = None

    @property
    def status(self) -> ApprovalStatus:
        return self.approval.status


class ApprovalService:
    """Human-in-the-loop approval decision and resumption service (§9.6, ADR-007, ADR-023)."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        driver: RunDriver,
        clock: Clock,
        lease: LeaseConfig,
        owner: str | None = None,
        ids: IdGenerator | None = None,
        before_resume: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._driver = driver
        self._clock = clock
        self._lease = lease
        self._ids = ids or UuidIdGenerator()
        self._owner = owner or new_worker_id(self._ids, label="approval-worker")
        self._before_resume = before_resume

    @property
    def owner(self) -> str:
        return self._owner

    async def get_approval(self, approval_id: uuid.UUID) -> ApprovalRow | None:
        """Retrieve a single approval by ID without exposing persistence internals."""
        async with self._uow_factory() as uow:
            approval = await uow.approvals.get(approval_id)
            await uow.commit()
            return approval

    async def list_pending_queue(self, *, limit: int = 50) -> list[ApprovalRow]:
        """List pending approvals ordered deterministically for the human approval queue."""
        async with self._uow_factory() as uow:
            approvals = await uow.approvals.list_pending(limit=limit)
            await uow.commit()
            return approvals

    async def decide_approval(
        self,
        approval_id: uuid.UUID,
        *,
        decision: ApprovalDecisionKind | str,
        args_hash: str,
        decided_by: str | None = None,
        reason: str | None = None,
        owner: str | None = None,
    ) -> DecideApprovalResult:
        if isinstance(decision, ApprovalDecisionKind):
            decision_kind = decision
        else:
            try:
                decision_kind = ApprovalDecisionKind(str(decision).lower())
            except ValueError as exc:
                raise InputValidationError(f"Invalid approval decision: {decision}") from exc

        target_status = (
            ApprovalStatus.APPROVED
            if decision_kind == ApprovalDecisionKind.APPROVE
            else ApprovalStatus.REJECTED
        )
        worker_owner = owner or self._owner
        now = self._clock.now()

        async with self._uow_factory() as uow:
            # 1. Load approval row
            approval = await uow.approvals.get(approval_id)
            if approval is None:
                raise NotFoundError(f"Approval {approval_id} not found")

            # 2. Validate exact args_hash binding
            if approval.args_hash != args_hash:
                raise PolicyViolation(
                    f"Approval args_hash mismatch: expected {approval.args_hash}, got {args_hash}",
                    detail={"approval_id": str(approval_id), "step_id": approval.step_id},
                )

            # 3. Check expiration
            if approval.status == ApprovalStatus.EXPIRED or (
                approval.status == ApprovalStatus.PENDING and now >= approval.expires_at
            ):
                raise ApprovalExpiredError(
                    f"Approval {approval_id} has expired",
                    detail={
                        "approval_id": str(approval_id),
                        "expires_at": approval.expires_at.isoformat(),
                    },
                )

            # 4. Check superseded
            if approval.status == ApprovalStatus.SUPERSEDED:
                raise ApprovalSupersededError(
                    f"Approval {approval_id} has been superseded",
                    detail={
                        "approval_id": str(approval_id),
                        "superseded_by": str(approval.superseded_by),
                    },
                )

            # 5. Check if already decided sequentially
            if approval.status != ApprovalStatus.PENDING:
                if approval.status != target_status:
                    # Opposite or non-pending status: conflict
                    raise ApprovalNotPendingError(
                        f"Approval {approval_id} is already decided as {approval.status}",
                        detail={
                            "approval_id": str(approval_id),
                            "current_status": approval.status.value,
                        },
                    )

                # Same decision: check run state to determine if resume retry is needed
                run = await uow.agent_runs.get(approval.run_id)
                if run is None or run.status not in (
                    RunStatus.AWAITING_APPROVAL,
                    RunStatus.RUNNING,
                ):
                    # Run is terminal or not in a leasable status: return idempotent duplicate
                    await uow.commit()
                    return DecideApprovalResult(approval=approval, is_winner=False, inspection=None)

                # If another live worker holds the lease, do not steal or resume
                if (
                    run.lease_owner is not None
                    and run.lease_owner != worker_owner
                    and run.lease_expires_at is not None
                    and run.lease_expires_at > now
                ):
                    await uow.commit()
                    return DecideApprovalResult(approval=approval, is_winner=False, inspection=None)

                # If lease is expired or held by us, claim ownership for retry resume
                claimed = await uow.agent_runs.acquire_lease(
                    approval.run_id,
                    owner=worker_owner,
                    now=now,
                    ttl=self._lease.ttl,
                    expected=(RunStatus.AWAITING_APPROVAL, RunStatus.RUNNING),
                    status=RunStatus.RUNNING,
                )
                if claimed is None:
                    # Another worker won the lease race meanwhile
                    await uow.commit()
                    return DecideApprovalResult(approval=approval, is_winner=False, inspection=None)

                await uow.commit()
                decided = approval
            else:
                # 6. Conditionally persist decision (UPDATE ... WHERE status='pending')
                decided_opt = await uow.approvals.decide(
                    approval_id,
                    status=target_status,
                    decided_by=decided_by,
                    decision_reason=reason,
                    decided_at=now,
                )
                if decided_opt is None:
                    # Lost race to concurrent decision: always raise ApprovalNotPendingError (§9.6)
                    existing = await uow.approvals.get(approval_id, fresh=True)
                    status_str = existing.status if existing is not None else "unknown"
                    raise ApprovalNotPendingError(
                        f"Approval {approval_id} decision race lost (current status: {status_str})",
                        detail={"approval_id": str(approval_id), "current_status": status_str},
                    )
                decided = decided_opt

                # 7. In same transaction, claim run and transition AWAITING_APPROVAL -> RUNNING
                claimed = await uow.agent_runs.acquire_lease(
                    decided.run_id,
                    owner=worker_owner,
                    now=now,
                    ttl=self._lease.ttl,
                    expected=(RunStatus.AWAITING_APPROVAL, RunStatus.RUNNING),
                    status=RunStatus.RUNNING,
                )
                if claimed is None:
                    raise RunNotResumableError(
                        f"Could not acquire run ownership on {decided.run_id} during decision",
                        detail={"run_id": str(decided.run_id)},
                    )

                # 8. Record approval trace event
                trace_kind = (
                    TraceEventKind.APPROVAL_GRANTED
                    if target_status == ApprovalStatus.APPROVED
                    else TraceEventKind.APPROVAL_REJECTED
                )
                await uow.trace_events.append(
                    run_id=decided.run_id,
                    kind=trace_kind,
                    status=target_status.value,
                    step_id=decided.step_id,
                    payload={
                        "approval_id": str(decided.id),
                        "decision": decision_kind.value,
                        "decided_by": decided_by,
                        "reason": reason,
                    },
                )

                # 9. COMMIT the transaction
                await uow.commit()

        _log.info(
            "approval_decided_committed",
            approval_id=str(decided.id),
            run_id=str(decided.run_id),
            decision=decision_kind.value,
            owner=worker_owner,
        )

        # Testing hook: simulate crash between commit and resume
        if self._before_resume is not None:
            await self._before_resume()

        # 10. Resume graph using Command(resume=stored_decision)
        stored_decision = decision_kind.value
        heartbeat = LeaseHeartbeat(
            uow_factory=self._uow_factory,
            run_id=decided.run_id,
            owner=worker_owner,
            clock=self._clock,
            config=self._lease,
        )
        heartbeat.start()
        try:
            inspection = await self._driver.resume(decided.run_id, resume_value=stored_decision)
        except Exception as exc:
            await heartbeat.stop()
            if not heartbeat.lost.is_set():
                async with self._uow_factory() as uow:
                    await uow.agent_runs.transition_status(
                        decided.run_id,
                        expected=(RunStatus.RUNNING,),
                        status=RunStatus.FAILED,
                        owner=worker_owner,
                        status_reason="resume_failed",
                        finished_at=self._clock.now(),
                        release_lease=True,
                    )
                    await uow.trace_events.append(
                        run_id=decided.run_id,
                        kind=TraceEventKind.RUN_FAILED,
                        severity=TraceEventSeverity.ERROR,
                        status=RunStatus.FAILED.value,
                        error={
                            "class": "resume_failed",
                            "message": "graph raised while resuming from approval decision",
                            "detail": repr(exc),
                        },
                        payload={
                            "approval_id": str(decided.id),
                            "owner": worker_owner,
                        },
                    )
                    await uow.commit()
            raise
        finally:
            await heartbeat.stop()

        # If lease was lost during resume execution, abort without settling the row
        if heartbeat.lost.is_set():
            _log.warning(
                "resume_abandoned",
                run_id=str(decided.run_id),
                owner=worker_owner,
                reason="lease_lost",
            )
            return DecideApprovalResult(approval=decided, is_winner=True, inspection=inspection)

        # 11. Settle the row
        if inspection.phase is CheckpointPhase.FINISHED and inspection.status is not None:
            async with self._uow_factory() as uow:
                await uow.agent_runs.transition_status(
                    decided.run_id,
                    expected=(RunStatus.RUNNING,),
                    status=inspection.status,
                    owner=worker_owner,
                    status_reason=inspection.status_reason,
                    finished_at=self._clock.now(),
                    release_lease=True,
                )
                await uow.commit()
        elif inspection.phase is CheckpointPhase.PAUSED:
            async with self._uow_factory() as uow:
                await uow.agent_runs.transition_status(
                    decided.run_id,
                    expected=(RunStatus.RUNNING,),
                    status=RunStatus.AWAITING_APPROVAL,
                    owner=worker_owner,
                    release_lease=True,
                )
                await uow.commit()

        return DecideApprovalResult(approval=decided, is_winner=True, inspection=inspection)
