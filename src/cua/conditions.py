"""The one condition evaluator: pure over an Observation (docs/spec.md 6.2, 10.3, 11.2).

No Playwright here and no fallback to live text queries. Only the six closed
condition kinds exist; ``role_exists`` and ``dialog_visible`` are deliberately
not implemented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from cua.models import (
    AllCondition,
    Condition,
    Observation,
    OutputPresent,
    TextAbsent,
    TextVisible,
    UrlMatches,
    ValueEqualsParameter,
)
from cua.resolver import ResolveFailure, normalize, resolve


@dataclass
class Bindings:
    """Runtime-only state a condition may refer to: parameter values and captured outputs."""

    params: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)


def text_visible(text: str, observation: Observation) -> bool:
    """Some Control in any frame has ``text`` or ``name`` containing ``text`` (whitespace-normalized)."""
    want = normalize(text)
    if not want:
        return False
    for control in observation.controls:
        if want in normalize(control.text) or want in normalize(control.name):
            return True
    return False


def url_matches(pattern: str, observation: Observation) -> bool:
    """``re.search(pattern, frame_url)`` over every frame URL (top-level included)."""
    urls = [observation.url, *observation.frames.values()]
    return any(re.search(pattern, url) for url in urls)


def evaluate(condition: Condition, observation: Observation, bindings: Bindings) -> bool:
    if isinstance(condition, TextVisible):
        return text_visible(condition.text, observation)
    if isinstance(condition, TextAbsent):
        return not text_visible(condition.text, observation)
    if isinstance(condition, UrlMatches):
        return url_matches(condition.pattern, observation)
    if isinstance(condition, ValueEqualsParameter):
        expected = bindings.params.get(condition.param)
        if expected is None:
            return False
        ref = resolve(condition.target, observation)
        if isinstance(ref, ResolveFailure):
            return False
        control = observation.find(ref)
        if control is None:
            return False
        observed = control.input_value if control.input_value is not None else control.text
        return normalize(observed) == normalize(expected)
    if isinstance(condition, OutputPresent):
        return bindings.outputs.get(condition.name) is not None
    if isinstance(condition, AllCondition):
        return all(evaluate(c, observation, bindings) for c in condition.conditions)
    raise TypeError(f"unknown condition {condition!r}")  # unreachable: closed union
