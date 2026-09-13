"""Implementation of send_email_mock tool (§8.4, TOOL-003)."""

from __future__ import annotations

from app.errors import InternalError
from app.integrations.ports import MailPort, OutboundMessage
from app.tools.registry import ToolContext
from app.tools.schemas import SendEmailMockInput, SendEmailMockOutput

__all__ = ["send_email_mock"]


async def send_email_mock(args: SendEmailMockInput, ctx: ToolContext) -> SendEmailMockOutput:
    """Record an outbound email in the mock outbox. Never sends real mail."""
    if not isinstance(ctx.port, MailPort):
        raise InternalError(
            "send_email_mock requires a MailPort",
            detail={"tool": ctx.tool.value, "port": type(ctx.port).__name__},
        )

    receipt = await ctx.port.send(
        OutboundMessage(draft_id=args.draft_id, to_email=args.to_email),
        token=args.approval_token,
        idempotency_key=args.idempotency_key,
    )

    return SendEmailMockOutput(
        message_id=receipt.message_id,
        outbox_id=receipt.outbox_id,
        status=receipt.status,
        provider=receipt.provider,
        to_email=receipt.to_email,
        draft_id=receipt.draft_id,
        sent_at=receipt.sent_at,
    )
