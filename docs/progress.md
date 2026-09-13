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

**Phase 1 — implementation: started.** FOUND-001..004 and DB-001..002 are
done. Continue at `docs/handoff.md` §3 with DB-003.

```
architecture   ████████████████████  complete
contract spine ████████████████████  complete (state, contracts, errors, security, config)
foundation     ████████░░░░░░░░░░░░  FOUND-001, 002, 003, 004 done; 005 outstanding
persistence    ████████░░░░░░░░░░░░  DB-001, 002 done; 003..007 outstanding
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
| `docs/decisions.md` | 22 ADRs, each with the rejected alternative and the cost; 6 open questions with current defaults |
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
cross-check. (Running `alembic check` locally shows cosmetic false-positive
FK diffs caused by this machine's Postgres role being named `opspilot`,
which happens to collide with the schema name and change Postgres's default
`search_path` resolution during reflection — verified as a reflection
artifact, not a real schema mismatch, by inspecting `\d` on every table
directly against the live container.)

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
