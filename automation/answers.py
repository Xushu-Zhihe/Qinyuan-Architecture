"""Deterministic workgroup-result to submission-variable mapping."""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Any

from automation.manifest import AnswerMapping


class AnswerMappingError(ValueError):
    """Raised when a declared answer field cannot be resolved."""


_PATH_TOKEN = re.compile(r"[^.\[\]]+|\[(\d+)\]")


def build_answer_variables(
    mapping: AnswerMapping | None,
    workgroup_result: object | None,
    proposed_variables: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build operation variables while honoring the declared answer mapping.

    LLM-proposed variables remain available for platform-specific templates, but
    fields declared by the Manifest are resolved deterministically from the
    current workgroup result and take precedence over proposals.
    """

    variables = dict(proposed_variables or {})
    if workgroup_result is not None:
        variables.setdefault("workgroup_result", workgroup_result)
    if mapping is None:
        return variables

    mapped: dict[str, object] = {}
    for field in mapping.fields:
        found, value = lookup_path(workgroup_result, field.source_path)
        if not found:
            if field.required:
                raise AnswerMappingError(
                    "required answer field is missing: "
                    f"{field.target} <- {field.source_path}"
                )
            continue
        mapped[field.target] = value

    if mapping.fields:
        variables.update(mapped)
        variables["answers"] = mapped
        variables["answer"] = (
            next(iter(mapped.values()))
            if mapping.mode == "raw" and len(mapped) == 1
            else mapped
        )
    return variables


def lookup_path(value: object | None, path: str) -> tuple[bool, object | None]:
    """Resolve a dotted or JSON-style indexed path from a workgroup result."""

    normalized = path.strip()
    if normalized.startswith("$"):
        normalized = normalized[1:]
    normalized = normalized.lstrip(".")
    if not normalized:
        return True, value

    tokens = [
        match.group(1) or match.group(0)
        for match in _PATH_TOKEN.finditer(normalized)
    ]
    if not tokens:
        return False, None
    current = value
    for token in tokens:
        if isinstance(current, str):
            try:
                decoded = json.loads(current)
            except (TypeError, ValueError):
                return False, None
            if not isinstance(decoded, (Mapping, list)):
                return False, None
            current = decoded
        if isinstance(current, Mapping) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current
