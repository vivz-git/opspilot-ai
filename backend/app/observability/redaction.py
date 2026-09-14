"""Redaction and truncation of persisted payloads (§14.5).

Applied before anything reaches `tool_calls.input`/`output`, a trace event
or a log line. Two rules are structural rather than best-effort:

1. any key matching the denylist (case-insensitive, recursive) is replaced by
   `"[redacted]"`, and known secret-shaped values are masked wherever they
   appear;
2. a payload over the configured byte budget is replaced by a marker that
   says data was *elided*, not absent — a timeline that silently drops an
   argument is worse than one that says it was too large.

Business data (names, `*.example` addresses) is deliberately kept: a CRM
trace with redacted recipients is useless for verifying that the right person
was contacted (§14.5). `approval_token` is removed by the dispatcher before
this function ever sees a payload; the denylist matching `token` is the
backstop for a token that arrives some other way.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Final

__all__ = ["REDACTED", "redact_payload"]

REDACTED: Final[str] = "[redacted]"

#: §14.5 rule 1 — matched with `re.search`, so `anthropic_api_key`,
#: `Authorization` and `refresh_token` all qualify.
_DENYLIST: Final[re.Pattern[str]] = re.compile(
    r"api_key|token|secret|password|authorization|credential", re.IGNORECASE
)

#: §14.5 rule 2 — provider-style API keys (Groq `gsk_…`, Anthropic `sk-ant-…`)
#: and bearer tokens, wherever they appear.
_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"gsk_[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
)

#: How much of an oversized payload's JSON text survives as a preview.
_PREVIEW_BYTES: Final[int] = 1024


def redact_payload(payload: Mapping[str, Any], *, max_bytes: int) -> dict[str, Any]:
    """Return a JSON-safe, redacted, size-bounded copy of `payload`.

    Non-JSON scalars (datetimes, enums, UUIDs) are stringified so the result
    can be written to a JSONB column as-is.
    """
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    redacted = _redact(payload)
    if not isinstance(redacted, dict):  # pragma: no cover - Mapping input always yields a dict
        raise TypeError("payload must be a mapping")
    encoded = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
    size = len(encoded.encode("utf-8"))
    if size > max_bytes:
        return {
            "_truncated": True,
            "_original_bytes": size,
            "_preview": encoded[:_PREVIEW_BYTES],
        }
    return redacted


def _redact(value: Any) -> Any:  # noqa: ANN401 - recurses over arbitrary JSON-like data
    if isinstance(value, Mapping):
        return {
            str(k): (REDACTED if _DENYLIST.search(str(k)) else _redact(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _mask(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _mask(str(value))


def _mask(text: str) -> str:
    for pattern in _VALUE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text
