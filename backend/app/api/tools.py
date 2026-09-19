"""Tool catalog HTTP endpoint (§13.7).

Exposes:
- `GET /tools`

The catalog is read-only and published straight from the contract registry
(`app.tools.contracts.catalog`) — the same source `ToolRegistry` itself
constructs against (§8). This endpoint never executes a tool; it only
renders what is already declared.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import require_authorization
from app.api.tools_schemas import ToolResource
from app.tools.contracts import catalog

__all__ = [
    "router",
]

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=list[ToolResource])
async def list_tools(_auth: None = Depends(require_authorization)) -> list[ToolResource]:
    """Return every registered tool contract (§8.2), in registry order."""
    return [ToolResource.model_validate(entry) for entry in catalog()]
