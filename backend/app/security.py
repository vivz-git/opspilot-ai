"""Approval binding primitives: the canonical argument hash and the token.

See docs/architecture.md §9.4-§9.5. This module is a leaf so that both the
agent (which mints tokens from stored decisions) and the integration layer
(whose mutating port methods require one) can depend on it.

Two properties are enforced here rather than documented:

1. An approval is bound to the *arguments*, not merely to a step, so a plan
   revision cannot reuse a human's grant for different arguments.
2. `ApprovalToken` cannot be constructed by ordinary code. Only
   `ApprovalGate.issue` — which is only reachable from a persisted, approved
   decision — can mint one. Any other call site raises `PolicyViolation`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import InitVar, dataclass
from typing import Any, Final

from app.errors import PolicyViolation

#: Excluded from the hash because they legitimately differ between attempts or
#: are themselves the authorisation. Including them would make every retry look
#: like a different operation and invalidate the grant it already holds.
VOLATILE_ARG_KEYS: Final[frozenset[str]] = frozenset(
    {"idempotency_key", "approval_token", "requested_at", "trace_id"}
)


def canonical_args_hash(args: dict[str, Any]) -> str:
    """Stable hash of the arguments an approval authorises.

    Canonicalisation sorts keys recursively and drops volatile keys, so the
    hash depends only on the operation's meaning. Any change to a meaningful
    argument produces a different hash and therefore invalidates the grant.
    """
    canonical = json.dumps(
        _strip_volatile(args),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_volatile(v) for k, v in value.items() if k not in VOLATILE_ARG_KEYS}
    if isinstance(value, (list, tuple)):
        return [_strip_volatile(v) for v in value]
    return value


_MINT: Final[object] = object()


@dataclass(frozen=True)
class ApprovalToken:
    """Proof that a human approved *these exact arguments*.

    Required in the signature of every mutating port method, so a real adapter
    written years from now inherits the guarantee: code that cannot obtain a
    token cannot perform the effect.

    `mint` is an `InitVar`, so the sentinel is never stored on the instance and
    can never leak into a trace, a log or an API response.
    """

    approval_id: str
    run_id: str
    step_id: str
    args_hash: str
    mint: InitVar[object | None] = None

    def __post_init__(self, mint: object | None) -> None:
        if mint is not _MINT:
            raise PolicyViolation(
                "ApprovalToken must be minted by ApprovalGate.issue() from a "
                "persisted approved decision",
                detail={"step_id": self.step_id},
            )

    def authorises(self, *, run_id: str, step_id: str, args: dict[str, Any]) -> bool:
        """Does this token authorise this call, right now, as composed?

        Re-checked inside the adapter (barrier 3 of §9.5): the token is not
        merely presented, it is matched against the payload being sent.
        """
        return (
            self.run_id == run_id
            and self.step_id == step_id
            and self.args_hash == canonical_args_hash(args)
        )


class ApprovalGate:
    """The only place an `ApprovalToken` comes from.

    The real implementation loads the approval row and refuses unless it is
    `approved` and its `args_hash` matches the arguments about to be sent. The
    signature is fixed here because it is a security boundary; the persistence
    lookup is HITL-002.
    """

    @staticmethod
    def issue(
        *,
        approval_id: str,
        run_id: str,
        step_id: str,
        args: dict[str, Any],
        approved_args_hash: str,
        decision: str,
    ) -> ApprovalToken:
        if decision != "approve":
            raise PolicyViolation(
                "cannot issue an approval token for a non-approval decision",
                detail={"step_id": step_id, "decision": decision},
            )
        actual = canonical_args_hash(args)
        if actual != approved_args_hash:
            # The time-of-check/time-of-use gap, closed (§9.4).
            raise PolicyViolation(
                "arguments changed since approval; a new approval is required",
                detail={"step_id": step_id, "approved": approved_args_hash, "actual": actual},
            )
        return ApprovalToken(
            approval_id=approval_id,
            run_id=run_id,
            step_id=step_id,
            args_hash=actual,
            mint=_MINT,
        )
