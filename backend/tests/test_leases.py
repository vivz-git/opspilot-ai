"""DB-007 — run ownership leases and heartbeats (§2.4, ADR-023).

Against real Postgres, with genuinely concurrent connections where the
property under test is a race:

- lease acquisition, and re-acquisition by the same owner;
- a competing acquisition race: N workers, exactly one owner;
- a valid heartbeat extends the lease; a stale or non-owner heartbeat is
  refused and never revives an expired lease;
- expired-lease detection, and what is *not* an orphan;
- ownership-guarded status transitions;
- the heartbeat loop and `hold_lease` context, driven by an injected clock
  and ticker (no real sleeping).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from app.agent.state import RunStatus
from app.config import Settings
from app.errors import ConfigurationError
from app.execution.leases import (
    LeaseConfig,
    LeaseHeartbeat,
    LeaseNotAcquired,
    hold_lease,
    new_worker_id,
)
from app.runtime import FixedClock, SequentialIdGenerator
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import (
    LEASE,
    T0,
    Ticker,
    create_run,
    migrate_to_head,
    read_run,
    require_database,
    uow_factory_for,
    wait_for,
)

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
def _database() -> None:
    """Integration classes opt in via `usefixtures`; the pure-function classes
    below run everywhere, database or not."""
    require_database()
    migrate_to_head()


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    from recovery_harness import settings

    engine = create_async_engine(
        settings().database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=20,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


class TestLeaseConfig:
    """Unit — no database."""

    def test_heartbeat_must_be_at_most_half_the_ttl(self) -> None:
        LeaseConfig(ttl=timedelta(seconds=30), heartbeat_interval=timedelta(seconds=15))
        with pytest.raises(ValueError):
            LeaseConfig(ttl=timedelta(seconds=30), heartbeat_interval=timedelta(seconds=16))
        with pytest.raises(ValueError):
            LeaseConfig(ttl=timedelta(seconds=0), heartbeat_interval=timedelta(seconds=1))

    def test_settings_fuse_refuses_an_interval_that_would_orphan_healthy_runs(self) -> None:
        good = Settings(
            OPSPILOT_LEASE_TTL_SECONDS=30,  # type: ignore[call-arg]
            OPSPILOT_HEARTBEAT_INTERVAL_SECONDS=10,  # type: ignore[call-arg]
        )
        good.validate_runtime()
        assert LeaseConfig.from_settings(good) == LeaseConfig(
            ttl=timedelta(seconds=30), heartbeat_interval=timedelta(seconds=10)
        )
        bad = Settings(
            OPSPILOT_LEASE_TTL_SECONDS=30,  # type: ignore[call-arg]
            OPSPILOT_HEARTBEAT_INTERVAL_SECONDS=20,  # type: ignore[call-arg]
        )
        with pytest.raises(ConfigurationError, match="HEARTBEAT_INTERVAL"):
            bad.validate_runtime()

    def test_worker_ids_are_unique_per_process_start(self) -> None:
        ids = SequentialIdGenerator()
        assert new_worker_id(ids) == "worker-1"
        assert new_worker_id(ids, label="reconciler") == "reconciler-2"


@pytest.mark.usefixtures("_database")
class TestAcquire:
    async def test_acquire_takes_a_free_lease_and_is_idempotent_for_its_owner(
        self, engine: AsyncEngine
    ) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with uow_factory() as uow:
            first = await uow.agent_runs.acquire_lease(run_id, owner="w1", now=T0, ttl=LEASE.ttl)
            assert first is not None
            assert (first.lease_owner, first.lease_expires_at) == ("w1", T0 + LEASE.ttl)
            again = await uow.agent_runs.acquire_lease(
                run_id, owner="w1", now=T0 + timedelta(seconds=5), ttl=LEASE.ttl
            )
            assert again is not None
            assert again.lease_expires_at == T0 + timedelta(seconds=5) + LEASE.ttl
            await uow.commit()

    async def test_acquire_can_move_status_in_the_same_statement(self, engine: AsyncEngine) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory, status=RunStatus.AWAITING_APPROVAL)
        async with uow_factory() as uow:
            # The approval-resume shape (§9.6): awaiting_approval → running + lease, atomically.
            row = await uow.agent_runs.acquire_lease(
                run_id,
                owner="w1",
                now=T0,
                ttl=LEASE.ttl,
                expected=(RunStatus.AWAITING_APPROVAL,),
                status=RunStatus.RUNNING,
            )
            assert row is not None
            assert row.status is RunStatus.RUNNING
            assert row.lease_owner == "w1"
            await uow.commit()

    async def test_acquire_refuses_a_run_that_is_not_in_an_expected_status(
        self, engine: AsyncEngine
    ) -> None:
        uow_factory = uow_factory_for(engine)
        for status in (
            RunStatus.CREATED,
            RunStatus.AWAITING_APPROVAL,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.REJECTED,
        ):
            run_id = await create_run(uow_factory, status=status)
            async with uow_factory() as uow:
                assert (
                    await uow.agent_runs.acquire_lease(run_id, owner="w1", now=T0, ttl=LEASE.ttl)
                    is None
                ), status

    async def test_acquire_refuses_while_another_owner_holds_a_live_lease(
        self, engine: AsyncEngine
    ) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with uow_factory() as uow:
            assert await uow.agent_runs.acquire_lease(run_id, owner="w1", now=T0, ttl=LEASE.ttl)
            await uow.commit()
        async with uow_factory() as uow:
            just_before_expiry = T0 + LEASE.ttl - timedelta(milliseconds=1)
            assert (
                await uow.agent_runs.acquire_lease(
                    run_id, owner="w2", now=just_before_expiry, ttl=LEASE.ttl
                )
                is None
            )
            # ... and succeeds the instant the lease has expired.
            taken = await uow.agent_runs.acquire_lease(
                run_id, owner="w2", now=T0 + LEASE.ttl, ttl=LEASE.ttl
            )
            assert taken is not None and taken.lease_owner == "w2"
            await uow.commit()

    async def test_competing_acquisition_race_yields_exactly_one_owner(
        self, engine: AsyncEngine
    ) -> None:
        """Twelve workers on twelve connections race for one free run. The
        conditional UPDATE serializes on the row lock; every loser
        re-evaluates its WHERE against the winner's committed row and gets
        nothing. This is the property that makes 'two live workers own the
        same run' impossible, and it is decided by Postgres, not Python."""
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        start = asyncio.Event()

        async def contend(owner: str) -> str | None:
            async with uow_factory() as uow:
                await start.wait()
                row = await uow.agent_runs.acquire_lease(run_id, owner=owner, now=T0, ttl=LEASE.ttl)
                await uow.commit()
                return row.lease_owner if row is not None else None

        tasks = [asyncio.create_task(contend(f"w{i}")) for i in range(12)]
        await asyncio.sleep(0)
        start.set()
        results = await asyncio.gather(*tasks)
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        row = await read_run(uow_factory, run_id)
        assert row.lease_owner == winners[0]
        assert row.lease_expires_at == T0 + LEASE.ttl


