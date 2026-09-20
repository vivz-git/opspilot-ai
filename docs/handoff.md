# Handoff

**Read this first.** It tells you how to resume work on OpsPilot AI without
repeating anything and without re-deciding anything that is already decided.

---

## 1. Orientation, in order

Do these seven things before writing any code. They take about ten minutes and
they are the difference between continuing and restarting.

```bash
cd /path/to/opspilot-ai
cat docs/progress.md          # 1. what is done and verified
cat docs/tasks.md             # 2. what is next, with acceptance criteria
cat docs/decisions.md         # 3. what is already decided, and why
git status                    # 4. is the tree clean?
git log --oneline -15         # 5. what actually landed
cd backend && uv run pytest   # 6. still green? expect 1799 passed, 1 skipped (the opt-in live Groq smoke test) with DATABASE_URL at a reachable Postgres
grep -rn "TODO\|FIXME" backend/app 2>/dev/null   # 7. any unfinished edges
```

Then open `docs/architecture.md` at the section for the task you are picking
up. Do not work from this file's summary — the architecture is the
specification, and it is detailed enough to implement against directly.

**Never redo completed work because conversational context changed.** The
repository is the source of truth. If a document and the code disagree, the
code is what runs and the document is a bug — fix the document, do not
"restore" the code to match it.

## 2. Where the project stands

**The system runs end to end.** The contract spine, the foundation, the whole
persistence layer, the nine tools behind `ToolRegistry.dispatch`, the
LangGraph graph with all nine nodes, both planners, recovery, cancellation,
the HITL approval path, the verifiers, the HTTP API (runs, trace, SSE,
approvals, evaluations, tools, health), the evaluation runner and the operator
console are implemented and tested: 1799 backend tests at 93% coverage, 123
frontend unit tests, 14 Playwright specs, 7/7 evaluation cases.

The canonical request runs against a real database, pauses for a human,
resumes on the decision, verifies its simulated effect and terminates with a
complete trace — driven from the console in a browser, live over SSE. That was
demonstrated, not assumed; `docs/progress.md` §LAUNCH-001 records exactly what
was checked.

**Nothing is hosted.** The deployment configuration exists
(`docs/deployment.md`, `railway.json`, `frontend/vercel.json`,
`backend/scripts/start.sh`), but no platform CLI was authenticated in the
session that wrote it, so the one-time interactive logins and the Cloudflare
Access policy are still a human's to perform.

**Do not expose this without the access layer.** OpsPilot authenticates
nobody (ADR-017). The hosted shape is `OPSPILOT_AUTH_MODE=proxy` behind an
identity-aware proxy (ADR-026), and the app's own header check is a
fail-closed backstop, not authentication.

## 3. Start here

Pick from these, in the order a portfolio reviewer would notice them:

1. **Finish the deployment.** `docs/deployment.md` §4 and §5: `railway login`,
   `vercel login`, the Cloudflare Access applications, then the three
   verification `curl`s in §5. The third one returning `200` means the access
   layer is not on — stop there.
2. **Two denormalised-projection bugs** (both pre-existing, both visible in
   the console, neither a safety issue): `agent_runs.step_count` stays 0 on a
   completed run, and `execution_steps.status` stays `pending` for steps that
   succeeded. The run resource's `verification_status` and the trace both
   disagree with those columns, so the projection is what is wrong.
3. **The reconciler's finalize path** emits `run_recovered` rather than the
   run's terminal trace event — the same defect that
   `ApprovalService.decide_approval` had until LAUNCH-001 fixed it there.
   `persistence/models.py::TERMINAL_TRACE_EVENTS` is the shared map to use.
4. **A run-submission form in the console.** The demo currently starts runs
   over `POST /runs`; an operator console that cannot ask for anything is an
   obvious gap.
5. **`trace_id` is never echoed on error responses**, contradicting §13.1.
   Needs per-request correlation (middleware) that does not exist yet.

Whatever you pick: `docs/architecture.md` is the specification, and
`docs/decisions.md` records what is already decided and what it cost.

Historical notes from the first implementation sessions (still accurate;
kept because they describe shapes later tasks must match):

