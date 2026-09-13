"""Redaction and truncation of persisted payloads (§14.5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from app.observability.redaction import REDACTED, redact_payload

pytestmark = [pytest.mark.unit]


class TestKeyDenylist:
    @pytest.mark.parametrize(
        "key",
        [
            "api_key",
            "ANTHROPIC_API_KEY",
            "token",
            "approval_token",
            "refresh_Token",
            "secret",
            "client_secret",
            "password",
            "Authorization",
            "credentials",
        ],
    )
    def test_denylisted_keys_are_redacted(self, key: str) -> None:
        assert redact_payload({key: "value"}, max_bytes=1024) == {key: REDACTED}

    def test_denylist_applies_recursively(self) -> None:
        payload = {"outer": {"inner": [{"password": "x"}, {"fine": 1}]}}
        assert redact_payload(payload, max_bytes=1024) == {
            "outer": {"inner": [{"password": REDACTED}, {"fine": 1}]}
        }

    def test_business_data_is_kept(self) -> None:
        """A CRM trace with redacted recipients is useless (§14.5)."""
        payload = {"to_email": "dana@northwind.example", "full_name": "Dana Miller"}
        assert redact_payload(payload, max_bytes=1024) == payload


class TestValuePatterns:
    def test_anthropic_style_keys_are_masked_wherever_they_appear(self) -> None:
        out = redact_payload({"note": "use sk-ant-api03-abcdefghijkl please"}, max_bytes=1024)
        assert out == {"note": f"use {REDACTED} please"}

    def test_bearer_tokens_are_masked(self) -> None:
        out = redact_payload({"header": "Bearer abc.def.ghi-jkl"}, max_bytes=1024)
        assert out == {"header": REDACTED}


class TestJsonSafety:
    def test_non_json_scalars_are_stringified(self) -> None:
        ts = datetime(2026, 9, 13, tzinfo=UTC)
        out = redact_payload({"at": ts, "ids": (1, 2)}, max_bytes=1024)
        assert out == {"at": str(ts), "ids": [1, 2]}
        json.dumps(out)  # must be writable to a JSONB column as-is


class TestTruncation:
    def test_oversized_payloads_are_marked_as_elided_not_absent(self) -> None:
        payload = {"body": "x" * 5000}
        out = redact_payload(payload, max_bytes=1024)
        assert out["_truncated"] is True
        assert out["_original_bytes"] > 1024
        assert len(out["_preview"].encode("utf-8")) <= 1024

    def test_payloads_within_budget_are_untouched(self) -> None:
        payload = {"body": "x" * 100}
        assert redact_payload(payload, max_bytes=1024) == payload

    def test_budget_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            redact_payload({}, max_bytes=0)
