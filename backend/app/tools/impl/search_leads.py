"""Implementation of search_leads tool (§8.4, TOOL-003)."""

from __future__ import annotations

from app.errors import InternalError
from app.integrations.ports import LeadFilter, LeadPort
from app.tools.registry import ToolContext
from app.tools.schemas import SearchLeadsInput, SearchLeadsOutput

__all__ = ["search_leads"]


async def search_leads(args: SearchLeadsInput, ctx: ToolContext) -> SearchLeadsOutput:
    """Find candidate leads matching business filters."""
    if not isinstance(ctx.port, LeadPort):
        raise InternalError(
            "search_leads requires a LeadPort",
            detail={"tool": ctx.tool.value, "port": type(ctx.port).__name__},
        )

    filters = LeadFilter(
        industry=args.industry,
        location=args.location,
        min_employees=args.min_employees,
        max_employees=args.max_employees,
        status=args.status,
        query=args.query,
        limit=args.limit,
        offset=args.offset,
    )
    page = await ctx.port.search(filters)
    return SearchLeadsOutput(
        leads=page.leads,
        total_matched=page.total_matched,
        truncated=page.truncated,
    )
