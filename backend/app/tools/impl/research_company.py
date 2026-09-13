"""Implementation of research_company tool (§8.4, TOOL-003)."""

from __future__ import annotations

from app.errors import InternalError, OutputValidationError
from app.integrations.ports import CompanyPort
from app.tools.registry import ToolContext
from app.tools.schemas import ResearchCompanyInput, ResearchCompanyOutput

__all__ = ["research_company"]


async def research_company(args: ResearchCompanyInput, ctx: ToolContext) -> ResearchCompanyOutput:
    """Enrich a company profile with firmographics and buying signals."""
    if not isinstance(ctx.port, CompanyPort):
        raise InternalError(
            "research_company requires a CompanyPort",
            detail={"tool": ctx.tool.value, "port": type(ctx.port).__name__},
        )

    profile = await ctx.port.profile(
        company_id=args.company_id,
        domain=args.domain,
        depth=args.depth,
    )

    if not profile.summary or not profile.summary.strip():
        raise OutputValidationError(
            "Company profile summary is empty",
            detail={"tool": ctx.tool.value, "company_id": profile.company_id},
        )
    if not (0.0 <= profile.confidence <= 1.0):
        raise OutputValidationError(
            "Company profile confidence out of range [0.0, 1.0]",
            detail={"tool": ctx.tool.value, "confidence": profile.confidence},
        )

    return ResearchCompanyOutput(profile=profile)
