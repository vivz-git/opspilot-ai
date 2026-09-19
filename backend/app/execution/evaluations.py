"""Evaluation application service (§13.6, API-005).

Wraps the existing `EvaluationRunner` (§15.5) with the control-plane
operations the API needs: trigger a suite in the background and return
immediately (`202`), list/read the persisted `evaluation_runs` rows the
runner already writes, and read the metric snapshot each run already
carries. This service adds no execution path of its own — `trigger_run`
schedules exactly the same `EvaluationRunner.run_suite` the CLI (`python -m
app.evaluation.cli run`) calls, and every read here is a read of rows the
runner (or the CLI wrapping it) already persisted.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, timedelta
from typing import Final

import structlog

from app.agent.state import PlannerKind
from app.errors import InputValidationError, NotFoundError
from app.evaluation.registry import EvaluationRegistry
from app.evaluation.runner import EvaluationRunner
from app.persistence.models import EvaluationResult, EvaluationRun, EvaluationRunStatus
from app.persistence.protocols import UnitOfWorkFactory
from app.runtime import Clock, SystemClock

__all__ = ["EvaluationService"]

_log = structlog.get_logger("opspilot.execution.evaluations")

#: `?window=30d` / `?window=24h` (§13.6). Deliberately small: this is a
#: dashboard filter, not a general date-math parser.
_WINDOW_PATTERN: Final = re.compile(r"^(?P<value>[1-9][0-9]*)(?P<unit>[hd])$")


def _parse_window(window: str | None) -> timedelta | None:
    if window is None:
        return None
    match = _WINDOW_PATTERN.match(window)
    if match is None:
        raise InputValidationError(
            f"window {window!r} is not a valid duration (expected e.g. '30d' or '24h')"
        )
    value = int(match.group("value"))
    return timedelta(hours=value) if match.group("unit") == "h" else timedelta(days=value)


class EvaluationService:
    """Control-plane service around `EvaluationRunner` (§15.5, §13.6)."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        registry: EvaluationRegistry,
        runner: EvaluationRunner,
        clock: Clock | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry
        self._runner = runner
        self._clock = clock or SystemClock()
        #: Keeps background tasks referenced so they are not garbage
        #: collected mid-flight (the well-known `asyncio.create_task` trap).
        self._background: set[asyncio.Task[None]] = set()

    def _validate_suite_and_cases(
        self, suite: str, case_ids: list[str] | None
    ) -> tuple[str, ...] | None:
        if suite not in self._registry.suite_names:
            raise InputValidationError(
                f"unknown suite {suite!r}",
                detail={"known_suites": sorted(self._registry.suite_names)},
            )
        if not case_ids:
            return None
        suite_case_ids = {c.id for c in self._registry.suite_cases(suite)}
        unknown = sorted(set(case_ids) - suite_case_ids)
        if unknown:
            raise InputValidationError(
                f"case_ids not in suite {suite!r}: {unknown}",
                detail={"suite": suite, "unknown": unknown},
            )
        return tuple(case_ids)

    async def trigger_run(
        self,
        *,
        suite: str,
        case_ids: list[str] | None = None,
        planner: PlannerKind | None = None,
    ) -> EvaluationRun:
        """Persist the `evaluation_runs` row and schedule the real suite
        mechanism in the background — the response never waits for the
        suite to finish (§13.2's `202` pattern, applied to evaluations)."""
        validated_case_ids = self._validate_suite_and_cases(suite, case_ids)

        async with self._uow_factory() as uow:
            eval_run = await uow.evaluations.create_run(
                suite=suite, planner_kind=planner or PlannerKind.RULES
            )
            await uow.commit()

        task = asyncio.create_task(self._run_and_settle(suite, eval_run.id, validated_case_ids))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return eval_run

    async def _run_and_settle(
        self, suite: str, evaluation_run_id: uuid.UUID, case_ids: tuple[str, ...] | None
    ) -> None:
        try:
            await self._runner.run_suite(
                suite, evaluation_run_id=evaluation_run_id, case_ids=case_ids
            )
        except Exception as exc:  # noqa: BLE001 - a crashed suite must still settle its row
            _log.error(
                "evaluation_run_crashed",
                evaluation_run_id=str(evaluation_run_id),
                suite=suite,
                error=repr(exc),
            )
            async with self._uow_factory() as uow:
                await uow.evaluations.complete_run(
                    evaluation_run_id,
                    status=EvaluationRunStatus.FAILED,
                    finished_at=self._clock.now(),
                    case_count=0,
                    passed=0,
                    failed=0,
                    metrics={},
                )
                await uow.commit()

    async def get_run(self, evaluation_run_id: uuid.UUID) -> EvaluationRun | None:
        async with self._uow_factory() as uow:
            run = await uow.evaluations.get_run(evaluation_run_id)
            await uow.commit()
        return run

    async def require_run(self, evaluation_run_id: uuid.UUID) -> EvaluationRun:
        run = await self.get_run(evaluation_run_id)
        if run is None:
            raise NotFoundError(f"Evaluation run {evaluation_run_id} not found")
        return run

    async def list_runs(self, *, suite: str | None = None, limit: int = 25) -> list[EvaluationRun]:
        async with self._uow_factory() as uow:
            runs = await uow.evaluations.list_runs(suite=suite, limit=limit)
            await uow.commit()
        return runs

    async def list_results(
        self, evaluation_run_id: uuid.UUID, *, passed: bool | None = None
    ) -> list[EvaluationResult]:
        await self.require_run(evaluation_run_id)
        async with self._uow_factory() as uow:
            results = await uow.evaluations.list_results(evaluation_run_id)
            await uow.commit()
        if passed is not None:
            results = [r for r in results if r.passed == passed]
        return results

    async def get_metrics(
        self, *, suite: str | None = None, window: str | None = None, limit: int = 100
    ) -> list[EvaluationRun]:
        """The persisted `metrics` snapshot (§15.4) per run, newest first,
        within `window` if given. `evaluation_runs` remains the source of
        truth — this reads what the runner already computed and wrote,
        never recomputes a metric."""
        span = _parse_window(window)
        runs = await self.list_runs(suite=suite, limit=limit)
        if span is None:
            return runs
        cutoff = self._clock.now().astimezone(UTC) - span
        return [r for r in runs if r.started_at.astimezone(UTC) >= cutoff]
