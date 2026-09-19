"""Pydantic v2 schemas for the tool catalog API (§13.7).

`GET /tools` publishes the contract registry (`app.tools.contracts.catalog`)
verbatim — this module only gives that payload a typed, `extra="forbid"`
shape so the generated OpenAPI contract (and the frontend types built from
it) are exact, not a hand-guessed re-description of the registry.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

__all__ = ["ToolFailureModeResource", "ToolResource"]


class ToolFailureModeResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_class: str
    description: str


class ToolResource(BaseModel):
    """One entry of the `GET /tools` catalog — a contract, not a live tool."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    purpose: str
    side_effect: str
    requires_approval: bool
    risk: str
    verification: str
    idempotent: bool
    nondeterministic: bool
    untrusted_output: bool
    timeout_ms: int
    failure_modes: list[ToolFailureModeResource]
    schemas: dict[str, Any]
