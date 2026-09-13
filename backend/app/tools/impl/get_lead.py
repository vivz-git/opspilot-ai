"""Implementation of get_lead tool (§8.4, TOOL-003)."""

from __future__ import annotations

from app.errors import InternalError
from app.integrations.ports import LeadPort
from app.tools.registry import ToolContext
from app.tools.schemas import GetLeadInput, GetLeadOutput

__all__ = ["get_lead"]


async def get_lead(args: GetLeadInput, ctx: ToolContext) -> GetLeadOutput:
    """Fetch one lead by id."""
    if not isinstance(ctx.port, LeadPort):
        raise InternalError(
            "get_lead requires a LeadPort",
            detail={"tool": ctx.tool.value, "port": type(ctx.port).__name__},
        )

    lead = await ctx.port.get(args.lead_id)
    return GetLeadOutput(lead=lead)
