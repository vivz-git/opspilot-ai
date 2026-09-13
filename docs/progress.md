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

**Phase 1 — implementation: started.** FOUND-001 (the FastAPI app factory) is
done. Continue at `docs/handoff.md` §3 with FOUND-002.

```
architecture   ████████████████████  complete
contract spine ████████████████████  complete (state, contracts, errors, security, config)
foundation     ██████░░░░░░░░░░░░░░  FOUND-001, 002, 004 done; 003/005 outstanding
persistence    ░░░░░░░░░░░░░░░░░░░░  DB-001..007
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

**Test suite: 242 passed, 1 skipped** (`cd backend && uv run pytest`)
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

Next task: **FOUND-003** (Alembic init with the three schemas) — the task
that actually unblocks the critical path (`DB-001` depends on it). It needs
a live Postgres to verify its acceptance criteria; try `docker compose up -d
db` (or start Docker Desktop) before starting it. **FOUND-005** (CI green on
the real matrix) is now also unblocked, since it depended only on FOUND-002.
