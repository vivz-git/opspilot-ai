"""Verifier registry and default bindings (§11.2, VERIFY-001).

Maps ToolContract / ToolName to its designated postcondition verifier.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from app.agent.state import VerificationResult, VerificationStatus
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
from app.errors import ConfigurationError
from app.tools.contracts import ToolContract, ToolName, VerificationMode

__all__ = [
    "DEFAULT_VERIFIERS",
    "NullVerifier",
    "VerifierRegistry",
    "default_verifiers",
]


class NullVerifier:
    """Verifier for `VerificationMode.NONE` tools (`get_lead`, `get_customer`).

    Per §11.2: "NONE still writes a VerificationResult. 'We checked nothing here,
    on purpose' is information; a silent gap is not."
    """

    async def verify(self, ctx: VerificationContext) -> VerificationResult:
        return VerificationResult(
            step_id=ctx.step_id,
            status=VerificationStatus.NOT_REQUIRED,
            mode="none",
            checks=[],
            detail="VerificationMode.NONE: schema validation only; no postconditions required",
        )


DEFAULT_VERIFIERS: Final[dict[ToolName, Verifier]] = {
    ToolName.SEARCH_LEADS: SearchLeadsVerifier(),
    ToolName.GET_LEAD: NullVerifier(),
    ToolName.RESEARCH_COMPANY: ResearchCompanyVerifier(),
    ToolName.SCORE_LEAD: ScoreLeadVerifier(),
    ToolName.DRAFT_OUTREACH: DraftOutreachVerifier(),
    ToolName.SAVE_DRAFT: SaveDraftVerifier(),
    ToolName.SEND_EMAIL_MOCK: SendEmailMockVerifier(),
    ToolName.GET_CUSTOMER: NullVerifier(),
    ToolName.UPDATE_CUSTOMER: UpdateCustomerVerifier(),
}


def default_verifiers() -> dict[ToolName, Verifier]:
    """Return a fresh dictionary mapping all tools to default verifiers."""
    return dict(DEFAULT_VERIFIERS)


class VerifierRegistry:
    """Registry managing verifiers for all tools (§11.2)."""

    def __init__(self, verifiers: Mapping[ToolName, Verifier] | None = None) -> None:
        self._verifiers: dict[ToolName, Verifier] = (
            dict(verifiers) if verifiers is not None else default_verifiers()
        )
        self._null_verifier = NullVerifier()

    def get_verifier(self, contract: ToolContract) -> Verifier:
        """Resolve a contract to its verifier.

        If the contract declares `VerificationMode.NONE`, returns `NullVerifier`.
        Otherwise retrieves the verifier registered for `contract.name`.
        """
        if contract.verification is VerificationMode.NONE:
            return self._verifiers.get(contract.name, self._null_verifier)

        verifier = self._verifiers.get(contract.name)
        if verifier is None:
            raise ConfigurationError(
                f"No verifier registered for tool {contract.name.value} "
                f"with verification mode {contract.verification.value}"
            )
        return verifier
