"""Implementation of get_customer tool (§8.4, TOOL-003)."""

from __future__ import annotations

from app.errors import InternalError
from app.integrations.ports import CustomerPort
from app.tools.registry import ToolContext
from app.tools.schemas import GetCustomerInput, GetCustomerOutput

__all__ = ["get_customer"]


async def get_customer(args: GetCustomerInput, ctx: ToolContext) -> GetCustomerOutput:
    """Fetch a customer record by ID or email."""
    if not isinstance(ctx.port, CustomerPort):
        raise InternalError(
            "get_customer requires a CustomerPort",
            detail={"tool": ctx.tool.value, "port": type(ctx.port).__name__},
        )

    customer = await ctx.port.get(
        customer_id=args.customer_id,
        email=str(args.email) if args.email else None,
    )
    return GetCustomerOutput(customer=customer)
