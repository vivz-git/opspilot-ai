"""Planner selection from `Settings` (§4.2, §17.3, ADR-002).

`OPSPILOT_PLANNER=rules` → `RulePlanner`. `llm` → `LLMPlanner` over Groq
(`validate_runtime` has already refused to start without a key). `auto` →
the LLM planner when `GROQ_API_KEY` is set, with the rule planner as its
in-run fallback for provider failures; otherwise the rule planner, and the
degradation is logged once, here, at composition time.
"""

from __future__ import annotations

from collections.abc import Mapping

import structlog

from app.agent.planner.groq import GroqStructuredClient
from app.agent.planner.llm import LLMPlanner
from app.agent.planner.protocol import Planner
from app.agent.planner.rules import RulePlanner
from app.agent.state import PlannerKind
from app.config import PlannerMode, Settings
from app.errors import ConfigurationError
from app.tools.contracts import REGISTRY, ToolContract, ToolName

__all__ = ["build_planner"]

_log = structlog.get_logger("opspilot.agent.planner")


def build_planner(
    settings: Settings, *, contracts: Mapping[ToolName, ToolContract] = REGISTRY
) -> Planner:
    rules = RulePlanner(contracts)
    if settings.effective_planner is PlannerKind.RULES:
        if settings.planner is PlannerMode.AUTO:
            _log.info("planner_selected", planner="rules", reason="no GROQ_API_KEY configured")
        else:
            _log.info("planner_selected", planner="rules", reason="OPSPILOT_PLANNER=rules")
        return rules
    if settings.groq_api_key is None:  # pragma: no cover - validate_runtime refuses this
        raise ConfigurationError("OPSPILOT_PLANNER=llm requires GROQ_API_KEY")
    client = GroqStructuredClient(
        api_key=settings.groq_api_key,
        model=settings.groq_model,
        base_url=settings.groq_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    _log.info(
        "planner_selected",
        planner="llm",
        model=settings.groq_model,
        fallback="rules" if settings.planner is PlannerMode.AUTO else None,
    )
    return LLMPlanner(
        client,
        model_id=settings.groq_model,
        contracts=contracts,
        fallback=rules if settings.planner is PlannerMode.AUTO else None,
    )
