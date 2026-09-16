"""Verification context and protocol definitions (§11, VERIFY-001).

Verification is post-execution and purely observational. A verifier never
executes tools, never mutates CRM state, never mints approval tokens, and
never calls an LLM.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.agent.state import VerificationResult
from app.integrations.ports import Adapters
from app.persistence.protocols import UnitOfWorkFactory
from app.runtime import Clock
from app.tools.contracts import ToolContract, ToolName
from app.tools.schemas import Customer

__all__ = [
    "VerificationContext",
    "Verifier",
]


@dataclass(frozen=True)
class VerificationContext:
    """The immutable observational context passed to a verifier.

    Carries the requested input arguments (the intent), tool output data,
    idempotency key, integration ports, and runtime dependencies.
    """

    run_id: uuid.UUID
    step_id: str
    tool: ToolName
    attempt: int
    contract: ToolContract
    input_args: dict[str, Any]
    output_data: dict[str, Any]
    idempotency_key: str | None = None
    adapters: Adapters | None = None
    uow_factory: UnitOfWorkFactory | None = None
    clock: Clock | None = None
    baseline_customer: Customer | None = None
    prior_tool_results: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Verifier(Protocol):
    """Protocol for contract-driven postcondition verifiers (§11.1, VERIFY-001)."""

    async def verify(self, ctx: VerificationContext) -> VerificationResult:
        """Evaluate expected postconditions against observed state.

        Must be read-only, deterministic, and free of side effects.
        """
        ...
