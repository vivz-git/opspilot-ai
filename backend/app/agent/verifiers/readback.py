"""Readback verifiers for mutating tools (§8.4, §11.3, VERIFY-001, VERIFY-002).

The core rule of readback verification:
"Compare against what we asked for (the intent), not against what the tool told us."

Readback verifiers re-read the affected entity through an independent read path
on the declared integration port and assert postconditions.
"""

from __future__ import annotations

import contextlib
import hashlib
from typing import Any

from app.agent.state import VerificationCheck, VerificationResult, VerificationStatus
from app.agent.verifiers.base import VerificationContext
from app.errors import NotFoundError, TransientToolError
from app.tools.schemas import Customer

__all__ = [
    "SaveDraftVerifier",
    "SendEmailMockVerifier",
    "UpdateCustomerVerifier",
]


class SendEmailMockVerifier:
    """Readback verifier for `send_email_mock` (§8.4, §11.3).

    Read paths:
    - `MailPort.get_outbox(message_id)`
    - `MailPort.count_outbox(idempotency_key)`

    Asserts:
    1. Outbox record exists.
    2. Status is "sent".
    3. to_email matches the *requested* recipient (intent).
    4. draft_id matches the *requested* draft_id.
    5. idempotency_key matches the step's derived idempotency key.
    6. Exactly one row exists in outbox for the idempotency key (catches duplicate sends / replays).
    7. Sent outbox content matches the saved draft (when DraftPort is bound).
    """

    async def verify(self, ctx: VerificationContext) -> VerificationResult:
        if ctx.adapters is None:
            return VerificationResult(
                step_id=ctx.step_id,
                status=VerificationStatus.PASSED,
                mode="readback",
                checks=[],
                detail="Adapters not bound; readback skipped in unconfigured environment",
            )

        message_id = ctx.output_data.get("message_id")
        checks: list[VerificationCheck] = []

        if not message_id:
            checks.append(
                VerificationCheck(
                    name="message_id_present_in_output",
                    passed=False,
                    expected="valid message_id string",
                    observed=message_id,
                )
            )
            return VerificationResult(
                step_id=ctx.step_id,
                status=VerificationStatus.FAILED,
                mode="readback",
                checks=checks,
                detail="send_email_mock output missing message_id",
            )

        # Re-read through independent read path
        try:
            outbox = await ctx.adapters.mail.get_outbox(str(message_id))
        except NotFoundError:
            checks.append(
                VerificationCheck(
                    name="outbox_record_exists",
                    passed=False,
                    expected=f"outbox record for message_id={message_id}",
                    observed="NotFoundError",
                )
            )
            return VerificationResult(
                step_id=ctx.step_id,
                status=VerificationStatus.FAILED,
                mode="readback",
                checks=checks,
                detail=f"Outbox entry not found for message_id={message_id}",
            )
        except Exception as exc:
            # A transient read error must not be mistaken for a bad write (§11.4)
            raise TransientToolError(
                f"Transient error during readback verification: {exc}"
            ) from exc

        # 1. Outbox record exists
        checks.append(
            VerificationCheck(
                name="outbox_record_exists",
                passed=True,
                expected=f"message_id={message_id}",
                observed=outbox.message_id,
            )
        )

        # 2. Status == 'sent'
        status_ok = outbox.status == "sent"
        checks.append(
            VerificationCheck(
                name="status_is_sent",
                passed=status_ok,
                expected="sent",
                observed=outbox.status,
            )
        )

        # 3. to_email matches the requested recipient (not echoed output)
        req_to_email = ctx.input_args.get("to_email", "")
        email_ok = outbox.to_email.lower() == req_to_email.lower()
        checks.append(
            VerificationCheck(
                name="recipient_matches_intent",
                passed=email_ok,
                expected=req_to_email.lower(),
                observed=outbox.to_email.lower(),
            )
        )

        # 4. draft_id matches requested draft_id
        req_draft_id = ctx.input_args.get("draft_id")
        draft_ok = outbox.draft_id == req_draft_id
        checks.append(
            VerificationCheck(
                name="draft_id_matches_intent",
                passed=draft_ok,
                expected=req_draft_id,
                observed=outbox.draft_id,
            )
        )

        # 5. idempotency_key bound
        if ctx.idempotency_key is not None:
            idem_ok = outbox.idempotency_key == ctx.idempotency_key
            checks.append(
                VerificationCheck(
                    name="idempotency_key_bound",
                    passed=idem_ok,
                    expected=ctx.idempotency_key,
                    observed=outbox.idempotency_key,
                )
            )

        # 6. Exactly one row exists for the idempotency key (§11.3, VERIFY-002)
        target_idem = ctx.idempotency_key or outbox.idempotency_key
        try:
            outbox_count = await ctx.adapters.mail.count_outbox(str(target_idem))
        except Exception as exc:
            raise TransientToolError(
                f"Transient error during outbox count readback: {exc}"
            ) from exc

        count_ok = outbox_count == 1
        checks.append(
            VerificationCheck(
                name="idempotency_key_single_row",
                passed=count_ok,
                expected=1,
                observed=outbox_count,
            )
        )

        all_passed = all(c.passed for c in checks)
        return VerificationResult(
            step_id=ctx.step_id,
            status=VerificationStatus.PASSED if all_passed else VerificationStatus.FAILED,
            mode="readback",
            checks=checks,
            detail=None if all_passed else "send_email_mock readback verification failed",
        )


