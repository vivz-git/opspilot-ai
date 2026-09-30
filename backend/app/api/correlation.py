"""Per-request correlation: the `trace_id` every error response echoes (§13.1, §14.6, §16.5).

`RequestCorrelationMiddleware` mints one id per HTTP request, before any
route, dependency or exception handler runs, and makes it available two
ways:

* on the request (`request_trace_id(request)`), so every problem+json
  response — domain errors, validation errors, unknown routes, the generic
  500 — carries the same `trace_id` the request was served under;
* in structlog's context variables for the lifetime of the request, so every
  log line written while serving it carries that id too. That is the whole
  point: an operator who reports "I got a 409 with trace_id X" can be
  matched to the server's log lines for that request.

It is a **request** correlation id, nothing more. It is not a run id, not an
approval id and not a trace-event `seq` — the product trace (`trace_events`)
is keyed by `run_id` and is unrelated to it. It is minted server-side from
the injected `IdGenerator` and never read from a client header, so a caller
cannot choose what lands in the logs under it.

A pure ASGI middleware rather than a `BaseHTTPMiddleware`: it does not buffer
bodies, so the SSE stream passes through untouched, and it writes the id into
the request's `scope["state"]` in place — the same dict Starlette's outermost
`ServerErrorMiddleware` reads when it runs the catch-all 500 handler, which
sits *outside* every user middleware.
"""

from __future__ import annotations

from typing import Final

import structlog
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.runtime import IdGenerator, UuidIdGenerator

__all__ = ["TRACE_ID_KEY", "RequestCorrelationMiddleware", "request_trace_id"]

#: The key under which the id lives in `request.state` and in log lines —
#: and the field name §13.1's error envelope uses.
TRACE_ID_KEY: Final = "trace_id"


class RequestCorrelationMiddleware:
    """Mint a `trace_id` per HTTP request (§13.1)."""

    def __init__(self, app: ASGIApp, *, ids: IdGenerator | None = None) -> None:
        self._app = app
        self._ids = ids or UuidIdGenerator()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        trace_id = self._ids.new_id()
        scope.setdefault("state", {})[TRACE_ID_KEY] = trace_id
        with structlog.contextvars.bound_contextvars(**{TRACE_ID_KEY: trace_id}):
            await self._app(scope, receive, send)


def request_trace_id(request: Request) -> str | None:
    """The correlation id minted for `request`, or `None` outside the middleware."""
    value = request.scope.get("state", {}).get(TRACE_ID_KEY)
    return value if isinstance(value, str) else None
