"""Rich human-approval preview builder (§6.3, §9.2, §14.5, HITL-005).

Constructs safe, deterministic, human-readable previews of gated tool operations
(`send_email_mock`, `update_customer`) without executing tools, without mutating
state, and without altering authorization tokens or canonical `args_hash`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any, Final

from app.agent.state import PlanStep, ToolResult
from app.observability.redaction import redact_payload
from app.persistence.protocols import UnitOfWork
from app.security import VOLATILE_ARG_KEYS
from app.tools.contracts import RiskLevel, ToolName

__all__ = ["build_approval_preview"]

_MAX_BODY_CHARS: Final[int] = 1500
_PREVIEW_MAX_BYTES: Final[int] = 4096


def _clean_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Strip volatile keys and convert non-JSON values to safe scalar representations."""
    clean: dict[str, Any] = {}
    for k, v in args.items():
        if k in VOLATILE_ARG_KEYS:
            continue
        clean[str(k)] = _sanitize_val(v)
    return clean


def _sanitize_val(val: Any) -> Any:  # noqa: ANN401
    """Convert enums and complex structures into JSON-safe dictionaries/lists/scalars."""
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if hasattr(val, "value"):
        return val.value
    if hasattr(val, "model_dump"):
        return _clean_args(val.model_dump(exclude_none=True))
    if isinstance(val, Mapping):
        return {str(k): _sanitize_val(v) for k, v in val.items() if k not in VOLATILE_ARG_KEYS}
    if isinstance(val, (list, tuple, set, frozenset)):
        return [_sanitize_val(item) for item in val]
    return str(val)


def _truncate_body(body: str | None, content_hash: str | None) -> str | None:
    """Truncate draft body if it exceeds budget, appending a content hash marker."""
    if body is None:
        return None
    if len(body) > _MAX_BODY_CHARS:
        hash_indicator = content_hash or "unknown"
        marker = (
            f"\n... [truncated: original length {len(body)} chars, content_hash: {hash_indicator}]"
        )
        return f"{body[:_MAX_BODY_CHARS]}{marker}"
    return body


async def _build_send_email_preview(
    step: PlanStep,
    resolved_args: Mapping[str, Any],
    tool_results: Mapping[str, ToolResult] | None,
    uow: UnitOfWork | None,
) -> tuple[dict[str, Any], str, str]:
    """Build de-referenced preview for send_email_mock."""
    draft_id = resolved_args.get("draft_id")
    if draft_id is not None:
        draft_id = str(draft_id)
    to_email = resolved_args.get("to_email") or resolved_args.get("to")
    if to_email is not None:
        to_email = str(to_email)

    subject: str | None = None
    body: str | None = None
    lead_id: str | None = None
    content_hash: str | None = None

    # 1. Tier 1: UnitOfWork read-only repository lookup
    if uow is not None and draft_id:
        with contextlib.suppress(Exception):
            draft_record = await uow.outreach_drafts.get(draft_id)
            if draft_record is not None:
                subject = draft_record.subject
                body = draft_record.body
                lead_id = draft_record.lead_id
                content_hash = draft_record.content_hash

    # 2. Tier 2: In-memory tool_results fallback
    if (subject is None or body is None) and tool_results:
        for res in tool_results.values():
            res_output = getattr(res, "output", None)
            if not isinstance(res_output, dict):
                continue
            # Check if this tool result produced the draft
            if res_output.get("draft_id") == draft_id:
                subject = subject or res_output.get("subject")
                body = body or res_output.get("body")
                lead_id = lead_id or res_output.get("lead_id")
                content_hash = content_hash or res_output.get("content_hash")
            # Check if draft_outreach produced subject/body
            tool_name = res.tool.value if hasattr(res.tool, "value") else str(res.tool)
            if (
                tool_name in (ToolName.DRAFT_OUTREACH.value, "draft_outreach")
                and subject is None
                and "subject" in res_output
            ):
                subject = res_output.get("subject")
                body = body or res_output.get("body")
                content_hash = content_hash or res_output.get("content_hash")

    clean_args = _clean_args(resolved_args)
    preview: dict[str, Any] = {
        "action": ToolName.SEND_EMAIL_MOCK.value,
        "tool": ToolName.SEND_EMAIL_MOCK.value,
        "risk": RiskLevel.HIGH.value,
        "to": to_email,
        "to_email": to_email,
        "draft_id": draft_id,
        "lead_id": lead_id,
        "subject": subject,
        "body": _truncate_body(body, content_hash),
        "content_hash": content_hash,
        "args": clean_args,
    }

    title = f"Send outreach email to {to_email}" if to_email else "Send outreach email"
    if step.rationale:
        summary = step.rationale
    elif subject:
        summary = f"Send saved draft ({draft_id or ''}) to {to_email or ''}: {subject}".strip()
    else:
        summary = f"Send saved draft ({draft_id or ''}) to {to_email or ''}".strip()

    return preview, title, summary