An implementation is `async def name(args: <InputModel>, ctx: ToolContext) ->
<OutputModel>`; it reaches its port only as `ctx.port` (narrow it with
`isinstance(ctx.port, MailPort)`), takes the token and the key from the
validated `args` for gated tools, and raises the taxonomy's exceptions. It
never validates input, checks a grant, derives a key, records a row or emits
a trace — the dispatcher already did or will. `tests/test_tool_dispatch.py`
carries scripted implementations for `search_leads`, `get_lead`,
`score_lead` and `send_email_mock` that show the shape. When `AGENT-005`
lands, the node calls **only** `ToolRegistry.dispatch(...)`; the structural
tests will reject anything else.

When you reach **AGENT-002** and **API-007**, DB-007 already provides what
they need — do not rebuild it: compile the graph with the saver from
`app.persistence.checkpointing.open_checkpointer` and invoke with
`durability=DURABILITY`; wrap each graph run in `app.execution.leases
.hold_lease`; run `app.execution.recovery.Reconciler(driver=
LangGraphRunDriver(graph)).reconcile_once()` at startup. The recovery state
machine is in `docs/architecture.md` §2.4 and ADR-023.

Then follow the critical path in `docs/tasks.md`:

```
FOUND-001 ▸ DB-001..004 ▸ TOOL-001 ▸ TOOL-002/003
  ▸ AGENT-002..008 ▸ HITL-001..005 ▸ VERIFY-001..003
  ▸ API-001..007 ▸ FE-001..008 ▸ EVAL-001..005
```

**Do not reorder the frontend earlier.** HITL and VERIFICATION must land before
the API and the UI. Building the dashboard first produces a demo that looks
finished while the properties this product claims are unimplemented — which is
the specific failure this plan exists to avoid.

## 4. Which model to use

`docs/tasks.md` carries a class per task. The short version:

- **OPUS** for the 15 listed tasks, plus any architectural refactor, any change
  to the approval or verification path, any change to a metric definition, and
  any debugging session where the cause is genuinely unclear.
- **SONNET** for everything else — all CRUD, migrations, tool implementations
  against the fixed contracts, route handlers, the whole frontend, styling,
  routine tests, documentation.

Escalate a SONNET task to OPUS only if: the architecture does not answer a
question the task needs, the obvious implementation would violate a documented
invariant, a test fails for a reason nobody understands, or the task turns out
to require changing a contract, a status machine or a metric — which is an ADR,
not an implementation detail.

Do not escalate because a task is large.

## 5. Rules that are not negotiable

These are the properties the product claims. Breaking one is a defect even if
every test still passes.

1. **No mutating or outbound tool executes without a grant matching the exact
   arguments.** Three barriers (§9.5) must all remain in place. Never add a
   bypass — no `skip_approval` flag, no query parameter, no test-only switch.
   Tests script the *human*, never disable the *gate*. Every tool call goes
   through `ToolRegistry.dispatch` (§8.5, ADR-024): nothing outside
   `app/integrations/` and `app/tools/impl/` may call a mutating port method,
   and the dispatcher alone derives idempotency keys, constructs a
   `ToolContext` and writes `tool_calls` rows — `tests/test_structure.py`
   enforces each of these and its scan is proven non-permissive by canaries.
2. **Never assume a tool succeeded because it returned.** Mutating effects are
   verified through an independent read path, compared against what was
   requested, not what the tool echoed.
3. **Every loop has a numeric budget** and `decide` checks budgets first.
4. **`send_email_mock` can never send mail.** No network-capable import may
   become reachable from `app/integrations/mock/` — a structural test enforces
   this, and it stops being skipped the moment TOOL-001 lands. A real sender
   would be a *new tool* (ADR-018), never a flag on the mock.
5. **Only `app/config.py` reads the environment.** Enforced by a test.
6. **No secrets in the repository.** It is public. `.env` is ignored,
   `.env.example` is placeholders, CI runs gitleaks. Fixture data uses RFC 2606
   reserved domains so no real person is in the dataset.
7. **`rejected` is not `failed`.** A human declining is the mechanism working.
8. **Nothing executes model output.** No `eval`, `exec`, shell, dynamic import,
   or expression language over planner or tool output.

## 6. Working rhythm

After each meaningful unit of work:

