"""Shared test configuration.

Windows only: psycopg's async connection (which the LangGraph Postgres saver
uses, DB-007) refuses asyncio's default `ProactorEventLoop`, so the suite
runs on a selector loop there. Linux and macOS — CI and the Docker image —
already use a selector loop and are unaffected. asyncpg works on either.
"""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
