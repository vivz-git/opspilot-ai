"""Implementation of update_customer tool (§8.4, TOOL-003)."""

from __future__ import annotations

from app.errors import InternalError
from app.integrations.ports import CustomerPort
from app.tools.registry import ToolContext
from app.tools.schemas import UpdateCustomerInput, UpdateCustomerOutput

__all__ = ["update_customer"]


async def update_customer(args: UpdateCustomerInput, ctx: ToolContext) -> UpdateCustomerOutput:
    """Apply an allowlisted patch to a customer record with optimistic concurrency."""
    if not isinstance(ctx.port, CustomerPort):
        raise InternalError(
            "update_customer requires a CustomerPort",
            detail={"tool": ctx.tool.value, "port": type(ctx.port).__name__},
        )

    # Read pre-update customer state to capture previous values for undo/audit
    existing = await ctx.port.get(customer_id=args.customer_id)

    patch_dict = args.patch.model_dump(exclude_none=True)
    updated_fields = list(patch_dict.keys())
    previous = {field: getattr(existing, field, None) for field in updated_fields}

    updated = await ctx.port.update(
        args.customer_id,
        args.patch,
        expected_version=args.expected_version,
        token=args.approval_token,
        idempotency_key=args.idempotency_key,
    )

    return UpdateCustomerOutput(
        customer_id=updated.customer_id,
        version=updated.version,
        updated_fields=updated_fields,
        previous=previous,
        updated_at=updated.updated_at,
    )
