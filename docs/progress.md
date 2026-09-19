# Progress

Repository state as of 2026-09-13 (FOUND-001 session). Architecture session
was 2026-09-12.
**The repository is the source of truth.** If this file and `git log` disagree,
`git log` wins — and this file is wrong and should be fixed.

---

## Where the project stands

**Phase 0 — architecture: complete.** The design is specified, the decisions
are recorded with their costs, the backlog is prioritized, and the contract
spine is implemented and tested.

**Phase 1 — implementation: started.** FOUND-001..004, DB-001..007, TOOL-001
and TOOL-002 are done. Continue at `docs/handoff.md` §3 with TOOL-003.

```
architecture   ████████████████████  complete
contract spine ████████████████████  complete (state, contracts, errors, security, config)
foundation     ████████░░░░░░░░░░░░  FOUND-001, 002, 003, 004 done; 005 outstanding
persistence    ████████████████████  DB-001..007 done
tools          ██████████░░░░░░░░░░  TOOL-001, 002, 003 done; TOOL-004..006 outstanding
agent graph    ████████████████████  AGENT-001..009 complete
hitl           ████████████████████  HITL-001..005 complete
verification   ██████████████░░░░░░  VERIFY-001..002 complete; VERIFY-003 outstanding
api            ███████████░░░░░░░░░  API-001, 002, 003, 007 done; API-004..006 outstanding
observability  ██░░░░░░░░░░░░░░░░░░  redaction (§14.5) built by TOOL-002; OBS-001..005 outstanding
frontend       ░░░░░░░░░░░░░░░░░░░░  FE-001..008
evaluation     ████████████████████  EVAL-001..005 done
```

---

## Completed

### Documentation — all deliverables written

| Document | Contents |
|---|---|
| `README.md` | Overview, honest status, quickstart, the seven production-style properties and how each is actually guaranteed |
| `docs/architecture.md` | 19 sections, ~2.4k lines: requirements analysis, system architecture, frontend/backend boundary, agent layering, state model with per-field justification, the LangGraph graph with exact transition conditions, node responsibilities, tool contracts, HITL, retry/recovery, verification, persistence, API, observability, evaluation, security, configuration, testing, integration boundary |
| `docs/decisions.md` | 23 ADRs, each with the rejected alternative and the cost; 6 open questions with current defaults |
| `docs/tasks.md` | 70 tasks, dependencies, acceptance criteria, OPUS/SONNET allocation, critical path |
| `docs/progress.md` | This file |
| `docs/handoff.md` | Continuation instructions |

### Configuration — the stack is concrete

- `backend/pyproject.toml` — dependency decisions, strict mypy, ruff with
  security/async rules, pytest markers (`unit`/`integration`/`contract`/`eval`)
- `docker-compose.yml` — `db` + `api`, dashboard behind a `frontend` profile so
  the stack runs before the UI exists; no credential in the file
- `backend/Dockerfile` — non-root, health-checked against `/readyz`
- `.github/workflows/ci.yml` — gitleaks over full history, backend
  lint/types/tests against real Postgres, frontend job that self-skips until a
  lockfile exists; CI pins `OPSPILOT_PLANNER=rules` so no job needs a key
- `Makefile` — every CI check runnable locally
- `.env.example` — the complete configuration surface, placeholders only
- `frontend/` — dependency and TypeScript decisions (Next 15/React 19, strict
  plus `noUncheckedIndexedAccess`, API types generated from OpenAPI)

### Code — the contract spine, tested

| Module | What it provides | Verified by |
|---|---|---|
| `app/errors.py` | 12-class error taxonomy; `recovery_action` and `backoff_delay_ms` as pure functions | `test_recovery_policy.py` — branch ordering, the `1 + MAX_RETRIES` bound, backoff values |
| `app/security.py` | `canonical_args_hash`, `ApprovalToken` (unforgeable), `ApprovalGate.issue` | `test_security.py` — direct construction refused, changed arguments refused, cross-run replay refused, volatile keys excluded |
| `app/tools/schemas.py` | Typed IO for all nine tools, every model `extra="forbid"` | `test_tool_policy.py` |
| `app/tools/contracts.py` | The contract registry and its policy invariants | `test_tool_policy.py` — P1–P7 over every entry |
| `app/agent/state.py` | 21 state channels, reducers, `ApprovalState.grants` | `test_state.py` — reducer semantics, the args-hash gate, lifecycle |
| `app/config.py` | One `Settings`, `SecretStr`, `safe_dump`, `validate_runtime` fuses | `test_config.py` — planner degradation, all four production fuses, budget bounds |

### Code — FOUND-001, the FastAPI app factory

| Module | What it provides | Verified by |
|---|---|---|
| `app/main.py` | `create_app()` factory: wires `Settings`, calls `validate_runtime()`, configures structlog, adds CORS from config, creates the async DB engine, includes the health router, disposes the engine on shutdown | `test_health.py::TestHealthEndpoints`, `::TestCorsFromConfiguration`, `::TestStartupLogging` |
| `app/logging_config.py` | structlog configured to emit one JSON object per line to stdout (§14.6) | `test_health.py::TestStartupLogging` — asserts a structured `startup` event and no secret in the output |
| `app/api/health.py` | `/healthz` (liveness) and `/readyz` (DB reachability, plus Alembic head once FOUND-003 exists); `check_readiness` is a pure function over an injected engine so it's unit-testable with no Postgres | `test_health.py::TestCheckReadiness`, `::TestDiscoverAlembicHead` |

`/readyz`'s migration-head comparison is written generically now
(`discover_alembic_head` returns `None` until `alembic.ini` exists) so it
needs no further change when FOUND-003 lands — it will start enforcing the
head automatically.

### Code — FOUND-004, injected time/identity/randomness

| Module | What it provides | Verified by |
|---|---|---|
| `app/runtime.py` | `Clock`/`IdGenerator`/`SeededRandom` protocols; `SystemClock`/`UuidIdGenerator`/`DeterministicRandom` (real) and `FixedClock`/`SequentialIdGenerator` (fake) | `test_runtime.py` |

Nothing calls these yet — there is no consumer before AGENT-002+, TOOL-003 or
DB-001's timestamp columns — so this task is deliberately just the primitive
plus the structural guarantee
(`test_structure.py::test_only_runtime_generates_time_ids_and_randomness`)
that nothing bypasses it later.

### FOUND-002, the dependency lockfile

`backend/uv.lock` pins all 86 resolved packages against Python 3.12
(`backend/.python-version`); CI, the `Dockerfile`, and every `Makefile`
target install from it via `uv sync --locked` / `uv run` instead of an
unpinned `pip install -e`. `astral-sh/setup-uv@v5` in CI is pinned to
`0.11.19` to match the uv version that wrote the lock.

