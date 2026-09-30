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

**Status: not deployed.** The repository is engineering-complete for this
shape and its configuration is in the tree (`render.yaml`,
`frontend/vercel.json`, `backend/Dockerfile`, `backend/scripts/start.sh`), but
no Render service, Supabase project, Vercel project or Cloudflare Access
application exists, and no hostname in this repository describes anything
real. §5 is the checklist a human with those accounts works through; §5.3 is
how they prove it worked.

---

## 1. Architecture

```
                         Cloudflare Access
               (authenticates; allows exactly one operator)
                                 │
                 ┌───────────────┴───────────────┐
                 │                               │
          console hostname                 API hostname
                 │                               │
        ┌────────▼────────┐            ┌─────────▼──────────┐
        │  Vercel         │            │  Render (free)     │
        │  Next.js 15     │  ── HTTPS ─►│  FastAPI + uvicorn │
        │  console        │  (browser, │  backend/Dockerfile│
        └─────────────────┘  cookies)  └─────────┬──────────┘
                                                 │ TLS (asyncpg + psycopg)
                                       ┌─────────▼──────────┐
                                       │  Supabase          │
                                       │  PostgreSQL 16+    │
                                       │  session pooler    │
                                       └────────────────────┘
```

| Piece | Provider | Configuration in the repo |
|---|---|---|
| Access boundary | Cloudflare Access | none — it is infrastructure (§4) |
| Console | Vercel | `frontend/vercel.json` |
| API | Render Web Service, free plan, Docker | `render.yaml` → `backend/Dockerfile` → `backend/scripts/start.sh` |
| Database | Supabase PostgreSQL | `DATABASE_URL` only; the app is unchanged |

Two origins, both behind the same access layer. The console calls the API
directly — there is no rewrite proxy, so the origin split stays explicit
(`frontend/next.config.mjs`) — which means:

* the API must allowlist the console's origin in `CORS_ALLOW_ORIGINS`;
* the console sends every request with credentials (`credentials: "include"`,
  and `withCredentials` on the `EventSource`) so the proxy's session cookie
  rides along on the cross-origin call. Without it the proxy bounces the XHR
  to a login page the browser cannot follow.

**One instance.** Runs are driven in-process (ADR-004). Leases make a second
instance *safe* (ADR-023), not useful; `render.yaml` pins `numInstances: 1`.
Never scale the service out.

**One runtime path.** The hosted API is the same Docker image and the same
`scripts/start.sh` entrypoint that docker-compose and CI exercise. There is
no deployment-only code path; `render.yaml` only chooses environment values.

**Not Render Postgres.** Render's free Postgres expires after 30 days. The
database is Supabase, reached through `DATABASE_URL`; the persistence layer
does not know or care which provider it is talking to.

---

## 2. What the platform needs from the image

`backend/Dockerfile` builds the API. `backend/scripts/start.sh` is its
entrypoint and handles the things a platform needs:

| Variable | Default | What it does |
|---|---|---|
| `PORT` | `8000` | The port the platform assigned. Render injects it; `start.sh` binds `0.0.0.0:$PORT`. |
| `OPSPILOT_MIGRATE_ON_START` | `false` | Run `alembic upgrade head` before serving. |
| `OPSPILOT_SEED_ON_START` | `false` | Load the mock CRM **additively** (never truncates). |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Peers uvicorn accepts `X-Forwarded-*` from. |

Migrations are opt-in on purpose. `/readyz` already refuses traffic when the
applied revision is not the code's head, so a container that boots without
migrating fails its health check loudly instead of serving a stale schema
quietly. Render's free plan has no pre-deploy command, so `render.yaml` turns
migrate-on-start on. The seed is additive, so a restart never truncates the
rows an operator was looking at.

Health checks:

