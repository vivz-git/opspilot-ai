"""FastAPI app factory, structured logging and health endpoints (§13.7, §17;
FOUND-001)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from app.api.health import check_readiness, discover_alembic_head, get_alembic_head, get_db_engine
from app.config import Settings
from app.main import BACKEND_ROOT, create_app
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, ProgrammingError

pytestmark = [pytest.mark.unit]


def settings(**overrides: object) -> Settings:
    """Construct settings without reading the developer's .env."""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# --- Fakes for the readiness probe — no real Postgres involved -------------


class _FakeResult:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> str | None:
        return self._value


class _FakeConnection:
    def __init__(
        self,
        *,
        connect_error: Exception | None = None,
        execute_error: Exception | None = None,
        version: str | None = None,
    ) -> None:
        self._connect_error = connect_error
        self._execute_error = execute_error
        self._version = version

    async def __aenter__(self) -> _FakeConnection:
        if self._connect_error is not None:
            raise self._connect_error
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, _stmt: object) -> _FakeResult:
        if self._execute_error is not None:
            raise self._execute_error
        return _FakeResult(self._version)


class _FakeEngine:
    def __init__(self, **kwargs: Any) -> None:
        self._connection = _FakeConnection(**kwargs)

    def connect(self) -> _FakeConnection:
        return self._connection


def _connection_refused() -> OperationalError:
    return OperationalError("connection refused", {}, Exception("refused"))


def _table_missing() -> ProgrammingError:
    return ProgrammingError('relation "alembic_version" does not exist', {}, Exception("missing"))


class TestDiscoverAlembicHead:
    def test_returns_none_when_alembic_is_not_wired_up(self, tmp_path: Any) -> None:
        """No `alembic.ini` at all (e.g. a repo checkout before FOUND-003):
        readiness falls back to a pure connectivity check."""
        assert discover_alembic_head(tmp_path) is None

    def test_the_real_repository_reports_its_actual_head(self) -> None:
        """FOUND-003 added alembic.ini and the first revision; this asserts
        readiness now enforces a real head instead of falling back. Not
        pinned to a specific revision id, so it survives future migrations."""
        head = discover_alembic_head(BACKEND_ROOT)
        assert isinstance(head, str)
        assert head != ""


class TestCheckReadiness:
    async def test_ready_when_reachable_and_no_migration_is_expected_yet(self) -> None:
        engine = _FakeEngine(execute_error=_table_missing())
        result = await check_readiness(engine, expected_head=None)  # type: ignore[arg-type]
        assert result.ready is True
        assert result.current_revision is None

    async def test_not_ready_when_the_database_is_down(self) -> None:
        engine = _FakeEngine(connect_error=_connection_refused())
        result = await check_readiness(engine, expected_head=None)  # type: ignore[arg-type]
        assert result.ready is False
        assert result.reason == "database_unreachable"

    async def test_not_ready_when_migrations_are_behind(self) -> None:
        engine = _FakeEngine(version="0001_initial")
        result = await check_readiness(engine, expected_head="0002_head")  # type: ignore[arg-type]
        assert result.ready is False
        assert result.reason == "migrations_pending"
        assert result.current_revision == "0001_initial"

    async def test_ready_when_at_the_expected_head(self) -> None:
        engine = _FakeEngine(version="0002_head")
        result = await check_readiness(engine, expected_head="0002_head")  # type: ignore[arg-type]
        assert result.ready is True


class TestHealthEndpoints:
    def test_healthz_is_always_ok(self) -> None:
        app = create_app(settings=settings())
        with TestClient(app) as client:
            resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_readyz_returns_503_when_the_database_is_down(self) -> None:
        app = create_app(settings=settings())
        app.dependency_overrides[get_db_engine] = lambda: _FakeEngine(
            connect_error=_connection_refused()
        )
        app.dependency_overrides[get_alembic_head] = lambda: None
        with TestClient(app) as client:
            resp = client.get("/readyz")
        assert resp.status_code == 503
        assert resp.headers["content-type"].startswith("application/problem+json")
        assert resp.json()["code"] == "integration_unavailable"

    def test_readyz_returns_503_when_migrations_are_behind(self) -> None:
        app = create_app(settings=settings())
        app.dependency_overrides[get_db_engine] = lambda: _FakeEngine(version="old")
        app.dependency_overrides[get_alembic_head] = lambda: "new"
        with TestClient(app) as client:
            resp = client.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["code"] == "integration_unavailable"

    def test_readyz_returns_200_when_the_database_is_reachable(self) -> None:
        app = create_app(settings=settings())
        app.dependency_overrides[get_db_engine] = lambda: _FakeEngine(
            execute_error=_table_missing()
        )
        app.dependency_overrides[get_alembic_head] = lambda: None
        with TestClient(app) as client:
            resp = client.get("/readyz")
        assert resp.status_code == 200


class TestStartupLogging:
    def test_startup_logs_safe_settings_with_no_secret(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        secret = "sk-ant-test-not-a-real-credential"  # noqa: S105 - test fixture, not a real key
        app = create_app(settings=settings(ANTHROPIC_API_KEY=secret))
        with TestClient(app):
            pass
        out = capsys.readouterr().out
        assert secret not in out

        events = []
        for line in out.splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        startup_events = [e for e in events if e.get("event") == "startup"]
        assert startup_events, f"expected a structured startup log line, got: {out!r}"
        payload = startup_events[0]
        assert "anthropic_api_key" not in payload
        assert "database_url" not in payload
        assert "postgres_password" not in payload


class TestCorsFromConfiguration:
    def test_allowed_origin_is_reflected(self) -> None:
        app = create_app(settings=settings(CORS_ALLOW_ORIGINS="https://ops.example.com"))
        with TestClient(app) as client:
            resp = client.get("/healthz", headers={"Origin": "https://ops.example.com"})
        assert resp.headers.get("access-control-allow-origin") == "https://ops.example.com"

    def test_disallowed_origin_is_not_reflected(self) -> None:
        app = create_app(settings=settings(CORS_ALLOW_ORIGINS="https://ops.example.com"))
        with TestClient(app) as client:
            resp = client.get("/healthz", headers={"Origin": "https://evil.example.com"})
        assert "access-control-allow-origin" not in resp.headers
