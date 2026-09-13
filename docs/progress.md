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

**Phase 1 — implementation: started.** FOUND-001..004 and DB-001..007 are
done. Continue at `docs/handoff.md` §3 with TOOL-001.

```
architecture   ████████████████████  complete
contract spine ████████████████████  complete (state, contracts, errors, security, config)
foundation     ████████░░░░░░░░░░░░  FOUND-001, 002, 003, 004 done; 005 outstanding
persistence    ████████████████████  DB-001..007 done
tools          ░░░░░░░░░░░░░░░░░░░░  TOOL-001..006
agent graph    ██░░░░░░░░░░░░░░░░░░  AGENT-001 done; 002..009 outstanding
hitl           ░░░░░░░░░░░░░░░░░░░░  HITL-001..005
verification   ░░░░░░░░░░░░░░░░░░░░  VERIFY-001..003
api            ░░░░░░░░░░░░░░░░░░░░  API-001..007
observability  ░░░░░░░░░░░░░░░░░░░░  OBS-001..005
frontend       ░░░░░░░░░░░░░░░░░░░░  FE-001..008
evaluation     ░░░░░░░░░░░░░░░░░░░░  EVAL-001..005
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

Next task: **TOOL-001** (`integrations/ports.py` Protocols, mock adapters,
seed dataset) — SONNET, depends only on DB-004 and is the head of the tools
chain. **AGENT-002** now has its checkpointer; it still depends on
AGENT-003..008. **API-007** depends on DB-007 (done) and AGENT-002.

The throwaway Postgres container (`opspilot-pg-dev`, port 55432) is still
running; remove with `docker rm -f opspilot-pg-dev` once no longer needed.