@pytest.mark.usefixtures("_database")
class TestHeartbeat:
    async def test_valid_heartbeat_extends_the_lease(self, engine: AsyncEngine) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with uow_factory() as uow:
            assert await uow.agent_runs.acquire_lease(run_id, owner="w1", now=T0, ttl=LEASE.ttl)
            later = T0 + timedelta(seconds=10)
            assert await uow.agent_runs.heartbeat_lease(
                run_id, owner="w1", now=later, ttl=LEASE.ttl
            )
            await uow.commit()
        row = await read_run(uow_factory, run_id)
        assert row.lease_expires_at == later + LEASE.ttl
        assert row.lease_owner == "w1"

    async def test_non_owner_heartbeat_is_refused_and_changes_nothing(
        self, engine: AsyncEngine
    ) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with uow_factory() as uow:
            assert await uow.agent_runs.acquire_lease(run_id, owner="w1", now=T0, ttl=LEASE.ttl)
            await uow.commit()
        async with uow_factory() as uow:
            assert not await uow.agent_runs.heartbeat_lease(
                run_id, owner="w2", now=T0 + timedelta(seconds=1), ttl=LEASE.ttl
            )
            await uow.commit()
        row = await read_run(uow_factory, run_id)
        assert (row.lease_owner, row.lease_expires_at) == ("w1", T0 + LEASE.ttl)

    async def test_expired_lease_cannot_be_revived_by_its_old_owner(
        self, engine: AsyncEngine
    ) -> None:
        """The ownership boundary: once the lease has lapsed, the previous
        owner is refused even if nobody else has claimed the run yet —
        because someone may be about to, and a revived lease would make two
        workers believe they own it."""
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with uow_factory() as uow:
            assert await uow.agent_runs.acquire_lease(run_id, owner="w1", now=T0, ttl=LEASE.ttl)
            await uow.commit()
        async with uow_factory() as uow:
            assert not await uow.agent_runs.heartbeat_lease(
                run_id, owner="w1", now=T0 + LEASE.ttl, ttl=LEASE.ttl
            )
            await uow.commit()
        row = await read_run(uow_factory, run_id)
        assert row.lease_expires_at == T0 + LEASE.ttl  # untouched

    async def test_old_owner_cannot_heartbeat_over_a_new_owner(self, engine: AsyncEngine) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with uow_factory() as uow:
            assert await uow.agent_runs.acquire_lease(run_id, owner="w1", now=T0, ttl=LEASE.ttl)
            await uow.commit()
        takeover = T0 + LEASE.ttl + timedelta(seconds=1)
        async with uow_factory() as uow:
            assert await uow.agent_runs.acquire_lease(
                run_id, owner="w2", now=takeover, ttl=LEASE.ttl
            )
            await uow.commit()
        async with uow_factory() as uow:
            # w1 wakes up late and tries to carry on.
            assert not await uow.agent_runs.heartbeat_lease(
                run_id, owner="w1", now=takeover + timedelta(seconds=1), ttl=LEASE.ttl
            )
            assert await uow.agent_runs.heartbeat_lease(
                run_id, owner="w2", now=takeover + timedelta(seconds=1), ttl=LEASE.ttl
            )
            await uow.commit()
        row = await read_run(uow_factory, run_id)
        assert row.lease_owner == "w2"

    async def test_heartbeat_is_refused_once_the_run_is_no_longer_active(
        self, engine: AsyncEngine
    ) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with uow_factory() as uow:
            assert await uow.agent_runs.acquire_lease(run_id, owner="w1", now=T0, ttl=LEASE.ttl)
            assert await uow.agent_runs.transition_status(
                run_id,
                expected=(RunStatus.QUEUED, RunStatus.RUNNING),
                status=RunStatus.AWAITING_APPROVAL,
                owner="w1",
                release_lease=True,
            )
            await uow.commit()
        async with uow_factory() as uow:
            assert not await uow.agent_runs.heartbeat_lease(
                run_id, owner="w1", now=T0 + timedelta(seconds=1), ttl=LEASE.ttl
            )


