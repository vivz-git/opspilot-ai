"""TEST-004: API contract tests over ASGI transport, for every endpoint and
every `code` in §13.1 (RFC 9457 Problem Details).

This module does not re-cover ground the existing API suites already hold.
Before adding anything here, the registered FastAPI routes and the eleven
`code`s of §13.1 were cross-checked against the suite that already exists:

| Endpoint | Covered in |
|---|---|
| `POST /runs`, `GET /runs`, `GET /runs/{id}` | `test_api_runs.py`, `test_api_runs_management.py` |
| `POST /runs/{id}/start` | `test_api_runs_management.py` (mocked), `test_executor.py` (real) |
| `POST /runs/{id}/cancel` | `test_api_runs_management.py`; real, but via direct calls (§below) |
| `POST /runs/{id}/retry` | `test_api_runs_management.py`; real, but via direct calls (§below) |
| `GET /runs/{id}/trace`, `GET /runs/{id}/events` (SSE) | `test_api_trace.py` |
| `GET /approvals/queue`, `/{id}`, `POST /approvals/{id}/decision` | `test_api_approvals.py` |
| `GET /healthz`, `GET /readyz` | `test_health.py` |

| §13.1 `code` | HTTP | Covered in |
|---|---|---|
| `validation_error` | 422 | every API test file; two `extra="forbid"` bodies closed here |
| `not_found` | 404 | `test_api_runs*.py`, `test_api_approvals.py`, `test_api_trace.py` |
| `run_not_startable` | 409 | `test_api_runs_management.py` (mocked), `test_executor.py` (real) |
| `run_not_resumable` | 409 | `test_api_approvals.py` (mocked + real) |
| `approval_not_pending` | 409 | `test_api_approvals.py` (mocked + real) |
| `approval_expired` | 409 | `test_api_approvals.py` (mocked + real) |
| `approval_superseded` | 409 | `test_api_approvals.py` (mocked + real) |
| `idempotency_conflict` | 409 | `test_api_runs.py` (mocked + real) |
| `budget_exhausted` | 409 | **new here** — no test, no handler even registered |
| `integration_unavailable` | 503 | `test_health.py` |
| `internal_error` | 500 | **new here** — no test anywhere |

**Two gaps this module closes.**

1. **`budget_exhausted` had no exception handler.** `app.errors.BudgetExhaustedError`
   is declared in the taxonomy and `docs/architecture.md` §13.1 documents
   `budget_exhausted` as a 409, but `app/api/errors.py` had no
   `@app.exception_handler(BudgetExhaustedError)` — if the error were ever
   raised inside a route, FastAPI's registered handlers would not match it
   and it would fall through to the generic `Exception` handler, surfacing
   as `500 internal_error` instead of the documented `409 budget_exhausted`.
   This is a genuine contract defect: an already-declared error class with an
   already-documented HTTP mapping that the API layer never wired up. Fixed
   in `app/api/errors.py` by adding the handler in the same shape as every
   other domain error — no new business rule, no new way to trigger the
   error, just the missing translation. **No production code path raises
   `BudgetExhaustedError` today** (grep confirms it), so the test below
   drives it through a mocked `RunService`, exactly the pattern the rest of
   this suite already uses for other otherwise-hard-to-reach conflict codes
   (e.g. `test_post_runs_idempotency_conflict_returns_409` in
   `test_api_runs.py`). It locks in the mapping for whenever a producer of
   this error exists; it does not invent one.
2. **`internal_error` had no test at all.** The catch-all `Exception` handler
   in `app/api/errors.py` was implemented but never exercised — worth a test
   because it is the API's one guarantee against leaking exception internals
   to a client.

**Genuine defect reported, not fixed.** §13.1 states "`trace_id` is echoed on
every error", but `register_error_handlers` never passes `trace_id` to
`problem_details(...)` on any of its ~13 handlers — every error response
today omits the field entirely. This is pre-existing, applies uniformly to
every error path (not something `budget_exhausted` introduced), and fixing
it needs a request-correlation mechanism (e.g. middleware minting a
per-request id) that doesn't exist yet and that no acceptance criterion of
this task requires. Out of scope here; recorded for whoever owns OBS-005 or
a future API task. The tests below assert what the API genuinely returns
(no `trace_id` key), not the aspirational doc text.

**§13.6/§13.7 are now built.** The evaluation endpoints (API-005) and the
`GET /tools` catalog route (API-006) are wired into `app/main.py` via
`app/api/evaluations.py` and `app/api/tools.py`. Their contract tests live in
`test_api_evaluations.py` and `test_api_tools.py`, not here — this module
stays scoped to the gaps it was written to close (§ above).

Every test here uses `httpx.AsyncClient(transport=ASGITransport(app=app))`
against a real `create_app()` instance and never calls a service method
directly; the two Postgres-backed tests use a real `RunService` over a real
database, reached only through the HTTP route.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from app.agent.state import RunStatus
from app.api.dependencies import get_run_service
from app.errors import BudgetExhaustedError
from app.execution.runs import RunCreateResult, RunService
from app.main import create_app
from app.runtime import FixedClock
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import T0, create_run, migrate_to_head, require_database, uow_factory_for
from recovery_harness import settings as harness_settings
from test_api_runs import _make_sample_run

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# budget_exhausted (409) — dormant error class, now correctly mapped
# ---------------------------------------------------------------------------
class TestBudgetExhaustedMapping:
    async def test_budget_exhausted_returns_409_problem_details(self) -> None:
        """No route raises `BudgetExhaustedError` today (§ module docstring),
        so the service dependency is substituted to raise it — the same
        technique `test_api_runs.py` uses for `idempotency_conflict`. This
        proves the *mapping* the handler added in `app/api/errors.py`
        performs, independent of whoever eventually produces the error."""
        mock_service = AsyncMock(spec=RunService)
        mock_service.create_run.side_effect = BudgetExhaustedError(
            "Run creation would exceed the configured concurrent-run budget",
            detail={"limit": 100},
        )

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/runs", json={"user_request": "One more run please"})

        assert resp.status_code == 409
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["code"] == "budget_exhausted"
        assert body["status"] == 409
        assert body["type"] == "https://opspilot.dev/errors/budget-exhausted"
        assert "budget" in body["detail"]
        assert body["errors"] == []


# ---------------------------------------------------------------------------
# internal_error (500) — the generic catch-all, never exercised before
# ---------------------------------------------------------------------------
class TestInternalErrorMapping:
    async def test_unhandled_exception_returns_generic_500_with_no_leak(self) -> None:
        mock_service = AsyncMock(spec=RunService)
        run_row = _make_sample_run()
        mock_service.create_run.return_value = RunCreateResult(run=run_row, is_duplicate=False)
        # An exception outside the declared taxonomy: no handler matches it
        # but `Exception`, so this proves the catch-all rather than any
        # domain-specific translation.
        # A fake leaked-message fixture, not a real credential.
        secret_detail = "psycopg.OperationalError: password authentication failed for user X"  # noqa: S105
        mock_service.get_run_details.side_effect = RuntimeError(secret_detail)

        app = create_app()
        app.dependency_overrides[get_run_service] = lambda: mock_service

        # `ServerErrorMiddleware` (Starlette) always re-raises the original
        # exception after sending our handler's response, so a test client
        # can choose to surface it for debugging. The response is already on
        # the wire by then; `raise_app_exceptions=False` tells httpx to
        # return it instead of propagating the exception it deliberately
        # re-raises (see `starlette.middleware.errors.ServerErrorMiddleware`).
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/runs", json={"user_request": "Trigger a bug"})

        assert resp.status_code == 500
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["code"] == "internal_error"
        assert body["status"] == 500
        assert body["type"] == "https://opspilot.dev/errors/internal-error"
        # The generic message only — never the real exception text (§16.5).
        assert body["detail"] == "An unexpected internal error occurred"
        assert secret_detail not in resp.text
        assert "psycopg" not in resp.text
        assert "password" not in resp.text
        # No stack trace, no exception class name, leaked to the client.
        assert "RuntimeError" not in resp.text
        assert body["errors"] == []


# ---------------------------------------------------------------------------
# extra="forbid" 422 — the two request bodies with no prior coverage
# ---------------------------------------------------------------------------
class TestExtraFieldsForbiddenOnRunManagementBodies:
    async def test_cancel_run_extra_field_returns_422(self) -> None:
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/runs/{uuid.uuid4()}/cancel",
                json={"reason": "changed my mind", "unexpected_field": "nope"},
            )

        assert resp.status_code == 422
        assert resp.headers["content-type"].startswith("application/problem+json")
        assert resp.json()["code"] == "validation_error"

    async def test_retry_run_extra_field_returns_422(self) -> None:
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/runs/{uuid.uuid4()}/retry",
                json={"auto_start": True, "unexpected_field": "nope"},
            )

        assert resp.status_code == 422
        assert resp.headers["content-type"].startswith("application/problem+json")
        assert resp.json()["code"] == "validation_error"


# ---------------------------------------------------------------------------
# run_not_cancellable / run_active over real ASGI transport + real Postgres.
# `test_api_runs_management.py` already covers the *state machine* for these
# two codes: through a mocked `RunService` for the HTTP shape, and through a
# real `RunService` for the transition logic itself — but the real-service
# case there calls `RunService.cancel_run`/`retry_run` directly, not the
# route. These two tests are the missing combination: real persistence,
# reached only through the HTTP route (§ TEST-004 "do not bypass route
# handlers with direct service calls when testing the HTTP contract").
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestRunManagementConflictsRealComposition:
    @pytest.fixture(scope="class")
    def _database(self) -> None:
        require_database()
        migrate_to_head()

    @pytest.fixture
    async def engine(self, _database: None) -> AsyncIterator[AsyncEngine]:
        eng = create_async_engine(
            harness_settings().database_url.get_secret_value(),
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=10,
        )
        try:
            yield eng
        finally:
            await eng.dispose()

    async def test_cancel_a_terminal_run_over_http_returns_409_run_not_cancellable(
        self, engine: AsyncEngine
    ) -> None:
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        run_id = await create_run(uow_factory, status=RunStatus.COMPLETED)

        service = RunService(uow_factory=uow_factory, settings=harness_settings(), clock=clock)
        app = create_app(settings=harness_settings(), run_service=service, clock=clock)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(f"/runs/{run_id}/cancel", json={"reason": "too late"})

        assert resp.status_code == 409
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["code"] == "run_not_cancellable"
        assert body["status"] == 409
        assert str(run_id) in body["detail"]

        async with uow_factory() as uow:
            row = await uow.agent_runs.get(run_id)
            assert row is not None
            assert row.status == RunStatus.COMPLETED
            await uow.commit()

    async def test_retry_an_active_run_over_http_returns_409_run_active(
        self, engine: AsyncEngine
    ) -> None:
        uow_factory = uow_factory_for(engine)
        clock = FixedClock(T0)
        # CREATED is not in TERMINAL_RUN_STATUSES: retry_run treats it as active.
        run_id = await create_run(uow_factory, status=RunStatus.CREATED)

        service = RunService(uow_factory=uow_factory, settings=harness_settings(), clock=clock)
        app = create_app(settings=harness_settings(), run_service=service, clock=clock)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(f"/runs/{run_id}/retry")

        assert resp.status_code == 409
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["code"] == "run_active"
        assert body["status"] == 409
        assert str(run_id) in body["detail"]

        # No child run was created for the refused retry.
        async with uow_factory() as uow:
            row = await uow.agent_runs.get(run_id)
            assert row is not None
            assert row.parent_run_id is None
            await uow.commit()
