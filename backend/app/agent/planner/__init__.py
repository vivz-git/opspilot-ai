"""The planner layer: `RulePlanner` | `LLMPlanner` behind one protocol (§4.2, ADR-002).

    normalized_task → Planner.plan → validate_plan → Plan → decide

Planning and execution are separated by construction: nothing in this package
imports the tool registry's dispatcher, a port, a repository or the graph.
The only I/O-capable module is `groq.py`, the provider transport, which the
composition root (`factory.build_planner`) injects into `LLMPlanner`.
"""

from __future__ import annotations

from app.agent.planner.context import (
    REVISION_REASONS,
    STATUS_REASON_REPLANNABLE_FAULT,
    build_revision_context,
    carry_over_settled_steps,
    revision_requested,
)
from app.agent.planner.llm import LLMPlanner, LLMProviderError, StructuredCompletionClient
from app.agent.planner.protocol import Planner, PlannerIdentity, PlanRevisionContext
from app.agent.planner.rules import RulePlanner
from app.agent.planner.validation import (
    PlanIssue,
    PlanValidationError,
    allowed_tools,
    assert_valid_plan,
    validate_plan,
)

__all__ = [
    "LLMPlanner",
    "LLMProviderError",
    "PlanIssue",
    "PlanRevisionContext",
    "PlanValidationError",
    "Planner",
    "PlannerIdentity",
    "REVISION_REASONS",
    "RulePlanner",
    "STATUS_REASON_REPLANNABLE_FAULT",
    "StructuredCompletionClient",
    "allowed_tools",
    "assert_valid_plan",
    "build_revision_context",
    "carry_over_settled_steps",
    "revision_requested",
    "validate_plan",
]