@pytest.mark.usefixtures("_database")
class TestRelease:
    async def test_release_clears_only_the_owners_own_lease(self, engine: AsyncEngine) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with uow_factory() as uow:
            assert await uow.agent_runs.acquire_lease(run_id, owner="w1", now=T0, ttl=LEASE.ttl)
            assert not await uow.agent_runs.release_lease(run_id, owner="w2")
            assert await uow.agent_runs.release_lease(run_id, owner="w1")
            assert not await uow.agent_runs.release_lease(run_id, owner="w1")
            await uow.commit()
        row = await read_run(uow_factory, run_id)
        assert (row.lease_owner, row.lease_expires_at) == (None, None)


@pytest.mark.usefixtures("_database")
class TestTransitionStatus:
    async def test_transition_is_guarded_by_expected_status_and_owner(
        self, engine: AsyncEngine
    ) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with uow_factory() as uow:
            assert await uow.agent_runs.acquire_lease(run_id, owner="w1", now=T0, ttl=LEASE.ttl)
            # wrong owner
            assert (
                await uow.agent_runs.transition_status(
                    run_id, expected=(RunStatus.QUEUED,), status=RunStatus.RUNNING, owner="w2"
                )
                is None
            )
            # wrong expected status
            assert (
                await uow.agent_runs.transition_status(
                    run_id, expected=(RunStatus.RUNNING,), status=RunStatus.COMPLETED, owner="w1"
                )
                is None
            )
            row = await uow.agent_runs.transition_status(
                run_id,
                expected=(RunStatus.QUEUED,),
                status=RunStatus.RUNNING,
                owner="w1",
                started_at=T0,
            )
            assert row is not None and row.status is RunStatus.RUNNING
            done = await uow.agent_runs.transition_status(
                run_id,
                expected=(RunStatus.RUNNING,),
                status=RunStatus.COMPLETED,
                owner="w1",
                finished_at=T0 + timedelta(seconds=3),
                duration_ms=3000,
                release_lease=True,
            )
            assert done is not None
            await uow.commit()
        row = await read_run(uow_factory, run_id)
        assert row.status is RunStatus.COMPLETED
        assert (row.lease_owner, row.lease_expires_at) == (None, None)
        assert row.duration_ms == 3000

    async def test_the_database_refuses_a_half_lease(self, engine: AsyncEngine) -> None:
        """`CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))` — a
        lease is all or nothing, enforced by Postgres, not by the repository."""
        import sqlalchemy as sa
        from sqlalchemy.exc import IntegrityError

        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    sa.text("UPDATE opspilot.agent_runs SET lease_owner = 'w1' WHERE id = :id"),
                    {"id": run_id},
                )


