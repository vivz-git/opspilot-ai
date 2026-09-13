"""Implementation of draft_outreach tool (§8.4, TOOL-003)."""

from __future__ import annotations

from app.errors import InternalError, OutputValidationError
from app.integrations.ports import ContentPort, OutreachBrief
from app.tools.registry import ToolContext
from app.tools.schemas import DraftOutreachInput, DraftOutreachOutput

__all__ = ["draft_outreach"]

_FORBIDDEN_PLACEHOLDERS = ("{{", "TODO", "[NAME]")


async def draft_outreach(args: DraftOutreachInput, ctx: ToolContext) -> DraftOutreachOutput:
    """Generate personalized outreach copy."""
    if not isinstance(ctx.port, ContentPort):
        raise InternalError(
            "draft_outreach requires a ContentPort",
            detail={"tool": ctx.tool.value, "port": type(ctx.port).__name__},
        )

    brief = OutreachBrief(
        lead_id=args.lead_id,
        lead_name=args.lead_id,
        company_name=args.company.name,
        tone=args.tone.value,
        max_words=args.max_words,
        company_summary=args.company.summary,
        recent_signals=[s.summary for s in args.company.recent_signals],
    )

    draft_content = await ctx.port.draft(brief)

    # Output guards (§8.4)
    subject = draft_content.subject
    body = draft_content.body
    word_count = draft_content.word_count

    if not subject or not subject.strip():
        raise OutputValidationError(
            "Draft outreach produced an empty subject",
            detail={"tool": ctx.tool.value, "lead_id": args.lead_id},
        )
    if not body or not body.strip():
        raise OutputValidationError(
            "Draft outreach produced an empty body",
            detail={"tool": ctx.tool.value, "lead_id": args.lead_id},
        )

    for placeholder in _FORBIDDEN_PLACEHOLDERS:
        if placeholder in subject or placeholder in body:
            raise OutputValidationError(
                f"Draft outreach output contained unresolved placeholder: {placeholder!r}",
                detail={
                    "tool": ctx.tool.value,
                    "lead_id": args.lead_id,
                    "placeholder": placeholder,
                },
            )

    if word_count > args.max_words:
        raise OutputValidationError(
            f"Draft outreach exceeded max_words ({word_count} > {args.max_words})",
            detail={
                "tool": ctx.tool.value,
                "lead_id": args.lead_id,
                "word_count": word_count,
                "max_words": args.max_words,
            },
        )

    return DraftOutreachOutput(
        lead_id=args.lead_id,
        subject=subject,
        body=body,
        word_count=word_count,
        personalization_notes=draft_content.personalization_notes,
        content_hash=draft_content.content_hash,
        model_version=draft_content.model_version,
        generated_at=ctx.clock.now(),
    )