* `GET /healthz` — liveness. The process answers; nothing else is claimed.
* `GET /readyz` — readiness. Database reachable **and** at the migration
  head, or `503` (problem+json, `integration_unavailable`). This is Render's
  `healthCheckPath`. Render's probe reaches the container directly, not
  through Cloudflare.

---

## 3. Environment

Placeholders only. Every real value is set in the provider's secret store,
never in the repository, never in a build log, never in this file.

### API (Render)

`render.yaml` declares every variable; the two marked `sync: false` are
prompted for when the Blueprint is applied and stored by Render as secrets.

```
OPSPILOT_ENV=production
OPSPILOT_AUTH_MODE=proxy
OPSPILOT_PROXY_IDENTITY_HEADER=Cf-Access-Authenticated-User-Email
DATABASE_URL=postgresql+asyncpg://postgres.<project-ref>:<password>@<pooler-host>:5432/postgres?ssl=require   # sync: false
CORS_ALLOW_ORIGINS=https://<console-hostname>                                                          # sync: false
OPSPILOT_PLANNER=rules
OPSPILOT_INTEGRATIONS=mock
LOG_LEVEL=INFO
OPSPILOT_MIGRATE_ON_START=true
OPSPILOT_SEED_ON_START=true
FORWARDED_ALLOW_IPS=*
```

`OPSPILOT_PLANNER=rules` is the deployment default: the demo needs no API
key and stays byte-reproducible. `GROQ_API_KEY` is optional and only changes
how plans and copy are written (ADR-002); if you set it, set it in Render's
secret store and nowhere else.

`FORWARDED_ALLOW_IPS=*` is deliberate on Render: the container is reachable
only through Render's proxy, whose addresses are not published, and the
forwarded headers only set the request's scheme and client address. Nothing
in the application authorises on either.

### Database (Supabase)

The application needs an async PostgreSQL URL and nothing provider-specific:

1. In the Supabase project, open **Connect** and copy the **Session pooler**
   connection string. Use the session pooler (port `5432` on the pooler
   host), not the transaction pooler (`6543`): the checkpointer bootstraps
   under a session-level advisory lock and asyncpg uses prepared statements,
   and neither survives transaction pooling. The session pooler is also the
   IPv4-reachable option.
2. Change the scheme from `postgresql://` to `postgresql+asyncpg://`.
3. Append `?ssl=require`. That is asyncpg's spelling of TLS;
   `app/persistence/checkpointing.py::libpq_conninfo` translates it to
   `sslmode=require` for the LangGraph saver's psycopg connection, so one URL
   serves both drivers (`tests/test_checkpointing.py` pins the translation).
4. URL-encode the password if it contains reserved characters (`@`, `:`,
   `/`, `%`, …).

Everything the application creates lives in its own schemas — `opspilot` and
`mock_crm` (Alembic), `langgraph` (the saver) — so it does not collide with
Supabase's own. The first boot creates them (`OPSPILOT_MIGRATE_ON_START`).
Size the connection budget against your plan's pooler limit: at peak the API
holds its SQLAlchemy pool (5, overflow 10), the checkpointer's pool (≤ 4) and
one short-lived bootstrap connection.

### Console (Vercel)

```
NEXT_PUBLIC_API_BASE_URL=https://<api-hostname>
```

This is baked into the client bundle at **build** time. Changing the API
hostname needs a rebuild, not a restart. It must be `https://` or the browser
blocks it as mixed content. It is the only variable the console reads, and
it is not a secret.

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

This is the security boundary. It is not optional, and the application cannot
verify it for you.

1. Serve both hostnames through Cloudflare as **proxied** (orange-cloud) DNS
   records: the console hostname to the Vercel project, the API hostname to
   the Render service. Follow each provider's own Cloudflare DNS guide for the
   record type and domain verification
   (<https://render.com/docs/configure-cloudflare-dns>, Vercel's custom-domain
   docs). Access protects only proxied hostnames.
2. Create a Cloudflare Access application covering the console hostname, and
   a second covering the API hostname.
