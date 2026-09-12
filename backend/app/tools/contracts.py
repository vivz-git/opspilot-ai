"""The tool contract registry (§8).

A tool is a contract, not a function. The contract — not the implementation —
is what the planner reads, the approval gate consults, the verifier obeys,
`GET /tools` publishes and the dashboard renders.

The seven policy invariants in `POLICY_INVARIANTS` are asserted by
`tests/test_tool_policy.py`, which iterates this registry. A future tool that
mutates customer data without an approval flag therefore fails CI rather than
depending on a reviewer noticing.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from app.errors import ErrorClass
from app.tools import schemas as s


class ToolName(StrEnum):
    """The agent's entire vocabulary. The planner cannot name anything else."""

    SEARCH_LEADS = "search_leads"
    GET_LEAD = "get_lead"
    RESEARCH_COMPANY = "research_company"
    SCORE_LEAD = "score_lead"
    DRAFT_OUTREACH = "draft_outreach"
    SAVE_DRAFT = "save_draft"
    SEND_EMAIL_MOCK = "send_email_mock"
    GET_CUSTOMER = "get_customer"
    UPDATE_CUSTOMER = "update_customer"


class SideEffect(StrEnum):
    READ_ONLY = "read_only"
    INTERNAL_WRITE = "internal_write"      # our own artifact; reversible
    CUSTOMER_WRITE = "customer_write"      # business-owned records
    OUTBOUND = "outbound"                  # leaves the system
    DESTRUCTIVE = "destructive"            # irreversible removal


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class VerificationMode(StrEnum):
    NONE = "none"            # schema validation only; recorded as not_required
    INVARIANT = "invariant"  # semantic assertions on the output
    READBACK = "readback"    # independent re-read of the affected entity


#: Side effects that a human must authorise (invariant P1).
GATED_SIDE_EFFECTS: Final[frozenset[SideEffect]] = frozenset(
    {SideEffect.CUSTOMER_WRITE, SideEffect.OUTBOUND, SideEffect.DESTRUCTIVE}
)


class FailureMode(BaseModel):
    model_config = ConfigDict(frozen=True)

    error_class: ErrorClass
    description: str


