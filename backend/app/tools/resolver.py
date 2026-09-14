"""Reference resolution re-export surface (§4.4, TOOL-004, AGENT-005).

Re-exports the core resolver implementations from `app.agent.resolver`.
"""

from __future__ import annotations

from app.agent.resolver import (
    OUTPUT_SEGMENT,
    REF_KEY,
    REF_PREFIX,
    parse_ref_path,
    resolve_ref_path,
    resolve_step_args,
    resolve_value,
)

__all__ = [
    "OUTPUT_SEGMENT",
    "REF_KEY",
    "REF_PREFIX",
    "parse_ref_path",
    "resolve_ref_path",
    "resolve_step_args",
    "resolve_value",
]
