"""Node handlers and execution boundaries for the LangGraph agent graph (§6, §7).

Every node is `async (state) -> dict` returning a partial state delta.
Nodes never mutate `AgentState` in place; declared reducers own composition (§5.3).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import structlog
from langgraph.types import interrupt

from app.agent.state import (
    AgentError,
    AgentState,
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalState,
    FinalResponse,
    NormalizedTask,
    Plan,
    PlanStep,
    RunMetadata,
    RunStatus,
    StepStatus,
    ToolCall,
    ToolResult,
    VerificationResult,
    VerificationStatus,
)
from app.errors import (
    ErrorClass,
    PolicyViolation,
    RecoveryAction,
    recovery_action,
)
from app.persistence.protocols import UnitOfWorkFactory
from app.runtime import Clock, IdGenerator, SystemClock, UuidIdGenerator
from app.security import ApprovalToken, canonical_args_hash
from app.tools.contracts import REGISTRY, ToolContract, ToolName, VerificationMode
from app.tools.registry import ApprovalRequiredError, ToolRegistry

__all__ = [
    "NodeHandlers",
    "create_initial_state",
]

_log = structlog.get_logger("opspilot.agent.nodes")


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
        token_issuer: Callable[[str, str, dict[str, Any]], ApprovalToken | None] | None = None,
        arg_resolver: Callable[[AgentState, PlanStep], dict[str, Any]] | None = None,
        understand_handler: Callable[[AgentState], Awaitable[dict[str, Any]]] | None = None,
        plan_handler: Callable[[AgentState], Awaitable[dict[str, Any]]] | None = None,
        verify_handler: Callable[[AgentState], Awaitable[dict[str, Any]]] | None = None,
        recover_handler: Callable[[AgentState], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._registry = registry
        self._uow_factory = uow_factory
        self._clock = clock or SystemClock()
        self._id_gen = id_gen or UuidIdGenerator()
        self._token_issuer = token_issuer
        self._arg_resolver = arg_resolver
        self._understand_handler = understand_handler
        self._plan_handler = plan_handler
        self._verify_handler = verify_handler
        self._recover_handler = recover_handler

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------
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
        # Default resolver: resolves dotted strings like 's1.output.lead_id'
        tool_results = state.get("tool_results", {})
        raw_args = dict(step.args)
        resolved: dict[str, Any] = {}
        for k, v in raw_args.items():
            if isinstance(v, str) and ".output" in v:
                parts = v.split(".")
                step_ref = parts[0]
                if step_ref in tool_results:
                    curr: Any = tool_results[step_ref].output
                    for p in parts[2:]:
                        if isinstance(curr, dict) and p in curr:
                            curr = curr[p]
                        elif isinstance(curr, list) and p.isdigit():
                            curr = curr[int(p)]
                    resolved[k] = curr
                else:
                    resolved[k] = v
            else:
                resolved[k] = v
        return resolved

    def _find_runnable_step(self, state: AgentState, plan: Plan | None) -> PlanStep | None:
        if plan is None or not plan.steps:
            return None
        current_id = state.get("current_step_id")
        if current_id is not None:
            current_step = plan.step(current_id)
            if current_step and current_step.status not in (
                StepStatus.SUCCEEDED,
                StepStatus.SKIPPED,
                StepStatus.REJECTED,
                StepStatus.FAILED,
            ):
                return current_step
        for s in plan.steps:
            if s.status in (StepStatus.PENDING, StepStatus.READY):
                return s
        return None

    def _dependencies_satisfied(self, state: AgentState, plan: Plan, step: PlanStep) -> bool:
        tool_results = state.get("tool_results", {})
        for dep_id in step.depends_on:
            dep = plan.step(dep_id)
            if dep is None:
                return False
            if dep.status != StepStatus.SUCCEEDED and dep_id not in tool_results:
                return False
        return True

    # ---------------------------------------------------------------------------
    # 1. understand
    # ---------------------------------------------------------------------------
    async def understand(self, state: AgentState) -> dict[str, Any]:
        if self._understand_handler is not None:
            return await self._understand_handler(state)
        task = state.get("normalized_task")
        if task is None:
            user_req = state.get("user_request", "")
            in_scope = (
                "out of scope" not in user_req.lower()
                and state.get("status_reason") != "out_of_scope"
            )
            task = NormalizedTask(intent=user_req, in_scope=in_scope)
        return {
            "normalized_task": task,
            "status": RunStatus.RUNNING,
            "status_reason": None if task.in_scope else "out_of_scope",
        }

    def route_after_understand(self, state: AgentState) -> str:
        task = state.get("normalized_task")
        if task is not None and task.in_scope:
            return "plan"
        return "fail"

    # ---------------------------------------------------------------------------
    # 2. plan
    # ---------------------------------------------------------------------------
    async def plan(self, state: AgentState) -> dict[str, Any]:
        if self._plan_handler is not None:
            return await self._plan_handler(state)
        existing_plan = state.get("plan")
        if existing_plan is not None:
            replan_count = state.get("replan_count", 0) + 1
            return {
                "plan": existing_plan,
                "plan_history": [existing_plan],
                "replan_count": replan_count,
            }
        meta_extra = state.get("metadata", RunMetadata()).extra
        if "plan" in meta_extra and isinstance(meta_extra["plan"], Plan):
            return {"plan": meta_extra["plan"], "replan_count": 0}
        plan_id = f"p_{self._id_gen.new_id()[:8]}"
        new_plan = Plan(plan_id=plan_id, revision=0, steps=[])
        return {"plan": new_plan, "replan_count": 0}

    def route_after_plan(self, state: AgentState) -> str:
        budgets = state.get("metadata", RunMetadata()).budgets
        replan_count = state.get("replan_count", 0)
        if replan_count > budgets.max_replans:
            return "fail"
        plan_obj = state.get("plan")
        if plan_obj is None or state.get("status_reason") == "invalid_plan":
            return "fail"
        return "decide"

    # ---------------------------------------------------------------------------
    # 3. decide
    # ---------------------------------------------------------------------------
    async def decide(self, state: AgentState) -> dict[str, Any]:
        budgets = state.get("metadata", RunMetadata()).budgets
        now = self._clock.now()
        deadline = state.get("deadline_at")
        if (deadline is not None and now > deadline) or (
            state.get("step_count", 0) >= budgets.max_steps
        ):
            return {"status_reason": "budget_exhausted"}
        plan = state.get("plan")
        if plan is None:
            return {"current_step_id": None}
        runnable = self._find_runnable_step(state, plan)
        if runnable is None:
            return {"current_step_id": None}
        if not self._dependencies_satisfied(state, plan, runnable):
            if state.get("replan_count", 0) >= budgets.max_replans:
                return {"status_reason": "unresolvable_plan", "current_step_id": runnable.step_id}
            return {"status_reason": "replan_required", "current_step_id": runnable.step_id}
        return {"current_step_id": runnable.step_id}

    def route_after_decide(self, state: AgentState) -> str:
        # Rule 1: deadline_at passed, or step_count >= MAX_STEPS -> fail(budget_exhausted)
        budgets = state.get("metadata", RunMetadata()).budgets
        now = self._clock.now()
        deadline = state.get("deadline_at")
        if deadline is not None and now > deadline:
            return "fail"
        if state.get("step_count", 0) >= budgets.max_steps:
            return "fail"

        # Rule 2: a pending approval exists and is undecided -> request_approval (re-pause)
        approval_state = state.get("approval_state")
        if (
            approval_state is not None
            and approval_state.pending is not None
            and approval_state.pending.step_id not in approval_state.decisions
        ):
            return "request_approval"

        # Rule 3: no runnable step remains -> complete
        plan = state.get("plan")
        if plan is None:
            return "complete"
        runnable = self._find_runnable_step(state, plan)
        if runnable is None:
            return "complete"

        # Rule 4: next step's dependencies unsatisfied/unresolvable
        # and replan_count < MAX_REPLANS -> plan, else -> fail(unresolvable_plan)
        if not self._dependencies_satisfied(state, plan, runnable):
            if state.get("replan_count", 0) < budgets.max_replans:
                return "plan"
            return "fail"

        # Rule 5: next step's fanout is unexpanded -> plan/fail if unhandled in AGENT-002
        if runnable.fanout is not None:
            if state.get("replan_count", 0) < budgets.max_replans:
                return "plan"
            return "fail"

        # Rule 6: contract.requires_approval(step) AND NOT approval_state.grants(step)
        # -> request_approval
        contract = self._get_contract(runnable.tool)
        if contract and contract.requires_approval:
            resolved_args = self._resolve_step_args(state, runnable)
            if approval_state is None or not approval_state.grants(runnable.step_id, resolved_args):
                return "request_approval"

        # Rule 7: otherwise -> execute_tool
        return "execute_tool"

    # ---------------------------------------------------------------------------
    # 4. request_approval
    # ---------------------------------------------------------------------------
    async def request_approval(self, state: AgentState) -> dict[str, Any]:
        current_step_id = state.get("current_step_id")
        plan = state.get("plan")
        step = plan.step(current_step_id) if plan and current_step_id else None
        if step is None:
            return {"status": RunStatus.FAILED, "status_reason": "no_step_for_approval"}

        resolved_args = self._resolve_step_args(state, step)
        args_hash = canonical_args_hash(resolved_args)
        run_id_str = str(state.get("run_id", "default_run"))
        approval_id = f"appr_{run_id_str[:8]}_{current_step_id}"

        approval_state = state.get("approval_state")
        if approval_state and current_step_id in approval_state.decisions:
            # Already decided upon re-execution
            return {"status": RunStatus.RUNNING}

        # Dynamic interruption point: execution pauses here, no tools invoked!
        interrupted_val = interrupt(
            {
                "approval_id": approval_id,
                "run_id": run_id_str,
                "step_id": current_step_id,
                "tool": step.tool.value,
                "args_hash": args_hash,
                "payload_preview": resolved_args,
            }
        )

        # Resumed execution continues below
        decision_obj: ApprovalDecision
        if isinstance(interrupted_val, ApprovalDecision):
            decision_obj = interrupted_val
        elif isinstance(interrupted_val, dict):
            dec_kind = ApprovalDecisionKind(interrupted_val.get("decision", "approve"))
            decision_obj = ApprovalDecision(
                approval_id=approval_id,
                step_id=current_step_id,
                decision=dec_kind,
                args_hash=args_hash,
                decided_by=str(interrupted_val.get("decided_by", "operator")),
                decided_at=self._clock.now(),
                reason=interrupted_val.get("reason"),
            )
        else:
            dec_kind = ApprovalDecisionKind(str(interrupted_val))
            decision_obj = ApprovalDecision(
                approval_id=approval_id,
                step_id=current_step_id,
                decision=dec_kind,
                args_hash=args_hash,
                decided_by="operator",
                decided_at=self._clock.now(),
            )

        updated_plan = plan
        if decision_obj.decision == ApprovalDecisionKind.REJECT and plan is not None:
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
                decisions={current_step_id: decision_obj},
            ),
            "plan": updated_plan,
            "status": RunStatus.RUNNING,
        }

    # ---------------------------------------------------------------------------
    # 5. execute_tool
    # ---------------------------------------------------------------------------
    async def execute_tool(self, state: AgentState) -> dict[str, Any]:
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
        resolved_args = self._resolve_step_args(state, step)
        args_hash = canonical_args_hash(resolved_args)
        contract = self._get_contract(step.tool) or REGISTRY[step.tool]
        budgets = state.get("metadata", RunMetadata()).budgets

        # Gate Re-assertion (Barrier 2, §9.5, §16.2)
        if contract.requires_approval:
            approval_state = state.get("approval_state")
            if approval_state is None or not approval_state.grants(current_step_id, resolved_args):
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

        # Issue or retrieve approval token if token issuer is provided
        token: ApprovalToken | None = None
        if contract.requires_approval and self._token_issuer is not None:
            token = self._token_issuer(str(state["run_id"]), current_step_id, resolved_args)

        # The only tool execution path: ToolRegistry.dispatch (§8.5, ADR-024)
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

        try:
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

            updated_steps = []
            for s in plan.steps:
                if s.step_id == current_step_id:
                    updated_steps.append(s.model_copy(update={"status": StepStatus.SUCCEEDED}))
                else:
                    updated_steps.append(s)
            updated_plan = plan.model_copy(update={"steps": updated_steps})

            return {
                "tool_results": {current_step_id: tool_result},
                "tool_calls": [tool_call],
                "plan": updated_plan,
                "step_count": step_count,
            }
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
            agent_err = AgentError(
                step_id=current_step_id,
                error_class=err_class,
                message=str(exc),
                attempt=attempt,
                recovery=rec,
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
        current_step_id = state.get("current_step_id")
        errors = state.get("errors", [])
        if errors and errors[-1].step_id == current_step_id:
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
        current_step_id = state.get("current_step_id") or "unknown"
        res = VerificationResult(
            step_id=current_step_id,
            status=VerificationStatus.PASSED,
            mode="default",
        )
        return {"verification_result": {current_step_id: res}}

    def route_after_verify(self, state: AgentState) -> str:
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
        current_step_id = state.get("current_step_id")
        errors = state.get("errors", [])
        latest_err = errors[-1] if errors else None
        budgets = state.get("metadata", RunMetadata()).budgets
        retries = state.get("retry_count", {})
        current_retries = retries.get(current_step_id, 0) if current_step_id else 0

        plan = state.get("plan")
        step = plan.step(current_step_id) if plan and current_step_id else None

        err_class = latest_err.error_class if latest_err else ErrorClass.INTERNAL
        action = (
            latest_err.recovery
            if latest_err and latest_err.recovery
            else recovery_action(
                err_class,
                idempotent=False,
                nondeterministic=False,
                retries_remaining=max(0, budgets.max_retries - current_retries),
                replans_remaining=max(0, budgets.max_replans - state.get("replan_count", 0)),
                step_optional=step.optional if step else False,
            )
        )

        if action == RecoveryAction.RETRY and current_step_id:
            return {"retry_count": {current_step_id: current_retries + 1}}
        if action == RecoveryAction.REPLAN:
            return {"status_reason": "replannable_fault"}
        if action == RecoveryAction.SKIP and step and step.optional and plan is not None:
            updated_steps = []
            for s in plan.steps:
                if s.step_id == current_step_id:
                    updated_steps.append(s.model_copy(update={"status": StepStatus.SKIPPED}))
                else:
                    updated_steps.append(s)
            return {
                "plan": plan.model_copy(update={"steps": updated_steps}),
                "status_reason": "optional_step_skipped",
            }
        return {"status_reason": "recovery_exhausted"}

    def route_after_recover(self, state: AgentState) -> str:
        current_step_id = state.get("current_step_id")
        errors = state.get("errors", [])
        latest_err = errors[-1] if errors else None
        budgets = state.get("metadata", RunMetadata()).budgets
        retries = state.get("retry_count", {})
        current_retries = retries.get(current_step_id, 0) if current_step_id else 0
        replan_count = state.get("replan_count", 0)

        plan = state.get("plan")
        step = plan.step(current_step_id) if plan and current_step_id else None
        if step and step.optional and state.get("status_reason") == "optional_step_skipped":
            return "decide"

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
        approval_state = state.get("approval_state")
        plan = state.get("plan")
        any_rejected = False
        if approval_state:
            for step_id, dec in approval_state.decisions.items():
                if dec.decision == ApprovalDecisionKind.REJECT:
                    step = plan.step(step_id) if plan else None
                    if not step or not step.optional:
                        any_rejected = True
                        break

        if any_rejected:
            return {
                "status": RunStatus.REJECTED,
                "status_reason": "approval_rejected",
                "final_response": FinalResponse(
                    summary="Run was rejected by operator",
                    not_done=["outreach_sent"],
                ),
            }

        partial = False
        if plan:
            for s in plan.steps:
                if s.status == StepStatus.SKIPPED and s.optional:
                    partial = True

        return {
            "status": RunStatus.COMPLETED,
            "status_reason": None,
            "final_response": FinalResponse(
                summary="All planned operations completed successfully",
                partial=partial,
            ),
        }

    # ---------------------------------------------------------------------------
    # 9. fail
    # ---------------------------------------------------------------------------
    async def fail(self, state: AgentState) -> dict[str, Any]:
        reason = state.get("status_reason")
        if not reason:
            errors = state.get("errors", [])
            reason = str(errors[-1].error_class.value) if errors else "unspecified_failure"
        return {
            "status": RunStatus.FAILED,
            "status_reason": reason,
        }
