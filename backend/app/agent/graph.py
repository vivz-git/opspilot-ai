"""Production LangGraph execution graph assembly (§6, ADR-001, ADR-007).

Wires all 9 nodes, entry/exit points, static edges, and conditional edges
exactly matching docs/architecture.md §6.1. Compiles with the DB-007 checkpointer
and enforces dynamic `interrupt()` in `request_approval` with empty static interrupt lists.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.nodes import NodeHandlers
from app.agent.normalizer import TaskNormalizer
from app.agent.planner import Planner
from app.agent.state import AgentState, PlanStep
from app.persistence.protocols import UnitOfWorkFactory
from app.runtime import Clock, IdGenerator, SeededRandom
from app.tools.registry import ToolRegistry

__all__ = [
    "create_agent_graph",
]


def create_agent_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    *,
    registry: ToolRegistry | None = None,
    uow_factory: UnitOfWorkFactory | None = None,
    clock: Clock | None = None,
    id_gen: IdGenerator | None = None,
    normalizer: TaskNormalizer | None = None,
    planner: Planner | None = None,
    arg_resolver: Callable[[AgentState, PlanStep], dict[str, Any]] | None = None,
    node_handlers: NodeHandlers | None = None,
    retry_base_delay_ms: int = 250,
    retry_max_delay_ms: int = 8_000,
    seeded_random: SeededRandom | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> CompiledStateGraph[AgentState, Any, Any, Any]:
    """Assemble and compile the production LangGraph agent graph (§6.1)."""
    handlers = node_handlers or NodeHandlers(
        registry=registry,
        uow_factory=uow_factory,
        clock=clock,
        id_gen=id_gen,
        normalizer=normalizer,
        planner=planner,
        arg_resolver=arg_resolver,
        retry_base_delay_ms=retry_base_delay_ms,
        retry_max_delay_ms=retry_max_delay_ms,
        seeded_random=seeded_random,
        sleep=sleep,
    )

    builder: StateGraph[AgentState] = StateGraph(AgentState)

    # 1. Register the 9 nodes (§7)
    builder.add_node("understand", handlers.understand)
    builder.add_node("plan", handlers.plan)
    builder.add_node("decide", handlers.decide)
    builder.add_node("request_approval", handlers.request_approval)
    builder.add_node("execute_tool", handlers.execute_tool)
    builder.add_node("verify", handlers.verify)
    builder.add_node("recover", handlers.recover)
    builder.add_node("complete", handlers.complete)
    builder.add_node("fail", handlers.fail)

    # 2. Static edges (§6.1)
    builder.add_edge(START, "understand")
    builder.add_edge("request_approval", "decide")
    builder.add_edge("complete", END)
    builder.add_edge("fail", END)

    # 3. Conditional edges (§6.1, §6.2)
    builder.add_conditional_edges(
        "understand",
        handlers.route_after_understand,
        {"plan": "plan", "fail": "fail"},
    )
    builder.add_conditional_edges(
        "plan",
        handlers.route_after_plan,
        {"decide": "decide", "fail": "fail"},
    )
    builder.add_conditional_edges(
        "decide",
        handlers.route_after_decide,
        {
            "execute_tool": "execute_tool",
            "request_approval": "request_approval",
            "complete": "complete",
            "plan": "plan",
            "fail": "fail",
        },
    )
    builder.add_conditional_edges(
        "execute_tool",
        handlers.route_after_execute,
        {"verify": "verify", "decide": "decide", "recover": "recover"},
    )
    builder.add_conditional_edges(
        "verify",
        handlers.route_after_verify,
        {"decide": "decide", "recover": "recover"},
    )
    builder.add_conditional_edges(
        "recover",
        handlers.route_after_recover,
        {
            "execute_tool": "execute_tool",
            "plan": "plan",
            "decide": "decide",
            "fail": "fail",
        },
    )

    # 4. Compile with checkpointer and no static interrupt lists (ADR-007)
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=[],
        interrupt_after=[],
    )
