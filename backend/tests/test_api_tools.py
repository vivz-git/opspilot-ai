"""API-006: `GET /tools` catalog endpoint tests (§13.7).

Covers:
1. `GET /tools` and `GET /api/v1/tools` return every registered tool.
2. The response is exactly `app.tools.contracts.catalog()`, JSON-normalized —
   the route sources the registry directly and adds no metadata of its own.
3. Each entry carries the documented mutating/approval/verification flags.
4. Each entry carries JSON Schema for both input and output.
5. Method not allowed on the catalog route.
"""

from __future__ import annotations

import json

import pytest
from app.main import create_app
from app.tools.contracts import REGISTRY, ToolName, catalog
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.unit]


def _normalize(value: object) -> object:
    """Round-trip through JSON so a Python dict (`catalog()`) and a decoded
    HTTP response body compare equal regardless of key/tuple/enum shape."""
    return json.loads(json.dumps(value, default=str))


class TestGetToolCatalog:
    def test_returns_every_registered_tool(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/tools")

        assert resp.status_code == 200
        body = resp.json()
        names = {t["name"] for t in body["tools"]}
        assert names == {t.value for t in ToolName}
        assert len(body["tools"]) == len(REGISTRY)

    def test_matches_contracts_catalog_exactly(self) -> None:
        """The route must not duplicate or drift from the registry's own
        rendering (API-006 requirement): the payload is `catalog()`,
        verbatim, modulo JSON round-tripping."""
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/tools")

        body = resp.json()
        assert _normalize(body["tools"]) == _normalize(catalog())

    def test_api_v1_prefix_parity(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            root = client.get("/tools")
            v1 = client.get("/api/v1/tools")

        assert root.status_code == v1.status_code == 200
        assert root.json() == v1.json()

    def test_post_is_not_allowed(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            resp = client.post("/tools")

        assert resp.status_code == 405


class TestToolCatalogContractFlags:
    """Spot checks against §8.3's contract matrix, sourced from the same
    registry the route reads — a drift here would mean the route stopped
    reflecting the registry, not that the registry itself changed."""

    def _entry(self, name: str) -> dict[str, object]:
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/tools")
        return next(t for t in resp.json()["tools"] if t["name"] == name)

    def test_send_email_mock_is_gated_outbound_and_readback_verified(self) -> None:
        entry = self._entry("send_email_mock")
        assert entry["requires_approval"] is True
        assert entry["side_effect"] == "outbound"
        assert entry["risk"] == "high"
        assert entry["verification"] == "readback"
        assert entry["idempotent"] is True

    def test_search_leads_is_read_only_and_ungated(self) -> None:
        entry = self._entry("search_leads")
        assert entry["requires_approval"] is False
        assert entry["side_effect"] == "read_only"
        assert entry["verification"] == "invariant"

    def test_update_customer_is_gated_and_readback_verified(self) -> None:
        entry = self._entry("update_customer")
        assert entry["requires_approval"] is True
        assert entry["side_effect"] == "customer_write"
        assert entry["verification"] == "readback"


class TestToolCatalogSchemas:
    """API-006 requires input/output JSON Schema per tool (§13.7)."""

    def test_every_tool_carries_input_and_output_json_schema(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/tools")

        for entry in resp.json()["tools"]:
            schemas = entry["schemas"]
            assert set(schemas) == {"input", "output"}
            assert schemas["input"]["type"] == "object"
            assert schemas["output"]["type"] == "object"
            assert "properties" in schemas["input"]
            assert "properties" in schemas["output"]

    def test_search_leads_schema_names_its_fields(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/tools")

        entry = next(t for t in resp.json()["tools"] if t["name"] == "search_leads")
        assert "limit" in entry["schemas"]["input"]["properties"]
        assert "leads" in entry["schemas"]["output"]["properties"]
