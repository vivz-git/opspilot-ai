"""In-memory, immutable index over loaded evaluation definitions (EVAL-001).

Built once by `app.evaluation.loader.load_registry`; never mutated, never
global. Lookups are deterministic: cases are ordered by id, suites by name,
and a suite's cases come back in the order the manifest lists them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.evaluation.schemas import REQUIRED_CASE_IDS, EvalCase, FixtureDataset, SuiteSpec

__all__ = ["EvaluationRegistry"]


@dataclass(frozen=True)
class EvaluationRegistry:
    cases: Mapping[str, EvalCase]
    suites: Mapping[str, SuiteSpec]
    fixtures: Mapping[str, FixtureDataset]

    @classmethod
    def build(
        cls,
        cases: tuple[EvalCase, ...],
        suites: Mapping[str, SuiteSpec],
        fixtures: tuple[FixtureDataset, ...],
    ) -> EvaluationRegistry:
        return cls(
            cases=MappingProxyType({c.id: c for c in sorted(cases, key=lambda c: c.id)}),
            suites=MappingProxyType(dict(sorted(suites.items()))),
            fixtures=MappingProxyType({f.name: f for f in sorted(fixtures, key=lambda f: f.name)}),
        )

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(self.cases)

    @property
    def suite_names(self) -> tuple[str, ...]:
        return tuple(self.suites)

    def case(self, case_id: str) -> EvalCase:
        try:
            return self.cases[case_id]
        except KeyError:
            raise KeyError(f"unknown evaluation case {case_id!r}") from None

    def suite_cases(self, suite: str) -> tuple[EvalCase, ...]:
        """The suite's cases in manifest order."""
        try:
            spec = self.suites[suite]
        except KeyError:
            raise KeyError(f"unknown evaluation suite {suite!r}") from None
        return tuple(self.cases[case_id] for case_id in spec.cases)

    def fixture_set(self, name: str) -> FixtureDataset:
        try:
            return self.fixtures[name]
        except KeyError:
            raise KeyError(f"unknown fixture set {name!r}") from None

    def missing_required_cases(self) -> tuple[str, ...]:
        """Which of the seven required cases (§15.3) this registry lacks."""
        return tuple(case_id for case_id in REQUIRED_CASE_IDS if case_id not in self.cases)