Verifying "`make check` reproduces CI exactly" surfaced pre-existing
`ruff check` / `ruff format --check` violations across files nobody had
touched this session (`app/errors.py`, `app/security.py`,
`app/tools/contracts.py`, `app/tools/schemas.py`, and several test files) —
they were real, just never caught because earlier local runs happened to
review truncated output. All were mechanical (line wraps, import order, two
narrow `# noqa`s for a recursive `Any` helper and a test's joke password) —
no behaviour changed, and the fix is what actually makes `make check`
green end to end for the first time.

This machine also had no Python 3.12 install and lives inside a
OneDrive-synced folder, which makes `uv`'s default hardlink-based install
fail (`os error 396`). Fixed generally, not just worked around locally: `uv
python install 3.12` gives every environment a real 3.12 interpreter
regardless of what's already on the machine, and `[tool.uv] link-mode =
"copy"` in `pyproject.toml` makes `copy` the project's install mode
everywhere (local, CI, Docker) rather than a one-off flag.

### FOUND-003, Alembic init

`backend/alembic.ini` + `backend/alembic/env.py` (the async template);
`env.py` overrides `sqlalchemy.url` from `app.config.get_settings()` rather
than trusting `alembic.ini`'s own placeholder, keeping `Settings` the only
thing that reads the environment (§17.1). The first revision
(`19463f144188`) issues `CREATE SCHEMA IF NOT EXISTS opspilot` and `... mock_crm`
on upgrade, and drops both (`CASCADE`) on downgrade; `langgraph` is never
created or touched (ADR-011) — it belongs to the LangGraph Postgres saver.

Docker Desktop was started this session specifically to verify this task
against a real `postgres:16-alpine` container, since its acceptance criteria
cannot be checked any other way. All three behaviours were confirmed live —
`alembic upgrade head` creates both schemas, a second `upgrade head` is a
true no-op (no `Running upgrade` log line), `downgrade base` drops both
schemas cleanly — and are now also encoded as `tests/test_migrations.py`
(`@pytest.mark.integration`), so future sessions don't have to redo this by
hand. The suite skips those 4 tests cleanly (~7s, one shared connectivity
probe) when no database is reachable, so plain `pytest` still passes
everywhere.

`/readyz` (FOUND-001) needed no code change to pick this up — confirmed live
against the same container: `GET /readyz` → `{"status": "ready", "revision":
"19463f144188"}`. `tests/test_health.py`'s
`test_the_real_repository_has_no_alembic_ini_yet` became
`test_the_real_repository_reports_its_actual_head`, asserting a non-empty
head without pinning the specific revision id.

**Test suite: 246 passed, 1 skipped** (`cd backend && uv run pytest`, with
`DATABASE_URL` pointed at a reachable Postgres — 4 of the 246 are the new
migration tests; without a database they skip instead, for 242 passed + 5
skipped)
One intentional skip: the mock-package network-import check activates when
TOOL-001 creates `app/integrations/mock/`.

---

## Verified rather than asserted

Facts established by running the code, not by writing them down:

1. The approval gate refuses a changed payload — approving draft `d_1` does not
   authorise sending `d_2`, and changing the recipient invalidates the grant.
2. A retry's new idempotency key does **not** invalidate an existing grant
   (volatile keys are excluded from the hash, recursively).
3. `ApprovalToken` cannot be constructed outside `ApprovalGate.issue`; a
   rejection cannot mint one; a token cannot be replayed into another run or step.
4. `merge_approval_state` does not re-open a decided approval when the
   interrupted node re-executes.
5. All four production startup fuses fire: no auth mode, placeholder password,
   wildcard CORS, and `integrations=real`.
6. `OPSPILOT_PLANNER=llm` without a key fails fast, while `auto` degrades to the
   rule planner — the distinction that makes three modes worth having.
7. The registry's gated tools are exactly `send_email_mock` and
   `update_customer`, and `CustomerPatch` has no field for `email`, `mrr` or any
   identity column.
8. Every task ID and ADR referenced anywhere in the repository resolves.
9. No credential-shaped string is tracked in git.

---

## Not done, deliberately

Everything in `docs/tasks.md`. Specifically **not** started tonight, because
the session's value was in decisions rather than volume:

- the LangGraph graph and its nodes (design complete, code not written)
- database models, migrations and repositories
- the nine tool implementations and the mock adapters
- the REST API and the dashboard
- the evaluation runner and the seven cases

## Known gaps and risks

| # | Gap | Mitigation already designed |
|---|---|---|
| 1 | Two sources of truth: LangGraph checkpoint vs control-plane tables | ADR-012; lease heartbeat plus the DB-007 reconciler, with a test that kills the process between the two writes |
| 2 | In-process executor — a deploy interrupts in-flight runs | ADR-004; nothing durable lives in the task, so runs resume from checkpoints and the queue upgrade is infrastructure, not redesign |
| 3 | No authentication | ADR-017; schema already carries `actor_id`/`decided_by`, and production refuses to start without auth |
| 4 | Rule planner coverage is narrow | ADR-002; it is evaluation scaffolding, not a product feature, and must be documented as such wherever it is user-visible |
| 5 | `trace_events` is the fastest-growing table | Truncation and redaction at write time; OBS-004 adds partitions and 90-day retention |
| 6 | Python is pinned to 3.12 in `pyproject.toml`, but the container that built this spine ran 3.11 | The code avoids 3.12-only syntax and passes on 3.11; FOUND-002 should confirm the suite on 3.12 in CI |

## Manual actions outstanding

| Action | Needed for | Blocking? |
|---|---|---|
| Obtain `ANTHROPIC_API_KEY` | LLM planning and LLM-written outreach copy | **No.** Everything runs on the rule planner and template generator. |
| Choose a license for this public repository | Contribution and reuse clarity | No, but it should be decided early |
| Deployment credentials / host | Anything beyond local Docker | No; local `docker compose` is the supported target |

No credential was fabricated or stubbed to work around any of these.

---

## Commit sequence for this session

1. `chore: initialize project configuration`
2. `docs: define system architecture, agent state and graph`
3. `docs: define tool contracts, HITL, retry and verification design`
4. `docs: define persistence model and API contracts`
5. `docs: define observability, evaluation, security and testing strategy`
6. `feat(agent): encode state, tool contracts and approval binding as typed code`
7. `docs: record architecture decisions and open questions`
8. `docs: add prioritized implementation backlog and model allocation`
9. `docs: add README, progress and handoff; fix the production password fuse`
10. `docs: sync progress with final session state`

Each commit is one architectural deliverable, inspected and secret-scanned
before committing. Run `git log --stat` for the detail; commit bodies record
what was decided and why, not merely what changed.

---

## Implementation session — 2026-09-13

**FOUND-001 done.** The FastAPI app factory: `create_app()` wires `Settings`
(with fail-fast `validate_runtime()`), structlog JSON logging, CORS from
config, an async SQLAlchemy engine, and the `/healthz`/`/readyz` routes. See
the FOUND-001 row in the table above and its entry in `docs/tasks.md`.

Local dev note: this machine has no Python 3.12 install (only 3.10 and 3.14
were available), so a `.venv` was created with 3.14 to install and run the
suite — `pyproject.toml`'s `requires-python = ">=3.12"` is satisfied, and
nothing 3.14-specific was used. The venv is untracked (`.gitignore` already
covers `.venv/`); FOUND-002's lockfile task should pin the real target
version in CI.

Commit: `feat(api): add the FastAPI app factory with health and readiness endpoints`.

**FOUND-004 done**, immediately after, as an unambiguously independent
SONNET task depending only on FOUND-001: `Clock`/`IdGenerator`/`SeededRandom`
protocols in `app/runtime.py`, with the structural test that nothing bypasses
them. See its row in `docs/tasks.md` for the `SeededRandom` design note (one
implementation, not a real/fake pair).

Commit: `feat(agent): add injected Clock, IdGenerator and SeededRandom primitives`.

**FOUND-002 done** next, since FOUND-003's acceptance criteria need a live
Postgres to verify (`alembic upgrade head` / `downgrade base`) and this
machine's Docker Desktop daemon was not running — FOUND-002 needed no
database and was the other unblocked, unambiguously independent SONNET task.
See its row in `docs/tasks.md` for the full list of what changed, including
the pre-existing lint violations it fixed to make `make check` actually
green.

Commit: `chore(build): lock backend dependencies with uv and wire CI/Docker to install from it`.

**FOUND-003 done** last, once Docker Desktop was started and a throwaway
`postgres:16-alpine` container was available to verify against — see its row
in `docs/tasks.md` and the section above for what was built and confirmed.

Commit: `feat(db): initialize Alembic with the opspilot and mock_crm schemas`.

Session stopped here on explicit instruction, with FOUND-003 as the last
coherent unit finished, checked, documented and pushed. No further task was
started automatically.

A leftover from this session: a throwaway Postgres container
(`opspilot-pg-dev`, port 55432) is still running locally for whoever picks up
DB-001 next; it is not part of the committed stack and can be removed with
`docker rm -f opspilot-pg-dev` once no longer needed.

## DB-001 — control-plane persistence models — 2026-09-13

**Done.** `app/persistence/base.py` (the shared `Base` — `DeclarativeBase`
scoped to the `opspilot` schema, with a naming convention so hand-written
migrations and a future `--autogenerate` diff agree on constraint names) and
`app/persistence/models.py`: `AgentRun`, `ExecutionStep`, `ToolCallRow`,
`ApprovalRow`, matching §12.3–§12.6 column-for-column, including nullability
and defaults. Migration `c6d1db7aa718` (`Revises: 19463f144188`) creates all
four tables by hand, self-contained (enum values spelled out as literals
rather than imported from the model module, so the migration's behaviour
can't drift if that module changes later).

Column typing follows §12.10 literally rather than by convenience: a column
the architecture types `enum` (`agent_runs.status`, `execution_steps.status`
and `.verification_status`, `tool_calls.status`, `approvals.status` and
`.risk`) is a real database-enforced `CHECK` constraint
(`sa.Enum(..., native_enum=False, create_constraint=True)` — a constrained
text column, not a Postgres `CREATE TYPE`, so adding a value later is a plain
constraint migration rather than an `ALTER TYPE` lifecycle outside a
transaction); a column the architecture types `text` (`planner_kind`, `tool`,
`tool_version`) stays a plain unconstrained text column even though its
values happen to come from a Python `StrEnum` elsewhere in the codebase,
because the architecture deliberately did not gate those at the database.
`tool_calls.status` needed a new enum (`ToolCallStatus`:
`succeeded/failed/timeout/duplicate_suppressed`) since nothing existing named
it — everything else reuses `RunStatus`/`StepStatus`/`VerificationStatus`/
`ApprovalStatus` from `app.agent.state` and `RiskLevel` from
`app.tools.contracts`, so the DB-level vocabulary can't drift from the
in-process one.

One deliberate deviation, recorded rather than silently patched: §12.3 lists
`evaluation_run_id` as an FK to `evaluation_runs`, but that table doesn't
exist until DB-003 (which depends on DB-001, not the reverse). The column
exists now with no FK constraint; DB-003's migration adds the constraint once
its target table exists, rather than DB-001 reaching forward to create a
table out of order.

`alembic/env.py` now imports `app.persistence.models` and sets
`target_metadata = Base.metadata` (previously `None`, per FOUND-003's
comment that this was deferred until models existed) — migrations are still
written by hand, but `alembic check`/`--autogenerate` are now available as a
cross-check. (Running `alembic check` locally shows false-positive FK diffs
caused by this machine's Postgres role being named `opspilot`, which happens
to collide with the schema name and change Postgres's default `search_path`
resolution during reflection — verified as a reflection artifact, not a real
schema mismatch, by inspecting `\d` on every table directly against the live
container. **Correction (DB-003):** this was assumed to be an inherent,
un-fixable quirk of the local role name and left as noise. It is not — it is
a genuine `alembic/env.py` configuration gap that would misfire in any real
deployment using the documented `POSTGRES_USER=opspilot`, and DB-003 fixes
it properly. See that section for the root cause and the fix; `alembic
check` is clean from DB-003 onward and a regression test
(`tests/test_migrations.py::TestNoAutogenerateDrift`) now enforces it.)

Verified against a real `postgres:16-alpine` container (`opspilot-pg-dev`,
port 55432): `alembic upgrade head` creates all four tables with every
column type, default, `CHECK`, FK and index in §12.3–§12.6; `alembic
downgrade 19463f144188` drops exactly those four tables and leaves the
`opspilot` schema itself (FOUND-003's) untouched; a downgrade → upgrade
cycle reproduces an identical schema. All three behaviours are now also
`tests/test_migrations.py::TestControlPlaneTablesMigration`, following the
same pattern FOUND-003 established.

`tests/test_persistence_models.py` (new, `@pytest.mark.integration`, skips
cleanly with no reachable database) covers the rest of the acceptance
criteria against the live container: every model can be created with
sane defaults; all six enum `CHECK` constraints reject an out-of-vocabulary
value (short enough — `"bogus"`, 5 chars — to fit inside every column's
narrowest varchar width, so the test actually exercises the `CHECK` rather
than tripping a column-width `DataError` first); both foreign keys reject an
unknown parent id; deleting a run cascades to its steps, tool calls and
approvals (checked with a direct `SELECT count(*)`, not `Session.get` — the
ORM has no relationships declared, so its identity map has no way to know
the DB-level `ON DELETE CASCADE` fired, and would otherwise hand back stale
cached objects); every named index in §12.3–§12.6 is present in
`pg_indexes`; a second **pending** approval for the same `(run_id, step_id)`
is rejected by the partial unique index, while a new pending approval is
allowed once the first is decided (the positive case the partial condition
exists to permit); a second `tool_calls` row with the same
`(execution_step_id, attempt)` is rejected, while a second attempt with a
different number is allowed. Each test runs inside a `Session` joined to an
external transaction via a savepoint (`join_transaction_mode=
"create_savepoint"`) so a `pytest.raises(IntegrityError)` — which aborts the
current savepoint — doesn't poison the rest of the test, and nothing written
survives the test.

**Test suite: 266 passed, 1 skipped** (`cd backend && uv run pytest`, with
`DATABASE_URL` pointed at a reachable Postgres — 7 of the 266 are new
migration-determinism tests plus 19 new model tests; without a database,
those 26 skip instead, matching the FOUND-003 pattern). `ruff check .`,
`ruff format --check .` and `mypy app` (strict) are all clean.

Next task: **DB-002** (`trace_events` with a per-run monotonic `seq` and
`unique(run_id, seq)`) — SONNET, next on the critical path since it depends
only on DB-001. **DB-004** (`mock_crm` models) is also unblocked, since it
depends only on FOUND-003 — SONNET, independent of DB-002/003. **FOUND-005**
(CI green on the real matrix) remains unblocked and unstarted from the prior
session, lower priority than the DB-00x chain since it's a verification task
rather than a critical-path blocker.

A leftover from this session: the same throwaway Postgres container
(`opspilot-pg-dev`, port 55432) is still running for whoever picks up DB-002
next; remove with `docker rm -f opspilot-pg-dev` once no longer needed.

## DB-002 — `trace_events` with per-run monotonic `seq` — 2026-09-13

**Done.** `app/persistence/models.py` gains `TraceEvent` (§12.7),
`TraceEventKind` (all 29 kinds in §14.2, spelled out as a closed `StrEnum` —
`@traced_node`/`ToolRegistry.dispatch` are the only structural emitters, so
this is exactly what can ever be produced, not an open vocabulary) and
`TraceEventSeverity` (`debug/info/warning/error`). Migration `21765d8fa136`
(`Revises: c6d1db7aa718`) creates the table by hand, self-contained like
`c6d1db7aa718` — enum values spelled out as literals rather than imported.
Every column, nullability, default and index in §12.7 is implemented
literally: `id bigserial` (not a uuid, unlike every other control-plane
table — §12.7 is explicit, and this is the fastest-growing table by an order
of magnitude so a sequential key avoids the write amplification a random
uuid PK would cause), `UNIQUE(run_id, seq)`, `(run_id, id)`, `(kind, ts DESC)`,
and a BRIN index on `ts` for the retention scans OBS-004 will run.

**The concurrency mechanism (the point of this task).** `UNIQUE(run_id, seq)`
alone does not make concurrent allocation correct — under `READ COMMITTED`,
two transactions can both read the same `MAX(seq)` before either inserts and
race to write the same value, which the constraint only catches after the
fact. `app/persistence/trace_events.append_trace_event` instead takes a
**transaction-scoped Postgres advisory lock keyed by `run_id`**
(`pg_advisory_xact_lock(hashtextextended(run_id::text, 0))`) immediately
before the `MAX(seq)+1` read and the insert. The lock blocks other writers
for the *same* run until it releases at commit/rollback, serializing exactly
the read-then-insert window per run; different runs proceed independently
(a `hashtextextended` collision between two live run ids is practically
impossible, and would only cost extra serialization, never an incorrect
`seq`, if it happened). `UNIQUE(run_id, seq)` remains as the database-level
backstop against any code path that bypasses this function — not the
mechanism that makes concurrent appends correct in the first place. Chosen
over a `SELECT ... FOR UPDATE` on a per-run row (no such row exists before
the first event, and creating one is more moving parts for no extra
correctness) and over a Postgres sequence per run (sequences aren't
naturally scoped per row without one sequence object per run, which doesn't
compose with an unbounded number of runs). No new infrastructure — Postgres
advisory locks are built for exactly this "serialize by an application key"
case (ADR-019).

**Verified against a real `postgres:16-alpine` container**
(`opspilot-pg-dev`, port 55432), not simulated: 25 concurrent threads, each
with its own connection and its own committing transaction, appending to the
*same* run, produce exactly `1, 2, ..., 25` in the database — no duplicates,
no gaps (`tests/test_trace_events.py::TestConcurrentSequenceAllocation
::test_concurrent_appends_yield_1_through_n_with_no_duplicates_or_gaps`).
A second test runs two runs' workers interleaved on one thread pool and
confirms each run's sequence is independently `1..15` with no cross-run
interference. A third forces the actual race the constraint exists to catch
— two threads racing to insert the same explicit `(run_id, seq)` bypassing
the allocator — and confirms exactly one wins and one gets `IntegrityError`.
These are genuine separate transactions racing against a live server, not
one session's in-order calls standing in for concurrency.

`tests/test_trace_events.py` (18 tests, `@pytest.mark.integration`, skips
cleanly with no reachable database, same pattern as `test_persistence_models
.py`) also covers: event creation (minimal and every field populated),
`run_id`/`seq`/`kind` NOT NULL and FK enforcement, both enum `CHECK`
constraints, cascade delete from `agent_runs`, the duplicate-`(run_id, seq)`
rejection and the same-`seq`-different-run positive case, every named index
present in `pg_indexes` plus a direct check that the `ts` index actually uses
the `brin` access method and that the `run_id, seq` index is actually
`UNIQUE`, and sequential (non-concurrent) allocation producing `1, 2, 3`
within one run and independent counters across two runs.

`tests/test_migrations.py` gains `TestTraceEventsMigration`: upgrade creates
`trace_events`; downgrade to `c6d1db7aa718` drops only `trace_events` and
leaves DB-001's four tables untouched; a second `upgrade head` is a true
no-op; downgrade-then-reupgrade reproduces the same schema. One pre-existing
DB-001 test hardcoded `c6d1db7aa718` as "the head" — true when DB-001 was the
newest revision, false now that DB-002 sits on top of it. Fixed to compare
against Alembic's own `ScriptDirectory.get_current_head()` instead of a
literal id, in both the DB-001 and the new DB-002 test, so neither goes stale
again the next time a migration lands on top. This is a test-assertion fix
required by adding a revision, not a change to DB-001's actual schema or
behavior — the four control-plane tables and their migration are untouched.

**Test suite: 288 passed, 1 skipped** (`cd backend && uv run pytest`, with
`DATABASE_URL` pointed at a reachable Postgres — 22 of the 288 are new:
18 in `test_trace_events.py`, 4 in `test_migrations.py`; without a database,
those 22 skip instead, for 241 passed + 48 skipped, matching the established
pattern). `ruff check .`, `ruff format --check .` and `mypy app` (strict)
are all clean.

Next task: **DB-003** (`evaluation_runs`/`evaluation_results`, and the FK
`agent_runs.evaluation_run_id` deferred by DB-001) — SONNET, depends only on
DB-001. **DB-004** (`mock_crm` models) remains unblocked and independent,
depending only on FOUND-003.

The same throwaway Postgres container (`opspilot-pg-dev`, port 55432) is
still running for whoever picks up the next task; remove with
`docker rm -f opspilot-pg-dev` once no longer needed.

## DB-003 — `evaluation_runs`/`evaluation_results`, and the deferred `agent_runs` FK — 2026-09-13

**Done.** `app/persistence/models.py` gains `EvaluationRun`, `EvaluationResult`
and `EvaluationRunStatus` (`running/completed/failed` — §12.8). Migration
`62fe5fff7640` (`Revises: 21765d8fa136`) creates both tables by hand,
self-contained like the prior migrations — enum values spelled out as
literals rather than imported. Every column, nullability, default and index
in §12.8 is implemented literally: `unique(evaluation_run_id, case_id)` and
`(case_id, passed)` on `evaluation_results`, `(suite, started_at desc)` on
`evaluation_runs`. `git_sha`, `prompt_version` and `seed` are persisted on
`evaluation_runs` exactly as specified — nullable, like the equivalent
reproducibility fields already on `agent_runs` (`model_id`/`prompt_version`/
`seed`), since a suite run without a pinned prompt or a clean git tree still
needs to be recorded rather than rejected.

**The deferred FK.** DB-001's migration (`c6d1db7aa718`) deliberately left
`agent_runs.evaluation_run_id` without a foreign key, since `evaluation_runs`
didn't exist yet (DB-003 depends on DB-001, not the reverse). This revision
adds it (`fk_agent_runs_evaluation_run_id_evaluation_runs`) via
`op.create_foreign_key` against the now-existing table, on top of the column
and its index DB-001 already created — no change to `c6d1db7aa718` itself.

**Deletion semantics — one deliberate design decision beyond what §12.8
spells out per-column.** §12.8 states `evaluation_results.evaluation_run_id`
is `ON DELETE CASCADE` explicitly; it does not state a deletion rule for
`evaluation_results.run_id` (→ `agent_runs`) or `agent_runs.evaluation_run_id`
(→ `evaluation_runs`). Both were given the database's default (`NO ACTION`,
i.e. the delete is rejected while a referencing row exists), not `CASCADE`,
for the same reason `parent_run_id` and `approvals.superseded_by` are
unspecified rather than cascading: these are *sideways* references to a row
the referencing table does not own — an `evaluation_results` row is
evaluation history *about* a real `agent_runs` row, and an `agent_runs` row
that happens to have been produced by the eval suite is still real execution
history, not part of the `evaluation_runs` aggregate. Cascading either would
silently destroy history that a plain "delete this one row" was never meant
to touch; blocking the delete is the safer default and matches every other
unspecified FK in the schema. This is a design decision that fills a gap the
architecture leaves open, not a deviation from anything it states — recorded
here per DOC-003's spirit (an ADR would be for reversing something the
architecture actually decided).

**Verified against a real `postgres:16-alpine` container**
(`opspilot-pg-dev`, port 55432): `alembic upgrade head` creates both tables
and the deferred FK; a second `upgrade head` is a true no-op; `alembic
downgrade 21765d8fa136` drops exactly the two new tables and the new FK,
leaving DB-002's `trace_events` and DB-001's four control-plane tables
untouched; a downgrade → upgrade cycle reproduces an identical schema. All
four behaviours are now `tests/test_migrations.py::TestEvaluationTablesMigration`
(5 tests), following the established pattern.

`tests/test_evaluation_models.py` (21 tests, `@pytest.mark.integration`,
skips cleanly with no reachable database) covers: creation of both models
(minimal and fully populated, including explicit assertions that `git_sha`,
`prompt_version` and `seed` persist and round-trip); required fields and
defaults (`status` defaults to `running`, counters default to `0`, `metrics`/
`assertions` default to `{}`/`[]`); the invalid-enum-value `CHECK` rejection
on `evaluation_runs.status`; the evaluation-run-to-results relationship,
including the `ON DELETE CASCADE` verified live (delete the run, the result
row is gone); the evaluation-result-to-real-agent-run relationship, including
both FK-miss rejections (unknown `run_id`, unknown `evaluation_run_id`) and
the `NO ACTION` restriction verified live in both directions (deleting an
`agent_runs` row referenced by a result, and deleting an `evaluation_runs`
row referenced by an agent run, both raise `IntegrityError`); the deferred
`agent_runs.evaluation_run_id` FK end to end (valid reference accepted,
unknown id rejected, restrict-on-delete verified); the `uq_evaluation_results
_evaluation_run_id_case_id` uniqueness constraint, including the positive
case (the same `case_id` is allowed across two different evaluation runs);
and every named index and the deferred FK's existence read directly from
`pg_indexes`/`pg_constraint`.

**`alembic check` schema-drift bug found and fixed, not suppressed.**
Adding a second cross-schema FK (the deferred `agent_runs.evaluation_run_id
→ evaluation_runs.id`) made it worth actually running `alembic check` again
rather than repeating DB-001/DB-002's "cosmetic, ignore it" note — and that
note was wrong. Root cause: `alembic/env.py`'s `context.configure()` never
passed `include_schemas=True`, and Postgres's default `search_path`
(`"$user", public`) makes a role named `opspilot` resolve `opspilot` as the
connection's *ambient default* schema — the same name as our real schema
(confirmed directly: `inspector.default_schema_name == "opspilot"`).
Unqualified reflection then reports every table/FK actually in `opspilot`
as `schema=None`, while `target_metadata` says `schema="opspilot"`
explicitly (`app.persistence.base.Base`) — two different identities for the
same object, so the autogenerate comparator reported **every** foreign key
in the schema as simultaneously removed and re-added. This is not a
machine-specific fluke: `docker-compose.yml`/`.env.example` both use
`POSTGRES_USER=opspilot`, so any real deployment following the documented
setup hits the identical ambiguity.

Fix, in `alembic/env.py` only (no migration touched): `include_schemas=True`
on both `context.configure()` calls (needed so `opspilot`/`mock_crm` are
reflected and compared at all, instead of only the connection's default
schema — confirmed insufficient by itself, since it didn't change which
schema counts as "default"); and the async engine's `connect_args` now pin
`search_path` to `public` (a schema with no ORM tables, so it can never
collide with a real one), which is what actually removes the ambiguity.
This is a reflection-time fix only — every migration already fully
qualifies its DDL with `schema=SCHEMA` and every ORM query is
schema-qualified through `Base.metadata`, so nothing relies on
`search_path` for correctness at runtime, only Alembic's comparator did.
Confirmed live: `alembic check` now prints "No new upgrade operations
detected" (stable across repeated runs), including the `alembic_version`
false-positive `include_schemas=True` introduced along the way (also
resolved by the same `search_path` pin). A regression test,
`tests/test_migrations.py::TestNoAutogenerateDrift`, calls
`alembic.command.check()` against a migrated database and fails loudly if
this ambiguity ever reappears, instead of being noticed only as local CLI
noise and tolerated again. The DB-001 note that first observed this and
mis-diagnosed it as unfixable is corrected in place, with a pointer here.

**Test suite: 315 passed, 1 skipped** (`cd backend && uv run pytest`, with
`DATABASE_URL` pointed at a reachable Postgres — 27 of the 315 are new: 21 in
`test_evaluation_models.py`, 6 in `test_migrations.py` (5 for the new
migration plus `TestNoAutogenerateDrift`); without a database, those 27 skip
instead, matching the established pattern). `ruff check .` and
`ruff format --check .` are clean; `mypy app` (strict) is clean. Full
migration lifecycle re-verified after the `env.py` fix: upgrade head, a
second upgrade head is a true no-op, downgrade to `21765d8fa136` cleanly
drops only the DB-003 tables and FK, re-upgrade reproduces an identical
schema, and `alembic check` is clean throughout.

## DB-004 — `mock_crm` persistence models — 2026-09-13

**Done.** `app/persistence/mock_crm.py` (simulated system-of-record models in the
`mock_crm` schema: `Company`, `Lead`, `Customer`, `OutreachDraft`, `EmailOutbox`,
cleanly separated from control plane persistence models per ADR-011 and §12.1),
`app/persistence/base.py` (`MOCK_CRM_SCHEMA = "mock_crm"`), and
`alembic/versions/e35beebb06d0_add_mock_crm_persistence_tables.py` (`Revises:
62fe5fff7640`). Every column, nullability, default, foreign key, unique
constraint, index and status enum in §12.9 is implemented.

Column details match the architecture literally:
- `companies`: `company_id` PK, `name`, `domain UNIQUE`, `industry`,
  `employee_count`, `revenue_band`, `hq_location`, `funding_stage`,
  `tech_stack jsonb`, `signals jsonb`, `created_at`, `updated_at`.
- `leads`: `lead_id` PK, `company_id` FK -> `mock_crm.companies.company_id`,
  `full_name`, `title`, `email`, `status` (`CHECK` constrained text enum:
  `new/working/qualified/disqualified`, default `new`), `source`, `owner`,
  `phone`, `timezone`, `tags text[]`, `notes`, `last_contacted_at`,
  `created_at`, `updated_at`.
- `customers`: `customer_id` PK, `account_name`, `primary_contact`,
  `email UNIQUE`, `phone`, `status` (`CHECK` constrained text enum:
  `prospect/active/churned`, default `prospect`), `plan`, `mrr numeric(12, 2)`,
  `owner`, `notes`, **`version int not null default 1`**, `created_at`,
  `updated_at`. The `version` column is the concurrency token `update_customer`
  requires (§8.5, §12.9) and is ready for optimistic concurrency handling.
- `outreach_drafts`: `draft_id` PK, `lead_id` FK -> `mock_crm.leads.lead_id`,
  `channel` (default `'email'`), `subject`, `body`, `content_hash`,
  `status` (`CHECK` constrained text enum: `saved/sent/archived`, default `saved`),
  `version int not null default 1`, `metadata jsonb`, `created_at`, `updated_at`.
- `email_outbox`: `outbox_id` PK, `message_id UNIQUE`, `draft_id` FK ->
  `mock_crm.outreach_drafts.draft_id`, `to_email`, `subject`, `body`,
  `status` (`CHECK` constrained text enum: `sent/failed`, default `sent`),
  `provider` (default `'mock'`), **`idempotency_key UNIQUE`**, `run_id`,
  `approval_id`, `created_at`, `sent_at`.
  `idempotency_key UNIQUE` is enforced directly at the PostgreSQL database level
  so a retried send is physically incapable of creating duplicate message records.
  `run_id` and `approval_id` ensure every send is traceable to its human approval.

**Verified against a real `postgres:16-alpine` container** (`opspilot-pg-dev`,
port 55432):
- `alembic upgrade head` creates all 5 tables in `mock_crm` with all columns,
  defaults, FKs, unique constraints, and indexes.
- A second `upgrade head` is a true no-op.
- `alembic downgrade 62fe5fff7640` cleanly drops all 5 `mock_crm` tables and
  leaves control-plane tables and schemas untouched.
- Downgrade-then-reupgrade reproduces an identical schema.
- `alembic check` reports zero drift ("No new upgrade operations detected").
- All migration behaviors encoded as `tests/test_migrations.py::TestMockCrmTablesMigration` (4 tests).

`tests/test_mock_crm_models.py` (30 new tests, `@pytest.mark.integration`, skips
cleanly without database):
- Creation of each model (minimal and fully populated).
- Foreign key enforcement: invalid `company_id` on `leads`, invalid `lead_id`
  on `outreach_drafts`, and invalid `draft_id` on `email_outbox` are all rejected
  by PostgreSQL `IntegrityError`.
- Duplicate rejection on all unique fields: `companies.domain`, `customers.email`,
  `email_outbox.message_id`, and `email_outbox.idempotency_key`.
- `customers.version` defaults to 1 and can be incremented for optimistic concurrency.
- Relationship navigation verified across all related models.
- All 4 status enum `CHECK` constraints reject out-of-vocabulary values at the database.
- Cascade and restrict behavior: deleting a company with leads, a lead with drafts,
  or a draft with outbox messages is restricted and rejected with `IntegrityError`.
- All named indexes and constraints verified directly against `pg_indexes` and `pg_constraint`.

**Test suite: 349 passed, 1 skipped** (`cd backend && uv run pytest`, with
`DATABASE_URL` pointed at a reachable Postgres — 34 of the 349 are new: 30 in
`test_mock_crm_models.py`, 4 in `test_migrations.py`; without a database, those
34 skip instead, matching established pattern). `ruff check .`,
`ruff format --check .` and `mypy app` (strict) are all clean.

Next task: **DB-005** (Async repositories per aggregate; no ORM session leaks outside them) — SONNET, depends on DB-001..004.

## DB-005 — Async repositories per aggregate — 2026-09-13

**Done.** Implemented the complete asynchronous repository layer for all persistence
aggregates across both control plane (`opspilot` schema) and mock CRM (`mock_crm`
schema) per §12, DB-005:

- `app/persistence/protocols.py`: Runtime-checkable protocols defining strict, typed
  contracts for all 11 aggregate repositories (`AgentRunRepository`, `ExecutionStepRepository`,
  `ToolCallRepository`, `ApprovalRepository`, `TraceEventRepository`, `EvaluationRepository`,
  `CompanyRepository`, `LeadRepository`, `CustomerRepository`, `OutreachDraftRepository`,
  `EmailOutboxRepository`) plus `UnitOfWork`. Higher application layers depend exclusively
  on these protocols.
- `app/persistence/repositories.py`: Asynchronous SQLAlchemy implementations encapsulating
  all `select()`, `insert()`, `update()`, and PostgreSQL advisory locks (`SqlAgentRunRepository`,
  `SqlExecutionStepRepository`, `SqlToolCallRepository`, `SqlApprovalRepository`,
  `SqlTraceEventRepository`, `SqlEvaluationRepository`, `SqlCompanyRepository`,
  `SqlLeadRepository`, `SqlCustomerRepository`, `SqlOutreachDraftRepository`,
  `SqlEmailOutboxRepository`, `SqlUnitOfWork`).
- `app/persistence/session.py`: `create_session_factory` and `unit_of_work()` context manager.
  Session lifecycle is strictly scoped to context blocks with explicit commit semantics:
  changes require an explicit `await uow.commit()`; exiting without commit or upon exception
  automatically executes rollback. Sessions are guaranteed closed in `finally:`. No global
  mutable session object exists.
- `app/persistence/__init__.py`: Clean public API exporting models, protocols, repositories,
  and session utilities.
- Architectural boundary enforcement: `tests/test_structure.py::test_no_orm_query_construction_outside_persistence`
  statically verifies via AST that zero `select()`, `insert()`, `update()`, or `delete()`
  ORM query building occurs outside `app/persistence/`.
- State transitions and concurrency:
  - `ApprovalRepository.decide`: atomic conditional update (`WHERE status='pending' RETURNING *`),
    resolving double-clicks and concurrent decisions directly at the database.
  - `CustomerRepository.update_optimistic`: atomic optimistic concurrency update checking
    `version == expected_version` and incrementing version; returns `None` on stale write.
  - `TraceEventRepository.append`: transaction-scoped Postgres advisory lock (`pg_advisory_xact_lock`)
    allocating monotonic gapless `seq` values per run.
- Integration tests (`tests/test_repositories.py`): 16 tests covering protocol conformance,
  CRUD and query behaviors, missing record semantics, orphan detection, explicit commit,
  rollback on error, rollback on uncommitted exit, session cleanup, database constraint
  violations, and concurrent gapless trace event sequence allocation.

**Test suite: 366 passed, 1 skipped** (`cd backend && uv run pytest`, with
`DATABASE_URL` pointed at a reachable Postgres — 17 of the 366 are new: 16 in
`test_repositories.py`, 1 in `test_structure.py`). `ruff check .`,
`ruff format --check .` and `mypy app` (strict) are all clean; `alembic check` clean.

Next task: **DB-006** (Constraint tests against real Postgres for every safety-relevant index) — SONNET, depends on DB-001..004.

## DB-006 — Database constraint / invariant verification — 2026-09-13

**Done.** Implemented database-level constraint and invariant verification suite against real PostgreSQL (`tests/test_database_invariants.py`, 34 tests), proving that all safety-critical invariants are enforced by PostgreSQL itself — not merely by Python or ORM pre-checks per DB-006 acceptance criteria:

1. **Safety-Critical Unique Constraints & Genuinely Concurrent Races**:
   - `uq_approvals_run_id_step_id_pending` (partial unique index on `opspilot.approvals` WHERE `status = 'pending'`):
     - Verified existence in `pg_indexes`.
     - Positive case: allows multiple non-pending approvals for same `(run_id, step_id)`.
     - Negative case: rejects second pending approval with PostgreSQL `IntegrityError`.
     - Concurrency race: 10 competing transactions, 1 successful commit, 9 rejected with `IntegrityError`, final DB count: 1 pending approval.
   - `uq_email_outbox_idempotency_key` (unique constraint on `mock_crm.email_outbox`):
     - Verified existence in `pg_indexes`.
     - Concurrency race: 10 competing transactions, 1 successful commit, 9 rejected with `IntegrityError`, final DB count: 1 row.
   - `uq_trace_events_run_id_seq` (unique constraint on `opspilot.trace_events`):
     - Verified existence in `pg_indexes`.
     - Raw concurrency race: 10 competing transactions attempting duplicate `seq=1`, 1 successful commit, 9 rejected with `IntegrityError`, final DB count: 1 row.
     - Coordinated sequence allocator race: 20 competing transactions under `pg_advisory_xact_lock`, 20 successful commits, 0 rejected operations, final DB state: gapless monotonic sequence `[1, ..., 20]`.
   - `uq_agent_runs_idempotency_key` (unique constraint on `opspilot.agent_runs`):
     - Verified existence in `pg_indexes`.
     - Concurrency race: 10 competing transactions, 1 successful commit, 9 rejected with `IntegrityError`, final DB count: 1 row.
   - `uq_execution_steps_run_step_revision` & `uq_tool_calls_execution_step_id_attempt`:
     - Verified existence in `pg_indexes`.
     - Tool call attempt race: 10 competing transactions inserting `attempt=1`, 1 successful commit, 9 rejected with `IntegrityError`, final DB count: 1 row.
   - `uq_evaluation_results_evaluation_run_id_case_id`:
     - Verified existence in `pg_indexes`.
     - Concurrency race: 10 competing transactions, 1 successful commit, 9 rejected with `IntegrityError`, final DB count: 1 row.
   - Mock CRM Uniqueness:
     - `uq_companies_domain`: 5 competing transactions, 1 successful commit, 4 rejected with `IntegrityError`.
     - `uq_customers_email`: 5 competing transactions, 1 successful commit, 4 rejected with `IntegrityError`.

2. **Customer Optimistic Concurrency**:
   - Default `version` is 1 on insert.
   - Concurrency race: 10 competing transactions issuing conditional `UPDATE mock_crm.customers ... WHERE version = 1`, exactly 1 transaction updates (1 row affected), 9 transactions receive 0 rows affected (lost-update prevented), final database state: `version = 2`.

3. **Referential Integrity (Cascade & Restrict)**:
   - Cascade delete: direct raw SQL `DELETE FROM opspilot.agent_runs` automatically cascades and deletes all child `execution_steps`, `tool_calls`, `approvals`, and `trace_events`.
   - Restrict / No Action: raw SQL `DELETE` is blocked by PostgreSQL `IntegrityError` when attempting to delete companies with leads, leads with drafts, drafts with outbox entries, or agent runs referenced by evaluation results.

4. **PostgreSQL Enum CHECK Constraints**:
   - Parametrized raw SQL test asserting database rejection across all 13 enum columns (`agent_runs.status`, `execution_steps.status`, `execution_steps.verification_status`, `tool_calls.status`, `approvals.status`, `approvals.risk`, `trace_events.kind`, `trace_events.severity`, `evaluation_runs.status`, `leads.status`, `customers.status`, `outreach_drafts.status`, `email_outbox.status`).

**Test suite: 400 passed, 1 skipped** (`cd backend && uv run pytest -v`, with
`DATABASE_URL` pointed at PostgreSQL container — all 34 new invariant tests in
`test_database_invariants.py` pass). `ruff check .`, `ruff format --check .` and
`mypy app` (strict) are 100% clean; `alembic check` reports zero drift.

Next task: **DB-007** (LangGraph Postgres checkpointer wiring, lease heartbeat, and the startup reconciler for orphaned runs) — OPUS, depends on DB-001, AGENT-002.

A leftover from this session: the throwaway Postgres container (`opspilot-pg-dev`, port 55432) remains running; remove with `docker rm -f opspilot-pg-dev` once no longer needed.

## DB-007 — LangGraph checkpointing, leases, heartbeat, crash recovery — 2026-09-13

**Done.** The durable-execution and ownership layer that ADR-004 and ADR-012
promised, recorded as ADR-023. Three modules, one migration, one `env.py`
fix, 57 new tests, all against real Postgres and the real LangGraph saver.

**Checkpointing — `app/persistence/checkpointing.py`.** The locked
`langgraph-checkpoint-postgres` (3.1.2, LangGraph 1.2.11) ships
`AsyncPostgresSaver` over psycopg 3, which is why `psycopg[binary,pool]` sits
next to asyncpg; both derive their target from the one `DATABASE_URL`
(`libpq_conninfo` strips the `+asyncpg` suffix). The saver's DDL is
unqualified, so its tables land wherever `search_path` points — every
checkpointer connection is opened with `-c search_path=langgraph`, which is
what makes ADR-011 literally true (verified: all four `checkpoint*` tables
exist in `langgraph` and nowhere else). `open_checkpointer(settings)` creates
the schema and runs the saver's own `setup()` under a session-level advisory
lock on a dedicated connection, so two API processes booting together
serialize instead of racing `CREATE TABLE IF NOT EXISTS` + the versions insert
(verified with three concurrent opens). `thread_id` is the run id
(`thread_config(run_id)` is the only way to build a config). Every invocation
passes `durability="sync"`: LangGraph 1.x defaults to `"async"`, which submits
step N's checkpoint write in the background while step N+1 starts — the first
draft of the crash test caught exactly that (the killed task had no
checkpoint for `prepare` yet). One round trip per step is the right price for
"the checkpoint is authoritative for resumption".

**Leases — migration `c88ad060adfa`, `SqlAgentRunRepository`,
`app/execution/leases.py`.** §12.3 had `lease_expires_at` but no owner, and
an expiry alone cannot fence a heartbeat — a worker that lost its run could
extend a lease that now belongs to someone else. `agent_runs.lease_owner` is
added with `CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))`.
Four repository operations, each exactly one conditional `UPDATE … RETURNING`:
`acquire_lease` (free, mine, or expired), `heartbeat_lease` (mine and still
live), `release_lease` (mine), `transition_status` (expected status, optional
owner, optional release in the same statement). Under READ COMMITTED two
concurrent `UPDATE`s of one row serialize on the row lock and the loser
re-evaluates its `WHERE` against the winner's committed row — ownership is
decided by Postgres, with no read-then-write anywhere. The expiry boundary is
exact and complementary (`> now` live for heartbeats, `<= now` claimable);
the first draft had `<`/`>=`, which left one instant where both the old
owner could renew and a new one could claim — a boundary test caught it. All
lease time comes from the injected `Clock` as a SQL parameter, so tests expire
leases by moving a `FixedClock`. `Settings` gains `OPSPILOT_LEASE_TTL_SECONDS`
(30) and `OPSPILOT_HEARTBEAT_INTERVAL_SECONDS` (10) with a startup fuse
(`interval * 2 <= ttl`) so one missed beat never orphans a healthy run.
`LeaseHeartbeat` renews on an injectable `sleep`, sets `lost` and calls
`on_lost` on refusal, and survives a transient database error (the next real
round trip decides); `hold_lease` is the acquire/heartbeat/release context
API-007's executor will wrap a graph run in. DB-005's placeholder
`heartbeat_lease(run_id, lease_expires_at)` was replaced (it was unfenced),
and its one caller in `test_repositories.py` updated.

**Reconciler — `app/execution/recovery.py`.** Candidates are exactly the
architecture's query: `queued`/`running` with an expired *or absent* lease.
`awaiting_approval` has no owner by design (§6.3) and is never a candidate —
a stale lease left on a paused row by a crash after the pause is inert.
After an atomic claim, the checkpoint decides (the table in §2.4): none →
`failed(orphaned)`; paused at `interrupt()` → repaired to `awaiting_approval`
(**this is the "died between checkpoint and row write" acceptance case** —
re-entering would only re-raise the interrupt, failing it would discard a
human's pending approval); finished → the terminal status recorded in the
state's `status`/`status_reason` channels (`rejected` stays `rejected`,
ADR-022); mid-execution → resumed under the reconciler's own lease and
heartbeat, then settled the same way; a resume that raises →
`failed(recovery_failed)`; a lease lost mid-resume → the resume is cancelled
and nothing is written; a settling transaction that fails → rolled back as a
unit (status and trace event share one unit of work), the run stays leased by
the reconciler and is a candidate again when that lease lapses. The
reconciler talks to the graph only through a `RunDriver` protocol
(`inspect`/`resume`); `LangGraphRunDriver` implements it over any compiled
graph by classifying `aget_state` into the four phases. One product-trace
kind, `run_recovered` (`status` = `resumed` | `awaiting_approval` |
`<terminal>`, payload = previous owner, expiry, new owner, checkpoint id,
next nodes), records a takeover; lease acquisition/heartbeat/loss and every
reconciler decision are structured logs (§14.6).

**Crash-recovery methodology.** AGENT-002 is unwritten, so
`tests/recovery_harness.py` builds a small graph over the real saver with the
three shapes the state machine distinguishes — a node that can be killed
mid-execution, a node with a keyed mutating effect, and an `interrupt()`
gate — carrying the same `status`/`status_reason` channels as `AgentState`.
Worker death is simulated at the ownership boundary, the smallest faithful
way: the worker's graph task is *cancelled* while `work` is blocked (no
checkpoint for that node), its heartbeat task is stopped, and nothing else is
touched — no release, no status write, exactly what a dead process leaves.
The lease then expires only because the `FixedClock` moves. The acceptance
test asserts all nine steps: a valid lease; a durable checkpoint whose id is
captured before the kill; death mid-`work`; a pass one second before expiry
does nothing; the pass after expiry claims, resumes and completes;
`prepare` ran once across both workers, `work` twice, `finish` once; the
final checkpoint continues the pre-crash one; and `run_recovered(resumed)`
carries the pre-crash checkpoint id. A second test makes `work` insert a real
`mock_crm.email_outbox` row keyed by an attempt-invariant idempotency key
*before* dying: recovery re-executes the node and the real
`UNIQUE(idempotency_key)` yields `duplicate_suppressed` — one row, no
bypass. Concurrency is real: 12 connections racing `acquire_lease` (one
owner), four reconcilers racing over eight crashed runs (each recovered
exactly once, one `run_recovered` each), six resumers racing an approval
(one wins the conditional decision and the lease, one `finish`).

**HITL safety, verified.** A paused run with no heartbeat for six hours is
untouched; a stale lease on a paused row neither makes it a candidate nor
blocks the approval path (the resume claim takes over an expired lease); a
paused checkpoint under a wrongly-`running` row is repaired, and across three
reconciler passes `resume` is never called and `finish` never runs; after
approval, `Command(resume=…)` completes the run.

**Migration `c88ad060adfa`** (`Revises: e35beebb06d0`): adds the column and
`CHECK`, and rewrites `ck_trace_events_trace_event_kind` to admit
`run_recovered` (the constraint names are wrapped in `op.f()` so Alembic does
not apply the naming convention twice). Downgrade deletes `run_recovered`
rows, restores the previous vocabulary, drops the constraint and column;
upgrade / no-op / downgrade / re-upgrade all verified live and encoded as
`TestLeaseOwnerMigration`. **`alembic/env.py` fix:** with
`include_schemas=True`, autogenerate reflected the saver-owned `langgraph`
schema and proposed dropping its tables; `include_name` now restricts
comparison to `opspilot`, `mock_crm` and the default schema, which is ADR-011
made mechanical. FOUND-003's `test_langgraph_schema_is_never_created_by_our_
migrations` asserted the schema's *absence*, which was only true while nothing
had ever opened a checkpointer; it now asserts the invariant that actually
holds — a full downgrade/upgrade cycle neither creates nor drops it.

**Not wired into `create_app` yet, deliberately.** API-007 owns the
`Executor` and "reconcile on startup"; the app lifespan must keep booting
without a database (`test_health.py`), and there is no graph to resume until
AGENT-002. DB-007 delivers the primitives API-007 composes:
`open_checkpointer` in the lifespan, `hold_lease` around each graph run,
`Reconciler(driver=LangGraphRunDriver(graph)).reconcile_once()` at startup.

**Local-dev note.** psycopg's async connection refuses Windows' default
Proactor event loop; `tests/conftest.py` selects a selector loop on `win32`
only (Linux/CI/Docker unaffected). The venv also had a half-installed
`jsonpointer` (dist-info present, module file missing — a cloud-sync
artefact), fixed with `uv sync --reinstall-package jsonpointer`; nothing in
the lockfile changed.

**Test suite: 457 passed, 1 skipped** (`cd backend && uv run pytest` with
`DATABASE_URL` at a reachable Postgres — 57 new: `test_checkpointing.py` 7,
`test_leases.py` 21, `test_recovery.py` 24, `test_migrations.py` 5; without a
database the integration classes skip and the pure-function classes still
run). `ruff check .`, `ruff format --check .`, `mypy app` (strict) and
`alembic check` are all clean.

Next task: **TOOL-001** (done below). **AGENT-002** now has its checkpointer;
it still depends on AGENT-003..008. **API-007** depends on DB-007 (done) and AGENT-002.

### Code — TOOL-001, tool ports, mock adapters, and deterministic seed dataset

| Module | What it provides | Verified by |
|---|---|---|
| `app/integrations/ports.py` | Typed port protocols (`LeadPort`, `CompanyPort`, `CustomerPort`, `DraftPort`, `MailPort`, `ContentPort`), carrier models, and `Adapters` bundle. Mutating methods require `ApprovalToken` | `test_mock_integrations.py::TestProtocolConformance` |
| `app/integrations/mock/adapters.py` | Zero-network mock implementations bound to `mock_crm` persistence with contract-driven readback queries and deterministic failure injection | `test_mock_integrations.py` (search, profile, get/update customer, save/get draft, send email, idempotency replay) |
| `app/integrations/mock/fixtures.py` | 8 companies, 11 leads (including canonical `L-104` "Dana Miller"), and 5 customers strictly bound to RFC 2606 `*.example`/`example.com` domains | `test_mock_integrations.py::TestSeedFixturesAndRFC2606` |
| `app/integrations/mock/seed.py` | `seed_database(session_factory, reset=True)` function and CLI entrypoint for `make seed` (`python -m app.integrations.mock.seed`) | `test_mock_integrations.py::test_seeding_is_idempotent`, manual CLI check |
| `app/integrations/__init__.py` | Factory `build_adapters(settings, ...)` which refuses `IntegrationMode.REAL` with `ConfigurationError` | `test_mock_integrations.py::TestBuildAdapters` |

**Zero network enforcement:** `tests/test_structure.py::test_mock_integrations_cannot_reach_the_network` now runs and passes (was previously skipped).
**Test suite: 494 passed, 0 skipped** (`cd backend && uv run pytest` with `DATABASE_URL` — 36 new tests in `test_mock_integrations.py`, 1 unskipped structural test). `ruff check .`, `ruff format --check .`, `mypy app` (strict) and `alembic check` are all clean.

Next task: **TOOL-002** (`ToolRegistry.dispatch`: input validation → approval gate re-assertion → idempotency key → timeout → dispatch → output validation → trace, as the single choke point) — OPUS.

## TOOL-002 — the single tool dispatch choke point — 2026-09-13

**Done.** `ToolRegistry.dispatch` is the one path from the agent to any tool
implementation and therefore to any mutating port. Recorded as ADR-024;
§8.5 of the architecture now describes what was built.

| Module | What it provides | Verified by |
|---|---|---|
| `app/tools/registry.py` | `ToolRegistry` (contracts + implementations, refuses policy-violating or unsupported-port contracts at construction), `dispatch(run_id, execution_step_id, step_id, tool_name, arguments, attempt, approval_token=None)`, `DispatchResult`/`DispatchOutcome`, `ToolContext` (the only thing an implementation receives: its declared port and the attempt's identity), and the dispatch error family (`UnknownToolError`, `ApprovalRequiredError`, `ApprovalInvalidError`, `ToolNotBoundError`, `ToolTimeoutError`, `DuplicateAttemptError` — each a member of the §10.1 taxonomy) | `tests/test_tool_dispatch.py` (53), `tests/test_tool_registry.py` (14) |
| `app/observability/redaction.py` | `redact_payload`: the §14.5 key denylist, value patterns and byte-budget truncation, applied to every persisted input/output/error before OBS-001's recorder exists | `tests/test_redaction.py` (18) |
| `app/security.py` | `idempotency_key_for(run_id, step_id, args_hash)` — the one derivation (ADR-020) | `test_tool_registry.py`, structural test |
| `app/tools/contracts.py` | `policy_violations(contract)` — P1–P6 as a function, re-asserted by the registry over injected contracts | `test_tool_registry.py` |
| `app/integrations/ports.py` | `PORT_FIELDS` and `Adapters.port(name)` — a contract's declared port resolves to exactly one adapter, fail-closed | `test_tool_registry.py` |
| `app/persistence/protocols.py` / `repositories.py` | `ToolCallRepository.lock_idempotency_key` (transaction-scoped advisory lock, same mechanism as the per-run trace lock) and `list_by_idempotency_key`; `UnitOfWorkFactory` moved here from `app/execution/leases.py` (re-exported there) | `test_tool_dispatch.py::TestRaces` |
| `app/errors.py` | `InternalError` (the `INTERNAL` class had no concrete exception) | — |

**The dispatch sequence, as built.** resolve (unknown → refused before any
I/O) → argument hygiene (a plan may not carry `idempotency_key` or
`approval_token`) → `args_hash` and derived key → gate re-assertion (barrier 2:
token required for gated tools, bound to run/step/hash; a token on an ungated
tool is a violation) → input validation (`extra="forbid"`) → binding
(implementation + declared port) → execution-step identity (the row is this
run, this step, this tool) → attempt-slot check → `tool_started` committed →
stored-decision check (the `approvals` row is `approved` for the same run,
step, tool, hash, risk) → per-key advisory lock (mutating tools) → execute
under `timeout_ms` → output validation → exactly one `tool_calls` row and the
closing `tool_*` event (plus `policy_violation` at `error` severity, and an
`error`-level structured log, when that is what happened).

**Verified rather than asserted**, all against real Postgres and the real
mock adapters, with the human scripted through the real `approvals`
repository and the gate never disabled:

- a read tool dispatches with no approval and records one `succeeded` row
  and a `tool_started`/`tool_succeeded` pair; a pure tool sees no port;
- unknown tool → refused, nothing recorded; malformed input (extra field,
  missing field, wrong type, out of range, contradictory filters) → recorded
  `failed/input_validation` with `adapter=NULL` and a `rejected` trace;
- a gated mutation with no grant → `ApprovalRequiredError(PolicyViolation)`,
  zero outbox rows, `policy_violation` event; with a valid grant → exactly
  one outbox row, one `succeeded` row carrying the derived key, and no
  `approval_token` anywhere in what was persisted;
- grants for another run, another step, another tool, another risk, modified
  arguments (caught by the token) and modified arguments with a forged
  matching token (caught by the row) are all refused; tokens whose row is
  pending/rejected/expired/cancelled/superseded, unknown or malformed are
  stale; a retry with the same grant replays (`duplicate_suppressed`, one
  outbox row, same `message_id`), across plan revisions and across separate
  registry instances;
- integration failures are recorded with their class and `adapter="mock"`;
  timeouts as `timeout`/`transient` via `tool_timeout`; malformed or
  non-model output as `output_validation`; unexpected exceptions as
  `internal` with the cause chained; store unavailability as `transient`; an
  adapter-raised `PolicyViolation` (recipient pinning) as an executed attempt
  that failed loudly; a cancelled attempt is recorded and the cancellation
  propagates;
- a policy rejection and an integration failure of the same tool are
  distinguishable by exception type, `error_class`, adapter and trace status;
- races: six concurrent retries of one approved send → one `succeeded`, five
  `duplicate_suppressed`, one outbox row; four concurrent dispatches of the
  *same* attempt → one executes, three `DuplicateAttemptError`, one row; a
  rejection racing execution never sends; an approval racing execution is
  consistent with the effect (one row iff the dispatcher saw `approved`) and
  a retry converges to one row;
- structural (AST): no module outside `app/integrations/`, `app/tools/impl/`
  and `app/persistence/` calls a mutating port method or references a
  mutation-capable `Adapters` field except as a read receiver (with eight
  bypass canaries proving the scan fires and five read-path canaries proving
  it does not over-fire); `app.integrations.mock` is imported only inside
  `app/integrations/`; `ToolContext` is constructed only by the dispatcher
  and `app.tools.impl` is imported only inside `app/tools/` and `main.py`;
  `authorises`/`grants`/`canonical_args_hash` are called only in the three
  barrier modules and `ApprovalGate.issue` nowhere in the application yet;
  `tool_calls.record_call` and the `TOOL_*` trace kinds are used only by the
  dispatcher; `idempotency_key_for` only by the dispatcher. A live canary
  (`app/agent/_bypass_canary.py` doing `adapters.mail.send`, importing
  `MockMailAdapter` and constructing a `ToolContext`) failed three of them
  and was removed.

**Deliberately not done.** No `rejected` value added to `tool_calls.status`
(no migration; `error_class` + `adapter=NULL` is the distinction — ADR-024).
No change to the TOOL-001 adapters. No `TraceRecorder`/`@traced_node`
(OBS-001), which must adopt `app.observability.redaction` rather than
re-implement §14.5. No tool implementations (TOOL-003): the tests use
scripted implementations over the real ports.

**Test suite: 599 passed, 0 skipped** (`cd backend && uv run pytest` with
`DATABASE_URL` at a reachable Postgres — 105 new: `test_tool_dispatch.py`
53, `test_tool_registry.py` 14, `test_redaction.py` 18, `test_structure.py`
+20 including the canaries). `ruff check .`, `ruff format --check .`,
`mypy app` (strict) and `alembic check` are all clean.

Next task: **TOOL-003** (done below).

## TOOL-003 — the nine tool implementations over ports — 2026-09-13

**Done.** All nine concrete tool implementations under `app/tools/impl/` bound
into `ToolRegistry` by default.

| Module | What it provides | Verified by |
|---|---|---|
| `app/tools/impl/search_leads.py` | `search_leads`: queries `LeadPort.search`, maps filters and returns pagination summaries; empty match returns `leads: []`, `total_matched: 0` | `tests/test_tool_impl.py::TestSearchLeadsTool` |
| `app/tools/impl/get_lead.py` | `get_lead`: queries `LeadPort.get`, returns `LeadDetail` or raises `NotFoundError` | `tests/test_tool_impl.py::TestGetLeadTool` |
| `app/tools/impl/research_company.py` | `research_company`: queries `CompanyPort.profile`, verifies confidence and summary invariants; raises `OutputValidationError` on invalid profile or `NotFoundError` | `tests/test_tool_impl.py::TestResearchCompanyTool` |
| `app/tools/impl/score_lead.py` | `score_lead`: pure deterministic rule engine (§8.4, ADR-009) evaluating company fit, engagement, signal strength and data quality; returns `ScoreBand` (`hot`/`warm`/`cold`) and `factors` breakdown summing to `score` (±1) | `tests/test_tool_impl.py::TestScoreLeadTool` |
| `app/tools/impl/draft_outreach.py` | `draft_outreach`: queries `ContentPort.draft` across direct/warm/formal tones; enforces placeholder (`{{`, `TODO`, `[NAME]`) and length guards with `OutputValidationError` | `tests/test_tool_impl.py::TestDraftOutreachTool` |
| `app/tools/impl/save_draft.py` | `save_draft`: verifies `content_hash == sha256(subject||body)` (rejects mismatch with `PolicyViolation`), persists draft via `DraftPort.save` | `tests/test_tool_impl.py::TestSaveDraftTool` |
| `app/tools/impl/send_email_mock.py` | `send_email_mock`: outbound gated action requiring approval token; passes derived idempotency key and token to `MailPort.send`; adapter rejects recipient mismatch with `PolicyViolation` | `tests/test_tool_impl.py::TestSendEmailMockTool` |
| `app/tools/impl/get_customer.py` | `get_customer`: queries `CustomerPort.get` by id or email; raises `NotFoundError` on missing record | `tests/test_tool_impl.py::TestGetCustomerTool` |
| `app/tools/impl/update_customer.py` | `update_customer`: customer-write gated action requiring approval token; reads pre-update state for undo diff, invokes `CustomerPort.update` under optimistic concurrency; raises `StaleWriteError` on version conflict | `tests/test_tool_impl.py::TestUpdateCustomerTool` |
| `app/tools/impl/__init__.py` | `TOOL_IMPLEMENTATIONS` bundle and `default_implementations()` factory exposing all 9 tools | `tests/test_tool_impl.py::TestRegistryCompleteness` |
| `app/tools/registry.py` | `ToolRegistry.__init__` defaults `implementations=None` to `default_implementations()` while preserving custom injection support | `tests/test_tool_impl.py`, `tests/test_tool_dispatch.py` |

**Test suite: 627 passed, 0 skipped** (`cd backend && uv run pytest` with
`DATABASE_URL` at a reachable Postgres — 28 new tests in `test_tool_impl.py`).
`ruff check .`, `ruff format --check .`, `mypy app` (strict) and `alembic check`
are all clean.

## AGENT-002 — LangGraph execution graph assembly and conditional edges — 2026-09-13

**Done.** Production LangGraph execution graph assembled with all nine nodes and exact static/conditional edges from architecture §6.1. Compiled with `AsyncPostgresSaver` and `MemorySaver`. Dynamic `interrupt()` in `request_approval` with `interrupt_before=[]`, `interrupt_after=[]` (ADR-007).

| Module | What it provides | Verified by |
|---|---|---|
| `app/agent/nodes.py` | Complete `NodeHandlers` class implementing all 9 nodes (`understand`, `plan`, `decide`, `request_approval`, `execute_tool`, `verify`, `recover`, `complete`, `fail`), pluggable boundaries for future tasks, 6 conditional router functions (`route_after_understand`, `route_after_plan`, `route_after_decide`, `route_after_execute`, `route_after_verify`, `route_after_recover`), `create_initial_state` factory, and strict gate re-assertion (barrier 2) before `ToolRegistry.dispatch()` | `tests/test_agent_graph.py` |
| `app/agent/graph.py` | `create_agent_graph` factory building `StateGraph[AgentState]`, wiring all static edges (`START -> understand`, `request_approval -> decide`, `complete -> END`, `fail -> END`) and conditional edges matching §6.1, compiling with checkpointer and empty static interrupt lists | `tests/test_agent_graph.py` |
| `tests/test_agent_graph.py` | Comprehensive test suite covering graph topology (all 9 nodes, all static/conditional edges, no unreachable edges), all routing branches across all 6 decision points, dynamic `interrupt()` / HITL pause-and-resume via `Command(resume=...)`, rejection terminal states (`REJECTED` vs `FAILED`), real Postgres checkpointer sync persistence, and tool safety | `tests/test_agent_graph.py` |
| `tests/test_structure.py` | Added AST invariants: only `execute_tool` invokes `ToolRegistry.dispatch()`, and static interrupt lists (`interrupt_before`/`interrupt_after`) are forbidden across the agent graph (ADR-007) | `tests/test_structure.py` |

**Test suite: 658 passed, 0 skipped** (`cd backend && uv run pytest` with
`DATABASE_URL` at a reachable Postgres — 29 new tests in `test_agent_graph.py`,
2 new structural checks in `test_structure.py`).
`ruff check .`, `ruff format --check .`, `mypy app` (strict) and `alembic check`
are all clean.

## AGENT-003 — production `understand` node and deterministic `RuleTaskNormalizer` — 2026-09-14

**Done.** Deterministic task understanding engine and production `understand` node delegating to an injected `TaskNormalizer` protocol. Zero external I/O, pure and reproducible. Canonical request produces `industry='fintech'`, `location='London'`, `limit=3'`, `requires_mutation=True`, `in_scope=True`, and canonical intent `prospect_and_outreach`.

| Module | What it provides | Verified by |
|---|---|---|
| `app/agent/normalizer.py` | `TaskNormalizer` protocol, `CanonicalIntent` taxonomy (`prospect_and_outreach`, `lead_search`, `lead_lookup`, `company_research`, `lead_scoring`, `draft_outreach`, `customer_lookup`, `customer_update`, `out_of_scope`), and pure deterministic `RuleTaskNormalizer`. Extracts industry, location, limit, lead/company/customer IDs, email, and customer patch fields. Evaluates `requires_mutation`. Treats prompt injections strictly as data. Rejects off-domain, destructive, admin, unsupported CRM operations, empty, and gibberish queries early with `in_scope=False`, `confidence=0.0`, and stable rejection notes. | `tests/test_understand.py` |
| `app/agent/nodes.py` | Updated `NodeHandlers.__init__` to accept `normalizer: TaskNormalizer | None = None` (defaulting to `RuleTaskNormalizer()`). Implemented `understand` node to delegate normalization when `normalized_task` is absent, setting `status=RunStatus.RUNNING` and `status_reason="out_of_scope"` for out-of-scope requests to route cleanly via `route_after_understand` to `fail -> END`. | `tests/test_understand.py`, `tests/test_agent_graph.py` |
| `app/agent/graph.py` | Added optional `normalizer: TaskNormalizer | None = None` parameter to `create_agent_graph` factory forwarding directly to `NodeHandlers`. | `tests/test_understand.py` |
| `tests/test_understand.py` | 32 comprehensive tests covering the canonical request, entity extraction (industry, location, limit, UUIDs, IDs, emails, field updates), mutation detection, supported taxonomy, empty/whitespace/gibberish rejection, destructive/admin rejection, prompt-injection safety, determinism across repeated executions, graph routing (`in_scope -> plan`, `out_of_scope -> fail -> END` with `status_reason="out_of_scope"` and zero tool/plan calls), real PostgreSQL checkpointer persistence, and AST structural purity invariants (no port/mock/sql imports). | `tests/test_understand.py` |

**Test suite: 690 passed, 0 skipped** (`cd backend && uv run pytest` with
`DATABASE_URL` at a reachable Postgres — 32 new tests in `test_understand.py`).
`ruff check .`, `ruff format --check .`, `mypy app` (strict) and `alembic check`
are all clean.

Next task: **AGENT-004** (done below).

## AGENT-004 — the `decide` router: seven ordered rules plus fan-out expansion — 2026-09-14

**Done.** The safety router of §6.2 as one pure function, and the fan-out
expansion engine of §4.5/ADR-006, wired into the existing graph topology
without changing any other node.

| Module | What it provides | Verified by |
|---|---|---|
| `app/agent/decide.py` | `evaluate_decision(state, *, now, contract_for, resolve_args) -> Decision`: the seven rules in a single ascending sequence — (1) `deadline_at` passed or `step_count ≥ MAX_STEPS` → `fail(budget_exhausted)`; (2) undecided pending approval → `request_approval`; (3) no runnable step → `complete`; (4) dependencies unsatisfied or fan-out unresolvable → `plan` while `replan_count < MAX_REPLANS`, else `fail(unresolvable_plan)`; (5) unexpanded fan-out → expand, re-evaluate from 1; (6) `requires_approval` and no grant for the arguments *as they will now be sent* → `request_approval`; (7) → `execute_tool`. A §5.4 lifecycle guard ahead of rule 1 routes an already-terminal run to its terminal node, never into execution or a pause. "No runnable step" includes a required step a human rejected (§9.6), so nothing after it executes. `Decision.state_delta()` writes only `current_step_id`, the expanded `plan` and `status_reason` (rules 6/7 clear a stale advisory reason; rules 2/3 leave it alone). Re-evaluation after expansion is bounded by the number of fan-out steps. | `tests/test_decide.py` |
| `app/agent/fanout.py` | `plan_fanout_expansion` resolves `fanout.over` (`<step>.output[.key or index…]`, must land on a list), binds `{"$ref": "<as>[.path]"}` inside the parent's args to literals per item, truncates to `max_items` in list order and builds children `s2[0]`, `s2[1]`, … (same tool, copied `depends_on`/`optional`/`rationale`, `parent_step_id` set, `fanout=None`); `apply_expansion` places them immediately after the parent, marks the parent `succeeded` (its job — producing children — is done) and is idempotent: existing children are kept in canonical position, never duplicated. A dependency on an expanded parent is satisfied only once every child has succeeded. Any resolution or binding failure is `FanOutResolutionError`, a `ReferenceResolutionError` (`reference_resolution` — a planning fault, §4.4), so the whole expansion is all-or-nothing. No general `$ref` resolver: step references are left for `execute_tool` (AGENT-005). | `tests/test_fanout.py` |
| `app/agent/nodes.py` | `decide` and `route_after_decide` both call `_evaluate_decision` (clock reading, `_get_contract`, `_resolve_step_args` injected), so the node's delta and the conditional edge can never disagree; the AGENT-002 placeholder rules and helpers are removed. No other node changed. | `tests/test_decide.py::TestDeterminismAndPurity`, `tests/test_agent_graph.py` |
| `tests/test_structure.py` | `agent/decide.py` admitted to the set of modules allowed to ask `ApprovalState.grants` (it *is* the router barrier of §9.5). | `tests/test_structure.py` |

**Acceptance criteria verified.** Each rule in isolation with every boundary
(`deadline_at == now` is not passed; `step_count == MAX_STEPS` is; `replan_count
== MAX_REPLANS` fails; `max_items` exact vs. one over). Ordering by a precedence
ladder in which rules 1, 2, 4, 5 and 6 are simultaneously true and conditions
are removed one at a time; the acceptance case — budget-exhausted **and**
approval-requiring — fails and does not pause, for both `MAX_STEPS` and
`deadline_at`. Expanded children count against `MAX_STEPS`: with `max_steps=3`
the graph executes `s1`, `s2[0]`, `s2[1]` and fails `budget_exhausted` before
`s2[2]`. On `MemorySaver`: a three-lead fan-out executes every child in order
with bound arguments; each gated child pauses for its own approval and the
two re-entries add no duplicate child; rejecting a required child ends the run
`rejected` without executing the rest; an optional rejected child is skipped
(`partial=True`); an unresolvable fan-out replans exactly `MAX_REPLANS` times
(each revision kept) then fails `unresolvable_plan`; the same run twice is
identical. On the real Postgres saver with the real registry and seeded mock
CRM: `search_leads` → fan out `get_lead` over `s1.output.leads`, the children
dispatch through `ToolRegistry.dispatch` with real lead ids and the expanded
plan survives the checkpoint round trip. Structural: both router modules are
synchronous, import nothing I/O-capable (no langgraph, sqlalchemy, httpx,
anthropic, persistence, integrations, registry), call no `dispatch`/`send`/
`update`/`save`, and `evaluate_decision` cites `DecisionRule` members in
strictly ascending source order; `decide`/`route_after_decide` contain no
branching of their own; no AGENT-005+ module or LLM import appeared under
`app/agent/`.

**Decisions.** The parent of an expansion stays in the plan (readability, and
`step_id` stability for later `$ref`s and `depends_on`) and is marked
`succeeded` rather than a new `StepStatus` value, because `execution_steps.
status` carries a `CHECK` over the existing eight values and no migration is
warranted. Over-limit lists are truncated to `max_items` in list order (the
bound the architecture names); `MAX_STEPS` is enforced as children execute,
via rule 1 on every pass, not by refusing the expansion. Rule 4 records its
fault in `status_reason` (`replan_required`/`unresolvable_plan`) and does not
append to `errors`, because `route_after_execute` keys on `errors[-1]` and a
decide-time entry would misroute a later successful attempt of the same step
into `recover`; AGENT-006's planner can carry the detail when it lands.

**Deliberately not done.** No `fanout_expanded` trace event (OBS-001). No
change to `request_approval`: a resume whose decision carries a stale
`args_hash` would be re-routed by rule 6 to a new request, but the node's
early return on an existing decision would short-circuit it — unreachable in
the current graph (nothing changes arguments between the pause and the
re-decide) and HITL-003's supersede path owns it. No general `$ref` resolver,
planner, responder, verifier or cancellation (AGENT-005..009).

**Test suite: 902 passed, 0 skipped** (`cd backend && uv run pytest` with
`DATABASE_URL` at a reachable Postgres — 212 new: `test_decide.py` 174,
`test_fanout.py` 38). `ruff check .`, `ruff format --check .`, `mypy app`
(strict) and `alembic check` are all clean.

---

### AGENT-005 — `execute_tool` argument reference resolution and execution pipeline

Implemented the reference resolver and integrated it into the `execute_tool` pipeline while preserving all approval invariants.

| File | What was built | Tests |
|---|---|---|
| `app/agent/resolver.py` | `parse_ref_path`, `resolve_ref_path`, `resolve_value`, `resolve_step_args`: deterministic path navigation (`<step>.<path>`, dot and bracket syntax e.g. `leads[0].id`), value deepcopy isolation, self-reference / circularity detection, and `ReferenceResolutionError` (class `REFERENCE_RESOLUTION`, recovery `REPLAN`) on missing step, unpopulated field, out-of-bounds list index or type error. Pure function over state/results, zero I/O, zero DB/network/LLM. | `tests/test_resolver.py` |
| `app/tools/resolver.py` | Clean re-export surface satisfying the TOOL-004 interface specification without duplicated logic. | `tests/test_resolver.py` |
| `app/agent/nodes.py` | Updated `execute_tool` to follow the architecture-mandated sequence: (1) resolve `$ref` arguments from `tool_results`, (2) validate resolved arguments against `contract.input_model`, (3) re-assert approval gate via `approval_state.grants(current_step_id, resolved_args)` using the canonical hash of final resolved arguments, (4) dispatch through `ToolRegistry.dispatch()`. | `tests/test_resolver.py` |
| `app/agent/graph.py` | Added optional `arg_resolver` injection to `create_agent_graph`, forwarding to `NodeHandlers`. | `tests/test_agent_graph.py` |

**Invariants verified.**
1. In-memory approval evaluated strictly against canonical hash of final resolved arguments.
2. Resolution failure fails closed: records `AgentError` with recovery `REPLAN` and halts execution before any tool dispatch.
3. Source `ToolResult`s are never mutated in place.
4. Fan-out expanded children (e.g. `s2[0]`, `s2[1]`) resolve references to parent/earlier step outputs cleanly.
5. Structural AST invariants: resolver performs no I/O, no DB imports, and no approval minting outside `security.py`.

**Test suite: 951 passed, 0 skipped** (`cd backend && uv run pytest` with `DATABASE_URL` at a reachable Postgres — 49 new tests in `test_resolver.py`). `ruff check .`, `ruff format --check .`, `mypy app` (strict) and `alembic check` are all clean.

Next task: **AGENT-006** (done below).

---

## AGENT-006 — the planner layer: `Planner` protocol, `RulePlanner`, `LLMPlanner`, validation, one-shot repair — 2026-09-14

**Done.** `normalized_task → Planner.plan → validate_plan → Plan → decide`,
with the model treated as an untrusted proposer behind one deterministic
validator, and the provider switched to Groq (ADR-025).

| Module | What it provides | Verified by |
|---|---|---|
| `app/agent/planner/protocol.py` | `Planner` protocol — `plan(task, prior, *, budgets) -> Plan` plus an `identity` (`PlannerIdentity(kind, model_id, prompt_version)`) for the run record; `PlanRevisionContext` (previous plan, `replan_count`, `reason`, `failed_step_id`, classified errors, `settled_step_ids`) — no tool output ever travels to a planner. `normalize` stays with AGENT-003's `TaskNormalizer`. | `tests/test_planner.py` |
| `app/agent/planner/validation.py` | `validate_plan(plan, *, task, contracts, budgets) -> list[PlanIssue]` / `assert_valid_plan`, `PlanValidationError` (`planner_error`). Registered tool; intent allowlist (`INTENT_TOOLS`: customer intents never reach lead tools, read-only intents never reach a mutating tool, `lead_search` gains outreach only with `requires_mutation`); well-formed unique ids; dependencies exist, precede, no cycle (iterative DFS); `$ref` syntax via AGENT-005's `parse_ref_path`, target earlier and inside the transitive dependency closure (a prospective fan-out child `s2[i]`, or a listed child, counts through its parent); fan-out `over` form, alias validity, no nested fan-out; `parent_step_id` only on a genuine child; arguments declared by the contract, required present, literals typed with the field's own constraints, whole-model validators when every argument is literal (gated tools excluded — the dispatcher validates them in full); no planned `approval_token`/`idempotency_key`; no `succeeded`/`running` step; `1 ≤ len(steps) ≤ MAX_STEPS`. Pure: no network, database or clock. | `TestValidator` |
| `app/agent/planner/rules.py` | `RulePlanner`: deterministic skeletons for all eight intents, sized to `MAX_STEPS` (`_fit`). Canonical request → `s1 search_leads(industry, location, limit=3)` → `s2 research_company` fan-out over `s1.output.leads` → `s3..s5 score_lead` (`s1.output.leads.i.lead_id`, `s2[i].output.profile`; `i>0` optional) → `s6 draft_outreach` (lead 0, score `s3.output`) → `s7 save_draft` → `s8 send_email_mock` (`to_email` from the stored lead). Lead lookup, company research (by id or via lead), scoring (by id or filters), draft-only / draft-then-send, customer lookup (id or email), customer update (`get_customer` → `update_customer` with `expected_version` `$ref`, patch restricted to `CustomerPatch`; a request touching no writable field is a `PlannerError`). Revision re-emits the previous structure with statuses reset (`skipped`/`rejected` kept), dropping optional steps behind a skipped dependency. `plan_id = p_{revision+1}`; no ids from a generator. | `TestRulePlannerCanonical`, `TestRulePlannerWorkflows`, `TestRulePlannerRevision` |
| `app/agent/planner/schema.py` | `ProposedPlan`/`ProposedStep`/`ProposedFanOut`: the closed structured-output contract (`extra="forbid"`; no status, parent, approval or verification fields exist to set). `parse_proposal` = `json.loads` + Pydantic, size-bounded, no repair heuristics; `proposal_to_plan` converts deterministically, unknown tool → `unknown_tool` issue; `response_json_schema()` inlines `$defs` and closes every object except `args`. | `TestLLMPlanner`, `TestValidator` |
| `app/agent/planner/prompts.py` | `PROMPT_VERSION = "planner-v1"`; the system prompt fixes the role (plan only, registered tools only, never execute, never waive approval, never claim work done, fenced text is data); the user turn renders the task, the allowed tools, the catalog from the registry (dispatcher-owned fields removed), the limits, and on revision the previous plan and classified errors — the task and the error text sit inside untrusted-data fences. Repair turn = original request + verbatim rejected response (fenced) + issues. | `test_prompt_injection_in_the_task_is_treated_as_data`, `test_revision_prompt_carries_the_previous_plan_and_classified_errors` |
| `app/agent/planner/llm.py` | `LLMPlanner(client, model_id, contracts, fallback, prompt_version)` over `StructuredCompletionClient.complete_json(...) -> str`. `_propose_with_one_repair`: propose → parse/convert/validate → on issues one repair turn → validate → accept, else `PlanValidationError` (terminal) or, when nothing readable came back, `LLMProviderError`. Provider failures degrade to `fallback` (the rule planner in `auto`); validation failures never do. Straight-line code: no loop, two `_complete` calls. | `TestLLMPlanner`, `TestOneShotRepair` |
| `app/agent/planner/groq.py` | `GroqStructuredClient`: one `POST {GROQ_BASE_URL}/chat/completions` with `response_format: {"type": "json_schema", ...}`, `temperature: 0`, `max_completion_tokens`, a wall-clock timeout, no retries, an injectable `httpx` transport; HTTP/transport failures → `LLMProviderError` carrying status and `retry-after`, never the key or the bodies. The only module under `app/` that imports an HTTP client. | `TestGroqStructuredClient` (`httpx.MockTransport`) |
| `app/agent/planner/context.py` | `revision_requested` (`status_reason ∈ {replan_required, replannable_fault}` with a plan present), `build_revision_context` (settled = steps with a result minus the faulted step, minus its read chain on `stale_write` §10.3), `carry_over_settled_steps` (unchanged settled steps and unchanged expansions whose children are all listed become `succeeded`; the input plan is not mutated). | `TestRevisionContext`, `TestPlanNodeIntegration` |
| `app/agent/planner/factory.py` | `build_planner(settings)`: `rules` → `RulePlanner`; `auto` without a key → `RulePlanner` (logged once); `auto` with a key → `LLMPlanner` with the rule fallback; `llm` → `LLMPlanner` without one. | `TestPlannerSelection` |
| `app/agent/nodes.py` | `plan` delegates to the injected `Planner` (default `RulePlanner`), re-validates whatever comes back, keeps a plan supplied at run creation (validated, not replaced), appends the previous revision to `plan_history` and advances `replan_count` on revision, records `planner_kind`/`model_id`/`prompt_version` on `metadata`, clears `status_reason`, and on any failure returns `invalid_plan` (validation) or `planner_error` (`PLANNER_ERROR`; an unexpected exception is `INTERNAL`) with an `AgentError` and no plan — `route_after_plan` sends both to `fail`. `route_after_execute` now keys on the latest attempt of the current step, so a successful re-execution after a replan (or retry) is not misrouted into `recover` by the older error at the tail of `errors`. `create_agent_graph(planner=...)` forwards the injection. | `TestPlanNodeIntegration`, `test_route_after_execute_keys_on_the_latest_attempt` |
| `app/config.py`, `.env.example`, `app/observability/redaction.py`, `pyproject.toml`, `uv.lock` | `GROQ_API_KEY` (`SecretStr`), `GROQ_MODEL=openai/gpt-oss-120b`, `GROQ_BASE_URL`, `OPSPILOT_LLM_TIMEOUT_SECONDS` replace the Anthropic fields; `has_groq_key` drives `effective_planner` and the `llm`-without-key fuse. `gsk_…` values are redacted. `anthropic` removed from the dependencies (never imported); `httpx` promoted to a runtime dependency. | `tests/test_config.py`, `tests/test_health.py`, `tests/test_redaction.py` |

**Acceptance criteria verified.** An LLM plan naming an unknown tool is an
`unknown_tool` issue fed back in the one repair turn and never dispatched
(the scripted executor sees nothing; `dispatch` is absent from the whole
package by AST). An invalid plan is repaired at most once then fails: the
scripted client's third answer is never consumed, six invalid answers across
three `plan()` calls produce exactly six model calls, and
`_propose_with_one_repair` contains no loop and exactly two `_complete`
calls. Injected instructions never alter the plan: task text is fenced and
labelled data, the catalog lists only the intent's tools, a model that
"obeys" an injection (`update_customer` on a lead task, `requires_approval:
false`, a planted `approval_token`) is rejected by the validator or the
closed schema, and `research_company`'s injected `summary` flows through a
`$ref` as data into the next tool. `planner_kind` is recorded on the plan
(`created_by`) and on the run (`metadata.planner_kind`, with `model_id` and
`prompt_version` for LLM plans; a degraded plan records `rules`).

**Graph behaviour.** On `MemorySaver`: the canonical request plans, expands
the research fan-out, executes `s1, s2[0..2], s3..s7` with every `$ref`
resolved by AGENT-005's resolver (the draft receives `s2[0]`'s profile and
`s3`'s score; the send receives `d_1` and `lead0@example.com`), pauses for
`s8`, completes on approval with `step_count == 10` and no errors. A
`not_found` on `s2` replans once, carries `s1` over, re-executes only `s2` and
completes (`replan_count == 1`, `plan_history == [p_1]`, plan `p_2`). A
`$ref` the world never satisfies (`leads.0` after an empty search) faults
identically three times and fails once `MAX_REPLANS=2` is spent, each
revision kept and the empty expansion carried over. A supplied plan is kept
and the planner never asked. Invalid and failing planners route to `fail`
with nothing executed. On the real Postgres saver with the real registry:
"Find the top 2 leads in Seattle, research them and score them" plans,
searches, fans out over the two Northwind leads, scores lead 0 from
`s2[0].output.profile` and completes; the plan and `metadata.planner_kind`
survive the checkpoint round trip.

**Decisions.** Planning is expressed against the fan-out engine as built:
a fan-out binds only its alias, so per-lead chains use one fan-out for the
research and explicit indexed score steps referencing `s2[i]` (validated as
prospective children). "The best one" cannot be selected inside a plan —
the `$ref` language has no expressions and no tool ranks — so the rule
planner targets the first returned lead and says so; recorded as open
question Q7 rather than resolved implicitly. The intent allowlist is
hint-independent for the outreach intents (the human gate bounds what can
*happen*; the allowlist bounds what can be *proposed*) and uses
`requires_mutation` only to extend `lead_search`. `fanout.max_items` is
bounded by its model (1..50), not by `MAX_STEPS`, per AGENT-004's decision
that the step budget is enforced as children execute. The plan validator is
applied to supplied plans too, which is why it lives in the node and not
only in the planners. `route_after_execute` was hardened because the replan
path this task delivers is otherwise cut short: after a successful
re-execution the older error would route to `recover`, spend the remaining
replan budget and fail a run that had just succeeded.

**Deliberately not done.** No backoff, retry mechanics or `recover`
rewrite (AGENT-007); no responder or terminal status computation
(AGENT-008); no cancellation (AGENT-009); no HITL endpoints; no verifiers;
no `plan_created`/`plan_revised` trace events (OBS-001); no run-creation
service that stamps `RunMetadata` from `Settings` (API-007 composes
`build_planner`). The Groq request shape is exercised against
`httpx.MockTransport` only; the opt-in live smoke test
(`OPSPILOT_LIVE_LLM=1` + `GROQ_API_KEY`) is the first thing to run once a
key exists.

**Test suite: 1095 passed, 1 skipped** (`cd backend && uv run pytest` with
`DATABASE_URL` at a reachable Postgres — 144 new in `tests/test_planner.py`,
the skip is the opt-in live smoke test). `ruff check .`, `ruff format --check
.`, `mypy app` (strict) and `alembic check` are all clean.

## AGENT-007 — `recover` node wired to `recovery_action`, with backoff via the injected clock — 2026-09-14

Implemented the production `recover` node mechanics, retry counting, exponential backoff calculation, virtual clock delays, and deterministic graph routing exactly per §10, ADR-001, and FOUND-004.

| File | Role | Tests |
|---|---|---|
| `app/runtime.py` | `Clock.sleep(seconds: float)` protocol addition, `SystemClock.sleep` (asyncio-backed), `FixedClock.sleep` (virtual advance without wall-clock wait, recording `slept_seconds` and `sleep_calls`) | `tests/test_runtime.py` |
| `app/agent/nodes.py` | Full `NodeHandlers.recover` and `NodeHandlers.route_after_recover`: local error classification via `recovery_action` (§10.2), deterministic retry backoff with `backoff_delay_ms` (§10.4) honoring `retry_after_ms` / `retry_after` hints and optional `SeededRandom` jitter, virtual sleep via `_clock_sleep`, crash/resume retry idempotency guard, optional step skipping (`StepStatus.SKIPPED`, `status_reason='optional_step_skipped'`), bounded replan routing without double-counting `replan_count`, and terminal failure routing on budget exhaustion (`MAX_RETRIES`, `MAX_REPLANS`, `MAX_STEPS`, `deadline_at`) or unrecoverable error | `tests/test_recover.py`, `tests/test_agent_graph.py` |
| `app/agent/graph.py` | `create_agent_graph` parameter forwarding: `retry_base_delay_ms=250`, `retry_max_delay_ms=8000`, `seeded_random`, and `sleep` injectable into `NodeHandlers` | `tests/test_recover.py` |
| `tests/test_recover.py` | 34 comprehensive unit and graph integration tests: retry increments, attempt bounds (`1 + MAX_RETRIES`), exponential growth and max delay cap, server hints, seeded jitter, virtual clock delay, replan routing and budget exhaustion, optional step skipping vs required step failure, terminal failure, boundary conditions (`max_retries=0`, `max_replans=0`, deadline exceeded, stale error fail-closed), crash/resume idempotency, full LangGraph integration over `MemorySaver`, and AST checks for no tool dispatch, no mock adapters, and no ORM in `recover` | `tests/test_recover.py` |

**Recovery semantics verified.**
1. `RETRY`: retryable error classes route `recover -> execute_tool` while `retry_count[step] < MAX_RETRIES`. Retries increment `retry_count[step]` exactly once per failed attempt, evaluate exponential backoff `min(base * 2^(attempt-1), max)`, and advance the injected clock without wall-clock blocking. Permanently failing tools stop at exactly `1 + MAX_RETRIES` attempts.
2. `REPLAN`: replannable errors route `recover -> plan` while `replan_count < MAX_REPLANS`. The error is preserved in state history for planner consumption; `recover` does not touch `replan_count` (the `plan` node increments on revision).
3. `SKIP`: optional step failures route `recover -> decide` with `StepStatus.SKIPPED` and `status_reason='optional_step_skipped'`, leaving telemetry and prior attempts intact. Required step failures cannot skip and route to `plan` or `fail`.
4. `FAIL`: non-recoverable error classes (`POLICY_VIOLATION`, `INTERNAL`), exhausted budgets, or already-terminal runs route `recover -> fail`. Terminal states cannot be resurrected.
5. `Crash/Resume Idempotency`: re-entering `recover` after checkpoint persistence where `retry_count[step]` was already updated for the current attempt does not double-increment and does not double-sleep.

**Deliberately not done.** No responder or terminal status computation (`complete`/`fail` response synthesis, AGENT-008); no cooperative cancellation or budget sweeper (AGENT-009); no HITL UI/API; no verifier ports.

**Test suite: 1131 passed, 1 skipped** (`cd backend && uv run pytest` with `DATABASE_URL` pointing to PostgreSQL container on port 55432 — 34 new in `tests/test_recover.py`, 2 new in `tests/test_runtime.py`, the skip is the opt-in live smoke test). `ruff check .`, `ruff format --check .`, `mypy app` (strict, 62 source files), and `alembic check` are all clean.

## AGENT-008 — `complete` and `fail` nodes plus `Responder`; terminal status computation including `partial` and `unconfirmed` — 2026-09-14

Implemented the production terminal response layer for OpsPilot AI across `complete` and `fail` nodes exactly per §7, §10.6, §11.4, ADR-001, and Invariants P1–P7.

| File | Role | Tests |
|---|---|---|
| `backend/app/agent/nodes.py` | Implementation of `synthesize_complete_response`, `synthesize_fail_response`, `format_failure_explanation`, `is_required_step_rejected`, `sanitize_text`, `NodeHandlers.complete`, and `NodeHandlers.fail`. Categorizes steps into `done`, `not_done`, `unconfirmed`, and `pending`. Enforces Invariant P5 (`UNCONFIRMED` / `FAILED` verification results never fold into `done`). Handles required step rejections (`RunStatus.REJECTED`, `status_reason="approval_rejected"`), partial completions (`partial=True` when optional steps skipped), unconfirmed outcomes, and successful completions (`RunStatus.COMPLETED`). In `fail`, preserves machine-readable `status_reason`, maps human-readable explanations safely, scrubs credentials via regex sanitization, and flags `partial=False`. | `tests/test_responder.py`, `tests/test_agent_graph.py` |
| `backend/tests/test_responder.py` | 40 comprehensive unit, security, safety, integration, and structural tests: rejection outcome and partitioning, normal completion outcome and summaries, partial completion when optional steps are skipped, Invariant P5 enforcement for unconfirmed mutations, failure status preservation, human-readable explanations, credential redacting (Bearer tokens, API keys, passwords, secrets), graph execution over `MemorySaver` to terminal states, and AST checks verifying zero tool dispatch, zero mock adapter imports, zero ORM operations, and zero unconstrained LLM calls. | `tests/test_responder.py` |

**Terminal Response Semantics Verified.**
1. **Rejection Outcome**: If any required step was rejected (`StepStatus.REJECTED` or `ApprovalDecisionKind.REJECT`), `complete` transitions the run to `RunStatus.REJECTED` with `status_reason="approval_rejected"`, names rejected and incomplete steps in `not_done`, and preserves already completed steps in `done`.
2. **Normal Completion**: When all steps complete without errors or rejections, `complete` transitions to `RunStatus.COMPLETED` with a clean summary, populates `done`, and keeps `partial=False`.
3. **Partial Completion**: When optional steps are skipped (`StepStatus.SKIPPED`), `complete` transitions to `RunStatus.COMPLETED` with `partial=True`, summarizing completed vs skipped steps.
4. **Invariant P5 (Unconfirmed Mutations)**: Any step whose verification result is `UNCONFIRMED` or `FAILED` is strictly placed into `unconfirmed` and never reported as `done`.
5. **Deterministic Failure Response**: `fail` transitions to `RunStatus.FAILED`, strictly preserves the machine-readable `status_reason` without overwriting with generic text, maps safe explanations into `FinalResponse.summary`, and scrubs credentials.
6. **Architectural Isolation**: AST checks verify that the response layer never dispatches tools, never queries the ORM, and never executes unconstrained LLM calls.

**Deliberately not done.** No budget enforcement sweeper or cooperative cancellation (AGENT-009); no HITL endpoints or UI.

**Test suite: 1171 passed, 1 skipped** (`cd backend && uv run pytest` with `DATABASE_URL` pointing to PostgreSQL container on port 55432 — 40 new in `tests/test_responder.py`, the skip is the opt-in live smoke test). `ruff check .`, `ruff format --check .`, `mypy app` (strict, 62 source files), and `alembic check` are all clean.

## AGENT-009 — Cooperative cancellation and budget termination at node boundaries — 2026-09-14

Implemented production cooperative cancellation for OpsPilot AI across all graph node-entry boundaries exactly per §13.2, ADR-001, ADR-006, and Invariants P1–P7.

| File | Role | Tests |
|---|---|---|
| `backend/app/runtime.py` | Added `CancellationSource` protocol (`is_cancelled(run_id) -> bool | Awaitable[bool]`) and production thread-safe `InMemoryCancellationSource` with atomic set tracking and synchronous/async query support. | `tests/test_cancellation.py` |
| `backend/app/agent/decide.py` | Updated `evaluate_decision` (§6.2, ADR-006) Lifecycle Guard to check `status_reason in ("cancelled", "operator_cancelled")` or `status == RunStatus.CANCELLED` ahead of rule 1 (budgets), routing cleanly to `DecisionRoute.FAIL` with `rule=DecisionRule.LIFECYCLE_GUARD`. Preserves the 7-rule router sequence and pure evaluation contract without conditional branching AST drift. | `tests/test_cancellation.py`, `tests/test_decide.py` |
| `backend/app/agent/nodes.py` | Added `cancellation_source` injection to `NodeHandlers`. Added node-entry boundary checks `_is_cancelled` (async) and `_is_cancelled_sync` across `understand`, `plan`, `request_approval` (entry and post-`interrupt`), `execute_tool`, `verify`, `recover`, and `complete`. Added sync cancellation checks to conditional routers `route_after_understand`, `route_after_plan`, `route_after_execute`, `route_after_verify`, and `route_after_recover`. Added `"cancelled"` and `"operator_cancelled"` reason mapping to `REASON_EXPLANATIONS`. | `tests/test_cancellation.py`, `tests/test_agent_graph.py` |
| `backend/app/agent/graph.py` | Forwarding `cancellation_source` in `create_agent_graph` to `NodeHandlers`. Preserved 9-node graph topology and no static interrupt lists. | `tests/test_cancellation.py` |
| `backend/tests/test_cancellation.py` | 24 comprehensive unit, safety, concurrency, checkpointing, and structural AST tests covering all cancellation mechanics. | `tests/test_cancellation.py` |

**Cancellation Semantics Verified.**
1. **Cooperative Node-Entry Check**: Cancellation is observed at entry boundaries for all meaningful nodes (`understand`, `plan`, `decide`, `request_approval`, `execute_tool`, `verify`, `recover`, `complete`). Resilient routers route directly to `fail -> END`.
2. **Critical Tool In-Flight Safety**: A tool already dispatched into execution is **never** interrupted mid-flight. No `task.cancel()`, thread interruption, or process termination is used against active tools. The active tool finishes normally, its output/call telemetry is recorded in state, and cancellation is observed at the subsequent node boundary (`decide` / `route_after_execute`).
3. **Clean Terminal Failure**: Honors `RunStatus.FAILED` with `status_reason="cancelled"`, synthesizing a structured `FinalResponse` detailing completed and unrun steps.
4. **Approval Safety & Pause/Resume Durability**: If cancellation is pending before `request_approval`, no pause occurs; if cancelled while paused awaiting approval, resuming via `Command(resume=...)` exits cleanly to `fail(cancelled)` without dispatching the tool, without prompting the operator, and without converting cancellation into an approval rejection (`RunStatus.REJECTED` remains distinct).
5. **Precedence Over Recovery & Budgets**: Cancellation strictly suppresses retries, plan revisions, and optional-step skips in `recover` without erasing prior error history. Cancellation reason takes precedence over step/replan/deadline budget exhaustion.
6. **Terminal Invariant Protection**: Completed (`RunStatus.COMPLETED`) and rejected (`RunStatus.REJECTED`) runs can never be converted to cancelled or failed.
7. **Concurrency & Idempotency**: Repeated or concurrent cancellation requests against the same run ID are safe and idempotent.

**Deliberately not done.** No HITL API/UI endpoints (HITL-001..005); no verifier frameworks (VERIFY-001..003); no public API routes (API-001..007).

**Test suite: 1195 passed, 1 skipped** (`cd backend && uv run pytest` with `DATABASE_URL` pointing to PostgreSQL container on port 55432 — 24 new in `tests/test_cancellation.py`, the skip is the opt-in live smoke test). `ruff check .`, `ruff format --check .`, `mypy app` (strict, 62 source files), and `alembic check` are all clean.

AGENT-009 is the final agent core node task. All 9 core agent nodes (`understand`, `plan`, `decide`, `request_approval`, `execute_tool`, `verify`, `recover`, `complete`, `fail`) and their lifecycle, routing, and safety mechanisms are complete and verified.

## HITL-001 — HTTP Approval API and RFC 9457 Error Handling — 2026-09-15

Implemented the HTTP approval API for OpsPilot AI exposing approval queue and decision endpoints adhering strictly to §9, §13, RFC 9457 Problem Details, ADR-001, ADR-007, and ADR-023.

| File | Role | Tests |
|---|---|---|
| `backend/app/errors.py` | Added domain exception hierarchy for approval and lease states: `ApprovalNotPendingError`, `ApprovalExpiredError`, `ApprovalSupersededError` (all inheriting from `ApprovalConflictError`), and `RunNotResumableError` (inheriting from `LeaseAcquisitionError`). | `tests/test_api_approvals.py` |
| `backend/app/execution/approvals.py` | Added `get_approval` and `list_pending_queue` to `ApprovalService` (ensuring read transactions commit cleanly to retain loaded attributes upon session exit with `expire_on_commit=False`). Enhanced `decide_approval` to check `expires_at` and `superseded` before conflict, and map lease acquisition failure on non-resumable runs to `RunNotResumableError`. Preserved atomic decision transition and lease handoff in single DB transaction. | `tests/test_api_approvals.py`, `tests/test_hitl_recovery.py` |
| `backend/app/api/schemas.py` | Strict Pydantic v2 schemas (`extra="forbid"`) for `ApprovalDecisionRequest` (`decision`, `args_hash`, `decided_by`, `reason` with non-empty validation on reject) and `ApprovalResource` (safe approval serialization with `redact_payload` masking sensitive fields in `payload_preview`). | `tests/test_api_approvals.py` |
| `backend/app/api/errors.py` | Implemented standard RFC 9457 Problem Details formatting (`application/problem+json`) with machine-readable error codes (`not_found`, `approval_not_pending`, `approval_expired`, `approval_superseded`, `run_not_resumable`, `validation_error`, `internal_error`). Strips internal SQL, credentials, stack traces, and database errors. | `tests/test_api_approvals.py` |
| `backend/app/api/dependencies.py` | Production dependency wiring reusing existing `Settings`, `create_session_factory`, `SqlUnitOfWork`, `create_agent_graph`, `LangGraphRunDriver`, `SystemClock`, `LeaseConfig`, and security authorization (`require_authorization` respecting `auth_mode`). Reusable and overridable in tests. | `tests/test_api_approvals.py` |
| `backend/app/api/approvals.py` | Canonical `/approvals` router providing `GET /approvals/queue`, `GET /approvals/{approval_id}`, and `POST /approvals/{approval_id}/decision`. Strictly delegates to `ApprovalService` without performing direct ORM queries, mutating persistence directly, or invoking LangGraph resume. Passes caller's `args_hash` unchanged to `ApprovalService`. | `tests/test_api_approvals.py`, `tests/test_structure.py` |
| `backend/app/main.py` | Wired `approvals_router`, `register_error_handlers`, and optional DI overrides in `create_app`. | `tests/test_api_approvals.py` |
| `backend/tests/test_api_approvals.py` | 17 comprehensive unit and PostgreSQL integration tests verifying queue retrieval, approval retrieval, 404 on missing approval, valid approve/reject, schema validation, reject reason enforcement, extra-field forbidding, exact `args_hash` flow, idempotent retry, conflicting decision 409, expired approval 409, superseded approval 409, non-resumable run 409, authorization enforcement, and safe serialization. | `tests/test_api_approvals.py` |
| `backend/tests/test_structure.py` | Added 4 structural invariants proving API layer does not call `ToolRegistry.dispatch()`, does not perform direct ORM queries, does not directly invoke LangGraph resume, and delegates decisions to `ApprovalService`. | `tests/test_structure.py` |

**Verification & Invariants:**
1. **Delegation Boundary**: AST structural tests verify the API layer never queries ORM models, never calls `ToolRegistry.dispatch()`, and never calls LangGraph resume directly. All operations delegate to `ApprovalService`.
2. **Exact args_hash**: The API layer passes the caller's `args_hash` unchanged to `ApprovalService.decide_approval(...)`, ensuring persisted hash equality.
3. **Crash-Safe Transaction Preserved**: Atomic decision handoff (`approvals` status update + `agent_runs` status update + trace event + commit) in `ApprovalService` is fully preserved.
4. **RFC 9457 Conformance**: All error responses return `application/problem+json` with standard machine-readable codes.
5. **No Secret Leaks**: Payload previews are redacted with `redact_payload`; database errors and stack traces are suppressed from client responses.

**Deliberately not done.** No frontend/UI (FE-001..008); no verification framework (VERIFY-001..003); no public execution or campaign API routes (API-001..003, API-005..007); no token minting / `ApprovalGate` lookup (HITL-002).

**Test suite: 1226 passed, 1 skipped** (`cd backend && uv run pytest` with `DATABASE_URL` pointing to PostgreSQL test database — 17 new in `tests/test_api_approvals.py`, 4 new in `tests/test_structure.py`). `ruff check .`, `ruff format --check .`, `mypy app` (strict, 67 source files), and `alembic check` are all clean.

## HITL-002 — Token minting and `ApprovalGate` lookup from approved rows — 2026-09-15

Barrier 3 of §9.5 is now real: a mutating tool receives an `ApprovalToken`
only when a durable, currently valid `approved` row exists for the exact
`(run_id, step_id, tool, canonical_args_hash(args))`, and the token is minted
on exactly one path. HITL-002 bridges a stored decision to an execution-time
capability; it approves, rejects, requests, supersedes and traces nothing.

| File | Role | Tests |
|---|---|---|
| `app/security.py` | `ApprovalRecordProtocol` — the read-only structural view of an `approvals` row (`id`, `run_id`, `step_id`, `tool`, `status`, `args_hash`, `superseded_by`, `expires_at`) the gate decides from, so the leaf never imports the ORM. `ApprovalGate.issue_from_persisted(record, *, run_id, step_id, tool, args, now)` — the application's single issuing path. Refuses `ApprovalRequiredError` when `record is None`; otherwise collects every failed binding — `status != "approved"`, `run_id`, `step_id`, `tool` (when stated), `superseded_by is not None`, `now >= expires_at`, `args_hash != canonical_args_hash(args)` — and raises `ApprovalInvalidError` naming all of them. Only then does it call `ApprovalGate.issue`, the one expression in the codebase that passes `_MINT`. `APPROVED_STATUS` spells the enum value locally (a leaf cannot import `ApprovalStatus`); a test pins the two. | `tests/test_hitl_gate.py` (gate matrix), `tests/test_security.py` |
| `app/errors.py` | `ApprovalRequiredError` and `ApprovalInvalidError` moved into the taxonomy (both `PolicyViolation`, terminal) so the leaf can raise them. `app.tools.registry` re-exports them; every existing import site is unchanged. | `tests/test_security.py` |
| `app/persistence/protocols.py`, `app/persistence/repositories.py` | `ApprovalRepository.get_approved(run_id, step_id) -> ApprovalRow \| None`: `status = 'approved'` only, exact run and step, ordered `decided_at DESC NULLS LAST, requested_at DESC, id DESC LIMIT 1`, `populate_existing` so a session that loaded the row before another transaction decided it never answers from its stale copy. The partial unique index bounds *pending* rows to one per step; approved rows accumulate across replans, so the latest decision is the current one and an older grant never outranks it. | `tests/test_hitl_gate.py::TestApprovedRowLookup` |
| `app/agent/nodes.py` | `execute_tool` for a gated tool: resolve `$ref`s → validate the plan's own arguments → barrier 2 (`approval_state.grants`) → `uow.approvals.get_approved` → `ApprovalGate.issue_from_persisted(..., now=clock.now())` → `ToolRegistry.dispatch(approval_token=token)`. Ungated tools receive no token. Two second authorisation paths were removed: the placeholder token `execute_tool` used to allocate with `object.__new__(ApprovalToken)` for input pre-validation (replaced by `_validate_plan_arguments`, which ignores only errors located at a dispatcher-owned field), and the injectable `token_issuer` constructor hook. A gated step on handlers with no `uow_factory` fails closed (`ApprovalRequiredError`). Every attempt re-reads the row; nothing is cached. | `tests/test_hitl_gate.py::TestExecuteToolMintsFromTheRow`, `::TestAgentGraphEndToEnd` |
| `app/tools/registry.py` | `_verify_stored_decision` now also refuses `superseded_by IS NOT NULL` and `clock.now() >= expires_at`, reading the row fresh — so the dispatcher's stored-decision check agrees with the gate and a token that was valid when minted is re-judged at dispatch. | `tests/test_hitl_gate.py::TestDispatcherRechecksAGateMintedToken`, `tests/test_tool_dispatch.py` |
| `tests/test_structure.py` | 12 new structural tests: a token-forgery scan (constructor, `_MINT` by import or attribute, `object.__new__`/`ApprovalToken.__new__`) proven by six canaries and three allowed uses; `ApprovalGate.issue` only in `security.py` and `issue_from_persisted` only in `agent/nodes.py`; `get_approved` called only by `execute_tool`; no `approvals.create_request/decide/supersede` and no `TraceEventKind.APPROVAL_*` on the token path (HITL-003 keeps request creation); the API layer never imports or names `ApprovalGate`/`ApprovalToken`; `NodeHandlers`/`create_agent_graph` accept no token/gate/mint/issuer parameter; `security.py` imports only the stdlib and `app.errors`. | `tests/test_structure.py` |

**What the token is.** An in-process capability, not a signed credential: a
frozen dataclass whose `__post_init__` demands a module-private sentinel that
no other module references (the structural scan proves it). It is never
serialised, persisted or sent anywhere, so there is nothing to sign — the
guarantee is that code which cannot reach the sentinel cannot produce an
instance, and the only code that can is the gate, which first checks the row.
It is proof of provenance; the dispatcher and the adapter still re-check every
token they are handed.

**Defense in depth, as it now stands.** Router (rule 6) → `execute_tool`'s
`approval_state.grants` re-assertion → `ApprovalGate.issue_from_persisted`
over the durable row → `ToolRegistry._assert_gate` (`token.authorises`) →
`ToolRegistry._verify_stored_decision` (row is `approved`, same run/step/tool/
hash/risk, not superseded, inside its TTL, read fresh at dispatch) → the
adapter's token requirement → the per-key advisory lock and the outbox
`UNIQUE(idempotency_key)`. No layer was removed; one gained two conditions.

**Concurrency and replay** (real Postgres, `tests/test_hitl_gate.py`): two
concurrent `execute_tool` calls with identical valid authorisation each mint a
token and dispatch; the key lock serialises them and the constraint makes the
loser `duplicate_suppressed` — one outbox row. Two drivers of the *same*
attempt: one `DuplicateAttemptError`, one effect. A token replayed into another
run, step or tool, or presented with other arguments, is refused by the
dispatcher. An approval that expires (clock moved past `expires_at`) or is
chained forward (`superseded_by` set by raw SQL, since no repository method
does it to an approved row today) between minting and dispatch is refused by
the dispatcher. Expiry at the check boundary is closed: `now == expires_at` is
expired, the same comparison `ApprovalService` uses.

**Deliberately not done.** HITL-003 (`request_approval` idempotent upsert and
`approval_requested` emission — the graph still persists no approval row when
it pauses, so the end-to-end test scripts the human through the repository);
HITL-004 (single-flight resume contention); HITL-005 (payload preview); no
migration (every check is expressible over the existing `approvals` columns);
no durable `tool_calls` row or `policy_violation` trace for a gate refusal
inside `execute_tool` — like barrier 2's refusal today, it is recorded in the
run state (`errors`, `tool_calls`) and fails the run, and the dispatcher's
durable record covers refusals of a presented token.

**Test suite: 1304 passed, 1 skipped** (`cd backend && uv run pytest` with
`DATABASE_URL` pointing at PostgreSQL — 60 new in `tests/test_hitl_gate.py`,
4 in `tests/test_security.py`, 12 in `tests/test_structure.py`; one assertion in
`tests/test_tool_dispatch.py` widened to the fuller mismatch detail).
`ruff check .`, `ruff format --check .`, `mypy app` (strict, 67 source files)
and `alembic check` are clean. gitleaks reports two pre-existing findings in
`tests/test_responder.py` (commit `83cf2573`): deliberate credential-shaped
strings that exercise the sanitizer, not credentials.

## HITL-003 — `request_approval`: idempotent durable request, then `interrupt()` — 2026-09-15

The pause is now durable and idempotent (§6.3, §7, §9.7, ADR-007). Every
execution of `request_approval` — the first, and every re-execution LangGraph
performs on resume or re-entry — makes the same request: the `approvals` row
is upserted on the logical identity `(run_id, step_id, args_hash)`,
`approval_requested` is emitted only when that upsert genuinely inserted, and
both commit in one transaction before the node pauses. No tool is invoked in
this node, ever.

| File | Role | Tests |
|---|---|---|
| `app/persistence/protocols.py`, `app/persistence/repositories.py` | `ApprovalRepository.upsert_request(...) -> ApprovalUpsert(row, created, superseded)`. A transaction-scoped advisory lock keyed on `(run_id, step_id)` — the mechanism `trace_events` already uses for "no row exists to lock yet" — serialises requests for a step; it is taken before any approval row lock, the order `ApprovalService` also follows (row, then per-run trace lock), so no lock-ordering cycle exists. Under it, a bounded read → conditional-write loop: a current `approved` (inside its TTL at `requested_at`) or `rejected` row for these exact arguments is returned as-is; an identical `pending` row is returned; otherwise the open request is closed by `UPDATE … WHERE status = 'pending'` (zero rows means a decision landed between the read and the write — decisions do not take the request lock — and the loop re-reads it as a decision), a `pending` row is inserted with `INSERT … ON CONFLICT (run_id, step_id) WHERE status = 'pending' DO NOTHING RETURNING` against the existing partial unique index, and every current row for *other* arguments — open or decided — is marked `superseded` with `superseded_by` pointing at the new row (the pointer is a foreign key, so it is set after the insert, in the same transaction). Expired, cancelled and superseded rows are history: never reused, never in the way. No migration: the partial unique index remains the backstop. | `tests/test_hitl_request_approval.py::TestUpsertRequest`, `::TestUpsertRequestUnderConcurrency` |
| `app/agent/nodes.py` | `request_approval` → `_persist_approval_request`: one unit of work that upserts and, only when `created`, appends `approval_superseded` for each chained row and then `approval_requested` — through `uow.trace_events.append`, so the per-run monotonic `seq` and its advisory lock are unchanged — and commits. What the row says decides the rest: `pending` → `interrupt()` with the canonical payload `{approval_id, run_id, step_id, tool, args_hash, payload_preview}` (`approval_id` is the row id; `payload_preview` is the resolved arguments through `redact_payload`, the same redaction the API applies); `approved`/`rejected` for these exact arguments → the `ApprovalDecision` is built from the row (`decided_by`, `decided_at`, `decision_reason`) and written into `approval_state` without pausing again. The resume value is consumed only when the row is still pending; the durable row always outranks it. Control then falls through to `decide`, which re-applies rule 6 on the arguments as they will now be sent, and `execute_tool` still mints from the persisted row (HITL-002) — this node's word is never authorisation. Without `uow_factory` the checkpointed `approval_state` is the record: a held decision for these exact arguments is reused, anything else is asked, and the identity is derived from `(run_id, step_id, args_hash)`. A persistence failure propagates: there is no safe way to pause without a durable request to decide on. The TTL is `approval_ttl` on `NodeHandlers`/`create_agent_graph` (default 24 h), wired from `Settings.approval_ttl` by `app/api/dependencies.py`. | `tests/test_hitl_request_approval.py::TestNodeOverCheckpointedState`, `::TestGraphPausesDurably`, `::TestGraphResumes`, `::TestChangedArgumentsAfterAGrant` |
| `tests/test_hitl_gate.py` | The end-to-end HITL-002 test decided a request it had inserted itself; the pausing node now persists the request, so the human decides *that* row — the one the interrupt names — and the outbox row must trace to it. | `tests/test_hitl_gate.py::TestAgentGraphEndToEnd` |
| `tests/test_structure.py` | The token-path scan (`test_token_issuance_never_mutates_approval_state`) is now per function: `request_approval` and `_persist_approval_request` are the one exemption in `agent/nodes.py`; every other function there, and all of `security.py`, `agent/decide.py` and `tools/registry.py`, still writes no approval row and emits no approval event. A new test pins `upsert_request`, `approval_requested` and `approval_superseded` to that request path and forbids `create_request` — the unconditional insert — anywhere in application code, so a re-executed node cannot reach a path that inserts twice. | `tests/test_structure.py` |

**Why the naive `SELECT pending → mark superseded → INSERT` is wrong, and
what replaces it.** The partial unique index bounds `pending` rows to one per
step, but an *approved* row is no longer in the index: two workers that both
read "no request yet" can, after the first one's row is approved, insert a
second `pending` for the same arguments — the human asked twice, a grant
regressed. And a request for changed arguments that reads the open request
races the human's `UPDATE … WHERE status = 'pending'`: whichever commits
second must not overwrite the other. The advisory lock closes the first race
(every request for a step sees every committed request, so a decision is
found, not re-asked); the conditional close plus re-read closes the second
(the decision is kept and then chained forward as `superseded`, and the new
arguments are still asked). `ON CONFLICT DO NOTHING` remains under the lock
so that a writer bypassing it produces a re-read, not a duplicate.

**Replay and crash windows** (real Postgres saver, production graph): a
re-entry with no decision pauses on the identical payload with no new row
and no new event; a crash after the request transaction committed but before
the interrupt was checkpointed re-executes the node, which finds its row and
event; a decision that arrives while the worker is away is found on plain
re-entry (no resume value) and the run completes without pausing again; a
fresh process resumes from the checkpoint alone; the reconciler (ADR-023)
recovers a run whose decision committed before its worker died, resuming the
production graph with the stored decision into exactly one send. Changed
arguments after a grant: the old approval becomes `superseded` (pointer to the
new request), `approval_superseded` then `approval_requested` are traced,
`get_approved` finds nothing until the new request is decided, and the send
traces to the new row.

**Acceptance.** Pausing performs zero `mock_crm` writes — asserted by a
content fingerprint of every `mock_crm` table before and after, not a count
of one table — and re-entry creates no second approval row and no duplicate
trace event. Rejection is `rejected` with `status_reason=approval_rejected`,
never `failed`; an optional step is skipped and the run completes.

**Deliberately not done.** HITL-004 (single-flight resume contention: two
concurrent approve calls → one resume and one `409`); HITL-005 (the
de-referenced `payload_preview` — this task stores the redacted resolved
arguments, which is what the interrupt already surfaced); no `status =
awaiting_approval` write from inside the node (a node that raises
`GraphInterrupt` writes no state, so the run row's transition stays with the
executor and the reconciler, as today); no migration.

**Test suite: 1338 passed, 1 skipped** (`cd backend && uv run pytest` with
`DATABASE_URL` pointing at PostgreSQL — 33 new in
`tests/test_hitl_request_approval.py`, 1 new and 1 narrowed in
`tests/test_structure.py`, 1 amended in `tests/test_hitl_gate.py`; the skip
is the opt-in live Groq smoke test). `ruff check .`, `ruff format --check .`,
`mypy app` (strict, 67 source files) and `alembic check` are clean.

## HITL-004 — Single-flight resume contention / multi-worker resume coordination — 2026-09-15

Resume dispatch is now strictly single-flight and coordinated across concurrent
workers and processes (§6.3, §7.3, §18.4, ADR-007, ADR-023). When multiple
workers or HTTP requests race to decide and resume the same interrupted run,
the database remains the sole arbiter: exactly one worker claims the decision
transition, acquires the execution lease, and invokes `driver.resume(...)`.
Zero duplicate graph resumes, zero duplicate node re-entries, zero duplicate
downstream tool mutations (`send_email_mock`, `update_customer`), and clean 409
conflict semantics for losers.

| File | Role | Tests |
|---|---|---|
| `app/execution/approvals.py` | `ApprovalService.decide_and_resume`: single-flight resume coordination. Raced decisions execute conditional `UPDATE approvals SET status = :status ... WHERE id = :id AND status = 'pending'`. The winner commits the transition, locks `agent_runs`, acquires the execution lease, and calls `driver.resume(run_id, Command(resume=decision))`. Losers observing 0 rows updated raise `ApprovalNotPendingError` (mapped to RFC 9457 `409 approval_not_pending`). Sequential retries check run lease and terminal status: if a live worker holds lease (`lease_expires_at > now`) or run is terminal (`COMPLETED`/`REJECTED`/`FAILED`), the call succeeds idempotently (`is_winner=False`) without re-resuming. If the previous worker crashed and lease expired (`lease_expires_at <= now`), the retrying worker acquires the lease via `uow.agent_runs.acquire_lease` and safely re-enters graph execution. Resume exceptions are caught, settling run to `FAILED(status_reason="resume_failed")`, tracing `RUN_FAILED`, and releasing lease. Zombie workers whose lease lapsed during execution are fenced via `heartbeat.lost.is_set()` before settling state. | `tests/test_hitl_recovery.py::TestHitlSingleFlightResumeContention`, `tests/test_api_approvals.py::test_concurrent_http_post_decision_produces_one_200_and_one_409` |
| `tests/test_hitl_recovery.py` | 6 new tests covering: 2 concurrent approve calls → 1 resume and 1 `409`; 6-worker stampede race → 1 winner and 5 `409`s; retry after crash + lease expiry → re-acquires lease and resumes; retry while running → 200 idempotent, 0 duplicate resumes; resume failure → settles to `FAILED` with lease cleared; 2 concurrent reject calls → 1 resume and 1 `409`. | `tests/test_hitl_recovery.py` |
| `tests/test_api_approvals.py` | HTTP-level concurrency test using `httpx.AsyncClient` with `ASGITransport(app=app)`: 2 concurrent `POST /approvals/{id}/decision` calls over real PostgreSQL produce exactly one `200 OK` and one `409 Conflict` (`application/problem+json` with `type=".../approval_not_pending"`). | `tests/test_api_approvals.py` |

**Single-flight coordination design without distributed locks.** No Redis, no
etcd, no extra database tables, and no process-local mutexes (`threading.Lock` /
`asyncio.Lock`) that fail in multi-worker or multi-process deployments. The atomic
conditional `UPDATE approvals ... WHERE status = 'pending'` is the primary race
arbiter. The execution lease (`lease_owner`, `lease_expires_at`) coordinates the
re-entry boundary and handles crash recovery cleanly without orphan runs.

**Acceptance.** Two concurrent approve calls produce one resume and one `409`;
an N-worker stampede produces 1 winner and N-1 `409` conflicts; retries while running
are idempotent without second resume; retries after worker crash safely reclaim lease
and resume; unhandled driver resume errors transition the run to `FAILED` and release
lease. Downstream mutations are guaranteed strictly single-execution.

**Deliberately not done.** HITL-005 (the de-referenced `payload_preview` builder);
API-004 (the full approval API error mapping suite); no changes to `ApprovalGate` or
`ToolRegistry.dispatch` token invariants; no migration.

**Test suite: 1345 passed, 1 skipped** (`cd backend && uv run pytest` with
`DATABASE_URL` pointing at PostgreSQL — 6 new in `tests/test_hitl_recovery.py`,
1 new in `tests/test_api_approvals.py`; the skip is the opt-in live Groq smoke test).
`ruff check app tests`, `ruff format --check app tests`, `mypy app` (strict, 67 source files)
and `alembic check` are clean.

## HITL-005 — Rich Human-Approval Preview / De-Referenced Payload Preview — 2026-09-15

Approval requests now surface rich, human-readable, de-referenced previews of
gated actions (`send_email_mock`, `update_customer`) without executing tools,
without mutating state, and without altering authorization tokens or canonical
`args_hash` (§6.3, §9.2, §14.5, HITL-005). Operators see exactly what the agent
is asking permission to do: the recipient, subject, and body for outreach emails,
and the target customer, version match, and before/after field diffs for customer updates.

| File | Role | Tests |
|---|---|---|
| `app/agent/preview.py` | `build_approval_preview(step, resolved_args, tool_results, uow, risk)`: pure preview builder. For `send_email_mock`, de-references `draft_id` via read-only UoW query (Tier 1) or in-memory `tool_results` fallback (Tier 2), resolving `to_email`, `subject`, `body`, `lead_id`, and `content_hash`. Truncates body at 1500 chars with content hash indicator. For `update_customer`, de-references customer row, resolves `account_name`, `primary_contact`, `email`, `current_version`, and computes `version_match` and field-by-field `diff` (`{"before": ..., "after": ...}`). For generic tools, builds clean scalar preview. Strips volatile keys (`args_hash`, `correlation_id`, `now`, `_token`), masks secrets, and enforces recursive 4096-byte budget via `redact_payload`. | `tests/test_hitl_preview.py` |
| `app/agent/nodes.py` | Updated `_ApprovalRequest` dataclass to include `preview: dict[str, Any]`. Wired `build_approval_preview` into `request_approval`, `_persist_approval_request`, and `_checkpointed_approval_request`, persisting rich preview into `approvals.payload_preview` and emitting it in `approval_requested` trace events and `interrupt()` payloads. | `tests/test_hitl_request_approval.py`, `tests/test_decide.py` |
| `tests/test_hitl_preview.py` | 14 comprehensive unit and integration tests covering: `send_email_mock` with persisted draft; fallback to `tool_results`; graceful degradation on missing draft; body truncation; `update_customer` with persisted customer and diff calculation; fallback to `tool_results`; stale version mismatch detection (`version_match=False`); volatile arg stripping; secret masking; payload size budget (<= 4096 bytes); deterministic output; and zero CRM mutations verified by whole-schema fingerprint before and after. | `tests/test_hitl_preview.py` |

**Security and Invariant Preservation.**
- Preview generation is strictly read-only: no mock CRM state, customer rows, or drafts are modified. Verified by full-schema database fingerprinting before and after preview construction.
- Canonical `args_hash` calculation is completely unchanged: `args_hash = canonical_args_hash(resolved_args)` ensures exact parameter binding is preserved without weakening Barrier 3 or the `ApprovalGate`.
- Zero database schema migrations required: stored in existing `approvals.payload_preview` JSONB column.

**Deliberately not done.** API-004 (approvals endpoints error family); VERIFY-001..003 (verifier framework); no changes to `ApprovalGate` or `ToolRegistry.dispatch` token invariants; no migrations.

**Test suite: 1359 passed, 1 skipped** (`cd backend && uv run pytest` with
`DATABASE_URL` pointing at PostgreSQL — 14 new in `tests/test_hitl_preview.py`;
the skip is the opt-in live Groq smoke test). `ruff check app tests`,
`ruff format --check app tests`, `mypy app` (strict, 68 source files) and
`alembic check` are clean.

## VERIFY-001 — Postcondition Verification Framework & Invariant Verifiers — 2026-09-16

Postcondition verification framework (§11, ADR-008, VERIFY-001) is implemented.
The system strictly distinguishes *tool return success* from *intended business outcome verified*.
Verification is read-only, post-execution, and keyed to `VerificationMode` from contracts.

| File | Role | Tests |
|---|---|---|
| `app/agent/verifiers/base.py` | `VerificationContext` carrier and `Verifier` protocol. Pure read-only post-execution contract. | `tests/test_verifiers.py` |
| `app/agent/verifiers/invariants.py` | Invariant verifiers for read tools (§8.4): `SearchLeadsVerifier` (limit bounds, total matched consistency, filter compliance), `ResearchCompanyVerifier` (confidence bounds, non-empty summary, company ID matching), `ScoreLeadVerifier` (0..100 score bounds, factor contribution sum consistency, band threshold verification), `DraftOutreachVerifier` (word count bounded, non-empty subject/body, placeholder rejection, SHA-256 content hash integrity). | `tests/test_verifiers.py` |
| `app/agent/verifiers/readback.py` | Readback verifiers for mutating tools: `SendEmailMockVerifier` (outbox row exists, status="sent", recipient matching, draft ID matching, idempotency key count), `UpdateCustomerVerifier` (expected version increment, patch equality, unchanged untouched fields), `SaveDraftVerifier` (draft row exists, status="saved", lead ID match, requested content hash match). | `tests/test_verifiers.py` |
| `app/agent/verifiers/registry.py` | `NullVerifier` (records `status=NOT_REQUIRED` for `VerificationMode.NONE` tools e.g. `get_lead`, `get_customer`), `VerifierRegistry` with default bindings for all tools. | `tests/test_verifiers.py` |
| `app/agent/nodes.py` | Wired `verify` node and `route_after_verify` conditional routing. If verification passes or is not required, routes to `decide`. If verification fails or is unconfirmed, records `AgentError(error_class=ErrorClass.VERIFICATION_FAILED)` and routes to `recover`. Persists `VerificationResult` to `execution_steps.verification` and emits structured trace events (`verification_passed` / `verification_failed`). `execute_tool` explicitly records `VerificationStatus.NOT_REQUIRED` when contract mode is `NONE`. | `tests/test_verify_node.py` |
| `app/agent/graph.py` | Injected `verifier_registry` and `adapters` into `create_agent_graph` and forwarded to `NodeHandlers`. | `tests/test_verify_node.py` |
| `tests/test_verifiers.py` | 28 unit tests covering null verifier, invariant verifiers, readback verifiers, corrupted hashes, out-of-bound scores, placeholder detection, untouched field tampering, and duplicate outbox detection. | `tests/test_verifiers.py` |
| `tests/test_verify_node.py` | 5 integration tests covering full graph execution across passed invariants, failed invariants routing to recover, readback verifiers, and NONE mode recording `NOT_REQUIRED`. | `tests/test_verify_node.py` |
| `tests/test_structure.py` | Structural AST checks proving: verifiers never import `_MINT` or call `ApprovalGate`, verifiers never call `ToolRegistry.dispatch` or `execute_tool`, verifiers never import LLM clients or prompt modules, and verifiers only access integration ports via read methods (`get`, `get_outbox`). | `tests/test_structure.py` |

**Security and Boundary Invariants.**
- Verifiers are strictly read-only: zero tool calls, zero token minting, zero state mutation, zero LLM calls. AST enforced in `test_structure.py`.
- Level 0 schema validation remains mandatory on all tools; postcondition verification operates on validated output and requested input intent.
- `VerificationMode.NONE` produces an explicit `VerificationResult(status=NOT_REQUIRED)` — no silent gaps in the audit trail.
- Zero database migrations required: utilizes existing `execution_steps.verification_status`, `verification` JSONB, and `trace_events`.

**Test suite: 953 passed, 25 skipped, 417 deselected** (non-integration run; 400 passed against integration/graph files).
`ruff check app tests`, `ruff format --check app tests`, and `mypy app` (strict, 73 source files) are clean.

## VERIFY-002 — Concrete Postcondition Verifiers / Business-Outcome Verification — 2026-09-16

Concrete postcondition verifiers across all OpsPilot business workflows (§11, ADR-008, VERIFY-002) are hardened.
Every verifier strictly compares actual state/output against the caller's *requested intent* (`ctx.input_args`) rather than the tool's echoed response.
Enforces business-outcome invariants: single-row idempotency key assertion for email dispatch, untouched fields protection against baseline customer snapshots, alignment with rule-engine score band thresholds, independent outreach text word counts, and structural filter validations.

| File | Role | Tests |
|---|---|---|
| `app/agent/verifiers/readback.py` | Hardened readback verifiers: `SendEmailMockVerifier` (validates against `ctx.input_args`, asserts `count_outbox(idempotency_key) == 1` to prevent duplicate sends/replays), `UpdateCustomerVerifier` (asserts `untouched_fields_intact` against `ctx.baseline_customer` across non-patch fields, asserts immutable identity fields like `customer_id`, `account_name`, `email`, `mrr`), `SaveDraftVerifier` (verifies `channel_matches_intent` and direct content matching for `subject` and `body` alongside content hash). | `tests/test_verifiers.py`, `tests/test_verify_node.py` |
| `app/agent/verifiers/invariants.py` | Hardened invariant verifiers: `ScoreLeadVerifier` (aligned score band threshold with rule engine: `score >= 75` -> `"hot"`, `50..74` -> `"warm"`, `< 50` -> `"cold"`; asserts all 4 required score factors are present), `DraftOutreachVerifier` (independently calculates actual text word count `len(body.split()) <= max_words`, asserts `1 <= len(subject) <= 120`, expands placeholder detection across `<...>`, `[...]`, `TODO`, `FIXME`, `XXX`, `{% ... %}`), `SearchLeadsVerifier` (unique `lead_id` check, non-negative `total_matched >= 0`, multi-filter matching), `ResearchCompanyVerifier` (domain intent matching, list type validation). | `tests/test_verifiers.py` |
| `app/integrations/ports.py` | Added `count_outbox(self, idempotency_key: str) -> int` to `MailPort` protocol. | `tests/test_verifiers.py` |
| `app/integrations/mock/adapters.py` | Implemented `count_outbox` on `MockMailAdapter` querying `email_outbox.count_by_idempotency_key`. | `tests/test_verifiers.py` |
| `app/persistence/protocols.py` + `repositories.py` | Added `count_by_idempotency_key` to `EmailOutboxRepository` executing `SELECT count(*) FROM email_outbox WHERE idempotency_key = :key`. | `tests/test_verifiers.py` |
| `app/agent/verifiers/base.py` | Extended `VerificationContext` with `baseline_customer: Customer | None` and `prior_tool_results: dict[str, Any]`. | `tests/test_verifiers.py`, `tests/test_verify_node.py` |
| `app/agent/nodes.py` | Populated `baseline_customer` in `verify` node from prior `get_customer` results or inputs; handles `UNCONFIRMED` verification by recording `AgentError(error_class=ErrorClass.TRANSIENT)` and routing to `recover`. | `tests/test_verify_node.py` |
| `tests/test_structure.py` | Added `count_outbox` to `READ_METHOD_NAMES` AST whitelist; confirmed zero token minting, zero state mutations, zero LLM calls, zero unregistered port methods. | `tests/test_structure.py` |
| `tests/test_verifiers.py` | 40 unit tests covering all invariant and readback assertions, threshold alignment, duplicate outbox row detection, and customer tampering detection. | `tests/test_verifiers.py` |
| `tests/test_verify_node.py` | 7 integration tests covering full graph verify node execution, outbox duplicate detection routing to recover, and customer tampering routing to recover. | `tests/test_verify_node.py` |

**Security and Boundary Invariants.**
- Verifiers are strictly read-only and non-mutating: zero tool dispatch, zero token minting, zero LLM calls. AST enforced in `test_structure.py`.
- Verified business outcomes: outbox uniqueness asserts `count == 1` ensuring replay protection; customer modification asserts all non-patch fields remain identical to baseline.
- Score band thresholds aligned with rule engine (`score >= 75` is `"hot"`).
- Zero database migrations: executes standard SQL count query over existing indexed `email_outbox.idempotency_key`.

**Test suite: 968 passed, 25 skipped, 417 deselected** (non-integration run).
`ruff check app tests`, `ruff format --check app tests`, and `mypy app` (strict, 73 source files) are clean.

## VERIFY-003 — Verification Failure Recovery, Retry, Replan, Escalation & Terminal Semantics — 2026-09-16

The control loop after verification is closed. `execute_tool → verify` already existed; what was missing was everything on the far side of a verdict that is not `passed`.

The distinction the whole task turns on:

- **`failed`** — independent evidence proves the requested postcondition is false.
- **`unconfirmed`** — the system cannot currently determine whether the effect happened.

They are never collapsed. In particular, a mutating tool that **succeeded** whose read-back came back `unconfirmed` retries **`verify`**, never `execute_tool`. You do not answer "did the email send?" by sending another email.

| File | Role | Tests |
|---|---|---|
| `app/agent/verification_recovery.py` | **New.** The two questions verification adds to `recover`, as pure functions over checkpointed channels: *where does the retry go* (`retry_target_for`, `RetryTarget`, `tool_effect_succeeded`) and *is repeating the mutation safe at all* (`classify_verification_failure`, `VerificationSafety`, `is_safe_to_retry_mutation`, `PROVEN_ABSENCE_CHECKS`). Plus `verify_retry_key` and the `AgentError.detail` stamp keys. No I/O, no async, no ports, no registry, no security — it narrows the inputs `recovery_action` is given rather than deciding anything twice. | `tests/test_verify_recovery.py` |
| `app/agent/nodes.py` — `execute_tool` | A tool that returned is not yet a step that succeeded. For `verification != NONE` the step is left `running`; `VerificationMode.NONE` still settles at execution as before. | `tests/test_verify_recovery.py` |
| `app/agent/nodes.py` — `verify` | Stamps `VerificationResult.attempt` from the checkpointed read-back counter; settles the step to `succeeded` on `passed`/`not_required`; marks its own `AgentError`s with `source="verify"`, the verification attempt and the check evidence; forces a verifier exception to `TRANSIENT` unless the verifier itself is broken. Persistence and tracing moved into `_persist_verification`, so the exception path now persists too. | `tests/test_verify_recovery.py`, `tests/test_verify_node.py` |
| `app/agent/nodes.py` — `recover` | Reads the verification evidence before classifying. `unconfirmed` + tool succeeded → `_recover_unconfirmed` (retry `verify`). `failed` → retry `execute_tool` only when the contract is idempotent **and** every failing check is proof of absence; otherwise terminal (or `skipped`, for an optional step). Terminal reasons are `verification_failed` and `verification_unconfirmed`, never laundered into `recovery_exhausted`. Backoff and the server `retry_after` hint were factored into `_retry_delay_ms` and are shared by both retry targets. | `tests/test_verify_recovery.py`, `tests/test_recover.py` |
| `app/agent/nodes.py` — `route_after_recover` | New `verify` route, bounded by the read-back counter; `verification_failed` / `verification_unconfirmed` route to `fail`. Every existing route is unchanged. | `tests/test_verify_recovery.py`, `tests/test_agent_graph.py` |
| `app/agent/nodes.py` — responder | `verification_unconfirmed` gets its own operator-facing explanation, distinct from `verification_failed`: one is evidence, the other is the absence of it, and an operator acts differently on each. | `tests/test_verify_recovery.py`, `tests/test_responder.py` |
| `app/agent/state.py` | `VerificationResult.attempt` — a read-back retry does not re-run the tool, so it cannot borrow `ToolCall.attempt`. | `tests/test_verify_recovery.py` |
| `app/agent/graph.py` | The `recover → verify` conditional edge. | `tests/test_verify_recovery.py` |
| `tests/test_verify_recovery.py` | **New.** 61 tests: passed/failed/unconfirmed semantics, evidence-based retry safety, five crash-and-resume scenarios against a real `MemorySaver` checkpoint, replan preservation, end-to-end loops, and the structural invariants. | — |

**Why `unconfirmed` retries the read and not the write.** `retry_target_for` returns `verify` only when all three hold: the error came from `verify` (the `source` stamp, checkpointed with the error), the result is `unconfirmed`, and the latest `ToolCall` for the step says `succeeded`. All three are read from the append-only channels, so a resumed worker reaches the same conclusion the crashed one did — no process-local flag is involved. A generic `TRANSIENT` error from the dispatcher is *not* enough to route to `verify`, which is the point: the classification is evidence, not error class.

**Why the error class cannot decide retry safety.** All three mutating contracts are `idempotent=True` and list `VERIFICATION_FAILED` in `retryable_errors`, so `recovery_action` alone would happily re-send an email whose read-back showed the *wrong recipient*. `classify_verification_failure` reads the verifier's own checks instead. `PROVEN_ABSENCE_CHECKS` is an allowlist of exactly the three "we re-read the entity and it is not there" checks (`outbox_record_exists`, `draft_record_exists`, `customer_record_exists`); a failure consisting only of those is safe to repeat for an idempotent tool, and everything else — wrong recipient, duplicate outbox rows, tampered untouched field, corrupted content hash, a record in an unexpected state, or any check added in future — is not. Invariant P5 keeps its veto: a non-idempotent contract is never retried whatever the evidence says.

**Idempotency, stated honestly.** `send_email_mock` and `update_customer` carry a dispatcher-derived `idempotency_key`; `MockMailAdapter.send` returns the existing receipt for a known key, so a retry suppresses the duplicate rather than guaranteeing exactly-once end to end. `save_draft` has no key in its input contract (ADR-008) — a retry writes a *new* draft, which is safe precisely because the draft is internal and reversible, and is why only a proven-absent draft is retried. `update_customer` retries under optimistic concurrency: a repeat with a consumed `expected_version` raises `STALE_WRITE`, which follows the existing replan path and forces a fresh approval.

**Crash and checkpoint behaviour.** Read-back retries are counted under `retry_count[f"{step_id}::verify"]` — the same channel and the same `merge_dict` reducer, a separate namespace — and guarded exactly as tool retries are: the counter may only move past the attempt the evidence belongs to (`verify_retries >= result.attempt` ⇒ no increment). Five scenarios are tested against a real checkpointer by killing a node mid-flight and resuming: crash after `execute_tool` before `verify`, crash inside the verifier, crash after `unconfirmed`, crash during the retry backoff, and crash after the retry was scheduled. In every one the tool executes exactly once and the counter advances exactly once.

**What did not change.** The verification architecture (VERIFY-001/002), the approval API and `ApprovalGate` authority (HITL-001/002), durable interrupt semantics (HITL-003), single-flight resume and lease fencing (HITL-004), `ToolRegistry` as the only execution choke point, the retry/backoff/budget model, the replan taxonomy, and the terminal responder. Verification failure is still never a replan by itself; only the existing replannable classes replan, and a replanned mutation produces new arguments, a new `args_hash` and therefore no standing grant.

**Trace.** `verify` emits `verification_passed` / `verification_failed` as before; an `unconfirmed` result is `warning` severity whatever the tool does (absence of evidence is not evidence of a bad effect) and its payload carries `unconfirmed`, `classification="transient"`, `recovery="retry_readback"` and `tool_effect_succeeded`. `recover` now emits `retry_scheduled` with `target="verify"` or `target="execute_tool"`, so an auditor can see which retries re-read and which re-wrote without reconstructing the state machine. No arguments, previews, tokens or secrets reach a trace. No new trace kind, and no migration.

**Zero database migrations.**

**Test suite: 1035 passed, 4 skipped** (full run against a live PostgreSQL 16 and the real `AsyncPostgresSaver`).
`ruff check app tests`, `ruff format --check app tests`, `mypy app` (strict, 75 source files) and `alembic check` are clean.

## API-003 — Run Trace Retrieval & Live Event Streaming — 2026-09-17

Run execution trace retrieval and live event streaming (§13.4, §14, API-003) are implemented.
The API layer exposes OpsPilot's existing durable audit/trace system through safe, efficient HTTP control-plane boundaries without creating a secondary tracing engine or introducing external broker/queue infrastructure.

| File | Role | Tests |
|---|---|---|
| `app/api/runs.py` | `GET /runs/{run_id}/trace`, `GET /api/v1/runs/{run_id}/trace`, `GET /runs/{run_id}/events`, `GET /api/v1/runs/{run_id}/events`. Prefixed parity routes with RFC 9457 error handling, query/header validation, and EventSource streaming. | `tests/test_api_trace.py` |
| `app/api/schemas.py` | `TraceEventResource` (safe, presentation-redacted representation of durable trace events; omits DB surrogate `id`; exposes monotonic `seq`), `TraceResponse` envelope (`run_id`, `events`, `next_seq`, `complete`). | `tests/test_api_trace.py`, `tests/test_structure.py` |
| `app/execution/runs.py` | `RunService.get_run_trace` (keyset pagination with `limit + 1` windowing, `next_seq`, `complete` status detection) and `RunService.stream_run_events` async generator (PostgreSQL polling with 500ms backoff, SSE yield, disconnect checking, terminal event closure). | `tests/test_api_trace.py`, `tests/test_structure.py` |
| `app/persistence/protocols.py` + `repositories.py` | `TraceEventRepository.list_by_run` extended with `since_seq`, `kinds` filter list, and `severity_min` filtering using hierarchical severity rank (`DEBUG: 1, INFO: 2, WARNING: 3, ERROR: 4`). | `tests/test_api_trace.py` |
| `tests/test_api_trace.py` | 35 tests covering REST trace retrieval (monotonic seq, pagination, filtering, 404/422 errors, redaction, non-mutation of ORM objects), SSE event streaming (headers, formatting, Last-Event-ID replay, since_seq fallback, precedence, 15s keepalive, disconnects, multi-subscriber), and real PostgreSQL integration tests (concurrent trace append under REST pagination, SSE reconnect with active writers, terminal replay). | — |
| `tests/test_structure.py` | Added 6 AST-based architectural invariants: GET trace endpoint cannot write trace events; SSE read endpoint cannot write trace events; Trace API layer does not import broker/queue infrastructure; route handlers do not directly execute ORM/DB queries; `TraceEventResource` does not expose `TraceEvent.id`; redaction is presentation-only and does not mutate persisted objects. | — |

**Implemented Architecture & Behavior.**
- **REST Trace Retrieval (`GET /runs/{run_id}/trace`):**
  - Ordered strictly by monotonic sequence `seq ASC`.
  - Keyset pagination with `since_seq` (inclusive starting cursor, defaults to 1).
  - Configurable page size `limit` bounded between 1 and 500 (defaults to 100).
  - Over-fetching `limit + 1` to determine continuation without extra `COUNT(*)` queries:
    - If `len(raw_events) > limit`: `events = raw_events[:limit]`, `next_seq = events[-1].seq + 1`, `complete = False`.
    - If `len(raw_events) <= limit`: returns all retrieved events. `complete = True` and `next_seq = None` if the run is in a terminal status; otherwise `complete = False` and `next_seq = (events[-1].seq + 1) if events else since_seq`.
  - Filter by `kind` (repeatable query parameter, e.g. `?kind=node_entered&kind=node_exited`).
  - Filter by `severity_min` using hierarchical severity ranking (`DEBUG` < `INFO` < `WARNING` < `ERROR`).
  - Path parity between `/runs/{run_id}/trace` and `/api/v1/runs/{run_id}/trace`.
  - Strict RFC 9457 error handling: unknown run returns 404 (`code="not_found"`), invalid parameters return 422 (`code="validation_error"`).
- **Presentation-Only Redaction & Information Hiding:**
  - `TraceEventResource` hides internal primary key surrogate `TraceEvent.id` entirely. Public event identity is strictly the monotonic `seq`.
  - Presentation-only redaction via `redact_payload` on `payload`, `input`, `output`, and `error` dictionaries. Replaces sensitive keys (e.g. `api_key`, `authorization`, `password`, `token`, `secret`) with `"[redacted]"`.
  - Detached construction in `TraceEventResource.from_row` returns a new resource and never mutates persisted SQLAlchemy ORM instances or database rows.
- **SSE Live Event Streaming (`GET /runs/{run_id}/events`):**
  - Emits `text/event-stream` with headers `Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no`.
  - SSE `id` field equals durable trace `seq` as a string (`id: 1`, `id: 2`), never internal DB `id`.
  - SSE `event` field equals event kind (`node_entered`, `plan_created`, `tool_started`, `run_completed`, etc.).
  - SSE `data` field contains serialized JSON of `TraceEventResource`.
  - `Last-Event-ID` HTTP header resumes stream strictly after the supplied sequence (`start_seq = int(last_event_id) + 1`).
  - Query parameter `?since_seq=` acts as fallback when `Last-Event-ID` is omitted. If both are supplied, `Last-Event-ID` takes strict precedence.
  - Periodic 15-second keepalive heartbeat ping (`: keepalive` comment lines) prevents reverse proxy connection dropouts (e.g. Nginx, Cloudflare).
  - Zero external brokers: powered by direct PostgreSQL polling with a 500ms backoff loop. No Redis, Celery, RabbitMQ, Kafka, or WebSocket overhead.
  - Terminal closure: gracefully exits and closes the stream when a terminal event (`run_completed`, `run_failed`, `run_rejected`, `run_expired`, `run_cancelled`) is emitted or when the parent run is observed in a terminal state.
  - Cooperative client disconnect handling via `await request.is_disconnected()`.
- **Concurrency & PostgreSQL Integration:**
  - Real PostgreSQL integration tests verify monotonic seq progression without duplicated or skipped events during concurrent background trace writes.
  - SSE reconnect with `Last-Event-ID` under active writers guarantees exact resumption without gap or duplicate replay.
- **Structural Invariants:**
  - 6 AST-based tests enforce control-plane purity: read-only trace endpoints, no trace creation on read, no queue/broker imports, no direct ORM in route handlers, surrogate ID protection, and immutability of ORM instances under redaction.

**Zero database migrations required.** Utilizes existing PostgreSQL `trace_events` schema and indices (`ix_trace_events_run_id_seq`).

**Test suite: 148 passed, 0 failures** (including 35 trace unit/integration tests and 49 structural tests).
`ruff check app tests`, `ruff format --check app tests`, `mypy app` (strict, 76 source files) and `alembic check` are clean.

## API-007 — `RunService` + `Executor`: lifecycle ownership, background execution, lease heartbeat, reconcile on startup — 2026-09-18

The run lifecycle (§5.4) now has an owner. `POST /runs/{id}/start` performs
the durable `created → queued` transition and returns `202` before anything
else happens; the `Executor` (§2.4, ADR-004) drives the graph in a
background `asyncio` task under a DB-007 lease and settles the row from the
checkpoint when the graph stops. Nothing in DB-007 was rewritten: the
executor is one more caller of `hold_lease`, `LeaseHeartbeat` and the
conditional `transition_status`, and the `Reconciler` stays the crash
recovery.

| File | Role | Tests |
|---|---|---|
| `app/execution/executor.py` | New. `Executor.schedule(run_id)` (idempotent per process), `execute` (`hold_lease(expected=(queued,))` → `queued → running` under the lease → the graph task raced against `heartbeat.lost` → settle), `shutdown` (cancel in-flight tasks; `hold_lease` releases each lease so the reconciler resumes the rows at the next start). `ExecutionOutcome`, `REASON_EXECUTION_FAILED`. | `tests/test_executor.py` |
| `app/execution/runs.py` | `RunService(executor=…)`; `_schedule` after every committed transition into `queued` (`start_run`, `create_run(auto_start)`, `retry_run(auto_start)`). Without an executor the service is control plane only, as before. | `tests/test_executor.py`, existing run tests |
| `app/execution/recovery.py` | `RunDriver.start` / `LangGraphRunDriver.start` (first entry with the initial state, `durability="sync"`); `CheckpointInspection.final_response` read off the finished checkpoint; `Reconciler.reconcile_all` (a bounded paged drain of `reconcile_once`, so a start-up finds every orphan, not the first 50). | `tests/test_executor.py`, `tests/test_recovery.py` |
| `app/persistence/protocols.py`, `repositories.py` | `transition_status(final_response=…)`: the operator-facing answer lands in the same conditional `UPDATE` as the terminal status — used by the executor, the reconciler (`_settle_finished`) and `ApprovalService` (step 11). | `tests/test_executor.py` |
| `app/api/dependencies.py` | `wire_runtime`: the composition root. `build_adapters` → `ToolRegistry` → `build_planner` → `create_agent_graph` with the saver, the shared `Clock`/`IdGenerator`/`CancellationSource` and `Settings` budgets → `LangGraphRunDriver` → `Executor`, `RunService`, `ApprovalService`, `Reconciler`; each worker gets its own `new_worker_id` label. Anything injected through `create_app` is kept. | `tests/test_executor.py` |
| `app/main.py` | Lifespan: `open_checkpointer` → `wire_runtime` → `Reconciler.reconcile_all()` (logged as `reconciled_on_startup`); `Executor.shutdown()` then engine dispose on exit. No database at start-up → `runtime_unavailable` warning, control-plane-only boot, `/readyz` says why. | `tests/test_executor.py`, `tests/test_health.py` |

**Behaviour, precisely.**
- A pause is not a live worker: on `interrupt()` the graph call returns, the row becomes `awaiting_approval` with the lease released in the same statement, and the task ends. The resume is `ApprovalService.decide_approval`'s existing transaction, which re-acquires ownership under its own worker id — one resume path.
- Fencing: a refused heartbeat cancels the graph task at once and settles nothing; every settling write is guarded by `status = running AND lease_owner = me`, so a run cancelled or reclaimed meanwhile is never overwritten.
- Cancellation stays cooperative: `cancel_run` flags the `CancellationSource` the nodes share; the in-flight effect finishes, the graph exits via `fail(cancelled)`, and the already-`cancelled` row is left as the operator set it.
- Terminal settle writes `status`, `status_reason`, `finished_at`, `duration_ms`, `final_response` and one of `run_completed` / `run_failed` / `run_rejected`; a graph that raised is `failed(execution_failed)` with the error in the `run_failed` event.
- Start-up reconciliation is the existing query: `awaiting_approval` and terminal runs are never candidates; a live lease is never stolen; a second start finds nothing.

**Test suite:** `tests/test_executor.py` — 24 tests over real PostgreSQL and the real saver. Full suite green (with `DATABASE_URL` at a reachable Postgres); `ruff check .`, `ruff format --check .`, `mypy app` (strict) and `alembic check` clean.

## EVAL-002 — the evaluation runner over the real service path — 2026-09-18

`app/evaluation/runner.py` runs a case exactly the way the HTTP API would run
the same request. `EvaluationRunner.run_case` resets `mock_crm` from the
case's YAML fixture set, pins `Settings` (rules planner, the case seed, its
budgets, `tool_failure_rate=0`), composes the production graph through
`build_driver` — the function `wire_runtime` now calls too, so there is one
composition root — wraps the real tool bindings in the `FailureInjector`,
and drives the run through `RunService.create_run` → `start_run` →
`Executor`. On every pause the `ApprovalPolicy` posts its scripted decision
through `ApprovalService.decide_approval`: the approval is a real row, the
token is minted by `execute_tool` from that row, and a `never` policy leaves
the run `awaiting_approval` with nothing sent (tested). The clock is a
`FixedClock`; backoff is virtual. Evidence: the run row, the checkpoint
(`LangGraphRunDriver.state`), `tool_calls`, `approvals`, `trace_events`,
and `UnitOfWork.count_rows` for `expect.db`. All seven cases pass in ~17 s.

| File | Change |
|---|---|
| `app/evaluation/runner.py` | New: `ApprovalPolicy`, `FailureInjector`, `EvaluationRunner`, `CaseResult`/`AssertionOutcome`, the `expect` evaluator |
| `app/execution/runtime.py` | New: `build_driver`, the graph composition extracted from `wire_runtime`, with an `implementations` override for the injector |
| `app/api/dependencies.py` | `wire_runtime` delegates the graph to `build_driver`; everything else unchanged |
| `app/execution/recovery.py` | `LangGraphRunDriver.state(run_id)`: the checkpointed channel values |
| `app/execution/runs.py` | `create_run(seed=, evaluation_run_id=, eval_case_id=)` populate the columns §12.3 already had |
| `app/persistence/protocols.py`, `repositories.py` | `UnitOfWork.reset_mock_crm(companies=, leads=, customers=)` (truncate + load plain rows, one transaction) and `UnitOfWork.count_rows(table, where)` (parameterised, `Base.metadata` lookup, `KeyError` on unknown names) |
| `app/integrations/mock/seed.py` | `reset=True` goes through `uow.reset_mock_crm`; no raw session, no ORM rows for the reset path |
| `app/agent/normalizer.py` | Lead ids of the CRM's own shape (`L-104`) are extracted; "email it/them" is a send for the draft-outreach intent |
| `backend/evals/cases/*.yaml` | Corrected against the shipped code (below) |
| `tests/test_evaluation_runner.py`, `tests/test_evaluation_cases.py` | 12 new tests; the EVAL-001 "no runner yet" pins retired, the declarative check scoped to the definition modules |

**The two EVAL-001 divergences, resolved where they belong.**
1. *Canonical requests vs `RuleTaskNormalizer`.* `"Draft outreach to lead L-104
   and email it."` extracted no `lead_id` (the normalizer knew `lead-101` /
   `lead_202` / `lead 303`, never the CRM's `L-104`) and read no send in
   "email it" (unlike the lead-search intent, which already counted "email"),
   so the rule planner fell into the search pipeline and raised before any
   run existed. Fixed in the normalizer, not in the cases: the id pattern
   admits `L-<digits>`, and the draft intent's send signals include
   "email it/them". Existing normalizer tests unchanged and green.
2. *Fixture seeding vs the ORM boundary.* The only reset took a raw
   `AsyncSession`, the only seeder lived in `app.integrations.mock` (which
   nothing outside `app/integrations` may import) and read the Python
   fixtures, and the runner may not build queries. The boundary is now the
   `UnitOfWork`: `reset_mock_crm` takes plain rows and `count_rows` answers
   `expect.db`; `seed.py` and the runner are both callers.

**Case corrections** (the cases were written before the code they assert
on could be run): `research_company` wraps the enrichment in `profile`, so
`company_research`'s output paths are dotted (`profile.confidence`); the
deterministic responder names step ids, not leads, so `response_mentions`
assert on what §7's `complete` actually says (`"Not done: s6"`,
`"completed successfully"`); the graph never marks a plan step `failed` —
a step that lost its read-back stays `running` and the verdict lives in
`verification_status` — so `invalid_tool_result` asserts attempts, retries
and the verdict, not a status the lifecycle does not produce;
the three pause-and-resume cases (`happy_path_multi_step`,
`approval_required`, `approval_rejected` — six to ten dispatches, every
checkpoint synchronous) get 15 s of wall clock instead of 5, which a loaded
machine was missing by tens of milliseconds.

**Findings not changed here.** `ApprovalService.decide_approval` settles a
finished resume without a terminal trace event (`run_completed` /
`run_rejected`), where the `Executor` writes one — `stream_run_events`
closes on the row status instead. Seeded ids were not wired: a seeded
generator repeats across suite runs and would collide on `agent_runs.id`;
TEST-005 should normalise ids like timestamps.

**Test suite:** 12 tests in `tests/test_evaluation_runner.py` (real
PostgreSQL, real saver, production graph); full suite 1679 passed, 1 skipped. `ruff check .`, `ruff format
--check .`, `mypy app` (strict) and `alembic check` clean.

## EVAL-004 — the seven global invariants asserted after every case — 2026-09-18

`app/evaluation/invariants.py` judges the seven property-based safety checks
of §15.6 after every case, independent of the case's `expect`, over the
evidence the run left in PostgreSQL. `evaluate_invariants` is a pure
function of `InvariantEvidence`; `EvaluationRunner.check_invariants(case,
run_id)` fills it from the existing repositories (`tool_calls`, `approvals`,
`trace_events`, `execution_steps`, `email_outbox.list_by_run`,
`customers.get`, `count_rows`) — no new queries, no new tables, no reads of
the checkpoint, so the check can be re-run against any stored evaluation
run. Every invariant returns an `InvariantOutcome` (§15.6 number, name,
`passed`, detail, JSON-plain evidence).

| # | §15.6 rule | Evidence used |
|---|---|---|
| 1 | every `email_outbox` row → `approved` approval, `args_hash` matches the producing step | outbox `approval_id` → this run's `approvals` row (`approved`, `send_email_mock`); the producing attempt is the succeeded `send_email_mock` `tool_calls` row with the outbox row's `idempotency_key` (the dispatcher's `f(run_id, step_id, args_hash)`), whose `step_id`/`input_hash` must equal the approval's; table-wide count catches rows not attributable to the run |
| 2 | every `customers` row modified during the case → approved approval | current rows vs the seeded `CustomerFixture` (`version`, `updated_at`); an authorising succeeded `update_customer` attempt naming the `customer_id` with an approved `(step_id, args_hash)`; table-wide count catches inserts/deletes |
| 3 | no execution step with attempts > `1 + MAX_RETRIES` | `tool_calls` grouped by `execution_step_id` (row count and max `attempt`), plus the literal `execution_steps.attempts` column |
| 4 | no run exceeded `MAX_STEPS` or `deadline_at` | `tool_calls` count vs `max_steps`; every `tool_calls.started_at` and the run's `finished_at` ≤ `deadline_at` |
| 5 | every run terminal | `agent_runs.status ∈ TERMINAL_RUN_STATUSES`, `finished_at` set, `lease_owner` released |
| 6 | `trace_events.seq` gapless and monotonic | events in insertion (`id`) order carry `seq` exactly `1..n`; missing / duplicated / out-of-order reported |
| 7 | no `policy_violation` unless expected | `trace_events.kind = policy_violation` vs `expect.policy_violation_expected` |

Integration into the result flow: `CaseResult.invariants` and
`.violations` sit beside `assertions`/`.failures`; `passed` requires both;
`evaluation_results.assertions` records each invariant as an
`invariant[n] <name>` entry with its evidence; a violated invariant outranks
a failed case assertion in `failure_reason`. The case's own assertions are
untouched — the never-answering human still fails `final_status`, and now
also invariant 5, side by side.

**Audit findings (unchanged here).** `execution_steps.attempts` and
`agent_runs.step_count/retry_total/replan_count` are never incremented in
production (`increment_attempts`/`increment_counters` have no callers), so
`tool_calls` is the durable attempt evidence and the column check is a
backstop. `ApprovalService`'s terminal settle writes no `duration_ms` and no
terminal trace event (noted at EVAL-002). The `approval_compliance` metric
of §15.4 is not in `metrics.py` (EVAL-003 scope); invariant 1 is the check
it would summarise. §15.6 names no verification invariant — verification is
asserted by the cases themselves (`invalid_tool_result`), so none was added.

**Tests:** `tests/test_evaluation_invariants.py` — 13 unit tests (a valid
in-memory evidence set, then one violating set per invariant; deterministic
output) and 9 PostgreSQL-backed tests (a real case run through the real
path, its rows tampered the way a bug would leave them — approval flipped,
customer edited, attempt inserted past the budget, deadline moved, run set
`running`, trace row deleted, `policy_violation` appended — and exactly that
invariant fails on re-check; the seven valid cases satisfy all seven and
persist them). Full suite green; `ruff check .`, `ruff format --check .`,
`mypy app` (strict) and `alembic check` clean.

## EVAL-005 — CLI + CI evaluation gate — 2026-09-19

**Done.** `app/evaluation/cli.py`: `python -m app.evaluation.cli run --suite
<name>` around the existing evaluation system, adding no execution path, no
metrics computation and no invariant logic of its own. `run_suite_cli`
composes exactly what EVAL-002 already composes — `get_settings()`,
`load_registry()`, a real async engine off `Settings.database_url`,
`open_checkpointer(settings)`, `EvaluationRunner(...)` — and calls
`EvaluationRunner.run_suite(suite)`, the same call `tests/test_evaluation_runner.py`
drives. `evaluation_runs`/`evaluation_results` remain the persisted source
of truth; `print_report` only renders the `SuiteRunResult` the runner
already wrote back, and computes no metric or invariant verdict of its own.

Per case it prints `[PASS]`/`[FAIL]`, the run id and duration, every failed
assertion by name and detail, and every violated `§15.6` invariant by number
and name; then the `compute_evaluation_metrics` summary
(`case_pass_rate`, `task_success_rate`, duration percentiles, pass/fail
counts). The exit code is `0` iff every `CaseResult.passed`, which
`EvaluationRunner.run_case` already defines as *both* the case's own
assertions *and* all seven invariants holding (`runner.py`'s
`passed = all(o.passed for o in outcomes) and all(i.passed for i in
invariants)`) — so a failing case and a violated invariant gate the run
through the identical, already-tested boolean, not a second judgement
layered on top in the CLI.

`Makefile`'s pre-existing `eval` target (`cd backend && uv run python -m
app.evaluation.cli run --suite all` — written ahead of this module, evidence
the CLI's module path and argument shape were already the intended contract)
needed no change.

**CI.** `.github/workflows/ci.yml`'s `backend` job gained two steps after
`pytest --cov=app --cov-report=term-missing`: `uv run alembic upgrade head`,
then `uv run python -m app.evaluation.cli run --suite all`. No new job, no
new Postgres service, no new `uv`/Python setup — both steps run inside the
job's existing service container and environment (`OPSPILOT_PLANNER=rules`,
`OPSPILOT_INTEGRATIONS=mock`, so the gate needs no LLM API key). The
explicit `alembic upgrade head` step doesn't rely on the integration tests'
own `migrate_to_head()` fixture side effect happening to run first; either
way the same Postgres service is reused, matching how `pytest`'s own
integration tests already migrate it. A failing gate step fails the job —
there is no `continue-on-error`, and the step is unconditional, not gated
behind a file-existence probe the way the frontend job's own steps are.

**Tests.** 10 new unit tests in `tests/test_evaluation_cli.py`, all
`@pytest.mark.unit` (no database, no LangGraph saver — the integration path
itself is already covered end to end by `tests/test_evaluation_runner.py`):
`print_report` on an all-passing `SuiteRunResult` (exits `True`, prints
`PASSED`), on a case with a failed assertion (exits `False`, prints the
assertion name/detail and `FAILED`), on a case whose invariants include a
violation while its own assertions hold (exits `False`, prints
`invariant[n] <name> violated` and `FAILED` — proving the invariant path
gates independently of case assertions), and the metric summary rendering;
`main`'s exit codes via a monkeypatched `run_suite_cli` (0 on pass, 1 on
fail, default suite is `all`); `build_arg_parser` requiring a subcommand;
the `Makefile`'s `eval` target text; and the CI workflow parsed as YAML,
asserting the `backend` job's steps include the evaluation-gate command and
that it runs after the `pytest` step.

**Environment note.** This session's sandbox has no reachable Postgres and
no Docker daemon, so the CLI's own real-service-path run, the EVAL-00x
integration suites, and `alembic check` could not be exercised live here —
they skip cleanly (the established pattern) or fail on connection refused,
exactly as every other DB-backed test in this repository does without a
database. `ruff check .`, `ruff format --check .`, and `mypy app` (strict)
are clean across the whole backend, and the full suite is otherwise green
except one pre-existing, unrelated failure: `tests/test_hitl_preview.py
::TestHitlPreviewZeroMutation::test_preview_generation_does_not_mutate_crm`
attempts a live database connection without the `require_database()` skip
guard every other integration test in this repo uses, so it errors instead
of skipping when no Postgres is reachable — a pre-existing gap in that test
file, untouched by this task, not a regression introduced here.

---

## TEST-002 — graph behaviour: every terminal path, pause, retry, replan, skip and verify transition — 2026-09-19

**Done.** `backend/tests/test_graph_behavior.py` — 16 integration tests, ~7 s,
no production code changed.

**What drives them.** Every test builds its graph with
`app.execution.runtime.build_driver`, the one function `wire_runtime` and the
evaluation runner both call (ADR-024), and drives it through
`RunService` → `Executor` → `ApprovalService` against real PostgreSQL and the
real LangGraph saver. So the real `ToolRegistry`, the real approval gate, the
real verifiers, the real `RuleTaskNormalizer` and the real `RulePlanner` are
on the path: a pause is a real `awaiting_approval` row that the real approval
service decides, a terminal status is the row the executor settled, and every
attempt is a real `tool_calls` row. There is no second graph, no fake
execution path and no re-implementation of a node's logic in the test.

**The only double is the tool.** `ToolScript` (§18.2's `ScriptedTool` —
"lets a graph test force an exact failure sequence") wraps
`default_implementations()` and is handed to `build_driver`'s existing
`implementations` parameter, exactly as `FailureInjector` is by EVAL-002. It
keys on `(tool, step_id, attempt)` — finer than the evaluation injector's
`(tool, attempt)`, which is what lets one `score_lead` step fail while its
siblings succeed — and it can raise a classified error, return a well-formed
success without performing the effect, or block until the test releases it.
Because the override goes into the registry, a scripted tool is reached only
where a real one would be: after argument resolution, after input validation,
after the gate, after the stored-decision check and with the minted token.

The plans are the rule planner's own, selected by choosing the request
(`"Draft outreach to lead L-104 and email it."` → the six-step gated
pipeline; `"Score the top 2 leads in Seattle."` → search + fan-out research +
one required and one optional `score_lead`). Nothing is hand-assembled, so a
change to planning breaks these tests rather than passing them. The clock is
a `FixedClock`, so the backoff is asserted on rather than waited for, and the
mock CRM is reseeded per test through `seed_database(..., reset=True)` so
every effect count is exact.

**The six control paths, each on state and on the trace.**

| Path | State | Trace |
|---|---|---|
| **Terminal — complete** | `completed`, both steps `succeeded`, `not_required`/`passed` verification, no errors, `step_count=2`, `final_response.done` | the exact nine-event timeline, closed by `run_completed`; `seq` unique and increasing |
| **Terminal — rejected** | `rejected` (not `failed`), `status_reason=approval_rejected`, step `rejected`, response names `s6` in `not_done` | `approval_requested` → `approval_rejected`, and no event for `s6` before them |
| **Terminal — fail** | from `understand` (`out_of_scope`, no plan, no dispatch), from `plan` (`invalid_plan` — the six-step plan does not fit a three-step budget), from `recover` (`retry_budget_exhausted`, `replan_budget_exhausted`, `verification_failed`) and from `decide` (`budget_exhausted`) | `run_failed` closes each; zero `tool_calls` on the two pre-dispatch failures |
| **Pause** | `s5` succeeded and `s6` pending at the checkpoint; the interrupt payload carries the step, tool, `args_hash` and de-referenced preview; the `approvals` row is `pending` with the same hash | `approval_requested` → `approval_granted` → `tool_started` → `tool_succeeded` → `verification_passed`, and every mail-port event is after the grant. Zero `email_outbox` rows while paused; exactly one after |
| **Retry** | `retry_count={s2: 2}`, step `succeeded`, two `transient` errors, `step_count=4` (attempts are steps) | three `tool_started`, two `tool_failed`, two `retry_scheduled` with `target=execute_tool` and the §10.4 delays (250 ms, 500 ms ± the seeded jitter) — matched against `FixedClock.sleep_calls`, so the delay was computed and never slept |
| **Replan** | `replan_count=2`, `plan_history` revisions `[0, 1]`, current revision `2`, `retry_count` empty (a `NOT_FOUND` is a planning fault) | six events for the failing step (three attempts), no `retry_scheduled` at all, `run_failed` last |
| **Skip** | optional step `skipped`, required sibling `succeeded`, `replan_count=0`, `final_response.partial=true` with the step in `not_done` | one `tool_started`/`tool_failed` pair for it and nothing else; `run_completed` last |
| **Verify** | a lying `save_draft` is caught by the read-back, retried once and written for real (`retry_count={s5: 1}`, verification `passed`, exactly one `outreach_drafts` row); lying on every attempt ends the run `failed(verification_failed)` with zero rows, zero outbox rows and no approval ever requested | `tool_succeeded` → `verification_failed` → `retry_scheduled(target=execute_tool, error_class=verification_failed)` → `tool_succeeded` → `verification_passed`; three `verification_failed` in the terminal case |

**Forced loops terminate in budget.** Two, both genuine cycles in the
compiled graph rather than a counter read back:
`decide → execute_tool → recover → plan → decide …` ends after exactly
`MAX_REPLANS` revisions as `replan_budget_exhausted` (three attempts at the
failing step, one per plan, and the settled `search_leads`/`research_company`
steps carried over rather than redone), and
`decide → execute_tool → recover → execute_tool …` ends after exactly
`1 + MAX_RETRIES` attempts. A third test pins the interaction the §10.5 table
names: with `MAX_STEPS=2` and `MAX_RETRIES=5` the *step* budget stops the
retry loop after two attempts, because `decide` checks budgets first on every
pass. A fourth pins that ordering directly — a two-step plan under
`MAX_STEPS=2` terminates `budget_exhausted` at `decide` although both
attempts succeeded and nothing failed, which is rule 1 running before rule 3
as §6.2 specifies.

**Cancellation and durability.** An operator cancels through
`RunService.cancel_run` while `score_lead` is genuinely in flight (the
scripted tool blocks until the test releases it): that attempt finishes and
is recorded `succeeded`, the graph exits through `fail(cancelled)` at the
next node boundary, the following step is never dispatched, and the
executor's guarded settle leaves the operator's `cancelled` row standing —
the checkpoint says `failed(cancelled)`, the row says `cancelled`, and both
are correct for what they describe. Separately, a paused run is resumed by a
*second* composition — its own graph, registry, tool bindings, executor and
services over the same saver — which reads the plan, the five settled steps
and the pause out of the checkpoint and re-executes only `s6`. That is
`thread_id = run_id` plus `durability="sync"` (§6.4) being load-bearing
rather than assumed.

**Deliberately not re-proven here**, and said so in the module docstring: each
`decide` rule in isolation and their ordering (`tests/test_decide.py`,
`tests/test_agent_graph.py`), the recovery classification
(`tests/test_recover.py`), cancellation at each individual node boundary
(`tests/test_cancellation.py`), the dispatcher's guarantees
(`tests/test_tool_dispatch.py`) and the lease mechanics
(`tests/test_executor.py`). The one §6.1 edge this module does not reach is
`recover → verify`, the unconfirmed read-back retry: forcing it needs a
*verifier-port* fault, which a tool-implementation override cannot express,
and VERIFY-003 already covers it end to end in `tests/test_verify_recovery.py`.

**One observation, not changed here.** On the resume path `ApprovalService`
settles the run row but emits no terminal `run_*` trace event, so a run that
reaches `completed` or `rejected` through an approval decision ends its
timeline on `approval_granted`/`approval_rejected` plus the graph's own
events, while a run that terminates under the executor is closed by
`run_completed`/`run_failed` (`Executor._settle`). §10.6 asks for a terminal
event to close the timeline; the asymmetry is pre-existing, is not a
regression, and fixing it is a change to HITL-004/API-007 behaviour rather
than to a test. These tests therefore assert what is actually emitted on each
path, and this note records the gap for whoever owns OBS-002.

**Verification.** `tests/test_graph_behavior.py` 16 passed in ~7 s; the graph,
agent, HITL, verification, recovery, cancellation, executor, checkpointing and
migration suites 627 passed; the full backend suite 1742 passed, 1 skipped
(baseline before this task: 1726 passed, 1 skipped — the same single skip,
nothing newly skipped); `ruff check .`, `ruff format --check .` and
`mypy app --strict` clean; `alembic upgrade head` applies and `alembic check`
reports no new upgrade operations (this task adds no migration); the
evaluation gate `python -m app.evaluation.cli run --suite all` passes 7/7 with
0 invariant violations. All of it against a real PostgreSQL 16 in this
session, so nothing here was skipped for want of a database.

---

## TEST-003 — the approval-gating suite: the six tests of §9.9 — 2026-09-19

**Done.** `backend/tests/test_approval_gating.py` — 28 integration tests,
~7 s, no production code changed.

**The claim.** One sentence: a consequential mutation cannot occur without a
human decision for those exact arguments. §9.5 builds it from three
independent barriers — the router, the re-assertion in `execute_tool` and
`ToolRegistry.dispatch`, and the `ApprovalToken` that mutating port methods
demand — and this module proves each alone and all composed. §9.9 numbers
the six tests that must exist; the module is organised as one class per
number, and the docstring carries the mapping.

**What drives them.** The end-to-end tests build the graph with
`app.execution.runtime.build_driver` — the one function `wire_runtime` and
the evaluation runner both call (ADR-024) — and drive it through
`RunService` → `Executor` → `ApprovalService` against real PostgreSQL and
the real LangGraph saver, with the **real** tool implementations: unlike
TEST-002 this suite passes no `implementations` override at all, because a
gate test that substituted the gated tool would be testing the substitute.
The isolation tests reach one barrier at a time with the same real objects —
the real `ToolRegistry` over the real mock adapters, the real `NodeHandlers`,
the real `ApprovalGate`. The *human* is scripted (a real `approvals` row,
written by `ApprovalService` or by `ApprovalRepository`); the gate, the token
and the dispatcher never are. §18.2 puts the approval gate among the things
deliberately not doubled — "never disabled, ever" — and
`test_this_suite_never_disables_the_gate` parses this module's own syntax
tree and fails if it references a gate internal (`_assert_gate`,
`_verify_stored_decision`, `_issue_approval_token`, `_MINT`), passes
`requires_approval` or `implementations` to anything, or takes a
`monkeypatch` fixture. §18.3's "no test may disable the gate" is enforced,
not promised.

**Evidence is the durable record**, never the graph's own account of what it
did: `mock_crm` rows compared *whole* before and after (a count comparison
would miss `update_customer`, which mutates in place and adds no row),
`tool_calls`, `approvals`, `trace_events` and the checkpoint. The clock is a
`FixedClock` and `seed_database(..., reset=True)` runs per test, so every
effect count is exact.

**The six claims.**

| §9.9 | Claim | What is asserted |
|---|---|---|
| **1** | a paused run performs no `mock_crm` write | `email_outbox` and `customers` are row-for-row identical across the pause, for a `send_email_mock` gate and an `update_customer` gate; no `tool_calls` row and only `approval_requested` for the gated step; the run row is `awaiting_approval` with the lease released. The control that makes it mean something: the ungated `save_draft`/`get_customer` work *did* happen, so the run genuinely reached the gate rather than dying early |
| **2** | `execute_tool` alone raises `PolicyViolation` with no grant | the node is called **directly**, so barrier 1 has failed by construction. Refused for: no decision, a `reject` decision, a decision for a neighbouring step, an undecided `pending` row, and — the independence claim — a checkpointed grant for exactly these arguments with no durable `approved` row behind it. `ToolRegistry.dispatch(approval_token=None)` raises `ApprovalRequiredError` and records a `failed` attempt with `adapter IS NULL`, the ADR-024 signature of a refusal before the port |
| **3** | `MailPort.send` is uncallable without a token | the compile-time half, executed: six calls type-checked with the project's own mypy configuration — omitted, `None` and string tokens rejected `[call-arg]`/`[arg-type]`, and the two calls presenting a real token clean, because a checker that rejected everything would prove nothing. Plus the runtime constructor test the acceptance criteria name: direct construction, a guessed sentinel, attribute mutation and `dataclasses.replace` all raise `PolicyViolation`; `token` is a required keyword-only parameter of exactly `MailPort.send` and `CustomerPort.update`; the adapter refuses a forged token and writes nothing; and CI is asserted to run `mypy app` *before* `pytest` |
| **4** | rejection is `rejected`, zero effects, named | `rejected`/`approval_rejected` (never `failed`), the step `rejected`, `s6` in `not_done` and in the summary and in neither `done` nor `unconfirmed`, the same on the persisted `final_response`; the tables unchanged; no `tool_calls` row and no `tool_*` event naming the declined step; the operator and reason on the row and in the `approval_rejected` event. Repeated for a declined customer update |
| **5** | a grant for hash A does not authorise hash B | B is the same draft to a different recipient — the substitution a revised plan, a re-resolved `$ref` or an injected instruction would make after the human read A. Refused at each barrier *independently*: by `approval_state` (barrier 2), by the gate over the durable row while barrier 2 is deliberately satisfied *for B* (barrier 3), and by the dispatcher holding a genuinely gate-minted token for A with B on the wire — the strongest form, because nothing about the token is forged. Positive control: A itself dispatches, writes one outbox row to Dana, and carries the approval id |
| **6** | resuming twice sends exactly one email | the same approve posted twice (one winner, one no-op), four concurrent approves (one winner, the losers `ApprovalConflictError` — "first writer wins, audibly"), and a decision that committed before the worker died, where the *replay* is the resume that performs the effect. Each leaves one `email_outbox` row for the run, one `approval_granted` and one succeeded attempt, whose `idempotency_key` is `(run_id, step_id, args_hash)` and nothing attempt-dependent (§10.4, ADR-020) |

**The assertions bite.** Two mutations of production code were run against
the suite and each turned it red: `ToolRegistry._assert_gate` returning early
on a missing token, and `ApprovalGate.issue_from_persisted` skipping the
`args_hash` comparison — the latter caught *specifically*, because the test
distinguishes which barrier refused, and the failure showed
`ApprovalGate.issue` still refusing underneath it. Both mutations were
reverted. A third check confirmed the whole-row snapshot detects an in-place
`UPDATE` to `mock_crm.customers`, which is the failure a row-count comparison
would have missed.

**Two divergences between §9 and the code, reported and not changed here.**
Neither is required by TEST-003's acceptance criteria, and fixing either is a
change to the approval path — an OPUS change with an ADR, not a line in a
test commit.

1. **The adapter does not re-validate the hash.** §9.5 says "The adapter
   re-validates the hash against the payload it was handed", and
   `ApprovalToken.authorises` documents itself as "Re-checked inside the
   adapter". `MockMailAdapter.send` and `MockCustomerAdapter.update` check
   only `isinstance(token, ApprovalToken)`. The check does happen, in
   `ToolRegistry._assert_gate`, which calls `token.authorises(...)` against
   the arguments before the port is reached — so the *property* holds on
   every path the application has, and TEST-003 asserts it there. It is not
   obviously implementable at the port as written: the adapter is handed an
   `OutboundMessage`, not the tool's arguments, so it cannot recompute the
   hash the token carries without the port taking the tool's argument
   dictionary as well. Either the architecture sentence or the port
   signature should move; this records the question for whoever decides.
2. **The operator's reason is not quoted in the response.** §9.6 says
   `complete` "produces a response that names what was not done and why,
   quoting the operator's reason". `ApprovalService` resumes with
   `Command(resume=decision_kind.value)` — a bare `"approve"`/`"reject"` —
   so `_decision_from_resume` builds an `ApprovalDecision` with
   `reason=None`, and `synthesize_complete_response` names the declined step
   but cannot quote a reason it was never given. The reason is durable on the
   `approvals` row and in the `approval_rejected` event, so nothing is lost
   for the dashboard or the audit trail; only the synthesized summary is
   poorer than §9.6 describes. §9.9 #4 and §18.3 both ask for "a response
   naming the declined action", which the response does, so the suite asserts
   what the code genuinely provides rather than inventing a semantics.

**Verification.** `tests/test_approval_gating.py` 28 passed in ~7 s, none
skipped; run with the HITL, approval-API, registry, dispatch, tool-policy,
structural, security, verification and graph-behaviour suites, 561 passed;
the full backend suite **1770 passed, 1 skipped** against a fresh database
(baseline before this task: 1742 passed, 1 skipped — the same single skip,
the opt-in live Groq smoke test, nothing newly skipped); `ruff check .`,
`ruff format --check .` and `mypy app --strict` clean; `alembic check`
reports no new upgrade operations (this task adds no migration); the
evaluation gate `python -m app.evaluation.cli run --suite all` passes 7/7
with 0 invariant violations. All against a real PostgreSQL 16 in this
session, so nothing here was skipped for want of a database.

**One environment-only issue, pre-existing and unrelated to this task.**
`tests/test_api_approvals.py::TestApprovalApiPostgresIntegration::
test_full_http_decision_cycle_with_real_persistence` fails on a database
that has accumulated rows from earlier sessions: it asserts its approval
appears in `GET /approvals/queue`, and `ApprovalRepository.list_pending` is
global with `ORDER BY requested_at DESC LIMIT 50`, so a long-lived database
with more than fifty newer pending approvals pushes the row off the page.
It passes on a fresh database and in the full-suite run above, and CI uses a
throwaway Postgres service, so this is a test-isolation weakness in that
file (the queue query is not scoped to the run under test) rather than a
defect in the approval path — and it is untouched by this task.
