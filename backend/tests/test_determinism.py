"""TEST-005: the same case, run twice, yields an identical trace (§15.2).

    "with `OPSPILOT_PLANNER=rules`, a fixed seed and a frozen clock, a run
    is byte-reproducible" (§1.4) ... "running the same case twice produces
    an identical trace apart from wall-clock timestamps" (§15.2).

This drives `happy_path_multi_step` — the canonical request (§1.1): a
fan-out, a human approval, a mock send and two independent read-back
verifications — through the real `EvaluationRunner` twice, exactly as
EVAL-002 already does in `test_evaluation_runner.py::
test_fixtures_are_reset_before_every_case`. No new execution path is
introduced (§18.1/EVAL-002's two rules): `EvaluationRunner.run_case` resets
`mock_crm` from the same fixtures, pins the same seed/budgets/planner, and
drives the same `RunService` -> `Executor` -> `ApprovalService` over the
same production graph each time, exactly as it does for every other
evaluation case. The two runs share only read-only infrastructure (the
engine, the checkpointer, the fixture/case definitions); nothing that
carries state between one run and the next is reused (a fresh `FixedClock`,
`InMemoryCancellationSource` and graph composition per `run_case` call —
see `app/evaluation/runner.py::EvaluationRunner.run_case`), and each run
gets its own real, independently-generated `run_id` (EVAL-002's note: a
seeded id generator would collide across independent runs' durable rows,
so ids stay random — see `docs/tasks.md` EVAL-002 and requirement 9).

The comparison reads only durable PostgreSQL evidence — `trace_events`,
`tool_calls`, `approvals` — never the checkpoint, and normalizes exactly
two categories of field before comparing (see the row-dict builders and
`_IDENTITY_KEYS` below for the precise list and the reason each one is
there): wall-clock/virtual-clock timestamps and row `id`/`duration_ms`
columns dropped outright, and the handful of values a fresh `UuidIdGenerator`/
`gen_random_uuid()` mints anew every run (`run_id`, row surrogate keys,
`draft_id`/`message_id`/`outbox_id`, and the `args_hash`/`idempotency_key`
hashes derived from them). Everything else — event kind, severity,
sequence, node/tool/step identity, attempt numbers, resolved tool
arguments and outputs, retry counts, approval decisions and reasons,
verification status, and ordering — is compared byte-for-byte.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

import pytest
from app.evaluation import load_registry
from app.evaluation.registry import EvaluationRegistry
from app.evaluation.runner import CaseResult, EvaluationRunner
from app.persistence.checkpointing import open_checkpointer
from app.persistence.models import ApprovalRow, ToolCallRow, TraceEvent
from app.persistence.session import create_session_factory, unit_of_work
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import migrate_to_head, require_database, settings


# ---------------------------------------------------------------------------
# Fixtures: the same real-Postgres, real-saver, real-graph composition
# `test_evaluation_runner.py` uses for every other EVAL-002 integration test
# (EVAL-002/TEST-005 requirement 11: no second execution path).
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def _database() -> None:
    require_database()
    migrate_to_head()


@pytest.fixture(scope="module")
def registry() -> EvaluationRegistry:
    return load_registry()


@pytest.fixture
async def engine(_database: None) -> AsyncIterator[AsyncEngine]:
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


@pytest.fixture
async def checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    async with open_checkpointer(settings()) as saver:
        yield saver


@pytest.fixture
def runner(
    engine: AsyncEngine, checkpointer: AsyncPostgresSaver, registry: EvaluationRegistry
) -> EvaluationRunner:
    return EvaluationRunner(
        settings=settings(),
        session_factory=create_session_factory(engine),
        checkpointer=checkpointer,
        registry=registry,
    )


# ---------------------------------------------------------------------------
# What gets normalized, and why (requirement 7: explicit and minimal).
# ---------------------------------------------------------------------------
#
# 1. Timestamps. `trace_events.ts` is `server_default=now()` (Postgres wall
#    time — see `app/persistence/trace_events.append_trace_event`, which
#    never passes `ts` itself), so it is never reproducible. The other rows'
#    `*_at` columns are set from the injected `Clock` and are already
#    identical run-to-run under `EvaluationRunner`'s `FixedClock` (§15.2) —
#    but the acceptance criterion is "identical trace modulo timestamps",
#    so every wall/virtual-clock column is excluded uniformly rather than
#    relying on that incidental fact.
#
# 2. Durations. `duration_ms` (`trace_events`, `tool_calls`) is elapsed
#    virtual time; explicitly named as normalizable by the acceptance
#    criterion.
#
# 3. Run-scoped random identity strings. A fresh `UuidIdGenerator` mints a
#    new value every run for `run_id` (`app/execution/runs.py`), and
#    Postgres's `gen_random_uuid()` does the same for every row's own `id`
#    (`app/persistence/models.py`) and for `draft_id`/`message_id`/
#    `outbox_id` (`app/integrations/mock/adapters.py:380,501-502`). Neither
#    is influenced by the seed (EVAL-002: a seeded id generator would
#    collide with other durable rows across independent runs — requirement
#    9 — so this is deliberate, not a gap). `args_hash` and
#    `idempotency_key` (`app/security.py`) are pure functions of these
#    values (idempotency_key embeds `run_id` and `args_hash`; `args_hash`
#    for `send_email_mock` embeds `draft_id`), so they too differ between
#    runs by construction even though nothing about the *decision* they
#    encode changed. Each occurrence of one of these values — including
#    where it is quoted inside free text, e.g. the approval `summary` built
#    by `app/agent/preview.py` ("Send saved draft (<draft_id>) to ...") —
#    is replaced by a placeholder assigned in first-appearance order within
#    each run, so a value's *identity and reuse pattern* (the same
#    `draft_id` produced by `save_draft`'s output and consumed by
#    `send_email_mock`'s input, the same `idempotency_key` across retried
#    attempts of one step) is preserved and compared, without pinning it to
#    a literal random string that was never meant to be stable.
#
# Nothing else is touched: `run_id`'s own row-primary-key column, business
# timestamps embedded inside tool outputs (`retrieved_at`, `saved_at`,
# `sent_at`, ...), fixture-stable business ids (`lead_id`, `company_id`,
# `customer_id`), tool arguments/outputs, step ids, retry counts, approval
# decisions/reasons and verification status are all real content and are
# compared exactly.

_IDENTITY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "run_id",
        "approval_id",
        "tool_call_id",
        "execution_step_id",
        "superseded_by",
        "draft_id",
        "message_id",
        "outbox_id",
        "idempotency_key",
        "args_hash",
    }
)


def _event_dict(e: TraceEvent) -> dict[str, Any]:
    """`TraceEvent` as a plain dict, minus `id` (global bigserial storage
    position, not per-run trace identity — `seq` is) and `ts`/`duration_ms`
    (see module docstring, points 1-2)."""
    return {
        "seq": e.seq,
        "kind": e.kind.value,
        "severity": e.severity.value,
        "node": e.node,
        "tool": str(e.tool) if e.tool is not None else None,
        "step_id": e.step_id,
        "attempt": e.attempt,
        "input": e.input,
        "output": e.output,
        "status": e.status,
        "retry_count": e.retry_count,
        "error": e.error,
        "payload": e.payload,
        "run_id": str(e.run_id),
    }


def _tool_call_dict(c: ToolCallRow) -> dict[str, Any]:
    """`ToolCallRow` as a plain dict, minus `id`/`started_at`/`finished_at`/
    `duration_ms` (see module docstring, points 1-2)."""
    return {
        "run_id": str(c.run_id),
        "execution_step_id": str(c.execution_step_id),
        "step_id": c.step_id,
        "attempt": c.attempt,
        "tool": str(c.tool),
        "tool_version": c.tool_version,
        "input": c.input,
        "output": c.output,
        "input_hash": c.input_hash,
        "status": c.status.value,
        "error_class": c.error_class,
        "error_message": c.error_message,
        "idempotency_key": c.idempotency_key,
        "port": c.port,
        "adapter": c.adapter,
    }


def _approval_dict(a: ApprovalRow) -> dict[str, Any]:
    """`ApprovalRow` as a plain dict, minus `id`/`requested_at`/
    `expires_at`/`decided_at` (see module docstring, point 1)."""
    return {
        "run_id": str(a.run_id),
        "step_id": a.step_id,
        "tool": str(a.tool),
        "risk": a.risk.value,
        "title": a.title,
        "summary": a.summary,
        "payload_preview": a.payload_preview,
        "args_hash": a.args_hash,
        "status": a.status.value,
        "superseded_by": str(a.superseded_by) if a.superseded_by is not None else None,
        "decided_by": a.decided_by,
        "decision_reason": a.decision_reason,
    }


def _harvest(
    value: Any, key: str | None, aliases: dict[str, str], counters: dict[str, int]
) -> None:
    """Pass 1: register every value seen under an `_IDENTITY_KEYS` key, in
    first-appearance order, under a placeholder named after that key."""
    if isinstance(value, dict):
        for k, v in value.items():
            _harvest(v, k, aliases, counters)
        return
    if isinstance(value, list):
        for v in value:
            _harvest(v, key, aliases, counters)
        return
    if key in _IDENTITY_KEYS and isinstance(value, str) and value not in aliases:
        counters[key] = counters.get(key, 0) + 1
        aliases[value] = f"<{key}#{counters[key]}>"


def _substitute(text: str, aliases: dict[str, str]) -> str:
    """Replace every known random value inside `text`, including a value
    quoted mid-sentence (e.g. an approval summary), not only an exact
    field-value match. Longest values first so one id can never be a
    partial match inside another."""
    if text in aliases:
        return aliases[text]
    for raw in sorted(aliases, key=len, reverse=True):
        if raw and raw in text:
            text = text.replace(raw, aliases[raw])
    return text


def _rewrite(value: Any, aliases: dict[str, str]) -> Any:
    """Pass 2: apply the alias table built by `_harvest` everywhere a known
    value appears, standalone or embedded in a larger string."""
    if isinstance(value, dict):
        return {k: _rewrite(v, aliases) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite(v, aliases) for v in value]
    if isinstance(value, str):
        return _substitute(value, aliases)
    return value


@dataclass(frozen=True)
class _NormalizedRun:
    events: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    approvals: list[dict[str, Any]]


def _normalize_run(
    events: Sequence[TraceEvent],
    tool_calls: Sequence[ToolCallRow],
    approvals: Sequence[ApprovalRow],
) -> _NormalizedRun:
    raw_events = [_event_dict(e) for e in events]  # already seq-ordered
    sorted_calls = sorted(tool_calls, key=lambda c: (c.step_id, c.attempt))
    raw_calls = [_tool_call_dict(c) for c in sorted_calls]
    raw_approvals = [_approval_dict(a) for a in approvals]  # already requested_at-ordered

    aliases: dict[str, str] = {}
    counters: dict[str, int] = {}
    for collection in (raw_events, raw_calls, raw_approvals):
        for row in collection:
            _harvest(row, None, aliases, counters)

    return _NormalizedRun(
        events=[_rewrite(row, aliases) for row in raw_events],
        tool_calls=[_rewrite(row, aliases) for row in raw_calls],
        approvals=[_rewrite(row, aliases) for row in raw_approvals],
    )


def _first_difference(label: str, a: list[dict[str, Any]], b: list[dict[str, Any]]) -> str:
    if len(a) != len(b):
        return f"{label}: count differs, {len(a)} vs {len(b)}"
    for i, (x, y) in enumerate(zip(a, b, strict=True)):
        if x != y:
            diff_keys = sorted(k for k in x if x.get(k) != y.get(k))
            return f"{label}: first difference at index {i}, keys {diff_keys}\n  a={x}\n  b={y}"
    return f"{label}: no difference"


def _failures(result: CaseResult) -> list[str]:
    return [f"{a.name}: {a.detail}" for a in result.failures]


@pytest.mark.integration
class TestDeterminism:
    """§15.2's determinism claim, over real Postgres evidence."""

    @pytest.mark.critical
    async def test_the_same_case_run_twice_yields_an_identical_trace(
        self, runner: EvaluationRunner, registry: EvaluationRegistry, engine: AsyncEngine
    ) -> None:
        case = registry.case("happy_path_multi_step")

        first = await runner.run_case(case)
        second = await runner.run_case(case)
        assert first.passed, _failures(first)
        assert second.passed, _failures(second)
        # Independent runs, not the same row replayed twice.
        assert first.run_id != second.run_id

        async with unit_of_work(create_session_factory(engine)) as uow:
            events_a = await uow.trace_events.list_by_run(first.run_id, limit=10_000)
            events_b = await uow.trace_events.list_by_run(second.run_id, limit=10_000)
            calls_a = await uow.tool_calls.list_by_run(first.run_id)
            calls_b = await uow.tool_calls.list_by_run(second.run_id)
            approvals_a = await uow.approvals.list_by_run(first.run_id)
            approvals_b = await uow.approvals.list_by_run(second.run_id)
            await uow.commit()

        # -- raw, un-normalized facts: no field here is ever expected to
        # differ between two runs of the same case, so these are asserted
        # directly against the ORM rows, before any normalization at all.
        assert len(events_a) == len(events_b) > 0
        assert [e.seq for e in events_a] == list(range(1, len(events_a) + 1))
        assert [e.seq for e in events_b] == list(range(1, len(events_b) + 1))
        assert [e.kind.value for e in events_a] == [e.kind.value for e in events_b]
        assert [e.severity.value for e in events_a] == [e.severity.value for e in events_b]
        assert [e.node for e in events_a] == [e.node for e in events_b]
        assert [str(e.tool) if e.tool else None for e in events_a] == [
            str(e.tool) if e.tool else None for e in events_b
        ]
        assert [e.step_id for e in events_a] == [e.step_id for e in events_b]
        assert [e.attempt for e in events_a] == [e.attempt for e in events_b]
        assert [e.status for e in events_a] == [e.status for e in events_b]
        assert [e.retry_count for e in events_a] == [e.retry_count for e in events_b]

        assert len(calls_a) == len(calls_b) == 10  # §15.3: "the longest case"
        assert len(approvals_a) == len(approvals_b) == 1

        # -- the full, structural comparison after normalizing exactly the
        # fields documented at the top of this module.
        norm_a = _normalize_run(events_a, calls_a, approvals_a)
        norm_b = _normalize_run(events_b, calls_b, approvals_b)

        assert norm_a.events == norm_b.events, _first_difference(
            "trace_events", norm_a.events, norm_b.events
        )
        assert norm_a.tool_calls == norm_b.tool_calls, _first_difference(
            "tool_calls", norm_a.tool_calls, norm_b.tool_calls
        )
        assert norm_a.approvals == norm_b.approvals, _first_difference(
            "approvals", norm_a.approvals, norm_b.approvals
        )

        # The normalization actually did something -- otherwise this test
        # would be indistinguishable from a (weaker) literal-equality test
        # that happens to pass by accident. Every run-scoped random field
        # this test set out to normalize really did differ, literally.
        assert first.run_id != second.run_id
        raw_draft_ids_a = {
            e.output["draft_id"] for e in events_a if e.output and "draft_id" in e.output
        }
        raw_draft_ids_b = {
            e.output["draft_id"] for e in events_b if e.output and "draft_id" in e.output
        }
        assert raw_draft_ids_a and raw_draft_ids_b and raw_draft_ids_a.isdisjoint(raw_draft_ids_b)
        assert approvals_a[0].id != approvals_b[0].id
        assert approvals_a[0].args_hash != approvals_b[0].args_hash

    async def test_normalization_helpers_do_not_touch_business_content(self) -> None:
        """Unit-level pin on `_harvest`/`_rewrite`: a fixture-stable business
        id (e.g. `lead_id`) is untouched, an `_IDENTITY_KEYS` value reused
        across two events collapses to one alias, and the same value quoted
        inside free text is still replaced."""
        run_id = str(uuid.uuid4())
        draft_id = f"drf_{uuid.uuid4().hex}"
        rows = [
            {"run_id": run_id, "step_id": "s7", "output": {"draft_id": draft_id, "version": 1}},
            {
                "run_id": run_id,
                "step_id": "s8",
                "input": {"draft_id": draft_id, "lead_id": "L-201"},
                "summary": f"Send saved draft ({draft_id}) to priya@ledgerline.example",
            },
        ]
        aliases: dict[str, str] = {}
        counters: dict[str, int] = {}
        for row in rows:
            _harvest(row, None, aliases, counters)
        rewritten = [_rewrite(row, aliases) for row in rows]

        assert rewritten[0]["output"]["draft_id"] == "<draft_id#1>"
        assert rewritten[1]["input"]["draft_id"] == "<draft_id#1>"
        assert rewritten[0]["run_id"] == rewritten[1]["run_id"] == "<run_id#1>"
        assert rewritten[1]["input"]["lead_id"] == "L-201"  # untouched: not an identity key
        assert rewritten[1]["summary"] == (
            "Send saved draft (<draft_id#1>) to priya@ledgerline.example"
        )
