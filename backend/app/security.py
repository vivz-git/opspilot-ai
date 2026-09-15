"""Approval binding primitives: the canonical argument hash, the token and
the gate that mints it from a persisted decision.

See docs/architecture.md §9.4-§9.5. This module is a leaf so that both the
agent (which mints tokens from stored decisions) and the integration layer
(whose mutating port methods require one) can depend on it. It therefore
never sees a database: the persistence layer runs the query and hands the
gate an `ApprovalRecordProtocol` — the handful of columns the decision
needs — and the gate answers from that record alone.

Two properties are enforced here rather than documented:

1. An approval is bound to the *arguments*, not merely to a step, so a plan
   revision cannot reuse a human's grant for different arguments.
2. `ApprovalToken` cannot be constructed by ordinary code. Only
   `ApprovalGate.issue` — reached in the application solely through
   `ApprovalGate.issue_from_persisted`, from a durable `approved` row — can
   mint one. Any other call site raises `PolicyViolation`.

What the token is, precisely: an **in-process capability**, not a signed
credential. A frozen dataclass whose constructor demands a module-private
sentinel (`_MINT`) that nothing else imports; `tests/test_structure.py`
proves no other module references the sentinel or the constructor. It is
never serialised, never persisted and never crosses a process boundary, so
there is nothing to sign — the guarantee is that code which cannot reach
the sentinel cannot produce an instance, and the only code that can is the
gate, which first checks the row. The dispatcher and the adapter still
re-check every token they are handed (§8.5, ADR-024): the token is proof of
provenance, not a substitute for the stored decision.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import InitVar, dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from app.errors import ApprovalInvalidError, ApprovalRequiredError, PolicyViolation

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


def idempotency_key_for(*, run_id: str, step_id: str, args_hash: str) -> str:
    """The attempt-invariant key under which a mutating effect is applied
    (§10.4, ADR-020).

    Derived from `(run_id, step_id, args_hash)` and nothing else, so every
    retry of a step reuses the same key and the adapter's unique constraint
    turns a second application into a replay of the first. Only the dispatcher
    derives it; a plan or a caller cannot choose one (§8.5).
    """
    return f"{run_id}:{step_id}:{args_hash}"


def _strip_volatile(value: Any) -> Any:  # noqa: ANN401 - recurses over arbitrary JSON-like data
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


#: The one status from which a token may be minted. Spelled here rather
#: than imported from `app.agent.state.ApprovalStatus` because this module
#: is a leaf (§4.1); `tests/test_security.py` pins the two to each other.
APPROVED_STATUS: Final[str] = "approved"


class ApprovalRecordProtocol(Protocol):
    """The columns of an `approvals` row (§12.6) the gate decides from.

    A structural view, so the persistence layer's `ApprovalRow` satisfies it
    without this module importing the ORM, and a unit test can hand the gate
    a plain object. Read-only on purpose: the gate never mutates approval
    state (HITL-002 bridges a decision to a capability; it does not decide).
    """

    @property
    def id(self) -> uuid.UUID: ...
    @property
    def run_id(self) -> uuid.UUID: ...
    @property
    def step_id(self) -> str: ...
    @property
    def tool(self) -> str: ...
    @property
    def status(self) -> str: ...
    @property
    def args_hash(self) -> str: ...
    @property
    def superseded_by(self) -> uuid.UUID | None: ...
    @property
    def expires_at(self) -> datetime: ...


class ApprovalGate:
    """The only place an `ApprovalToken` comes from.

    `issue_from_persisted` is the application's single issuing path (HITL-002):
    it takes the current `approved` row the repository found for the step —
    or `None` — and refuses unless every binding holds. `issue` is the
    minting primitive underneath it, kept for the tests that script a human
    decision; `tests/test_structure.py` allows it nowhere else.
    """

    @staticmethod
    def issue_from_persisted(
        record: ApprovalRecordProtocol | None,
        *,
        run_id: str,
        step_id: str,
        tool: str | None,
        args: dict[str, Any],
        now: datetime,
    ) -> ApprovalToken:
        """Mint a token for `args` from a durable approved decision, or refuse.

        Barrier 3 of §9.5. Every check fails closed and none has a fallback:
        there is no "closest" approval, no "same step, other tool", no "same
        tool, other arguments". In order —

        1. a record exists for the requested run and step, else
           `ApprovalRequiredError`;
        2. its status is `approved` (a pending, rejected, expired, superseded
           or cancelled row authorises nothing);
        3. it names this run;
        4. it names this step;
        5. it names this tool, when the caller states one;
        6. it has not been superseded (`superseded_by` is unset);
        7. its TTL has not elapsed (`now < expires_at`) — a grant is usable
           only inside the window the human was shown;
        8. its `args_hash` equals `canonical_args_hash(args)`, the hash of
           the arguments about to be sent (§9.4).

        Any failure after (1) is `ApprovalInvalidError`. Both are
        `PolicyViolation`: terminal, never retried (§10.1). `now` is the
        injected clock's reading, never wall time (§18.2).
        """
        base: dict[str, Any] = {"run_id": run_id, "step_id": step_id, "tool": tool}
        if record is None:
            raise ApprovalRequiredError(
                "no approved decision is stored for this step; human approval is required",
                detail=base,
            )
        detail = {**base, "approval_id": str(record.id)}
        mismatches: list[str] = []
        if str(record.status) != APPROVED_STATUS:
            mismatches.append(f"status={record.status}")
        if str(record.run_id) != run_id:
            mismatches.append("run_id")
        if record.step_id != step_id:
            mismatches.append("step_id")
        if tool is not None and str(record.tool) != tool:
            mismatches.append("tool")
        if record.superseded_by is not None:
            mismatches.append("superseded")
        if now >= record.expires_at:
            mismatches.append("expired")
        actual = canonical_args_hash(args)
        if record.args_hash != actual:
            mismatches.append("args_hash")
        if mismatches:
            raise ApprovalInvalidError(
                "stored approval does not authorise this call",
                detail={**detail, "mismatch": mismatches},
            )
        return ApprovalGate.issue(
            approval_id=str(record.id),
            run_id=run_id,
            step_id=step_id,
            args=args,
            approved_args_hash=record.args_hash,
            decision="approve",
        )

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