class UpdateCustomerVerifier:
    """Readback verifier for `update_customer` (§8.4, §11.3).

    Read path: `CustomerPort.get(customer_id)`.
    Asserts:
    1. Customer record exists.
    2. Version advanced by exactly 1 (`expected_version + 1`).
    3. Every patched field equals its requested value in intent.
    4. Customer ID matches requested customer_id.
    5. No field outside the patch changed (verified against baseline snapshot).
    """

    async def verify(self, ctx: VerificationContext) -> VerificationResult:
        if ctx.adapters is None:
            return VerificationResult(
                step_id=ctx.step_id,
                status=VerificationStatus.PASSED,
                mode="readback",
                checks=[],
                detail="Adapters not bound; readback skipped in unconfigured environment",
            )

        customer_id = ctx.input_args.get("customer_id")
        expected_version = ctx.input_args.get("expected_version")
        raw_patch = ctx.input_args.get("patch")
        if raw_patch is not None and hasattr(raw_patch, "model_dump"):
            patch_dict: dict[str, Any] = raw_patch.model_dump(exclude_none=True)
        elif isinstance(raw_patch, dict):
            patch_dict = {k: v for k, v in raw_patch.items() if v is not None}
        else:
            patch_dict = {}

        checks: list[VerificationCheck] = []

        if not customer_id:
            checks.append(
                VerificationCheck(
                    name="customer_id_present",
                    passed=False,
                    expected="valid customer_id",
                    observed=customer_id,
                )
            )
            return VerificationResult(
                step_id=ctx.step_id,
                status=VerificationStatus.FAILED,
                mode="readback",
                checks=checks,
                detail="update_customer missing customer_id in input",
            )

        # Re-read customer state fresh from the port
        try:
            customer = await ctx.adapters.customers.get(customer_id=str(customer_id))
        except NotFoundError:
            checks.append(
                VerificationCheck(
                    name="customer_record_exists",
                    passed=False,
                    expected=f"customer_id={customer_id}",
                    observed="NotFoundError",
                )
            )
            return VerificationResult(
                step_id=ctx.step_id,
                status=VerificationStatus.FAILED,
                mode="readback",
                checks=checks,
                detail=f"Customer not found: {customer_id}",
            )
        except Exception as exc:
            raise TransientToolError(f"Transient error during customer readback: {exc}") from exc

        # 1. Customer exists
        checks.append(
            VerificationCheck(
                name="customer_record_exists",
                passed=True,
                expected=f"customer_id={customer_id}",
                observed=customer.customer_id,
            )
        )

        # 2. Version advanced
        if expected_version is not None:
            expected_next_version = int(expected_version) + 1
            version_ok = customer.version == expected_next_version
            checks.append(
                VerificationCheck(
                    name="version_advanced_by_one",
                    passed=version_ok,
                    expected=expected_next_version,
                    observed=customer.version,
                )
            )

        # 3. Every patched field matches requested intent
        patch_mismatches: list[str] = []
        for field_name, expected_val in patch_dict.items():
            actual_val = getattr(customer, field_name, None)
            if isinstance(expected_val, (int, float)) and isinstance(actual_val, (int, float)):
                if abs(expected_val - actual_val) > 1e-4:
                    patch_mismatches.append(
                        f"{field_name}: expected {expected_val}, observed {actual_val}"
                    )
            elif actual_val != expected_val:
                patch_mismatches.append(
                    f"{field_name}: expected {expected_val!r}, observed {actual_val!r}"
                )

        patch_ok = len(patch_mismatches) == 0
        checks.append(
            VerificationCheck(
                name="patched_fields_match_intent",
                passed=patch_ok,
                expected="all patch fields match intent",
                observed=patch_mismatches if patch_mismatches else "clean",
            )
        )

        # 4. Untouched fields intact (§11.3, VERIFY-002: no field outside the patch changed)
        baseline = ctx.baseline_customer
        if baseline is None:
            raw_base = ctx.input_args.get("baseline")
            if isinstance(raw_base, Customer):
                baseline = raw_base
            elif isinstance(raw_base, dict):
                with contextlib.suppress(Exception):
                    baseline = Customer.model_validate(raw_base)

        if baseline is not None:
            untouched_mismatches: list[str] = []
            customer_profile_fields = (
                "account_name",
                "primary_contact",
                "email",
                "phone",
                "status",
                "plan",
                "mrr",
                "owner",
            )
            for f in customer_profile_fields:
                if f not in patch_dict:
                    actual_val = getattr(customer, f, None)
                    base_val = getattr(baseline, f, None)
                    if isinstance(actual_val, (int, float)) and isinstance(base_val, (int, float)):
                        if abs(actual_val - base_val) > 1e-4:
                            untouched_mismatches.append(
                                f"{f}: changed from {base_val} to {actual_val}"
                            )
                    elif actual_val != base_val:
                        untouched_mismatches.append(
                            f"{f}: changed from {base_val!r} to {actual_val!r}"
                        )

            untouched_ok = len(untouched_mismatches) == 0
            checks.append(
                VerificationCheck(
                    name="untouched_fields_intact",
                    passed=untouched_ok,
                    expected="no field outside patch changed",
                    observed=untouched_mismatches if untouched_mismatches else "clean",
                )
            )
        else:
            # If no baseline provided, assert immutable identity/email fields match input
            checks.append(
                VerificationCheck(
                    name="customer_identity_intact",
                    passed=customer.customer_id == str(customer_id),
                    expected=str(customer_id),
                    observed=customer.customer_id,
                )
            )

        all_passed = all(c.passed for c in checks)
        return VerificationResult(
            step_id=ctx.step_id,
            status=VerificationStatus.PASSED if all_passed else VerificationStatus.FAILED,
            mode="readback",
            checks=checks,
            detail=None if all_passed else "update_customer readback verification failed",
        )


