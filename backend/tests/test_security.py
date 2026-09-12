"""Approval binding and the token barrier (§9.4-§9.5, §16.2)."""

from __future__ import annotations

import pytest

from app.errors import PolicyViolation
from app.security import VOLATILE_ARG_KEYS, ApprovalGate, ApprovalToken, canonical_args_hash

pytestmark = [pytest.mark.unit]

ARGS = {"draft_id": "d_1", "to_email": "dana@northwind.example"}


def mint(args: dict | None = None, approved: dict | None = None) -> ApprovalToken:
    payload = args or ARGS
    return ApprovalGate.issue(
        approval_id="a_1",
        run_id="r_1",
        step_id="s6",
        args=payload,
        approved_args_hash=canonical_args_hash(approved or payload),
        decision="approve",
    )


class TestCanonicalHash:
    def test_key_order_does_not_change_the_hash(self) -> None:
        assert canonical_args_hash({"a": 1, "b": 2}) == canonical_args_hash({"b": 2, "a": 1})

    def test_nested_key_order_does_not_change_the_hash(self) -> None:
        left = {"patch": {"plan": "pro", "status": "active"}}
        right = {"patch": {"status": "active", "plan": "pro"}}
        assert canonical_args_hash(left) == canonical_args_hash(right)

    def test_any_meaningful_change_changes_the_hash(self) -> None:
        base = canonical_args_hash(ARGS)
        assert canonical_args_hash({**ARGS, "draft_id": "d_2"}) != base
        assert canonical_args_hash({**ARGS, "to_email": "ceo@acme.example"}) != base

    @pytest.mark.parametrize("key", sorted(VOLATILE_ARG_KEYS))
    def test_volatile_keys_are_excluded(self, key: str) -> None:
        """A retry changes the idempotency key but not the operation; the
        grant must survive it."""
        assert canonical_args_hash({**ARGS, key: "anything"}) == canonical_args_hash(ARGS)

    def test_volatile_keys_are_excluded_recursively(self) -> None:
        nested = {"outer": {"idempotency_key": "k", "value": 1}}
        assert canonical_args_hash(nested) == canonical_args_hash({"outer": {"value": 1}})

    def test_list_order_is_significant(self) -> None:
        assert canonical_args_hash({"ids": ["a", "b"]}) != canonical_args_hash({"ids": ["b", "a"]})


class TestTokenBarrier:
    def test_direct_construction_is_refused(self) -> None:
        """Barrier 3 of §9.5: only ApprovalGate.issue can mint a token."""
        with pytest.raises(PolicyViolation):
            ApprovalToken(approval_id="a", run_id="r", step_id="s", args_hash="h")

    def test_the_gate_can_mint(self) -> None:
        assert mint().args_hash == canonical_args_hash(ARGS)

    def test_a_rejection_cannot_mint_a_token(self) -> None:
        with pytest.raises(PolicyViolation):
            ApprovalGate.issue(
                approval_id="a_1",
                run_id="r_1",
                step_id="s6",
                args=ARGS,
                approved_args_hash=canonical_args_hash(ARGS),
                decision="reject",
            )

    def test_changed_arguments_cannot_mint_a_token(self) -> None:
        with pytest.raises(PolicyViolation) as exc:
            mint(args={**ARGS, "draft_id": "d_2"}, approved=ARGS)
        assert "new approval is required" in str(exc.value)

    def test_retry_can_still_mint_despite_a_new_idempotency_key(self) -> None:
        token = mint(args={**ARGS, "idempotency_key": "k_attempt_2"}, approved=ARGS)
        assert token.authorises(run_id="r_1", step_id="s6", args=ARGS)

    def test_the_mint_sentinel_is_not_stored_on_the_instance(self) -> None:
        """An InitVar is consumed by __post_init__ and never becomes instance
        state, so the sentinel cannot leak into a trace, a log or an API
        response. (`mint` remains a class-level None because of the default;
        what matters is that the sentinel itself is unreachable.)"""
        from app.security import _MINT

        token = mint()
        assert set(vars(token)) == {"approval_id", "run_id", "step_id", "args_hash"}
        assert _MINT not in vars(token).values()
        assert "mint" not in repr(token)

    def test_a_token_is_immutable(self) -> None:
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            mint().args_hash = "forged"  # type: ignore[misc]


class TestTokenAuthorisation:
    def test_authorises_the_exact_call(self) -> None:
        assert mint().authorises(run_id="r_1", step_id="s6", args=ARGS)

    def test_refuses_a_different_payload(self) -> None:
        token = mint()
        assert not token.authorises(run_id="r_1", step_id="s6", args={**ARGS, "draft_id": "d_2"})

    def test_refuses_a_different_step(self) -> None:
        assert not mint().authorises(run_id="r_1", step_id="s9", args=ARGS)

    def test_refuses_a_replay_into_another_run(self) -> None:
        """A token from one run must not authorise the same effect in another."""
        assert not mint().authorises(run_id="r_2", step_id="s6", args=ARGS)
