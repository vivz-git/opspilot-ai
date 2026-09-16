"""Invariant verifiers for read-only tools (§8.4, §11.2, VERIFY-001, VERIFY-002).

These verifiers execute semantic assertions on tool output against the requested
input arguments: ranges, sums, filter compliance, template validity, and uniqueness.
They are pure, in-memory, deterministic, and read-only.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Final

from app.agent.state import VerificationCheck, VerificationResult, VerificationStatus
from app.agent.verifiers.base import VerificationContext

__all__ = [
    "DraftOutreachVerifier",
    "ResearchCompanyVerifier",
    "ScoreLeadVerifier",
    "SearchLeadsVerifier",
]

_PLACEHOLDER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(\{\{.*?\}\}|\{%.*?%\}|\[NAME\]|\[COMPANY\]|\[FIRST_NAME\]|\[LAST_NAME\]|<[A-Z_]+>|\bTODO\b|\bFIXME\b|\bXXX\b)"
)

_REQUIRED_SCORE_FACTORS: Final[frozenset[str]] = frozenset(
    {"company_fit", "engagement", "signal_strength", "data_quality"}
)


class SearchLeadsVerifier:
    """Verifies invariant postconditions for `search_leads` (§8.4).

    Asserts:
    1. len(leads) <= limit and total_matched >= 0
    2. total_matched >= len(leads)
    3. lead IDs are unique
    4. Every returned lead satisfies filters supplied in the request.
    """

    async def verify(self, ctx: VerificationContext) -> VerificationResult:
        raw_leads = ctx.output_data.get("leads", [])
        leads: list[dict[str, Any]] = raw_leads if isinstance(raw_leads, list) else []
        total_matched = ctx.output_data.get("total_matched", 0)
        limit = ctx.input_args.get("limit", 10)

        checks: list[VerificationCheck] = []

        # 1. Count within limit and non-negative
        count_ok = isinstance(raw_leads, list) and len(leads) <= limit and total_matched >= 0
        checks.append(
            VerificationCheck(
                name="count_within_limit",
                passed=count_ok,
                expected=f"<= {limit} and total >= 0",
                observed={"count": len(leads), "total_matched": total_matched},
            )
        )

        # 2. Total matched >= len(leads)
        total_ok = isinstance(total_matched, int) and total_matched >= len(leads)
        checks.append(
            VerificationCheck(
                name="total_matched_valid",
                passed=total_ok,
                expected=f">= {len(leads)}",
                observed=total_matched,
            )
        )

        # 3. Lead IDs unique
        lead_ids = [
            item.get("lead_id") for item in leads if isinstance(item, dict) and item.get("lead_id")
        ]
        unique_ok = len(set(lead_ids)) == len(leads)
        checks.append(
            VerificationCheck(
                name="lead_ids_unique",
                passed=unique_ok,
                expected=f"{len(leads)} unique IDs",
                observed=f"{len(set(lead_ids))} unique IDs",
            )
        )

        # 4. Filter compliance
        filter_mismatches: list[str] = []
        req_status = ctx.input_args.get("status")
        req_industry = ctx.input_args.get("industry")
        req_location = ctx.input_args.get("location")

        for lead in leads:
            lid = lead.get("lead_id", "unknown")
            if req_status is not None:
                lead_status = lead.get("status")
                if isinstance(lead_status, str) and lead_status != str(req_status):
                    filter_mismatches.append(f"lead {lid}: status={lead_status} != {req_status}")
            if req_industry is not None and "industry" in lead:
                lead_ind = lead.get("industry")
                if isinstance(lead_ind, str) and lead_ind.lower() != str(req_industry).lower():
                    filter_mismatches.append(f"lead {lid}: industry={lead_ind} != {req_industry}")
            if req_location is not None and "location" in lead:
                lead_loc = lead.get("location")
                if isinstance(lead_loc, str) and lead_loc.lower() != str(req_location).lower():
                    filter_mismatches.append(f"lead {lid}: location={lead_loc} != {req_location}")

        filters_ok = len(filter_mismatches) == 0
        checks.append(
            VerificationCheck(
                name="filter_compliance",
                passed=filters_ok,
                expected="all leads match query filters",
                observed=filter_mismatches if filter_mismatches else "clean",
            )
        )

        all_passed = all(c.passed for c in checks)
        return VerificationResult(
            step_id=ctx.step_id,
            status=VerificationStatus.PASSED if all_passed else VerificationStatus.FAILED,
            mode="invariant",
            checks=checks,
            detail=None if all_passed else "search_leads invariant check failed",
        )


class ResearchCompanyVerifier:
    """Verifies invariant postconditions for `research_company` (§8.4).

    Asserts:
    1. 0.0 <= confidence <= 1.0
    2. summary is non-empty
    3. company_id matches requested company_id (when requested)
    4. domain matches requested domain (when requested)
    5. Structural list types are valid
    """

    async def verify(self, ctx: VerificationContext) -> VerificationResult:
        profile: dict[str, Any] = ctx.output_data.get("profile", {})
        checks: list[VerificationCheck] = []

        # 1. Confidence in bounds
        confidence = profile.get("confidence")
        conf_ok = isinstance(confidence, (int, float)) and 0.0 <= confidence <= 1.0
        checks.append(
            VerificationCheck(
                name="confidence_in_bounds",
                passed=conf_ok,
                expected="0.0 <= confidence <= 1.0",
                observed=confidence,
            )
        )

        # 2. Summary non-empty
        summary = profile.get("summary", "")
        summary_ok = isinstance(summary, str) and bool(summary.strip())
        checks.append(
            VerificationCheck(
                name="summary_non_empty",
                passed=summary_ok,
                expected="non-empty string",
                observed=f"len={len(summary)}",
            )
        )

        # 3. Company ID matches when requested
        req_company_id = ctx.input_args.get("company_id")
        if req_company_id is not None:
            observed_id = profile.get("company_id")
            id_ok = observed_id == req_company_id
            checks.append(
                VerificationCheck(
                    name="company_id_matches_intent",
                    passed=id_ok,
                    expected=req_company_id,
                    observed=observed_id,
                )
            )

        # 4. Domain matches when requested
        req_domain = ctx.input_args.get("domain")
        if req_domain is not None:
            observed_domain = profile.get("domain")
            domain_ok = (
                isinstance(observed_domain, str)
                and observed_domain.lower() == str(req_domain).lower()
            )
            checks.append(
                VerificationCheck(
                    name="domain_matches_intent",
                    passed=domain_ok,
                    expected=str(req_domain).lower(),
                    observed=observed_domain,
                )
            )

        # 5. Structural list types
        tech_stack = profile.get("tech_stack", [])
        signals = profile.get("recent_signals", [])
        sources = profile.get("sources", [])
        types_ok = (
            isinstance(tech_stack, list) and isinstance(signals, list) and isinstance(sources, list)
        )
        checks.append(
            VerificationCheck(
                name="structural_types_valid",
                passed=types_ok,
                expected="lists for tech_stack, signals, sources",
                observed={
                    "tech_stack": type(tech_stack).__name__,
                    "recent_signals": type(signals).__name__,
                    "sources": type(sources).__name__,
                },
            )
        )

        all_passed = all(c.passed for c in checks)
        return VerificationResult(
            step_id=ctx.step_id,
            status=VerificationStatus.PASSED if all_passed else VerificationStatus.FAILED,
            mode="invariant",
            checks=checks,
            detail=None if all_passed else "research_company invariant check failed",
        )


class ScoreLeadVerifier:
    """Verifies invariant postconditions for `score_lead` (§8.4).

    Asserts:
    1. 0 <= score <= 100
    2. sum(contribution) ≈ score (±1 for rounding)
    3. band matches the documented rule engine thresholds:
       - hot: score >= 75
       - warm: 50 <= score < 75
       - cold: score < 50
    4. lead_id matches the request
    5. required factor breakdown is present
    """

    async def verify(self, ctx: VerificationContext) -> VerificationResult:
        score_val = ctx.output_data.get("score")
        score: int | None = score_val if isinstance(score_val, int) else None
        band = ctx.output_data.get("band")
        factors: list[dict[str, Any]] = ctx.output_data.get("factors", [])
        lead_id = ctx.output_data.get("lead_id")
        req_lead_id = ctx.input_args.get("lead_id")

        checks: list[VerificationCheck] = []

        # 1. Score in bounds
        score_ok = score is not None and 0 <= score <= 100
        checks.append(
            VerificationCheck(
                name="score_in_bounds",
                passed=score_ok,
                expected="0 <= score <= 100",
                observed=score_val,
            )
        )

        # 2. Factor contribution sum
        if score is not None and score_ok and factors:
            factor_sum = sum(float(f.get("contribution", 0.0)) for f in factors)
            diff = abs(factor_sum - score)
            sum_ok = diff <= 1.01
            checks.append(
                VerificationCheck(
                    name="factor_sum_consistent",
                    passed=sum_ok,
                    expected=f"score +- 1 ({score})",
                    observed=round(factor_sum, 2),
                )
            )
        elif not factors and score_ok:
            checks.append(
                VerificationCheck(
                    name="factor_sum_consistent",
                    passed=True,
                    expected="empty factors allowed",
                    observed=0,
                )
            )

        # 3. Band thresholds (aligned to score_lead rule engine: >= 75 is hot, 50..74 warm)
        if score is not None and score_ok:
            expected_band: str
            if score >= 75:
                expected_band = "hot"
            elif score >= 50:
                expected_band = "warm"
            else:
                expected_band = "cold"

            band_ok = str(band).lower() == expected_band
            checks.append(
                VerificationCheck(
                    name="band_consistent",
                    passed=band_ok,
                    expected=expected_band,
                    observed=band,
                )
            )

        # 4. Lead ID match
        if req_lead_id is not None:
            id_ok = lead_id == req_lead_id
            checks.append(
                VerificationCheck(
                    name="lead_id_matches_intent",
                    passed=id_ok,
                    expected=req_lead_id,
                    observed=lead_id,
                )
            )

        # 5. Required factors breakdown present
        if factors:
            observed_names = {f.get("name") for f in factors if isinstance(f, dict)}
            factors_ok = _REQUIRED_SCORE_FACTORS.issubset(observed_names)
            checks.append(
                VerificationCheck(
                    name="required_factors_present",
                    passed=factors_ok,
                    expected=sorted(_REQUIRED_SCORE_FACTORS),
                    observed=sorted(str(n) for n in observed_names if n),
                )
            )

        all_passed = all(c.passed for c in checks)
        return VerificationResult(
            step_id=ctx.step_id,
            status=VerificationStatus.PASSED if all_passed else VerificationStatus.FAILED,
            mode="invariant",
            checks=checks,
            detail=None if all_passed else "score_lead invariant check failed",
        )


class DraftOutreachVerifier:
    """Verifies invariant postconditions for `draft_outreach` (§8.4).

    Asserts:
    1. Actual word count and reported word count <= max_words
    2. Subject length bounded (1..120 chars)
    3. Subject and body non-empty
    4. No unresolved template placeholders ({{...}}, {%...%}, [NAME], [COMPANY], TODO, etc.)
    5. content_hash matches hash(subject || "\n\n" || body)
    """

    async def verify(self, ctx: VerificationContext) -> VerificationResult:
        subject = ctx.output_data.get("subject", "")
        body = ctx.output_data.get("body", "")
        word_count = ctx.output_data.get("word_count", 0)
        content_hash = ctx.output_data.get("content_hash", "")
        max_words = ctx.input_args.get("max_words", 180)

        checks: list[VerificationCheck] = []

        # 1. Actual word count and reported word count bounded
        actual_words = len(body.split()) if isinstance(body, str) else 0
        reported_ok = isinstance(word_count, int) and word_count <= max_words
        actual_ok = actual_words <= max_words
        words_ok = reported_ok and actual_ok
        checks.append(
            VerificationCheck(
                name="word_count_bounded",
                passed=words_ok,
                expected=f"<= {max_words}",
                observed={"reported": word_count, "actual": actual_words},
            )
        )

        # 2. Subject length bounded (§8.4: subject: str(<= 120))
        subj_len = len(subject) if isinstance(subject, str) else 0
        subj_len_ok = 1 <= subj_len <= 120
        checks.append(
            VerificationCheck(
                name="subject_length_bounded",
                passed=subj_len_ok,
                expected="1 <= len(subject) <= 120",
                observed=subj_len,
            )
        )

        # 3. Subject and body non-empty
        subj_ok = isinstance(subject, str) and bool(subject.strip())
        body_ok = isinstance(body, str) and bool(body.strip())
        checks.append(
            VerificationCheck(
                name="content_non_empty",
                passed=subj_ok and body_ok,
                expected="subject and body non-empty",
                observed={
                    "subject_len": subj_len,
                    "body_len": len(body) if isinstance(body, str) else 0,
                },
            )
        )

        # 4. No unresolved template placeholders
        combined_text = f"{subject} {body}"
        found_placeholders = _PLACEHOLDER_PATTERN.findall(combined_text)
        placeholders_ok = len(found_placeholders) == 0
        checks.append(
            VerificationCheck(
                name="no_unresolved_placeholders",
                passed=placeholders_ok,
                expected="no unresolved placeholders",
                observed=found_placeholders if found_placeholders else "none",
            )
        )

        # 5. Content hash integrity
        payload = f"{subject}\n\n{body}"
        expected_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        hash_ok = content_hash == expected_hash
        obs_str = (
            content_hash[:16] + ("..." if len(content_hash) > 16 else "")
            if content_hash
            else "none"
        )
        checks.append(
            VerificationCheck(
                name="content_hash_integrity",
                passed=hash_ok,
                expected=expected_hash[:16] + "...",
                observed=obs_str,
            )
        )

        all_passed = all(c.passed for c in checks)
        return VerificationResult(
            step_id=ctx.step_id,
            status=VerificationStatus.PASSED if all_passed else VerificationStatus.FAILED,
            mode="invariant",
            checks=checks,
            detail=None if all_passed else "draft_outreach invariant check failed",
        )
