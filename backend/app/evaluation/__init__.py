"""The evaluation definition layer (§15, EVAL-001).

    backend/evals/*.yaml ──safe_load──► schemas ──cross-validate──► EvaluationRegistry

This package *describes* evaluations; it does not run them. It has no
database session, no network client, no tool dispatch and no LLM call — the
runner that drives the real service path is EVAL-002 and lives beside this,
not inside it. Keeping the boundary here is what makes a case file
reviewable as a specification rather than as a test script.
"""

from __future__ import annotations

from app.evaluation.loader import (
    DEFAULT_EVALS_ROOT,
    load_case,
    load_cases,
    load_fixtures,
    load_registry,
    load_suites,
)
from app.evaluation.metrics import (
    CaseMetricInput,
    SuiteRunResult,
    calculate_agent_duration_ms,
    calculate_approval_wait_ms,
    calculate_case_pass_rate,
    calculate_task_success_rate,
    compute_evaluation_metrics,
    compute_evaluation_metrics_from_results,
)
from app.evaluation.registry import EvaluationRegistry
from app.evaluation.schemas import (
    REQUIRED_CASE_IDS,
    REQUIRED_SUITES,
    EvalCase,
    ExpectSpec,
    FixtureDataset,
    GivenSpec,
    SuitesManifest,
)

__all__ = [
    "DEFAULT_EVALS_ROOT",
    "REQUIRED_CASE_IDS",
    "REQUIRED_SUITES",
    "CaseMetricInput",
    "EvalCase",
    "EvaluationRegistry",
    "ExpectSpec",
    "FixtureDataset",
    "GivenSpec",
    "SuiteRunResult",
    "SuitesManifest",
    "calculate_agent_duration_ms",
    "calculate_approval_wait_ms",
    "calculate_case_pass_rate",
    "calculate_task_success_rate",
    "compute_evaluation_metrics",
    "compute_evaluation_metrics_from_results",
    "load_case",
    "load_cases",
    "load_fixtures",
    "load_registry",
    "load_suites",
]
