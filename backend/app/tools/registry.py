"""The single tool dispatch choke point (§8.5, TOOL-002).

Every tool execution in OpsPilot passes through `ToolRegistry.dispatch`. It is
the one place where input validation, the approval gate's re-assertion
(barrier 2 of §9.5), the idempotency key, the timeout, output validation and
the `tool_calls` / `tool_*` trace records are applied — so there is exactly
one place any of them can be forgotten, and `tests/test_structure.py` proves
that no node, service or endpoint reaches a tool implementation or a
mutating port around it.

    dispatch(run_id, execution_step_id, step_id, tool_name, arguments, attempt,
             approval_token=None)
        │
        ├─ resolve      unknown tool → UnknownToolError, before any I/O
        ├─ hygiene      the plan may not supply `idempotency_key` or `approval_token`
        ├─ hash/key     args_hash = canonical_args_hash(arguments)   (§9.4)
        │               idempotency_key = f(run_id, step_id, args_hash)   (§10.4)
        ├─ gate         gated tool: token required and bound to (run, step, hash)
        │               ungated tool: a token is a bug and is refused
        ├─ validate     contract.input_model, extra="forbid"
        ├─ bind         implementation + the *declared* port, nothing else
        ├─ step check   the execution_steps row is this run, this step, this tool
        ├─ decision     gated tool: the approval row is `approved`, same run,
        │               step, tool, hash and risk    (barrier "stored decision")
        ├─ tool_started trace, committed before anything executes
        ├─ lock         mutating tool: per-key advisory lock for the attempt
        ├─ execute      under contract.timeout_ms, through the port
        ├─ validate     contract.output_model
        └─ record       exactly one tool_calls row + the closing tool_* event

Layering (§4.1): this module is the capability layer's only writer of
control-plane rows, and it writes them solely through the repository
protocols of `app.persistence.protocols` — never a session, never a query.

**Outcomes and errors.** A successful or replayed attempt returns a
`DispatchResult`. Everything else raises the classified `OpsPilotError` the
recovery policy (§10.2) reads, after the attempt has been recorded:
rejections before execution (`UnknownToolError`, `InputValidationError`,
`ApprovalRequiredError`, `ApprovalInvalidError`, `PolicyViolation`,
`ToolNotBoundError`), execution failures (whatever the tool raised,
`ToolTimeoutError`, `OutputValidationError`, `InternalError`) and
`DuplicateAttemptError` when the `(step, attempt)` slot is already taken.
Every raised error carries `detail["dispatch"]` with the recorded outcome so
the caller can build its state records without a second lookup.

**Idempotency.** The key is derived here and only here (ADR-020). Mutating
attempts run under a transaction-scoped advisory lock on the key, so a
concurrent duplicate waits for the winner and is then recorded as
`duplicate_suppressed` rather than as a second `succeeded`. The lock is
bookkeeping; the adapter's unique constraint on the key is the effect-level
protection (§8.3). Holding the lock costs one pooled connection per waiting
dispatcher plus one for the executing adapter, which is fine for the
realistic contention (a double resume, a worker racing the reconciler) and is
why nothing here fans out.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError

from app.agent.state import ApprovalStatus
from app.errors import (
    ConfigurationError,
    ErrorClass,
    InputValidationError,
    InternalError,
    OpsPilotError,
    OutputValidationError,
    PolicyViolation,
    TransientToolError,
)
from app.integrations.ports import PORT_FIELDS, Adapters
from app.observability.redaction import redact_payload
from app.persistence.models import ToolCallStatus, TraceEventKind, TraceEventSeverity
from app.persistence.protocols import UnitOfWork, UnitOfWorkFactory
from app.runtime import Clock
from app.security import ApprovalToken, canonical_args_hash, idempotency_key_for
from app.tools.contracts import REGISTRY, ToolContract, ToolName, policy_violations

__all__ = [
    "DISPATCHER_OWNED_KEYS",
    "ApprovalInvalidError",
    "ApprovalRequiredError",
    "DispatchOutcome",
    "DispatchResult",
    "DuplicateAttemptError",
    "ToolContext",
    "ToolImplementation",
    "ToolNotBoundError",
    "ToolRegistry",
    "ToolTimeoutError",
    "UnknownToolError",
    "default_implementations",
]

_log = structlog.get_logger("opspilot.tools.dispatch")

#: Fields the dispatcher injects into a gated tool's input. A plan or a caller
#: that supplies them is either confused (`idempotency_key` — a planning
#: fault, replannable) or attempting to bypass the gate (`approval_token` — a
#: policy violation, terminal).
DISPATCHER_OWNED_KEYS: Final[frozenset[str]] = frozenset({"idempotency_key", "approval_token"})

#: The node that dispatches (§8.5). Recorded on every tool event so the
#: timeline groups attempts under it (§14.3).
_DISPATCH_NODE: Final[str] = "execute_tool"


# ---------------------------------------------------------------------------
# Errors specific to dispatch — each a member of the taxonomy (§10.1)
# ---------------------------------------------------------------------------
class UnknownToolError(InputValidationError):
    """The name is not in the registry. Raised before any I/O: an unknown
    tool is not an attempt of anything, so nothing is recorded (§16.3)."""


class ApprovalRequiredError(PolicyViolation):
    """A gated tool was dispatched with no grant (barrier 2 of §9.5)."""


class ApprovalInvalidError(PolicyViolation):
    """A token was presented but does not authorise *this* call: wrong run,
    step, tool or arguments, or its stored decision is not `approved`."""


class ToolNotBoundError(InternalError):
    """The contract exists but no implementation is registered for it. Fails
    closed: a missing capability is never "just executed" some other way."""


class ToolTimeoutError(TransientToolError):
    """`contract.timeout_ms` elapsed. Transient (§10.1); recorded as
    `timeout`, not `failed`, so the failure mix stays honest."""


class DuplicateAttemptError(InternalError):
    """A `tool_calls` row already exists for this `(execution_step, attempt)`.
    Two drivers are executing the same attempt — the leases should make that
    impossible — so nothing executes and nothing is recorded."""


# ---------------------------------------------------------------------------
# Public shapes
# ---------------------------------------------------------------------------
class DispatchOutcome(StrEnum):
    """What happened to one attempt. Finer than `ToolCallStatus` because the
    row's enum is fixed by the schema (§12.5): a rejection is persisted as
    `failed` with its `error_class`, and this is what distinguishes it."""

    SUCCEEDED = "succeeded"
    DUPLICATE_SUPPRESSED = "duplicate_suppressed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    REJECTED = "rejected"  # refused before the port was reached
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ToolContext:
    """What an implementation may know and reach. Constructed only here —
    `tests/test_structure.py` enforces it — so an implementation cannot be
    invoked from anywhere but `dispatch`."""

    run_id: uuid.UUID
    execution_step_id: uuid.UUID
    step_id: str
    tool: ToolName
    attempt: int
    args_hash: str
    idempotency_key: str | None
    #: Exactly the port the contract declares (`Adapters.port`), or `None`
    #: for a pure tool. Narrow with `isinstance(ctx.port, MailPort)`.
    port: object | None
    clock: Clock


#: `(validated_input, context) -> output_model instance`. The input is the
#: contract's `input_model`, already validated; the return value is validated
#: against `output_model` again by the dispatcher, whatever the implementation
#: claims.
ToolImplementation = Callable[[Any, ToolContext], Awaitable[BaseModel]]


@dataclass(frozen=True)
class DispatchResult:
    """A successful or replayed attempt. `output` is the validated,
    unredacted output the artifact store (`tool_results`) keeps for `$ref`;
    what was persisted on the row is its redacted, size-bounded copy."""

    tool: ToolName
    tool_version: str
    step_id: str
    attempt: int
    outcome: DispatchOutcome
    args_hash: str
    idempotency_key: str | None
    output: BaseModel
    output_data: dict[str, Any]
    tool_call_id: uuid.UUID
    port: str | None
    adapter: str | None
    started_at: datetime
    finished_at: datetime
    duration_ms: int


# ---------------------------------------------------------------------------
# Internal bookkeeping for one attempt
# ---------------------------------------------------------------------------
@dataclass
class _Attempt:
    contract: ToolContract
    run_id: uuid.UUID
    execution_step_id: uuid.UUID
    step_id: str
    attempt: int
    arguments: dict[str, Any]
    args_hash: str
    idempotency_key: str | None
    started_at: datetime
    token: ApprovalToken | None
    validated: BaseModel | None = None
    replay: bool = False
    persisted_input: dict[str, Any] = field(default_factory=dict)

    @property
    def tool(self) -> ToolName:
        return self.contract.name


def default_implementations() -> dict[ToolName, ToolImplementation]:
    """Return the default concrete tool implementations (§8.4, TOOL-003)."""
    from app.tools.impl import TOOL_IMPLEMENTATIONS

    return dict(TOOL_IMPLEMENTATIONS)


class ToolRegistry:
    """Contracts, implementations and the one `dispatch` (§8.5)."""

    def __init__(
        self,
        *,
        adapters: Adapters,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        implementations: Mapping[ToolName, ToolImplementation] | None = None,
        adapter_name: str = "mock",
        contracts: Mapping[ToolName, ToolContract] = REGISTRY,
        trace_payload_max_bytes: int = 16_384,
    ) -> None:
        if implementations is None:
            implementations = default_implementations()
        problems: list[str] = []
        for name, contract in contracts.items():
            if contract.name is not name:
                problems.append(f"{name}: registered under a different name")
            violated = policy_violations(contract)
            if violated:
                problems.append(f"{name}: violates policy invariants {violated}")
            if contract.port is not None and contract.port not in PORT_FIELDS:
                problems.append(f"{name}: declares unsupported port {contract.port!r}")
        unknown = set(implementations) - set(contracts)
        if unknown:
            problems.append(f"implementations without a contract: {sorted(unknown)}")
        if problems:
            # Fail closed at construction: a registry that would have to
            # guess at execution time never comes into existence.
            raise ConfigurationError("tool registry refused", detail={"problems": sorted(problems)})
        self._adapters = adapters
        self._uow_factory = uow_factory
        self._clock = clock
        self._implementations = dict(implementations)
        self._adapter_name = adapter_name
        self._contracts = dict(contracts)
        self._payload_max_bytes = trace_payload_max_bytes

    # -- policy questions (what `decide` asks) --------------------------------
    def contract(self, name: ToolName | str) -> ToolContract:
        try:
            key = ToolName(name)
        except ValueError:
            raise UnknownToolError(f"unknown tool {name!r}", detail={"tool": str(name)}) from None
        try:
            return self._contracts[key]
        except KeyError:
            raise UnknownToolError(
                f"tool {key.value!r} is not registered", detail={"tool": key.value}
            ) from None

    def is_bound(self, name: ToolName) -> bool:
        return name in self._implementations

    # -- the choke point ------------------------------------------------------
    async def dispatch(
        self,
        *,
        run_id: uuid.UUID,
        execution_step_id: uuid.UUID,
        step_id: str,
        tool_name: ToolName | str,
        arguments: Mapping[str, Any],
        attempt: int,
        approval_token: ApprovalToken | None = None,
    ) -> DispatchResult:
        """Execute one attempt of one step. See the module docstring."""
        contract = self.contract(tool_name)
        if attempt < 1:
            raise InternalError("attempt is 1-based", detail={"attempt": attempt})
        raw = dict(arguments)
        args_hash = canonical_args_hash(raw)
        key = (
            idempotency_key_for(run_id=str(run_id), step_id=step_id, args_hash=args_hash)
            if contract.is_mutating
            else None
        )
        a = _Attempt(
            contract=contract,
            run_id=run_id,
            execution_step_id=execution_step_id,
            step_id=step_id,
            attempt=attempt,
            arguments=raw,
            args_hash=args_hash,
            idempotency_key=key,
            started_at=self._clock.now(),
            token=approval_token,
        )
        a.persisted_input = self._redact(raw)

        # Stage 1 — pure checks, no I/O. A failure here is a rejection: it is
        # recorded as an attempt that never reached the port, then raised.
        try:
            self._check_argument_hygiene(a)
            self._assert_gate(a)
            a.validated = self._validate_input(a)
            a.persisted_input = self._redact(
                a.validated.model_dump(mode="json", exclude={"approval_token"})
            )
            impl = self._bind(a)
        except OpsPilotError as exc:
            await self._reject(a, exc)
            raise

        # Stage 2 — the stored decision and the start of the attempt, durable
        # before anything executes.
        async with self._uow_factory() as uow:
            await self._check_step_identity(uow, a)
            await self._refuse_taken_attempt(uow, a)
            await self._trace_started(uow, a)
            if contract.requires_approval:
                try:
                    await self._verify_stored_decision(uow, a)
                except PolicyViolation as exc:
                    await self._record(uow, a, outcome=DispatchOutcome.REJECTED, error=exc)
                    await uow.commit()
                    raise
            await uow.commit()

        # Stage 3 — execute and record, under the effect lock for mutations.
        return await self._execute_and_record(a, impl)

    # -- stage 1: pure checks -------------------------------------------------
    @staticmethod
    def _check_argument_hygiene(a: _Attempt) -> None:
        if "approval_token" in a.arguments:
            raise PolicyViolation(
                "arguments may not carry an approval token; authorisation is "
                "presented to the dispatcher, never planned",
                detail={"tool": a.tool.value, "step_id": a.step_id},
            )
        if "idempotency_key" in a.arguments:
            raise InputValidationError(
                "arguments may not carry an idempotency key; the dispatcher derives it",
                detail={"tool": a.tool.value, "step_id": a.step_id},
            )

    @staticmethod
    def _assert_gate(a: _Attempt) -> None:
        """Barrier 2 of §9.5: independent of the router, independent of the
        adapter. A token is matched against the payload being sent, never
        merely presented."""
        token = a.token
        if not a.contract.requires_approval:
            if token is not None:
                raise PolicyViolation(
                    "an approval token was presented for a tool that is not gated",
                    detail={"tool": a.tool.value, "step_id": a.step_id},
                )
            return
        if token is None:
            raise ApprovalRequiredError(
                f"{a.tool.value} requires human approval and none was granted",
                detail={"tool": a.tool.value, "step_id": a.step_id, "args_hash": a.args_hash},
            )
        mismatches = []
        if token.run_id != str(a.run_id):
            mismatches.append("run_id")
        if token.step_id != a.step_id:
            mismatches.append("step_id")
        if token.args_hash != a.args_hash:
            mismatches.append("args_hash")
        if mismatches or not token.authorises(
            run_id=str(a.run_id), step_id=a.step_id, args=a.arguments
        ):
            raise ApprovalInvalidError(
                "approval token does not authorise this call",
                detail={
                    "tool": a.tool.value,
                    "step_id": a.step_id,
                    "approval_id": token.approval_id,
                    "mismatch": mismatches or ["args_hash"],
                },
            )

    @staticmethod
    def _validate_input(a: _Attempt) -> BaseModel:
        candidate = dict(a.arguments)
        fields = a.contract.input_model.model_fields
        if "idempotency_key" in fields:
            if a.idempotency_key is None:  # pragma: no cover - P4 makes this unreachable
                raise InternalError("gated tool without an idempotency key")
            candidate["idempotency_key"] = a.idempotency_key
        if "approval_token" in fields:
            candidate["approval_token"] = a.token
        try:
            return a.contract.input_model.model_validate(candidate)
        except ValidationError as exc:
            raise InputValidationError(
                f"{a.tool.value} arguments failed the input schema",
                detail={
                    "tool": a.tool.value,
                    "step_id": a.step_id,
                    "errors": [
                        {"loc": list(map(str, e["loc"])), "msg": e["msg"], "type": e["type"]}
                        for e in exc.errors(include_url=False, include_input=False)
                    ],
                },
            ) from exc

    def _bind(self, a: _Attempt) -> ToolImplementation:
        impl = self._implementations.get(a.tool)
        if impl is None:
            raise ToolNotBoundError(
                f"no implementation is registered for {a.tool.value}",
                detail={"tool": a.tool.value, "step_id": a.step_id},
            )
        if a.contract.port is not None:
            try:
                self._adapters.port(a.contract.port)
            except KeyError:  # pragma: no cover - refused at construction
                raise ToolNotBoundError(
                    f"{a.tool.value} declares unsupported port {a.contract.port!r}",
                    detail={"tool": a.tool.value, "port": a.contract.port},
                ) from None
        return impl

    # -- stage 2: durable checks ---------------------------------------------
    @staticmethod
    async def _check_step_identity(uow: UnitOfWork, a: _Attempt) -> None:
        """The row the attempt is recorded against must be this run, this
        step and this tool — a caller cannot record an execution of one tool
        under a step planned for another."""
        step = await uow.execution_steps.get(a.execution_step_id)
        if step is None:
            raise InternalError(
                "execution step does not exist",
                detail={"execution_step_id": str(a.execution_step_id), "step_id": a.step_id},
            )
        if step.run_id != a.run_id or step.step_id != a.step_id or step.tool != a.tool:
            violation = PolicyViolation(
                "execution step does not match the dispatch",
                detail={
                    "execution_step_id": str(a.execution_step_id),
                    "planned": {
                        "run_id": str(step.run_id),
                        "step_id": step.step_id,
                        "tool": str(step.tool),
                    },
                    "dispatched": {
                        "run_id": str(a.run_id),
                        "step_id": a.step_id,
                        "tool": a.tool.value,
                    },
                },
            )
            await uow.trace_events.append(
                run_id=a.run_id,
                kind=TraceEventKind.POLICY_VIOLATION,
                severity=TraceEventSeverity.ERROR,
                node=_DISPATCH_NODE,
                tool=a.tool,
                step_id=a.step_id,
                attempt=a.attempt,
                status="rejected",
                error=_error_payload(violation),
            )
            await uow.commit()
            _log.error("policy_violation", **violation.detail)
            raise violation

    @staticmethod
    async def _refuse_taken_attempt(uow: UnitOfWork, a: _Attempt) -> None:
        existing = await uow.tool_calls.get_by_attempt(a.execution_step_id, a.attempt)
        if existing is not None:
            raise DuplicateAttemptError(
                "this attempt has already been recorded",
                detail={
                    "tool": a.tool.value,
                    "step_id": a.step_id,
                    "attempt": a.attempt,
                    "tool_call_id": str(existing.id),
                    "status": str(existing.status),
                },
            )

    @staticmethod
    async def _verify_stored_decision(uow: UnitOfWork, a: _Attempt) -> None:
        """The token says a human approved; the row proves it. Every field the
        token does not carry (tool, risk, current status) is checked here."""
        token = a.token
        if token is None:  # pragma: no cover - _assert_gate ran first
            raise ApprovalRequiredError("no approval token", detail={"step_id": a.step_id})
        base = {"tool": a.tool.value, "step_id": a.step_id, "approval_id": token.approval_id}
        try:
            approval_id = uuid.UUID(token.approval_id)
        except ValueError:
            raise ApprovalInvalidError(
                "approval token names a malformed approval id", detail=base
            ) from None
        row = await uow.approvals.get(approval_id)
        if row is None:
            raise ApprovalInvalidError("approval token names no stored approval", detail=base)
        mismatches = []
        if row.status != ApprovalStatus.APPROVED:
            mismatches.append(f"status={row.status}")
        if row.run_id != a.run_id:
            mismatches.append("run_id")
        if row.step_id != a.step_id:
            mismatches.append("step_id")
        if row.tool != a.tool:
            mismatches.append("tool")
        if row.args_hash != a.args_hash:
            mismatches.append("args_hash")
        if row.risk != a.contract.risk:
            mismatches.append("risk")
        if mismatches:
            raise ApprovalInvalidError(
                "stored approval does not authorise this call",
                detail={**base, "mismatch": mismatches},
            )

    # -- stage 3: execute and record -----------------------------------------
    async def _execute_and_record(self, a: _Attempt, impl: ToolImplementation) -> DispatchResult:
        contract = a.contract
        port = self._adapters.port(contract.port) if contract.port is not None else None
        ctx = ToolContext(
            run_id=a.run_id,
            execution_step_id=a.execution_step_id,
            step_id=a.step_id,
            tool=a.tool,
            attempt=a.attempt,
            args_hash=a.args_hash,
            idempotency_key=a.idempotency_key,
            port=port,
            clock=self._clock,
        )
        assert a.validated is not None  # noqa: S101 - set in stage 1

        async with self._uow_factory() as uow:
            if a.idempotency_key is not None:
                await uow.tool_calls.lock_idempotency_key(a.idempotency_key)
                # Under the lock, both questions are exact: is this attempt
                # slot free, and was this effect already applied?
                try:
                    await self._refuse_taken_attempt(uow, a)
                except DuplicateAttemptError as exc:
                    await self._trace_unrecorded_rejection(uow, a, exc)
                    await uow.commit()
                    raise
                # Replay is only claimable when the key actually reached the
                # effect (the input model carries it). A mutating tool without
                # the field — `save_draft`, ADR-008 — applies again on retry,
                # and its record must say `succeeded`, not pretend otherwise.
                if "idempotency_key" in contract.input_model.model_fields:
                    a.replay = any(
                        row.status
                        in (ToolCallStatus.SUCCEEDED, ToolCallStatus.DUPLICATE_SUPPRESSED)
                        for row in await uow.tool_calls.list_by_idempotency_key(a.idempotency_key)
                    )

            try:
                output = await self._execute(a, impl, ctx)
            except asyncio.CancelledError:
                await self._record(
                    uow,
                    a,
                    outcome=DispatchOutcome.CANCELLED,
                    error=InternalError(
                        "attempt cancelled while executing",
                        detail={"tool": a.tool.value, "step_id": a.step_id},
                    ),
                )
                await uow.commit()
                raise
            except OpsPilotError as exc:
                outcome = (
                    DispatchOutcome.TIMEOUT
                    if isinstance(exc, ToolTimeoutError)
                    else DispatchOutcome.FAILED
                )
                await self._record(uow, a, outcome=outcome, error=exc)
                await uow.commit()
                raise

            outcome = (
                DispatchOutcome.DUPLICATE_SUPPRESSED if a.replay else DispatchOutcome.SUCCEEDED
            )
            output_data = output.model_dump(mode="json")
            row_id, finished_at, duration_ms = await self._record(
                uow, a, outcome=outcome, output=output_data
            )
            await uow.commit()

        return DispatchResult(
            tool=a.tool,
            tool_version=contract.version,
            step_id=a.step_id,
            attempt=a.attempt,
            outcome=outcome,
            args_hash=a.args_hash,
            idempotency_key=a.idempotency_key,
            output=output,
            output_data=output_data,
            tool_call_id=row_id,
            port=contract.port,
            adapter=self._adapter_label(contract),
            started_at=a.started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
        )

    async def _execute(self, a: _Attempt, impl: ToolImplementation, ctx: ToolContext) -> BaseModel:
        """Run the implementation under the contract's timeout and validate
        what it returns. Everything that escapes is a member of the taxonomy."""
        contract = a.contract
        try:
            async with asyncio.timeout(contract.timeout_ms / 1000):
                result: Any = await impl(a.validated, ctx)
        except TimeoutError as exc:
            raise ToolTimeoutError(
                f"{a.tool.value} exceeded its {contract.timeout_ms} ms timeout",
                detail={
                    "tool": a.tool.value,
                    "step_id": a.step_id,
                    "timeout_ms": contract.timeout_ms,
                },
            ) from exc
        except (OpsPilotError, asyncio.CancelledError):
            raise
        except ValidationError as exc:
            raise OutputValidationError(
                f"{a.tool.value} produced data that fails its schema",
                detail={"tool": a.tool.value, "errors": _validation_errors(exc)},
            ) from exc
        except Exception as exc:
            raise _classify_unexpected(a, exc) from exc

        payload = result.model_dump() if isinstance(result, BaseModel) else result
        if not isinstance(payload, Mapping):
            raise OutputValidationError(
                f"{a.tool.value} returned {type(result).__name__}, not its output model",
                detail={"tool": a.tool.value, "step_id": a.step_id},
            )
        try:
            return contract.output_model.model_validate(dict(payload))
        except ValidationError as exc:
            raise OutputValidationError(
                f"{a.tool.value} output failed the output schema",
                detail={
                    "tool": a.tool.value,
                    "step_id": a.step_id,
                    "errors": _validation_errors(exc),
                },
            ) from exc

    # -- recording -----------------------------------------------------------
    async def _reject(self, a: _Attempt, exc: OpsPilotError) -> None:
        """A stage-1 rejection: an attempt that never reached the port."""
        async with self._uow_factory() as uow:
            await self._check_step_identity(uow, a)
            await self._refuse_taken_attempt(uow, a)
            await self._trace_started(uow, a)
            await self._record(uow, a, outcome=DispatchOutcome.REJECTED, error=exc)
            await uow.commit()

    async def _trace_started(self, uow: UnitOfWork, a: _Attempt) -> None:
        await uow.trace_events.append(
            run_id=a.run_id,
            kind=TraceEventKind.TOOL_STARTED,
            node=_DISPATCH_NODE,
            tool=a.tool,
            step_id=a.step_id,
            attempt=a.attempt,
            input=a.persisted_input,
            status="started",
            retry_count=a.attempt - 1,
            payload=self._provenance(a),
        )

    async def _trace_unrecorded_rejection(
        self, uow: UnitOfWork, a: _Attempt, error: OpsPilotError
    ) -> None:
        """Close the `tool_started` pair for an attempt that cannot have a row
        because its `(step, attempt)` slot is already taken."""
        _stamp(error, a, outcome=DispatchOutcome.REJECTED, duration_ms=0)
        await uow.trace_events.append(
            run_id=a.run_id,
            kind=TraceEventKind.TOOL_FAILED,
            node=_DISPATCH_NODE,
            tool=a.tool,
            step_id=a.step_id,
            attempt=a.attempt,
            status=DispatchOutcome.REJECTED.value,
            retry_count=a.attempt - 1,
            error=_error_payload(error),
            payload=self._provenance(a),
        )

    async def _record(
        self,
        uow: UnitOfWork,
        a: _Attempt,
        *,
        outcome: DispatchOutcome,
        output: dict[str, Any] | None = None,
        error: OpsPilotError | None = None,
    ) -> tuple[uuid.UUID, datetime, int]:
        """Exactly one `tool_calls` row and the closing `tool_*` event for this
        attempt (§10.7, §14.4) — plus `policy_violation` at error severity
        when that is what happened (§10.7)."""
        finished_at = self._clock.now()
        duration_ms = max(0, int((finished_at - a.started_at).total_seconds() * 1000))
        reached_port = outcome not in (DispatchOutcome.REJECTED,)
        adapter = self._adapter_label(a.contract) if reached_port else None
        persisted_output = self._redact(output) if output is not None else None
        error_payload = _error_payload(error) if error is not None else None

        try:
            row = await uow.tool_calls.record_call(
                run_id=a.run_id,
                execution_step_id=a.execution_step_id,
                step_id=a.step_id,
                attempt=a.attempt,
                tool=a.tool,
                tool_version=a.contract.version,
                input=a.persisted_input,
                status=_ROW_STATUS[outcome],
                output=persisted_output,
                input_hash=a.args_hash,
                error_class=error.error_class.value if error is not None else None,
                error_message=error.message if error is not None else None,
                idempotency_key=a.idempotency_key,
                port=a.contract.port,
                adapter=adapter,
                started_at=a.started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
            )
        except IntegrityError as exc:
            # `uq_tool_calls_execution_step_id_attempt`: a read-tool attempt
            # raced another driver past the pre-check. The database keeps the
            # one-row-per-attempt invariant; this attempt's outcome is lost,
            # which is the honest result of two drivers on one run.
            raise DuplicateAttemptError(
                "this attempt was recorded concurrently by another driver",
                detail={"tool": a.tool.value, "step_id": a.step_id, "attempt": a.attempt},
            ) from exc
        if error is not None:
            _stamp(error, a, outcome=outcome, duration_ms=duration_ms, tool_call_id=row.id)

        await uow.trace_events.append(
            run_id=a.run_id,
            kind=_TRACE_KIND[outcome],
            severity=(
                TraceEventSeverity.ERROR
                if error is not None and error.error_class is ErrorClass.POLICY_VIOLATION
                else TraceEventSeverity.INFO
            ),
            node=_DISPATCH_NODE,
            tool=a.tool,
            step_id=a.step_id,
            attempt=a.attempt,
            input=a.persisted_input,
            output=persisted_output,
            status=outcome.value,
            duration_ms=duration_ms,
            retry_count=a.attempt - 1,
            error=error_payload,
            payload={**self._provenance(a), "tool_call_id": str(row.id)},
        )
        if error is not None and error.error_class is ErrorClass.POLICY_VIOLATION:
            await uow.trace_events.append(
                run_id=a.run_id,
                kind=TraceEventKind.POLICY_VIOLATION,
                severity=TraceEventSeverity.ERROR,
                node=_DISPATCH_NODE,
                tool=a.tool,
                step_id=a.step_id,
                attempt=a.attempt,
                status=outcome.value,
                error=error_payload,
                payload={**self._provenance(a), "tool_call_id": str(row.id)},
            )
            _log.error(
                "policy_violation",
                run_id=str(a.run_id),
                step_id=a.step_id,
                tool=a.tool.value,
                attempt=a.attempt,
                message=error.message,
                detail=error.detail,
            )
        return row.id, finished_at, duration_ms

    def _redact(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return redact_payload(payload, max_bytes=self._payload_max_bytes)

    def _adapter_label(self, contract: ToolContract) -> str | None:
        return self._adapter_name if contract.port is not None else None

    @staticmethod
    def _provenance(a: _Attempt) -> dict[str, Any]:
        """Never the token — only the approval it came from (§14.5)."""
        payload: dict[str, Any] = {"args_hash": a.args_hash}
        if a.idempotency_key is not None:
            payload["idempotency_key"] = a.idempotency_key
        if a.token is not None:
            payload["approval_id"] = a.token.approval_id
        if a.contract.port is not None:
            payload["port"] = a.contract.port
        return payload


# ---------------------------------------------------------------------------
# Mappings and helpers
# ---------------------------------------------------------------------------
_ROW_STATUS: Final[dict[DispatchOutcome, ToolCallStatus]] = {
    DispatchOutcome.SUCCEEDED: ToolCallStatus.SUCCEEDED,
    DispatchOutcome.DUPLICATE_SUPPRESSED: ToolCallStatus.DUPLICATE_SUPPRESSED,
    DispatchOutcome.FAILED: ToolCallStatus.FAILED,
    DispatchOutcome.TIMEOUT: ToolCallStatus.TIMEOUT,
    DispatchOutcome.REJECTED: ToolCallStatus.FAILED,
    DispatchOutcome.CANCELLED: ToolCallStatus.FAILED,
}

_TRACE_KIND: Final[dict[DispatchOutcome, TraceEventKind]] = {
    DispatchOutcome.SUCCEEDED: TraceEventKind.TOOL_SUCCEEDED,
    DispatchOutcome.DUPLICATE_SUPPRESSED: TraceEventKind.TOOL_DUPLICATE_SUPPRESSED,
    DispatchOutcome.FAILED: TraceEventKind.TOOL_FAILED,
    DispatchOutcome.TIMEOUT: TraceEventKind.TOOL_TIMEOUT,
    DispatchOutcome.REJECTED: TraceEventKind.TOOL_FAILED,
    DispatchOutcome.CANCELLED: TraceEventKind.TOOL_FAILED,
}


def _stamp(
    error: OpsPilotError,
    a: _Attempt,
    *,
    outcome: DispatchOutcome,
    duration_ms: int,
    tool_call_id: uuid.UUID | None = None,
) -> None:
    error.detail = {
        **error.detail,
        "dispatch": {
            "tool": a.tool.value,
            "step_id": a.step_id,
            "attempt": a.attempt,
            "outcome": outcome.value,
            "error_class": error.error_class.value,
            "duration_ms": duration_ms,
            "tool_call_id": str(tool_call_id) if tool_call_id is not None else None,
            "args_hash": a.args_hash,
            "idempotency_key": a.idempotency_key,
        },
    }


def _error_payload(error: OpsPilotError) -> dict[str, Any]:
    detail = {k: v for k, v in error.detail.items() if k != "dispatch"}
    return {
        "class": error.error_class.value,
        "message": error.message,
        "detail": redact_payload(detail, max_bytes=4096),
    }


def _validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {"loc": list(map(str, e["loc"])), "msg": e["msg"], "type": e["type"]}
        for e in exc.errors(include_url=False, include_input=False)
    ]


def _classify_unexpected(a: _Attempt, exc: Exception) -> OpsPilotError:
    """An exception outside the taxonomy. Infrastructure unavailability is
    the world's fault and retryable (§10.1 `TRANSIENT`); anything else is a
    bug and terminal. Never swallowed: the cause is chained."""
    detail = {"tool": a.tool.value, "step_id": a.step_id, "exception": type(exc).__name__}
    if isinstance(exc, (OperationalError, InterfaceError, ConnectionError)) or (
        isinstance(exc, DBAPIError) and exc.connection_invalidated
    ):
        return TransientToolError(f"{a.tool.value}: store unavailable", detail=detail)
    return InternalError(f"{a.tool.value} raised {type(exc).__name__}", detail=detail)
