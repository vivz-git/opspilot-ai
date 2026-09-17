"""Loading and cross-validating `backend/evals/` (§15.3, EVAL-001).

    evals/
      cases/<id>.yaml      one `EvalCase` per file; the stem *is* the id
      fixtures/*.yaml      companies / leads / customers of the default set
      suites.yaml          named groupings of case ids

Pure I/O on the local tree: `yaml.safe_load` only (no tags, no object
construction), Pydantic validation, and the cross-file checks a single model
cannot make — duplicate ids, a suite naming a case that does not exist, a
case naming a suite the manifest does not declare, a case pointing at a
fixture set that was not loaded. Every failure is an
`EvaluationCaseValidationError` that names the file and the field.

No database, no network, no execution: the runner (EVAL-002) consumes the
resulting `EvaluationRegistry`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ValidationError

from app.errors import EvaluationCaseValidationError
from app.evaluation.registry import EvaluationRegistry
from app.evaluation.schemas import (
    DEFAULT_FIXTURE_SET,
    REQUIRED_SUITES,
    CompanyFixture,
    CustomerFixture,
    EvalCase,
    FixtureDataset,
    LeadFixture,
    SuitesManifest,
)

__all__ = [
    "CASES_DIRNAME",
    "DEFAULT_EVALS_ROOT",
    "FIXTURES_DIRNAME",
    "SUITES_FILENAME",
    "load_case",
    "load_cases",
    "load_fixtures",
    "load_registry",
    "load_suites",
    "read_yaml_mapping",
]

#: `backend/evals/` — the canonical tree, resolved relative to this package
#: so it works from any working directory.
DEFAULT_EVALS_ROOT: Final[Path] = Path(__file__).resolve().parents[2] / "evals"
CASES_DIRNAME: Final = "cases"
FIXTURES_DIRNAME: Final = "fixtures"
SUITES_FILENAME: Final = "suites.yaml"

#: Fixture file → (top-level key, row model). The key doubles as the
#: `FixtureDataset` field name.
_FIXTURE_FILES: Final[tuple[tuple[str, str, type[BaseModel]], ...]] = (
    ("companies.yaml", "companies", CompanyFixture),
    ("leads.yaml", "leads", LeadFixture),
    ("customers.yaml", "customers", CustomerFixture),
)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def _source(path: Path) -> str:
    """How a file is named in errors: relative to the evals root when it is
    under one, else as given. Never raises."""
    try:
        return path.resolve().relative_to(DEFAULT_EVALS_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Parse one YAML file into a mapping with the safe loader only."""
    source = _source(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvaluationCaseValidationError(
            "cannot read file", source=source, reason=str(exc)
        ) from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise EvaluationCaseValidationError(
            "malformed YAML", source=source, reason=str(exc).splitlines()[0]
        ) from exc
    if not isinstance(document, dict):
        raise EvaluationCaseValidationError(
            "document must be a mapping",
            source=source,
            reason=f"got {type(document).__name__}",
        )
    if any(not isinstance(key, str) for key in document):
        raise EvaluationCaseValidationError(
            "top-level keys must be strings", source=source, location="."
        )
    return document


def _format_location(loc: tuple[int | str, ...]) -> str:
    parts: list[str] = []
    for item in loc:
        parts.append(f"[{item}]" if isinstance(item, int) else str(item))
    return ".".join(parts).replace(".[", "[") or "."


def _validate[M: BaseModel](model: type[M], data: object, *, source: str) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        reason = first["msg"]
        offending = first.get("input")
        if isinstance(offending, str | int | float | bool):
            reason = f"{reason} (got {offending!r})"
        raise EvaluationCaseValidationError(
            f"invalid {model.__name__}",
            source=source,
            location=_format_location(tuple(first["loc"])),
            reason=reason,
            detail={"error_count": exc.error_count()},
        ) from exc


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------
def load_case(path: Path) -> EvalCase:
    """One case file. The file stem must equal `case.id`, so the id in the
    dashboard, the id in `evaluation_results` and the file a reviewer opens
    can never drift apart."""
    source = _source(path)
    case = _validate(EvalCase, read_yaml_mapping(path), source=source)
    if case.id != path.stem:
        raise EvaluationCaseValidationError(
            "case id does not match its filename",
            source=source,
            location="id",
            reason=f"id is {case.id!r} but the file stem is {path.stem!r}",
        )
    return case


def load_cases(directory: Path) -> tuple[EvalCase, ...]:
    """Every `*.yaml` in `directory`, ordered by id, with duplicate ids
    rejected (the stem check makes those impossible on a case-sensitive
    filesystem; on a case-insensitive one it is still worth asserting)."""
    if not directory.is_dir():
        raise EvaluationCaseValidationError(
            "cases directory does not exist", source=_source(directory)
        )
    cases: dict[str, EvalCase] = {}
    for path in sorted(directory.glob("*.yaml"), key=lambda p: p.name):
        case = load_case(path)
        if case.id in cases:
            raise EvaluationCaseValidationError(
                "duplicate case id",
                source=_source(path),
                location="id",
                reason=f"{case.id!r} is already defined",
            )
        cases[case.id] = case
    return tuple(cases[case_id] for case_id in sorted(cases))


# ---------------------------------------------------------------------------
# Suites
# ---------------------------------------------------------------------------
def load_suites(path: Path) -> SuitesManifest:
    return _validate(SuitesManifest, read_yaml_mapping(path), source=_source(path))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def load_fixtures(directory: Path, *, name: str = DEFAULT_FIXTURE_SET) -> FixtureDataset:
    """Assemble one dataset from the three fixture files."""
    if not directory.is_dir():
        raise EvaluationCaseValidationError(
            "fixtures directory does not exist", source=_source(directory)
        )
    rows: dict[str, list[BaseModel]] = {}
    for filename, key, model in _FIXTURE_FILES:
        path = directory / filename
        source = _source(path)
        document = read_yaml_mapping(path)
        if set(document) != {key}:
            raise EvaluationCaseValidationError(
                f"fixture file must contain exactly one top-level key {key!r}",
                source=source,
                location=".",
                reason=f"found {sorted(document)}",
            )
        items = document[key]
        if not isinstance(items, list):
            raise EvaluationCaseValidationError(
                f"{key!r} must be a list", source=source, location=key
            )
        rows[key] = [
            _validate(model, item, source=f"{source}#{key}[{index}]")
            for index, item in enumerate(items)
        ]
    return _validate(
        FixtureDataset,
        {"name": name, **rows},
        source=_source(directory),
    )


# ---------------------------------------------------------------------------
# The whole tree
# ---------------------------------------------------------------------------
def _cross_validate(
    cases: tuple[EvalCase, ...],
    manifest: SuitesManifest,
    fixtures: Mapping[str, FixtureDataset],
    *,
    suites_source: str,
) -> None:
    case_ids = {c.id for c in cases}
    declared = set(manifest.suites)

    missing_suites = REQUIRED_SUITES - declared
    if missing_suites:
        raise EvaluationCaseValidationError(
            "manifest lacks required suites",
            source=suites_source,
            location="suites",
            reason=f"missing {sorted(missing_suites)}",
        )
    for name, suite in manifest.suites.items():
        unknown = [c for c in suite.cases if c not in case_ids]
        if unknown:
            raise EvaluationCaseValidationError(
                "suite references a case that does not exist",
                source=suites_source,
                location=f"suites.{name}.cases",
                reason=f"unknown {unknown}",
            )
    not_in_all = sorted(case_ids - set(manifest.suites["all"].cases))
    if not_in_all:
        raise EvaluationCaseValidationError(
            "suite 'all' must list every case",
            source=suites_source,
            location="suites.all.cases",
            reason=f"missing {not_in_all}",
        )
    for case in cases:
        source = f"cases/{case.id}.yaml"
        unknown_suites = [s for s in case.suite if s not in declared]
        if unknown_suites:
            raise EvaluationCaseValidationError(
                "case references a suite the manifest does not declare",
                source=source,
                location="suite",
                reason=f"unknown {unknown_suites}",
            )
        # Membership is declared in both places; they must agree, or one of
        # them is lying about what the suite runs.
        for name in case.suite:
            if case.id not in manifest.suites[name].cases:
                raise EvaluationCaseValidationError(
                    "case claims a suite that does not list it",
                    source=source,
                    location="suite",
                    reason=f"suites.yaml:{name} does not include {case.id!r}",
                )
        for name, suite in manifest.suites.items():
            if case.id in suite.cases and name not in case.suite:
                raise EvaluationCaseValidationError(
                    "suite lists a case that does not claim it",
                    source=suites_source,
                    location=f"suites.{name}.cases",
                    reason=f"{case.id!r} does not name {name!r} in its 'suite'",
                )
        if case.given.fixtures not in fixtures:
            raise EvaluationCaseValidationError(
                "case references an unknown fixture set",
                source=source,
                location="given.fixtures",
                reason=f"{case.given.fixtures!r} not in {sorted(fixtures)}",
            )


def load_registry(root: Path = DEFAULT_EVALS_ROOT) -> EvaluationRegistry:
    """Load and cross-validate the whole tree. Deterministic: the same files
    yield an identical registry, whatever the directory listing order."""
    cases = load_cases(root / CASES_DIRNAME)
    suites_path = root / SUITES_FILENAME
    manifest = load_suites(suites_path)
    dataset = load_fixtures(root / FIXTURES_DIRNAME)
    fixtures = {dataset.name: dataset}
    _cross_validate(cases, manifest, fixtures, suites_source=_source(suites_path))
    return EvaluationRegistry.build(cases, manifest.suites, (dataset,))
