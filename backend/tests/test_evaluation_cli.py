"""EVAL-005: the evaluation CLI and its exit-code gate.

Unit only — no database, no LangGraph saver. `print_report` and `main` are
pure over an already-computed `SuiteRunResult`/`CaseResult`, so the gate
logic (fail on any failing case or violated invariant, exit zero otherwise)
is exercised without re-running EVAL-002/003/004's own integration suite,
which `tests/test_evaluation_runner.py` already covers end to end.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml
from app.agent.state import RunStatus
from app.evaluation.cli import build_arg_parser, main, print_report
from app.evaluation.invariants import InvariantOutcome
from app.evaluation.metrics import SuiteRunResult
from app.evaluation.runner import AssertionOutcome, CaseResult

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent


def _case(
    case_id: str,
    *,
    passed: bool,
    assertions: tuple[AssertionOutcome, ...] = (),
    invariants: tuple[InvariantOutcome, ...] = (),
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        run_id=uuid.uuid4(),
        passed=passed,
        assertions=assertions,
        duration_ms=100,
        final_status=RunStatus.COMPLETED,
        status_reason=None,
        tool_calls_count=1,
        retry_count=0,
        approval_outcome=None,
        invariants=invariants,
    )


def _suite(*results: CaseResult, metrics: dict[str, object] | None = None) -> SuiteRunResult:
    return SuiteRunResult(results, evaluation_run_id=uuid.uuid4(), metrics=metrics or {})


@pytest.mark.unit
class TestPrintReport:
    def test_all_passing_cases_pass_the_suite(self, capsys: pytest.CaptureFixture[str]) -> None:
        results = _suite(_case("happy_path_multi_step", passed=True))
        assert print_report(results, suite="all") is True
        out = capsys.readouterr().out
        assert "PASS" in out
        assert "PASSED" in out
        assert "FAILED" not in out

    def test_a_failing_case_assertion_fails_the_suite(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        failing = _case(
            "invalid_tool_result",
            passed=False,
            assertions=(AssertionOutcome("final_status", False, "got failed"),),
        )
        results = _suite(_case("happy_path_multi_step", passed=True), failing)
        assert print_report(results, suite="all") is False
        out = capsys.readouterr().out
        assert "[FAIL] invalid_tool_result" in out
        assert "assertion failed: final_status" in out
        assert "FAILED" in out

    def test_a_violated_invariant_fails_the_suite_even_if_assertions_hold(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        violation = InvariantOutcome(
            invariant=6,
            name="trace_seq_gapless_and_monotonic",
            passed=False,
            detail="gap at seq=3",
        )
        case_with_violation = _case(
            "happy_path_multi_step",
            passed=False,
            invariants=(violation,),
        )
        results = _suite(case_with_violation)
        assert print_report(results, suite="all") is False
        out = capsys.readouterr().out
        assert "invariant[6] trace_seq_gapless_and_monotonic violated" in out
        assert "invariant violation" in out
        assert "FAILED" in out

    def test_metric_summary_is_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        results = _suite(
            _case("happy_path_multi_step", passed=True),
            metrics={
                "case_pass_rate": 1.0,
                "task_success_rate": 1.0,
                "avg_duration_ms": 42.0,
                "p50_duration_ms": 40.0,
                "p95_duration_ms": 50.0,
                "total_cases": 1,
                "passed_cases": 1,
                "failed_cases": 0,
            },
        )
        print_report(results, suite="all")
        out = capsys.readouterr().out
        assert "Metrics:" in out
        assert "case_pass_rate:    100.00%" in out
        assert "task_success_rate: 100.00%" in out
        assert "total_cases:       1" in out


@pytest.mark.unit
class TestMain:
    def test_exits_zero_when_the_suite_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, str] = {}

        async def fake_run_suite_cli(suite: str) -> bool:
            captured["suite"] = suite
            return True

        monkeypatch.setattr("app.evaluation.cli.run_suite_cli", fake_run_suite_cli)
        assert main(["run", "--suite", "smoke"]) == 0
        assert captured["suite"] == "smoke"

    def test_exits_nonzero_when_the_suite_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run_suite_cli(suite: str) -> bool:
            return False

        monkeypatch.setattr("app.evaluation.cli.run_suite_cli", fake_run_suite_cli)
        assert main(["run"]) == 1

    def test_defaults_to_the_all_suite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, str] = {}

        async def fake_run_suite_cli(suite: str) -> bool:
            captured["suite"] = suite
            return True

        monkeypatch.setattr("app.evaluation.cli.run_suite_cli", fake_run_suite_cli)
        main(["run"])
        assert captured["suite"] == "all"

    def test_requires_a_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            build_arg_parser().parse_args([])


@pytest.mark.unit
class TestMakeEvalTarget:
    def test_make_eval_invokes_the_cli_module_against_all(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        assert "python -m app.evaluation.cli run --suite all" in makefile


@pytest.mark.unit
class TestCiEvaluationGate:
    def test_ci_workflow_is_valid_yaml_and_runs_the_gate_in_the_backend_job(self) -> None:
        workflow_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["backend"]["steps"]
        run_commands = [step["run"] for step in steps if "run" in step]
        assert any("python -m app.evaluation.cli run --suite all" in cmd for cmd in run_commands), (
            "the backend CI job must run the evaluation gate"
        )
        assert any("pytest" in cmd for cmd in run_commands), "the gate runs after the test suite"
        pytest_index = next(i for i, cmd in enumerate(run_commands) if "pytest" in cmd)
        eval_index = next(
            i for i, cmd in enumerate(run_commands) if "python -m app.evaluation.cli" in cmd
        )
        assert eval_index > pytest_index, "the evaluation gate must run after the test suite"
