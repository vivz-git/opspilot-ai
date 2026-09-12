"""The tool policy invariants P1-P7 (docs/architecture.md §8.2).

These tests iterate the registry, so they apply to tools that do not exist
yet. A future tool that mutates customer data without an approval flag fails
CI rather than depending on a reviewer noticing.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.errors import ErrorClass
from app.tools.contracts import (
    GATED_SIDE_EFFECTS,
    REGISTRY,
    SideEffect,
    RiskLevel,
    ToolContract,
    ToolName,
    VerificationMode,
    catalog,
    contract,
)

ALL = pytest.mark.parametrize("c", list(REGISTRY.values()), ids=lambda c: c.name.value)
pytestmark = [pytest.mark.contract, pytest.mark.unit]


@ALL
def test_p1_gated_side_effects_require_approval(c: ToolContract) -> None:
    if c.side_effect in GATED_SIDE_EFFECTS:
        assert c.requires_approval, f"{c.name} is {c.side_effect} but is not gated"


@ALL
def test_p2_mutating_tools_require_readback(c: ToolContract) -> None:
    if c.is_mutating:
        assert c.verification is VerificationMode.READBACK, (
            f"{c.name} claims an effect on the world but is not independently verified"
        )


@ALL
def test_p3_read_only_tools_are_never_gated(c: ToolContract) -> None:
    # Approval fatigue is a safety failure, not a safety feature.
    if c.side_effect is SideEffect.READ_ONLY:
        assert not c.requires_approval


@ALL
def test_p4_gated_tools_take_an_idempotency_key(c: ToolContract) -> None:
    if c.requires_approval:
        fields = c.input_model.model_fields
        assert "idempotency_key" in fields, f"{c.name} is gated but cannot de-duplicate a retry"
        assert "approval_token" in fields, f"{c.name} is gated but takes no approval token"


@ALL
def test_p5_unverified_non_idempotent_mutations_are_never_retried(c: ToolContract) -> None:
    if not c.idempotent:
        assert ErrorClass.VERIFICATION_FAILED not in c.retryable_errors


@ALL
def test_p6_destructive_tools_are_high_risk(c: ToolContract) -> None:
    if c.side_effect is SideEffect.DESTRUCTIVE:
        assert c.risk is RiskLevel.HIGH


@ALL
def test_p7_declared_failure_modes_are_in_the_taxonomy(c: ToolContract) -> None:
    for mode in c.failure_modes:
        assert isinstance(mode.error_class, ErrorClass)
    for err in c.retryable_errors:
        assert isinstance(err, ErrorClass)


@ALL
def test_output_validation_retryable_only_for_nondeterministic_tools(c: ToolContract) -> None:
    """Re-asking a rule engine for a different answer is superstition (§10.1)."""
    if ErrorClass.OUTPUT_VALIDATION in c.retryable_errors:
        assert c.nondeterministic, f"{c.name} retries OUTPUT_VALIDATION but is deterministic"


@ALL
def test_terminal_error_classes_are_never_retryable(c: ToolContract) -> None:
    assert ErrorClass.POLICY_VIOLATION not in c.retryable_errors
    assert ErrorClass.INTERNAL not in c.retryable_errors
    assert ErrorClass.BUDGET_EXHAUSTED not in c.retryable_errors


@ALL
def test_schemas_are_strict_and_publishable(c: ToolContract) -> None:
    for model in (c.input_model, c.output_model):
        assert issubclass(model, BaseModel)
        assert model.model_config.get("extra") == "forbid", f"{model.__name__} allows extra fields"
    published = c.json_schemas()
    assert published["input"] and published["output"]


@ALL
def test_registry_key_matches_contract_name(c: ToolContract) -> None:
    assert REGISTRY[c.name] is c


def test_registry_declares_exactly_the_planned_tools() -> None:
    assert set(REGISTRY) == set(ToolName)
    assert len(REGISTRY) == 9


def test_exactly_the_documented_tools_are_gated() -> None:
    gated = {c.name.value for c in REGISTRY.values() if c.requires_approval}
    assert gated == {"send_email_mock", "update_customer"}


def test_unknown_tool_cannot_be_dispatched() -> None:
    """An LLM cannot invent a capability: an undeclared name is rejected
    before dispatch (§4.2)."""
    with pytest.raises(ValueError):
        contract("send_email")          # the future real sender does not exist
    with pytest.raises(ValueError):
        contract("delete_all_customers")


def test_send_email_mock_cannot_accept_raw_content() -> None:
    """The approved content is the saved draft; the tool takes a draft_id, so
    content cannot be swapped between approval and send (§8.4)."""
    fields = REGISTRY[ToolName.SEND_EMAIL_MOCK].input_model.model_fields
    assert "draft_id" in fields
    assert "subject" not in fields
    assert "body" not in fields


def test_customer_patch_cannot_touch_identity_or_billing() -> None:
    patch = REGISTRY[ToolName.UPDATE_CUSTOMER].input_model.model_fields["patch"]
    allowed = set(patch.annotation.model_fields)
    assert allowed == {"status", "plan", "owner", "phone", "primary_contact", "notes"}
    assert not allowed & {"customer_id", "email", "created_at", "mrr", "version"}


def test_untrusted_output_is_declared_where_third_party_text_enters() -> None:
    assert REGISTRY[ToolName.RESEARCH_COMPANY].untrusted_output is True


def test_catalog_is_serialisable_for_the_api() -> None:
    import json

    payload = catalog()
    assert len(payload) == 9
    json.dumps(payload)  # GET /tools must be JSON-serialisable
    assert {e["name"] for e in payload} == {t.value for t in ToolName}
