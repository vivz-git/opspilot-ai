"""API-005: evaluation endpoints tests (§13.6).

Covers:
1. `POST /evaluations/runs` triggers the real suite mechanism and returns
   the documented `202` `EvaluationRunResource(status=running)`.
2. `GET /evaluations/runs`, `GET /evaluations/runs/{id}` — success shapes.
3. `GET /evaluations/runs/{id}/results` — `?passed=false` filtering, and
   every result exposes the *real*, inspectable `run_id`.
4. `GET /evaluations/metrics` — the persisted metric snapshot, `suite`/
   `window` filtering.
5. Documented validation/error paths: unknown suite, `case_ids` not in the
   suite, malformed `evaluation_run_id`, unknown `evaluation_run_id`,
   `extra="forbid"` on the request body, malformed `window`.

Unit tests substitute a mocked `EvaluationService` (the same technique
`test_api_runs.py`/`test_api_approvals.py` use) to pin the HTTP shape
without a suite actually running. The integration tests drive
`EvaluationRunner.run_suite` for real — real Postgres, the real graph, the
real `ToolRegistry` — reached only through the HTTP route, and read the
persisted `evaluation_runs`/`evaluation_results`/`agent_runs` rows directly
to prove `run_id` is real and inspectable (not a second execution path).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from app.agent.state import PlannerKind, RunStatus
from app.api.dependencies import get_evaluation_service
from app.errors import InputValidationError, NotFoundError
from app.evaluation.loader import load_registry
from app.evaluation.registry import EvaluationRegistry
from app.evaluation.runner import EvaluationRunner
from app.execution.evaluations import EvaluationService
from app.main import create_app
from app.persistence.checkpointing import open_checkpointer
from app.persistence.models import EvaluationResult, EvaluationRun, EvaluationRunStatus
from app.persistence.session import create_session_factory, unit_of_work
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from recovery_harness import migrate_to_head, require_database, uow_factory_for
from recovery_harness import settings as harness_settings

pytestmark = [pytest.mark.unit]


def _make_evaluation_run(
    *,
    run_id: uuid.UUID | None = None,
    suite: str = "all",
    status: EvaluationRunStatus = EvaluationRunStatus.RUNNING,
    case_count: int = 0,
    passed: int = 0,
    failed: int = 0,
    metrics: dict[str, object] | None = None,
) -> EvaluationRun:
    now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
    return EvaluationRun(
        id=run_id or uuid.uuid4(),
        suite=suite,
        status=status,
        started_at=now,
        finished_at=None if status is EvaluationRunStatus.RUNNING else now,
        git_sha=None,
        planner_kind=PlannerKind.RULES,
        model_id=None,
        prompt_version=None,
        seed=1337,
        case_count=case_count,
        passed=passed,
        failed=failed,
        metrics=metrics or {},
    )


def _make_evaluation_result(
    *,
    evaluation_run_id: uuid.UUID,
    case_id: str = "lead_ranking",
    run_id: uuid.UUID | None = None,
    passed: bool = True,
) -> EvaluationResult:
    return EvaluationResult(
        id=uuid.uuid4(),
        evaluation_run_id=evaluation_run_id,
        case_id=case_id,
        run_id=run_id or uuid.uuid4(),
        passed=passed,
        assertions=[{"name": "final_status", "passed": True, "detail": ""}],
        duration_ms=42,
        retry_count=0,
        tool_calls_count=5,
        approval_outcome=None,
        failure_reason=None,
    )


# ---------------------------------------------------------------------------
# POST /evaluations/runs
# ---------------------------------------------------------------------------
class TestCreateEvaluationRun:
    def test_valid_request_returns_202_with_running_resource(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        eval_run = _make_evaluation_run(suite="smoke")
        mock_service.trigger_run.return_value = eval_run

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post("/evaluations/runs", json={"suite": "smoke"})

        assert resp.status_code == 202
        body = resp.json()
        assert body["evaluation_run_id"] == str(eval_run.id)
        assert body["suite"] == "smoke"
        assert body["status"] == "running"
        assert body["case_count"] == 0
        mock_service.trigger_run.assert_awaited_once_with(
            suite="smoke", case_ids=None, planner=None
        )

    def test_defaults_to_suite_all(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        mock_service.trigger_run.return_value = _make_evaluation_run(suite="all")

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post("/evaluations/runs", json={})

        assert resp.status_code == 202
        mock_service.trigger_run.assert_awaited_once_with(suite="all", case_ids=None, planner=None)

    def test_case_ids_and_planner_are_forwarded(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        mock_service.trigger_run.return_value = _make_evaluation_run(suite="all")

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                "/evaluations/runs",
                json={"suite": "all", "case_ids": ["lead_ranking"], "planner": "rules"},
            )

        assert resp.status_code == 202
        mock_service.trigger_run.assert_awaited_once_with(
            suite="all", case_ids=["lead_ranking"], planner=PlannerKind.RULES
        )

    def test_unknown_suite_returns_422_validation_error(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        mock_service.trigger_run.side_effect = InputValidationError("unknown suite 'bogus'")

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post("/evaluations/runs", json={"suite": "bogus"})

        assert resp.status_code == 422
        assert resp.headers["content-type"].startswith("application/problem+json")
        assert resp.json()["code"] == "validation_error"

    def test_case_ids_not_in_suite_returns_422(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        mock_service.trigger_run.side_effect = InputValidationError(
            "case_ids not in suite 'smoke': ['invalid_tool_result']"
        )

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.post(
                "/evaluations/runs",
                json={"suite": "smoke", "case_ids": ["invalid_tool_result"]},
            )

        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

    def test_extra_field_returns_422(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            resp = client.post("/evaluations/runs", json={"suite": "all", "unexpected": "nope"})

        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"


# ---------------------------------------------------------------------------
# GET /evaluations/runs, GET /evaluations/runs/{id}
# ---------------------------------------------------------------------------
class TestListAndGetEvaluationRuns:
    def test_list_returns_runs_newest_first_as_given_by_the_service(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        runs = [_make_evaluation_run(suite="all"), _make_evaluation_run(suite="smoke")]
        mock_service.list_runs.return_value = runs

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get("/evaluations/runs")

        assert resp.status_code == 200
        body = resp.json()
        assert [item["evaluation_run_id"] for item in body["items"]] == [str(r.id) for r in runs]
        mock_service.list_runs.assert_awaited_once_with(suite=None, limit=25)

    def test_list_forwards_suite_and_limit(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        mock_service.list_runs.return_value = []

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get("/evaluations/runs", params={"suite": "smoke", "limit": 10})

        assert resp.status_code == 200
        mock_service.list_runs.assert_awaited_once_with(suite="smoke", limit=10)

    def test_get_existing_run_returns_200(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        eval_run = _make_evaluation_run(
            suite="all",
            status=EvaluationRunStatus.COMPLETED,
            case_count=7,
            passed=7,
            failed=0,
            metrics={"case_pass_rate": 1.0},
        )
        mock_service.require_run.return_value = eval_run

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/evaluations/runs/{eval_run.id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["evaluation_run_id"] == str(eval_run.id)
        assert body["status"] == "completed"
        assert body["case_count"] == 7
        assert body["metrics"] == {"case_pass_rate": 1.0}

    def test_get_unknown_run_returns_404_not_found(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        mock_service.require_run.side_effect = NotFoundError("Evaluation run x not found")

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/evaluations/runs/{uuid.uuid4()}")

        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_get_malformed_uuid_returns_422(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/evaluations/runs/not-a-uuid")

        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"


# ---------------------------------------------------------------------------
# GET /evaluations/runs/{id}/results
# ---------------------------------------------------------------------------
class TestListEvaluationResults:
    def test_results_expose_the_real_run_id(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        eval_run_id = uuid.uuid4()
        real_agent_run_id = uuid.uuid4()
        mock_service.list_results.return_value = [
            _make_evaluation_result(evaluation_run_id=eval_run_id, run_id=real_agent_run_id)
        ]

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/evaluations/runs/{eval_run_id}/results")

        assert resp.status_code == 200
        [item] = resp.json()["items"]
        assert item["run_id"] == str(real_agent_run_id)
        assert item["case_id"] == "lead_ranking"
        mock_service.list_results.assert_awaited_once_with(eval_run_id, passed=None)

    def test_passed_false_filter_is_forwarded(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        eval_run_id = uuid.uuid4()
        mock_service.list_results.return_value = [
            _make_evaluation_result(evaluation_run_id=eval_run_id, passed=False)
        ]

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(
                f"/evaluations/runs/{eval_run_id}/results", params={"passed": "false"}
            )

        assert resp.status_code == 200
        [item] = resp.json()["items"]
        assert item["passed"] is False
        mock_service.list_results.assert_awaited_once_with(eval_run_id, passed=False)

    def test_results_for_unknown_run_return_404(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        mock_service.list_results.side_effect = NotFoundError("Evaluation run x not found")

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get(f"/evaluations/runs/{uuid.uuid4()}/results")

        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# GET /evaluations/metrics
# ---------------------------------------------------------------------------
class TestEvaluationMetrics:
    def test_returns_persisted_metrics_snapshots(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        mock_service.get_metrics.return_value = [
            _make_evaluation_run(
                suite="all",
                status=EvaluationRunStatus.COMPLETED,
                metrics={"case_pass_rate": 1.0, "task_success_rate": 1.0},
            )
        ]

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get("/evaluations/metrics", params={"window": "30d", "suite": "all"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["window"] == "30d"
        assert body["suite"] == "all"
        [entry] = body["items"]
        assert entry["metrics"]["case_pass_rate"] == 1.0
        mock_service.get_metrics.assert_awaited_once_with(suite="all", window="30d")

    def test_malformed_window_returns_422(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        mock_service.get_metrics.side_effect = InputValidationError(
            "window 'bogus' is not a valid duration (expected e.g. '30d' or '24h')"
        )

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            resp = client.get("/evaluations/metrics", params={"window": "bogus"})

        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"


# ---------------------------------------------------------------------------
# API-006 parity: /api/v1 prefix
# ---------------------------------------------------------------------------
class TestPrefixParity:
    def test_api_v1_and_root_evaluations_parity(self) -> None:
        mock_service = AsyncMock(spec=EvaluationService)
        mock_service.trigger_run.return_value = _make_evaluation_run(suite="all")

        app = create_app()
        app.dependency_overrides[get_evaluation_service] = lambda: mock_service

        with TestClient(app) as client:
            root = client.post("/evaluations/runs", json={"suite": "all"})
            v1 = client.post("/api/v1/evaluations/runs", json={"suite": "all"})

        assert root.status_code == v1.status_code == 202


# ---------------------------------------------------------------------------
# Integration: the real suite mechanism, driven only over HTTP (§15.5)
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestEvaluationsApiPostgresIntegration:
    @pytest.fixture(scope="class")
    def _database(self) -> None:
        require_database()
        migrate_to_head()

    @pytest.fixture(scope="class")
    def registry(self, _database: None) -> EvaluationRegistry:
        return load_registry()

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
    async def checkpointer(self) -> AsyncIterator[AsyncPostgresSaver]:
        async with open_checkpointer(harness_settings()) as saver:
            yield saver

    @pytest.fixture
    def service(
        self,
        engine: AsyncEngine,
        checkpointer: AsyncPostgresSaver,
        registry: EvaluationRegistry,
    ) -> EvaluationService:
        runner = EvaluationRunner(
            settings=harness_settings(),
            session_factory=create_session_factory(engine),
            checkpointer=checkpointer,
            registry=registry,
        )
        return EvaluationService(
            uow_factory=uow_factory_for(engine), registry=registry, runner=runner
        )

    async def _poll_until_terminal(
        self, client: AsyncClient, evaluation_run_id: str, *, timeout_s: float = 45.0
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout_s
        while True:
            resp = await client.get(f"/evaluations/runs/{evaluation_run_id}")
            body = resp.json()
            if body["status"] != "running":
                return body
            if time.monotonic() > deadline:
                raise AssertionError(f"evaluation run stayed running: {body}")
            await asyncio.sleep(0.25)

    async def test_post_triggers_the_real_suite_and_persists_results(
        self, service: EvaluationService, engine: AsyncEngine
    ) -> None:
        app = create_app(evaluation_service=service)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            create_resp = await client.post(
                "/evaluations/runs",
                json={"suite": "smoke", "case_ids": ["lead_ranking"]},
            )
            assert create_resp.status_code == 202
            created = create_resp.json()
            assert created["status"] == "running"
            assert created["suite"] == "smoke"
            evaluation_run_id = created["evaluation_run_id"]

            final = await self._poll_until_terminal(client, evaluation_run_id)
            assert final["status"] == "completed", final
            assert final["case_count"] == 1
            assert final["passed"] == 1
            assert final["failed"] == 0
            assert final["metrics"]["case_pass_rate"] == 1.0

            results_resp = await client.get(f"/evaluations/runs/{evaluation_run_id}/results")
            assert results_resp.status_code == 200
            [result] = results_resp.json()["items"]
            assert result["case_id"] == "lead_ranking"
            assert result["passed"] is True
            real_run_id = result["run_id"]

        # The result's run_id is a real, independently inspectable agent run.
        async with unit_of_work(create_session_factory(engine)) as uow:
            agent_run = await uow.agent_runs.get(uuid.UUID(real_run_id))
            assert agent_run is not None
            assert agent_run.eval_case_id == "lead_ranking"
            assert agent_run.status is RunStatus.COMPLETED
            assert str(agent_run.evaluation_run_id) == evaluation_run_id
            await uow.commit()

    async def test_passed_false_filter_excludes_a_passing_case(
        self, service: EvaluationService
    ) -> None:
        app = create_app(evaluation_service=service)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            create_resp = await client.post(
                "/evaluations/runs",
                json={"suite": "smoke", "case_ids": ["lead_ranking"]},
            )
            evaluation_run_id = create_resp.json()["evaluation_run_id"]
            await self._poll_until_terminal(client, evaluation_run_id)

            all_results = await client.get(f"/evaluations/runs/{evaluation_run_id}/results")
            failed_only = await client.get(
                f"/evaluations/runs/{evaluation_run_id}/results", params={"passed": "false"}
            )
            passed_only = await client.get(
                f"/evaluations/runs/{evaluation_run_id}/results", params={"passed": "true"}
            )

        assert len(all_results.json()["items"]) == 1
        assert failed_only.json()["items"] == []
        assert len(passed_only.json()["items"]) == 1

    async def test_unknown_suite_over_http_returns_422(self, service: EvaluationService) -> None:
        app = create_app(evaluation_service=service)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/evaluations/runs", json={"suite": "does-not-exist"})

        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

    async def test_case_ids_outside_suite_over_http_returns_422(
        self, service: EvaluationService
    ) -> None:
        app = create_app(evaluation_service=service)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/evaluations/runs",
                json={"suite": "smoke", "case_ids": ["invalid_tool_result"]},
            )

        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"
        assert "invalid_tool_result" in resp.json()["detail"]

    async def test_metrics_suite_filter_excludes_other_suites(
        self, service: EvaluationService
    ) -> None:
        app = create_app(evaluation_service=service)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            create_resp = await client.post(
                "/evaluations/runs",
                json={"suite": "smoke", "case_ids": ["lead_ranking"]},
            )
            evaluation_run_id = create_resp.json()["evaluation_run_id"]
            await self._poll_until_terminal(client, evaluation_run_id)

            matching = await client.get(
                "/evaluations/metrics", params={"suite": "smoke", "window": "24h"}
            )
            other = await client.get("/evaluations/metrics", params={"suite": "safety"})

        matching_ids = {item["evaluation_run_id"] for item in matching.json()["items"]}
        other_ids = {item["evaluation_run_id"] for item in other.json()["items"]}
        assert evaluation_run_id in matching_ids
        assert evaluation_run_id not in other_ids
