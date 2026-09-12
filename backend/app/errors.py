"""Error taxonomy and the recovery policy that reads it.

See docs/architecture.md §10. This module is a **leaf**: it imports nothing
from the rest of the application, so every layer may depend on it.

The recoverability tables and `recovery_action` live here rather than in the
graph because recoverability is a property of an error class plus a tool
contract — not of the node that happens to observe the failure. Keeping the
decision pure makes it unit-testable without a graph, a database or a network.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorClass(StrEnum):
    """Every failure is classified into exactly one of these (§10.1)."""

    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    INPUT_VALIDATION = "input_validation"
    REFERENCE_RESOLUTION = "reference_resolution"
    NOT_FOUND = "not_found"
    STALE_WRITE = "stale_write"
    OUTPUT_VALIDATION = "output_validation"
    VERIFICATION_FAILED = "verification_failed"
    POLICY_VIOLATION = "policy_violation"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PLANNER_ERROR = "planner_error"
    INTERNAL = "internal"


class RecoveryAction(StrEnum):
    """What `recover` does about a classified failure."""

    RETRY = "retry"
    REPLAN = "replan"
    SKIP = "skip"
    FAIL = "fail"


#: Classes that are never recoverable, whatever the contract says. A policy
#: violation means an attack or a bug; an internal error means a bug. Retrying
#: either is at best pointless and at worst a second unapproved effect.
TERMINAL_ERRORS: frozenset[ErrorClass] = frozenset(
    {ErrorClass.POLICY_VIOLATION, ErrorClass.BUDGET_EXHAUSTED, ErrorClass.INTERNAL}
)

#: Retryable for any tool: the failure is about the world, not the request.
ALWAYS_RETRYABLE: frozenset[ErrorClass] = frozenset(
    {ErrorClass.TRANSIENT, ErrorClass.RATE_LIMITED, ErrorClass.PLANNER_ERROR}
)

#: Retryable only when the contract opts in:
#:   OUTPUT_VALIDATION    — only for a nondeterministic tool; re-asking a rule
#:                          engine for a different answer is superstition.
#:   VERIFICATION_FAILED  — only for an idempotent tool; invariant P5 forbids
#:                          retrying an unverified non-idempotent mutation.
CONDITIONALLY_RETRYABLE: frozenset[ErrorClass] = frozenset(
    {ErrorClass.OUTPUT_VALIDATION, ErrorClass.VERIFICATION_FAILED}
)

#: A different plan might succeed; the same call certainly will not.
REPLANNABLE: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.INPUT_VALIDATION,
        ErrorClass.REFERENCE_RESOLUTION,
        ErrorClass.NOT_FOUND,
        ErrorClass.STALE_WRITE,
        ErrorClass.OUTPUT_VALIDATION,
    }
)


class OpsPilotError(Exception):
    """Base class. Every raised failure carries its classification."""

    error_class: ErrorClass = ErrorClass.INTERNAL

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def __str__(self) -> str:
        return self.message


class TransientToolError(OpsPilotError):
    error_class = ErrorClass.TRANSIENT


class RateLimitedError(OpsPilotError):
    error_class = ErrorClass.RATE_LIMITED

    def __init__(
        self, message: str, *, retry_after_ms: int | None = None, detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message, detail=detail)
        self.retry_after_ms = retry_after_ms


class InputValidationError(OpsPilotError):
    error_class = ErrorClass.INPUT_VALIDATION


class ReferenceResolutionError(OpsPilotError):
    error_class = ErrorClass.REFERENCE_RESOLUTION


class NotFoundError(OpsPilotError):
    error_class = ErrorClass.NOT_FOUND


class StaleWriteError(OpsPilotError):
    """`expected_version` did not match. Never retried: the record moved, so
    the plan must re-read and the human must re-approve the new diff (§10.3)."""

    error_class = ErrorClass.STALE_WRITE


class OutputValidationError(OpsPilotError):
    error_class = ErrorClass.OUTPUT_VALIDATION


class VerificationFailedError(OpsPilotError):
    error_class = ErrorClass.VERIFICATION_FAILED


class PolicyViolation(OpsPilotError):
    """An unapproved mutation, a disallowed field, a recipient mismatch, or a
    forged approval token. Terminal, loud, and never retried."""

    error_class = ErrorClass.POLICY_VIOLATION


class BudgetExhaustedError(OpsPilotError):
    error_class = ErrorClass.BUDGET_EXHAUSTED


class PlannerError(OpsPilotError):
    error_class = ErrorClass.PLANNER_ERROR


class ConfigurationError(OpsPilotError):
    """Raised at startup by `Settings.validate_runtime` (§17.3). Not a run
    failure — the process refuses to serve traffic at all."""

    error_class = ErrorClass.INTERNAL


def is_retryable(
    error_class: ErrorClass, *, idempotent: bool, nondeterministic: bool
) -> bool:
    """Is this class retryable for a tool with these contract properties?"""
    if error_class in TERMINAL_ERRORS:
        return False
    if error_class in ALWAYS_RETRYABLE:
        return True
    if error_class is ErrorClass.OUTPUT_VALIDATION:
        return nondeterministic
    if error_class is ErrorClass.VERIFICATION_FAILED:
        return idempotent  # invariant P5
    return False


def recovery_action(
    error_class: ErrorClass,
    *,
    idempotent: bool,
    nondeterministic: bool,
    retries_remaining: int,
    replans_remaining: int,
    step_optional: bool,
    budget_exhausted: bool = False,
) -> RecoveryAction:
    """The `recover` node's decision, as a pure function (§10.2).

    Order is the policy: terminal classes outrank budgets, budgets outrank
    retries, and skipping an optional step is preferred to spending a replan.
    """
    if error_class in TERMINAL_ERRORS:
        return RecoveryAction.FAIL
    if budget_exhausted:
        return RecoveryAction.FAIL
    if (
        is_retryable(error_class, idempotent=idempotent, nondeterministic=nondeterministic)
        and retries_remaining > 0
    ):
        return RecoveryAction.RETRY
    if step_optional:
        return RecoveryAction.SKIP
    if error_class in REPLANNABLE and replans_remaining > 0:
        return RecoveryAction.REPLAN
    return RecoveryAction.FAIL


def backoff_delay_ms(
    attempt: int,
    *,
    base_ms: int,
    max_ms: int,
    jitter: float = 1.0,
    retry_after_ms: int | None = None,
) -> int:
    """Exponential backoff with jitter, honouring a server hint (§10.4).

    `jitter` is injected rather than sampled internally so evaluations are
    deterministic and the delay is assertable.
    """
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    delay = min(base_ms * (2 ** (attempt - 1)), max_ms)
    delay = int(delay * jitter)
    if retry_after_ms is not None:
        delay = max(delay, retry_after_ms)
    return delay