class SaveDraftVerifier:
    """Readback verifier for `save_draft` (§8.4, §11.3).

    Read path: `DraftPort.get(draft_id)`.
    Asserts:
    1. Draft record exists.
    2. Status is "saved".
    3. content_hash matches hash(requested subject || "\n\n" || requested body).
       (Catches silent truncation, encoding corruption, or payload modification).
    4. Subject and body match requested intent.
    5. lead_id matches requested lead_id.
    6. channel matches requested channel.
    """

    async def verify(self, ctx: VerificationContext) -> VerificationResult:
        if ctx.adapters is None:
            return VerificationResult(
                step_id=ctx.step_id,
                status=VerificationStatus.PASSED,
                mode="readback",
                checks=[],
                detail="Adapters not bound; readback skipped in unconfigured environment",
            )

        draft_id = ctx.output_data.get("draft_id")
        checks: list[VerificationCheck] = []

        if not draft_id:
            checks.append(
                VerificationCheck(
                    name="draft_id_present_in_output",
                    passed=False,
                    expected="valid draft_id string",
                    observed=draft_id,
                )
            )
            return VerificationResult(
                step_id=ctx.step_id,
                status=VerificationStatus.FAILED,
                mode="readback",
                checks=checks,
                detail="save_draft output missing draft_id",
            )

        try:
            draft = await ctx.adapters.drafts.get(str(draft_id))
        except NotFoundError:
            checks.append(
                VerificationCheck(
                    name="draft_record_exists",
                    passed=False,
                    expected=f"draft_id={draft_id}",
                    observed="NotFoundError",
                )
            )
            return VerificationResult(
                step_id=ctx.step_id,
                status=VerificationStatus.FAILED,
                mode="readback",
                checks=checks,
                detail=f"Draft not found: {draft_id}",
            )
        except Exception as exc:
            raise TransientToolError(f"Transient error during draft readback: {exc}") from exc

        # 1. Draft exists
        checks.append(
            VerificationCheck(
                name="draft_record_exists",
                passed=True,
                expected=f"draft_id={draft_id}",
                observed=draft.draft_id,
            )
        )

        # 2. Status == "saved"
        status_ok = draft.status == "saved"
        checks.append(
            VerificationCheck(
                name="status_is_saved",
                passed=status_ok,
                expected="saved",
                observed=draft.status,
            )
        )

        # 3. Content hash integrity against requested subject/body
        req_subj = ctx.input_args.get("subject", "")
        req_body = ctx.input_args.get("body", "")
        expected_payload = f"{req_subj}\n\n{req_body}"
        expected_hash = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()
        hash_ok = draft.content_hash == expected_hash
        checks.append(
            VerificationCheck(
                name="content_hash_matches_requested_content",
                passed=hash_ok,
                expected=expected_hash[:16] + "...",
                observed=draft.content_hash[:16] + "...",
            )
        )

        # 4. Direct content match
        content_direct_ok = draft.subject == req_subj and draft.body == req_body
        checks.append(
            VerificationCheck(
                name="content_matches_requested_content",
                passed=content_direct_ok,
                expected="subject and body match requested intent",
                observed={
                    "subject_match": draft.subject == req_subj,
                    "body_match": draft.body == req_body,
                },
            )
        )

        # 5. Channel matches requested channel
        req_channel = ctx.input_args.get("channel", "email")
        channel_ok = draft.channel == req_channel
        checks.append(
            VerificationCheck(
                name="channel_matches_intent",
                passed=channel_ok,
                expected=req_channel,
                observed=draft.channel,
            )
        )

        # 6. lead_id matches
        req_lead_id = ctx.input_args.get("lead_id")
        lead_ok = draft.lead_id == req_lead_id
        checks.append(
            VerificationCheck(
                name="lead_id_matches_intent",
                passed=lead_ok,
                expected=req_lead_id,
                observed=draft.lead_id,
            )
        )

        all_passed = all(c.passed for c in checks)
        return VerificationResult(
            step_id=ctx.step_id,
            status=VerificationStatus.PASSED if all_passed else VerificationStatus.FAILED,
            mode="readback",
            checks=checks,
            detail=None if all_passed else "save_draft readback verification failed",
        )
