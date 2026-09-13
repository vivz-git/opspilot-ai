"""structlog JSON logging (§14.6).

Every log line is one JSON object on stdout. Binding `run_id`/`step_id`/
`node`/`tool`/`attempt` via `contextvars` at node boundaries is OBS-003's
job; this module only sets up the renderer, so any `structlog.get_logger()`
call anywhere in the app produces a structured line from day one.
"""

from __future__ import annotations

import logging
import sys

import structlog

from app.config import Settings


def configure_logging(settings: Settings) -> None:
    """Configure structlog once, at process startup."""
    level = getattr(logging, settings.log_level, logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
