"""CLI entrypoint for the evaluation gate (§15, EVAL-005).

    python -m app.evaluation.cli run --suite all
    make eval

This module adds no execution path, no metrics computation and no invariant
logic of its own. It composes exactly what EVAL-002/EVAL-003/EVAL-004 already
built — `load_registry`, `EvaluationRunner.run_suite` (which drives the real
service path and persists `evaluation_runs`/`evaluation_results` as it goes),
and the metrics/invariants already attached to each `CaseResult` — then
prints a report and turns the result into a process exit code. The persisted
run row remains the source of truth; this only reads what the runner already
wrote back.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.evaluation.loader import load_registry
from app.evaluation.metrics import SuiteRunResult
from app.evaluation.runner import CaseResult, EvaluationRunner
from app.persistence.checkpointing import open_checkpointer
from app.persistence.session import create_session_factory

__all__ = ["build_arg_parser", "main", "print_report", "run_suite_cli"]


def _echo(line: str = "") -> None:
    print(line)  # noqa: T201


def _print_case(result: CaseResult) -> None:
    status = "PASS" if result.passed else "FAIL"
    _echo(f"  [{status}] {result.case_id}  ({result.duration_ms} ms, run {result.run_id})")
    for failure in result.failures:
        _echo(f"      assertion failed: {failure.name} — {failure.detail}")
    for violation in result.violations:
        _echo(
            f"      invariant[{violation.invariant}] {violation.name} violated — {violation.detail}"
        )


def print_report(results: SuiteRunResult, *, suite: str) -> bool:
    """Print the case-by-case and metric summary; return whether the suite passed.

    A suite passes only when every case's own assertions AND all seven
    global invariants (EVAL-004) held — `CaseResult.passed` already encodes
    both, so this makes no separate judgement of its own.
    """
    _echo(f"Evaluation suite: {suite}")
    _echo(f"Evaluation run id: {results.evaluation_run_id}")
    _echo()
    _echo("Cases:")
    for result in results:
        _print_case(result)

    metrics = results.metrics
    _echo()
    _echo("Metrics:")
    _echo(f"  case_pass_rate:    {metrics.get('case_pass_rate', 0.0):.2%}")
    _echo(f"  task_success_rate: {metrics.get('task_success_rate', 0.0):.2%}")
    _echo(
        "  agent_duration_ms: "
        f"avg={metrics.get('avg_duration_ms', 0.0)} "
        f"p50={metrics.get('p50_duration_ms', 0.0)} "
        f"p95={metrics.get('p95_duration_ms', 0.0)}"
    )
    _echo(f"  total_cases:       {metrics.get('total_cases', 0)}")
    _echo(f"  passed_cases:      {metrics.get('passed_cases', 0)}")
    _echo(f"  failed_cases:      {metrics.get('failed_cases', 0)}")

    failed_cases = [r for r in results if not r.passed]
    invariant_violations = [(r.case_id, v) for r in results for v in r.violations]

    _echo()
    if failed_cases or invariant_violations:
        _echo(
            f"FAILED — {len(failed_cases)} of {len(results)} case(s) failed, "
            f"{len(invariant_violations)} invariant violation(s)"
        )
    else:
        _echo(f"PASSED — {len(results)} case(s), 0 invariant violations")

    return not failed_cases and not invariant_violations


async def run_suite_cli(suite: str) -> bool:
    """Compose the same runtime EVAL-002 composes and run `suite` through it."""
    settings = get_settings()
    registry = load_registry()
    engine = create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    try:
        async with open_checkpointer(settings) as checkpointer:
            runner = EvaluationRunner(
                settings=settings,
                session_factory=create_session_factory(engine),
                checkpointer=checkpointer,
                registry=registry,
            )
            results = await runner.run_suite(suite)
    finally:
        await engine.dispose()
    return print_report(results, suite=suite)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.evaluation.cli",
        description="Run the OpsPilot evaluation suite and gate on its result.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Run an evaluation suite")
    run_parser.add_argument(
        "--suite",
        default="all",
        help="Suite name declared in evals/suites.yaml (default: all)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    passed = asyncio.run(run_suite_cli(args.suite))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
