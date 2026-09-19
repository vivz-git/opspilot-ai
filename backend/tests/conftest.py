"""Shared test configuration.

Windows only: psycopg's async connection (which the LangGraph Postgres saver
uses, DB-007) refuses asyncio's default `ProactorEventLoop`, so the suite
runs on a selector loop there. Linux and macOS — CI and the Docker image —
already use a selector loop and are unaffected. asyncpg works on either.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ---------------------------------------------------------------------------
# TEST-006 / §18.4: the six critical tests must never disappear into an
# ordinary skip. Every other integration test may still skip cleanly with no
# reachable Postgres (§18.2) — this only watches tests carrying `critical`.
# ---------------------------------------------------------------------------
_skipped_critical: set[str] = set()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.skipped and "critical" in report.keywords:
        _skipped_critical.add(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _skipped_critical:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep(
            "=", "critical §18.4 test(s) skipped — treated as a failure (TEST-006)", red=True
        )
        for nodeid in sorted(_skipped_critical):
            reporter.write_line(f"  SKIPPED (critical): {nodeid}", red=True)
    session.exitstatus = 1
