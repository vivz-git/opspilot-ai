"""Injected time, identity and randomness (§1.4, §18.2).

Determinism is a ranked quality attribute: with a frozen clock, a fixed seed
and the rule planner, a run must be byte-reproducible. That is only true if
every non-reproducible primitive is swappable for a fake, which means no
other module may call `datetime.now()`, `uuid.uuid4()` or the `random` module
directly — `tests/test_structure.py` enforces this structurally.

`SeededRandom` has one implementation, not a real/fake pair: unlike wall-clock
time or a UUID, determinism here comes from the seed itself
(`Settings.seed`), not from the object being a test double. Production and
the evaluation suite use the same class with different seeds.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new_id(self, *, prefix: str = "") -> str: ...


class SeededRandom(Protocol):
    def random(self) -> float: ...
    def uniform(self, a: float, b: float) -> float: ...
    def randint(self, a: int, b: int) -> int: ...


class SystemClock:
    """Real time, timezone-aware UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Test double: time only moves when told to (§10.5's known trap —
    never `time.sleep` in a test)."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value

    def advance(self, *, seconds: float = 0, ms: int = 0) -> None:
        self._now += timedelta(seconds=seconds, milliseconds=ms)


class UuidIdGenerator:
    """Real ids: a UUID4 hex string, optionally prefixed (e.g. `run_...`)."""

    def new_id(self, *, prefix: str = "") -> str:
        value = uuid.uuid4().hex
        return f"{prefix}{value}" if prefix else value


class SequentialIdGenerator:
    """Test double: deterministic, human-readable, strictly increasing ids."""

    def __init__(self) -> None:
        self._counter = 0

    def new_id(self, *, prefix: str = "") -> str:
        self._counter += 1
        return f"{prefix}{self._counter}" if prefix else str(self._counter)


class DeterministicRandom:
    """`random.Random` behind the `SeededRandom` protocol — the only
    legitimate call site for the `random` module outside this file."""

    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)  # noqa: S311 - deterministic simulation, not crypto

    def random(self) -> float:
        return self._random.random()

    def uniform(self, a: float, b: float) -> float:
        return self._random.uniform(a, b)

    def randint(self, a: int, b: int) -> int:
        return self._random.randint(a, b)
