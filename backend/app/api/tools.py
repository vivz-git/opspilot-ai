"""Tool catalog HTTP endpoint (§13.7, API-006).

Exposes:
- `GET /tools`

Sourced directly from `app.tools.contracts.catalog()` — the same rendering
of the contract registry the planner, the approval gate and the verifier
already obey (§8.1). This route adds no tool metadata of its own.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import require_authorization
from app.api.schemas import ToolCatalogResponse, ToolResource
from app.tools.contracts import catalog

__all__ = [
    "router",
]

router = APIRouter(tags=["tools"])


@router.get("/tools", response_model=ToolCatalogResponse)
async def get_tool_catalog(
    _auth: None = Depends(require_authorization),
) -> ToolCatalogResponse:
    """Return the tool contract registry verbatim (§13.7)."""
    return ToolCatalogResponse(tools=[ToolResource.model_validate(entry) for entry in catalog()])
