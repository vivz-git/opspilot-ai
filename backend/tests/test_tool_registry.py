"""`ToolRegistry` construction and pure resolution — no database (TOOL-002).

The behaviour of `dispatch` against real Postgres is in
`tests/test_tool_dispatch.py`; this file proves the registry fails closed
before it ever exists, and that an unknown tool is refused before any I/O.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.errors import ConfigurationError, ErrorClass, InputValidationError
from app.integrations.ports import PORT_FIELDS, Adapters
from app.runtime import FixedClock
from app.security import idempotency_key_for
from app.tools.contracts import (
    REGISTRY,
    SideEffect,
    ToolContract,
    ToolName,
    VerificationMode,
    policy_violations,
)
from app.tools.registry import ToolRegistry, UnknownToolError
from app.tools.schemas import GetLeadInput, GetLeadOutput
from tests.recovery_harness import T0

pytestmark = [pytest.mark.unit]


def _adapters() -> Adapters:
    # Six opaque objects: resolution is by name, and nothing here dispatches.
    return Adapters(*(object() for _ in PORT_FIELDS))  # type: ignore[arg-type]


def _no_io() -> Any:
    raise AssertionError("the unit of work must not be opened")


async def _noop(args: Any, ctx: Any) -> Any:
    raise AssertionError("must not execute")


def _registry(**overrides: Any) -> ToolRegistry:
    kwargs: dict[str, Any] = {
        "adapters": _adapters(),
        "uow_factory": _no_io,
        "clock": FixedClock(T0),
        "implementations": {},
    }
    kwargs.update(overrides)
    return ToolRegistry(**kwargs)


class TestConstructionFailsClosed:
    def test_the_shipped_registry_is_accepted(self) -> None:
        registry = _registry(implementations={ToolName.GET_LEAD: _noop})
        assert registry.contract("get_lead") is REGISTRY[ToolName.GET_LEAD]
        assert registry.is_bound(ToolName.GET_LEAD)
        assert not registry.is_bound(ToolName.SEND_EMAIL_MOCK)

    def test_a_contract_violating_a_policy_invariant_is_refused(self) -> None:
        """An injected contract cannot execute around its own metadata: an
        OUTBOUND tool that claims not to need approval violates P1."""
        bad = REGISTRY[ToolName.SEND_EMAIL_MOCK].model_copy(update={"requires_approval": False})
        assert policy_violations(bad) == ["P1"]
        with pytest.raises(ConfigurationError) as exc:
            _registry(contracts={**REGISTRY, ToolName.SEND_EMAIL_MOCK: bad})
        assert "P1" in str(exc.value.detail)

    def test_a_mutating_contract_without_readback_is_refused(self) -> None:
        bad = REGISTRY[ToolName.SAVE_DRAFT].model_copy(
            update={"verification": VerificationMode.INVARIANT}
        )
        assert policy_violations(bad) == ["P2"]
        with pytest.raises(ConfigurationError):
            _registry(contracts={**REGISTRY, ToolName.SAVE_DRAFT: bad})

    def test_a_gated_read_is_refused(self) -> None:
        bad = REGISTRY[ToolName.GET_LEAD].model_copy(update={"requires_approval": True})
        assert set(policy_violations(bad)) == {"P3", "P4"}
        with pytest.raises(ConfigurationError):
            _registry(contracts={**REGISTRY, ToolName.GET_LEAD: bad})

    def test_an_implementation_without_a_contract_is_refused(self) -> None:
        contracts = {k: v for k, v in REGISTRY.items() if k is not ToolName.GET_LEAD}
        with pytest.raises(ConfigurationError) as exc:
            _registry(contracts=contracts, implementations={ToolName.GET_LEAD: _noop})
        assert "without a contract" in str(exc.value.detail)

    def test_a_contract_naming_an_unsupported_port_is_refused(self) -> None:
        bad = REGISTRY[ToolName.GET_LEAD].model_copy(update={"port": "TelephonyPort"})
        with pytest.raises(ConfigurationError) as exc:
            _registry(contracts={**REGISTRY, ToolName.GET_LEAD: bad})
        assert "unsupported port" in str(exc.value.detail)

    def test_a_contract_registered_under_another_name_is_refused(self) -> None:
        with pytest.raises(ConfigurationError):
            _registry(contracts={**REGISTRY, ToolName.GET_LEAD: REGISTRY[ToolName.GET_CUSTOMER]})

    def test_every_shipped_contract_is_policy_clean(self) -> None:
        assert {c.name: policy_violations(c) for c in REGISTRY.values()} == {
            name: [] for name in REGISTRY
        }

    def test_every_declared_port_is_resolvable(self) -> None:
        adapters = _adapters()
        for c in REGISTRY.values():
            if c.port is not None:
                assert adapters.port(c.port) is getattr(adapters, PORT_FIELDS[c.port])
        with pytest.raises(KeyError):
            adapters.port("TelephonyPort")


class TestResolution:
    async def test_unknown_tool_is_refused_before_any_io(self) -> None:
        registry = _registry()
        with pytest.raises(UnknownToolError) as exc:
            await registry.dispatch(
                run_id=uuid.uuid4(),
                execution_step_id=uuid.uuid4(),
                step_id="s1",
                tool_name="delete_everything",
                arguments={},
                attempt=1,
            )
        assert isinstance(exc.value, InputValidationError)
        assert exc.value.error_class is ErrorClass.INPUT_VALIDATION
        assert exc.value.detail == {"tool": "delete_everything"}

    async def test_a_known_name_missing_from_this_registry_is_still_unknown(self) -> None:
        contracts = {k: v for k, v in REGISTRY.items() if k is not ToolName.GET_LEAD}
        registry = _registry(contracts=contracts)
        with pytest.raises(UnknownToolError):
            registry.contract(ToolName.GET_LEAD)

    async def test_attempt_numbers_are_one_based(self) -> None:
        registry = _registry(implementations={ToolName.GET_LEAD: _noop})
        with pytest.raises(Exception, match="1-based"):
            await registry.dispatch(
                run_id=uuid.uuid4(),
                execution_step_id=uuid.uuid4(),
                step_id="s1",
                tool_name=ToolName.GET_LEAD,
                arguments={"lead_id": "L-104"},
                attempt=0,
            )


class TestIdempotencyKeyDerivation:
    def test_the_key_is_attempt_invariant_and_argument_bound(self) -> None:
        """ADR-020: derived from (run_id, step_id, args_hash) and nothing else."""
        a = idempotency_key_for(run_id="r", step_id="s6", args_hash="h1")
        assert a == idempotency_key_for(run_id="r", step_id="s6", args_hash="h1")
        assert a != idempotency_key_for(run_id="r", step_id="s7", args_hash="h1")
        assert a != idempotency_key_for(run_id="r2", step_id="s6", args_hash="h1")
        assert a != idempotency_key_for(run_id="r", step_id="s6", args_hash="h2")
        real = idempotency_key_for(run_id=str(uuid.uuid4()), step_id="s6", args_hash="a" * 64)
        assert len(real) >= 8  # satisfies the gated input models' min_length


def test_policy_violations_ignore_read_only_tools_without_ports() -> None:
    pure = ToolContract(
        name=ToolName.GET_LEAD,
        purpose="x",
        input_model=GetLeadInput,
        output_model=GetLeadOutput,
        side_effect=SideEffect.READ_ONLY,
        requires_approval=False,
        risk="low",  # type: ignore[arg-type]
        verification=VerificationMode.NONE,
        idempotent=True,
    )
    assert policy_violations(pure) == []