@pytest.mark.usefixtures("_database")
class TestOrphanQuery:
    async def test_lists_expired_and_absent_leases_on_active_runs_only(
        self, engine: AsyncEngine
    ) -> None:
        uow_factory = uow_factory_for(engine)
        expired = await create_run(uow_factory, status=RunStatus.RUNNING)
        never_leased = await create_run(uow_factory, status=RunStatus.RUNNING)
        healthy = await create_run(uow_factory, status=RunStatus.RUNNING)
        paused = await create_run(uow_factory, status=RunStatus.AWAITING_APPROVAL)
        terminal = await create_run(uow_factory, status=RunStatus.COMPLETED)
        created = await create_run(uow_factory, status=RunStatus.CREATED)
        long_ago = T0 - timedelta(minutes=10)
        async with uow_factory() as uow:
            assert await uow.agent_runs.acquire_lease(
                expired, owner="dead", now=long_ago, ttl=LEASE.ttl
            )
            assert await uow.agent_runs.acquire_lease(healthy, owner="alive", now=T0, ttl=LEASE.ttl)
            # A paused run may carry a stale lease if the process died right
            # after pausing; it is still not an orphan (§6.3).
            assert await uow.agent_runs.acquire_lease(
                paused,
                owner="dead",
                now=long_ago,
                ttl=LEASE.ttl,
                expected=(RunStatus.AWAITING_APPROVAL,),
            )
            await uow.commit()
        async with uow_factory() as uow:
            found = {row.id for row in await uow.agent_runs.list_orphaned_runs(now=T0, limit=500)}
        assert {expired, never_leased} <= found
        assert not ({healthy, paused, terminal, created} & found)


