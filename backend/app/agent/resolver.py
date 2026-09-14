"""Deterministic reference ($ref) resolution engine (§4.4, §16.3, AGENT-005).

Resolves dotted keys and bracketed indices against the artifact store
(`state.tool_results`, keyed by `step_id`).

Guarantees:
1. Deterministic and side-effect free: no network, no database, no LLM calls.
2. Immutability: source `ToolResult`s and original `PlanStep.args` are never mutated.
3. Strict syntax: dot-separated keys and non-negative numeric indices only.
4. Security: expressions, arithmetic, code, and dunder/attribute traversal are rejected (§16.3).
5. Error taxonomy: any unresolvable reference raises `ReferenceResolutionError`
   (`ErrorClass.REFERENCE_RESOLUTION`), classified as a replannable planning fault.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.agent.state import AgentState, PlanStep, ToolResult
from app.errors import ReferenceResolutionError

__all__ = [
    "OUTPUT_SEGMENT",
    "REF_KEY",
    "REF_PREFIX",
    "parse_ref_path",
    "resolve_ref_path",
    "resolve_step_args",
    "resolve_value",
]

REF_KEY: Final[str] = "$ref"
REF_PREFIX: Final[str] = "$ref:"
OUTPUT_SEGMENT: Final[str] = "output"

#: Characters forbidden in reference paths to prevent expression injection (§16.3).
_FORBIDDEN_CHARS: Final[frozenset[str]] = frozenset("();=<>!+*&|^%#@~`{}?\\")


def _tokenize_segments(rest: str, raw_path: str) -> list[str]:
    """Tokenize the segment path following '.output' into keys and index strings."""
    if not rest:
        return []

    segments: list[str] = []
    i = 0
    n = len(rest)
    current_token: list[str] = []

    last_was_dot = False

    while i < n:
        char = rest[i]
        if char == ".":
            if last_was_dot:
                raise ReferenceResolutionError(
                    f"Consecutive dots in reference path {raw_path!r}",
                    detail={"path": raw_path},
                )
            if current_token:
                seg = "".join(current_token).strip()
                if not seg:
                    raise ReferenceResolutionError(
                        f"Empty segment in reference path {raw_path!r}",
                        detail={"path": raw_path},
                    )
                segments.append(seg)
                current_token = []
            elif not segments:
                raise ReferenceResolutionError(
                    f"Leading dot in reference path {raw_path!r}",
                    detail={"path": raw_path},
                )
            last_was_dot = True
            i += 1
            continue

        last_was_dot = False
        if char == "[":
            if current_token:
                seg = "".join(current_token).strip()
                if not seg:
                    raise ReferenceResolutionError(
                        f"Empty segment before bracket in reference path {raw_path!r}",
                        detail={"path": raw_path},
                    )
                segments.append(seg)
                current_token = []
            close_idx = rest.find("]", i + 1)
            if close_idx == -1:
                raise ReferenceResolutionError(
                    f"Unclosed bracket in reference path {raw_path!r}",
                    detail={"path": raw_path},
                )
            bracket_content = rest[i + 1 : close_idx].strip()
            if not bracket_content:
                raise ReferenceResolutionError(
                    f"Empty bracket '[]' in reference path {raw_path!r}",
                    detail={"path": raw_path},
                )
            # Handle quoted keys inside brackets e.g. ['foo'] or ["foo"]
            if (bracket_content.startswith('"') and bracket_content.endswith('"')) or (
                bracket_content.startswith("'") and bracket_content.endswith("'")
            ):
                inner = bracket_content[1:-1]
                if not inner:
                    raise ReferenceResolutionError(
                        f"Empty quoted key in bracket in reference path {raw_path!r}",
                        detail={"path": raw_path},
                    )
                segments.append(inner)
            elif bracket_content.isdigit():
                segments.append(bracket_content)
            else:
                raise ReferenceResolutionError(
                    f"Invalid index or key {bracket_content!r} in bracket in "
                    f"reference path {raw_path!r}",
                    detail={"path": raw_path, "content": bracket_content},
                )
            i = close_idx + 1
            if i < n and rest[i] not in (".", "["):
                raise ReferenceResolutionError(
                    f"Invalid character {rest[i]!r} after bracket in reference path {raw_path!r}",
                    detail={"path": raw_path},
                )
        else:
            if char in _FORBIDDEN_CHARS:
                raise ReferenceResolutionError(
                    f"Forbidden character {char!r} in reference path {raw_path!r} "
                    "(expressions disallowed)",
                    detail={"path": raw_path, "char": char},
                )
            current_token.append(char)
            i += 1

    if last_was_dot:
        raise ReferenceResolutionError(
            f"Trailing dot in reference path {raw_path!r}",
            detail={"path": raw_path},
        )

    if current_token:
        seg = "".join(current_token).strip()
        if not seg:
            raise ReferenceResolutionError(
                f"Trailing empty segment in reference path {raw_path!r}",
                detail={"path": raw_path},
            )
        segments.append(seg)

    return segments


def parse_ref_path(path: str) -> tuple[str, list[str]]:
    """Parse a reference path into (step_id, segments).

    Canonical path format: `<step_id>.output[.<traversal>]`

    Examples:
        's1.output' -> ('s1', [])
        's1.output.leads.0.id' -> ('s1', ['leads', '0', 'id'])
        's1.output.leads[0].id' -> ('s1', ['leads', '0', 'id'])
        's2[0].output.score' -> ('s2[0]', ['score'])
    """
    raw = path
    if path.startswith(REF_PREFIX):
        path = path[len(REF_PREFIX) :]
    path = path.strip()
    if not path:
        raise ReferenceResolutionError("Reference path is empty", detail={"path": raw})

    # The artifact-store path must cross the .output segment
    dot_output = f".{OUTPUT_SEGMENT}"
    if path.startswith(dot_output) or path.startswith(OUTPUT_SEGMENT):
        raise ReferenceResolutionError(
            f"Reference path {raw!r} missing step_id before '{OUTPUT_SEGMENT}'",
            detail={"path": raw},
        )

    output_idx = path.find(dot_output)
    if output_idx == -1:
        raise ReferenceResolutionError(
            f"Reference path {raw!r} must have the form '<step_id>.output[.<key>...]'",
            detail={"path": raw},
        )

    step_id = path[:output_idx].strip()
    if not step_id:
        raise ReferenceResolutionError(
            f"Reference path {raw!r} missing step_id before '{OUTPUT_SEGMENT}'",
            detail={"path": raw},
        )

    rest = path[output_idx + len(dot_output) :]
    if rest.startswith("."):
        rest = rest[1:]
        if not rest:
            raise ReferenceResolutionError(
                f"Reference path {raw!r} has trailing dot after '{OUTPUT_SEGMENT}'",
                detail={"path": raw},
            )
    elif rest.startswith("["):
        pass
    elif rest != "":
        raise ReferenceResolutionError(
            f"Reference path {raw!r} must have the form '<step_id>.output[.<key>...]'",
            detail={"path": raw},
        )

    segments = _tokenize_segments(rest, raw)

    # Invariant §16.3: disallow dunder / attribute access
    for seg in segments:
        if seg.startswith("__"):
            raise ReferenceResolutionError(
                f"Access to private/dunder attribute {seg!r} forbidden in path {raw!r}",
                detail={"path": raw, "segment": seg},
            )

    return step_id, segments


def _walk_output(
    root: Any,  # noqa: ANN401 - walks arbitrary JSON-like data
    segments: Sequence[str],
    *,
    step_id: str,
    path: str,
) -> Any:  # noqa: ANN401
    """Traverse root dictionary/list using dot keys and index segments."""
    current = root
    for seg in segments:
        if isinstance(current, Mapping):
            if seg not in current:
                raise ReferenceResolutionError(
                    f"Path {path!r}: key {seg!r} not found in step {step_id!r} output",
                    detail={
                        "path": path,
                        "step_id": step_id,
                        "segment": seg,
                        "available_keys": list(current.keys()),
                    },
                )
            current = current[seg]
        elif isinstance(current, (list, tuple)):
            if not seg.isdigit():
                raise ReferenceResolutionError(
                    f"Path {path!r}: cannot index list with non-integer segment {seg!r}",
                    detail={"path": path, "step_id": step_id, "segment": seg},
                )
            idx = int(seg)
            if idx >= len(current):
                raise ReferenceResolutionError(
                    f"Path {path!r}: index {idx} out of range for list of length {len(current)}",
                    detail={
                        "path": path,
                        "step_id": step_id,
                        "segment": seg,
                        "index": idx,
                        "length": len(current),
                    },
                )
            current = current[idx]
        else:
            raise ReferenceResolutionError(
                f"Path {path!r}: cannot traverse into scalar "
                f"{type(current).__name__} with segment {seg!r}",
                detail={
                    "path": path,
                    "step_id": step_id,
                    "segment": seg,
                    "type": type(current).__name__,
                },
            )
    return current


def resolve_ref_path(
    path: str,
    tool_results: Mapping[str, ToolResult],
    current_step_id: str | None = None,
) -> Any:  # noqa: ANN401
    """Resolve a single reference path string against tool_results."""
    step_id, segments = parse_ref_path(path)

    if current_step_id is not None and step_id == current_step_id:
        raise ReferenceResolutionError(
            f"Self-reference detected: step {current_step_id!r} cannot reference its own output",
            detail={"step_id": current_step_id, "path": path},
        )

    result = tool_results.get(step_id)
    if result is None:
        raise ReferenceResolutionError(
            f"Referenced step {step_id!r} has produced no result in tool_results",
            detail={"step_id": step_id, "path": path},
        )

    resolved_raw = _walk_output(result.output, segments, step_id=step_id, path=path)
    # Deep copy ensures downstream mutation cannot alter stored ToolResult (§1.4)
    return copy.deepcopy(resolved_raw)


def resolve_value(
    value: Any,  # noqa: ANN401
    tool_results: Mapping[str, ToolResult],
    current_step_id: str | None = None,
    *,
    depth: int = 0,
    max_depth: int = 10,
) -> Any:  # noqa: ANN401
    """Recursively resolve any $ref markers in value."""
    if depth > max_depth:
        raise ReferenceResolutionError(
            f"Maximum reference resolution depth ({max_depth}) exceeded",
            detail={"depth": depth},
        )

    if isinstance(value, dict):
        if REF_KEY in value:
            if len(value) != 1:
                raise ReferenceResolutionError(
                    f"Malformed $ref dictionary: expected single '$ref' key, "
                    f"got {list(value.keys())}",
                    detail={"keys": list(value.keys())},
                )
            ref_path = value[REF_KEY]
            if not isinstance(ref_path, str):
                raise ReferenceResolutionError(
                    f"Malformed $ref: expected string path, got {type(ref_path).__name__}",
                    detail={"ref": ref_path},
                )
            return resolve_ref_path(ref_path, tool_results, current_step_id)
        return {
            k: resolve_value(v, tool_results, current_step_id, depth=depth + 1, max_depth=max_depth)
            for k, v in value.items()
        }

    if isinstance(value, str):
        if value.startswith(REF_PREFIX):
            path = value[len(REF_PREFIX) :]
            return resolve_ref_path(path, tool_results, current_step_id)
        return value

    if isinstance(value, list):
        return [
            resolve_value(elem, tool_results, current_step_id, depth=depth + 1, max_depth=max_depth)
            for elem in value
        ]

    if isinstance(value, tuple):
        return tuple(
            resolve_value(elem, tool_results, current_step_id, depth=depth + 1, max_depth=max_depth)
            for elem in value
        )

    return value


def resolve_step_args(
    step_or_state: AgentState | PlanStep,
    step_or_results: PlanStep | Mapping[str, ToolResult] | None = None,
) -> dict[str, Any]:
    """Resolve all references in a step's arguments against tool_results.

    Can be invoked either as:
        resolve_step_args(step, tool_results)
    or as an ArgResolver:
        resolve_step_args(state, step)
    """
    step: PlanStep
    tool_results: Mapping[str, ToolResult]

    if isinstance(step_or_state, dict) and "plan" in step_or_state:
        state = step_or_state
        if not isinstance(step_or_results, PlanStep):
            raise ValueError(
                "Expected PlanStep as second argument when first argument is AgentState"
            )
        step = step_or_results
        tool_results = state.get("tool_results") or {}
    else:
        if not isinstance(step_or_state, PlanStep):
            raise ValueError("Expected PlanStep as first argument")
        step = step_or_state
        tool_results = step_or_results or {}  # type: ignore[assignment]

    resolved = resolve_value(dict(step.args), tool_results, step.step_id)
    if not isinstance(resolved, dict):
        raise ReferenceResolutionError("Resolved arguments must be a dictionary")
    return resolved