```bash
git status && git diff                     # 1. inspect what you changed
make secrets-scan                          # 2. no credentials (or grep, if no Docker)
cd backend && pytest && ruff check . && mypy app   # 3. green
# 4. confirm docs and code still agree — if you changed behaviour,
#    update docs/architecture.md or write an ADR in docs/decisions.md
git commit                                 # 5. one focused conventional commit
```

Then update `docs/progress.md` and mark the task in `docs/tasks.md`. A task is
done only when its **acceptance criteria** are met — not when the code exists.

Commit style, as used so far: `docs:`, `chore:`, `feat(agent):`, `feat(api):`,
`test:`, `fix:`. One deliverable per commit; the body says what was decided and
why, not just what changed.

Work on the branch `claude/great-euler-ql1wxj`, push with
`git push -u origin claude/great-euler-ql1wxj`. Do not open a pull request
unless asked.

## 7. Before a context refresh

If the context window is filling up, do not stop early and do not summarise
into the conversation. Save state into the repository instead:

1. commit whatever is working (a WIP commit on the branch is fine, and better
   than losing it);
2. update `docs/progress.md` with what you just finished and what you were
   mid-way through;
3. add any new unresolved question to the open-questions table in
   `docs/decisions.md`;
4. note the exact next step in §3 of this file.

Then continue from the repository state. Never re-derive what a document
already records.

## 8. Manual actions a human must take

Nothing here blocks implementation, and nothing was fabricated to work around
it.

| Action | Needed for | Blocking? |
|---|---|---|
| Obtain a `GROQ_API_KEY` from <https://console.groq.com/keys> and put it in `.env` (`GROQ_MODEL=openai/gpt-oss-120b`, ADR-025) | `OPSPILOT_PLANNER=llm`; LLM-written plans and outreach copy | **No.** With no key, `auto` uses the deterministic rule planner and the template content generator. All nine tools, approvals, verification, the evaluation suite and the dashboard work unchanged. |
| Choose and add a license file | Reuse and contribution clarity on a public repository | No, but decide early |
| Provide a deployment target and credentials | Anything beyond local Docker | No. Local `docker compose` is the supported environment, and `OPSPILOT_ENV=production` deliberately refuses to start without authentication (ADR-017). |
| Grant the Claude GitHub App access to `vivz-git/opspilot-ai` | Pushing this branch to the remote | **Yes, for pushing only.** The architecture session's ten commits exist locally on `claude/great-euler-ql1wxj`; `git push` returned 403 because the app is not installed for the repository. An org admin can install it at <https://github.com/apps/claude/installations/select_target>, or reconnect GitHub from claude.ai settings. Re-run `git push -u origin claude/great-euler-ql1wxj` afterwards — nothing needs rebuilding. |

If you hit a new blocker of this kind: add the correct placeholder to
`.env.example`, wire it through `app/config.py`, document it in this table, and
keep going. **Never invent a credential, and never stub a service in a way that
pretends to work.**

## 9. Open questions

Six are recorded with their current defaults at the end of
`docs/decisions.md`. Each has a working default, so none blocks you. If one
becomes relevant, decide it deliberately and write an ADR — do not resolve it
implicitly in an implementation.

The two most likely to come up:

- **Q1** — should rejection be able to carry guidance that triggers a replan
  instead of terminating? Current default: rejection is terminal for the step.
- **Q3** — should the operator be able to edit a draft before approving it?
  Current default: no. Editing changes the content hash, so it requires a
  re-save and a fresh approval — a new flow, not a UI tweak.

## 10. Known traps

Things the architecture handles that are easy to get wrong on the way through.