@pytest.mark.usefixtures("_database")
class TestHeartbeatLoop:
    async def test_loop_renews_until_refused_then_signals_loss(self, engine: AsyncEngine) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        clock = FixedClock(T0)
        async with uow_factory() as uow:
            assert await uow.agent_runs.acquire_lease(run_id, owner="w1", now=T0, ttl=LEASE.ttl)
            await uow.commit()

        ticker = Ticker()
        lost_callbacks: list[str] = []
        heartbeat = LeaseHeartbeat(
            uow_factory=uow_factory,
            run_id=run_id,
            owner="w1",
            clock=clock,
            config=LEASE,
            sleep=ticker.sleep,
            on_lost=lambda: lost_callbacks.append("lost"),
        )
        heartbeat.start()
        await wait_for(lambda: ticker.sleeps == 1)

        clock.advance(seconds=10)
        ticker.tick()
        await wait_for(lambda: heartbeat.beats == 1)
        row = await read_run(uow_factory, run_id)
        assert row.lease_expires_at == T0 + timedelta(seconds=10) + LEASE.ttl
        assert not heartbeat.lost.is_set()

        # The process stalls (GC pause, blocked loop) past the whole TTL...
        clock.advance(seconds=LEASE.ttl.total_seconds() + 1)
        ticker.tick()
        await wait_for(lambda: heartbeat.lost.is_set())
        assert lost_callbacks == ["lost"]
        assert heartbeat.beats == 1
        # ... and the loop has ended on its own; a later beat stays refused.
        assert await heartbeat.beat() is False
        await heartbeat.stop()

    async def test_loop_survives_a_transient_failure_and_lets_the_database_decide(
        self, engine: AsyncEngine
    ) -> None:
        """A heartbeat that *errors* (database blip) is not a heartbeat that
        is *refused*: the loop keeps going, and ownership is decided by the
        next round trip that actually reaches Postgres."""
        real_factory = uow_factory_for(engine)
        run_id = await create_run(real_factory)
        clock = FixedClock(T0)
        async with real_factory() as uow:
            assert await uow.agent_runs.acquire_lease(run_id, owner="w1", now=T0, ttl=LEASE.ttl)
            await uow.commit()

        failures = {"remaining": 1}

        def flaky_factory() -> Any:
            if failures["remaining"]:
                failures["remaining"] -= 1
                raise ConnectionError("database blip")
            return real_factory()

        ticker = Ticker()
        heartbeat = LeaseHeartbeat(
            uow_factory=flaky_factory,
            run_id=run_id,
            owner="w1",
            clock=clock,
            config=LEASE,
            sleep=ticker.sleep,
        )
        heartbeat.start()
        await wait_for(lambda: ticker.sleeps == 1)
        ticker.tick()  # this beat raises ...
        await wait_for(lambda: ticker.sleeps == 2)  # ... and the loop is still alive
        assert heartbeat.beats == 0 and not heartbeat.lost.is_set()
        clock.advance(seconds=5)
        ticker.tick()  # this one reaches the database and renews
        await wait_for(lambda: heartbeat.beats == 1)
        assert (await read_run(real_factory, run_id)).lease_expires_at == (
            T0 + timedelta(seconds=5) + LEASE.ttl
        )
        await heartbeat.stop()

    async def test_hold_lease_acquires_heartbeats_and_releases(self, engine: AsyncEngine) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        clock = FixedClock(T0)
        ticker = Ticker()
        async with hold_lease(
            uow_factory=uow_factory,
            run_id=run_id,
            owner="w1",
            clock=clock,
            config=LEASE,
            status=RunStatus.RUNNING,
            sleep=ticker.sleep,
        ) as (row, heartbeat):
            assert row.status is RunStatus.RUNNING and row.lease_owner == "w1"
            with pytest.raises(LeaseNotAcquired):
                async with hold_lease(
                    uow_factory=uow_factory, run_id=run_id, owner="w2", clock=clock, config=LEASE
                ):
                    pass
            await wait_for(lambda: ticker.sleeps == 1)
            clock.advance(seconds=5)
            ticker.tick()
            await wait_for(lambda: heartbeat.beats == 1)
        released = await read_run(uow_factory, run_id)
        assert (released.lease_owner, released.lease_expires_at) == (None, None)
        assert released.status is RunStatus.RUNNING  # hold_lease owns the lease, not the status

    async def test_hold_lease_releases_on_exception_but_never_someone_elses_lease(
        self, engine: AsyncEngine
    ) -> None:
        uow_factory = uow_factory_for(engine)
        run_id = await create_run(uow_factory)
        clock = FixedClock(T0)
        with pytest.raises(RuntimeError, match="boom"):
            async with hold_lease(
                uow_factory=uow_factory, run_id=run_id, owner="w1", clock=clock, config=LEASE
            ):
                # Meanwhile the lease lapses and another worker takes it over.
                clock.advance(seconds=LEASE.ttl.total_seconds() + 1)
                async with uow_factory() as uow:
                    assert await uow.agent_runs.acquire_lease(
                        run_id, owner="w2", now=clock.now(), ttl=LEASE.ttl
                    )
                    await uow.commit()
                raise RuntimeError("boom")
        row = await read_run(uow_factory, run_id)
        assert row.lease_owner == "w2"  # w1's exit did not release w2's lease
