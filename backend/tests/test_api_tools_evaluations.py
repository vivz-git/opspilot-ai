"""API contract tests for the tool catalog (§13.7, API-006) and the
evaluation endpoints (§13.6, API-005).

Every test drives `httpx.AsyncClient(transport=ASGITransport(app=app))`
against a real `create_app()` instance, the same convention
`test_api_contract.py` uses. `GET /tools` needs no database — it renders the
in-process contract registry. The `/evaluations` tests are Postgres
integration tests: `POST /evaluations/runs` drives a real `EvaluationRunner`
(the same one `python -m app.evaluation.cli` uses) over the real `smoke`
suite from `backend/evals/`, and the `GET` endpoints read back the rows it
persisted — nothing here fabricates a run or a result.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from app.config import Settings
from app.evaluation.loader import load_registry
from app.evaluation.registry import EvaluationRegistry
from app.evaluation.runner import EvaluationRunner
from app.main import create_app
from app.persistence.checkpointing import open_checkpointer
from app.persistence.session import create_session_factory
from app.tools.contracts import REGISTRY
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import migrate_to_head, require_database
from recovery_harness import settings as harness_settings

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# GET /tools (API-006) — no database needed, pure registry render
# ---------------------------------------------------------------------------
class TestListTools:
    async def test_returns_every_registered_tool_with_its_schema(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/tools")

        assert resp.status_code == 200
        body = resp.json()
        assert {entry["name"] for entry in body} == {name.value for name in REGISTRY}

        gated = next(entry for entry in body if entry["name"] == "send_email_mock")
        assert gated["requires_approval"] is True
        assert gated["risk"] == "high"
        assert gated["side_effect"] == "outbound"
        assert "input" in gated["schemas"] and "output" in gated["schemas"]
        assert all(fm["error_class"] and fm["description"] for fm in gated["failure_modes"])

        read_only = next(entry for entry in body if entry["name"] == "search_leads")
        assert read_only["requires_approval"] is False

    async def test_is_served_under_the_api_v1_prefix_too(self) -> None:
        app = create_app(settings=Settings(_env_file=None))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/tools")
        assert resp.status_code == 200
        assert len(resp.json()) == len(REGISTRY)

    async def test_never_executes_anything(self) -> None:
        """The catalog is read-only: no POST route exists to invoke a tool."""
        app = create_app(settings=Settings(_env_file=None))
        paths = app.openapi()["paths"]["/tools"]
        assert set(paths) == {"get"}


# ---------------------------------------------------------------------------
# /evaluations/* (API-005) — Postgres integration
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestEvaluationsApi:
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

    @pytest.fixture
    async def checkpointer(self, _database: None) -> AsyncIterator[AsyncPostgresSaver]:
        async with open_checkpointer(harness_settings()) as saver:
            yield saver

    @pytest.fixture(scope="class")
    def registry(self) -> EvaluationRegistry:
        return load_registry()

    @pytest.fixture
    def runner(
        self, engine: AsyncEngine, checkpointer: AsyncPostgresSaver, registry: EvaluationRegistry
    ) -> EvaluationRunner:
        return EvaluationRunner(
            settings=harness_settings(),
            session_factory=create_session_factory(engine),
            checkpointer=checkpointer,
            registry=registry,
        )

    async def test_post_runs_executes_the_suite_and_persists_it(
        self, runner: EvaluationRunner
    ) -> None:
        app = create_app(settings=harness_settings(), evaluation_runner=runner)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/evaluations/runs", json={"suite": "smoke"})
            assert created.status_code == 202
            run_payload = created.json()
            assert run_payload["suite"] == "smoke"
            evaluation_run_id = run_payload["evaluation_run_id"]

            # The background task runs to completion before the ASGI call
            # returns control (single event loop, no real network hop), so
            # the run is already terminal by the time we read it back.
            fetched = await client.get(f"/evaluations/runs/{evaluation_run_id}")
            assert fetched.status_code == 200
            fetched_body = fetched.json()
            assert fetched_body["status"] == "completed"
            assert fetched_body["case_count"] > 0
            assert fetched_body["case_count"] == fetched_body["passed"] + fetched_body["failed"]
            assert "case_pass_rate" in fetched_body["metrics"]

            results = await client.get(f"/evaluations/runs/{evaluation_run_id}/results")
            assert results.status_code == 200
            case_ids = {r["case_id"] for r in results.json()}
            assert case_ids  # at least one case ran
            for row in results.json():
                assert row["evaluation_run_id"] == evaluation_run_id
                assert isinstance(row["assertions"], list)

            listed = await client.get("/evaluations/runs", params={"suite": "smoke", "limit": 5})
            assert listed.status_code == 200
            listed_ids = {item["evaluation_run_id"] for item in listed.json()["items"]}
            assert evaluation_run_id in listed_ids

            metrics = await client.get("/evaluations/metrics", params={"suite": "smoke"})
            assert metrics.status_code == 200
            metrics_body = metrics.json()
            assert metrics_body["evaluation_run_id"] == evaluation_run_id
            assert metrics_body["metrics"]["total_cases"] == fetched_body["case_count"]

    async def test_post_runs_rejects_an_unknown_suite(self, runner: EvaluationRunner) -> None:
        app = create_app(settings=harness_settings(), evaluation_runner=runner)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/evaluations/runs", json={"suite": "does_not_exist"})
        assert resp.status_code == 422
        assert resp.headers["content-type"].startswith("application/problem+json")
        assert resp.json()["code"] == "validation_error"

    async def test_get_run_404s_for_an_unknown_id(self, runner: EvaluationRunner) -> None:
        app = create_app(settings=harness_settings(), evaluation_runner=runner)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/evaluations/runs/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_get_results_404s_for_an_unknown_run(self, runner: EvaluationRunner) -> None:
        app = create_app(settings=harness_settings(), evaluation_runner=runner)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/evaluations/runs/{uuid.uuid4()}/results")
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_metrics_is_empty_for_a_suite_that_never_ran(
        self, runner: EvaluationRunner
    ) -> None:
        app = create_app(settings=harness_settings(), evaluation_runner=runner)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/evaluations/metrics", params={"suite": f"nonexistent-{uuid.uuid4()}"}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["evaluation_run_id"] is None
        assert body["metrics"] == {}

    async def test_extra_fields_on_create_are_rejected(self, runner: EvaluationRunner) -> None:
        app = create_app(settings=harness_settings(), evaluation_runner=runner)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/evaluations/runs", json={"suite": "smoke", "unexpected": True}
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"
