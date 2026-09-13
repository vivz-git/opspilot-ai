# Implementation Backlog

Prioritized work for the implementation phase. `docs/architecture.md` is the
specification; this file is the order to build it in.

**Status legend** — `done` · `next` · `todo` · `blocked`
**Model class** — `SONNET` for specified work, `OPUS` where a wrong decision is
expensive to unwind. See [Model allocation](#model-allocation).

---

## Critical path

Build in this order. Each phase is shippable and demonstrable on its own.

```
P0  FOUND-001 ▸ DB-001..004 ▸ TOOL-001 ▸ TOOL-002/003      backend runs, tools work
P1  AGENT-002..008 ▸ OBS-001                                agent completes a run end to end
P2  HITL-001..005 ▸ VERIFY-001..003                         the safety story is real
P3  API-001..007                                            the contract is callable
P4  FE-001..008                                             the dashboard exists
P5  EVAL-001..005 ▸ TEST-002..006                           behaviour is measured
P6  OBS-004/005, DOC-*                                      hardening and polish
```

The dependency that matters most: **HITL and VERIFICATION land before the API
and the UI.** Building the dashboard first would produce a demo that looks
finished while the properties the product claims are unimplemented.

---

## FOUNDATION

| ID | Task | Deps | Model | Acceptance criteria |
|---|---|---|---|---|
| FOUND-001 | FastAPI app factory: settings wiring, structlog JSON logging, `/healthz`, `/readyz`, CORS from config, `validate_runtime()` at startup | — | SONNET | **done** — `app/main.py`, `app/logging_config.py`, `app/api/health.py`, `tests/test_health.py`. `/readyz`'s Alembic-head check is written generically (`expected_head` from `discover_alembic_head`) and returns `None` until FOUND-003 adds `alembic.ini`; until then it enforces DB reachability only, as designed |
| FOUND-002 | Dependency lockfile and reproducible install (`uv` or `pip-tools`); CI installs from the lock | FOUND-001 | SONNET | **done** — `backend/uv.lock` (86 packages, Python 3.12), `backend/.python-version`. CI (`astral-sh/setup-uv@v5`, pinned `0.11.19`), the `Dockerfile`, and every `Makefile` target now run `uv sync --locked` / `uv run`, replacing bare `pip install -e`. `[tool.uv] link-mode = "copy"` avoids a hardlink failure on cloud-synced folders (e.g. OneDrive). Fixed pre-existing `ruff check`/`ruff format --check` violations across the repo (import order, line length, two narrow `noqa`s) uncovered while verifying `make check` is actually green end to end — mechanical only, no behaviour change |
| FOUND-003 | Alembic init with the three schemas (`opspilot`, `mock_crm`, and `langgraph` left to its saver) | FOUND-001 | SONNET | **done** — `backend/alembic.ini`, `backend/alembic/env.py` (async, URL sourced from `Settings` — §17.1), first revision `19463f144188` creates `opspilot`+`mock_crm` via `CREATE SCHEMA IF NOT EXISTS`; `langgraph` untouched. All three acceptance behaviours verified against a real Postgres 16 container and encoded as `tests/test_migrations.py` (marked `integration`, skips cleanly with no reachable database). `/readyz` (FOUND-001) now enforces the real head automatically — confirmed live: `GET /readyz` → `{"status": "ready", "revision": "19463f144188"}` |
| FOUND-004 | `Clock`, `IdGenerator` and `SeededRandom` protocols with real and fake implementations, injected everywhere | FOUND-001 | SONNET | **done** — `app/runtime.py`, `tests/test_runtime.py`; `tests/test_structure.py::test_only_runtime_generates_time_ids_and_randomness` is the structural check. `SeededRandom` has one implementation (`DeterministicRandom`), not a real/fake pair — determinism comes from the seed, not from being a double (documented in the module). Nothing yet calls these (no consumer exists before AGENT-002+/TOOL-003/DB-001); this task lands the primitives so later tasks inject them from day one instead of retrofitting |
| FOUND-005 | CI green on the real matrix: ruff, ruff format, mypy strict, pytest with Postgres, gitleaks | FOUND-002 | SONNET | All five jobs pass on a PR; `mypy app` is clean with `strict = true` |

## DATABASE

| ID | Task | Deps | Model | Acceptance criteria |
|---|---|---|---|---|
| DB-001 | Control-plane models: `agent_runs`, `execution_steps`, `tool_calls`, `approvals` with every column, index and enum in §12.3–§12.6 | FOUND-003 | SONNET | **done** — `app/persistence/base.py`, `app/persistence/models.py`; migration `c6d1db7aa718` (on top of `19463f144188`). Every column, nullability, default, FK, unique/partial index in §12.3–§12.6 implemented; the partial unique index on `approvals(run_id, step_id) WHERE status='pending'` and `unique(execution_step_id, attempt)` both verified against real Postgres, including the positive case (a second pending approval is allowed once the first is decided). `tests/test_persistence_models.py` (19 tests: creation, FK enforcement, cascade delete, all six enum `CHECK` constraints, every named index, both duplicate-rejection constraints) plus three new tests in `tests/test_migrations.py` for this revision's upgrade/downgrade/re-upgrade determinism |
| DB-002 | `trace_events` with per-run monotonic `seq` and `unique(run_id, seq)` | DB-001 | SONNET | **done** — `TraceEvent`/`TraceEventKind`/`TraceEventSeverity` in `app/persistence/models.py`; migration `21765d8fa136` (`Revises: c6d1db7aa718`). Every column, enum vocabulary (§14.2) and index in §12.7 implemented, including the BRIN index on `ts`. Sequence allocation is a transaction-scoped `pg_advisory_xact_lock(run_id)` around a `MAX(seq)+1` read-then-insert (`app/persistence/trace_events.append_trace_event`), with `UNIQUE(run_id, seq)` as the database backstop, not the sole mechanism. Verified against real Postgres with 25 concurrent threads on one run: exactly `1..25`, no duplicates, no gaps (`tests/test_trace_events.py::TestConcurrentSequenceAllocation`), plus a two-run non-interference test and an explicit-duplicate-under-concurrency rejection test. 18 new tests in `tests/test_trace_events.py`, 4 new migration tests in `tests/test_migrations.py` |
| DB-003 | `evaluation_runs`, `evaluation_results` with `git_sha`/`prompt_version`/`seed` | DB-001 | SONNET | **done** — `EvaluationRun`/`EvaluationRunStatus`/`EvaluationResult` in `app/persistence/models.py` (§12.8); migration `62fe5fff7640` (`Revises: 21765d8fa136`). Every column, nullability, default and index in §12.8 implemented, including `unique(evaluation_run_id, case_id)` and `(case_id, passed)`. `evaluation_results.run_id` links to a real `agent_runs.id`; deleting `evaluation_runs` cascades to its results (§12.8's explicit `ON DELETE CASCADE`) — verified against real Postgres. The migration also adds the FK `agent_runs.evaluation_run_id → evaluation_runs.id` that `c6d1db7aa718` (DB-001) deliberately left off since this table did not exist yet; upgrade/no-op/downgrade/re-upgrade all verified live. Also fixes a real `alembic/env.py` schema-drift bug this task's second cross-schema FK surfaced (a Postgres role named `opspilot` colliding with the `opspilot` schema's default `search_path` resolution, misidentifying every existing FK as removed-and-re-added) via `include_schemas=True` plus pinning the migration connection's `search_path` to `public` — `alembic check` is now genuinely clean, guarded by `tests/test_migrations.py::TestNoAutogenerateDrift`. 27 new tests: `tests/test_evaluation_models.py` (21) and `tests/test_migrations.py` (6) |
| DB-004 | `mock_crm` models: `companies`, `leads`, `customers` (with `version`), `outreach_drafts`, `email_outbox` (with `UNIQUE(idempotency_key)`, `run_id`, `approval_id`) | FOUND-003 | SONNET | **done** — `app/persistence/mock_crm.py`, `app/persistence/base.py` (`MOCK_CRM_SCHEMA`), migration `e35beebb06d0` (`Revises: 62fe5fff7640`). Every column, nullability, default, FK, unique constraint, index and status enum in §12.9 implemented. `email_outbox.idempotency_key UNIQUE` database constraint verified live against real Postgres (duplicate key raises `IntegrityError`), `customers.version` defaults to 1 and increments for optimistic concurrency. 30 new tests in `tests/test_mock_crm_models.py` (creation, relationships, invalid FK rejection, cascade/restrict deletion behavior, all enum `CHECK` constraints, unique constraints, and index reflection), plus 4 new tests in `tests/test_migrations.py` for upgrade/idempotence/downgrade/re-upgrade determinism; `alembic check` clean |
| DB-005 | Async repositories per aggregate; no ORM session leaks outside them | DB-001..004 | SONNET | **done** — `app/persistence/protocols.py` (runtime-checkable protocols for all 11 domain aggregates and `UnitOfWork`), `app/persistence/repositories.py` (SQLAlchemy implementations encapsulating all `select()`, updates, advisory locks, and optimistic concurrency version checks), `app/persistence/session.py` (`create_session_factory`, `unit_of_work`). Transaction boundaries are explicit: exiting without commit rolls back; zero ORM sessions leak outside persistence. Structural test `tests/test_structure.py::test_no_orm_query_construction_outside_persistence` enforces no `select()` or query building outside `app/persistence/`. 16 new tests in `tests/test_repositories.py` verifying protocols, lifecycle CRUD, missing record semantics, atomic approval decisions, customer optimistic concurrency, concurrent gapless trace event sequence allocation, rollback on error, and rollback on uncommitted exit |
| DB-006 | Constraint tests against real Postgres for every safety-relevant index | DB-001..004 | SONNET | **done** — `tests/test_database_invariants.py` (34 tests). Verified at PostgreSQL level with real connections and zero ORM pre-checks: partial unique index `uq_approvals_run_id_step_id_pending` (second pending rejected with `IntegrityError`, decided permits new pending, 10-tx race), outbox idempotency `uq_email_outbox_idempotency_key` (10-tx race), trace seq `uq_trace_events_run_id_seq` (10-tx race + 20-tx advisory lock gapless allocator), agent run idempotency `uq_agent_runs_idempotency_key` (10-tx race), step revisions `uq_execution_steps_run_step_revision`, tool call attempts `uq_tool_calls_execution_step_id_attempt` (10-tx race), evaluation results `uq_evaluation_results_evaluation_run_id_case_id` (10-tx race), mock CRM uniqueness (5-tx company domain and 5-tx customer email races), customer optimistic concurrency (10-tx version update race), foreign keys (ON DELETE CASCADE and RESTRICT / NO ACTION), and 13 enum CHECK constraints rejecting raw out-of-vocabulary SQL. All catalog indexes verified in `pg_indexes`. Full test suite 400 passed, 1 skipped. |
| DB-007 | LangGraph Postgres checkpointer wiring, lease heartbeat, and the startup reconciler for orphaned runs | DB-001, AGENT-002 | **OPUS** | A run killed mid-execution is either re-entered from its checkpoint or marked `failed(orphaned)` on restart; a run killed while `awaiting_approval` still resumes correctly after approval; test kills the task between checkpoint and row write |

## TOOLS

| ID | Task | Deps | Model | Acceptance criteria |
|---|---|---|---|---|
| TOOL-001 | `integrations/ports.py` Protocols (mutating methods take `ApprovalToken`), mock adapters, and the seed fixture dataset using RFC 2606 domains | DB-004 | SONNET | `make seed` loads a deterministic dataset; `test_structure.py::test_mock_integrations_cannot_reach_the_network` passes (no longer skipped); every fixture address is `*.example`/`example.com` |
| TOOL-002 | `ToolRegistry.dispatch`: input validation → **approval gate re-assertion** → idempotency key → timeout → dispatch → output validation → trace, as the single choke point | TOOL-001, OBS-001 | **OPUS** | A gated tool dispatched with no grant raises `PolicyViolation`; no node can reach a tool implementation directly (structural test); every attempt produces exactly one `tool_calls` row and a `tool_started`/`tool_*` trace pair |
| TOOL-003 | The nine tool implementations over ports, each honouring its declared failure modes | TOOL-002 | SONNET | Every `failure_mode` in the registry is reachable in a test; `send_email_mock` rejects a recipient that is not the owning lead's address with `POLICY_VIOLATION` |
| TOOL-004 | `$ref` resolver: dotted keys and numeric indices only, `ReferenceResolutionError` on miss | — | SONNET | Resolves `s1.output.leads.0.company_id`; rejects expressions, function calls and attribute traversal; a miss classifies as `REFERENCE_RESOLUTION` |
| TOOL-005 | Deterministic scoring rule engine with configurable weights and a `factors` breakdown | TOOL-003 | SONNET | Same inputs → identical score; `sum(contribution) ≈ score` (±1); `band` matches documented thresholds |
| TOOL-006 | `ContentPort`: deterministic template generator plus an Anthropic generator; placeholder and length guards | TOOL-003 | SONNET | With no API key the template generator produces a valid draft; no output containing `{{`, `TODO` or `[NAME]` ever validates |

## AGENT

| ID | Task | Deps | Model | Acceptance criteria |
|---|---|---|---|---|
| AGENT-001 | Typed `AgentState`, models and reducers | — | — | **done** — `app/agent/state.py`, `tests/test_state.py` |
| AGENT-002 | Graph assembly: nodes, conditional edges, compile with the checkpointer, no static interrupt lists | AGENT-003..008 | **OPUS** | Every node and every conditional edge in §6.1 is exercised by a test; no unreachable edge; `interrupt_before`/`after` are empty (ADR-007) |
| AGENT-003 | `understand` node: `NormalizedTask`, out-of-scope rejection | AGENT-006 | SONNET | Canonical request yields the documented entities; an out-of-scope request fails with `out_of_scope` before any planning |
| AGENT-004 | `decide` router: the seven ordered rules plus fan-out expansion | TOOL-004, HITL-003 | **OPUS** | Each rule tested in isolation **and** rule ordering tested (a step that is both budget-exhausted and approval-requiring must fail, not pause); expanded children count against `MAX_STEPS` |
| AGENT-005 | `execute_tool` node: resolve → validate → re-assert gate → dispatch → validate → record; one attempt per invocation | TOOL-002 | SONNET | Retry is an edge, not a loop inside the node; each attempt gets its own trace event and duration; the gate assertion fires when the node is called directly |
| AGENT-006 | `Planner` protocol, `RulePlanner`, `LLMPlanner` with strict schema validation, registry allowlisting and exactly one bounded repair attempt | — | **OPUS** | An LLM plan naming an unknown tool is rejected, not dispatched; an invalid plan is repaired at most once then fails; injected instructions in tool output never alter the plan; `planner_kind` recorded on the run |
| AGENT-007 | `recover` node wired to `recovery_action`, with backoff via the injected clock | FOUND-004 | SONNET | Exactly `1 + MAX_RETRIES` attempts on a permanently failing tool; delays match the formula; every decision traced with its branch and reason |
| AGENT-008 | `complete` and `fail` nodes plus `Responder`; terminal status computation including `partial` and `unconfirmed` | VERIFY-003 | SONNET | A rejected run yields `rejected` with a response naming what was not done; an unverified effect is reported as unconfirmed and never as done |
| AGENT-009 | Budget enforcement and cooperative cancellation at node boundaries | AGENT-002 | SONNET | Each of `MAX_STEPS`, `MAX_REPLANS` and `deadline_at` terminates with the right `status_reason`; cancellation never interrupts a tool mid-effect |

## HITL

| ID | Task | Deps | Model | Acceptance criteria |
|---|---|---|---|---|
| HITL-001 | Approval persistence and state machine: `pending → approved/rejected/expired/superseded/cancelled`, plus the TTL sweeper | DB-001 | SONNET | Every transition in §9.3 implemented; the sweeper expires a stale approval and terminates the run as `expired` |
| HITL-002 | `ApprovalGate` persistence lookup and token issuance from a stored approved decision | HITL-001 | **OPUS** | A token is mintable only from an `approved` row whose `args_hash` matches the arguments about to be sent; no other code path can mint one |
| HITL-003 | `request_approval` node: idempotent upsert, `approval_requested` on genuine insert only, then `interrupt()` — never a tool call | HITL-001 | **OPUS** | Pausing performs **zero** `mock_crm` writes (before/after table snapshot); re-entry creates no second approval row and no duplicate trace event |
| HITL-004 | Resume dispatch: conditional `UPDATE … WHERE status='pending'`, single-flight re-entry with `Command(resume=…)` | HITL-003 | **OPUS** | Two concurrent approve calls produce one resume and one `409`; a double resume produces exactly one outbox row |
| HITL-005 | `payload_preview` builder: de-referenced draft content and field-level customer diff | HITL-001 | SONNET | A send approval shows full subject and body; an update approval shows before/after per field; secrets and tokens never appear |

## VERIFICATION

| ID | Task | Deps | Model | Acceptance criteria |
|---|---|---|---|---|
| VERIFY-001 | Verifier framework keyed to `VerificationMode`, plus invariant verifiers for the read tools | TOOL-003 | SONNET | `NONE` still records `not_required`; each invariant in §8.4 is enforced; a verifier never mutates and never calls an LLM |
| VERIFY-002 | Readback verifiers for `save_draft`, `send_email_mock`, `update_customer` | VERIFY-001 | SONNET | Each compares against the **requested** intent, not the tool's echoed response; the outbox check asserts exactly one row for the idempotency key; the customer check asserts no field outside the patch changed |
| VERIFY-003 | `verify` node, `VerificationResult` persistence, `unconfirmed` propagation; verifier port errors classify as `TRANSIENT` | VERIFY-002 | SONNET | A `save_draft` that returns success but persists nothing is caught; the run does not report success; a non-idempotent unverified mutation is not retried |

## API

| ID | Task | Deps | Model | Acceptance criteria |
|---|---|---|---|---|
| API-001 | Response models, OpenAPI generation, RFC 9457 problem+json handler with the `code` table | FOUND-001 | SONNET | Every code in §13.1 is produced by some path; request bodies are `extra="forbid"`; `/openapi.json` is stable enough to generate types from |
| API-002 | Runs endpoints: create (with `Idempotency-Key`), start, get, list (keyset), cancel, retry | DB-005, API-001 | SONNET | Replay with an identical body returns the same run; a different body returns `409 idempotency_conflict`; pagination is stable while rows are inserted |
| API-003 | Trace endpoints: paginated `GET /trace` and SSE `GET /events` with `Last-Event-ID` replay | DB-002 | SONNET | The dashboard is correct with polling alone; a reconnect leaves no gap; the stream terminates on a terminal event |
| API-004 | Approvals endpoints: queue, decision with the full conflict family and the optional `args_hash` echo | HITL-004 | **OPUS** | Each of `approval_not_pending`, `approval_expired`, `approval_superseded`, `run_not_resumable` is returned for its exact condition; same decision twice is idempotent, a conflicting second decision is `409` |
| API-005 | Evaluation endpoints: trigger a suite, list runs, results, metrics | EVAL-003 | SONNET | Each result carries the `run_id` of a real inspectable run; `?passed=false` filters |
| API-006 | `GET /tools` catalog from the registry, plus health endpoints | TOOL-002 | SONNET | Payload matches `contracts.catalog()`; JSON Schemas are present for input and output |
| API-007 | `RunService` + `Executor`: lifecycle transitions, background execution, lease heartbeat, reconcile on startup | DB-007, AGENT-002 | **OPUS** | `POST /start` returns 202 without waiting; status transitions match §5.4; a killed process leaves no run permanently `running` |

## OBSERVABILITY

| ID | Task | Deps | Model | Acceptance criteria |
|---|---|---|---|---|
| OBS-001 | `TraceRecorder`, `@traced_node`, redaction denylist and payload truncation | DB-002 | SONNET | Every compiled node emits `node_entered` (test over the compiled graph); denylisted keys are `[redacted]`; oversized payloads carry `_truncated` |
| OBS-002 | Trace assembly service: cursors, filters, step/attempt grouping for the timeline | OBS-001 | SONNET | `since_seq` never skips or repeats an event; filters compose |
| OBS-003 | structlog `contextvars` binding of `run_id`/`step_id`/`node`/`tool`/`attempt` at node boundaries | OBS-001 | SONNET | Every log line emitted inside a node carries the run correlation fields without being passed one explicitly |
| OBS-004 | `trace_events` monthly range partitions and a 90-day retention job | DB-002 | SONNET | Expiry is a partition drop, not a mass delete; queries still use the `(run_id, seq)` index |
| OBS-005 | OpenTelemetry hook for HTTP and database spans, off by default | FOUND-001 | SONNET | Disabled adds no measurable overhead; enabling it does not duplicate product trace events |

## FRONTEND

| ID | Task | Deps | Model | Acceptance criteria |
|---|---|---|---|---|
| FE-001 | Next.js app scaffold: App Router, Tailwind, shadcn/ui, React Query, generated API types, lockfile | API-001 | SONNET | `npm run typecheck`, `lint` and `build` pass; the frontend CI job stops self-skipping; `schema.d.ts` is generated, not hand-written |
| FE-002 | Run list: status, request, duration, step and retry counters, filters, keyset pagination | FE-001, API-002 | SONNET | Renders from fixture JSON with no backend; filters map to query parameters |
| FE-003 | Run detail: plan-vs-actual list, timeline with nested retries and a labelled approval gap, step inspector with input/output and verification checks | FE-001, API-003 | SONNET | Fully reconstructible after a reload with no client state; planned-but-unrun steps are visibly distinct |
| FE-004 | Approval card and queue: full de-referenced payload, approve/reject with reason, `args_hash` echo, conflict handling | FE-001, API-004 | SONNET | Approving shows the complete draft body; a stale screen surfaces `approval_superseded` rather than silently retrying; the UI never computes `requires_approval` |
| FE-005 | Evaluations dashboard: suite runs, metric trends, per-case pass/fail with the failing assertion and a link to the run | FE-001, API-005 | SONNET | A failing case shows which assertion failed and links to its trace |
| FE-006 | Tools catalog page rendered from `GET /tools`, showing mutating and approval flags and the JSON Schemas | FE-001, API-006 | SONNET | Contract changes are visible in the UI without a code change |
| FE-007 | SSE live updates with polling fallback and reconnect via `Last-Event-ID` | FE-003, API-003 | SONNET | Killing the stream degrades to polling with no missing events and no duplicates |
| FE-008 | Frontend tests: Vitest + Testing Library with MSW typed by the generated schema, plus one Playwright approval smoke | FE-002..007 | SONNET | A backend contract change breaks the frontend tests; the e2e run submits, approves and completes a run |

## EVALUATION

| ID | Task | Deps | Model | Acceptance criteria |
|---|---|---|---|---|
| EVAL-001 | Case format, fixture dataset, suites file, and the seven required cases written to the assertions in §15.3 | TOOL-001 | **OPUS** | All seven cases present; `company_research` carries an injected instruction and asserts the plan is unchanged; `invalid_tool_result` forces a lying `save_draft` |
| EVAL-002 | Runner: fixture reset, settings overrides, `ApprovalPolicy`, `FailureInjector`, virtual clock — driving the **real** service path | EVAL-001, API-007 | **OPUS** | The suite calls the same services the API calls; approvals are real rows and real tokens; the gate is never disabled; the full suite runs in seconds |
| EVAL-003 | Metrics computation exactly as defined in §15.4, persisted to `evaluation_runs.metrics` | EVAL-002, DB-003 | SONNET | `case_pass_rate` and `task_success_rate` are computed separately; `agent_duration_ms` excludes approval wait |
| EVAL-004 | The seven global invariants asserted after every case | EVAL-002 | **OPUS** | Every outbox row traces to a matching approved approval; no step exceeds `1 + MAX_RETRIES`; no run is left non-terminal; `seq` is gapless |
| EVAL-005 | `python -m app.evaluation.cli` and a CI job that gates on `case_pass_rate` | EVAL-003 | SONNET | `make eval` prints a metric summary and exits non-zero on any failing case or violated invariant |

## TESTING

| ID | Task | Deps | Model | Acceptance criteria |
|---|---|---|---|---|
| TEST-001 | Test infrastructure: Postgres test database, transactional fixtures, model factories, the doubles in §18.2 | FOUND-003 | SONNET | `pytest -m "unit or contract"` needs no database; integration tests roll back cleanly and can run repeatedly |
| TEST-002 | Graph behaviour tests: every terminal path, pause, retry, replan, skip and verify transition, with scripted tools | AGENT-002 | **OPUS** | Each of the six control paths is asserted on state **and** on the trace; a forced loop terminates within budget |
| TEST-003 | The approval-gating suite: the six tests in §18.4 | HITL-004, VERIFY-003 | **OPUS** | All six pass; no test disables the gate; the type-level barrier is covered by mypy in CI plus a runtime constructor test |
| TEST-004 | API contract tests over ASGI transport for every endpoint and every error code | API-002..006 | SONNET | Every `code` in §13.1 has a test; `extra="forbid"` yields 422 |
| TEST-005 | Determinism test: the same case twice yields an identical trace modulo timestamps | EVAL-002 | SONNET | Byte-identical after normalising timestamps and durations |
| TEST-006 | Coverage reporting and a CI floor (80% backend), with the §18.4 tests marked required | FOUND-005 | SONNET | CI fails below the floor; the six critical tests cannot be skipped silently |

## DOCUMENTATION

| ID | Task | Deps | Model | Acceptance criteria |
|---|---|---|---|---|
| DOC-001 | Verify the README quickstart on a clean machine; fix whatever is wrong | FOUND-001 | SONNET | A fresh clone reaches a completed run with no API key by following the README only |
| DOC-002 | Keep `progress.md`, `tasks.md` and `handoff.md` current at the end of each work unit | — | SONNET | No task marked done without its acceptance criteria met; progress reflects `git log` |
| DOC-003 | Write an ADR for any deviation from the architecture, rather than editing the architecture silently | — | **OPUS** when the deviation is architectural | Divergence between docs and code is a bug; the ADR names what changed and why |
| DOC-004 | Operator walkthrough with dashboard screenshots, and an architecture diagram export | FE-003, FE-004 | SONNET | A reader who has not seen the code understands the approval flow |

---

## Model allocation

Use the stronger model where a wrong decision is expensive to unwind, not where
the work is merely long.

### OPUS 5 — 16 of 70 tasks

| Task | Why it needs deeper reasoning |
|---|---|
| DB-007 | Crash recovery across two sources of truth; the failure modes are the subtle part |
| TOOL-002 | The single choke point where validation, gating, idempotency and tracing are applied; a gap here is a safety hole |
| AGENT-002 | Graph topology; an unreachable or wrong edge is a silent behavioural bug |
| AGENT-004 | The safety router — rule *ordering* is the security property |
| AGENT-006 | Handling untrusted model output as data, with bounded repair |
| HITL-002 | Token issuance: the narrowest security boundary in the system |
| HITL-003 | Interrupt idempotency under LangGraph's re-execution semantics |
| HITL-004 | Concurrency: single-flight resume and the conflict family |
| API-004 | Approval decision conflicts; each wrong status code is a real bug for the UI |
| API-007 | Async run ownership, leases, reconciliation |
| EVAL-001 | Case design is where evaluation quality is actually decided |
| EVAL-002 | Determinism plumbing through the real service path |
| EVAL-004 | Property-based safety invariants |
| TEST-002 | Choosing which graph behaviours to pin, and how |
| TEST-003 | The tests that carry the product's safety claims |

| DOC-003 | Writing an ADR for a deviation — deciding whether a change is architectural is the judgement |

Also OPUS, by policy rather than by task: any architectural refactor, any
change to the approval or verification path, any difficult debugging session,
and any change that would alter a metric definition.

### SONNET 5 — the other 53 tasks (AGENT-001 is already done)

All CRUD and repositories, every migration, the nine tool implementations
(contracts are already fixed), all API route handlers once API-001 defines the
envelope, the entire frontend, all styling, routine and contract tests, metric
computation from stated formulas, documentation, and straightforward bug fixes.

The contracts, schemas, error taxonomy, state model and acceptance criteria are
all written down precisely so that this is genuinely routine work. **Do not
escalate a task to OPUS because it is large.** Escalate when the specification
is ambiguous, when a safety property is at stake, or when a mistake would be
discovered late.

### Escalation triggers

Move a SONNET task to OPUS if any of these becomes true mid-task:

1. The architecture does not actually answer a question the task requires.
2. The straightforward implementation would violate a documented invariant.
3. A test that should pass fails for a reason nobody understands.
4. The task turns out to require changing a contract, a status machine or a
   metric definition — that is an ADR, not an implementation detail.
