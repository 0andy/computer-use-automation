"""Same-session human-in-the-loop handoff seam (docs/spec.md 15).

HITL is a RunControl ownership state, never a result kind. When Replay is stuck
on a handoff-eligible step-level code and handoff is enabled, it hands the
*same* live session to an Operator:

    AUTOMATION -> NEEDS_HUMAN -> HUMAN -> AUTOMATION   (-> COMPLETED at run end)

The engine side of the loop lives in ``cua.replay`` (it re-uses the normative
settle loop for Continue/Retry). This module holds the pieces around it:

* ``Operator`` protocol with ``ConsoleOperator`` (real headed demo; blocks on
  console input) and ``ScriptedOperator`` (deterministic tests; never blocks);
* the sanitized ``Intervention`` record handed to the operator and persisted as
  ``intervention.json``;
* ``HumanEventCapture`` protocol: pull-based capture of sanitized human UI
  events (implemented for Playwright in ``cua.playwright_surface``);
* structural collection of known sensitive controls (declared Fill/Read targets,
  parameter anchors, and controls showing a known runtime literal) used both for
  opaque screenshot masking (Pillow, no OCR) and for redacting observed summaries.

Nothing here imports Playwright.
"""

from __future__ import annotations

import builtins
import math
from collections.abc import Callable, Iterable, Sequence
from enum import Enum
from io import BytesIO
from typing import Any, Literal, Protocol

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict

from cua.evidence import Literals
from cua.models import (
    AllCondition,
    BBox,
    CapabilityArtifact,
    Condition,
    ControlRef,
    FillAction,
    Observation,
    Owner,
    ReadAction,
    Target,
    ValueEqualsParameter,
)
from cua.resolver import ResolveFailure, normalize, resolve

# --------------------------------------------------------------------------- #
# Operator decisions and the intervention record (spec 15.2, 15.4, 15.7)
# --------------------------------------------------------------------------- #


class OperatorDecision(str, Enum):
    CONTINUE = "continue"  # re-run settle for the current contract; never redo the business action
    RETRY = "retry"  # re-resolve the target, Policy, re-execute the step action, then settle
    ABORT = "abort"  # result kind aborted


DECISION_KEYS: dict[str, OperatorDecision] = {
    "c": OperatorDecision.CONTINUE,
    "continue": OperatorDecision.CONTINUE,
    "r": OperatorDecision.RETRY,
    "retry": OperatorDecision.RETRY,
    "a": OperatorDecision.ABORT,
    "abort": OperatorDecision.ABORT,
}

Phase = Literal["before", "act", "after"]


class Intervention(BaseModel):
    """Sanitized handoff record (spec 15.4). Every text field is already sanitized when built."""

    model_config = ConfigDict(extra="forbid")

    app: str
    capability: str
    step_id: str
    step_description: str
    phase: Phase  # which part of the step contract was unresolved
    code: str  # reason / error code (LOCATOR_NOT_FOUND | POSTCONDITION_FAILED | RECOVERY_EXHAUSTED)
    expected: Condition | None
    observed_summary: str
    masked_screenshot: str | None  # evidence-relative path of the masked PNG; the raw image is never persisted
    owner: Owner  # owner while the human holds the session


class Operator(Protocol):
    def take_control(self, intervention: Intervention) -> OperatorDecision: ...


# --------------------------------------------------------------------------- #
# ConsoleOperator: the real headed demo. Blocks on console input by design.
# --------------------------------------------------------------------------- #

PROMPT = "[R] Retry  [C] Continue  [A] Abort > "


def parse_decision(text: str) -> OperatorDecision | None:
    return DECISION_KEYS.get(normalize(text).lower())


def format_intervention(intervention: Intervention) -> str:
    expected = intervention.expected.model_dump(mode="json") if intervention.expected is not None else None
    lines = [
        "",
        "=== HUMAN INTERVENTION REQUIRED ===",
        f"app/capability : {intervention.app} / {intervention.capability}",
        f"step           : {intervention.step_id} - {intervention.step_description} ({intervention.phase})",
        f"code           : {intervention.code}",
        f"expected       : {expected}",
        f"observed       : {intervention.observed_summary}",
        f"screenshot     : {intervention.masked_screenshot} (masked)",
        f"owner          : {intervention.owner.value}",
        "You now own the live browser window. Fix the state in that window, then choose:",
        "  [C] Continue - revalidate the current step (the business action is NOT repeated)",
        "  [R] Retry    - re-resolve the target and re-execute the step action through Policy",
        "  [A] Abort    - stop with result kind 'aborted'",
    ]
    return "\n".join(lines)


class ConsoleOperator:
    """Reads the decision from the console. Requires a real headed session; never used under pytest."""

    def __init__(
        self,
        input_fn: Callable[[str], str] | None = None,
        output_fn: Callable[[str], None] | None = None,
    ) -> None:
        self._input = input_fn  # None -> builtins.input, looked up at call time
        self._output = output_fn if output_fn is not None else print

    def take_control(self, intervention: Intervention) -> OperatorDecision:
        read = self._input if self._input is not None else builtins.input
        self._output(format_intervention(intervention))
        while True:
            try:
                raw = read(PROMPT)
            except EOFError:
                self._output("no console input available: aborting")
                return OperatorDecision.ABORT
            decision = parse_decision(raw)
            if decision is not None:
                return decision
            self._output(f"unrecognized choice {raw.strip()!r}; type R, C or A")


# --------------------------------------------------------------------------- #
# ScriptedOperator: deterministic tests. Never blocks; a callable entry lets the
# test act as the human on the live page before returning its decision.
# --------------------------------------------------------------------------- #