class ToolContract(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: ToolName
    version: str = "1.0.0"
    purpose: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    side_effect: SideEffect
    requires_approval: bool
    risk: RiskLevel
    verification: VerificationMode
    idempotent: bool
    nondeterministic: bool = False
    untrusted_output: bool = False
    timeout_ms: int = Field(default=10_000, ge=100, le=120_000)
    retryable_errors: frozenset[ErrorClass] = frozenset()
    failure_modes: tuple[FailureMode, ...] = ()
    port: str | None = None

    @property
    def is_mutating(self) -> bool:
        return self.side_effect is not SideEffect.READ_ONLY

    def json_schemas(self) -> dict[str, object]:
        """What `GET /tools` publishes for this tool."""
        return {
            "input": self.input_model.model_json_schema(),
            "output": self.output_model.model_json_schema(),
        }


POLICY_INVARIANTS: Final[dict[str, str]] = {
    "P1": "gated side effect implies requires_approval",
    "P2": "any mutating tool implies verification == READBACK",
    "P3": "read-only implies not requires_approval (approval fatigue is a safety failure)",
    "P4": "requires_approval implies an idempotency key is part of the input contract",
    "P5": "not idempotent implies VERIFICATION_FAILED is not retryable",
    "P6": "DESTRUCTIVE implies risk == HIGH",
    "P7": "every declared failure mode uses a class in the error taxonomy",
}

_TRANSIENT = FailureMode(
    error_class=ErrorClass.TRANSIENT, description="store or adapter temporarily unavailable"
)
_NOT_FOUND = FailureMode(
    error_class=ErrorClass.NOT_FOUND, description="the referenced record does not exist"
)
_INPUT = FailureMode(
    error_class=ErrorClass.INPUT_VALIDATION, description="arguments failed the input schema"
)

_CONTRACTS: tuple[ToolContract, ...] = (
    ToolContract(
        name=ToolName.SEARCH_LEADS,
        purpose="Find candidate leads matching business filters.",
        input_model=s.SearchLeadsInput,
        output_model=s.SearchLeadsOutput,
        side_effect=SideEffect.READ_ONLY,
        requires_approval=False,
        risk=RiskLevel.LOW,
        verification=VerificationMode.INVARIANT,
        idempotent=True,
        port="LeadPort",
        retryable_errors=frozenset({ErrorClass.TRANSIENT, ErrorClass.RATE_LIMITED}),
        failure_modes=(_INPUT, _TRANSIENT),
    ),
    ToolContract(
        name=ToolName.GET_LEAD,
        purpose="Fetch one lead by id.",
        input_model=s.GetLeadInput,
        output_model=s.GetLeadOutput,
        side_effect=SideEffect.READ_ONLY,
        requires_approval=False,
        risk=RiskLevel.LOW,
        verification=VerificationMode.NONE,
        idempotent=True,
        port="LeadPort",
        retryable_errors=frozenset({ErrorClass.TRANSIENT}),
        failure_modes=(_NOT_FOUND, _TRANSIENT),
    ),
    ToolContract(
        name=ToolName.RESEARCH_COMPANY,
        purpose="Enrich a company profile with firmographics and buying signals.",
        input_model=s.ResearchCompanyInput,
        output_model=s.ResearchCompanyOutput,
        side_effect=SideEffect.READ_ONLY,
        requires_approval=False,
        risk=RiskLevel.LOW,
        verification=VerificationMode.INVARIANT,
        idempotent=True,
        # Third-party text: the prompt-injection surface (§16.3).
        untrusted_output=True,
        timeout_ms=15_000,
        port="CompanyPort",
        retryable_errors=frozenset({ErrorClass.TRANSIENT, ErrorClass.RATE_LIMITED}),
        failure_modes=(
            _NOT_FOUND,
            _TRANSIENT,
            FailureMode(
                error_class=ErrorClass.OUTPUT_VALIDATION,
                description="profile missing required fields or confidence out of range",
            ),
        ),
    ),
    ToolContract(
        name=ToolName.SCORE_LEAD,
        purpose="Score and band a lead deterministically from its company profile.",
        input_model=s.ScoreLeadInput,
        output_model=s.ScoreLeadOutput,
        side_effect=SideEffect.READ_ONLY,
        requires_approval=False,
        risk=RiskLevel.LOW,
        verification=VerificationMode.INVARIANT,
        idempotent=True,
        # A rule engine, not an LLM: lead-ranking evals must test ranking,
        # not sampling luck (ADR-009).
        nondeterministic=False,
        port=None,
        retryable_errors=frozenset(),
        failure_modes=(_INPUT,),
    ),
    ToolContract(
        name=ToolName.DRAFT_OUTREACH,
        purpose="Generate personalized outreach copy. Persists nothing.",
        input_model=s.DraftOutreachInput,
        output_model=s.DraftOutreachOutput,
        side_effect=SideEffect.READ_ONLY,
        requires_approval=False,
        risk=RiskLevel.MEDIUM,
        verification=VerificationMode.INVARIANT,
        idempotent=True,
        # The only nondeterministic tool, which is why OUTPUT_VALIDATION is
        # retryable here and nowhere else (§10.1).
        nondeterministic=True,
        timeout_ms=30_000,
        port="ContentPort",
        retryable_errors=frozenset(
            {ErrorClass.TRANSIENT, ErrorClass.RATE_LIMITED, ErrorClass.OUTPUT_VALIDATION}
        ),
        failure_modes=(
            _TRANSIENT,
            FailureMode(
                error_class=ErrorClass.OUTPUT_VALIDATION,
                description="unresolved placeholder, empty subject/body, or over max_words",
            ),
        ),
    ),
    ToolContract(
        name=ToolName.SAVE_DRAFT,
        purpose="Persist generated copy as a durable, addressable draft.",
        input_model=s.SaveDraftInput,
        output_model=s.SaveDraftOutput,
        side_effect=SideEffect.INTERNAL_WRITE,
        # Deliberately not gated (ADR-008): internal, reversible, non-outbound.
        # The operator's meaningful decision is "send this".
        requires_approval=False,
        risk=RiskLevel.MEDIUM,
        verification=VerificationMode.READBACK,
        idempotent=True,
        port="DraftPort",
        retryable_errors=frozenset({ErrorClass.TRANSIENT, ErrorClass.VERIFICATION_FAILED}),
        failure_modes=(
            _NOT_FOUND,
            _TRANSIENT,
            FailureMode(
                error_class=ErrorClass.POLICY_VIOLATION,
                description="content_hash does not match the submitted subject/body",
            ),
        ),
    ),
    ToolContract(
        name=ToolName.SEND_EMAIL_MOCK,
        purpose="Record an outbound email in the mock outbox. Never sends real mail.",
        input_model=s.SendEmailMockInput,
        output_model=s.SendEmailMockOutput,
        side_effect=SideEffect.OUTBOUND,
        requires_approval=True,
        risk=RiskLevel.HIGH,
        verification=VerificationMode.READBACK,
        # Idempotent via the step's idempotency key plus a UNIQUE constraint on
        # the outbox, not intrinsically (§8.3 note 3).
        idempotent=True,
        port="MailPort",
        retryable_errors=frozenset({ErrorClass.TRANSIENT, ErrorClass.VERIFICATION_FAILED}),
        failure_modes=(
            _NOT_FOUND,
            _TRANSIENT,
            FailureMode(
                error_class=ErrorClass.POLICY_VIOLATION,
                description="recipient does not match the owning lead, or the approval token is absent/stale",
            ),
        ),
    ),
    ToolContract(
        name=ToolName.GET_CUSTOMER,
        purpose="Fetch a customer record, including the version needed for a safe update.",
        input_model=s.GetCustomerInput,
        output_model=s.GetCustomerOutput,
        side_effect=SideEffect.READ_ONLY,
        requires_approval=False,
        risk=RiskLevel.LOW,
        verification=VerificationMode.NONE,
        idempotent=True,
        port="CustomerPort",
        retryable_errors=frozenset({ErrorClass.TRANSIENT}),
        failure_modes=(_NOT_FOUND, _TRANSIENT),
    ),
    ToolContract(
        name=ToolName.UPDATE_CUSTOMER,
        purpose="Apply an allowlisted patch to a customer record.",
        input_model=s.UpdateCustomerInput,
        output_model=s.UpdateCustomerOutput,
        side_effect=SideEffect.CUSTOMER_WRITE,
        requires_approval=True,
        risk=RiskLevel.HIGH,
        verification=VerificationMode.READBACK,
        idempotent=True,
        port="CustomerPort",
        retryable_errors=frozenset({ErrorClass.TRANSIENT, ErrorClass.VERIFICATION_FAILED}),
        failure_modes=(
            _NOT_FOUND,
            _TRANSIENT,
            FailureMode(
                error_class=ErrorClass.STALE_WRITE,
                description="expected_version conflict; forces a re-read and a fresh approval",
            ),
            FailureMode(
                error_class=ErrorClass.POLICY_VIOLATION,
                description="patch touches a field outside the write allowlist",
            ),
        ),
    ),
)

REGISTRY: Final[dict[ToolName, ToolContract]] = {c.name: c for c in _CONTRACTS}


def contract(name: ToolName | str) -> ToolContract:
    """Look up a contract, rejecting anything the registry does not declare.

    This is the function that makes an LLM unable to invent a capability: an
    unknown tool name is a `KeyError`, not a dispatch attempt (§4.2).
    """
    key = ToolName(name)
    return REGISTRY[key]


def requires_approval(name: ToolName | str) -> bool:
    return contract(name).requires_approval


def catalog() -> list[dict[str, object]]:
    """The `GET /tools` payload (§13.7)."""
    return [
        {
            "name": c.name.value,
            "version": c.version,
            "purpose": c.purpose,
            "side_effect": c.side_effect.value,
            "requires_approval": c.requires_approval,
            "risk": c.risk.value,
            "verification": c.verification.value,
            "idempotent": c.idempotent,
            "nondeterministic": c.nondeterministic,
            "untrusted_output": c.untrusted_output,
            "timeout_ms": c.timeout_ms,
            "failure_modes": [
                {"error_class": f.error_class.value, "description": f.description}
                for f in c.failure_modes
            ],
            "schemas": c.json_schemas(),
        }
        for c in _CONTRACTS
    ]
