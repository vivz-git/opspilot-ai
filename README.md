# OpsPilot AI

A production-style **AI operations agent**. It takes a natural-language
business request, turns it into an explicit multi-step plan, executes that plan
against a CRM-shaped system of record, pauses for human approval before
anything leaves the system, verifies that its important effects actually
happened, and records everything it did as an inspectable trace.

It is deliberately **not** a chatbot. A request produces a *run*: a planned,
budgeted, terminating unit of work with a status, a trace and a verdict.

> **Project status — architecture complete, implementation starting.**
> The design is finished and specified in [`docs/architecture.md`](docs/architecture.md).
> What is implemented today is the contract spine: the typed agent state, the
> tool contract registry, the error/recovery taxonomy, the approval-binding
> primitives and configuration, with 214 passing tests. The graph, tools,
> persistence, API and dashboard are the next phase —
> see [`docs/tasks.md`](docs/tasks.md). No document here claims more than exists;
> [`docs/architecture.md` §4.1.1](docs/architecture.md#411-package-layout)
> marks exactly which modules are built.

---

## The worked example

> *"Find the top 3 fintech leads in London, research their companies, score
> them, draft outreach to the best one and email it to them."*

```
understand ─► plan ─► decide ─► search_leads          ✓ verified (invariant)
                        ├────► research_company ×3    ✓
                        ├────► score_lead ×3          ✓
                        ├────► draft_outreach         ✓
                        ├────► save_draft             ✓ verified (read-back)
                        │
                        ├────► ⏸  APPROVAL REQUIRED — run pauses here,
                        │          state checkpointed, process may restart
                        │      ▼ operator approves in the dashboard
                        └────► send_email_mock        ✓ verified (outbox read-back)
                                       │
                                       ▼  complete → final response + full trace
```

## What makes it production-style

| Property | How it is actually guaranteed |
|---|---|
| **Nothing sends without a human** | Approval is bound to a canonical hash of the exact arguments, and three independent barriers enforce it — the router, a re-assertion inside the executing node, and an `ApprovalToken` that mutating adapters require and that only a stored approved decision can mint. No single bug is sufficient. |
| **Tool success is never assumed** | A tool returning without raising is a *claim*. Mutating effects are confirmed through an independent read path that compares against what was **requested**, not what the tool echoed back. A tool that lies about persisting is caught. |
| **Retries cannot double-apply** | Effects are keyed by an attempt-invariant idempotency key with a `UNIQUE` database constraint, so a retry after an ambiguous timeout returns the original result instead of sending twice. |
| **Execution is bounded** | Per-step retries, plan revisions, total steps and an absolute wall-clock deadline are all numeric budgets, checked before every decision. Infinite loops are structurally impossible. |
| **It runs without an API key** | A deterministic rule planner backs the LLM planner, so the whole system — all nine tools, approvals, verification, evaluation, dashboard — runs end to end with no credential, and CI needs no secret. |
| **Behaviour is measured, not asserted** | Deterministic evaluation cases pin the safety and reliability claims, including a rejected approval, a retryable failure, a prompt-injection attempt and a tool that falsely reports success. |
| **Every run is explainable** | An append-only trace records each node entry, tool attempt, retry decision, approval event and verification check with durations — emitted structurally, so an untraced node cannot be added. |

## Quickstart

```bash
git clone <this repo> && cd opspilot-ai
cp .env.example .env          # or: make env
make up                       # Postgres + API at http://localhost:8000
make migrate                  # apply schema        (available after DB-001)
make seed                     # load the mock CRM   (available after TOOL-001)
make up-full                  # adds the dashboard  (available after FE-001)
```

**No `ANTHROPIC_API_KEY` is required.** Leave it blank and the agent uses the
deterministic rule planner (`OPSPILOT_PLANNER=auto`). Set it to get
LLM-generated plans and outreach copy. Nothing else changes.

What works today:

```bash
cd backend
pip install -e ".[dev]"
pytest                        # 214 passed, 1 skipped — contracts, state, recovery, security, structure
```

## Documentation

| Document | What it is for |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | The specification. System, agent, graph, state, tool contracts, approval, retry, verification, persistence, API, observability, evaluation, security, testing, integration boundary. |
| [`docs/decisions.md`](docs/decisions.md) | 22 ADRs — each decision, the alternative rejected, and what it costs. Plus the open questions. |
| [`docs/tasks.md`](docs/tasks.md) | The prioritized backlog: 70 tasks with dependencies, acceptance criteria and model allocation. |
| [`docs/progress.md`](docs/progress.md) | What is done, verified and outstanding. |
| [`docs/handoff.md`](docs/handoff.md) | How the next session continues. **Start here.** |

## The nine tools

`send_email_mock` **never sends real email.** It records a row in a mock
outbox; no network-capable client exists anywhere in the mock package's
dependency graph, and a test enforces that.

| Tool | Effect | Approval | Verification |
|---|---|---|---|
| `search_leads` | read | — | invariant |
| `get_lead` | read | — | — |
| `research_company` | read | — | invariant |
| `score_lead` | read (deterministic rules, not an LLM) | — | invariant |
| `draft_outreach` | read (generative) | — | invariant |
| `save_draft` | internal write | — | read-back |
| `send_email_mock` | outbound (simulated) | **required** | read-back |
| `get_customer` | read | — | — |
| `update_customer` | customer write | **required** | read-back |

These flags are not conventions: a test iterates the registry and fails CI if a
tool that mutates business data is not gated, or if a mutating tool is not
independently verified.

## Stack

**Backend** Python 3.12 · FastAPI · LangGraph (Postgres checkpointer) ·
Pydantic v2 · SQLAlchemy 2 async · Anthropic API
**Frontend** Next.js 15 · TypeScript · Tailwind · shadcn/ui
**Data** PostgreSQL 16 — three schemas: control plane, LangGraph runtime,
simulated system of record
**Infra** Docker · docker-compose · GitHub Actions (lint, strict types, tests,
gitleaks)

## Security

This is a public repository and contains **no credentials**. `.env` is
gitignored, `.env.example` holds placeholders only, CI runs gitleaks over full
history, and `OPSPILOT_ENV=production` refuses to start without authentication
configured, with a placeholder database password, or with wildcard CORS.

OpsPilot has **no authentication in v1** — a deliberate, fenced scope decision
([ADR-017](docs/decisions.md#adr-017)). It is a localhost single-operator tool.
Read that ADR before exposing it anywhere.

## Development

```bash
make help            # all targets
make lint            # ruff + ruff format --check + mypy strict
make test            # backend suite
make test-unit       # no database required
make eval            # deterministic evaluation suite (after EVAL-005)
make secrets-scan    # gitleaks over the working tree
make check           # everything CI enforces
```
