"""Verification-failure recovery policy (§10.2, §10.3, §11.4, VERIFY-003).

`recover` already knows what to do with a *tool* failure. This module answers
the two questions verification adds to it, and answers them as pure functions
over checkpointed state so that a resumed run reaches the same conclusion the
crashed one did:

1. **Where does the retry go?** A tool that already succeeded must never be
   re-executed merely because the read-back could not be reached. The retry
   target is `verify`, not `execute_tool`.

2. **Is retrying the mutation safe at all?** `VERIFICATION_FAILED` is not one
   answer but two. "The write is simply absent" is safe to repeat for an
   idempotent tool. "The persisted state contradicts the request" — a wrong
   recipient, a duplicated outbox row, a field nobody asked to touch — is
   evidence that repeating the call would compound a real effect, and the
   error *class* cannot tell those apart. Only the verifier's own checks can,
   so this module reads them.

Nothing here performs I/O, holds state, or decides anything `recovery_action`
already decides: it narrows the inputs that function is given.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Final

from app.agent.state import (
    AgentError,
    ToolCall,
    VerificationResult,
    VerificationStatus,
)
from app.tools.contracts import ToolContract

__all__ = [
    "ERROR_SOURCE_KEY",
    "ERROR_SOURCE_VERIFY",
    "ERROR_VERIFY_ATTEMPT_KEY",
    "PROVEN_ABSENCE_CHECKS",
    "VERIFY_RETRY_SUFFIX",
    "RetryTarget",
    "VerificationSafety",
    "classify_verification_failure",
    "failed_check_names",
    "is_safe_to_retry_mutation",
    "is_verification_error",
    "last_tool_call_for",
    "retry_target_for",
    "tool_effect_succeeded",
    "unconfirmed_step_ids",
    "verification_attempt",
    "verify_retry_key",
]

#: Marks an `AgentError` as raised by the `verify` node rather than by
#: `execute_tool`. Carried in `AgentError.detail`, so it is checkpointed with
#: the error itself and survives a resume — the routing decision must never
#: depend on a process-local flag.
ERROR_SOURCE_KEY: Final = "source"
ERROR_SOURCE_VERIFY: Final = "verify"
#: The verification attempt this error belongs to, mirrored from
#: `VerificationResult.attempt` for the exactly-once retry accounting.
ERROR_VERIFY_ATTEMPT_KEY: Final = "verification_attempt"

#: `retry_count` is keyed by step id. A read-back retry consumes attempts
#: without producing a new `ToolCall`, so it needs its own key in the same
#: channel — a separate namespace rather than a second counter, so the
#: existing `merge_dict` reducer and the existing budget both still apply.
VERIFY_RETRY_SUFFIX: Final = "::verify"


def verify_retry_key(step_id: str) -> str:
    """The `retry_count` key counting read-back retries for `step_id`."""
    return f"{step_id}{VERIFY_RETRY_SUFFIX}"


class RetryTarget(StrEnum):
    """Which node a scheduled retry re-enters."""

    EXECUTE_TOOL = "execute_tool"
    VERIFY = "verify"


class VerificationSafety(StrEnum):
    """What the verifier's evidence says about repeating the mutation."""

    #: Every failed check proves the requested effect is simply absent.
    #: Repeating an idempotent call cannot duplicate what never happened.
    ABSENT = "absent"
    #: At least one failed check describes a persisted effect that
    #: contradicts the request. Repeating the call could compound it.
    CONTRADICTED = "contradicted"


#: The verifier checks whose failure is *proof of absence* rather than proof
#: of a wrong effect. Each is the "we re-read the entity and it is not there"
#: check of its read-back verifier (`app/agent/verifiers/readback.py`); those
#: verifiers return early on it, so a result carrying one carries no other
#: evidence. Every other check name — a mismatched recipient, a duplicated
#: outbox row, a tampered field, a corrupted content hash, a record in an
#: unexpected state — describes something that *did* happen, and is therefore
#: unsafe to repeat. This is deliberately an allowlist: a check added later is
#: treated as unsafe until someone decides otherwise.
PROVEN_ABSENCE_CHECKS: Final[frozenset[str]] = frozenset(
    {
        "outbox_record_exists",
        "draft_record_exists",
        "customer_record_exists",
    }
)


