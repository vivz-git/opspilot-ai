"""Contract-driven postcondition verifiers (§11, VERIFY-001)."""

from __future__ import annotations

from app.agent.verifiers.base import VerificationContext, Verifier
from app.agent.verifiers.invariants import (
    DraftOutreachVerifier,
    ResearchCompanyVerifier,
    ScoreLeadVerifier,
    SearchLeadsVerifier,
)
from app.agent.verifiers.readback import (
    SaveDraftVerifier,
    SendEmailMockVerifier,
    UpdateCustomerVerifier,
)
from app.agent.verifiers.registry import (
    DEFAULT_VERIFIERS,
    NullVerifier,
    VerifierRegistry,
    default_verifiers,
)

__all__ = [
    "DEFAULT_VERIFIERS",
    "DraftOutreachVerifier",
    "NullVerifier",
    "ResearchCompanyVerifier",
    "SaveDraftVerifier",
    "ScoreLeadVerifier",
    "SearchLeadsVerifier",
    "SendEmailMockVerifier",
    "UpdateCustomerVerifier",
    "VerificationContext",
    "Verifier",
    "VerifierRegistry",
    "default_verifiers",
]