| Trap | What to do |
|---|---|
| LangGraph re-executes the interrupted node on resume | `request_approval` must stay idempotent: upsert the approval row, emit `approval_requested` only on a genuine insert, and rely on `merge_approval_state` never regressing a decided approval (§9.7) |
| Crash recovery re-executes the node that was in flight | Same rule, wider: every node must be safe to run twice. The reconciler (DB-007) resumes a mid-execution checkpoint from its last committed step; mutating effects are made idempotent by ADR-020's keyed constraint, not by recovery. Never add a "skip if recovered" flag to a node. |
| Writing `awaiting_approval` after an interrupt | The graph task ends on interrupt; the executor must write the status *after* the checkpoint is durable (`durability="sync"`) and then release the lease. If the process dies in between, the reconciler repairs the row from the checkpoint — that is expected, not a bug to paper over. |
| A worker that keeps going after a refused heartbeat | `LeaseHeartbeat.lost` / `on_lost` mean *stop driving this run now*. Never retry a refused heartbeat; an expired lease is never revived because someone else may own the run. |
| Alembic proposing to drop the `checkpoint*` tables | They are the saver's, in the `langgraph` schema; `alembic/env.py`'s `include_name` excludes that schema. Do not add them to a migration. |
| Retrying a planning fault | `INPUT_VALIDATION` and `REFERENCE_RESOLUTION` route to **replan**, never retry. The same call with the same broken argument cannot succeed. |
| Retrying an unverified non-idempotent mutation | Forbidden by invariant P5. Report the effect as *unconfirmed* — not as done, and not as failed. |
| A `STALE_WRITE` on an approved update | Replan, which produces new arguments, a new hash, and therefore a **fresh approval**. Do not re-apply the old patch. |
| Backoff in tests | Use the injected `Clock`. Never `time.sleep` in a test; the suite must stay fast and deterministic. |
| Human wait time in duration metrics | `agent_duration_ms` excludes approval wait, or the agent looks slower the more carefully an operator reads. |
| Conflating `case_pass_rate` with `task_success_rate` | A case that expects rejection and gets it is a **pass**. Keep the metrics separate (§15.4). |
| Verification reading the wrong source | Read `mock_crm` through a port, and compare against the **requested** intent — never the tool's echoed response, never the control-plane tables. |
| Fan-out without a cap | `fanout.max_items` is mandatory, and expanded children count against `MAX_STEPS`. |
| Adding a tool | Add the contract to the registry first. `tests/test_tool_policy.py` will tell you immediately if it violates a policy invariant; `ToolRegistry` refuses to construct with a violating contract, too. Then bind its implementation; an unbound contract dispatches as `ToolNotBoundError`, never as "just run it". |
| Calling a port from a node, a service or an endpoint | Don't. Dispatch the tool. `test_no_direct_mutating_port_calls_outside_tool_implementations` fails on `adapters.mail.send(...)`, on `mail = adapters.mail`, on passing a port along, and on `ctx.port.send(...)` from anywhere but `app/tools/impl/`. Verifiers use the read methods (`get`, `get_outbox`) and nothing else. |
| Adding a mutating port method | Add it to `MUTATING_PORT_METHODS` in `tests/test_structure.py` in the same change; `test_the_mutating_port_surface_is_declared` fails until you do. |
| Passing `idempotency_key` or `approval_token` in a plan's arguments | The dispatcher rejects both (`INPUT_VALIDATION` / `POLICY_VIOLATION`). The key is derived from `(run_id, step_id, args_hash)`; the token is passed to `dispatch(approval_token=...)` by the node, from a stored approved decision (HITL-002). |
| Reading `tool_calls.status` to tell a refusal from a failure | Both are `failed`. Read `error_class` (`input_validation`, `policy_violation`, `internal` with `adapter IS NULL` = refused before the port) — ADR-024. |
| Holding the per-key dispatch lock | It pins one pooled connection per waiting dispatcher plus one for the executing adapter. Never fan out concurrent dispatches of the same keyed effect from application code; the realistic contention is two. |

## 11. Definition of done for the whole system

OpsPilot v1 is complete when, on a clean clone with **no API key**:

1. `make up && make migrate && make seed` brings the stack up;
2. submitting the canonical request produces a run that plans, executes,
   pauses for approval, resumes on approval, verifies the send, and completes;
3. rejecting instead produces `status=rejected`, zero outbox rows, and a
   response naming what was not done;
4. the dashboard shows the plan, the timeline with retries, the approval card
   with the full draft, and the verification badges;
5. `make eval` passes all seven cases plus the seven global invariants, and
   prints the metric summary;
6. `make check` is green: ruff, strict mypy, the full test suite;
7. `make secrets-scan` finds nothing.

Anything less than all seven is not done, however good the demo looks.
