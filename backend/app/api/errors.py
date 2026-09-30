"""RFC 9457 Problem Details error translation layer (§13.1).

Translates domain errors, validation errors, and unhandled exceptions into standard
`application/problem+json` envelopes with machine-readable `code` slugs. Never leaks
internal database errors, SQL fragments, raw credentials, or stack traces.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.correlation import request_trace_id
from app.errors import (
    AccessDenied,
    ApprovalConflictError,
    ApprovalExpiredError,
    ApprovalNotPendingError,
    ApprovalSupersededError,
    BudgetExhaustedError,
    IdempotencyConflictError,
    InputValidationError,
    LeaseAcquisitionError,
    NotFoundError,
    PolicyViolation,
    RunActiveError,
    RunNotCancellableError,
    RunNotResumableError,
    RunNotStartableError,
)

__all__ = [
    "problem_details",
    "register_error_handlers",
]

_log = structlog.get_logger("opspilot.api.errors")


def problem_details(
    status: int,
    *,
    code: str,
    title: str,
    detail: str,
    instance: str,
    errors: list[Any] | None = None,
    trace_id: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build a standard RFC 9457 problem details JSONResponse."""
    content: dict[str, Any] = {
        "type": f"https://opspilot.dev/errors/{code.replace('_', '-')}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        "code": code,
        "errors": errors or [],
    }
    if trace_id is not None:
        content["trace_id"] = trace_id
    return JSONResponse(
        status_code=status,
        content=content,
        media_type="application/problem+json",
        headers=dict(headers) if headers is not None else None,
    )