3. Policy on both: **Allow**, with an include rule matching exactly the
   operator's email address. Never `Everyone`. Never a `Bypass` over the whole
   hostname.
4. On the API's Access application, configure CORS for the console origin
   with credentials allowed, so the browser's cross-origin calls carry the
   `CF_Authorization` cookie and their preflights are answered. The API's own
   `CORSMiddleware` echoes only that one origin.
5. **Disable Render's default `onrender.com` subdomain.** Every Render web
   service is also published at `https://<service>.onrender.com`, which does
   *not* pass through Cloudflare. The application's `proxy` fence checks only
   that the identity header is *present* (ADR-026) — it is defence in depth,
   not authentication — so a caller who found that URL could send the header
   themselves. Once the Cloudflare-proxied API hostname is added to the
   service and verified, set the service's **Render Subdomain** to
   **Disabled** (Settings → Custom Domains), or uncomment `domains:` and
   `renderSubdomainPolicy: disabled` in `render.yaml` and re-sync. Render then
   answers every `onrender.com` request with `404` without forwarding it. The
   deployment is **not** complete until §5.3 proves this.
6. `/healthz` and `/readyz` need no bypass for Render: its health check reaches
   the container directly. Add a Bypass policy scoped to exactly those two
   paths **only** if an external uptime monitor must reach them through the
   public hostname — they expose a status word and a migration revision,
   nothing else. They are the *only* paths that answer without the proxy
   header: `/openapi.json`, `/docs` and `/redoc` are fenced with everything
   else, so a bypassed proxy is not handed the shape of the API (LAUNCH-002).

The console's own `*.vercel.app` alias also bypasses Cloudflare. It holds no
data and no secret (the bundle's only configuration is
`NEXT_PUBLIC_API_BASE_URL`), and from that origin it cannot use the API —
CORS allowlists only the Cloudflare-fronted console origin and the API itself
sits behind Access. Turn on Vercel's deployment protection for it anyway if
your plan offers it.

With `OPSPILOT_AUTH_MODE=proxy` the API refuses any request that does not
carry `OPSPILOT_PROXY_IDENTITY_HEADER`. That is **not** authentication — it
is the application failing closed when the proxy is bypassed or
misconfigured. The header's value is never read as identity, never stored in
`actor_id`, and grants nothing. `decided_by` remains client-supplied
attribution, exactly as ADR-017 says.

---

## 5. Deploy

Order matters: the database must exist before the API can become ready, and
the API hostname must exist before the console can be built against it.

### 5.1 Provision

1. **Supabase.** Create a project (choose the region nearest the Render
   region in `render.yaml`, `frankfurt`), set a strong database password,
   and build `DATABASE_URL` as in §3.
2. **Render.** New → Blueprint → this repository. Render reads `render.yaml`,
   creates `opspilot-api` on the free plan and prompts for `DATABASE_URL` and
   `CORS_ALLOW_ORIGINS` (`https://<console-hostname>`). The first deploy
   builds `backend/Dockerfile`, migrates, seeds and turns healthy on
   `/readyz`.
3. **Vercel.** Import the repository with **Root Directory** `frontend`
   (`frontend/vercel.json` pins `npm ci` / `npm run build`), set
   `NEXT_PUBLIC_API_BASE_URL=https://<api-hostname>` for Production, and
   deploy.
4. **Cloudflare.** Add both custom domains (§4.1), verify them in Render and
   Vercel, create both Access applications (§4.2–4.4), then disable the
   `onrender.com` subdomain (§4.5).

### 5.2 Free-tier behaviour to expect

* **Cold start.** A free Render web service spins down after a period with
  no inbound traffic and takes about a minute to come back on the next
  request. The first request after an idle spell — through Cloudflare, after
  the Access login — simply waits. This is an accepted trade-off for a
  portfolio demo, not a defect; a paid instance removes it.
