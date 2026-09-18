"""The production graph composition (§2.1, §6.4, ADR-024).

One function builds the graph the way it runs in production: mock adapters
→ `ToolRegistry` → planner → `create_agent_graph` over the Postgres saver,
with the shared clock, id generator and cancellation source. The API
lifespan (`wire_runtime`) and the evaluation runner (§15.5) both call it, so
the suite drives the same graph, registry and gate the HTTP API drives —
never a harness of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.graph import create_agent_graph
from app.agent.planner.factory import build_planner
from app.config import Settings
from app.execution.recovery import LangGraphRunDriver
from app.integrations import build_adapters
from app.persistence.protocols import UnitOfWorkFactory
from app.runtime import CancellationSource, Clock, DeterministicRandom, IdGenerator
from app.tools.contracts import ToolName
from app.tools.registry import ToolImplementation, ToolRegistry

__all__ = ["build_driver"]


def build_driver(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    uow_factory: UnitOfWorkFactory,
    checkpointer: BaseCheckpointSaver[Any],
    clock: Clock,
    ids: IdGenerator,
    cancellation_source: CancellationSource,
    implementations: Mapping[ToolName, ToolImplementation] | None = None,
) -> LangGraphRunDriver:
    """Compose the production graph and wrap it in a `RunDriver`.

    `implementations` overrides the registry's tool bindings; the evaluation
    runner passes the real implementations wrapped by its `FailureInjector`.
    Everything on the path to a mutating port — the registry's gate, the
    stored-decision check, the token — is untouched by that override.
    """
    adapters = build_adapters(
        settings,
        session_factory,
        clock,
        ids,
        DeterministicRandom(settings.seed),
        failure_rate=settings.tool_failure_rate,
    )
    graph = create_agent_graph(
        checkpointer,
        registry=ToolRegistry(
            adapters=adapters,
            uow_factory=uow_factory,
            clock=clock,
            implementations=implementations,
        ),
        uow_factory=uow_factory,
        clock=clock,
        id_gen=ids,
        planner=build_planner(settings),
        retry_base_delay_ms=settings.retry_base_delay_ms,
        retry_max_delay_ms=settings.retry_max_delay_ms,
        seeded_random=DeterministicRandom(settings.seed),
        cancellation_source=cancellation_source,
        approval_ttl=settings.approval_ttl,
    )
    return LangGraphRunDriver(graph)