async def _build_update_customer_preview(
    step: PlanStep,
    resolved_args: Mapping[str, Any],
    tool_results: Mapping[str, ToolResult] | None,
    uow: UnitOfWork | None,
) -> tuple[dict[str, Any], str, str]:
    """Build de-referenced preview for update_customer with field-level diff."""
    customer_id = resolved_args.get("customer_id")
    if customer_id is not None:
        customer_id = str(customer_id)
    expected_version = resolved_args.get("expected_version")
    if expected_version is not None:
        with contextlib.suppress(ValueError, TypeError):
            expected_version = int(expected_version)
    raw_patch = resolved_args.get("patch") or {}
    reason = resolved_args.get("reason")
    if reason is not None:
        reason = str(reason)

    if hasattr(raw_patch, "model_dump"):
        patch_dict = raw_patch.model_dump(exclude_none=True)
    elif isinstance(raw_patch, dict):
        patch_dict = {
            str(k): v
            for k, v in raw_patch.items()
            if v is not None and str(k) not in VOLATILE_ARG_KEYS
        }
    else:
        patch_dict = {}

    account_name: str | None = None
    primary_contact: str | None = None
    email: str | None = None
    current_version: int | None = None
    customer_row: Any = None

    # 1. Tier 1: UnitOfWork lookup
    if uow is not None and customer_id:
        with contextlib.suppress(Exception):
            customer_row = await uow.customers.get(customer_id)

    # 2. Tier 2: In-memory tool_results fallback
    if customer_row is None and tool_results:
        for res in tool_results.values():
            res_output = getattr(res, "output", None)
            if not isinstance(res_output, dict):
                continue
            c_data = res_output.get("customer")
            if isinstance(c_data, dict) and c_data.get("customer_id") == customer_id:
                customer_row = c_data
                break

    if customer_row is not None:
        account_name = getattr(customer_row, "account_name", None) or (
            customer_row.get("account_name") if isinstance(customer_row, dict) else None
        )
        primary_contact = getattr(customer_row, "primary_contact", None) or (
            customer_row.get("primary_contact") if isinstance(customer_row, dict) else None
        )
        email = getattr(customer_row, "email", None) or (
            customer_row.get("email") if isinstance(customer_row, dict) else None
        )
        raw_version = getattr(customer_row, "version", None) or (
            customer_row.get("version") if isinstance(customer_row, dict) else None
        )
        if raw_version is not None:
            with contextlib.suppress(ValueError, TypeError):
                current_version = int(raw_version)

    version_match: bool | None = None
    if current_version is not None and expected_version is not None:
        version_match = current_version == expected_version

    diff: dict[str, dict[str, Any]] = {}
    for field_name, new_val in sorted(patch_dict.items()):
        old_val: Any = None
        if customer_row is not None:
            old_val = (
                getattr(customer_row, field_name, None)
                if not isinstance(customer_row, dict)
                else customer_row.get(field_name)
            )
        old_val = _sanitize_val(old_val)
        new_val = _sanitize_val(new_val)
        diff[field_name] = {
            "before": old_val,
            "after": new_val,
        }

    clean_args = _clean_args(resolved_args)
    preview: dict[str, Any] = {
        "action": ToolName.UPDATE_CUSTOMER.value,
        "tool": ToolName.UPDATE_CUSTOMER.value,
        "risk": RiskLevel.HIGH.value,
        "customer_id": customer_id,
        "account_name": account_name,
        "primary_contact": primary_contact,
        "email": email,
        "reason": reason,
        "expected_version": expected_version,
        "current_version": current_version,
        "version_match": version_match,
        "diff": diff,
        "args": clean_args,
    }

    target_desc = account_name or customer_id or ""
    title = f"Update customer {target_desc}".strip()
    fields_str = ", ".join(sorted(patch_dict.keys()))
    if step.rationale:
        summary = step.rationale
    elif fields_str:
        summary = f"Apply patch to {target_desc}: {fields_str}"
    else:
        summary = f"Update customer {target_desc}"

    return preview, title, summary


def _build_generic_preview(
    step: PlanStep,
    resolved_args: Mapping[str, Any],
    risk: RiskLevel | None,
) -> tuple[dict[str, Any], str, str]:
    """Fallback preview for any arbitrary or generic tool."""
    tool_name = step.tool.value if hasattr(step.tool, "value") else str(step.tool)
    risk_val = risk.value if (risk and hasattr(risk, "value")) else (str(risk) if risk else "low")
    clean_args = _clean_args(resolved_args)

    preview: dict[str, Any] = {
        "action": tool_name,
        "tool": tool_name,
        "risk": risk_val,
        "summary": step.rationale or "",
        "args": clean_args,
    }
    title = f"{tool_name}: approve step {step.step_id}"
    summary = step.rationale or ""
    return preview, title, summary


async def build_approval_preview(
    step: PlanStep,
    resolved_args: Mapping[str, Any],
    *,
    tool_results: Mapping[str, ToolResult] | None = None,
    uow: UnitOfWork | None = None,
    risk: RiskLevel | None = None,
) -> tuple[dict[str, Any], str, str]:
    """Construct a safe, redacted, human-readable preview for approval.

    Returns:
        tuple[dict[str, Any], str, str]: (payload_preview, title, summary)
    """
    tool_val = step.tool.value if hasattr(step.tool, "value") else str(step.tool)
    is_send_email = tool_val == ToolName.SEND_EMAIL_MOCK.value
    is_update_customer = tool_val == ToolName.UPDATE_CUSTOMER.value

    if is_send_email:
        raw_preview, title, summary = await _build_send_email_preview(
            step, resolved_args, tool_results, uow
        )
    elif is_update_customer:
        raw_preview, title, summary = await _build_update_customer_preview(
            step, resolved_args, tool_results, uow
        )
    else:
        raw_preview, title, summary = _build_generic_preview(step, resolved_args, risk)

    # Apply strict recursive redaction and byte budget (§14.5)
    safe_preview = redact_payload(raw_preview, max_bytes=_PREVIEW_MAX_BYTES)
    return safe_preview, title, summary
