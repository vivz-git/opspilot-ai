"""`LLMPlanner`: model-proposed plans behind deterministic validation (§4.2, §16.3, ADR-002).

The model is untrusted structurally. It may *propose*; it never authorises.
Every response is parsed into `ProposedPlan` by Pydantic, converted into a
`Plan` and validated against the registry, the intent allowlist and the
budgets. A rejected proposal earns exactly one repair turn, in which the model
sees its own response and the deterministic issues; a second rejection is a
terminal `PlanValidationError`. The provider being unreachable, or answering
with something that is not a plan at all after that one repair, is a
`LLMProviderError`; in `auto` mode a configured fallback (the rule planner)
takes over for that plan (§10.1: `PLANNER_ERROR` → degrade), otherwise the
error surfaces.

The provider is behind `StructuredCompletionClient`, a one-method protocol.
This module knows nothing about HTTP, API keys or vendors; `groq.py` does.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import structlog

from app.agent.planner.prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    render_planning_request,
    render_repair_request,
)
from app.agent.planner.protocol import Planner, PlannerIdentity, PlanRevisionContext
from app.agent.planner.rules import plan_id_for
from app.agent.planner.schema import (
    PLAN_SCHEMA_NAME,
    parse_proposal,
    proposal_to_plan,
    response_json_schema,
)
from app.agent.planner.validation import (
    PlanIssue,
    PlanValidationError,
    allowed_tools,
    validate_plan,
)
from app.agent.state import Budgets, NormalizedTask, Plan, PlannerKind
from app.errors import PlannerError
from app.tools.contracts import REGISTRY, ToolContract, ToolName

__all__ = [
    "LLMPlanner",
    "LLMProviderError",
    "StructuredCompletionClient",
]

_log = structlog.get_logger("opspilot.agent.planner.llm")


class LLMProviderError(PlannerError):
    """The provider could not be reached, refused the request, or returned
    nothing that could be read as a plan (§10.1 `PLANNER_ERROR`)."""


class StructuredCompletionClient(Protocol):
    """The whole provider surface the planner needs: one structured completion."""

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> str:
        """Return the raw JSON text of a completion constrained to `schema`.
        Raises `LLMProviderError` on any transport or provider failure."""
        ...


class LLMPlanner:
    def __init__(
        self,
        client: StructuredCompletionClient,
        *,
        model_id: str,
        contracts: Mapping[ToolName, ToolContract] = REGISTRY,
        fallback: Planner | None = None,
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        self._client = client
        self._model_id = model_id
        self._contracts = contracts
        self._fallback = fallback
        self._prompt_version = prompt_version
        self._schema = response_json_schema()

    @property
    def identity(self) -> PlannerIdentity:
        return PlannerIdentity(
            kind=PlannerKind.LLM, model_id=self._model_id, prompt_version=self._prompt_version
        )

    async def plan(
        self,
        task: NormalizedTask,
        prior: PlanRevisionContext | None = None,
        *,
        budgets: Budgets | None = None,
    ) -> Plan:
        budgets = budgets or Budgets()
        if not task.in_scope:
            raise PlannerError("cannot plan an out-of-scope task", detail={"intent": task.intent})
        try:
            return await self._propose_with_one_repair(task, prior, budgets)
        except PlanValidationError:
            raise
        except LLMProviderError as exc:
            if self._fallback is None:
                raise
            _log.warning(
                "planner_degraded",
                planner="llm",
                fallback=self._fallback.identity.kind.value,
                reason=exc.message,
            )
            return await self._fallback.plan(task, prior, budgets=budgets)

    # ------------------------------------------------------------------
    # The bounded sequence: propose → validate → (one repair → validate)
    # ------------------------------------------------------------------
    async def _propose_with_one_repair(
        self,
        task: NormalizedTask,
        prior: PlanRevisionContext | None,
        budgets: Budgets,
    ) -> Plan:
        revision = prior.previous.revision + 1 if prior is not None else 0
        request = render_planning_request(
            task,
            contracts=self._contracts,
            allowed=allowed_tools(task),
            budgets=budgets,
            prior=prior,
        )

        first = await self._complete(request)
        plan, issues, parsed = self._evaluate(first, task, budgets, revision)
        if plan is not None:
            return plan
        _log.info("plan_rejected", attempt=1, issues=[i.code for i in issues])

        repaired = await self._complete(render_repair_request(request, first, issues))
        plan, issues, parsed = self._evaluate(repaired, task, budgets, revision)
        if plan is not None:
            return plan
        _log.info("plan_rejected", attempt=2, issues=[i.code for i in issues])
        if not parsed:
            raise LLMProviderError(
                "the model returned no readable plan after one repair attempt",
                detail={"issues": [i.model_dump() for i in issues]},
            )
        raise PlanValidationError(
            issues, message="the proposed plan is still invalid after one repair attempt"
        )

    async def _complete(self, user: str) -> str:
        try:
            return await self._client.complete_json(
                system=SYSTEM_PROMPT,
                user=user,
                schema_name=PLAN_SCHEMA_NAME,
                schema=self._schema,
            )
        except LLMProviderError:
            raise
        except Exception as exc:  # a client bug is still "the provider failed"
            raise LLMProviderError(
                f"planner client failed: {type(exc).__name__}", detail={"error": str(exc)[:500]}
            ) from exc

    def _evaluate(
        self,
        raw: str,
        task: NormalizedTask,
        budgets: Budgets,
        revision: int,
    ) -> tuple[Plan | None, list[PlanIssue], bool]:
        """Parse, convert, validate. Returns `(plan, issues, parsed)` where
        `parsed` says whether the response was at least a well-formed proposal."""
        proposal, issues = parse_proposal(raw)
        if proposal is None:
            return None, issues, False
        plan, issues = proposal_to_plan(
            proposal,
            plan_id=plan_id_for(revision),
            revision=revision,
            created_by=PlannerKind.LLM,
            contracts=self._contracts,
        )
        if plan is None:
            return None, issues, True
        issues = validate_plan(plan, task=task, contracts=self._contracts, budgets=budgets)
        if issues:
            return None, issues, True
        return plan, [], True
