"""Closed demo vocabulary: Observation, Policy, artifact, runtime and result models.

Everything here mirrors docs/spec.md sections 6, 7, 10, 14 and 15 field-for-field.
Every model forbids extra fields, and every polymorphic value is a discriminated
union on ``kind`` so an unknown kind or an unexpected field is a validation error,
never silently accepted data.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class _Closed(BaseModel):
    """Base for every model: no undeclared fields."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Observation (spec 6.2)
# --------------------------------------------------------------------------- #

ControlRef = str  # ephemeral; valid for exactly one Observation


class BBox(_Closed):
    """Page-relative bounding box in CSS pixels (top-level viewport coordinates)."""

    x: float
    y: float
    width: float
    height: float


class Control(_Closed):
    ref: ControlRef
    role: str
    name: str | None = None
    label: str | None = None
    text: str | None = None
    input_value: str | None = None
    attrs: dict[str, str] = Field(default_factory=dict)  # safe attrs only
    href: str | None = None  # policy metadata only; never a locator candidate
    frame_path: str
    ancestor_roles: list[str] = Field(default_factory=list)
    bbox: BBox
    table_index: int | None = None
    row_index: int | None = None
    col_index: int | None = None


class Observation(_Closed):
    url: str  # top-level URL
    frames: dict[str, str]  # frame_path -> frame URL
    controls: list[Control]

    def find(self, ref: ControlRef) -> Control | None:
        for control in self.controls:
            if control.ref == ref:
                return control
        return None


# --------------------------------------------------------------------------- #
# Artifact: inputs/outputs, locators, conditions, actions, steps (spec 10)
# --------------------------------------------------------------------------- #


class InputSpec(_Closed):
    type: Literal["string"]
    required: bool = True
    sensitive: bool = True


class OutputSpec(_Closed):
    type: Literal["money"]
    from_step: str
    capture_as: str
    sensitive: bool = True


class LabelLocator(_Closed):
    kind: Literal["label"]
    text: str


class RoleNameLocator(_Closed):
    kind: Literal["role_name"]
    role: str
    name: str


class AttrLocator(_Closed):
    kind: Literal["attr"]
    attr: Literal["id", "name"]
    value: str


class TableCellLocator(_Closed):
    kind: Literal["table_cell"]
    row_anchor: str
    col_offset: int


Locator = Annotated[
    Union[LabelLocator, RoleNameLocator, AttrLocator, TableCellLocator],
    Field(discriminator="kind"),
]


class Target(_Closed):
    locators: list[Locator]


class TextVisible(_Closed):
    kind: Literal["text_visible"]
    text: str


class TextAbsent(_Closed):
    kind: Literal["text_absent"]
    text: str


class UrlMatches(_Closed):
    kind: Literal["url_matches"]
    pattern: str


class ValueEqualsParameter(_Closed):
    kind: Literal["value_equals_parameter"]
    target: Target
    param: str


class OutputPresent(_Closed):
    kind: Literal["output_present"]
    name: str


class AllCondition(_Closed):
    kind: Literal["all"]
    conditions: list[Condition]


Condition = Annotated[
    Union[TextVisible, TextAbsent, UrlMatches, ValueEqualsParameter, OutputPresent, AllCondition],
    Field(discriminator="kind"),
]

AllCondition.model_rebuild()


class ParameterValue(_Closed):
    kind: Literal["parameter"] = "parameter"
    name: str


class ClickAction(_Closed):
    kind: Literal["click"]


class FillAction(_Closed):
    kind: Literal["fill"]
    value: ParameterValue


class ReadAction(_Closed):
    kind: Literal["read"]
    capture_as: str
    parser: Literal["money"]


Action = Annotated[Union[ClickAction, FillAction, ReadAction], Field(discriminator="kind")]


class Step(_Closed):
    id: str
    description: str
    action: Action
    target: Target | None
    postcondition: Condition | None


class BusinessOutcome(_Closed):
    code: str
    when: Condition


class Recovery(_Closed):
    code: str
    when: Condition
    action: Action
    target: Target | None


