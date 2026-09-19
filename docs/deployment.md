# Hosted operator demo

How to put OpsPilot somewhere a browser can reach it **without** breaking the
security model it was designed under.

Read [ADR-017](decisions.md#adr-017) and [ADR-026](decisions.md#adr-026)
before you deploy anything. The short version:

> OpsPilot has **no application authentication**. Approval is a privileged
> operation and nothing in the application authenticates the person
> performing it. A deployment that is reachable without an access layer in
> front of it is an unauthenticated control plane over a system that sends
> things. Do not build one.

This document describes the only deployment shape the project supports: a
**hosted operator demo** — single operator, behind an identity-aware proxy,
with the mock integrations that can never send real mail (ADR-018).

---

## 1. Architecture

```
                    ┌──────────────────────────────────────────┐
   operator ───────►│  identity-aware proxy (Cloudflare Access) │
   (browser)        │  authenticates; allows one operator       │
                    └───────────────┬──────────────┬───────────┘
                                    │              │
                    ┌───────────────▼──┐   ┌───────▼──────────────┐
                    │  console          │   │  API                 │
                    │  Next.js 15       │   │  FastAPI + uvicorn   │
                    │  Vercel           │   │  Railway (Docker)    │
                    │  ops.example.com  │   │  api.example.com     │
                    └───────────────────┘   └───────┬──────────────┘
                                                    │
                                          ┌─────────▼──────────┐
                                          │  PostgreSQL 16     │
                                          │  Railway, private  │
                                          │  never public      │
                                          └────────────────────┘
```

Two origins, both behind the same access layer. The console calls the API
directly — there is no rewrite proxy, so the origin split stays explicit
(`frontend/next.config.mjs`) — which means:

* the API must allowlist the console's origin in `CORS_ALLOW_ORIGINS`;
* the console sends every request with credentials (`credentials: "include"`,
  and `withCredentials` on the `EventSource`) so the proxy's session cookie
  rides along on the cross-origin call. Without it the proxy bounces the XHR
  to a login page the browser cannot follow.

**One replica.** Runs are driven in-process (ADR-004). Leases make a second
replica *safe* (ADR-023), not useful; `railway.json` pins `numReplicas: 1`.

---

## 2. What the platform needs from the image

`backend/Dockerfile` builds the API. `backend/scripts/start.sh` is its
entrypoint and handles the three things a platform needs:

| Variable | Default | What it does |
|---|---|---|
| `PORT` | `8000` | The port the platform assigned. |
| `OPSPILOT_MIGRATE_ON_START` | `false` | Run `alembic upgrade head` before serving. |
| `OPSPILOT_SEED_ON_START` | `false` | Load the mock CRM **additively** (never truncates). |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Peers uvicorn accepts `X-Forwarded-*` from. |

Migrations are opt-in on purpose. `/readyz` already refuses traffic when the
applied revision is not the code's head, so a container that boots without
migrating fails its health check loudly instead of serving a stale schema
quietly. Turn it on where there is no separate release phase (Railway); leave
it off where migrations run as their own deploy step.

Health checks:

* `GET /healthz` — liveness. The process answers; nothing else is claimed.
* `GET /readyz` — readiness. Database reachable **and** at the migration
  head, or `503`. This is the platform's health check path.

---

## 3. Environment

Placeholders only. Every real value is set in the platform's secret store,
never in the repository, never in a build log, never in this file.

### API (Railway)

```
OPSPILOT_ENV=production
OPSPILOT_AUTH_MODE=proxy
OPSPILOT_PROXY_IDENTITY_HEADER=Cf-Access-Authenticated-User-Email
DATABASE_URL=postgresql+asyncpg://<user>:<password>@<host>:<port>/<db>
CORS_ALLOW_ORIGINS=https://<console-hostname>
OPSPILOT_PLANNER=rules
OPSPILOT_INTEGRATIONS=mock
LOG_LEVEL=INFO
OPSPILOT_MIGRATE_ON_START=true
OPSPILOT_SEED_ON_START=true
FORWARDED_ALLOW_IPS=<platform proxy range>
```

`OPSPILOT_PLANNER=rules` is the deployment default: the demo needs no API
key and stays byte-reproducible. `GROQ_API_KEY` is optional and only changes
how plans and copy are written (ADR-002); if you set it, set it in the
platform's secret store and nowhere else.

### Console (Vercel)

```
NEXT_PUBLIC_API_BASE_URL=https://<api-hostname>
```

This is baked into the client bundle at **build** time. Changing the API
hostname needs a rebuild, not a restart. It must be `https://` or the browser
blocks it as mixed content.

### The fuses this trips if you get it wrong

`OPSPILOT_ENV=production` refuses to start when:

* `OPSPILOT_AUTH_MODE` is unset, or is anything other than `proxy`;
* the password in `DATABASE_URL` is a known placeholder;
* `CORS_ALLOW_ORIGINS` is empty, contains `*`, or names a non-`https://` origin;
* `LOG_LEVEL=DEBUG`;
* `OPSPILOT_TOOL_FAILURE_RATE > 0`;
* `OPSPILOT_INTEGRATIONS=real` (no real adapter exists — ADR-018);
* `DATABASE_URL` does not use an async driver.

These are fuses, not warnings. `backend/tests/test_config.py` pins every one.

---

## 4. The access layer

This is the security boundary. It is not optional.

1. Put both hostnames behind Cloudflare (proxied DNS records).
2. Create a Cloudflare Access application covering the console hostname, and
   a second covering the API hostname.
3. Policy: **Allow**, with a rule matching exactly the operator's email
   address. Never `Everyone`. Never `Bypass`.
4. On the API's Access application, enable CORS for the console origin with
   credentials allowed, so the browser's cross-origin calls carry the
   `CF_Authorization` cookie.
5. Leave `/healthz` and `/readyz` outside the Access application (a bypass
   policy scoped to those two paths) so the platform's probe still works.
   They expose a status word and a migration revision, nothing else.

With `OPSPILOT_AUTH_MODE=proxy` the API refuses any request that does not
carry `OPSPILOT_PROXY_IDENTITY_HEADER`. That is **not** authentication — it
is the application failing closed when the proxy is bypassed or
misconfigured. The header's value is never read as identity, never stored in
`actor_id`, and grants nothing. `decided_by` remains client-supplied
attribution, exactly as ADR-017 says.

---

## 5. Deploy

```bash
# API + database
railway login                       # one-time, interactive
railway init                        # or: railway link <project>
railway add --database postgres
railway up                          # builds backend/Dockerfile per railway.json
# then set the API environment above in the Railway dashboard or:
#   railway variables --set OPSPILOT_ENV=production ...

# console
cd frontend
vercel login                        # one-time, interactive
vercel link
vercel env add NEXT_PUBLIC_API_BASE_URL production
vercel deploy --prod
```

Then verify, in this order:

```bash
curl -fsS https://<api-hostname>/healthz          # {"status":"ok"}
curl -fsS https://<api-hostname>/readyz           # {"status":"ready","revision":"..."}
curl -si  https://<api-hostname>/runs | head -1   # 401/302 — the access layer is on
```

A `200` on that third command means the API is exposed without the access
layer. Stop and fix the Access policy before going further.

---

## 6. The canonical demo

The request, exactly:

> Find the top 3 fintech leads in London, research their companies, score
> them, draft outreach to the best one and email it to them.

The console has no run-submission form, so start the run over the API and
drive the approval in the browser:

```bash
curl -fsS -X POST https://<api-hostname>/runs \
  -H 'Content-Type: application/json' \
  -d '{"user_request":"Find the top 3 fintech leads in London, research their companies, score them, draft outreach to the best one and email it to them.","auto_start":true}'
```

What happens:

```
run_created → search_leads → research_company ×3 → score_lead ×3
            → draft_outreach → save_draft
            → ⏸ approval_requested (send_email_mock)     the run pauses here
            → operator opens /approvals/<id>, reads the payload, approves
            → approval_granted → send_email_mock → verification_passed
            → run_completed
```

Open `/runs/<run_id>` in the console before approving: the timeline updates
live over SSE as the run resumes, with no page reload.

What to check:

| Claim | Where to see it |
|---|---|
| The run pauses before the mutation | run status `awaiting_approval`, step `s8` not executed |
| No effect exists before approval | `mock_crm.email_outbox` has no row for the run |
| The exact arguments are bound | the approval's `args_hash`; a decision with any other hash is `409 approval_superseded` |
| The effect is simulated | the outbox row's `provider` is `mock`; no network-capable client exists in the mock package (`tests/test_structure.py`) |
| It was verified, not assumed | `verification_passed` on `s8`, read back from the outbox |
| The run ended | the trace's last event is `run_completed` |
| Reload reconstructs it | refresh `/runs/<run_id>` — the timeline is rebuilt from the trace |

### The rejection path

Start the same run again and reject it. The run ends `rejected` with
`status_reason=approval_rejected`, the outbox gains no row, and the decision
(`decided_by`, `decision_reason`) is on the approval row.

### Reproducing it locally

```bash
cp .env.example .env
make up && make migrate && make seed
cd frontend && npm ci && npm run dev      # console on :3000, API on :8000
```

Everything above works identically against `http://localhost:8000`, with no
access layer, because nothing is exposed — which is the whole of ADR-017.

---

## 7. What this deployment is not

* **Not multi-user.** One operator, one workspace, no identity in the
  application (§16.6).
* **Not audited.** `decided_by` is attribution, not authentication.
* **Not connected to anything real.** `OPSPILOT_INTEGRATIONS=mock` is the
  only implemented adapter set, and a real sender would be a new tool, never
  a flag on the mock (ADR-018).
* **Not "production-ready"** in the sense of running someone's business. It
  is a production-*style* system, demonstrated on a hosted deployment behind
  an access layer.
