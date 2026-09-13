"""Artifact Compiler: deterministic code that freezes a successful Discovery (docs/spec.md 12).

    successful Discovery -> DiscoveryRecord -> compile() -> CapabilityArtifact
                                                         -> capabilities/<app>/<capability_id>.json

The compiler runs immediately after a successful Discovery while the in-memory
``DiscoveryRecord`` (executed actions, pre-action Observations, verification
results, bindings, accepted Done, current sensitive literals) is still alive.
There is no historical compile command and no LLM involvement.

Compile rules (spec 12.2), in the order they are applied here:

 1/2. every actually executed physical action is compiled, in physical order;
 3.   ephemeral refs never enter the artifact (the artifact models have no ref field;
      nothing from ``ExecutedAction.ref`` is read here);
 4.   step targets are regenerated from the action's recorded pre-action Observation;
 5.   only exact-unique, non-sensitive candidates are frozen
      (``resolver.locator_candidates``); a control with no such candidate fails compilation;
 6/7. fill from a parameter -> ``FillAction(value=ParameterValue)`` with postcondition
      ``value_equals_parameter(target, param)`` over the regenerated target;
 8.   a read that captured its output -> typed money ``OutputSpec`` and postcondition
      ``output_present(capture_as)``;
 9.   a click whose model expect verified freezes that expect as its postcondition; a
      click whose expect failed is frozen with ``postcondition: None`` - nothing is invented;
 10.  deterministic step descriptions: ``Fill Member ID with {member_id}`` /
      ``Click Search`` / ``Read Savings balance``;
 11.  success = the accepted Done condition ANDed with ``output_present`` for every
      declared output;
 12.  a final-Observation control whose value equals a parameter and that has a unique
      non-sensitive *structural* (table_cell) locator adds a
      ``value_equals_parameter(target, param)`` success anchor;
 13.  authored business outcomes and recoveries are copied from the app config;
 14.  serialization is deterministic (declared field order, 2-space indent, trailing newline);
 15.  the complete serialized artifact is scanned against the run's sensitive literals;
      any hit fails compilation with an error that names the literal, never its value.
"""

from __future__ import annotations

import json
from pathlib import Path

from cua.config import AppConfig
from cua.discovery import DiscoveryRecord, ExecutedAction
from cua.evidence import Literals, find_sensitive, flat_literals
from cua.models import (
    AllCondition,
    CapabilityArtifact,
    ClickAction,
    Condition,
    Control,
    FillAction,
    InputSpec,
    Observation,
    OutputPresent,
    OutputSpec,
    ParameterValue,
    ReadAction,
    Step,
    TableCellLocator,
    Target,
    ValueEqualsParameter,
)
from cua.resolver import locator_candidates, normalize

SCHEMA_VERSION = "1.0"
INITIAL_REVISION = 1
CAPABILITIES_DIR = Path(__file__).resolve().parents[2] / "capabilities"


class CompileError(ValueError):
    """The DiscoveryRecord cannot be frozen into an artifact. The message is sanitized."""


# --------------------------------------------------------------------------- #
# Paths and serialization (rules 14, 15)
# --------------------------------------------------------------------------- #


def artifact_path(app: str, capability_id: str, root: Path = CAPABILITIES_DIR) -> Path:
    """``<root>/<app>/<capability_id>.json`` - the capability id deterministically selects the path."""
    return Path(root) / app / f"{capability_id}.json"