def _problem(
    request: Request,
    status: int,
    *,
    code: str,
    title: str,
    detail: str,
    errors: list[Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """`problem_details` for the request being answered: `instance` is its
    path and `trace_id` the correlation id it was served under (§13.1)."""
    return problem_details(
        status,
        code=code,
        title=title,
        detail=detail,
        instance=request.url.path,
        errors=errors,
        trace_id=request_trace_id(request),
        headers=headers,
    )


#: Framework-level refusals (no route, wrong method) in the §13.1 envelope.
_HTTP_ERROR_CODES: Final[dict[int, tuple[str, str]]] = {
    404: ("not_found", "Not found"),
    405: ("method_not_allowed", "Method not allowed"),
}


def register_error_handlers(app: FastAPI) -> None:
    """Register RFC 9457 exception handlers on the FastAPI application."""

    @app.exception_handler(NotFoundError)
    async def handle_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return _problem(
            request,
            404,
            code="not_found",
            title="Not found",
            detail=str(exc),
        )

    @app.exception_handler(ApprovalExpiredError)
    async def handle_approval_expired(request: Request, exc: ApprovalExpiredError) -> JSONResponse:
        return _problem(
            request,
            409,
            code="approval_expired",
            title="Approval expired",
            detail=str(exc),
        )

    @app.exception_handler(ApprovalSupersededError)
    async def handle_approval_superseded(
        request: Request, exc: ApprovalSupersededError
    ) -> JSONResponse:
        return _problem(
            request,
            409,
            code="approval_superseded",
            title="Approval superseded",
            detail=str(exc),
        )

    @app.exception_handler(ApprovalNotPendingError)
    async def handle_approval_not_pending(
        request: Request, exc: ApprovalNotPendingError
    ) -> JSONResponse:
        return _problem(
            request,
            409,
            code="approval_not_pending",
            title="Approval is not pending",
            detail=str(exc),
        )

    @app.exception_handler(ApprovalConflictError)
    async def handle_approval_conflict(
        request: Request, exc: ApprovalConflictError
    ) -> JSONResponse:
        return _problem(
            request,
            409,
            code="approval_not_pending",
            title="Approval is not pending",
            detail=str(exc),
        )

    @app.exception_handler(AccessDenied)
    async def handle_access_denied(request: Request, exc: AccessDenied) -> JSONResponse:
        """§16.6 — the request never reached the access boundary. Starlette
        resolves handlers along the exception's MRO, so this wins over the
        `PolicyViolation` handler below."""
        return _problem(
            request,
            401,
            code="policy_violation",
            title="Unauthorized",
            detail=str(exc),
        )

    @app.exception_handler(PolicyViolation)
    async def handle_policy_violation(request: Request, exc: PolicyViolation) -> JSONResponse:
        msg = str(exc)
        # Stale or mismatched args_hash maps to approval_superseded per §13.5
        if "args_hash" in msg or "mismatch" in msg:
            return _problem(
                request,
                409,
                code="approval_superseded",
                title="Approval superseded",
                detail=msg,
            )
        return _problem(
            request,
            409,
            code="policy_violation",
            title="Policy violation",
            detail=msg,
        )

    @app.exception_handler(RunNotResumableError)
    async def handle_run_not_resumable(request: Request, exc: RunNotResumableError) -> JSONResponse:
        return _problem(
            request,
            409,
            code="run_not_resumable",
            title="Run not resumable",
            detail=str(exc),
        )

    @app.exception_handler(LeaseAcquisitionError)
    async def handle_lease_acquisition_error(
        request: Request, exc: LeaseAcquisitionError
    ) -> JSONResponse:
        return _problem(
            request,
            409,
            code="run_not_resumable",
            title="Run not resumable",
            detail=str(exc),
        )

    @app.exception_handler(IdempotencyConflictError)
    async def handle_idempotency_conflict(
        request: Request, exc: IdempotencyConflictError
    ) -> JSONResponse:
        return _problem(
            request,
            409,
            code="idempotency_conflict",
            title="Idempotency conflict",
            detail=str(exc),
        )

    @app.exception_handler(RunNotStartableError)
    async def handle_run_not_startable(request: Request, exc: RunNotStartableError) -> JSONResponse:
        return _problem(
            request,
            409,
            code="run_not_startable",
            title="Run not startable",
            detail=str(exc),
        )

    @app.exception_handler(RunNotCancellableError)
    async def handle_run_not_cancellable(
        request: Request, exc: RunNotCancellableError
    ) -> JSONResponse:
        return _problem(
            request,
            409,
            code="run_not_cancellable",
            title="Run not cancellable",
            detail=str(exc),
        )

    @app.exception_handler(RunActiveError)
    async def handle_run_active(request: Request, exc: RunActiveError) -> JSONResponse:
        return _problem(
            request,
            409,
            code="run_active",
            title="Run active",
            detail=str(exc),
        )

    @app.exception_handler(BudgetExhaustedError)
    async def handle_budget_exhausted(request: Request, exc: BudgetExhaustedError) -> JSONResponse:
        return _problem(
            request,
            409,
            code="budget_exhausted",
            title="Budget exhausted",
            detail=str(exc),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Unknown routes and wrong methods, raised by the router itself
        rather than by a route — still a non-2xx response, so still §13.1's
        envelope, with any headers the framework attached (`Allow` on 405)."""
        code, title = _HTTP_ERROR_CODES.get(exc.status_code, ("http_error", "HTTP error"))
        return _problem(
            request,
            exc.status_code,
            code=code,
            title=title,
            detail=str(exc.detail),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        raw_errors = exc.errors()
        # Convert Pydantic error details to JSON-serializable structures
        clean_errors: list[dict[str, Any]] = []
        for err in raw_errors:
            clean_err: dict[str, Any] = {
                "loc": list(err.get("loc", [])),
                "msg": str(err.get("msg", "")),
                "type": str(err.get("type", "")),
            }
            clean_errors.append(clean_err)
        return _problem(
            request,
            422,
            code="validation_error",
            title="Validation error",
            detail="Request body or query parameters failed schema validation",
            errors=clean_errors,
        )

    @app.exception_handler(InputValidationError)
    async def handle_input_validation(request: Request, exc: InputValidationError) -> JSONResponse:
        return _problem(
            request,
            422,
            code="validation_error",
            title="Validation error",
            detail=str(exc),
            errors=[{"msg": str(exc)}],
        )

    @app.exception_handler(Exception)
    async def handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        # Bound explicitly: this handler runs in Starlette's outermost
        # middleware, after the correlation middleware's context has unwound.
        _log.exception(
            "unhandled_api_error",
            path=request.url.path,
            error=str(exc),
            trace_id=request_trace_id(request),
        )
        return _problem(
            request,
            500,
            code="internal_error",
            title="Internal server error",
            detail="An unexpected internal error occurred",
        )