class CapabilityArtifact(_Closed):
    schema_version: Literal["1.0"]
    revision: int
    capability_id: str
    app: str
    description: str
    inputs: dict[str, InputSpec]
    outputs: dict[str, OutputSpec]
    steps: list[Step]
    business_outcomes: list[BusinessOutcome]
    recoveries: list[Recovery]
    success: Condition


# --------------------------------------------------------------------------- #
# Runtime actions and results (spec 10.4, 6.1) - never persisted
# --------------------------------------------------------------------------- #


class RuntimeNavigate(_Closed):
    kind: Literal["navigate"]
    url: str  # startup only: the app config entry_url


class RuntimeClick(_Closed):
    kind: Literal["click"]
    ref: ControlRef


class RuntimeFill(_Closed):
    kind: Literal["fill"]
    ref: ControlRef
    value: str  # raw runtime binding; memory-only


class RuntimeRead(_Closed):
    kind: Literal["read"]
    ref: ControlRef


RuntimeAction = Annotated[
    Union[RuntimeNavigate, RuntimeClick, RuntimeFill, RuntimeRead],
    Field(discriminator="kind"),
]


class ActionResult(_Closed):
    """Outcome of one ``Surface.act`` call.

    ``executed`` is True only if the physical action actually ran. A rejected
    action (missing/denied authorization, wrong owner, stale ref) is not executed
    and carries an ``error`` code.
    """

    executed: bool
    value: str | None = None  # raw read value (runtime memory only)
    error: str | None = None


# --------------------------------------------------------------------------- #
# Policy (spec 7)
# --------------------------------------------------------------------------- #


class PolicyDecision(_Closed):
    """Authorization for exactly one proposed RuntimeAction.

    The decision is bound to the action it was made for: ``Surface.act`` refuses
    an authorization whose ``action`` differs from the action being performed.
    """

    allowed: bool
    reason: str
    action: RuntimeAction


# --------------------------------------------------------------------------- #
# Replay result contract (spec 14.6) - shape only; Replay itself is Phase 4
# --------------------------------------------------------------------------- #


class ReplayFailure(_Closed):
    step_id: str | None
    code: str
    expected: Condition | None
    observed_summary: str
    escalation: Literal["unavailable_headless"] | None = None


class ReplayResult(_Closed):
    kind: Literal["success", "business_outcome", "failure", "aborted"]
    outputs: dict[str, str]
    business_outcome: str | None
    failure: ReplayFailure | None
    llm_calls: Literal[0] = 0
    recovery_count: int


# --------------------------------------------------------------------------- #
# RunControl ownership (spec 15.1) - the state only; handoff is Phase 5
# --------------------------------------------------------------------------- #


class Owner(str, Enum):
    AUTOMATION = "AUTOMATION"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    HUMAN = "HUMAN"
    COMPLETED = "COMPLETED"


# Allowed ownership transitions (spec 15.1):
#   AUTOMATION -> NEEDS_HUMAN -> HUMAN -> AUTOMATION -> ... -> COMPLETED
OWNER_TRANSITIONS: dict[Owner, frozenset[Owner]] = {
    Owner.AUTOMATION: frozenset({Owner.NEEDS_HUMAN, Owner.COMPLETED}),
    Owner.NEEDS_HUMAN: frozenset({Owner.HUMAN}),
    Owner.HUMAN: frozenset({Owner.AUTOMATION}),
    Owner.COMPLETED: frozenset(),
}


@dataclass
class RunControl:
    """Who currently owns the live session. Automated actions run only under AUTOMATION.

    One RunControl is shared by the Surface (which gates ``act``) and the engine
    (which moves ownership through ``transfer``). HITL is this state, never a
    result kind.
    """

    owner: Owner = Owner.AUTOMATION

    def transfer(self, to: Owner) -> Owner:
        """Move ownership along the spec 15.1 state machine; return the previous owner."""
        if to not in OWNER_TRANSITIONS[self.owner]:
            raise ValueError(f"ownership cannot move from {self.owner.value} to {to.value}")
        previous, self.owner = self.owner, to
        return previous