* **A spin-down never loses a run.** Paused runs have no worker by design, and
  a run interrupted mid-execution is resumed from its LangGraph checkpoint by
  the startup reconciler on the next boot (DB-007, ADR-023).
* **Supabase free projects** are subject to the plan's inactivity and size
  limits; check the current terms, and resume the project from the Supabase
  dashboard if it has been paused.

### 5.3 Verify

In this order. Each line states what a correct deployment answers.

```bash
# The access layer is on, for the API and for its schema.
curl -si https://<api-hostname>/runs | head -1          # 302 or 401/403 from Access — never 200
curl -si https://<api-hostname>/openapi.json | head -1  # likewise
curl -si https://<console-hostname>/ | head -1          # 302 to the Access login

# The origin cannot be reached around Cloudflare — not even with a forged header.
curl -si https://<service>.onrender.com/runs \
  -H 'Cf-Access-Authenticated-User-Email: attacker@example.com' | head -1   # 404 from Render

# Health and readiness over TLS, through the access layer as the operator
# (`cloudflared access curl` performs the Access login; plain curl works only
# if the optional two-path bypass of §4.6 was configured).
cloudflared access curl https://<api-hostname>/healthz  # {"status":"ok"}
cloudflared access curl https://<api-hostname>/readyz   # {"status":"ready","revision":"<alembic head>"}
```

A `200` on `/runs` or `/openapi.json` without signing in, or anything but
`404` from the `onrender.com` URL, means the API is exposed. Stop and fix it
before going further.

Then, signed in through Access in a browser, run the canonical demo (§6) end
to end, including the rejection path and an expired approval.

---

## 6. The canonical demo

The request, exactly:

> Find the top 3 fintech leads in London, research their companies, score
> them, draft outreach to the best one and email it to them.

Open **Runs** in the console, click **Use the canonical request**, then
**Submit run**. The console posts it to `POST /runs` with `auto_start` and
opens the run the server created. (The API route works directly too:
`POST /runs` with `{"user_request": "...", "auto_start": true}`.)

What happens:

```
run_created → search_leads → research_company ×3 → score_lead ×3
            → draft_outreach → save_draft
            → ⏸ approval_requested (send_email_mock)     the run pauses here
            → operator opens /approvals/<id>, reads the payload, approves
            → approval_granted → send_email_mock → verification_passed
            → run_completed
```

Keep `/runs/<run_id>` open before approving: the timeline updates live over
SSE as the run resumes, with no page reload.

What to check:

| Claim | Where to see it |
|---|---|
| The run pauses before the mutation | run status `awaiting_approval`; step `s8` has no `execution_steps` row and `step_count` is 9 |
| No effect exists before approval | `mock_crm.email_outbox` has no row for the run |
| The exact arguments are bound | the approval's `args_hash`; a decision with any other hash is `409 approval_superseded` |
| The effect is simulated | the outbox row's `provider` is `mock`; no network-capable client exists in the mock package (`tests/test_structure.py`) |
| It was verified, not assumed | `verification_passed` on `s8`, read back from the outbox |
| The run ended | the trace's last event is `run_completed`; every step is `succeeded`, `step_count` is 10 |
| Reload reconstructs it | refresh `/runs/<run_id>` — the timeline is rebuilt from the trace over REST |

### The rejection path

Submit the same request again and reject it. The run ends `rejected` with
`status_reason=approval_rejected`, the outbox gains no row, the gated step
never gets an `execution_steps` row, and the decision (`decided_by`,
`decision_reason`) is on the approval row.

### The expired-approval path

Approvals expire after `OPSPILOT_APPROVAL_TTL_SECONDS` (minimum 60). A
decision on an expired approval is refused `409 approval_expired` and has no
side effect.

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
* **Not always warm.** The free Render instance sleeps when idle and wakes in
  about a minute (§5.2).
* **Not "production-ready"** in the sense of running someone's business. It
  is a production-*style* system, demonstrated on a hosted deployment behind
  an access layer.
