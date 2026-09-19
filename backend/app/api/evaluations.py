"""Evaluation HTTP endpoints (§13.7, §15).

Exposes:
- `POST /evaluations/runs` (trigger a suite run)
- `GET /evaluations/runs` (list runs)
- `GET /evaluations/runs/{id}` (one run)
- `GET /evaluations/runs/{id}/results` (that run's per-case results)
- `GET /evaluations/metrics` (the latest run's §15.4 metric snapshot)

Every read is a direct render of `evaluation_runs`/`evaluation_results`
(§12.8) — the rows `EvaluationRunner` (EVAL-002) already persists. Nothing
here recomputes a pass/fail verdict, an invariant, or a metric: the runner
decided those; this module only exposes what it wrote. `POST` starts the
run row synchronously (so the caller gets a real id back) and hands the
actual suite execution — the same `EvaluationRunner.run_suite` the CLI (§EVAL-005)
calls — to a background task, exactly as `python -m app.evaluation.cli run`
does, just triggered over HTTP instead of a terminal.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status

from app.agent.state import PlannerKind
from app.api.dependencies import (
    get_evaluation_registry,
    get_evaluation_runner,
    get_uow_factory,
    require_authorization,
)
from app.api.evaluations_schemas import (
    EvaluationMetricsResponse,
    EvaluationResultResource,
    EvaluationRunCreateRequest,
    EvaluationRunListResponse,
    EvaluationRunResource,
)
from app.errors import InputValidationError, NotFoundError
from app.evaluation.registry import EvaluationRegistry
from app.evaluation.runner import EvaluationRunner
from app.persistence.protocols import UnitOfWorkFactory

__all__ = [
    "router",
]

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


@router.post("/runs", response_model=EvaluationRunResource, status_code=status.HTTP_202_ACCEPTED)
async def create_evaluation_run(
    body: EvaluationRunCreateRequest,
    background_tasks: BackgroundTasks,
    runner: EvaluationRunner = Depends(get_evaluation_runner),
    registry: EvaluationRegistry = Depends(get_evaluation_registry),
    uow_factory: UnitOfWorkFactory = Depends(get_uow_factory),
    _auth: None = Depends(require_authorization),
) -> EvaluationRunResource:
    """Start a suite run. Returns immediately with the `running` row; the
    suite executes in the background and completes the row as the CLI does."""
    if body.suite not in registry.suite_names:
        raise InputValidationError(
            f"unknown evaluation suite {body.suite!r}",
            detail={"suite": body.suite, "known_suites": sorted(registry.suite_names)},
        )
    async with uow_factory() as uow:
        run = await uow.evaluations.create_run(suite=body.suite, planner_kind=PlannerKind.RULES)
        await uow.commit()
    background_tasks.add_task(runner.run_suite, body.suite, evaluation_run_id=run.id)
    return EvaluationRunResource.from_row(run)


@router.get("/runs", response_model=EvaluationRunListResponse)
async def list_evaluation_runs(
    suite: str | None = Query(default=None, description="Filter to one suite."),
    limit: int = Query(default=50, ge=1, le=100, description="Max runs to return."),
    uow_factory: UnitOfWorkFactory = Depends(get_uow_factory),
    _auth: None = Depends(require_authorization),
) -> EvaluationRunListResponse:
    """Return evaluation runs, most recently started first (§12.8)."""
    async with uow_factory() as uow:
        runs = await uow.evaluations.list_runs(suite=suite, limit=limit)
        await uow.commit()
    return EvaluationRunListResponse(items=[EvaluationRunResource.from_row(r) for r in runs])


@router.get("/runs/{evaluation_run_id}", response_model=EvaluationRunResource)
async def get_evaluation_run(
    evaluation_run_id: uuid.UUID,
    uow_factory: UnitOfWorkFactory = Depends(get_uow_factory),
    _auth: None = Depends(require_authorization),
) -> EvaluationRunResource:
    async with uow_factory() as uow:
        run = await uow.evaluations.get_run(evaluation_run_id)
        await uow.commit()
    if run is None:
        raise NotFoundError(f"Evaluation run {evaluation_run_id} not found")
    return EvaluationRunResource.from_row(run)


@router.get("/runs/{evaluation_run_id}/results", response_model=list[EvaluationResultResource])
async def list_evaluation_results(
    evaluation_run_id: uuid.UUID,
    uow_factory: UnitOfWorkFactory = Depends(get_uow_factory),
    _auth: None = Depends(require_authorization),
) -> list[EvaluationResultResource]:
    """Return every case result for one evaluation run, ordered by case id."""
    async with uow_factory() as uow:
        run = await uow.evaluations.get_run(evaluation_run_id)
        if run is None:
            raise NotFoundError(f"Evaluation run {evaluation_run_id} not found")
        results = await uow.evaluations.list_results(evaluation_run_id)
        await uow.commit()
    return [EvaluationResultResource.from_row(r) for r in results]


@router.get("/metrics", response_model=EvaluationMetricsResponse)
async def get_evaluation_metrics(
    suite: str | None = Query(default=None, description="Restrict to the latest run of one suite."),
    uow_factory: UnitOfWorkFactory = Depends(get_uow_factory),
    _auth: None = Depends(require_authorization),
) -> EvaluationMetricsResponse:
    """Return the §15.4 metric snapshot of the most recent matching run.

    An empty `metrics` object (with `evaluation_run_id: null`) means no run
    has been recorded yet — never a fabricated zero-valued snapshot.
    """
    async with uow_factory() as uow:
        runs = await uow.evaluations.list_runs(suite=suite, limit=1)
        await uow.commit()
    if not runs:
        return EvaluationMetricsResponse(evaluation_run_id=None, suite=suite, metrics={})
    latest = runs[0]
    return EvaluationMetricsResponse(
        evaluation_run_id=latest.id, suite=latest.suite, metrics=latest.metrics or {}
    )
