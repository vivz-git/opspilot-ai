"""Error classification and the recovery decision (§10.1-§10.4)."""

from __future__ import annotations

import pytest

from app.errors import (
    ALWAYS_RETRYABLE,
    REPLANNABLE,
    TERMINAL_ERRORS,
    ErrorClass,
    RecoveryAction,
    backoff_delay_ms,
    is_retryable,
    recovery_action,
)
from app.tools.contracts import REGISTRY, ToolName

pytestmark = [pytest.mark.unit]

IDEMPOTENT_DETERMINISTIC = {"idempotent": True, "nondeterministic": False}


def act(error_class: ErrorClass, **overrides: object) -> RecoveryAction:
    kwargs: dict = {
        "idempotent": True,
        "nondeterministic": False,
        "retries_remaining": 2,
        "replans_remaining": 2,
        "step_optional": False,
    }
    kwargs.update(overrides)
    return recovery_action(error_class, **kwargs)  # type: ignore[arg-type]


class TestRetryability:
    @pytest.mark.parametrize("cls", sorted(ALWAYS_RETRYABLE))
    def test_world_failures_are_retryable(self, cls: ErrorClass) -> None:
        assert is_retryable(cls, **IDEMPOTENT_DETERMINISTIC)

    @pytest.mark.parametrize("cls", sorted(TERMINAL_ERRORS))
    def test_terminal_classes_are_never_retryable(self, cls: ErrorClass) -> None:
        assert not is_retryable(cls, idempotent=True, nondeterministic=True)

    def test_planning_faults_are_not_retryable(self) -> None:
        """The same call with the same broken argument cannot succeed (§10.1)."""
        assert not is_retryable(ErrorClass.INPUT_VALIDATION, **IDEMPOTENT_DETERMINISTIC)
        assert not is_retryable(ErrorClass.REFERENCE_RESOLUTION, **IDEMPOTENT_DETERMINISTIC)
        assert not is_retryable(ErrorClass.NOT_FOUND, **IDEMPOTENT_DETERMINISTIC)

    def test_output_validation_is_retryable_only_when_nondeterministic(self) -> None:
        assert is_retryable(ErrorClass.OUTPUT_VALIDATION, idempotent=True, nondeterministic=True)
        assert not is_retryable(ErrorClass.OUTPUT_VALIDATION, **IDEMPOTENT_DETERMINISTIC)

    def test_verification_failure_is_retryable_only_when_idempotent(self) -> None:
        """Invariant P5: never retry an unverified non-idempotent mutation."""
        assert is_retryable(ErrorClass.VERIFICATION_FAILED, idempotent=True, nondeterministic=False)
        assert not is_retryable(
            ErrorClass.VERIFICATION_FAILED, idempotent=False, nondeterministic=False
        )


class TestRecoveryDecision:
    def test_policy_violation_fails_before_anything_else(self) -> None:
        assert act(ErrorClass.POLICY_VIOLATION, retries_remaining=99, step_optional=True) is RecoveryAction.FAIL

    def test_budget_exhaustion_outranks_a_remaining_retry(self) -> None:
        assert act(ErrorClass.TRANSIENT, budget_exhausted=True) is RecoveryAction.FAIL

    def test_transient_failure_retries_while_budget_remains(self) -> None:
        assert act(ErrorClass.TRANSIENT, retries_remaining=1) is RecoveryAction.RETRY

    def test_boundary_exhausted_retries_do_not_retry(self) -> None:
        """retry_count == MAX_RETRIES must fail or replan, never retry again."""
        assert act(ErrorClass.TRANSIENT, retries_remaining=0) is RecoveryAction.FAIL

    def test_planning_fault_replans_rather_than_retrying(self) -> None:
        assert act(ErrorClass.REFERENCE_RESOLUTION) is RecoveryAction.REPLAN
        assert act(ErrorClass.INPUT_VALIDATION) is RecoveryAction.REPLAN

    def test_stale_write_replans_so_the_human_re_approves(self) -> None:
        """§10.3 — a new plan produces new args, a new hash, and a fresh
        approval. Re-applying an approved patch blindly is the bug this
        prevents."""
        assert act(ErrorClass.STALE_WRITE) is RecoveryAction.REPLAN

    def test_optional_step_is_skipped_before_spending_replan_budget(self) -> None:
        assert act(ErrorClass.NOT_FOUND, step_optional=True) is RecoveryAction.SKIP

    def test_exhausted_replans_fail(self) -> None:
        assert act(ErrorClass.NOT_FOUND, replans_remaining=0) is RecoveryAction.FAIL

    def test_unverified_non_idempotent_mutation_fails_rather_than_retrying(self) -> None:
        assert (
            act(ErrorClass.VERIFICATION_FAILED, idempotent=False, replans_remaining=0)
            is RecoveryAction.FAIL
        )

    @pytest.mark.parametrize("cls", sorted(REPLANNABLE))
    def test_every_replannable_class_reaches_replan(self, cls: ErrorClass) -> None:
        assert act(cls, nondeterministic=False, retries_remaining=0) is RecoveryAction.REPLAN

    def test_no_error_class_is_unhandled(self) -> None:
        """Every class in the taxonomy yields a decision — there is no
        implicit error sink (§7 `fail`)."""
        for cls in ErrorClass:
            assert isinstance(act(cls), RecoveryAction)


class TestBackoff:
    def test_delay_grows_exponentially_and_is_capped(self) -> None:
        delays = [backoff_delay_ms(a, base_ms=250, max_ms=8000) for a in (1, 2, 3, 4, 10)]
        assert delays == [250, 500, 1000, 2000, 8000]

    def test_jitter_is_injected_so_evaluations_stay_deterministic(self) -> None:
        assert backoff_delay_ms(2, base_ms=250, max_ms=8000, jitter=0.8) == 400
        assert backoff_delay_ms(2, base_ms=250, max_ms=8000, jitter=1.2) == 600

    def test_server_hint_wins_when_longer(self) -> None:
        assert backoff_delay_ms(1, base_ms=250, max_ms=8000, retry_after_ms=5000) == 5000
        assert backoff_delay_ms(4, base_ms=250, max_ms=8000, retry_after_ms=100) == 2000

    def test_attempts_are_one_based(self) -> None:
        with pytest.raises(ValueError):
            backoff_delay_ms(0, base_ms=250, max_ms=8000)


class TestContractsAgreeWithThePolicy:
    """The registry's `retryable_errors` must not contradict the taxonomy."""

    @pytest.mark.parametrize("name", sorted(REGISTRY), ids=lambda n: n.value)
    def test_declared_retryable_errors_are_actually_retryable(self, name: ToolName) -> None:
        c = REGISTRY[name]
        for cls in c.retryable_errors:
            assert is_retryable(
                cls, idempotent=c.idempotent, nondeterministic=c.nondeterministic
            ), f"{name} declares {cls} retryable but the policy forbids it"

    def test_bounded_attempts(self) -> None:
        """Worst case is 1 + MAX_RETRIES attempts per step (§10.5)."""
        max_retries = 2
        attempts = 1
        remaining = max_retries
        while act(ErrorClass.TRANSIENT, retries_remaining=remaining) is RecoveryAction.RETRY:
            attempts += 1
            remaining -= 1
            assert attempts <= 1 + max_retries, "unbounded retry loop"
        assert attempts == 1 + max_retries
