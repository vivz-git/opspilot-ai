"""Injected time, identity and randomness (§1.4, §18.2; FOUND-004)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.runtime import (
    Clock,
    DeterministicRandom,
    FixedClock,
    IdGenerator,
    SeededRandom,
    SequentialIdGenerator,
    SystemClock,
    UuidIdGenerator,
)

pytestmark = [pytest.mark.unit]


class TestClock:
    def test_system_clock_returns_timezone_aware_utc(self) -> None:
        clock: Clock = SystemClock()
        now = clock.now()
        assert now.tzinfo is not None
        assert now.utcoffset() is not None and now.utcoffset().total_seconds() == 0

    def test_fixed_clock_does_not_move_on_its_own(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        clock: Clock = FixedClock(start)
        assert clock.now() == start
        assert clock.now() == start

    def test_fixed_clock_only_advances_when_told(self) -> None:
        clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
        clock.advance(seconds=30)
        assert clock.now() == datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
        clock.advance(ms=500)
        assert clock.now() == datetime(2026, 1, 1, 0, 0, 30, 500_000, tzinfo=UTC)

    def test_fixed_clock_can_be_set_directly(self) -> None:
        clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
        later = datetime(2026, 6, 1, tzinfo=UTC)
        clock.set(later)
        assert clock.now() == later


class TestIdGenerator:
    def test_uuid_id_generator_produces_unique_ids(self) -> None:
        gen: IdGenerator = UuidIdGenerator()
        ids = {gen.new_id() for _ in range(100)}
        assert len(ids) == 100

    def test_uuid_id_generator_honours_a_prefix(self) -> None:
        gen: IdGenerator = UuidIdGenerator()
        assert gen.new_id(prefix="run_").startswith("run_")

    def test_sequential_id_generator_is_deterministic(self) -> None:
        gen: IdGenerator = SequentialIdGenerator()
        assert [gen.new_id() for _ in range(3)] == ["1", "2", "3"]

    def test_sequential_id_generator_honours_a_prefix(self) -> None:
        gen = SequentialIdGenerator()
        assert gen.new_id(prefix="s") == "s1"
        assert gen.new_id(prefix="s") == "s2"

    def test_two_generators_are_independent(self) -> None:
        a, b = SequentialIdGenerator(), SequentialIdGenerator()
        a.new_id()
        assert b.new_id() == "1"


class TestSeededRandom:
    def test_same_seed_produces_the_same_sequence(self) -> None:
        a: SeededRandom = DeterministicRandom(1337)
        b: SeededRandom = DeterministicRandom(1337)
        assert [a.random() for _ in range(5)] == [b.random() for _ in range(5)]

    def test_different_seeds_diverge(self) -> None:
        a = DeterministicRandom(1)
        b = DeterministicRandom(2)
        assert [a.random() for _ in range(5)] != [b.random() for _ in range(5)]

    def test_uniform_is_within_bounds(self) -> None:
        rng = DeterministicRandom(1337)
        for _ in range(50):
            assert 0.0 <= rng.uniform(0.0, 1.0) <= 1.0

    def test_randint_is_within_bounds_inclusive(self) -> None:
        rng = DeterministicRandom(1337)
        for _ in range(50):
            assert 1 <= rng.randint(1, 3) <= 3
