"""Node handlers and execution boundaries for the LangGraph agent graph (§6, §7).

Every node is `async (state) -> dict` returning a partial state delta.
Nodes never mutate `AgentState` in place; declared reducers own composition (§5.3).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import structlog
from langgraph.types import interrupt
from pydantic import ValidationError

from app.agent.decide import Decision, evaluate_decision
from app.agent.normalizer import RuleTaskNormalizer, TaskNormalizer
from app.agent.planner import (
    Planner,
    PlanValidationError,
    RulePlanner,
    build_revision_context,
    carry_over_settled_steps,
    revision_requested,
    validate_plan,
)
from app.agent.preview import build_approval_preview
from app.agent.resolver import resolve_step_args
from app.agent.state import (
    TERMINAL_RUN_STATUSES,
    AgentError,
    AgentState,
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalState,
    ApprovalStatus,
    Budgets,
    FinalResponse,
    Plan,
    PlanStep,
    RunMetadata,
    RunStatus,
    StepStatus,
    ToolCall,
    ToolResult,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
)
from app.agent.verification_recovery import (
    ERROR_SOURCE_KEY,
    ERROR_SOURCE_VERIFY,
    ERROR_VERIFY_ATTEMPT_KEY,
    RetryTarget,
    is_safe_to_retry_mutation,
    is_verification_error,
    retry_target_for,
    tool_effect_succeeded,
    verification_attempt,
    verify_retry_key,
)
from app.agent.verifiers import VerificationContext, VerifierRegistry
from app.errors import (
    REPLANNABLE,
    TERMINAL_ERRORS,
    ApprovalRequiredError,
    ErrorClass,
    InputValidationError,
    PlannerError,
    PolicyViolation,
    RecoveryAction,
    backoff_delay_ms,
    is_retryable,
    recovery_action,
)
from app.integrations.ports import Adapters
from app.persistence.models import TraceEventKind, TraceEventSeverity
from app.persistence.protocols import ApprovalUpsert, UnitOfWorkFactory
from app.runtime import (
    CancellationSource,
    Clock,
    IdGenerator,
    SeededRandom,
    SystemClock,
    UuidIdGenerator,
)
from app.security import ApprovalGate, ApprovalToken, canonical_args_hash
from app.tools.contracts import (
    REGISTRY,
    SideEffect,
    ToolContract,
    ToolName,
    VerificationMode,
)
from app.tools.registry import DISPATCHER_OWNED_KEYS, ToolRegistry
from app.tools.schemas import Customer

__all__ = [
    "DEFAULT_APPROVAL_TTL",
    "NodeHandlers",
    "PLANNING_FAILURE_REASONS",
    "STATUS_REASON_INVALID_PLAN",
    "STATUS_REASON_PLANNER_ERROR",
    "create_initial_state",
    "format_failure_explanation",
    "is_required_step_rejected",
    "sanitize_text",
    "synthesize_complete_response",
    "synthesize_fail_response",
]

_log = structlog.get_logger("opspilot.agent.nodes")

#: `plan` could not produce an acceptable plan: it failed deterministic
#: validation (after the planner's one bounded repair), or the planner itself
#: failed. Both route to `fail` (§7 `plan`: "fails invalid after one bounded
#: repair attempt").
STATUS_REASON_INVALID_PLAN = "invalid_plan"
STATUS_REASON_PLANNER_ERROR = "planner_error"
PLANNING_FAILURE_REASONS = frozenset({STATUS_REASON_INVALID_PLAN, STATUS_REASON_PLANNER_ERROR})

#: How long a human has to answer an approval request (§9.8) when the graph is
#: assembled without `Settings` — the same 24 hours `OPSPILOT_APPROVAL_TTL_SECONDS`
#: defaults to.
DEFAULT_APPROVAL_TTL: Final = timedelta(hours=24)
#: The stored `payload_preview` is written through the same redaction the API
#: applies when it serves it (§14.5); this is the byte budget it uses.
_PREVIEW_MAX_BYTES: Final = 4096


@dataclass(frozen=True)
class _ApprovalRequest:
    """What `request_approval` learned about its step's approval, from the
    durable row (or, without a store, from the checkpointed state): the
    identity to surface in the interrupt, and the decision if one exists."""

    approval_id: str
    args_hash: str
    decision: ApprovalDecision | None = None
    preview: dict[str, Any] = field(default_factory=dict)


def create_initial_state(
    run_id: uuid.UUID | str,
    user_request: str,
    *,
    metadata: RunMetadata | None = None,
    deadline_at: datetime | None = None,
    clock: Clock | None = None,
    plan: Plan | None = None,
) -> AgentState:
    """Helper to initialize a complete AgentState channel dictionary."""
    clk = clock or SystemClock()
    now = clk.now()
    meta = metadata or RunMetadata()
    return {
        "run_id": str(run_id),
        "user_request": user_request,
        "normalized_task": None,
        "plan": plan,
        "plan_history": [],
        "current_step_id": None,
        "tool_calls": [],
        "tool_results": {},
        "approval_state": ApprovalState(),
        "errors": [],
        "retry_count": {},
        "replan_count": 0,
        "step_count": 0,
        "verification_result": {},
        "final_response": None,
        "status": RunStatus.CREATED,
        "status_reason": None,
        "created_at": now,
        "updated_at": now,
        "deadline_at": deadline_at or (now + timedelta(seconds=meta.budgets.run_deadline_seconds)),
        "metadata": meta,
    }


class NodeHandlers:
    """Encapsulates node execution logic and dependencies for the agent graph (§7)."""

    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        uow_factory: UnitOfWorkFactory | None = None,
        clock: Clock | None = None,
        id_gen: IdGenerator | None = None,
        normalizer: TaskNormalizer | None = None,
        planner: Planner | None = None,
        arg_resolver: Callable[[AgentState, PlanStep], dict[str, Any]] | None = None,
        understand_handler: Callable[[AgentState], Awaitable[dict[str, Any]]] | None = None,
        plan_handler: Callable[[AgentState], Awaitable[dict[str, Any]]] | None = None,
        verify_handler: Callable[[AgentState], Awaitable[dict[str, Any]]] | None = None,
        recover_handler: Callable[[AgentState], Awaitable[dict[str, Any]]] | None = None,
        complete_handler: (
            Callable[[AgentState], Awaitable[dict[str, Any]] | dict[str, Any]] | None
        ) = None,
        fail_handler: (
            Callable[[AgentState], Awaitable[dict[str, Any]] | dict[str, Any]] | None
        ) = None,
        retry_base_delay_ms: int = 250,
        retry_max_delay_ms: int = 8_000,
        seeded_random: SeededRandom | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        cancellation_source: (
            CancellationSource | Callable[[str], bool | Awaitable[bool]] | None
        ) = None,
        approval_ttl: timedelta | None = None,
        adapters: Adapters | None = None,
        verifier_registry: VerifierRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._adapters = adapters
        self._verifier_registry = verifier_registry or VerifierRegistry()
        self._uow_factory = uow_factory
        self._clock = clock or SystemClock()
        self._approval_ttl = approval_ttl or DEFAULT_APPROVAL_TTL
        self._id_gen = id_gen or UuidIdGenerator()
        self._normalizer = normalizer or RuleTaskNormalizer()
        self._planner: Planner = planner or RulePlanner()
        self._arg_resolver = arg_resolver
        self._understand_handler = understand_handler
        self._plan_handler = plan_handler
        self._verify_handler = verify_handler
        self._recover_handler = recover_handler
        self._complete_handler = complete_handler
        self._fail_handler = fail_handler
        self._retry_base_delay_ms = retry_base_delay_ms
        self._retry_max_delay_ms = retry_max_delay_ms
        self._seeded_random = seeded_random
        self._sleep_fn = sleep
        self._cancellation_source = cancellation_source

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------
    async def _clock_sleep(self, seconds: float) -> None:
        if self._sleep_fn is not None:
            await self._sleep_fn(seconds)
        elif hasattr(self._clock, "sleep"):
            res = self._clock.sleep(seconds)
            if asyncio.iscoroutine(res):
                await res
        elif hasattr(self._clock, "advance"):
            self._clock.advance(seconds=seconds)

    def _get_contract(self, tool_name: ToolName) -> ToolContract | None:
        if self._registry is not None:
            try:
                return self._registry.contract(tool_name)
            except Exception:
                return REGISTRY.get(tool_name)
        return REGISTRY.get(tool_name)

    def _resolve_step_args(self, state: AgentState, step: PlanStep) -> dict[str, Any]:
        if self._arg_resolver is not None:
            return self._arg_resolver(state, step)
        return resolve_step_args(state, step)

    @staticmethod
    def _validate_plan_arguments(
        contract: ToolContract, step_id: str, resolved_args: dict[str, Any]
    ) -> None:
        """Classify a planning fault (`INPUT_VALIDATION`) before the gate.

        Validates the resolved arguments against `contract.input_model` and
        ignores only the errors located at a dispatcher-owned field: those
        fields are injected by `ToolRegistry.dispatch`, which validates the
        complete input again. A plan that *supplies* one is still refused —
        by the dispatcher's hygiene check, which is the one place that
        distinction is made.
        """
        try:
            contract.input_model.model_validate(dict(resolved_args))
        except ValidationError as exc:
            problems = [
                {"loc": list(map(str, e["loc"])), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors(include_url=False, include_input=False)
                if not (e["loc"] and str(e["loc"][0]) in DISPATCHER_OWNED_KEYS)
            ]
            if problems:
                raise InputValidationError(
                    f"Step {step_id} arguments invalid for tool {contract.name.value}",
                    detail={"step_id": step_id, "tool": contract.name.value, "errors": problems},
                ) from exc

    async def _issue_approval_token(
        self, *, run_id: str, step_id: str, tool: ToolName, resolved_args: dict[str, Any]
    ) -> ApprovalToken:
        """Barrier 3 of §9.5 (HITL-002): the durable decision becomes a
        capability, or the step is refused.

        The repository selects the current `approved` row for exactly this
        run and step (SQL lives there, §12); `ApprovalGate.issue_from_persisted`
        binds it to this tool and to `canonical_args_hash(resolved_args)` and
        mints the only kind of token the dispatcher and the adapter accept.
        Nothing here approves, rejects, requests or mutates an approval, and
        nothing is cached: every attempt re-reads the row, so a decision that
        expired or was superseded after an earlier attempt is seen. Without a
        durable store there is nothing to mint from, so the step fails closed.
        """
        if self._uow_factory is None:
            raise ApprovalRequiredError(
                "no durable approval store is bound; an approval token cannot be issued",
                detail={"step_id": step_id, "tool": tool.value},
            )
        run_uuid = uuid.UUID(run_id)
        async with self._uow_factory() as uow:
            record = await uow.approvals.get_approved(run_uuid, step_id)
            await uow.commit()
        return ApprovalGate.issue_from_persisted(
            record,
            run_id=str(run_uuid),
            step_id=step_id,
            tool=tool.value,
            args=resolved_args,
            now=self._clock.now(),
        )

    async def _is_cancelled(self, state: AgentState) -> bool:
        """Cooperative cancellation check at node entry boundaries (§13.2)."""
        current_status = state.get("status")
        # Invariant: Terminal completion or rejection cannot be converted to failed/cancelled
        if current_status in (RunStatus.COMPLETED, RunStatus.REJECTED, RunStatus.EXPIRED):
            return False
        if current_status == RunStatus.FAILED and state.get("status_reason") != "cancelled":
            return False

        if current_status == RunStatus.CANCELLED or state.get("status_reason") in (
            "cancelled",
            "operator_cancelled",
        ):
            return True

        run_id = state.get("run_id")
        if self._cancellation_source is not None and run_id is not None:
            source = self._cancellation_source
            run_id_str = str(run_id)
            if hasattr(source, "is_cancelled"):
                res = source.is_cancelled(run_id_str)
            else:
                res = source(run_id_str)
            if isinstance(res, Awaitable):
                return bool(await res)
            return bool(res)

        return False

    def _is_cancelled_sync(self, state: AgentState) -> bool:
        """Synchronous cancellation check for routing decisions (§13.2)."""
        current_status = state.get("status")
        if current_status in (RunStatus.COMPLETED, RunStatus.REJECTED, RunStatus.EXPIRED):
            return False
        if current_status == RunStatus.FAILED and state.get("status_reason") != "cancelled":
            return False

        if current_status == RunStatus.CANCELLED or state.get("status_reason") in (
            "cancelled",
            "operator_cancelled",
        ):
            return True

        run_id = state.get("run_id")
        if self._cancellation_source is not None and run_id is not None:
            source = self._cancellation_source
            run_id_str = str(run_id)
            if hasattr(source, "is_cancelled"):
                res = source.is_cancelled(run_id_str)
            else:
                res = source(run_id_str)
            if not isinstance(res, Awaitable):
                return bool(res)

        return False

    # ---------------------------------------------------------------------------
    # 1. understand
    # ---------------------------------------------------------------------------
    async def understand(self, state: AgentState) -> dict[str, Any]:
        if self._understand_handler is not None:
            return await self._understand_handler(state)
        if await self._is_cancelled(state):
            return {
                "status": RunStatus.FAILED,
                "status_reason": "cancelled",
            }
        task = state.get("normalized_task")
        if task is None:
            user_req = state.get("user_request", "")
            task = await self._normalizer.normalize(user_req)
        return {
            "normalized_task": task,
            "status": RunStatus.RUNNING,
            "status_reason": None if task.in_scope else "out_of_scope",
        }

    def route_after_understand(self, state: AgentState) -> str:
        if (
            state.get("status") in TERMINAL_RUN_STATUSES
            or state.get("status_reason") == "cancelled"
        ):
            return "fail"
        task = state.get("normalized_task")
        if task is not None and task.in_scope:
            return "plan"
        return "fail"

    # ---------------------------------------------------------------------------
    # 2. plan
    # ---------------------------------------------------------------------------
    async def plan(self, state: AgentState) -> dict[str, Any]:
        """Delegate to the injected `Planner`, then validate deterministically (§7).

        Three entries, one contract: no plan yet → plan the task; a plan plus
        a revision request from `decide`/`recover` → revise it (the previous
        revision is kept in `plan_history`, `replan_count` advances); a plan
        with no revision request → it was supplied at run creation and is
        validated, not replaced. No tool runs here, and no plan is fabricated:
        a planner failure is recorded and routed to `fail`.
        """
        if self._plan_handler is not None:
            return await self._plan_handler(state)
        if await self._is_cancelled(state):
            return {
                "status": RunStatus.FAILED,
                "status_reason": "cancelled",
            }
        task = state.get("normalized_task")
        existing = state.get("plan")
        metadata = state.get("metadata") or RunMetadata()
        budgets = metadata.budgets
        revising = existing is not None and revision_requested(state)
        prior = build_revision_context(state, existing) if revising and existing else None
        try:
            if task is None or not task.in_scope:
                raise PlannerError("plan requires an in-scope normalized task")
            if existing is not None and prior is None:
                candidate = existing
            else:
                candidate = await self._planner.plan(task, prior, budgets=budgets)
            issues = validate_plan(candidate, task=task, contracts=REGISTRY, budgets=budgets)
            if issues:
                raise PlanValidationError(issues)
        except Exception as exc:  # every planner failure is classified below
            return self._planning_failure(exc, existing, revising, state)

        accepted = carry_over_settled_steps(candidate, prior) if prior is not None else candidate
        identity = self._planner.identity
        is_llm = accepted.created_by is identity.kind and identity.model_id is not None
        delta: dict[str, Any] = {
            "plan": accepted,
            "status_reason": None,
            "metadata": metadata.model_copy(
                update={
                    "planner_kind": accepted.created_by,
                    "model_id": identity.model_id if is_llm else None,
                    "prompt_version": (
                        identity.prompt_version
                        if is_llm and identity.prompt_version
                        else metadata.prompt_version
                    ),
                }
            ),
        }
        if revising and existing is not None:
            delta["plan_history"] = [existing]
            delta["replan_count"] = state.get("replan_count", 0) + 1
        return delta

    def _planning_failure(
        self, exc: Exception, existing: Plan | None, revising: bool, state: AgentState
    ) -> dict[str, Any]:
        if isinstance(exc, PlanValidationError):
            reason, error_class = STATUS_REASON_INVALID_PLAN, ErrorClass.PLANNER_ERROR
        elif isinstance(exc, PlannerError):
            reason, error_class = STATUS_REASON_PLANNER_ERROR, ErrorClass.PLANNER_ERROR
        else:
            reason, error_class = STATUS_REASON_PLANNER_ERROR, ErrorClass.INTERNAL
        detail = dict(getattr(exc, "detail", {}) or {})
        _log.warning("plan_failed", reason=reason, error_class=error_class.value, message=str(exc))
        delta: dict[str, Any] = {
            "status_reason": reason,
            "errors": [
                AgentError(
                    step_id=None,
                    error_class=error_class,
                    message=str(exc),
                    recovery=RecoveryAction.FAIL,
                    detail=detail,
                    occurred_at=self._clock.now(),
                )
            ],
        }
        if revising and existing is not None:
            delta["plan_history"] = [existing]
            delta["replan_count"] = state.get("replan_count", 0) + 1
        return delta

    def route_after_plan(self, state: AgentState) -> str:
        if (
            state.get("status") in TERMINAL_RUN_STATUSES
            or state.get("status_reason") == "cancelled"
        ):
            return "fail"
        budgets = state.get("metadata", RunMetadata()).budgets
        replan_count = state.get("replan_count", 0)
        if replan_count > budgets.max_replans:
            return "fail"
        plan_obj = state.get("plan")
        if plan_obj is None or state.get("status_reason") in PLANNING_FAILURE_REASONS:
            return "fail"
        return "decide"

    # ---------------------------------------------------------------------------
    # 3. decide
    # ---------------------------------------------------------------------------
    def _evaluate_decision(self, state: AgentState) -> Decision:
        """One evaluation of §6.2, shared by the node and its conditional edge
        so the two cannot disagree (`app.agent.decide`)."""
        effective_state = state
        if self._is_cancelled_sync(state):
            effective_state = {
                **state,
                "status": RunStatus.FAILED,
                "status_reason": "cancelled",
            }
        return evaluate_decision(
            effective_state,
            now=self._clock.now(),
            contract_for=self._get_contract,
            resolve_args=self._resolve_step_args,
        )

    async def decide(self, state: AgentState) -> dict[str, Any]:
        """The safety router (§6.2, ADR-006). Pure over state plus the
        registry: writes `current_step_id`, the expanded `plan` when a fan-out
        was expanded, and `status_reason`. Never calls a tool."""
        return self._evaluate_decision(state).state_delta()

    def route_after_decide(self, state: AgentState) -> str:
        return self._evaluate_decision(state).route.value

    # ---------------------------------------------------------------------------
    # 4. request_approval
    # ---------------------------------------------------------------------------
    async def request_approval(self, state: AgentState) -> dict[str, Any]:
        """The only pause in the graph (§6.3, §7, §9.7, ADR-007) — and never a
        tool call.

        LangGraph re-executes this node from the top on `Command(resume=…)`,
        so every execution makes the same idempotent request: the `approvals`
        row is upserted on `(run_id, step_id, args_hash)` and
        `approval_requested` is emitted only when that upsert genuinely
        inserted, both in one transaction. What the row says then decides
        the rest: a `pending` row is what `interrupt()` surfaces (the run
        pauses, or — on re-execution — the human's resume value comes back);
        an `approved`/`rejected` row for these exact arguments is the
        decision itself, recorded into `approval_state` without pausing
        again. Either way control falls through to `decide`, which re-applies
        rule 6 to the arguments as they will now be sent; the durable row is
        what `execute_tool` mints from (HITL-002), never this node's word.
        """
        if await self._is_cancelled(state):
            return {
                "status": RunStatus.FAILED,
                "status_reason": "cancelled",
            }
        current_step_id = state.get("current_step_id")
        plan = state.get("plan")
        step = plan.step(current_step_id) if plan and current_step_id else None
        if step is None or current_step_id is None or plan is None:
            return {"status": RunStatus.FAILED, "status_reason": "no_step_for_approval"}

        resolved_args = self._resolve_step_args(state, step)
        args_hash = canonical_args_hash(resolved_args)
        run_id_str = str(state.get("run_id", "default_run"))
        contract = self._get_contract(step.tool) or REGISTRY[step.tool]

        if self._uow_factory is not None:
            request = await self._persist_approval_request(
                self._uow_factory,
                run_id=uuid.UUID(run_id_str),
                step=step,
                args_hash=args_hash,
                resolved_args=resolved_args,
                tool_results=state.get("tool_results"),
                contract=contract,
            )
            preview = request.preview
        else:
            preview, _, _ = await build_approval_preview(
                step,
                resolved_args,
                tool_results=state.get("tool_results"),
                uow=None,
                risk=contract.risk,
            )
            request = self._checkpointed_approval_request(
                state, step, run_id_str, args_hash, preview=preview
            )

        decision = request.decision
        if decision is None:
            # Dynamic interruption point: execution pauses here, no tools invoked.
            # The payload is canonical — the same durable identity on every
            # execution — so a re-entry surfaces the same request, not a new one.
            interrupted_val = interrupt(
                {
                    "approval_id": request.approval_id,
                    "run_id": run_id_str,
                    "step_id": current_step_id,
                    "tool": step.tool.value,
                    "args_hash": request.args_hash,
                    "payload_preview": preview,
                }
            )
            # Resumed execution continues below
            if await self._is_cancelled(state):
                return {
                    "status": RunStatus.FAILED,
                    "status_reason": "cancelled",
                }
            decision = self._decision_from_resume(
                interrupted_val,
                approval_id=request.approval_id,
                step_id=current_step_id,
                args_hash=request.args_hash,
            )

        updated_plan = plan
        if decision.decision is ApprovalDecisionKind.REJECT:
            new_steps = []
            for s in plan.steps:
                if s.step_id == current_step_id:
                    new_status = StepStatus.SKIPPED if s.optional else StepStatus.REJECTED
                    new_steps.append(s.model_copy(update={"status": new_status}))
                else:
                    new_steps.append(s)
            updated_plan = plan.model_copy(update={"steps": new_steps})

        return {
            "approval_state": ApprovalState(
                pending=None,
                decisions={current_step_id: decision},
            ),
            "plan": updated_plan,
            "status": RunStatus.RUNNING,
        }

    async def _persist_approval_request(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        run_id: uuid.UUID,
        step: PlanStep,
        args_hash: str,
        preview: dict[str, Any] | None = None,
        resolved_args: dict[str, Any] | None = None,
        tool_results: Mapping[str, ToolResult] | None = None,
        contract: ToolContract | None = None,
    ) -> _ApprovalRequest:
        """The durable request (§9.7): one transaction that upserts the
        `approvals` row and, only when this transaction inserted it, appends
        `approval_requested` — plus `approval_superseded` for every earlier
        row the changed arguments invalidated. Roll back and neither exists;
        commit and both do. The row is read fresh on every execution, so a
        decision the human made while the worker was away is found here
        rather than requested again. A persistence failure propagates: there
        is no safe way to pause without a durable request to decide on.
        """
        target_contract = contract or self._get_contract(step.tool) or REGISTRY[step.tool]
        now = self._clock.now()
        async with uow_factory() as uow:
            if preview is None:
                final_preview, title, summary = await build_approval_preview(
                    step,
                    resolved_args or step.args,
                    tool_results=tool_results,
                    uow=uow,
                    risk=target_contract.risk,
                )
            else:
                final_preview = preview
                title = f"{step.tool.value}: approve step {step.step_id}"
                summary = step.rationale or target_contract.purpose

            upsert: ApprovalUpsert = await uow.approvals.upsert_request(
                run_id=run_id,
                step_id=step.step_id,
                tool=step.tool,
                risk=target_contract.risk,
                title=title,
                summary=summary,
                payload_preview=final_preview,
                args_hash=args_hash,
                requested_at=now,
                expires_at=now + self._approval_ttl,
            )
            row = upsert.row
            if upsert.created:
                for old in upsert.superseded:
                    await uow.trace_events.append(
                        run_id=run_id,
                        kind=TraceEventKind.APPROVAL_SUPERSEDED,
                        node="request_approval",
                        tool=step.tool,
                        step_id=step.step_id,
                        status=ApprovalStatus.SUPERSEDED.value,
                        payload={
                            "approval_id": str(old.id),
                            "superseded_by": str(row.id),
                            "args_hash": old.args_hash,
                        },
                    )
                await uow.trace_events.append(
                    run_id=run_id,
                    kind=TraceEventKind.APPROVAL_REQUESTED,
                    node="request_approval",
                    tool=step.tool,
                    step_id=step.step_id,
                    status=ApprovalStatus.PENDING.value,
                    payload={
                        "approval_id": str(row.id),
                        "args_hash": row.args_hash,
                        "risk": target_contract.risk.value,
                        "expires_at": row.expires_at.isoformat(),
                    },
                )
            await uow.commit()

        decision: ApprovalDecision | None = None
        if row.status is ApprovalStatus.APPROVED or row.status is ApprovalStatus.REJECTED:
            decision = ApprovalDecision(
                approval_id=str(row.id),
                step_id=step.step_id,
                decision=(
                    ApprovalDecisionKind.APPROVE
                    if row.status is ApprovalStatus.APPROVED
                    else ApprovalDecisionKind.REJECT
                ),
                args_hash=row.args_hash,
                decided_by=row.decided_by or "operator",
                decided_at=row.decided_at or now,
                reason=row.decision_reason,
            )
        _log.info(
            "approval_request_settled",
            run_id=str(run_id),
            step_id=step.step_id,
            approval_id=str(row.id),
            created=upsert.created,
            superseded=[str(old.id) for old in upsert.superseded],
            status=row.status.value,
        )
        return _ApprovalRequest(
            approval_id=str(row.id),
            args_hash=row.args_hash,
            decision=decision,
            preview=row.payload_preview or final_preview,
        )

    @staticmethod
    def _checkpointed_approval_request(
        state: AgentState,
        step: PlanStep,
        run_id: str,
        args_hash: str,
        preview: dict[str, Any] | None = None,
    ) -> _ApprovalRequest:
        """Without a durable store the checkpointed `approval_state` is the
        only record: a decision already held for these exact arguments is
        reused, anything else is asked. The identity is derived, not
        generated, so re-execution surfaces the same request."""
        approval_id = f"appr_{run_id[:8]}_{step.step_id}_{args_hash[:12]}"
        approval_state = state.get("approval_state") or ApprovalState()
        held = approval_state.decisions.get(step.step_id)
        decision = held if held is not None and held.args_hash == args_hash else None
        return _ApprovalRequest(
            approval_id=approval_id,
            args_hash=args_hash,
            decision=decision,
            preview=preview or {},
        )

    def _decision_from_resume(
        self,
        value: Any,  # noqa: ANN401 - whatever `Command(resume=...)` carried
        *,
        approval_id: str,
        step_id: str,
        args_hash: str,
    ) -> ApprovalDecision:
        """The human's answer as `Command(resume=…)` delivered it: a stored
        decision kind (`"approve"`/`"reject"`, what the approval service and
        the reconciler send), a mapping with `decision`/`decided_by`/`reason`,
        or a complete `ApprovalDecision`. Bound to the hash the human saw."""
        if isinstance(value, ApprovalDecision):
            return value
        if isinstance(value, dict):
            return ApprovalDecision(
                approval_id=approval_id,
                step_id=step_id,
                decision=ApprovalDecisionKind(value.get("decision", "approve")),
                args_hash=args_hash,
                decided_by=str(value.get("decided_by", "operator")),
                decided_at=self._clock.now(),
                reason=value.get("reason"),
            )
        return ApprovalDecision(
            approval_id=approval_id,
            step_id=step_id,
            decision=ApprovalDecisionKind(str(value)),
            args_hash=args_hash,
            decided_by="operator",
            decided_at=self._clock.now(),
        )

    # ---------------------------------------------------------------------------
    # 5. execute_tool
    # ---------------------------------------------------------------------------
    async def execute_tool(self, state: AgentState) -> dict[str, Any]:
        if await self._is_cancelled(state):
            return {
                "status": RunStatus.FAILED,
                "status_reason": "cancelled",
            }
        current_step_id = state.get("current_step_id")
        plan = state.get("plan")
        step = plan.step(current_step_id) if plan and current_step_id else None
        if step is None or current_step_id is None or plan is None:
            return {
                "errors": [
                    AgentError(
                        step_id=current_step_id,
                        error_class=ErrorClass.INTERNAL,
                        message=f"Current step {current_step_id} not found in plan",
                        occurred_at=self._clock.now(),
                    )
                ]
            }

        step_count = state.get("step_count", 0) + 1
        current_retries = state.get("retry_count", {}).get(current_step_id, 0)
        attempt = current_retries + 1
        contract = self._get_contract(step.tool) or REGISTRY[step.tool]
        budgets = state.get("metadata", RunMetadata()).budgets
        args_hash = canonical_args_hash(step.args)

        try:
            # 1. Resolve $ref parameters from tool_results
            resolved_args = self._resolve_step_args(state, step)
            args_hash = canonical_args_hash(resolved_args)

            # 2. Validate the plan's own arguments against the tool input
            #    model, so a planning fault is classified before the gate is
            #    consulted. The dispatcher-owned fields (`idempotency_key`,
            #    `approval_token`) are not the plan's to supply and are not
            #    validated here — the dispatcher injects the real ones (§8.5).
            #    Nothing token-shaped is ever fabricated for this preview.
            self._validate_plan_arguments(contract, current_step_id, resolved_args)

            # 3. Gate Re-assertion (Barrier 2, §9.5, §16.2)
            if contract.requires_approval:
                approval_state = state.get("approval_state")
                if approval_state is None or not approval_state.grants(
                    current_step_id, resolved_args
                ):
                    err = ApprovalRequiredError(
                        f"Step {current_step_id} requires approval for tool {step.tool.value}",
                        detail={"step_id": current_step_id, "tool": step.tool.value},
                    )
                    agent_err = AgentError(
                        step_id=current_step_id,
                        error_class=ErrorClass.POLICY_VIOLATION,
                        message=str(err),
                        attempt=attempt,
                        recovery=RecoveryAction.FAIL,
                        occurred_at=self._clock.now(),
                    )
                    tool_call = ToolCall(
                        step_id=current_step_id,
                        tool=step.tool,
                        attempt=attempt,
                        args_hash=args_hash,
                        status="failed",
                        error_class=ErrorClass.POLICY_VIOLATION,
                        error_message=str(err),
                        started_at=self._clock.now(),
                    )
                    return {
                        "errors": [agent_err],
                        "tool_calls": [tool_call],
                        "step_count": step_count,
                    }

            # 4. The only tool execution path: ToolRegistry.dispatch (§8.5, ADR-024)
            if self._registry is None:
                no_reg_err = PolicyViolation("ToolRegistry not bound in node handlers")
                return {
                    "errors": [
                        AgentError(
                            step_id=current_step_id,
                            error_class=ErrorClass.POLICY_VIOLATION,
                            message=str(no_reg_err),
                            attempt=attempt,
                            recovery=RecoveryAction.FAIL,
                            occurred_at=self._clock.now(),
                        )
                    ],
                    "step_count": step_count,
                }

            # 5. Barrier 3 (§9.5, HITL-002): a gated tool is dispatched with a
            #    token minted from the durable `approved` row for exactly this
            #    run, step, tool and argument hash — or not at all. An ungated
            #    tool never receives one (the dispatcher refuses a token for
            #    an ungated tool as a policy violation).
            token: ApprovalToken | None = None
            if contract.requires_approval:
                token = await self._issue_approval_token(
                    run_id=str(state["run_id"]),
                    step_id=current_step_id,
                    tool=step.tool,
                    resolved_args=resolved_args,
                )

            # Resolve execution_step_id if uow_factory is provided
            execution_step_id = uuid.UUID(hex=self._id_gen.new_id())
            if self._uow_factory is not None:
                try:
                    run_uuid = uuid.UUID(str(state["run_id"]))
                    async with self._uow_factory() as uow:
                        step_row = await uow.execution_steps.get_by_step_id(
                            run_uuid, current_step_id, plan.revision if plan else 0
                        )
                        if step_row is None:
                            step_row = await uow.execution_steps.create(
                                run_id=run_uuid,
                                step_id=current_step_id,
                                plan_revision=plan.revision if plan else 0,
                                seq=step_count,
                                tool=step.tool,
                                args=resolved_args,
                                args_hash=args_hash,
                            )
                            await uow.commit()
                        execution_step_id = step_row.id
                except Exception as e:
                    _log.warning("execution_step_persistence_fallback", error=str(e))

            dispatch_result = await self._registry.dispatch(
                run_id=uuid.UUID(str(state["run_id"])),
                execution_step_id=execution_step_id,
                step_id=current_step_id,
                tool_name=step.tool,
                arguments=resolved_args,
                attempt=attempt,
                approval_token=token,
            )

            tool_result = ToolResult(
                step_id=current_step_id,
                tool=step.tool,
                output=dispatch_result.output_data,
                produced_at=dispatch_result.finished_at,
            )
            tool_call = ToolCall(
                step_id=current_step_id,
                tool=step.tool,
                attempt=attempt,
                args_hash=dispatch_result.args_hash,
                idempotency_key=dispatch_result.idempotency_key,
                status="succeeded",
                duration_ms=dispatch_result.duration_ms,
                started_at=dispatch_result.started_at,
            )

            # A tool that returned is not yet a step that succeeded. For a
            # contract with postconditions the step stays `running` until
            # `verify` says `passed` (VERIFY-003): nothing downstream may read
            # an unverified mutation as settled. `VerificationMode.NONE` has
            # no postcondition to wait for, so it settles here as it always
            # did — `route_after_execute` sends it straight to `decide`.
            settled_now = contract is None or contract.verification == VerificationMode.NONE
            executed_status = StepStatus.SUCCEEDED if settled_now else StepStatus.RUNNING
            updated_steps = []
            for s in plan.steps:
                if s.step_id == current_step_id:
                    updated_steps.append(s.model_copy(update={"status": executed_status}))
                else:
                    updated_steps.append(s)
            updated_plan = plan.model_copy(update={"steps": updated_steps})

            delta: dict[str, Any] = {
                "tool_results": {current_step_id: tool_result},
                "tool_calls": [tool_call],
                "plan": updated_plan,
                "step_count": step_count,
            }

            if contract and contract.verification == VerificationMode.NONE:
                not_req_res = VerificationResult(
                    step_id=current_step_id,
                    status=VerificationStatus.NOT_REQUIRED,
                    mode="none",
                    checks=[],
                    detail=(
                        "VerificationMode.NONE: schema validation only; no postconditions required"
                    ),
                )
                delta["verification_result"] = {current_step_id: not_req_res}
                if self._uow_factory is not None:
                    try:
                        run_uuid = uuid.UUID(str(state["run_id"]))
                        async with self._uow_factory() as uow:
                            step_row = await uow.execution_steps.get_by_step_id(
                                run_uuid, current_step_id, plan.revision if plan else 0
                            )
                            if step_row is not None:
                                await uow.execution_steps.record_verification(
                                    step_row.id,
                                    verification_status=VerificationStatus.NOT_REQUIRED,
                                    verification=not_req_res.model_dump(mode="json"),
                                )
                            await uow.trace_events.append(
                                run_id=run_uuid,
                                kind=TraceEventKind.VERIFICATION_PASSED,
                                severity=TraceEventSeverity.INFO,
                                node="execute_tool",
                                tool=step.tool,
                                step_id=current_step_id,
                                attempt=attempt,
                                status=VerificationStatus.NOT_REQUIRED.value,
                                payload={"mode": "none", "not_required": True},
                            )
                            await uow.commit()
                    except Exception as e:
                        _log.warning("none_verification_persistence_fallback", error=str(e))

            return delta
        except Exception as exc:
            err_class = getattr(exc, "error_class", ErrorClass.INTERNAL)
            rec = recovery_action(
                err_class,
                idempotent=contract.idempotent if contract else False,
                nondeterministic=contract.nondeterministic if contract else False,
                retries_remaining=max(0, budgets.max_retries - current_retries),
                replans_remaining=max(0, budgets.max_replans - state.get("replan_count", 0)),
                step_optional=step.optional,
            )
            err_detail = dict(getattr(exc, "detail", {}) or {})
            retry_hint = getattr(exc, "retry_after_ms", None)
            if retry_hint is not None:
                err_detail["retry_after_ms"] = retry_hint
            agent_err = AgentError(
                step_id=current_step_id,
                error_class=err_class,
                message=str(exc),
                attempt=attempt,
                recovery=rec,
                detail=err_detail,
                occurred_at=self._clock.now(),
            )
            tool_call = ToolCall(
                step_id=current_step_id,
                tool=step.tool,
                attempt=attempt,
                args_hash=args_hash,
                status="failed",
                error_class=err_class,
                error_message=str(exc),
                started_at=self._clock.now(),
            )
            return {
                "errors": [agent_err],
                "tool_calls": [tool_call],
                "step_count": step_count,
            }

    def route_after_execute(self, state: AgentState) -> str:
        if (
            state.get("status") in TERMINAL_RUN_STATUSES
            or state.get("status_reason") == "cancelled"
        ):
            return "decide"
        current_step_id = state.get("current_step_id")
        errors = state.get("errors", [])
        tool_calls = state.get("tool_calls", [])
        # The attempt just made decides the route. A step re-executed after a
        # retry or a replan still has its earlier failure at the tail of
        # `errors`; only when no newer successful attempt of the same step
        # exists is that failure the current one.
        last_attempt = next((c for c in reversed(tool_calls) if c.step_id == current_step_id), None)
        succeeded_now = last_attempt is not None and last_attempt.status == "succeeded"
        if errors and errors[-1].step_id == current_step_id and not succeeded_now:
            return "recover"

        plan = state.get("plan")
        step = plan.step(current_step_id) if plan and current_step_id else None
        if step:
            contract = self._get_contract(step.tool)
            if contract and contract.verification != VerificationMode.NONE:
                return "verify"
        return "decide"

    # ---------------------------------------------------------------------------
    # 6. verify
    # ---------------------------------------------------------------------------
    async def verify(self, state: AgentState) -> dict[str, Any]:
        if self._verify_handler is not None:
            return await self._verify_handler(state)
        if await self._is_cancelled(state):
            return {
                "status": RunStatus.FAILED,
                "status_reason": "cancelled",
            }
        current_step_id = state.get("current_step_id")
        plan = state.get("plan")
        step = plan.step(current_step_id) if plan and current_step_id else None
        if step is None or current_step_id is None:
            return {}

        contract = self._get_contract(step.tool) or REGISTRY.get(step.tool)
        if contract is None:
            return {}

        # 1. Resolve requested intent (resolved arguments)
        resolved_args = self._resolve_step_args(state, step)

        # 2. Get tool output from tool_results
        tool_results = state.get("tool_results", {})
        tool_res = tool_results.get(current_step_id)
        output_data = tool_res.output if tool_res else {}

        # 3. Get attempt number and idempotency key from latest tool_call
        tool_calls = state.get("tool_calls", [])
        last_call = next((c for c in reversed(tool_calls) if c.step_id == current_step_id), None)
        attempt = last_call.attempt if last_call else 1
        idempotency_key = last_call.idempotency_key if last_call else None

        # 3a. Which *verification* attempt is this? A read-back retry does not
        #     re-run the tool, so it cannot borrow `ToolCall.attempt`; it is
        #     counted under its own `retry_count` key, which is checkpointed
        #     state and therefore identical after a resume (VERIFY-003).
        verify_attempt = state.get("retry_count", {}).get(verify_retry_key(current_step_id), 0) + 1

        # 4. Resolve adapters
        adapters = self._adapters or (
            getattr(self._registry, "adapters", None) if self._registry else None
        )

        # 5. Resolve baseline customer if updating customer
        baseline_customer: Customer | None = None
        if step.tool == ToolName.UPDATE_CUSTOMER:
            cid = resolved_args.get("customer_id")
            if cid:
                for tr in tool_results.values():
                    out = getattr(tr, "output", None)
                    if isinstance(out, dict):
                        c_data = out.get("customer")
                        if isinstance(c_data, dict) and c_data.get("customer_id") == cid:
                            with contextlib.suppress(Exception):
                                baseline_customer = Customer.model_validate(c_data)
                                break
                        elif isinstance(c_data, Customer) and c_data.customer_id == cid:
                            baseline_customer = c_data
                            break
            if baseline_customer is None:
                raw_base = resolved_args.get("baseline")
                if isinstance(raw_base, Customer):
                    baseline_customer = raw_base
                elif isinstance(raw_base, dict):
                    with contextlib.suppress(Exception):
                        baseline_customer = Customer.model_validate(raw_base)

        ctx = VerificationContext(
            run_id=uuid.UUID(str(state["run_id"])),
            step_id=current_step_id,
            tool=step.tool,
            attempt=attempt,
            contract=contract,
            input_args=resolved_args,
            output_data=output_data,
            idempotency_key=idempotency_key,
            adapters=adapters,
            uow_factory=self._uow_factory,
            clock=self._clock,
            baseline_customer=baseline_customer,
            prior_tool_results=tool_results,
        )

        verifier = self._verifier_registry.get_verifier(contract)
        started_at = self._clock.now()

        try:
            res = await verifier.verify(ctx)
            finished_at = self._clock.now()
            duration_ms = int((finished_at - started_at).total_seconds() * 1000)
            res = res.model_copy(update={"duration_ms": duration_ms, "attempt": verify_attempt})
            verifier_error_class: ErrorClass | None = None
        except Exception as exc:
            # §11.4: inability to check is not evidence of a bad write. The
            # result is `unconfirmed` and the error is `TRANSIENT`, never
            # `VERIFICATION_FAILED` — unless the verifier itself is broken
            # (a bug, a policy breach), which stays terminal.
            raw_class = getattr(exc, "error_class", ErrorClass.TRANSIENT)
            verifier_error_class = (
                raw_class if raw_class in TERMINAL_ERRORS else ErrorClass.TRANSIENT
            )
            res = VerificationResult(
                step_id=current_step_id,
                status=VerificationStatus.UNCONFIRMED,
                mode=contract.verification.value,
                checks=[
                    VerificationCheck(
                        name="verifier_execution",
                        passed=False,
                        expected="no exception",
                        observed=str(exc),
                    )
                ],
                duration_ms=int((self._clock.now() - started_at).total_seconds() * 1000),
                detail=f"Verifier error: {exc}",
                attempt=verify_attempt,
            )

        delta: dict[str, Any] = {"verification_result": {current_step_id: res}}

        # A step is settled successfully only once its postconditions hold.
        if res.status in (VerificationStatus.PASSED, VerificationStatus.NOT_REQUIRED):
            if plan is not None and step.status is not StepStatus.SUCCEEDED:
                delta["plan"] = plan.model_copy(
                    update={
                        "steps": [
                            s.model_copy(update={"status": StepStatus.SUCCEEDED})
                            if s.step_id == current_step_id
                            else s
                            for s in plan.steps
                        ]
                    }
                )
        elif res.status is VerificationStatus.FAILED:
            # Independent evidence proves the postcondition is false.
            delta["errors"] = [
                AgentError(
                    step_id=current_step_id,
                    error_class=ErrorClass.VERIFICATION_FAILED,
                    message=(
                        res.detail
                        or f"Postcondition verification failed for step {current_step_id} "
                        f"({step.tool.value})"
                    ),
                    attempt=attempt,
                    detail=self._verification_error_detail(res, verify_attempt),
                    occurred_at=self._clock.now(),
                )
            ]
        else:
            # UNCONFIRMED is not FAILED: the system cannot currently tell
            # whether the effect happened, which is a transient condition to
            # be re-read — never a licence to mutate again.
            delta["errors"] = [
                AgentError(
                    step_id=current_step_id,
                    error_class=verifier_error_class or ErrorClass.TRANSIENT,
                    message=(
                        res.detail
                        or f"Postcondition verification unconfirmed for step {current_step_id} "
                        f"({step.tool.value})"
                    ),
                    attempt=attempt,
                    detail=self._verification_error_detail(res, verify_attempt),
                    occurred_at=self._clock.now(),
                )
            ]

        await self._persist_verification(
            state,
            step=step,
            plan=plan,
            result=res,
            attempt=attempt,
            contract=contract,
        )
        return delta

    @staticmethod
    def _verification_error_detail(
        result: VerificationResult, verify_attempt: int
    ) -> dict[str, Any]:
        """What `recover` needs to route this failure, and nothing else.

        `source` says the failure came from `verify` rather than the
        dispatcher; `verification_attempt` is the read-back retry counter the
        exactly-once accounting compares against; the checks are the evidence
        the retry-safety decision reads. All of it is already operator-facing
        verification output — no arguments, previews or secrets.
        """
        return {
            ERROR_SOURCE_KEY: ERROR_SOURCE_VERIFY,
            ERROR_VERIFY_ATTEMPT_KEY: verify_attempt,
            "verification_status": result.status.value,
            "checks": [c.model_dump() for c in result.checks],
        }

    async def _persist_verification(
        self,
        state: AgentState,
        *,
        step: PlanStep,
        plan: Plan | None,
        result: VerificationResult,
        attempt: int,
        contract: ToolContract,
    ) -> None:
        """Mirror the result into `execution_steps` and the product trace.

        Persistence is best-effort by design: a trace outage must not turn a
        healthy verification into a failed run. The graph's own decision is
        already carried by the returned delta.
        """
        if self._uow_factory is None:
            return
        try:
            run_uuid = uuid.UUID(str(state["run_id"]))
            async with self._uow_factory() as uow:
                step_row = await uow.execution_steps.get_by_step_id(
                    run_uuid, step.step_id, plan.revision if plan else 0
                )
                if step_row is not None:
                    await uow.execution_steps.record_verification(
                        step_row.id,
                        verification_status=result.status,
                        verification=result.model_dump(mode="json"),
                    )

                is_ok = result.status in (
                    VerificationStatus.PASSED,
                    VerificationStatus.NOT_REQUIRED,
                )
                unconfirmed = result.status is VerificationStatus.UNCONFIRMED
                if is_ok:
                    kind = TraceEventKind.VERIFICATION_PASSED
                    severity = TraceEventSeverity.INFO
                else:
                    kind = TraceEventKind.VERIFICATION_FAILED
                    # An unconfirmed read-back is a warning whatever the tool
                    # does: we have found no evidence of a bad effect, only an
                    # absence of evidence. Only a proven failed postcondition
                    # on a mutating tool is an error.
                    severity = (
                        TraceEventSeverity.WARNING
                        if unconfirmed or not contract.is_mutating
                        else TraceEventSeverity.ERROR
                    )
                payload: dict[str, Any] = {
                    "mode": result.mode,
                    "checks": [c.model_dump() for c in result.checks],
                    "detail": result.detail,
                    "verification_attempt": result.attempt,
                }
                if unconfirmed:
                    # Name the distinction in the trace itself: this is an
                    # inability to check, it is classified transient, and the
                    # recovery for it is another read-back, not another write.
                    payload.update(
                        {
                            "unconfirmed": True,
                            "classification": ErrorClass.TRANSIENT.value,
                            "recovery": "retry_readback",
                            "tool_effect_succeeded": tool_effect_succeeded(
                                state.get("tool_calls", []), step.step_id
                            ),
                        }
                    )
                await uow.trace_events.append(
                    run_id=run_uuid,
                    kind=kind,
                    severity=severity,
                    node="verify",
                    tool=step.tool,
                    step_id=step.step_id,
                    attempt=attempt,
                    status=result.status.value,
                    duration_ms=result.duration_ms,
                    retry_count=result.attempt - 1,
                    payload=payload,
                )
                await uow.commit()
        except Exception as e:
            _log.warning("verification_persistence_fallback", error=str(e))

    def route_after_verify(self, state: AgentState) -> str:
        if (
            state.get("status") in TERMINAL_RUN_STATUSES
            or state.get("status_reason") == "cancelled"
            or self._is_cancelled_sync(state)
        ):
            return "decide"
        current_step_id = state.get("current_step_id")
        results = state.get("verification_result", {})
        if current_step_id in results:
            res = results[current_step_id]
            if res.status in (VerificationStatus.PASSED, VerificationStatus.NOT_REQUIRED):
                return "decide"
        return "recover"

    # ---------------------------------------------------------------------------
    # 7. recover
    # ---------------------------------------------------------------------------
    async def recover(self, state: AgentState) -> dict[str, Any]:
        if self._recover_handler is not None:
            return await self._recover_handler(state)

        # 1. Terminal / cancelled run check (§5.4 lifecycle guard)
        run_status = state.get("status")
        if run_status in (RunStatus.COMPLETED, RunStatus.REJECTED, RunStatus.EXPIRED):
            return {"status_reason": state.get("status_reason") or "terminal_run"}

        if await self._is_cancelled(state):
            return {
                "status": RunStatus.FAILED,
                "status_reason": state.get("status_reason") or "cancelled",
            }
        if run_status in TERMINAL_RUN_STATUSES:
            return {"status_reason": state.get("status_reason") or "terminal_run"}

        # 2. Identify current step
        current_step_id = state.get("current_step_id")
        if not current_step_id:
            return {"status_reason": "missing_current_step"}

        plan = state.get("plan")
        step = plan.step(current_step_id) if plan and current_step_id else None
        if not step:
            return {"status_reason": "step_not_found"}

        # 3. Error inspection and validation
        errors = state.get("errors", [])
        latest_err = errors[-1] if errors else None
        if not latest_err:
            return {"status_reason": "missing_error"}

        # Stale error check: latest error must match current step
        if latest_err.step_id != current_step_id:
            return {"status_reason": "stale_error"}

        # 4. Budget evaluation
        metadata = state.get("metadata") or RunMetadata()
        budgets = metadata.budgets
        step_count = state.get("step_count", 0)
        deadline_at = state.get("deadline_at")

        now = self._clock.now()
        deadline_passed = deadline_at is not None and now >= deadline_at
        step_budget_exhausted = step_count >= budgets.max_steps
        budget_exhausted = deadline_passed or step_budget_exhausted

        # 5. Contract and attempt state
        contract = self._get_contract(step.tool)
        idempotent = contract.idempotent if contract else False
        nondeterministic = contract.nondeterministic if contract else False

        retries = state.get("retry_count", {})
        current_retries = retries.get(current_step_id, 0)
        replan_count = state.get("replan_count", 0)

        # 5a. Verification evidence (VERIFY-003). `verify` stamps its errors,
        #     so a failure that came from the read-back is distinguishable
        #     from a tool-execution failure without inferring it from the
        #     error class — and the stamp is checkpointed with the error.
        verification = state.get("verification_result", {}).get(current_step_id)
        from_verify = is_verification_error(latest_err)
        target = retry_target_for(
            error=latest_err,
            result=verification,
            tool_calls=state.get("tool_calls", []),
            step_id=current_step_id,
        )

        if target is RetryTarget.VERIFY:
            # The tool already succeeded and the verifier could not reach a
            # conclusion. The only safe recovery is another read-back.
            return await self._recover_unconfirmed(
                state,
                step=step,
                plan=plan,
                latest_err=latest_err,
                verification=verification,
                budgets=budgets,
                deadline_passed=deadline_passed,
                step_budget_exhausted=step_budget_exhausted,
            )

        # A *proven* failed postcondition. Whether the mutation may be
        # repeated is a question about the evidence, not the error class: an
        # absent write is safe to redo, a wrong or duplicated one is not, and
        # neither is a non-idempotent tool (invariant P5). An optional step
        # still skips rather than ending the run — skipping compounds nothing,
        # and the responder reports the effect as unconfirmed either way.
        if (
            from_verify
            and verification is not None
            and verification.status is VerificationStatus.FAILED
            and not is_safe_to_retry_mutation(verification, contract)
        ):
            if step.optional and plan is not None:
                return self._skip_optional_step(plan, current_step_id)
            return {"status_reason": "verification_failed"}

        # Crash / resume idempotency: has this attempt already been accounted for?
        retry_already_counted = (
            latest_err.attempt is not None and current_retries >= latest_err.attempt
        )

        retries_remaining = max(0, budgets.max_retries - current_retries)
        replans_remaining = max(0, budgets.max_replans - replan_count)

        # 6. Recovery action classification (§10.2)
        action = recovery_action(
            latest_err.error_class,
            idempotent=idempotent,
            nondeterministic=nondeterministic,
            retries_remaining=retries_remaining,
            replans_remaining=replans_remaining,
            step_optional=step.optional,
            budget_exhausted=budget_exhausted,
        )

        # 7. Execute recovery action
        if action == RecoveryAction.RETRY:
            if not retry_already_counted:
                new_retries = current_retries + 1
                delay_ms = self._retry_delay_ms(latest_err, new_retries)
                await self._clock_sleep(delay_ms / 1000.0)
                await self._trace_retry_scheduled(
                    state,
                    step=step,
                    target=RetryTarget.EXECUTE_TOOL,
                    attempt=new_retries,
                    delay_ms=delay_ms,
                    error_class=latest_err.error_class,
                )
                return {
                    "retry_count": {current_step_id: new_retries},
                    "status_reason": f"retry_attempt_{new_retries}",
                }
            return {
                "retry_count": {current_step_id: current_retries},
                "status_reason": f"retry_attempt_{current_retries}",
            }

        if action == RecoveryAction.SKIP and step.optional and plan is not None:
            return self._skip_optional_step(plan, current_step_id)

        if action == RecoveryAction.REPLAN:
            return {
                "status_reason": "replannable_fault",
            }

        # Terminal / Unrecoverable failure
        if deadline_passed:
            fail_reason = "deadline_exceeded"
        elif step_budget_exhausted:
            fail_reason = "budget_exhausted"
        elif latest_err.error_class in TERMINAL_ERRORS:
            fail_reason = f"terminal_error_{latest_err.error_class.value}"
        elif from_verify:
            # The architecture names both of these; never launder either into
            # the generic exhaustion reason (§10.3, §11.4).
            fail_reason = (
                "verification_unconfirmed"
                if verification is not None
                and verification.status is VerificationStatus.UNCONFIRMED
                else "verification_failed"
            )
        elif retries_remaining == 0 and is_retryable(
            latest_err.error_class, idempotent=idempotent, nondeterministic=nondeterministic
        ):
            fail_reason = "retry_budget_exhausted"
        elif replans_remaining == 0 and latest_err.error_class in REPLANNABLE:
            fail_reason = "replan_budget_exhausted"
        elif not step.optional and latest_err.error_class == ErrorClass.NOT_FOUND:
            fail_reason = "required_step_not_found"
        else:
            fail_reason = "recovery_exhausted"

        return {"status_reason": fail_reason}

    async def _recover_unconfirmed(
        self,
        state: AgentState,
        *,
        step: PlanStep,
        plan: Plan | None,
        latest_err: AgentError,
        verification: VerificationResult | None,
        budgets: Budgets,
        deadline_passed: bool,
        step_budget_exhausted: bool,
    ) -> dict[str, Any]:
        """Retry the read-back, never the write.

        The tool reported success; the verifier could not say whether the
        effect landed. Re-executing would risk a second real effect to answer
        a question a second read can answer for free. Read-back attempts are
        counted under their own `retry_count` key and bounded by the same
        retry budget; exhausting it ends the run as `verification_unconfirmed`
        — honest about what is and is not known.
        """
        step_id = step.step_id
        key = verify_retry_key(step_id)
        verify_retries = state.get("retry_count", {}).get(key, 0)
        attempt_seen = verification_attempt(latest_err, verification)

        if deadline_passed:
            return {"status_reason": "deadline_exceeded"}
        if step_budget_exhausted:
            return {"status_reason": "budget_exhausted"}
        if latest_err.error_class in TERMINAL_ERRORS:
            return {"status_reason": f"terminal_error_{latest_err.error_class.value}"}

        # Crash/resume idempotency, the same guard `execute_tool` retries use:
        # the counter may only move past the attempt the evidence belongs to.
        # Checked before the budget, because a read-back already scheduled and
        # paid for must still happen — it was within budget when it was made.
        if verify_retries >= attempt_seen:
            return {
                "retry_count": {key: verify_retries},
                "status_reason": f"retry_verify_attempt_{verify_retries}",
            }

        if verify_retries >= budgets.max_retries:
            if step.optional and plan is not None:
                return self._skip_optional_step(plan, step_id)
            return {"status_reason": "verification_unconfirmed"}

        new_retries = verify_retries + 1
        delay_ms = self._retry_delay_ms(latest_err, new_retries)
        await self._clock_sleep(delay_ms / 1000.0)
        await self._trace_retry_scheduled(
            state,
            step=step,
            target=RetryTarget.VERIFY,
            attempt=new_retries,
            delay_ms=delay_ms,
            error_class=latest_err.error_class,
        )
        return {
            "retry_count": {key: new_retries},
            "status_reason": f"retry_verify_attempt_{new_retries}",
        }

    @staticmethod
    def _skip_optional_step(plan: Plan, step_id: str) -> dict[str, Any]:
        return {
            "plan": plan.model_copy(
                update={
                    "steps": [
                        s.model_copy(update={"status": StepStatus.SKIPPED})
                        if s.step_id == step_id
                        else s
                        for s in plan.steps
                    ]
                }
            ),
            "status_reason": "optional_step_skipped",
        }

    def _retry_delay_ms(self, latest_err: AgentError, attempt: int) -> int:
        """Exponential backoff with deterministic jitter, honouring a server
        hint carried on the error (§10.4)."""
        retry_after_ms = None
        detail = latest_err.detail or {}
        if detail.get("retry_after_ms") is not None:
            with contextlib.suppress(ValueError, TypeError):
                retry_after_ms = int(detail["retry_after_ms"])
        elif detail.get("retry_after") is not None:
            with contextlib.suppress(ValueError, TypeError):
                val = float(detail["retry_after"])
                retry_after_ms = int(val * 1000) if val < 1000 else int(val)

        jitter = 1.0
        if self._seeded_random is not None:
            jitter = self._seeded_random.uniform(0.8, 1.2)

        return backoff_delay_ms(
            attempt,
            base_ms=self._retry_base_delay_ms,
            max_ms=self._retry_max_delay_ms,
            jitter=jitter,
            retry_after_ms=retry_after_ms,
        )

    async def _trace_retry_scheduled(
        self,
        state: AgentState,
        *,
        step: PlanStep,
        target: RetryTarget,
        attempt: int,
        delay_ms: int,
        error_class: ErrorClass,
    ) -> None:
        """Record which node the retry re-enters.

        `target="verify"` versus `target="execute_tool"` is the difference
        between re-reading and re-writing, so an auditor must be able to see
        it without reconstructing the state machine. Best-effort: a trace
        outage may not change what the run does.
        """
        if self._uow_factory is None:
            return
        try:
            async with self._uow_factory() as uow:
                await uow.trace_events.append(
                    run_id=uuid.UUID(str(state["run_id"])),
                    kind=TraceEventKind.RETRY_SCHEDULED,
                    severity=TraceEventSeverity.WARNING,
                    node="recover",
                    tool=step.tool,
                    step_id=step.step_id,
                    attempt=attempt,
                    retry_count=attempt,
                    status=target.value,
                    payload={
                        "target": target.value,
                        "delay_ms": delay_ms,
                        "error_class": error_class.value,
                    },
                )
                await uow.commit()
        except Exception as e:
            _log.warning("retry_trace_persistence_fallback", error=str(e))

    def route_after_recover(self, state: AgentState) -> str:
        if (
            state.get("status") in TERMINAL_RUN_STATUSES
            or state.get("status_reason") == "cancelled"
            or self._is_cancelled_sync(state)
        ):
            return "fail"

        status_reason = state.get("status_reason")
        if status_reason == "optional_step_skipped":
            return "decide"

        current_step_id = state.get("current_step_id")
        plan = state.get("plan")
        step = plan.step(current_step_id) if plan and current_step_id else None

        errors = state.get("errors", [])
        latest_err = errors[-1] if errors else None
        if not latest_err or (current_step_id and latest_err.step_id != current_step_id):
            return "fail"

        budgets = (state.get("metadata") or RunMetadata()).budgets
        retries = state.get("retry_count", {})
        current_retries = retries.get(current_step_id, 0) if current_step_id else 0
        replan_count = state.get("replan_count", 0)

        # A read-back retry re-enters `verify`. The reason string is written
        # by `recover` in the same super-step and checkpointed with it, and
        # the budget it is checked against is the read-back counter — so a
        # resumed run routes exactly where the crashed one was going.
        if status_reason and status_reason.startswith("retry_verify_attempt_"):
            verify_retries = (
                retries.get(verify_retry_key(current_step_id), 0) if current_step_id else 0
            )
            if verify_retries <= budgets.max_retries:
                return "verify"
            return "fail"

        if status_reason and status_reason.startswith(("retry_attempt_", "retry_scheduled_")):
            if current_retries <= budgets.max_retries:
                return "execute_tool"
            return "fail"

        if status_reason == "replannable_fault":
            if replan_count < budgets.max_replans:
                return "plan"
            return "fail"

        if status_reason in (
            "recovery_exhausted",
            "retry_budget_exhausted",
            "replan_budget_exhausted",
            "budget_exhausted",
            "deadline_exceeded",
            "stale_error",
            "missing_error",
            "missing_current_step",
            "step_not_found",
            "required_step_not_found",
            "verification_failed",
            "verification_unconfirmed",
        ) or (status_reason and status_reason.startswith("terminal_error_")):
            return "fail"

        # Fallback / direct call check (matching existing tests in test_agent_graph.py)
        action = latest_err.recovery if latest_err and latest_err.recovery else RecoveryAction.FAIL
        if action == RecoveryAction.RETRY and current_retries <= budgets.max_retries:
            return "execute_tool"
        if action == RecoveryAction.REPLAN and replan_count < budgets.max_replans:
            return "plan"
        if action == RecoveryAction.SKIP and step and step.optional:
            return "decide"
        return "fail"

    # ---------------------------------------------------------------------------
    # 8. complete
    # ---------------------------------------------------------------------------
    async def complete(self, state: AgentState) -> dict[str, Any]:
        if self._complete_handler is not None:
            res = self._complete_handler(state)
            if isinstance(res, Awaitable):
                return await res
            return res
        if await self._is_cancelled(state):
            return synthesize_fail_response(
                {**state, "status": RunStatus.FAILED, "status_reason": "cancelled"}
            )
        return synthesize_complete_response(state)

    # ---------------------------------------------------------------------------
    # 9. fail
    # ---------------------------------------------------------------------------
    async def fail(self, state: AgentState) -> dict[str, Any]:
        if self._fail_handler is not None:
            res = self._fail_handler(state)
            if isinstance(res, Awaitable):
                return await res
            return res
        return synthesize_fail_response(state)


# ---------------------------------------------------------------------------
# Terminal Response Layer Helpers (§7, §10.6, §11.4)
# ---------------------------------------------------------------------------
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[A-Za-z0-9_\-\.]+"),
    re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(secret\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(token\s*[:=]\s*)[A-Za-z0-9_\-\.]+"),
)

REASON_EXPLANATIONS: Final[dict[str, str]] = {
    "approval_rejected": "A required execution step was rejected by the operator",
    "budget_exhausted": "The maximum allowed step count was exceeded",
    "deadline_exceeded": "The execution deadline elapsed before completion",
    "retry_budget_exhausted": "The maximum retry attempts for a failing step were exhausted",
    "replan_budget_exhausted": "The maximum plan revision attempts were exhausted",
    "unresolvable_plan": "Next step dependencies could not be resolved and replans were exhausted",
    "invalid_plan": "The planner produced an invalid plan that could not be repaired",
    "planner_error": "The planning subsystem encountered an error",
    "out_of_scope": "The user request was determined to be out of scope",
    "terminal_error_policy_violation": "A policy violation terminated execution",
    "terminal_error_internal": "An unrecoverable internal error occurred",
    # The two verification terminals are deliberately distinct. The first is
    # evidence; the second is the absence of it, and an operator acts
    # differently on each (§11.4).
    "verification_failed": (
        "Independent read-back proved the requested effect did not hold, and repeating the "
        "call could not safely correct it; the effect is unconfirmed"
    ),
    "verification_unconfirmed": (
        "The effect could not be confirmed after repeated read-back attempts — no evidence "
        "of failure, only an inability to check; the effect is unconfirmed"
    ),
    "operator_cancelled": "The run was cancelled by an operator",
    "cancelled": "The run was cancelled by an operator",
    "stale_error": "Recovery received a stale error not matching the current step",
    "missing_error": "Recovery invoked without an error recorded",
    "missing_current_step": "Recovery invoked without a current step",
    "terminal_run": "Run is already in a terminal state",
}


def sanitize_text(text: str) -> str:
    """Scrub sensitive credentials, tokens, or headers from operator-facing text."""
    sanitized = text
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub(r"\1[REDACTED]", sanitized)
    return sanitized


def format_failure_explanation(reason: str, errors: list[AgentError] | None = None) -> str:
    """Produce a safe, concise explanation for a machine-readable failure reason."""
    base_explanation = REASON_EXPLANATIONS.get(reason, f"Failure code: {reason}")
    if errors:
        last_error = errors[-1]
        msg = sanitize_text(last_error.message.split("\n")[0].strip())
        if msg and msg not in base_explanation:
            return f"{base_explanation} ({last_error.error_class.value}: {msg})"
    return base_explanation


def is_required_step_rejected(
    plan: Plan | None,
    approval_state: ApprovalState | None,
) -> tuple[bool, list[str]]:
    """Determine whether any required step was rejected, returning the rejected step IDs."""
    app_state = approval_state or ApprovalState()
    rejected_ids: list[str] = []

    if plan is not None and plan.steps:
        for s in plan.steps:
            if not s.optional and (
                s.status == StepStatus.REJECTED or app_state.rejected(s.step_id)
            ):
                rejected_ids.append(s.step_id)
    elif app_state.decisions:
        for step_id, dec in app_state.decisions.items():
            if dec.decision == ApprovalDecisionKind.REJECT:
                rejected_ids.append(step_id)

    return (len(rejected_ids) > 0, rejected_ids)


def _is_unconfirmed(
    step_id: str,
    verification_result: dict[str, VerificationResult],
) -> bool:
    """Invariant P5: An unverified effect is reported as unconfirmed and never as done."""
    ver = verification_result.get(step_id)
    return ver is not None and ver.status in (
        VerificationStatus.UNCONFIRMED,
        VerificationStatus.FAILED,
    )


def _is_outbound_tool(tool_name: ToolName | None) -> bool:
    """Check if tool has outbound side effects (e.g., sending emails)."""
    if tool_name is None:
        return False
    if tool_name == ToolName.SEND_EMAIL_MOCK:
        return True
    contract = REGISTRY.get(tool_name)
    return contract is not None and contract.side_effect == SideEffect.OUTBOUND


def _categorize_steps(
    plan: Plan | None,
    tool_results: dict[str, ToolResult],
    verification_result: dict[str, VerificationResult],
    approval_state: ApprovalState | None,
    errors: list[AgentError] | None = None,
    failing_step_id: str | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    done: list[str] = []
    not_done: list[str] = []
    unconfirmed: list[str] = []
    pending: list[str] = []

    if plan is None or not plan.steps:
        return done, not_done, unconfirmed, pending

    app_state = approval_state or ApprovalState()
    error_step_ids = {e.step_id for e in (errors or []) if e.step_id is not None}
    if failing_step_id:
        error_step_ids.add(failing_step_id)

    for step in plan.steps:
        sid = step.step_id

        # 1. Unconfirmed check (Invariant P5: never fold into done)
        if _is_unconfirmed(sid, verification_result):
            unconfirmed.append(sid)
            continue

        # 2. Succeeded check
        if step.status == StepStatus.SUCCEEDED or (
            sid in tool_results and step.status != StepStatus.FAILED and sid not in error_step_ids
        ):
            done.append(sid)
            continue

        # 3. Explicit not-done statuses
        if (
            step.status in (StepStatus.REJECTED, StepStatus.FAILED, StepStatus.SKIPPED)
            or app_state.rejected(sid)
            or sid in error_step_ids
        ):
            not_done.append(sid)
            continue

        # 4. Planned but unrun
        pending.append(sid)

    return done, not_done, unconfirmed, pending


def synthesize_complete_response(state: AgentState) -> dict[str, Any]:
    """Synthesize terminal response for the `complete` node (§7)."""
    plan = state.get("plan")
    approval_state = state.get("approval_state")
    tool_results = state.get("tool_results") or {}
    verification_result = state.get("verification_result") or {}
    errors = state.get("errors") or []

    has_rejection, rejected_ids = is_required_step_rejected(plan, approval_state)

    done, not_done, unconfirmed, pending = _categorize_steps(
        plan=plan,
        tool_results=tool_results,
        verification_result=verification_result,
        approval_state=approval_state,
        errors=errors,
    )

    # CASE 1 — REQUIRED APPROVAL REJECTED
    if has_rejection:
        for rid in rejected_ids:
            if rid not in not_done and rid not in unconfirmed:
                not_done.append(rid)
            if rid in pending:
                pending.remove(rid)

        summary_parts = [
            f"Execution stopped: required step approval rejected by operator "
            f"({', '.join(rejected_ids)})."
        ]
        if done:
            summary_parts.append(f"Completed before rejection: {', '.join(done)}.")
        if not_done:
            summary_parts.append(f"Not done: {', '.join(not_done)}.")
        if unconfirmed:
            summary_parts.append(f"Unconfirmed: {', '.join(unconfirmed)}.")
        if pending:
            summary_parts.append(f"Unrun: {', '.join(pending)}.")

        return {
            "status": RunStatus.REJECTED,
            "status_reason": "approval_rejected",
            "final_response": FinalResponse(
                summary=" ".join(summary_parts),
                done=done,
                not_done=not_done,
                unconfirmed=unconfirmed,
                pending=pending,
                partial=False,
            ),
        }

    # CASE 3 — PARTIAL COMPLETION (Optional steps skipped)
    skipped_optional = [
        s.step_id
        for s in (plan.steps if plan else [])
        if s.optional and s.status == StepStatus.SKIPPED
    ]
    partial = len(skipped_optional) > 0

    summary_parts = []
    if partial:
        summary_parts.append(
            f"Run completed with partial execution: {len(done)} step(s) completed "
            f"({', '.join(done)}), {len(skipped_optional)} optional step(s) skipped "
            f"({', '.join(skipped_optional)})."
        )
    elif unconfirmed:
        summary_parts.append(
            f"Run completed with {len(done)} step(s) completed ({', '.join(done)}), "
            f"but effect(s) for {', '.join(unconfirmed)} could not be confirmed."
        )
    else:
        summary_parts.append(
            f"All planned operations completed successfully: {len(done)} step(s) completed"
            + (f" ({', '.join(done)})." if done else ".")
        )

    if unconfirmed:
        outbound_unconfirmed = [
            uid
            for uid in unconfirmed
            if plan and plan.step(uid) and _is_outbound_tool(plan.step(uid).tool)  # type: ignore[union-attr]
        ]
        if outbound_unconfirmed:
            summary_parts.append(
                f"Outbound effect for step(s) {', '.join(outbound_unconfirmed)} is unconfirmed "
                f"— check the outbox before retrying."
            )
        else:
            summary_parts.append(f"Unconfirmed step(s): {', '.join(unconfirmed)}.")

    if not_done and not partial:
        summary_parts.append(f"Not done: {', '.join(not_done)}.")
    if pending:
        summary_parts.append(f"Pending: {', '.join(pending)}.")

    return {
        "status": RunStatus.COMPLETED,
        "status_reason": None,
        "final_response": FinalResponse(
            summary=" ".join(summary_parts),
            done=done,
            not_done=not_done,
            unconfirmed=unconfirmed,
            pending=pending,
            partial=partial,
        ),
    }


def synthesize_fail_response(state: AgentState) -> dict[str, Any]:
    """Synthesize terminal response for the `fail` node (§7, §10.6)."""
    existing_reason = state.get("status_reason")
    errors = state.get("errors") or []
    plan = state.get("plan")
    approval_state = state.get("approval_state")
    tool_results = state.get("tool_results") or {}
    verification_result = state.get("verification_result") or {}
    current_step_id = state.get("current_step_id")

    if existing_reason and existing_reason != "keep":
        reason = existing_reason
    elif errors:
        last_err = errors[-1]
        reason = str(last_err.error_class.value)
    else:
        reason = "unspecified_failure"

    done, not_done, unconfirmed, pending = _categorize_steps(
        plan=plan,
        tool_results=tool_results,
        verification_result=verification_result,
        approval_state=approval_state,
        errors=errors,
        failing_step_id=current_step_id,
    )

    explanation = format_failure_explanation(reason, errors)
    summary_parts = [f"Run failed ({reason}): {explanation}."]

    if done:
        summary_parts.append(f"Completed before failure: {', '.join(done)}.")
    if not_done:
        summary_parts.append(f"Not done: {', '.join(not_done)}.")
    if unconfirmed:
        outbound_unconfirmed = [
            uid
            for uid in unconfirmed
            if plan and plan.step(uid) and _is_outbound_tool(plan.step(uid).tool)  # type: ignore[union-attr]
        ]
        if outbound_unconfirmed:
            summary_parts.append(
                f"Outbound effect for step(s) {', '.join(outbound_unconfirmed)} is unconfirmed "
                f"— check the outbox before retrying."
            )
        else:
            summary_parts.append(f"Unconfirmed step(s): {', '.join(unconfirmed)}.")
    if pending:
        summary_parts.append(f"Unrun: {', '.join(pending)}.")

    return {
        "status": RunStatus.FAILED,
        "status_reason": reason,
        "final_response": FinalResponse(
            summary=" ".join(summary_parts),
            done=done,
            not_done=not_done,
            unconfirmed=unconfirmed,
            pending=pending,
            partial=False,
        ),
    }