def failed_check_names(result: VerificationResult | None) -> tuple[str, ...]:
    """The names of the checks that did not pass, in verifier order."""
    if result is None:
        return ()
    return tuple(c.name for c in result.checks if not c.passed)


def classify_verification_failure(result: VerificationResult | None) -> VerificationSafety:
    """Read the evidence, not the error class.

    Safe only when there *is* evidence and all of it is proof of absence. A
    `failed` result with no failing check at all is not evidence of absence,
    so it is treated as contradicted.
    """
    failed = failed_check_names(result)
    if not failed:
        return VerificationSafety.CONTRADICTED
    if all(name in PROVEN_ABSENCE_CHECKS for name in failed):
        return VerificationSafety.ABSENT
    return VerificationSafety.CONTRADICTED


def is_safe_to_retry_mutation(
    result: VerificationResult | None,
    contract: ToolContract | None,
) -> bool:
    """May `execute_tool` be re-entered after this verification failure?

    Two independent conditions, both required: the contract says the call is
    safe to repeat (invariant P5), and the verifier's evidence says there is
    nothing for the repeat to land on top of.
    """
    if contract is None or not contract.idempotent:
        return False
    return classify_verification_failure(result) is VerificationSafety.ABSENT


def is_verification_error(error: AgentError | None) -> bool:
    """Did `verify` raise this, as opposed to `execute_tool`?"""
    if error is None:
        return False
    return error.detail.get(ERROR_SOURCE_KEY) == ERROR_SOURCE_VERIFY


def verification_attempt(error: AgentError | None, result: VerificationResult | None) -> int:
    """The 1-based verification attempt the latest evidence belongs to."""
    if error is not None:
        raw = error.detail.get(ERROR_VERIFY_ATTEMPT_KEY)
        if isinstance(raw, int) and raw >= 1:
            return raw
    if result is not None:
        return result.attempt
    return 1


def last_tool_call_for(calls: Sequence[ToolCall] | None, step_id: str | None) -> ToolCall | None:
    """The most recent attempt recorded for `step_id`."""
    if not calls or step_id is None:
        return None
    return next((c for c in reversed(calls) if c.step_id == step_id), None)


def tool_effect_succeeded(calls: Sequence[ToolCall] | None, step_id: str | None) -> bool:
    """Did the tool itself report success on its latest attempt?

    This is the fact that makes re-executing unsafe and re-verifying correct.
    It is read from the append-only `tool_calls` channel, so it is exactly as
    durable as the checkpoint.
    """
    last = last_tool_call_for(calls, step_id)
    return last is not None and last.status == "succeeded"


def retry_target_for(
    *,
    error: AgentError | None,
    result: VerificationResult | None,
    tool_calls: Sequence[ToolCall] | None,
    step_id: str | None,
) -> RetryTarget:
    """Where a retry for this failure must go.

    `verify` only when all three hold: the failure came from `verify`, the
    verifier could not reach a conclusion, and the tool's own attempt
    succeeded. Anything else — including a generic `TRANSIENT` error from the
    dispatcher — re-enters `execute_tool` as it always did.
    """
    if not is_verification_error(error):
        return RetryTarget.EXECUTE_TOOL
    if result is None or result.status is not VerificationStatus.UNCONFIRMED:
        return RetryTarget.EXECUTE_TOOL
    if not tool_effect_succeeded(tool_calls, step_id):
        return RetryTarget.EXECUTE_TOOL
    return RetryTarget.VERIFY


def unconfirmed_step_ids(
    results: Mapping[str, VerificationResult] | None,
    step_ids: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Steps whose effect the system could not confirm, in `step_ids` order."""
    if not results:
        return ()
    candidates = list(step_ids) if step_ids is not None else list(results)
    return tuple(
        sid
        for sid in candidates
        if (r := results.get(sid)) is not None and r.status is VerificationStatus.UNCONFIRMED
    )