def serialize(artifact: CapabilityArtifact) -> str:
    """Deterministic JSON: declared field order, 2-space indent, UTF-8 text, trailing newline."""
    return json.dumps(artifact.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"


def scan_for_leaks(text: str, literals: Literals) -> str | None:
    """Name of the sensitive literal found in ``text`` (never the value), or None."""
    return find_sensitive(text, literals)


def write_artifact(artifact: CapabilityArtifact, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize(artifact), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def control_display_name(control: Control) -> str:
    """Human-readable, deterministic name of a control for step descriptions."""
    for candidate in (control.label, control.name, control.attrs.get("id"), control.attrs.get("name")):
        if candidate and normalize(candidate):
            return normalize(candidate)
    return control.role


def humanize(identifier: str) -> str:
    """``savings_balance`` -> ``Savings balance``."""
    words = identifier.replace("_", " ").split()
    if not words:
        return identifier
    return " ".join([words[0].capitalize(), *words[1:]])


_LEADING_VERB_FORMS = {"lookup": "look up"}  # id tokens that read better as two words


def default_description(capability_id: str) -> str:
    """Deterministic plain sentence from the capability id: ``lookup_member_balance`` -> ``Look up member balance.``

    Never derived from the goal: the goal is a template with ``{placeholders}``,
    while the description is the human sentence the Catalog shows.
    """
    words = capability_id.split("_")
    words[0] = _LEADING_VERB_FORMS.get(words[0], words[0])
    return humanize(" ".join(w for w in words if w).replace(" ", "_")) + "."


def describe(action: ExecutedAction) -> str:
    if action.kind == "fill":
        return f"Fill {control_display_name(action.control)} with {{{action.from_param}}}"
    if action.kind == "click":
        return f"Click {control_display_name(action.control)}"
    if action.kind == "read":
        return f"Read {humanize(action.capture_as or '')}".rstrip()
    raise CompileError(f"unknown executed action kind {action.kind!r}")  # unreachable: closed


def _regenerate_target(action: ExecutedAction, step_id: str, literals: Literals) -> Target:
    """Rule 4/5: locator candidates from the recorded pre-action Observation, unique + non-sensitive only."""
    candidates = locator_candidates(action.control, action.pre_observation, flat_literals(literals))
    if not candidates:
        raise CompileError(
            f"step {step_id} ({action.kind} on {action.control.role} {control_display_name(action.control)!r}): "
            "no exact-unique, non-sensitive locator candidate in its pre-action Observation"
        )
    return Target(locators=candidates)


def _flatten(condition: Condition) -> list[Condition]:
    if isinstance(condition, AllCondition):
        out: list[Condition] = []
        for child in condition.conditions:
            out.extend(_flatten(child))
        return out
    return [condition]


def _observed_value(control: Control) -> str:
    return normalize(control.input_value if control.input_value is not None else control.text)


def parameter_anchors(observation: Observation, record: DiscoveryRecord) -> list[ValueEqualsParameter]:
    """Rule 12: one ``value_equals_parameter`` anchor per parameter whose value is shown by a
    control that has a unique, non-sensitive structural (table_cell) locator."""
    literals = flat_literals(record.literals)
    anchors: list[ValueEqualsParameter] = []
    for name, param in record.params.items():
        want = normalize(param.value)
        if not want:
            continue
        for control in observation.controls:
            if _observed_value(control) != want:
                continue
            structural = [
                locator
                for locator in locator_candidates(control, observation, literals)
                if isinstance(locator, TableCellLocator)
            ]
            if structural:
                anchors.append(
                    ValueEqualsParameter(kind="value_equals_parameter", target=Target(locators=structural), param=name)
                )
                break
    return anchors


# --------------------------------------------------------------------------- #
# compile
# --------------------------------------------------------------------------- #


def compile_record(record: DiscoveryRecord, config: AppConfig, description: str | None = None) -> CapabilityArtifact:
    """Freeze a successful in-memory DiscoveryRecord into a CapabilityArtifact (spec 12.2).

    ``description`` is the authored human sentence for the Catalog; when omitted it is
    derived from the capability id. It is never the goal template.
    """
    if description is not None and not description.strip():
        raise CompileError("description must not be blank")
    description = description.strip() if description is not None else default_description(record.capability_id)
    if record.stop_reason != "goal_completed" or record.done_condition is None:
        raise CompileError(
            f"only a Discovery that stopped with goal_completed can be compiled (stop_reason={record.stop_reason!r})"
        )
    if record.app != config.app:
        raise CompileError(f"record app {record.app!r} does not match config app {config.app!r}")
    if not record.actions:
        raise CompileError("the Discovery executed no physical action; nothing to compile")
    literals = record.literals

    inputs = {name: InputSpec(type=param.type, required=True, sensitive=param.sensitive) for name, param in record.params.items()}

    steps: list[Step] = []
    outputs: dict[str, OutputSpec] = {}
    for position, action in enumerate(record.actions, start=1):  # rule 1/2: physical order
        step_id = f"s{position}"
        target = _regenerate_target(action, step_id, literals)
        if action.kind == "fill":
            if not action.from_param or action.from_param not in record.params:
                raise CompileError(f"step {step_id}: fill did not come from a declared parameter")
            step_action = FillAction(kind="fill", value=ParameterValue(name=action.from_param))
            postcondition: Condition | None = ValueEqualsParameter(
                kind="value_equals_parameter", target=target, param=action.from_param
            )
        elif action.kind == "click":
            step_action = ClickAction(kind="click")
            postcondition = action.expect if action.expect_verified else None  # rule 9: never invented
        elif action.kind == "read":
            if not action.capture_as or action.parser != "money":
                raise CompileError(f"step {step_id}: read without a capture name or with an unsupported parser")
            step_action = ReadAction(kind="read", capture_as=action.capture_as, parser="money")
            if action.output_captured:
                outputs[action.capture_as] = OutputSpec(
                    type="money", from_step=step_id, capture_as=action.capture_as, sensitive=True
                )
                postcondition = OutputPresent(kind="output_present", name=action.capture_as)
            else:
                postcondition = None  # the read executed but captured nothing; nothing is invented
        else:
            raise CompileError(f"step {step_id}: unknown action kind {action.kind!r}")
        steps.append(Step(id=step_id, description=describe(action), action=step_action, target=target, postcondition=postcondition))

    # rule 11: accepted Done AND output_present for every declared output
    conditions = _flatten(record.done_condition)
    present = {c.name for c in conditions if isinstance(c, OutputPresent)}
    for name in outputs:
        if name not in present:
            conditions.append(OutputPresent(kind="output_present", name=name))
    # rule 12: parameter anchor(s) from the final Observation
    if record.final_observation is not None:
        conditions.extend(parameter_anchors(record.final_observation, record))
    success: Condition = conditions[0] if len(conditions) == 1 else AllCondition(kind="all", conditions=conditions)

    artifact = CapabilityArtifact(
        schema_version=SCHEMA_VERSION,
        revision=INITIAL_REVISION,
        capability_id=record.capability_id,
        app=record.app,
        description=description,
        inputs=inputs,
        outputs=outputs,
        steps=steps,
        business_outcomes=list(config.business_outcomes),  # rule 13
        recoveries=list(config.recoveries),
        success=success,
    )

    # rule 15: the complete serialized artifact must contain no sensitive runtime literal
    hit = scan_for_leaks(serialize(artifact), literals)
    if hit is not None:
        raise CompileError(f"compiled artifact contains the sensitive value of {hit!r}; compilation refused")
    return artifact
