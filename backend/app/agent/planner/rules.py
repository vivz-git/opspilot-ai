"""`RulePlanner`: the deterministic planner (§4.2, ADR-002).

Pattern-matches the canonical intent of a `NormalizedTask` and emits the
canonical plan skeleton for it, sized to the run's budgets. Same task, same
budgets, same prior → the same plan, byte for byte: no clock, no ids drawn
from a generator, no randomness, no I/O. That determinism is what makes the
evaluation suite meaningful and CI keyless (§15).

The skeletons are the workflows the registry can express. Two limits of the
`$ref` language (§4.4, ADR-003) shape them:

- A fan-out binds only its own alias, so a per-item *chain* (research lead
  *i*, then score lead *i* with that profile) is written as one fan-out for
  the research plus one explicit, indexed step per lead for the score, each
  referencing the prospective child `s2[i]`. The number of leads is known at
  plan time (`limit`), so the plan stays bounded.
- The language has no expressions, so "the best one" cannot be selected
  from scores inside a plan. The outreach steps target the first returned
  lead and say so in their rationale; choosing by score needs a selection
  tool or a plan continuation and is recorded as an open question (Q7).

On revision the rule planner has no new information — its skeleton is a
function of the task it already planned — so it re-emits the previous plan's
structure: settled work is carried over by the node, the faulted step runs
again, and optional steps that can never become runnable (they depend on a
skipped step) are dropped. A bounded number of identical revisions followed by
a terminal failure is the honest outcome for a fault a rule planner cannot
reason about.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from app.agent.normalizer import CanonicalIntent
from app.agent.planner.protocol import PlannerIdentity, PlanRevisionContext
from app.agent.planner.validation import assert_valid_plan
from app.agent.state import Budgets, FanOut, NormalizedTask, Plan, PlannerKind, PlanStep, StepStatus
from app.errors import PlannerError
from app.tools.contracts import REGISTRY, ToolContract, ToolName
from app.tools.schemas import CustomerPatch

__all__ = [
    "DEFAULT_OUTREACH_LEADS",
    "DEFAULT_SEARCH_LIMIT",
    "RulePlanner",
    "plan_id_for",
]

#: How many leads an outreach pipeline works when the request names none.
DEFAULT_OUTREACH_LEADS: Final = 3
#: Page size for a plain search when the request names none.
DEFAULT_SEARCH_LIMIT: Final = 10
#: `search_leads.limit` is capped by its contract.
MAX_SEARCH_LIMIT: Final = 50
#: The customer fields a plan may write (`CustomerPatch`, §16.3 rule 4).
WRITABLE_CUSTOMER_FIELDS: Final[frozenset[str]] = frozenset(CustomerPatch.model_fields)

#: Statuses a revision resets to `pending`. `skipped` and `rejected` are
#: kept: the former is not re-attempted, the latter is a human's decision.
_RESET_STATUSES: Final[frozenset[StepStatus]] = frozenset(
    {
        StepStatus.READY,
        StepStatus.AWAITING_APPROVAL,
        StepStatus.RUNNING,
        StepStatus.SUCCEEDED,
        StepStatus.FAILED,
    }
)


def plan_id_for(revision: int) -> str:
    """`p_1` for the first plan, `p_2` for its first revision (§13.3)."""
    return f"p_{revision + 1}"


def _ref(path: str) -> dict[str, str]:
    return {"$ref": path}


@dataclass
class _Draft:
    """A skeleton under construction: sequential ids, steps in order."""

    steps: list[PlanStep] = field(default_factory=list)

    def add(
        self,
        tool: ToolName,
        args: dict[str, Any],
        *,
        depends_on: list[str] | None = None,
        rationale: str,
        optional: bool = False,
        fanout: FanOut | None = None,
    ) -> str:
        step_id = f"s{len(self.steps) + 1}"
        self.steps.append(
            PlanStep(
                step_id=step_id,
                tool=tool,
                args=args,
                depends_on=depends_on or [],
                rationale=rationale,
                optional=optional,
                fanout=fanout,
            )
        )
        return step_id


class RulePlanner:
    """Deterministic planning for the eight supported intents."""

    def __init__(self, contracts: Mapping[ToolName, ToolContract] = REGISTRY) -> None:
        self._contracts = contracts

    @property
    def identity(self) -> PlannerIdentity:
        return PlannerIdentity(kind=PlannerKind.RULES)

    async def plan(
        self,
        task: NormalizedTask,
        prior: PlanRevisionContext | None = None,
        *,
        budgets: Budgets | None = None,
    ) -> Plan:
        return self.plan_sync(task, prior, budgets=budgets)

    def plan_sync(
        self,
        task: NormalizedTask,
        prior: PlanRevisionContext | None = None,
        *,
        budgets: Budgets | None = None,
    ) -> Plan:
        budgets = budgets or Budgets()
        if not task.in_scope:
            raise PlannerError("cannot plan an out-of-scope task", detail={"intent": task.intent})
        if prior is not None:
            plan = self._revise(prior)
        else:
            plan = Plan(
                plan_id=plan_id_for(0),
                revision=0,
                created_by=PlannerKind.RULES,
                steps=self._skeleton(task, budgets),
            )
        return assert_valid_plan(plan, task=task, contracts=self._contracts, budgets=budgets)

    # ------------------------------------------------------------------
    # Revision
    # ------------------------------------------------------------------
    def _revise(self, prior: PlanRevisionContext) -> Plan:
        previous = prior.previous
        unrunnable: set[str] = {
            s.step_id
            for s in previous.steps
            if s.status in (StepStatus.SKIPPED, StepStatus.REJECTED)
        }
        # An optional step behind a skipped/rejected dependency (transitively)
        # can never satisfy rule 4; dropping it is the only revision that helps.
        changed = True
        dropped: set[str] = set()
        while changed:
            changed = False
            for s in previous.steps:
                if s.step_id in dropped or s.step_id in unrunnable or not s.optional:
                    continue
                if any(d in unrunnable or d in dropped for d in s.depends_on):
                    dropped.add(s.step_id)
                    changed = True
        steps = [
            s.model_copy(update={"status": StepStatus.PENDING})
            if s.status in _RESET_STATUSES
            else s
            for s in previous.steps
            if s.step_id not in dropped
        ]
        revision = previous.revision + 1
        return Plan(
            plan_id=plan_id_for(revision),
            revision=revision,
            created_by=PlannerKind.RULES,
            steps=steps,
        )

    # ------------------------------------------------------------------
    # Skeletons
    # ------------------------------------------------------------------
    def _skeleton(self, task: NormalizedTask, budgets: Budgets) -> list[PlanStep]:
        intent = task.intent
        entities = task.entities
        draft = _Draft()
        if intent == CanonicalIntent.PROSPECT_AND_OUTREACH:
            self._outreach_pipeline(draft, task, budgets, score=True, send=True)
        elif intent == CanonicalIntent.LEAD_SEARCH:
            if task.requires_mutation:
                self._outreach_pipeline(draft, task, budgets, score=False, send=True)
            else:
                draft.add(
                    ToolName.SEARCH_LEADS,
                    self._search_args(task, self._lead_count(task, DEFAULT_SEARCH_LIMIT)),
                    rationale="Locate candidate leads matching the requested filters",
                )
        elif intent == CanonicalIntent.LEAD_LOOKUP:
            lead_id = self._require(entities, "lead_id", intent)
            draft.add(ToolName.GET_LEAD, {"lead_id": lead_id}, rationale=f"Fetch lead {lead_id}")
        elif intent == CanonicalIntent.COMPANY_RESEARCH:
            self._company_research(draft, task)
        elif intent == CanonicalIntent.LEAD_SCORING:
            self._lead_scoring(draft, task, budgets)
        elif intent == CanonicalIntent.DRAFT_OUTREACH:
            self._draft_outreach(draft, task, budgets)
        elif intent == CanonicalIntent.CUSTOMER_LOOKUP:
            draft.add(
                ToolName.GET_CUSTOMER,
                self._customer_locator(entities, intent),
                rationale="Fetch the customer record",
            )
        elif intent == CanonicalIntent.CUSTOMER_UPDATE:
            self._customer_update(draft, task)
        else:
            raise PlannerError(f"no rule skeleton for intent {intent!r}", detail={"intent": intent})
        return draft.steps

    # -- shared fragments ----------------------------------------------------
    @staticmethod
    def _require(entities: Mapping[str, Any], key: str, intent: str) -> str:
        value = entities.get(key)
        if not isinstance(value, str) or not value:
            raise PlannerError(
                f"intent {intent!r} needs a {key} and the request named none",
                detail={"intent": intent, "missing": key},
            )
        return value

    @staticmethod
    def _search_args(task: NormalizedTask, limit: int) -> dict[str, Any]:
        args: dict[str, Any] = {}
        industry = task.entities.get("industry")
        location = task.entities.get("location")
        if isinstance(industry, str) and industry:
            args["industry"] = industry
        if isinstance(location, str) and location:
            args["location"] = location
        if not args:
            raise PlannerError(
                "a lead search needs at least one filter (industry or location) and the "
                "request named none",
                detail={"intent": task.intent},
            )
        args["limit"] = limit
        return args

    @staticmethod
    def _lead_count(task: NormalizedTask, default: int) -> int:
        requested = task.constraints.get("max_leads", task.entities.get("limit"))
        count = requested if isinstance(requested, int) and requested > 0 else default
        return min(count, MAX_SEARCH_LIMIT)

    @staticmethod
    def _fit(count: int, budgets: Budgets, *, per_lead_steps: int, fixed_steps: int) -> int:
        """Shrink the lead count so the *executed* steps (fan-out children
        included, §4.5) fit MAX_STEPS. At least one lead is always planned;
        an impossible budget then fails validation, loudly."""
        room = (budgets.max_steps - fixed_steps) // per_lead_steps
        return max(1, min(count, room))

    def _outreach_pipeline(
        self,
        draft: _Draft,
        task: NormalizedTask,
        budgets: Budgets,
        *,
        score: bool,
        send: bool,
    ) -> None:
        """search → research (fan-out) → [score per lead] → draft → [save → send]."""
        fixed = 1 + 1 + (2 if send else 0)  # search, draft, save, send
        per_lead = 2 if score else 1  # research child (+ score)
        count = self._fit(
            self._lead_count(task, DEFAULT_OUTREACH_LEADS),
            budgets,
            per_lead_steps=per_lead,
            fixed_steps=fixed,
        )
        search = draft.add(
            ToolName.SEARCH_LEADS,
            self._search_args(task, count),
            rationale=f"Locate the top {count} candidate leads matching the requested filters",
        )
        research = draft.add(
            ToolName.RESEARCH_COMPANY,
            {"company_id": _ref("lead.company_id")},
            depends_on=[search],
            rationale="Research each lead's company for firmographics and buying signals",
            fanout=FanOut(over=f"{search}.output.leads", as_="lead", max_items=count),
        )
        first_score: str | None = None
        if score:
            for i in range(count):
                score_id = draft.add(
                    ToolName.SCORE_LEAD,
                    {
                        "lead_id": _ref(f"{search}.output.leads.{i}.lead_id"),
                        "company": _ref(f"{research}[{i}].output.profile"),
                    },
                    depends_on=[search, research],
                    rationale=f"Score lead #{i + 1} deterministically from its company profile",
                    optional=i > 0,
                )
                if i == 0:
                    first_score = score_id
        draft_args: dict[str, Any] = {
            "lead_id": _ref(f"{search}.output.leads.0.lead_id"),
            "company": _ref(f"{research}[0].output.profile"),
        }
        if first_score is not None:
            draft_args["score"] = _ref(f"{first_score}.output")
        drafted = draft.add(
            ToolName.DRAFT_OUTREACH,
            draft_args,
            depends_on=[search, research] + ([first_score] if first_score else []),
            rationale=(
                "Draft outreach for the first returned lead (the plan language cannot "
                "select by score; see open question Q7)"
            ),
        )
        if not send:
            return
        saved = draft.add(
            ToolName.SAVE_DRAFT,
            {
                "lead_id": _ref(f"{search}.output.leads.0.lead_id"),
                "subject": _ref(f"{drafted}.output.subject"),
                "body": _ref(f"{drafted}.output.body"),
                "content_hash": _ref(f"{drafted}.output.content_hash"),
            },
            depends_on=[search, drafted],
            rationale="Persist the draft so the sent content is exactly the verified content",
        )
        draft.add(
            ToolName.SEND_EMAIL_MOCK,
            {
                "draft_id": _ref(f"{saved}.output.draft_id"),
                "to_email": _ref(f"{search}.output.leads.0.email"),
            },
            depends_on=[search, saved],
            rationale="Send the saved draft to the lead's stored address (requires approval)",
        )

    def _lead_pipeline_from_id(self, draft: _Draft, lead_id: str) -> tuple[str, str]:
        """get_lead → research_company for one known lead."""
        lead = draft.add(ToolName.GET_LEAD, {"lead_id": lead_id}, rationale=f"Fetch lead {lead_id}")
        research = draft.add(
            ToolName.RESEARCH_COMPANY,
            {"company_id": _ref(f"{lead}.output.lead.company_id")},
            depends_on=[lead],
            rationale="Research the lead's company for firmographics and buying signals",
        )
        return lead, research

    @staticmethod
    def _score_from(draft: _Draft, lead: str, research: str) -> str:
        return draft.add(
            ToolName.SCORE_LEAD,
            {
                "lead_id": _ref(f"{lead}.output.lead.lead_id"),
                "company": _ref(f"{research}.output.profile"),
            },
            depends_on=[lead, research],
            rationale="Score the lead deterministically from its company profile",
        )

    def _company_research(self, draft: _Draft, task: NormalizedTask) -> None:
        company_id = task.entities.get("company_id")
        lead_id = task.entities.get("lead_id")
        if isinstance(company_id, str) and company_id:
            draft.add(
                ToolName.RESEARCH_COMPANY,
                {"company_id": company_id},
                rationale=f"Research company {company_id}",
            )
        elif isinstance(lead_id, str) and lead_id:
            self._lead_pipeline_from_id(draft, lead_id)
        else:
            raise PlannerError(
                "company research needs a company id or a lead id and the request named none",
                detail={"intent": task.intent},
            )

    def _lead_scoring(self, draft: _Draft, task: NormalizedTask, budgets: Budgets) -> None:
        lead_id = task.entities.get("lead_id")
        if isinstance(lead_id, str) and lead_id:
            lead, research = self._lead_pipeline_from_id(draft, lead_id)
            self._score_from(draft, lead, research)
            return
        count = self._fit(
            self._lead_count(task, DEFAULT_OUTREACH_LEADS), budgets, per_lead_steps=2, fixed_steps=1
        )
        search = draft.add(
            ToolName.SEARCH_LEADS,
            self._search_args(task, count),
            rationale=f"Locate the top {count} candidate leads matching the requested filters",
        )
        research = draft.add(
            ToolName.RESEARCH_COMPANY,
            {"company_id": _ref("lead.company_id")},
            depends_on=[search],
            rationale="Research each lead's company for firmographics and buying signals",
            fanout=FanOut(over=f"{search}.output.leads", as_="lead", max_items=count),
        )
        for i in range(count):
            draft.add(
                ToolName.SCORE_LEAD,
                {
                    "lead_id": _ref(f"{search}.output.leads.{i}.lead_id"),
                    "company": _ref(f"{research}[{i}].output.profile"),
                },
                depends_on=[search, research],
                rationale=f"Score lead #{i + 1} deterministically from its company profile",
                optional=i > 0,
            )

    def _draft_outreach(self, draft: _Draft, task: NormalizedTask, budgets: Budgets) -> None:
        lead_id = task.entities.get("lead_id")
        if not (isinstance(lead_id, str) and lead_id):
            # No lead named: prospect first, then draft (and send if asked).
            self._outreach_pipeline(draft, task, budgets, score=True, send=task.requires_mutation)
            return
        lead, research = self._lead_pipeline_from_id(draft, lead_id)
        scored = self._score_from(draft, lead, research)
        drafted = draft.add(
            ToolName.DRAFT_OUTREACH,
            {
                "lead_id": _ref(f"{lead}.output.lead.lead_id"),
                "company": _ref(f"{research}.output.profile"),
                "score": _ref(f"{scored}.output"),
            },
            depends_on=[lead, research, scored],
            rationale=f"Draft personalised outreach for lead {lead_id}",
        )
        if not task.requires_mutation:
            return
        saved = draft.add(
            ToolName.SAVE_DRAFT,
            {
                "lead_id": _ref(f"{lead}.output.lead.lead_id"),
                "subject": _ref(f"{drafted}.output.subject"),
                "body": _ref(f"{drafted}.output.body"),
                "content_hash": _ref(f"{drafted}.output.content_hash"),
            },
            depends_on=[lead, drafted],
            rationale="Persist the draft so the sent content is exactly the verified content",
        )
        draft.add(
            ToolName.SEND_EMAIL_MOCK,
            {
                "draft_id": _ref(f"{saved}.output.draft_id"),
                "to_email": _ref(f"{lead}.output.lead.email"),
            },
            depends_on=[lead, saved],
            rationale="Send the saved draft to the lead's stored address (requires approval)",
        )

    @staticmethod
    def _customer_locator(entities: Mapping[str, Any], intent: str) -> dict[str, Any]:
        customer_id = entities.get("customer_id")
        email = entities.get("email")
        if isinstance(customer_id, str) and customer_id:
            return {"customer_id": customer_id}
        if isinstance(email, str) and email:
            return {"email": email}
        raise PlannerError(
            f"intent {intent!r} needs a customer id or email and the request named none",
            detail={"intent": intent},
        )

    def _customer_update(self, draft: _Draft, task: NormalizedTask) -> None:
        updates = task.entities.get("field_updates")
        requested = dict(updates) if isinstance(updates, dict) else {}
        patch = {k: v for k, v in requested.items() if k in WRITABLE_CUSTOMER_FIELDS}
        refused = sorted(set(requested) - set(patch))
        if not patch:
            raise PlannerError(
                "the requested customer update touches no writable field "
                f"(writable: {sorted(WRITABLE_CUSTOMER_FIELDS)}; requested: {sorted(requested)})",
                detail={"intent": task.intent, "refused": refused},
            )
        fetched = draft.add(
            ToolName.GET_CUSTOMER,
            self._customer_locator(task.entities, task.intent),
            rationale="Read the customer record and its version before changing it",
        )
        rationale = f"Apply the requested change to {', '.join(sorted(patch))} (requires approval)"
        if refused:
            rationale += f"; {', '.join(refused)} cannot be written by the agent and was dropped"
        draft.add(
            ToolName.UPDATE_CUSTOMER,
            {
                "customer_id": _ref(f"{fetched}.output.customer.customer_id"),
                "expected_version": _ref(f"{fetched}.output.customer.version"),
                "patch": patch,
                "reason": f"Requested update of {', '.join(sorted(patch))}",
            },
            depends_on=[fetched],
            rationale=rationale,
        )
