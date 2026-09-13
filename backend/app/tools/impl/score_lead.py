"""Implementation of score_lead tool (§8.4, ADR-009, TOOL-003)."""

from __future__ import annotations

from app.errors import InternalError
from app.tools.registry import ToolContext
from app.tools.schemas import (
    ScoreBand,
    ScoreFactor,
    ScoreLeadInput,
    ScoreLeadOutput,
    ScoringWeights,
)

__all__ = ["score_lead"]

_TARGET_INDUSTRIES = frozenset({"finance", "fintech", "technology", "software", "logistics"})
_HIGH_FUNDING_STAGES = frozenset({"Series B", "Series C", "Series D", "Growth", "Public"})


async def score_lead(args: ScoreLeadInput, ctx: ToolContext) -> ScoreLeadOutput:
    """Score and band a lead deterministically from its company profile."""
    if ctx.port is not None:
        raise InternalError(
            "score_lead is a pure tool and expects no port",
            detail={"tool": ctx.tool.value, "port": type(ctx.port).__name__},
        )

    weights = args.weights or ScoringWeights()
    company = args.company

    # 1. Company fit (0..100)
    industry_score = (
        80.0
        if company.industry and company.industry.strip().lower() in _TARGET_INDUSTRIES
        else 55.0
    )
    emp = company.employee_count
    if emp is not None:
        if 50 <= emp <= 5000:
            size_score = 20.0
        elif emp > 5000:
            size_score = 15.0
        else:
            size_score = 10.0
    else:
        size_score = 10.0
    company_fit_val = min(100.0, industry_score + size_score)

    # 2. Engagement (0..100)
    if company.recent_signals:
        engagement_val = min(100.0, 25.0 + len(company.recent_signals) * 25.0)
    else:
        engagement_val = 20.0

    # 3. Signal strength (0..100)
    tech_score = min(40.0, len(company.tech_stack) * 10.0)
    stage = company.funding_stage or ""
    if stage in _HIGH_FUNDING_STAGES:
        stage_score = 40.0
    elif stage in ("Seed", "Series A"):
        stage_score = 30.0
    else:
        stage_score = 20.0
    signal_strength_val = min(100.0, 20.0 + tech_score + stage_score)

    # 4. Data quality (0..100)
    data_quality_val = min(100.0, max(0.0, company.confidence * 100.0))

    factors = [
        ScoreFactor(
            name="company_fit",
            weight=weights.company_fit,
            value=round(company_fit_val, 2),
            contribution=round(weights.company_fit * company_fit_val, 2),
        ),
        ScoreFactor(
            name="engagement",
            weight=weights.engagement,
            value=round(engagement_val, 2),
            contribution=round(weights.engagement * engagement_val, 2),
        ),
        ScoreFactor(
            name="signal_strength",
            weight=weights.signal_strength,
            value=round(signal_strength_val, 2),
            contribution=round(weights.signal_strength * signal_strength_val, 2),
        ),
        ScoreFactor(
            name="data_quality",
            weight=weights.data_quality,
            value=round(data_quality_val, 2),
            contribution=round(weights.data_quality * data_quality_val, 2),
        ),
    ]

    total_score = max(0, min(100, int(round(sum(f.contribution for f in factors)))))

    if total_score >= 75:
        band = ScoreBand.HOT
    elif total_score >= 50:
        band = ScoreBand.WARM
    else:
        band = ScoreBand.COLD

    rationale = (
        f"Lead {args.lead_id} scored {total_score} ({band.value}) with company fit "
        f"{company_fit_val:.1f}, engagement {engagement_val:.1f}, "
        f"signals {signal_strength_val:.1f}, and data confidence {data_quality_val:.1f}."
    )

    return ScoreLeadOutput(
        lead_id=args.lead_id,
        score=total_score,
        band=band,
        factors=factors,
        rationale=rationale,
        model_version="rules-v1",
    )
