"""The evaluation runner (§15.2, §15.5, EVAL-002).

    for each case:
        reset mock_crm from the case's fixture set          (one transaction)
        pin Settings: rules planner, seed, budgets, no random failures
        compose the PRODUCTION graph through `build_driver`, with the real
            implementations wrapped by the FailureInjector
        RunService.create_run → start_run → Executor drives the graph
        while the run is awaiting_approval:
            ApprovalPolicy posts a real decision through ApprovalService
        read the run row, the checkpoint, tool_calls, approvals, trace
        evaluate the case's `expect`, then the seven §15.6 invariants over
            the persisted rows (`app.evaluation.invariants`) → CaseResult

Two rules from §15.5 hold by construction. The suite calls the same
services the HTTP API calls (`RunService`, `Executor`, `ApprovalService`)
over the same graph `wire_runtime` composes (`app.execution.runtime`), so
there is no evaluation-only execution path. `ApprovalPolicy` replaces the
human, never the gate: every decision is a real `approvals` row decided by
`ApprovalService`, and the token is minted by `execute_tool` from that row
as in production — nothing here can grant, skip or forge one.

The clock is a `FixedClock`: backoff is virtual (the `recover` node advances
it), so retries are asserted on and never waited for.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Final

from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.state import (
    TERMINAL_RUN_STATUSES,
    ApprovalDecisionKind,
    ApprovalStatus,
    PlannerKind,
    RunStatus,
)
from app.config import PlannerMode, Settings
from app.errors import ConfigurationError, RateLimitedError, TransientToolError
from app.evaluation.invariants import InvariantEvidence, InvariantOutcome, evaluate_invariants
from app.evaluation.metrics import (
    SuiteRunResult,
    calculate_agent_duration_ms,
    calculate_approval_wait_ms,
    compute_evaluation_metrics_from_results,
)
from app.evaluation.registry import EvaluationRegistry
from app.evaluation.schemas import (
    RUN_ID_PLACEHOLDER,
    ApprovalPolicyKind,
    ApprovalsSpec,
    CustomerFixture,
    DbAssertion,
    EvalCase,
    FailureInjection,
    FailureInjectionKind,
    FixtureDataset,
)
from app.execution.approvals import ApprovalService
from app.execution.executor import Executor
from app.execution.leases import LeaseConfig, new_worker_id
from app.execution.recovery import LangGraphRunDriver
from app.execution.runs import RunService
from app.execution.runtime import build_driver
from app.persistence.models import (
    AgentRun,
    ApprovalRow,
    EvaluationRunStatus,
    ToolCallRow,
    TraceEvent,
    TraceEventKind,
)
from app.persistence.protocols import UnitOfWorkFactory
from app.persistence.session import unit_of_work
from app.runtime import (
    Clock,
    FixedClock,
    IdGenerator,
    InMemoryCancellationSource,
    SystemClock,
    UuidIdGenerator,
)
from app.tools.contracts import ToolName
from app.tools.registry import (
    ToolContext,
    ToolImplementation,
    ToolTimeoutError,
    default_implementations,
)
from app.tools.schemas import SaveDraftOutput

__all__ = [
    "EVAL_EPOCH",
    "ApprovalPolicy",
    "AssertionOutcome",
    "CaseResult",
    "EvaluationRunner",
    "FailureInjector",
    "InvariantOutcome",
    "SuiteRunResult",
]

#: The frozen wall clock every case starts from (§15.2).
EVAL_EPOCH: Final = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
#: A run may pause once per gated step; more pauses than steps is a loop.
_MAX_PAUSES: Final = 25


# ---------------------------------------------------------------------------
# The scripted human
# ---------------------------------------------------------------------------
class ApprovalPolicy:
    """`approve | reject | approve_after(n) | never`, applied to the n-th
    pending approval of a run. It only *chooses*; `ApprovalService` decides."""

    def __init__(self, spec: ApprovalsSpec | None) -> None:
        self._spec = spec

    @property
    def reason(self) -> str | None:
        return self._spec.reason if self._spec is not None else None

    def decide(self, ordinal: int) -> ApprovalDecisionKind | None:
        """`None` leaves the request pending (the human never answers)."""
        spec = self._spec
        if spec is None or spec.policy is ApprovalPolicyKind.NEVER:
            return None
        if spec.policy is ApprovalPolicyKind.APPROVE:
            return ApprovalDecisionKind.APPROVE
        if spec.policy is ApprovalPolicyKind.REJECT:
            return ApprovalDecisionKind.REJECT
        after = spec.after or 1
        return ApprovalDecisionKind.APPROVE if ordinal >= after else ApprovalDecisionKind.REJECT


# ---------------------------------------------------------------------------
# Explicit failures, keyed by (tool, attempt)
# ---------------------------------------------------------------------------
class FailureInjector:
    """Wraps the real tool implementations so a scripted `(tool, attempt)`
    misbehaves instead of executing. It sits *inside* the dispatcher, after
    the gate and the stored-decision check, so a gated tool is only ever
    reached with a valid token whether it is about to lie or not."""

    def __init__(self, injections: Sequence[FailureInjection]) -> None:
        self._kinds: dict[tuple[ToolName, int | None], FailureInjectionKind] = {}
        for inj in injections:
            if (
                inj.kind is FailureInjectionKind.LYING_SUCCESS
                and inj.tool is not ToolName.SAVE_DRAFT
            ):
                raise ConfigurationError(
                    "lying_success is scripted for save_draft only",
                    detail={"tool": inj.tool.value},
                )
            attempts: list[int | None] = (
                [None] if isinstance(inj.attempts, str) else list(inj.attempts)
            )
            for attempt in attempts:
                self._kinds[(inj.tool, attempt)] = inj.kind

    def kind_for(self, tool: ToolName, attempt: int) -> FailureInjectionKind | None:
        return self._kinds.get((tool, attempt)) or self._kinds.get((tool, None))

    def wrap(
        self, implementations: Mapping[ToolName, ToolImplementation]
    ) -> dict[ToolName, ToolImplementation]:
        tools = {tool for tool, _ in self._kinds}
        return {
            name: self._wrapped(impl) if name in tools else impl
            for name, impl in implementations.items()
        }

    def _wrapped(self, impl: ToolImplementation) -> ToolImplementation:
        async def injected(validated: BaseModel, ctx: ToolContext) -> BaseModel:
            kind = self.kind_for(ctx.tool, ctx.attempt)
            detail = {"injected": kind.value if kind else None, "attempt": ctx.attempt}
            if kind is None:
                return await impl(validated, ctx)
            if kind is FailureInjectionKind.TRANSIENT:
                raise TransientToolError("injected transient failure", detail=detail)
            if kind is FailureInjectionKind.RATE_LIMITED:
                raise RateLimitedError("injected rate limit", detail=detail)
            if kind is FailureInjectionKind.TIMEOUT:
                raise ToolTimeoutError("injected timeout", detail=detail)
            # lying_success: a well-formed DraftRecord, and no row written.
            return SaveDraftOutput(
                draft_id=f"d_lie_{ctx.step_id}_{ctx.attempt}",
                version=1,
                status="saved",
                content_hash=getattr(validated, "content_hash", ""),
                saved_at=ctx.clock.now(),
            )

        return injected


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AssertionOutcome:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    run_id: uuid.UUID
    passed: bool
    assertions: tuple[AssertionOutcome, ...]
    duration_ms: int
    final_status: RunStatus
    status_reason: str | None
    tool_calls_count: int
    retry_count: int
    approval_outcome: str | None
    agent_duration_ms: int = 0
    approval_wait_ms: int = 0
    #: The seven §15.6 invariants (EVAL-004). `passed` is false if any failed,
    #: whatever `assertions` say.
    invariants: tuple[InvariantOutcome, ...] = ()

    @property
    def failures(self) -> tuple[AssertionOutcome, ...]:
        return tuple(a for a in self.assertions if not a.passed)

    @property
    def violations(self) -> tuple[InvariantOutcome, ...]:
        return tuple(i for i in self.invariants if not i.passed)


@dataclass(frozen=True)
class _Evidence:
    run: AgentRun
    state: dict[str, Any]
    tool_calls: list[ToolCallRow]
    approvals: list[ApprovalRow]
    events: list[TraceEvent]


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------
class EvaluationRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        checkpointer: BaseCheckpointSaver[Any],
        registry: EvaluationRegistry,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._uow_factory: UnitOfWorkFactory = partial(unit_of_work, session_factory)
        self._checkpointer = checkpointer
        self._registry = registry
        self._clock = clock or SystemClock()
        self._ids = ids or UuidIdGenerator()

    async def run_suite(
        self, suite: str, *, evaluation_run_id: uuid.UUID | None = None
    ) -> SuiteRunResult:
        cases = self._registry.suite_cases(suite)
        if evaluation_run_id is None:
            async with self._uow_factory() as uow:
                eval_run = await uow.evaluations.create_run(
                    suite=suite,
                    planner_kind=PlannerKind.RULES,
                )
                await uow.commit()
                evaluation_run_id = eval_run.id

        results: list[CaseResult] = []
        for case in cases:
            res = await self.run_case(case, evaluation_run_id=evaluation_run_id)
            results.append(res)

        metrics = compute_evaluation_metrics_from_results(cases, results)

        async with self._uow_factory() as uow:
            passed_count = sum(1 for r in results if r.passed)
            failed_count = len(results) - passed_count
            await uow.evaluations.complete_run(
                evaluation_run_id,
                status=EvaluationRunStatus.COMPLETED,
                finished_at=self._clock.now(),
                case_count=len(results),
                passed=passed_count,
                failed=failed_count,
                metrics=metrics,
            )
            await uow.commit()

        return SuiteRunResult(
            results,
            evaluation_run_id=evaluation_run_id,
            metrics=metrics,
        )

    async def run_case(
        self, case: EvalCase, *, evaluation_run_id: uuid.UUID | None = None
    ) -> CaseResult:
        started = time.monotonic()
        await self._reset_fixtures(self._registry.fixture_set(case.given.fixtures))

        settings = self._pinned_settings(case)
        clock = FixedClock(EVAL_EPOCH)
        cancellation = InMemoryCancellationSource()
        driver = build_driver(
            settings,
            session_factory=self._session_factory,
            uow_factory=self._uow_factory,
            checkpointer=self._checkpointer,
            clock=clock,
            ids=self._ids,
            cancellation_source=cancellation,
            implementations=FailureInjector(case.given.inject).wrap(default_implementations()),
        )
        lease = LeaseConfig.from_settings(settings)
        executor = Executor(
            uow_factory=self._uow_factory,
            driver=driver,
            clock=clock,
            lease=lease,
            owner=new_worker_id(self._ids, label="eval-executor"),
            budgets=settings.budgets,
        )
        runs = RunService(
            uow_factory=self._uow_factory,
            settings=settings,
            clock=clock,
            ids=self._ids,
            cancellation_source=cancellation,
            executor=executor,
        )
        approvals = ApprovalService(
            uow_factory=self._uow_factory, driver=driver, clock=clock, lease=lease, ids=self._ids
        )

        created = await runs.create_run(
            case.given.request,
            seed=case.given.seed,
            evaluation_run_id=evaluation_run_id,
            eval_case_id=case.id,
            planner_kind=PlannerKind.RULES,
        )
        run_id = created.run.id
        await runs.start_run(run_id)
        # `schedule` is idempotent: it hands back the task `start_run` created.
        await executor.schedule(run_id)

        outcomes: list[AssertionOutcome] = []
        policy = ApprovalPolicy(case.given.approvals)
        ordinal = 0
        run = await self._read_run(run_id)
        while run.status is RunStatus.AWAITING_APPROVAL and ordinal < _MAX_PAUSES:
            ordinal += 1
            pending = await self._pending_approval(run_id)
            if ordinal == 1 and case.expect.approval is not None:
                outcomes.extend(
                    await self._check_db(case.expect.approval.while_paused, run_id, "while_paused")
                )
            decision = policy.decide(ordinal)
            if decision is None:
                break
            await approvals.decide_approval(
                pending.id,
                decision=decision,
                decided_by=f"eval:{case.id}",
                reason=policy.reason,
            )
            run = await self._read_run(run_id)

        evidence = await self._collect(run, driver)
        duration_ms = int((time.monotonic() - started) * 1000)
        outcomes.extend(_evaluate(case, evidence, duration_ms))
        outcomes.extend(await self._check_db(case.expect.db, run_id, "db"))
        invariants = await self.check_invariants(case, run_id)
        approvals_rows = evidence.approvals

        approval_wait_ms = calculate_approval_wait_ms(evidence.approvals, run=evidence.run)
        effective_run_duration_ms = (
            evidence.run.duration_ms
            if evidence.run.duration_ms is not None and evidence.run.duration_ms > 0
            else duration_ms
        )
        agent_duration_ms = calculate_agent_duration_ms(effective_run_duration_ms, approval_wait_ms)
        passed = all(o.passed for o in outcomes) and all(i.passed for i in invariants)

        if evaluation_run_id is not None:
            failure_reason = None
            if not passed:
                # A violated invariant outranks a failed case assertion.
                failed: list[AssertionOutcome | InvariantOutcome] = [
                    *(i for i in invariants if not i.passed),
                    *(o for o in outcomes if not o.passed),
                ]
                failure_reason = failed[0].detail if failed else "assertion_failed"
            async with self._uow_factory() as uow:
                await uow.evaluations.record_result(
                    evaluation_run_id=evaluation_run_id,
                    case_id=case.id,
                    run_id=run_id,
                    passed=passed,
                    assertions=[
                        *(
                            {"name": a.name, "passed": a.passed, "detail": a.detail}
                            for a in outcomes
                        ),
                        *(
                            {
                                "name": f"invariant[{i.invariant}] {i.name}",
                                "passed": i.passed,
                                "detail": i.detail,
                                "invariant": i.invariant,
                                "evidence": i.evidence,
                            }
                            for i in invariants
                        ),
                    ],
                    duration_ms=agent_duration_ms,
                    retry_count=sum(_retry_counts(evidence.state).values()),
                    tool_calls_count=len(evidence.tool_calls),
                    approval_outcome=approvals_rows[-1].status.value if approvals_rows else None,
                    failure_reason=failure_reason,
                )
                await uow.commit()

        return CaseResult(
            case_id=case.id,
            run_id=run_id,
            passed=passed,
            assertions=tuple(outcomes),
            duration_ms=duration_ms,
            final_status=run.status,
            status_reason=run.status_reason,
            tool_calls_count=len(evidence.tool_calls),
            retry_count=sum(_retry_counts(evidence.state).values()),
            approval_outcome=approvals_rows[-1].status.value if approvals_rows else None,
            agent_duration_ms=agent_duration_ms,
            approval_wait_ms=approval_wait_ms,
            invariants=invariants,
        )

    async def check_invariants(
        self, case: EvalCase, run_id: uuid.UUID
    ) -> tuple[InvariantOutcome, ...]:
        """The seven §15.6 invariants over what `run_id` left in PostgreSQL
        (EVAL-004). Reads only persisted rows — never the checkpoint — so it
        can be re-run against any stored evaluation run."""
        budgets = case.given.budgets or self._settings.budgets
        # By table name, as `_reset_fixtures` does: fixture data, not a port.
        dataset = dict(self._registry.fixture_set(case.given.fixtures))
        seeded: Sequence[CustomerFixture] = dataset["customers"]
        run = await self._read_run(run_id)
        async with self._uow_factory() as uow:
            customers = [
                row
                for fixture in seeded
                if (row := await uow.customers.get(fixture.customer_id)) is not None
            ]
            evidence = InvariantEvidence(
                run=run,
                tool_calls=await uow.tool_calls.list_by_run(run_id),
                approvals=await uow.approvals.list_by_run(run_id),
                events=await uow.trace_events.list_by_run(run_id, limit=10_000),
                steps=await uow.execution_steps.list_by_run(run_id),
                outbox=await uow.email_outbox.list_by_run(str(run_id)),
                outbox_total=await uow.count_rows("mock_crm.email_outbox", {}),
                customer_rows=customers,
                customers_total=await uow.count_rows("mock_crm.customers", {}),
                seeded_customers=seeded,
                max_retries=budgets.max_retries,
                max_steps=budgets.max_steps,
                policy_violation_expected=case.expect.policy_violation_expected,
            )
            await uow.commit()
        return evaluate_invariants(evidence)

    # -- per-case plumbing --------------------------------------------------
    def _pinned_settings(self, case: EvalCase) -> Settings:
        budgets = case.given.budgets or self._settings.budgets
        return self._settings.model_copy(
            update={
                "planner": PlannerMode(case.given.planner.value),
                "seed": case.given.seed,
                "tool_failure_rate": 0.0,
                "max_retries": budgets.max_retries,
                "max_replans": budgets.max_replans,
                "max_steps": budgets.max_steps,
                "run_deadline_seconds": budgets.run_deadline_seconds,
            }
        )

    async def _reset_fixtures(self, dataset: FixtureDataset) -> None:
        # By table name, as `FixtureDataset` does: the fixture tables share
        # names with the mutating ports, and this is data, not a port.
        rows = {
            table: [_row(fixture) for fixture in getattr(dataset, table)]
            for table in ("companies", "leads", "customers")
        }
        async with self._uow_factory() as uow:
            await uow.reset_mock_crm(**rows)
            await uow.commit()

    async def _read_run(self, run_id: uuid.UUID) -> AgentRun:
        async with self._uow_factory() as uow:
            run = await uow.agent_runs.get(run_id)
            await uow.commit()
        if run is None:
            raise ConfigurationError("evaluation run vanished", detail={"run_id": str(run_id)})
        return run

    async def _pending_approval(self, run_id: uuid.UUID) -> ApprovalRow:
        async with self._uow_factory() as uow:
            rows = await uow.approvals.list_by_run(run_id)
            await uow.commit()
        pending = [r for r in rows if r.status is ApprovalStatus.PENDING]
        if len(pending) != 1:
            raise ConfigurationError(
                "a paused run must hold exactly one pending approval",
                detail={"run_id": str(run_id), "pending": len(pending)},
            )
        return pending[0]

    async def _collect(self, run: AgentRun, driver: LangGraphRunDriver) -> _Evidence:
        async with self._uow_factory() as uow:
            tool_calls = await uow.tool_calls.list_by_run(run.id)
            approvals = await uow.approvals.list_by_run(run.id)
            events = await uow.trace_events.list_by_run(run.id, limit=10_000)
            await uow.commit()
        return _Evidence(
            run=run,
            state=await driver.state(run.id),
            tool_calls=tool_calls,
            approvals=sorted(approvals, key=lambda r: (r.requested_at, str(r.id))),
            events=events,
        )

    async def _check_db(
        self, assertions: Sequence[DbAssertion], run_id: uuid.UUID, label: str
    ) -> list[AssertionOutcome]:
        outcomes = []
        async with self._uow_factory() as uow:
            for index, assertion in enumerate(assertions):
                where = {
                    k: str(run_id) if v == RUN_ID_PLACEHOLDER else v
                    for k, v in assertion.where.items()
                }
                count = await uow.count_rows(assertion.table, where)
                outcomes.append(
                    AssertionOutcome(
                        f"{label}[{index}] {assertion.table} {assertion.where}",
                        count == assertion.count,
                        f"expected {assertion.count} rows, found {count}",
                    )
                )
            await uow.commit()
        return outcomes


def _row(fixture: BaseModel) -> dict[str, Any]:
    row = fixture.model_dump()
    if "signals" in row:  # JSONB: the nested timestamps must be JSON, not datetime
        row["signals"] = fixture.model_dump(mode="json")["signals"]
    return row


# ---------------------------------------------------------------------------
# Assertions over the evidence (§15.3)
# ---------------------------------------------------------------------------
def _as_dict(value: object) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value) if isinstance(value, Mapping) else {}


def _retry_counts(state: Mapping[str, Any]) -> dict[str, int]:
    return {str(k): int(v) for k, v in (state.get("retry_count") or {}).items()}


def _lookup(data: object, path: str) -> tuple[bool, Any]:
    """`profile.confidence` → (found, value) over nested mappings."""
    current = data
    for key in path.split("."):
        if not isinstance(current, Mapping) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _evaluate(case: EvalCase, ev: _Evidence, duration_ms: int) -> list[AssertionOutcome]:
    expect = case.expect
    out: list[AssertionOutcome] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        out.append(AssertionOutcome(name, passed, detail))

    check("final_status", ev.run.status is expect.final_status, f"got {ev.run.status.value}")
    check(
        "status_reason",
        ev.run.status_reason == expect.status_reason,
        f"got {ev.run.status_reason!r}",
    )
    check("terminal", ev.run.status in TERMINAL_RUN_STATUSES, f"got {ev.run.status.value}")

    called: dict[ToolName, int] = {}
    for row in ev.tool_calls:
        tool = ToolName(str(row.tool))
        called[tool] = called.get(tool, 0) + 1
    for tool in expect.tools_called:
        check(f"tools_called[{tool.value}]", tool in called, f"dispatched {called.get(tool, 0)}x")
    for tool in expect.tools_not_called:
        check(
            f"tools_not_called[{tool.value}]",
            tool not in called,
            f"dispatched {called.get(tool, 0)}x",
        )
    for tool, count in expect.tool_call_counts.items():
        check(
            f"tool_call_counts[{tool.value}]",
            called.get(tool, 0) == count,
            f"got {called.get(tool, 0)}",
        )

    state_calls = [_as_dict(c) for c in ev.state.get("tool_calls") or []]
    if expect.tool_sequence is not None:
        sequence = [c["tool"] for c in state_calls if c.get("status") == "succeeded"]
        check(
            "tool_sequence",
            sequence == [t.value for t in expect.tool_sequence],
            f"got {sequence}",
        )

    plan = _as_dict(ev.state.get("plan"))
    steps = {s["step_id"]: s for s in plan.get("steps", [])}
    retries = _retry_counts(ev.state)
    verification = {k: _as_dict(v) for k, v in (ev.state.get("verification_result") or {}).items()}
    for exp in expect.steps:
        step = steps.get(exp.step_id)
        prefix = f"steps[{exp.step_id}]"
        if step is None:
            check(prefix, False, "not in the final plan")
            continue
        check(f"{prefix}.tool", step["tool"] == exp.tool.value, f"got {step['tool']}")
        if exp.status is not None:
            check(f"{prefix}.status", step["status"] == exp.status.value, f"got {step['status']}")
        attempts = sum(1 for c in state_calls if c["step_id"] == exp.step_id)
        if exp.attempts is not None:
            check(f"{prefix}.attempts", attempts == exp.attempts, f"got {attempts}")
        if exp.retry_count is not None:
            check(
                f"{prefix}.retry_count",
                retries.get(exp.step_id, 0) == exp.retry_count,
                f"got {retries.get(exp.step_id, 0)}",
            )
        if exp.verification_status is not None:
            got = (verification.get(exp.step_id) or {}).get("status")
            check(
                f"{prefix}.verification_status", got == exp.verification_status.value, f"got {got}"
            )
        if exp.backoff is not None:
            delays = [
                int(e.payload.get("delay_ms", -1))
                for e in ev.events
                if e.kind is TraceEventKind.RETRY_SCHEDULED and e.step_id == exp.step_id
            ]
            bounds = []
            for n, delay in enumerate(delays, start=1):
                nominal = min(exp.backoff.base_ms * 2 ** (n - 1), exp.backoff.max_ms)
                lo = int(nominal * exp.backoff.jitter_min)
                hi = int(nominal * exp.backoff.jitter_max)
                bounds.append(lo <= delay <= hi)
            check(
                f"{prefix}.backoff",
                len(delays) == attempts - 1 and all(bounds),
                f"delays {delays} for {attempts} attempts",
            )

    if expect.plan is not None:
        top_level = [s for s in plan.get("steps", []) if "[" not in s["step_id"]]
        check(
            "plan.revision",
            plan.get("revision") == expect.plan.revision,
            f"got {plan.get('revision')}",
        )
        check(
            "plan.steps",
            [(s["step_id"], s["tool"]) for s in top_level]
            == [(s.step_id, s.tool.value) for s in expect.plan.steps],
            f"got {[(s['step_id'], s['tool']) for s in top_level]}",
        )
        for exp_step in expect.plan.steps:
            if exp_step.args is not None:
                got_args = (steps.get(exp_step.step_id) or {}).get("args")
                check(
                    f"plan.steps[{exp_step.step_id}].args",
                    got_args == exp_step.args,
                    f"got {got_args}",
                )

    if expect.approval is not None:
        ea = expect.approval
        by_status = {s: sum(1 for r in ev.approvals if r.status is s) for s in ApprovalStatus}
        check("approval.requested", len(ev.approvals) == ea.requested, f"got {len(ev.approvals)}")
        check(
            "approval.approved",
            by_status[ApprovalStatus.APPROVED] == ea.approved,
            f"got {by_status}",
        )
        check(
            "approval.rejected",
            by_status[ApprovalStatus.REJECTED] == ea.rejected,
            f"got {by_status}",
        )
        if ev.approvals:
            first = ev.approvals[0]
            if ea.tool is not None:
                check("approval.tool", str(first.tool) == ea.tool.value, f"got {first.tool}")
            for key in ea.preview_contains:
                check(
                    f"approval.preview_contains[{key}]",
                    key in first.payload_preview and first.payload_preview[key] not in (None, ""),
                    f"preview keys {sorted(first.payload_preview)}",
                )

    results = {k: _as_dict(v) for k, v in (ev.state.get("tool_results") or {}).items()}
    for index, exp_out in enumerate(expect.tool_outputs):
        outputs = [
            r["output"]
            for sid, r in results.items()
            if r.get("tool") == exp_out.tool.value and (exp_out.step_id in (None, sid))
        ]
        prefix = f"tool_outputs[{index}] {exp_out.tool.value}"
        check(prefix, bool(outputs), f"{len(outputs)} result(s) for this tool/step")
        for output in outputs:
            for field_path in exp_out.required_fields:
                found, _ = _lookup(output, field_path)
                check(f"{prefix}.required[{field_path}]", found, "present" if found else "missing")
            for field_path, span in exp_out.ranges.items():
                found, value = _lookup(output, field_path)
                ok = found and isinstance(value, int | float) and span[0] <= value <= span[1]
                check(f"{prefix}.range[{field_path}]", ok, f"got {value!r}")
            for field_path, expected in exp_out.equals.items():
                found, value = _lookup(output, field_path)
                check(
                    f"{prefix}.equals[{field_path}]", found and value == expected, f"got {value!r}"
                )

    if expect.ranking is not None:
        scored = [
            r["output"] for r in results.values() if r.get("tool") == ToolName.SCORE_LEAD.value
        ]
        ordered = sorted(scored, key=lambda o: (-int(o["score"]), str(o["lead_id"])))
        order = [o["lead_id"] for o in ordered]
        check("ranking.order", order == expect.ranking.expected_order, f"got {order}")
        scores = {o["lead_id"]: int(o["score"]) for o in ordered}
        for lead_id, score in expect.ranking.expected_scores.items():
            check(
                f"ranking.score[{lead_id}]",
                scores.get(lead_id) == score,
                f"got {scores.get(lead_id)}",
            )
        if expect.ranking.factors_sum_to_score:
            for o in ordered:
                total = sum(float(f["contribution"]) for f in o.get("factors", []))
                check(
                    f"ranking.factors_sum[{o['lead_id']}]",
                    abs(total - int(o["score"])) <= expect.ranking.tolerance,
                    f"sum {total} vs score {o['score']}",
                )

    response = _as_dict(ev.run.final_response) or _as_dict(ev.state.get("final_response"))
    text = str(response)
    for phrase in expect.response_mentions:
        check(f"response_mentions[{phrase}]", phrase in text, "absent from the final response")
    for phrase in expect.response_not_mentions:
        check(
            f"response_not_mentions[{phrase}]", phrase not in text, "present in the final response"
        )

    check("max_duration_ms", duration_ms <= expect.max_duration_ms, f"took {duration_ms} ms")
    violations = sum(1 for e in ev.events if e.kind is TraceEventKind.POLICY_VIOLATION)
    check(
        "policy_violation",
        (violations > 0) == expect.policy_violation_expected,
        f"{violations} policy_violation event(s)",
    )
    return out
