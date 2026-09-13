# Architecture Decision Records

Each record states the decision, why the obvious alternative was rejected, and
what the decision costs. A decision with no cost listed is a decision that was
not actually examined.

Status values: **accepted** (in force), *proposed*, ~~superseded~~.
Reversing an accepted ADR requires a new ADR, not an edit.

| ADR | Decision | Status |
|---|---|---|
| [001](#adr-001) | LangGraph as the orchestration runtime | accepted |
| [002](#adr-002) | Dual planner: deterministic rules + LLM, `auto` by default | accepted |
| [003](#adr-003) | Explicit typed plan with stable step ids and `$ref` binding | accepted |
| [004](#adr-004) | In-process asyncio executor, with a documented upgrade path | accepted |
| [005](#adr-005) | Sequential step execution in v1 | accepted |
| [006](#adr-006) | Fan-out expanded at decide time, not by replanning | accepted |
| [007](#adr-007) | Dynamic `interrupt()` rather than static interrupt lists | accepted |
| [008](#adr-008) | `save_draft` is deliberately not approval-gated | accepted |
| [009](#adr-009) | Lead scoring is a rule engine, not an LLM | accepted |
| [010](#adr-010) | Approval bound to a canonical argument hash, enforced by a token | accepted |
| [011](#adr-011) | Three Postgres schemas: control plane, runtime, simulated world | accepted |
| [012](#adr-012) | Two sources of truth, with an explicit reconciler | accepted |
| [013](#adr-013) | Frontend types generated from OpenAPI | accepted |
| [014](#adr-014) | JSONB for evolving shapes, promoted columns for anything queried | accepted |
| [015](#adr-015) | The trace is a product feature; OpenTelemetry is deferred | accepted |
| [016](#adr-016) | Deterministic evaluation only; no LLM-as-judge gate in v1 | accepted |
| [017](#adr-017) | No authentication in v1, fenced by startup fuses | accepted |
| [018](#adr-018) | A real sender will be a new tool, never a flag on the mock | accepted |
| [019](#adr-019) | Postgres is the only infrastructure dependency | accepted |
| [020](#adr-020) | Retry safety comes from keyed effects, not from hope | accepted |
| [021](#adr-021) | Verification is a graph node, not a tool wrapper | accepted |
| [022](#adr-022) | `rejected` is a terminal status distinct from `failed` | accepted |
| [023](#adr-023) | Run ownership is a fenced lease; recovery is a checkpoint-driven state machine | accepted |

---

## ADR-001
### LangGraph as the orchestration runtime

**Context.** The agent needs explicit state, conditional branching, durable
pause/resume for human approval, and the ability to restart mid-run.

**Decision.** Use LangGraph with the Postgres checkpointer as the orchestration
runtime. Nodes are thin functions returning state deltas; all control flow is
edges.

**Consequences.** Pause/resume and checkpointing come for free and are battle
-tested. We inherit LangGraph's re-execution semantics on resume, which forces
every interrupting node to be idempotent (§9.7) — a real cost that shows up in
`merge_approval_state` and the `approvals` upsert. We also accept a third-party
table namespace, which is why it gets its own schema (ADR-011).

**Alternatives rejected.** A hand-rolled state machine: we would have written a
worse checkpointer. A plain ReAct loop: no explicit plan, therefore no
plan-vs-actual, no meaningful approval preview, and no structural place to put
verification.

---

## ADR-002
### Dual planner: deterministic rules + LLM, `auto` by default

**Context.** LLM planning is the product, but a nondeterministic planner makes
evaluation meaningless and makes the system unrunnable without a paid key.

**Decision.** One `Planner` protocol, two implementations. `OPSPILOT_PLANNER=auto`
(default) uses the LLM when `ANTHROPIC_API_KEY` is present and the rule planner
otherwise; `llm` and `rules` force the choice. `planner_kind` is recorded on
every run.

**Consequences.** The whole system — nine tools, approvals, verification,
evaluation, dashboard — runs end to end with no credential, so CI needs no
secret and a new contributor is never blocked. Regression evaluation is
deterministic. The cost is two planners to maintain and a rule planner whose
coverage of phrasings is narrow; it is a scaffold for evaluation, not a product
feature, and it must be documented as such so nobody mistakes its limits for the
agent's limits.

**Alternatives rejected.** LLM-only with recorded cassettes: cassettes go stale
and silently encode yesterday's behaviour. LLM-only with a low temperature:
"nearly deterministic" is not deterministic, and the failures are the
interesting cases.

---

## ADR-003
### Explicit typed plan with stable step ids and `$ref` binding

**Decision.** The planner emits a typed `Plan` of `PlanStep`s with stable
`step_id`s. Arguments are literals or `{"$ref": "s1.output.leads.0.company_id"}`
resolved against an artifact store by a path language with no expressions.

**Consequences.** Everything else becomes addressable: approvals bind to a
`step_id`, traces cite one, the UI can show plan-vs-actual, and an operator can
see what happens next before approving. The path language's deliberate weakness
means some plans are inexpressible (no arithmetic, no filtering) and must be
expressed as extra steps instead — accepted, because an expression evaluator
consuming model output is a code-execution surface (§16.3).

**Alternatives rejected.** Free-form tool calling: nothing to preview, nothing
to verify against. A general expression language: strictly more power and
strictly more attack surface, for a workflow that does not need it.

---

## ADR-004
### In-process asyncio executor, with a documented upgrade path

**Context.** A run is long-lived, may pause for a human for hours, and must
survive a process restart. A real system would use a worker queue.

**Decision.** v1 drives the graph as an `asyncio` task inside the API process.
Nothing durable lives in the task: the checkpoint, run status, steps and trace
are all in Postgres. `agent_runs.lease_expires_at` is a heartbeat, and a startup
reconciler either re-enters an orphaned run from its checkpoint or marks it
`failed(orphaned)`.

**Consequences.** One process, no broker, trivial local setup — appropriate for
a single-operator tool. The honest limits: no horizontal scaling, a deploy
interrupts in-flight runs (they resume from checkpoints), and the reconciler is
now load-bearing and must be tested. Because the durable state is already
external, moving to arq/Celery plus a `run_queue` table is an infrastructure
change rather than a redesign.

**Alternatives rejected.** A worker queue now: a broker, a second deployable and
a new failure mode, for a system with one operator. `BackgroundTasks`: tied to a
request's lifecycle, so it cannot own a run that outlives the response.

---

## ADR-005
### Sequential step execution in v1

**Decision.** Steps execute one at a time, even when independent.

**Consequences.** Deterministic trace ordering (which the evaluation determinism
guarantee depends on), simple reducers, and unambiguous approval semantics — two
steps cannot interrupt simultaneously. The cost is latency: three
`research_company` calls that could overlap do not. For a three-lead workflow
against mock adapters this is invisible; against a real enrichment API it would
matter, and that is when this ADR should be revisited.

**What parallelism would require** (so the future change is scoped): concurrent
-write-safe reducers on every channel, a trace ordering key that tolerates
interleaving, a fan-out barrier before any gated step, and a per-run concurrency
budget.

---

## ADR-006
### Fan-out expanded at decide time, not by replanning

**Decision.** A `fanout` step is expanded into concrete child steps by `decide`
once the referenced list exists. `max_items` is mandatory.

**Consequences.** Replan budget is reserved for actual faults; plans stay
readable ("research each lead" is one step, not N); children are individually
traced, retried and gated. The cost is that `decide` is no longer purely a
router — it mutates the plan — so its expansion path needs its own tests, and
expanded children must count against `MAX_STEPS` or fan-out becomes an unbounded
loop.

**Alternatives rejected.** Replanning after every list-producing step: burns the
replan budget on a non-error and conflates "the agent changed its mind" with
"the agent is iterating". Planning N steps up front: impossible, since N is not
known before the search runs.

---

## ADR-007
### Dynamic `interrupt()` rather than static interrupt lists

**Decision.** `compile()` declares no `interrupt_before`/`interrupt_after`.
Pausing is decided inside `request_approval` by calling `interrupt()`.

**Consequences.** Whether a step needs approval depends on its tool contract and
its arguments, not on the identity of a node — which a static list cannot
express. The cost is that the interrupting node is re-executed on resume and
must therefore be idempotent; §9.7 and `merge_approval_state` pay that cost
explicitly.

---

## ADR-008
### `save_draft` is deliberately not approval-gated

**Context.** `save_draft` mutates state, and the policy is that mutations need
approval. Applying the rule literally would gate it.

**Decision.** Gate on *outbound, destructive, or business-owned-record*
mutations. `save_draft` writes an internal, reversible, non-outbound artifact,
so it is not gated — but it is `INTERNAL_WRITE` and requires readback
verification.

**Consequences.** The canonical workflow raises exactly one approval, at the
moment that matters ("send this"), and the operator approves a draft that has
already been persisted and verified. This is a safety argument, not a
convenience one: **approval fatigue is a safety failure.** A system that asks
about every internal write teaches operators to approve without reading, which
defeats the gate on the send. The cost is a policy with a judgement in it rather
than a mechanical "all writes", so invariant P1 encodes the side-effect classes
explicitly and a test enforces exactly which two tools are gated.

---

## ADR-009
### Lead scoring is a rule engine, not an LLM

**Decision.** `score_lead` computes a weighted score from the company profile
with configurable weights and returns a `factors` breakdown. No model call.

**Consequences.** Lead ranking becomes evaluable: the `lead_ranking` case
asserts an exact ordering rather than tolerating sampling variance. The
`factors` breakdown also gives the dashboard a real justification to show, and
identical inputs provably produce identical scores. The cost is that scoring
quality is bounded by the rules; qualitative signals in `summary` are only used
for personalization, not for the score. An LLM scorer could be added later as a
separate tool with its own contract and its own non-gating evaluation.

---

## ADR-010
### Approval bound to a canonical argument hash, enforced by a token

**Context.** A step-id-only grant leaves a time-of-check/time-of-use gap: a
replan or re-resolution can change the arguments after the human approved.

**Decision.** Approval binds to `canonical_args_hash(resolved_args)` with
volatile keys excluded. The gate grants a step only when the hash of the
arguments about to be sent equals the hash the human saw. Three barriers enforce
it: the `decide` router, `execute_tool`'s independent re-assertion, and an
`ApprovalToken` that only `ApprovalGate.issue` can mint from a persisted
approved decision and that mutating port signatures require.

**Consequences.** "Approve sending draft A" cannot authorise sending draft B,
and a `STALE_WRITE` retry automatically forces a fresh approval because the new
arguments hash differently (§10.3) — a correct behaviour that falls out of the
design rather than needing special-case code. No single bug produces an
unapproved mutation. The costs: canonicalisation must be exactly right (the
volatile-key list is security-relevant, and a retry's new idempotency key must
not invalidate a grant — tested), and every future adapter must thread a token,
which is the point.

---

## ADR-011
### Three Postgres schemas: control plane, runtime, simulated world

**Decision.** `opspilot` (our tables), `langgraph` (checkpointer-owned),
`mock_crm` (the simulated system of record).

**Consequences.** "Replace the mock with something real" becomes an adapter
change rather than a schema entanglement, and a LangGraph upgrade cannot collide
with an Alembic revision. It also makes a hard rule expressible: verification
reads `mock_crm` through a port, never the control plane — otherwise
verification would only confirm that we wrote down what we were told. The cost
is cross-schema awareness in migrations and connection setup.

---

## ADR-012
### Two sources of truth, with an explicit reconciler

**Context.** The LangGraph checkpoint can resume a run but cannot be queried;
our tables can be queried but cannot resume a graph.

**Decision.** Keep both, with a stated rule: **the checkpoint is authoritative
for resumption, the control-plane tables for reporting.** A reconciler repairs
runs whose process died between the two writes.

**Consequences.** The dashboard gets fast indexed queries and the agent gets
durable resume. The risk — divergence — is named rather than hidden: writes are
made within the node's transaction boundary where possible, the reconciler
detects stale leases, and a test covers "process dies between checkpoint and
row". Pretending a single source of truth exists would not remove the risk, only
the mitigation.

---

## ADR-013
### Frontend types generated from OpenAPI

**Decision.** `openapi-typescript` generates `schema.d.ts` from the FastAPI
document. Hand-written API types are not permitted.

**Consequences.** A backend contract change breaks the frontend build in CI
rather than at runtime in a browser. Combined with `exactOptionalPropertyTypes`
and `noUncheckedIndexedAccess`, the compiler rejects optimistic assumptions
about nullable fields. The cost is a generation step in the workflow and
regeneration discipline after contract changes; the CI type check enforces it.

---

## ADR-014
### JSONB for evolving shapes, promoted columns for anything queried

**Decision.** `plan`, `args`, `result`, `verification` and `payload` are JSONB.
Anything aggregated or filtered — `status`, `tool`, `error_class`,
`verification_status`, `duration_ms`, `attempt` — is a real indexed column.

**Consequences.** Planner improvements do not require a migration, while the
metrics in §15.4 stay single-scan aggregates. The cost is a documented rule that
must be applied on each new field, and some duplication between a JSONB payload
and its promoted columns.

**Alternatives rejected.** Fully normalising the plan: a schema migration per
planner improvement. Everything in JSONB: the tool-success metric becomes a
full table scan and a GIN index we would have to reason about.

---

## ADR-015
### The trace is a product feature; OpenTelemetry is deferred

**Decision.** `trace_events` in Postgres, with a schema and an API, is the
product-level trace. An OTel hook is reserved but not built.

**Consequences.** The operator-facing timeline and the evaluation assertions run
against a queryable, retained, contract-bound record instead of scraped logs.
The cost is a fast-growing table (mitigated by truncation, retention and planned
partitioning) and two observability systems once OTel arrives. They answer
different questions — agent semantics versus request latency — so conflating
them would serve neither.

---

## ADR-016
### Deterministic evaluation only; no LLM-as-judge gate in v1

**Decision.** v1 grades deterministic, checkable properties: final status, tool
sequences, retry counts, verification outcomes, database state. Response-quality
grading by a model is out of scope.

**Consequences.** A failing case names a specific broken assertion, so a
regression is attributable. A nondeterministic grader would make it impossible
to tell whether the agent got worse or the judge did. The cost is that prose
quality is unmeasured; a rubric grader may be added later as a separate,
clearly-labelled, **non-gating** report.

---

## ADR-017
### No authentication in v1, fenced by startup fuses

**Context.** OpsPilot is a local single-operator tool, and approval is a
privileged operation that a real deployment must authorise properly.

**Decision.** No authentication in v1. Instead: `actor_id` and `decided_by`
already exist in the schema; the stack is localhost-bound and CORS-allowlisted;
and `OPSPILOT_ENV=production` **refuses to start** without an auth mode, with a
placeholder database password, or with wildcard CORS.

**Consequences.** Scope stays on the agent, and adding identity later does not
migrate history away. `decided_by` is client-supplied and is therefore
**attribution, not authentication** — documented as such, in code and in docs,
so nobody mistakes it for an audit guarantee. The fuses are tested, because a
paragraph in a README does not stop a system being deployed open.

---

## ADR-018
### A real sender will be a new tool, never a flag on the mock

**Decision.** There will never be a configuration value that turns
`send_email_mock` into a real sender. A real integration arrives as a new tool
`send_email`, with its own contract, approval requirement, verification and
adapter.

**Consequences.** A configuration mistake can cause a *missing capability*,
never an unintended real email. The mock keeps working unchanged for evaluation
and local development, so the eval suite never has to be rewritten or pointed at
a live provider. The tool's name is visible in the registry, the plan, the trace
and the approval payload, so an operator always knows which one they approved.
The cost is two tools coexisting once a real sender exists, and a planner that
must be told which to prefer — a small price for making the dangerous case
unreachable by misconfiguration.

---

## ADR-019
### Postgres is the only infrastructure dependency

**Decision.** No Redis, no broker, no search engine. Postgres provides
persistence, checkpoints, the trace store, idempotency constraints and
(later, if needed) the run queue.

**Consequences.** `docker compose up` is two services; a contributor is running
in minutes; there is one backup story and one set of transactional guarantees —
which matters, because the safety mechanisms *are* database constraints (a
partial unique index and a unique idempotency key). The cost is that Postgres
must also do queueing work if ADR-004 is revisited; `SELECT … FOR UPDATE SKIP
LOCKED` is sufficient at this scale.

---

## ADR-020
### Retry safety comes from keyed effects, not from hope

**Context.** A timeout is ambiguous: the effect may or may not have happened.

**Decision.** Every mutating step derives an attempt-invariant
`idempotency_key` from `(run_id, step_id, args_hash)`. Adapters enforce
uniqueness at the database level; a replay returns the original result and is
recorded as `duplicate_suppressed`.

**Consequences.** Retries are safe because effects are keyed, not because we
assume the first attempt did nothing. Verification can then assert "exactly one
outbox row", which is what actually proves the mechanism works. The cost is that
every mutating adapter must implement the constraint, and the key derivation
must stay attempt-invariant — which is why volatile keys are excluded from the
argument hash (ADR-010).

---

## ADR-021
### Verification is a graph node, not a tool wrapper

**Decision.** A separate `verify` node runs contract-driven verification after
`execute_tool`, reading through a different port method than the one that wrote.

**Consequences.** A verification failure is a first-class, observable,
recoverable event with its own trace record, rather than an exception swallowed
inside a tool. It also enforces independence: the verifier compares against the
requested intent, not the tool's echoed response, so a tool that lies about
success is caught. The cost is an extra node transition per mutating step and a
verification latency budget.

---

## ADR-022
### `rejected` is a terminal status distinct from `failed`

**Decision.** A human declining an action terminates the run as `rejected`, with
its own `status_reason`, not as `failed`.

**Consequences.** The safety mechanism working correctly is not reported as a
system failure — to the operator, or to the metrics. `task_success_rate`
excludes intentionally-rejected runs from its denominator (§15.4), so a
well-functioning gate cannot depress the success metric and create pressure to
weaken it. The cost is one more terminal status for every consumer to handle,
which the status machine and the API contract both enumerate explicitly.

---

## ADR-023
### Run ownership is a fenced lease; recovery is a checkpoint-driven state machine

**Context.** ADR-004 and ADR-012 promise that a crashed process leaves nothing
permanently `running` and that the reconciler repairs runs whose process died
between the checkpoint write and the row write. §12.3 gave the run row a
`lease_expires_at` but no owner, and the architecture did not say when a
stale lease is an orphan versus a human's pause, nor what "re-enter or mark
orphaned" decides on.

**Decision.** Three things, implemented in DB-007.

1. *Fenced ownership.* `agent_runs` gains `lease_owner`; a lease is the pair
   `(owner, expiry)` or nothing (a `CHECK` enforces it). Acquire, heartbeat,
   release and every status transition are single conditional `UPDATE`s whose
   `WHERE` names the expected status and owner, so ownership is decided by
   Postgres's row lock, never by a read-then-write in Python. A heartbeat is
   refused for a non-owner *and* for an owner whose lease has expired — an
   expired lease is never revived, because someone else may already hold it.
   The liveness predicate (`expiry > now`) and the claim predicate
   (`expiry <= now`) are exact complements. All lease time comes from the
   injected `Clock`, passed to SQL as a parameter.
2. *Candidates.* Only `queued`/`running` runs with an expired or absent lease.
   `awaiting_approval` has no owner by design (§6.3) and is never reconciled;
   terminal runs never are. This is the rule that keeps an intentional pause
   from being mistaken for a crash.
3. *The checkpoint decides.* After an atomic claim, the reconciler inspects the
   LangGraph checkpoint: none → `failed(orphaned)`; paused at an interrupt →
   `awaiting_approval` (the "died between checkpoint and row write" case);
   finished → the terminal status recorded in the state; mid-execution →
   resumed under the reconciler's own lease and heartbeat, and settled the
   same way once the graph next stops. Every settling write (status + trace
   event) is one transaction; a failure rolls back as a unit and the run
   becomes a candidate again when the reconciler's lease expires. One
   product-trace kind, `run_recovered`, records the takeover; leases and
   heartbeats are structured logs.

Graph invocations use `durability="sync"` so a checkpoint is committed before
the next step starts; the default `"async"` mode would make the checkpoint
only approximately authoritative.

**Consequences.** N reconcilers over the same orphans hand each run to exactly
one of them, and a second pass is a no-op. Recovery re-executes the node that
was in flight (ADR-001's re-execution semantics), which is safe only because
ADR-010 and ADR-020 already make gated and mutating steps idempotent — recovery
adds no bypass and depends on none. The costs: one more column and one more
trace kind, a migration that alters a `CHECK` constraint, one extra round trip
per graph step, and a second Postgres driver (psycopg, required by the saver)
next to asyncpg. A resume that raises marks the run `failed(recovery_failed)`
rather than retrying forever; the operator retries as a new run (§10.6).

**Alternatives rejected.** `SELECT … FOR UPDATE` around a read-then-write:
correct, but two statements and a held lock where one conditional `UPDATE`
suffices. A lease without an owner (expiry only): cannot fence a late
heartbeat from a worker that already lost the run. Treating every stale lease
as a crash regardless of status: would fail runs a human is about to approve.
Skipping the reconciler when a run has a checkpoint and simply re-entering it:
would re-raise interrupts on paused runs and could not settle a finished one.

---

## ADR-024
### One dispatcher owns the key, the port and the record; a rejection is an attempt

**Context.** §8.5 names `ToolRegistry.dispatch` as the single choke point but
leaves five things to the implementation: who derives the idempotency key,
how a presented `ApprovalToken` is tied to the *stored* decision, what an
implementation may reach, how a refusal before the port is recorded in a
`tool_calls.status` enum that has no `rejected` value, and how two concurrent
attempts of one keyed effect are told apart in the record.

**Decision.** Implemented in TOOL-002 (`app/tools/registry.py`).

1. *The dispatcher derives the key.* `idempotency_key = f(run_id, step_id,
   args_hash)` (ADR-020) is computed in one place,
   `security.idempotency_key_for`, called only by the dispatcher. A plan's
   arguments may not carry `idempotency_key` (rejected as
   `INPUT_VALIDATION`) or `approval_token` (rejected as `POLICY_VIOLATION`):
   authorisation is presented to the dispatcher by the node, never planned.
2. *Two checks, one path.* For a gated tool the token must authorise the
   call (`ApprovalToken.authorises`, barrier 2) **and** the `approvals` row
   it names must be `approved` for the same run, step, tool, `args_hash` and
   risk. The token carries no tool and no status; the row does. A token for
   a pending, rejected, expired, cancelled or superseded row authorises
   nothing. A token presented for an ungated tool is a `POLICY_VIOLATION`.
3. *One port per tool.* An implementation receives a `ToolContext` holding
   exactly the port its contract declares, and nothing else. `ToolContext`
   is constructed only by the dispatcher, and `tests/test_structure.py` fails
   on any module that calls a mutating port method, references a
   mutation-capable `Adapters` field other than as the receiver of a read
   method, imports `app.integrations.mock`, constructs a `ToolContext`,
   writes a `tool_calls` row or derives a key elsewhere. The scan is proven
   non-permissive by canary snippets.
4. *A rejection is an attempt.* A refusal before the port is reached —
   malformed input, missing or invalid grant, unbound implementation —
   consumes its `(execution_step, attempt)` slot and is recorded as
   `status=failed` with its `error_class` and `adapter=NULL`, plus the
   `tool_started`/`tool_failed` pair and, for policy violations, a
   `policy_violation` event at `error` severity. `error_class` and the null
   adapter are what distinguish "refused" from "the integration failed";
   the in-memory `DispatchOutcome` says `rejected` outright. An unknown tool
   is refused before any I/O and records nothing: it is not an attempt of
   anything.
5. *Replay is classified under a lock.* A mutating attempt runs inside a
   transaction-scoped advisory lock on its idempotency key (the mechanism
   DB-002 uses per run). A concurrent duplicate waits for the winner, sees
   its recorded attempt, and is recorded `duplicate_suppressed`; a second
   dispatch of the *same* attempt number is refused without executing. The
   lock is bookkeeping — the adapter's unique constraint on the key remains
   the effect-level protection — and it is only claimed when the key
   actually reached the effect (the input model declares the field), so
   `save_draft` retries honestly record `succeeded`.

**Consequences.** The three barriers of §9.5 stay independent and gain a
fourth check (the stored row) without a second execution path; every
attempt is visible in `tool_calls` and the trace whether or not it ran;
adding a tool means binding an implementation to its contract, and adding a
mutation means the structural scan must be updated deliberately. The costs:
holding the key lock across execution pins one pooled connection per waiting
dispatcher plus one for the executing adapter (fine for the realistic
contention of a double resume or a reconciler racing a worker — nothing
fans out); the `tool_calls` enum is not extended, so dashboards must read
`error_class` to separate refusals from failures; and the §14.5 redaction
rules live in `app/observability/redaction.py` ahead of OBS-001's recorder,
which must adopt them rather than re-implement them.

**Alternatives rejected.** Letting the node pass an idempotency key: a
second scheme waiting to diverge from ADR-020. Trusting the token alone: it
cannot know the row was superseded after minting. Adding `rejected` to the
`tool_calls` enum: a migration and a contract change for a distinction
`error_class` already makes. Classifying replay from the adapter's return
value: the port carriers have no such field, and an adapter without keyed
replay would be mis-recorded. A non-blocking `pg_try_advisory_xact_lock`
that fails the loser as `TRANSIENT`: no held connection, but an honest
retry recorded as a failure.

---

## Open questions

Recorded rather than guessed. None blocks the current backlog.

| # | Question | Bears on | Current default |
|---|---|---|---|
| Q1 | Should a rejection be able to carry guidance that triggers a replan ("wrong segment, try enterprise") instead of terminating? | HITL, ADR-022 | Reject is terminal for the step. A `reject_with_feedback` variant would need a replan budget rule and a new approval, so it needs its own ADR. |
| Q2 | Should `research_company` results be cached across runs, and for how long? | TOOL-001, determinism | No cache. A cache would break per-case determinism unless it is part of the fixture reset. |
| Q3 | Does the operator need a way to edit a draft before approving it? | HITL, FE-004 | No. Editing would change the content hash and therefore require a re-save and a new approval — clean, but it is a new flow, not a UI tweak. |
| Q4 | Should the LLM planner be allowed to propose a tool sequence the rule planner cannot express, and how is that evaluated? | ADR-002, EVAL | Allowed at runtime; evaluated only in the non-gating LLM suite. |
| Q5 | What is the retention policy for `mock_crm.email_outbox`? | DB-004, OBS-004 | Unbounded for now; it is fixture-scale data. |
| Q6 | Should approvals be assignable to a specific operator (queue ownership)? | HITL, ADR-017 | No. Needs identity first. |
