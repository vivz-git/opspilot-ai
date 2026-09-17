"""Execution ownership (§2.4, ADR-004, ADR-023): who is driving a run, and
what happens when that process dies.

`leases` — a worker's lease on a run and the heartbeat that keeps it alive.
`recovery` — the reconciler that finds runs whose worker died and resumes
them from their LangGraph checkpoint, repairs them to `awaiting_approval`,
or marks them `failed(orphaned)`.

API-007's `Executor` composes these; nothing here starts a graph on its
own.
"""

from app.execution.approvals import ApprovalService, DecideApprovalResult
from app.execution.leases import (
    LeaseConfig,
    LeaseHeartbeat,
    LeaseNotAcquired,
    hold_lease,
    new_worker_id,
)
from app.execution.recovery import (
    CheckpointInspection,
    CheckpointPhase,
    LangGraphRunDriver,
    Reconciler,
    ReconciliationReport,
    RecoveryOutcome,
    RunDriver,
)
from app.execution.runs import (
    RunCreateResult,
    RunDetails,
    RunService,
)

__all__ = [
    "ApprovalService",
    "CheckpointInspection",
    "CheckpointPhase",
    "DecideApprovalResult",
    "LangGraphRunDriver",
    "LeaseConfig",
    "LeaseHeartbeat",
    "LeaseNotAcquired",
    "Reconciler",
    "ReconciliationReport",
    "RecoveryOutcome",
    "RunCreateResult",
    "RunDetails",
    "RunDriver",
    "RunService",
    "hold_lease",
    "new_worker_id",
]