ScriptEntry = OperatorDecision | str | Callable[[Intervention], "OperatorDecision | str"]


class ScriptExhausted(RuntimeError):
    """Replay asked for more decisions than the script holds (a test bug, surfaced loudly)."""


class ScriptedOperator:
    def __init__(self, script: Sequence[ScriptEntry]) -> None:
        self._script = list(script)
        self.interventions: list[Intervention] = []
        self.decisions: list[OperatorDecision] = []

    @property
    def calls(self) -> int:
        return len(self.interventions)

    def take_control(self, intervention: Intervention) -> OperatorDecision:
        self.interventions.append(intervention)
        if not self._script:
            raise ScriptExhausted(f"no scripted decision left for handoff #{len(self.interventions)} ({intervention.code})")
        entry = self._script.pop(0)
        if callable(entry):
            entry = entry(intervention)
        decision = parse_decision(entry) if isinstance(entry, str) else entry
        if not isinstance(decision, OperatorDecision):
            raise ValueError(f"scripted entry {entry!r} is not an operator decision")
        self.decisions.append(decision)
        return decision


# --------------------------------------------------------------------------- #
# Human event capture seam (spec 15.6): pull-based, sanitized, HUMAN-owner only
# --------------------------------------------------------------------------- #


class HumanEventCapture(Protocol):
    """Pull-based capture of human UI events on the live session.

    ``begin()`` is called right after ownership moves to HUMAN, ``end()`` right
    before it moves back to AUTOMATION. ``end()`` returns every captured event
    (in-page buffer + Python-side navigation records) and stops capturing.
    Events carry ``kind`` (click | input | change | navigation), ``source``
    (page | python) and a control descriptor; never an input value.
    """

    def begin(self) -> None: ...

    def end(self) -> list[dict[str, Any]]: ...


class NoHumanCapture:
    """Capture for surfaces without a live page (unit tests over scripted Observations)."""

    def begin(self) -> None:
        return None

    def end(self) -> list[dict[str, Any]]:
        return []


# --------------------------------------------------------------------------- #
# Known sensitive controls -> masking and summary redaction (spec 9.4, 15.6)
# --------------------------------------------------------------------------- #


def _walk_conditions(condition: Condition | None) -> Iterable[Condition]:
    if condition is None:
        return
    yield condition
    if isinstance(condition, AllCondition):
        for child in condition.conditions:
            yield from _walk_conditions(child)


def sensitive_targets(artifact: CapabilityArtifact) -> list[tuple[str, Target]]:
    """(name, structural target) for every control the artifact declares as carrying a sensitive value."""
    found: list[tuple[str, Target]] = []
    conditions: list[Condition | None] = [artifact.success]
    for step in artifact.steps:
        conditions.append(step.postcondition)
        if step.target is None:
            continue
        action = step.action
        if isinstance(action, FillAction):
            spec = artifact.inputs.get(action.value.name)
            if spec is not None and spec.sensitive:
                found.append((action.value.name, step.target))
        elif isinstance(action, ReadAction):
            spec = artifact.outputs.get(action.capture_as)
            if spec is None or spec.sensitive:
                found.append((action.capture_as, step.target))
    for root in conditions:
        for condition in _walk_conditions(root):
            if isinstance(condition, ValueEqualsParameter):
                spec = artifact.inputs.get(condition.param)
                if spec is not None and spec.sensitive:
                    found.append((condition.param, condition.target))
    return found


def sensitive_controls(
    artifact: CapabilityArtifact,
    observation: Observation,
    literals: Literals,
) -> dict[ControlRef, str]:
    """``ref -> name`` of every control on screen that is known to carry a sensitive value.

    Structural: declared Fill targets of sensitive inputs, Read targets of
    sensitive outputs and ``value_equals_parameter`` anchors, resolved on this
    Observation (so a diverged page still masks the Member ID / Savings cells);
    plus any control whose text or input value contains a known runtime literal.
    No OCR, no fuzzy matching.
    """
    refs: dict[ControlRef, str] = {}
    for name, target in sensitive_targets(artifact):
        ref = resolve(target, observation)
        if not isinstance(ref, ResolveFailure):
            refs.setdefault(ref, name)
    forms = [(form, name) for name, values in literals.items() for form in values if form]
    if forms:
        for control in observation.controls:
            if control.ref in refs:
                continue
            shown = [normalize(control.text), normalize(control.input_value)]
            for form, name in forms:
                if any(form in value for value in shown if value):
                    refs[control.ref] = name
                    break
    return refs


def sensitive_bboxes(
    artifact: CapabilityArtifact,
    observation: Observation,
    literals: Literals,
) -> list[BBox]:
    refs = sensitive_controls(artifact, observation, literals)
    return [control.bbox for control in observation.controls if control.ref in refs]


def mask_screenshot(png: bytes, bboxes: Iterable[BBox], scale: float = 1.0) -> Image.Image:
    """Opaque black rectangles over ``bboxes`` (page-relative CSS px * ``scale``) on the PNG.

    The raw bytes are consumed in memory only; the caller persists the returned
    image and nothing else.
    """
    image = Image.open(BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    for box in bboxes:
        if box.width <= 0 or box.height <= 0:
            continue
        x0 = max(0, math.floor(box.x * scale))
        y0 = max(0, math.floor(box.y * scale))
        x1 = min(width, math.ceil((box.x + box.width) * scale))
        y1 = min(height, math.ceil((box.y + box.height) * scale))
        if x1 <= x0 or y1 <= y0:
            continue
        draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=(0, 0, 0))
    return image
