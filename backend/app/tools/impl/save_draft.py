"""Implementation of save_draft tool (§8.4, TOOL-003)."""

from __future__ import annotations

import hashlib

from app.errors import InternalError, PolicyViolation
from app.integrations.ports import DraftInput, DraftPort
from app.tools.registry import ToolContext
from app.tools.schemas import SaveDraftInput, SaveDraftOutput

__all__ = ["save_draft"]


async def save_draft(args: SaveDraftInput, ctx: ToolContext) -> SaveDraftOutput:
    """Persist generated copy as a durable, addressable draft."""
    if not isinstance(ctx.port, DraftPort):
        raise InternalError(
            "save_draft requires a DraftPort",
            detail={"tool": ctx.tool.value, "port": type(ctx.port).__name__},
        )

    # Security check (§8.4): verify content_hash matches subject and body
    payload = f"{args.subject}\n\n{args.body}"
    expected_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if args.content_hash != expected_hash:
        raise PolicyViolation(
            "content_hash does not match the submitted subject/body",
            detail={
                "tool": ctx.tool.value,
                "expected_hash": expected_hash,
                "provided_hash": args.content_hash,
            },
        )

    draft_record = await ctx.port.save(
        DraftInput(
            lead_id=args.lead_id,
            subject=args.subject,
            body=args.body,
            channel=args.channel,
            content_hash=args.content_hash,
            metadata=args.metadata,
        )
    )

    return SaveDraftOutput(
        draft_id=draft_record.draft_id,
        version=draft_record.version,
        status=draft_record.status,
        content_hash=draft_record.content_hash,
        saved_at=draft_record.saved_at,
    )
