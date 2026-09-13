"""Run ownership leases and the heartbeat that keeps them alive (§2.4, DB-007,
ADR-023).

**The model.** A run in `queued`/`running` is owned by at most one worker,
identified by `agent_runs.lease_owner`, until `agent_runs.lease_expires_at`.
The owner extends the lease on every heartbeat. If the owner's process dies,
no heartbeat arrives, the lease expires, and the reconciler
(`app.execution.recovery`) may take the run over. A run paused for a human
(`awaiting_approval`) has **no** owner — the driving task ends on interrupt
(§6.3) — so a missing heartbeat there is by design, never an orphan.

**Fencing.** Ownership decisions are made by the database in single
conditional `UPDATE`s (`SqlAgentRunRepository.acquire_lease` /
`heartbeat_lease` / `release_lease`), so two workers can never both be
told they own a run. A heartbeat succeeds only for the *current, unexpired*
owner: a worker whose lease lapsed is refused rather than revived, because
someone else may already have claimed the run. On refusal, `LeaseHeartbeat`
sets `lost` and calls `on_lost`, and the worker must stop driving the run.

**Time.** Every lease decision uses the injected `Clock` (FOUND-004), passed
explicitly to the repository as `now`, never `now()` in SQL — so tests can
expire a lease by advancing a `FixedClock` instead of sleeping, and every
worker and the reconciler agree on the same notion of time.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta

import structlog

from app.agent.state import RunStatus
from app.config import Settings
from app.persistence.models import AgentRun
from app.persistence.protocols import UnitOfWork
from app.runtime import Clock, IdGenerator

__all__ = [
    "LeaseConfig",
    "LeaseHeartbeat",
    "LeaseNotAcquired",
    "UnitOfWorkFactory",
    "hold_lease",
    "new_worker_id",
]

#: How the execution layer obtains a transaction: a zero-argument callable
#: returning a unit-of-work context (`functools.partial(unit_of_work,
#: session_factory)` in production). Keeps this package on the repository
#: protocols, never on a SQLAlchemy session (§12, DB-005).
UnitOfWorkFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]

_log = structlog.get_logger("opspilot.leases")


@dataclass(frozen=True)
class LeaseConfig:
    """`ttl` is how long a lease lasts without a heartbeat; the heartbeat
    interval must be at most half of it, so a single missed beat never costs
    a healthy worker its run (`Settings.validate_runtime` is the fuse for the
    environment; this guards direct construction)."""

    ttl: timedelta
    heartbeat_interval: timedelta

    def __post_init__(self) -> None:
        if self.ttl <= timedelta(0) or self.heartbeat_interval <= timedelta(0):
            raise ValueError("lease ttl and heartbeat interval must be positive")
        if self.heartbeat_interval * 2 > self.ttl:
            raise ValueError("heartbeat interval must be at most half the lease ttl")

    @classmethod
    def from_settings(cls, settings: Settings) -> LeaseConfig:
        return cls(ttl=settings.lease_ttl, heartbeat_interval=settings.heartbeat_interval)


class LeaseNotAcquired(Exception):
    """Another live worker owns the run, or it is not in a leasable status."""

    def __init__(self, run_id: uuid.UUID, owner: str) -> None:
        super().__init__(f"run {run_id} is not available to {owner}")
        self.run_id = run_id
        self.owner = owner


def new_worker_id(ids: IdGenerator, *, label: str = "worker") -> str:
    """A worker's identity for the lifetime of one process (or one
    reconciler). Unique per process start, so a restarted process is a
    *different* owner and cannot mistake its predecessor's lease for its
    own."""
    return ids.new_id(prefix=f"{label}-")


class LeaseHeartbeat:
    """Renews one run's lease every `heartbeat_interval` until stopped or
    refused.

    `beat()` is the single renewal and is public so callers (and tests) can
    drive it directly against an injected clock; `start()`/`stop()` run it
    on a background task with an injectable `sleep` so the loop itself is
    testable without real waiting.
    """

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        run_id: uuid.UUID,
        owner: str,
        clock: Clock,
        config: LeaseConfig,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_lost: Callable[[], None] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._run_id = run_id
        self._owner = owner
        self._clock = clock
        self._config = config
        self._sleep = sleep
        self._on_lost = on_lost
        self._task: asyncio.Task[None] | None = None
        #: Set once a heartbeat is refused. Never cleared: a lost lease is
        #: lost, and the run now belongs to someone else (or to nobody).
        self.lost = asyncio.Event()
        self.beats = 0

    @property
    def run_id(self) -> uuid.UUID:
        return self._run_id

    @property
    def owner(self) -> str:
        return self._owner

    async def beat(self) -> bool:
        """One renewal. `False` means the lease was refused — this worker no
        longer owns the run and must stop."""
        if self.lost.is_set():
            return False
        now = self._clock.now()
        async with self._uow_factory() as uow:
            renewed = await uow.agent_runs.heartbeat_lease(
                self._run_id, owner=self._owner, now=now, ttl=self._config.ttl
            )
            await uow.commit()
        if renewed:
            self.beats += 1
            _log.debug("lease_heartbeat", run_id=str(self._run_id), owner=self._owner)
            return True
        self._mark_lost()
        return False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"heartbeat:{self._run_id}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        interval = self._config.heartbeat_interval.total_seconds()
        while not self.lost.is_set():
            await self._sleep(interval)
            try:
                renewed = await self.beat()
            except Exception as exc:  # noqa: BLE001 - a blip must not silently kill the loop
                # The database, not an exception, decides ownership: keep
                # trying, and if the lease really lapsed the next successful
                # round trip is refused and marks the loss.
                _log.warning(
                    "lease_heartbeat_failed",
                    run_id=str(self._run_id),
                    owner=self._owner,
                    error=repr(exc),
                )
                continue
            if not renewed:
                return

    def _mark_lost(self) -> None:
        if self.lost.is_set():
            return
        self.lost.set()
        _log.warning("lease_lost", run_id=str(self._run_id), owner=self._owner)
        if self._on_lost is not None:
            self._on_lost()


@asynccontextmanager
async def hold_lease(
    *,
    uow_factory: UnitOfWorkFactory,
    run_id: uuid.UUID,
    owner: str,
    clock: Clock,
    config: LeaseConfig,
    expected: Iterable[RunStatus] = (RunStatus.QUEUED, RunStatus.RUNNING),
    status: RunStatus | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_lost: Callable[[], None] | None = None,
) -> AsyncIterator[tuple[AgentRun, LeaseHeartbeat]]:
    """Own `run_id` for the duration of the block.

    Acquires the lease (raising `LeaseNotAcquired` if another live worker
    holds it), starts the heartbeat, yields the claimed row and the
    heartbeat, and on exit — normal, exception or cancellation — stops the
    heartbeat and releases the lease if this owner still holds it. A crashed
    process never reaches the exit; that is the case the reconciler exists
    for.
    """
    now = clock.now()
    async with uow_factory() as uow:
        claimed = await uow.agent_runs.acquire_lease(
            run_id, owner=owner, now=now, ttl=config.ttl, expected=expected, status=status
        )
        if claimed is None:
            raise LeaseNotAcquired(run_id, owner)
        await uow.commit()
    _log.info("lease_acquired", run_id=str(run_id), owner=owner, status=str(claimed.status))

    heartbeat = LeaseHeartbeat(
        uow_factory=uow_factory,
        run_id=run_id,
        owner=owner,
        clock=clock,
        config=config,
        sleep=sleep,
        on_lost=on_lost,
    )
    heartbeat.start()
    try:
        yield claimed, heartbeat
    finally:
        await heartbeat.stop()
        async with uow_factory() as uow:
            released = await uow.agent_runs.release_lease(run_id, owner=owner)
            await uow.commit()
        _log.info("lease_released", run_id=str(run_id), owner=owner, released=released)
