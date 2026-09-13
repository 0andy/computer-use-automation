"""Pure, exact-only locator resolution over an Observation (docs/spec.md section 11.1).

Nothing here imports Playwright. Matching is exact after whitespace
normalization; no fuzzy locator ever drives an action.

Also provides ``locator_candidates`` (spec 12.1 / prompt 1.8): the ordered,
unique, non-sensitive locator candidates for a Control, which the Compiler
freezes into artifacts. ``href`` is never a candidate.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from cua.models import (
    AttrLocator,
    Control,
    ControlRef,
    LabelLocator,
    Locator,
    Observation,
    RoleNameLocator,
    TableCellLocator,
    Target,
)

LOCATOR_NOT_FOUND = "LOCATOR_NOT_FOUND"


@dataclass(frozen=True)
class ResolveFailure:
    code: str
    detail: str


def normalize(text: str | None) -> str:
    """Whitespace normalization used everywhere text is compared."""
    return " ".join((text or "").split())


def match_locator(locator: Locator, observation: Observation) -> list[Control]:
    """All controls in every frame that exactly match ``locator``."""
    controls = observation.controls
    if isinstance(locator, LabelLocator):
        want = normalize(locator.text)
        return [c for c in controls if c.label is not None and normalize(c.label) == want]
    if isinstance(locator, RoleNameLocator):
        want = normalize(locator.name)
        return [
            c
            for c in controls
            if c.role == locator.role and c.name is not None and normalize(c.name) == want
        ]
    if isinstance(locator, AttrLocator):
        return [c for c in controls if c.attrs.get(locator.attr) == locator.value]
    if isinstance(locator, TableCellLocator):
        return _match_table_cell(locator, controls)
    raise TypeError(f"unknown locator {locator!r}")  # unreachable: closed union


def _match_table_cell(locator: TableCellLocator, controls: list[Control]) -> list[Control]:
    anchor_text = normalize(locator.row_anchor)
    anchors = [
        c
        for c in controls
        if c.table_index is not None
        and c.row_index is not None
        and c.col_index is not None
        and normalize(c.text) == anchor_text
    ]
    matches: list[Control] = []
    for anchor in anchors:
        want_col = anchor.col_index + locator.col_offset
        for c in controls:
            if (
                c is not anchor
                and c.frame_path == anchor.frame_path
                and c.table_index == anchor.table_index
                and c.row_index == anchor.row_index
                and c.col_index == want_col
                and c not in matches
            ):
                matches.append(c)
    return matches


def resolve(target: Target, observation: Observation) -> ControlRef | ResolveFailure:
    """First locator with exactly one match wins; zero/many falls through."""
    tried: list[str] = []
    for locator in target.locators:
        matches = match_locator(locator, observation)
        if len(matches) == 1:
            return matches[0].ref
        tried.append(f"{locator.kind}:{len(matches)}")
    return ResolveFailure(LOCATOR_NOT_FOUND, "no locator matched exactly once (" + ", ".join(tried) + ")")


def resolves_uniquely(locator: Locator, observation: Observation) -> bool:
    return len(match_locator(locator, observation)) == 1


def _contains_sensitive(values: Iterable[str], literals: Iterable[str]) -> bool:
    return any(lit and lit in value for value in values for lit in literals)


def locator_candidates(
    control: Control,
    observation: Observation,
    sensitive_literals: Iterable[str] = (),
) -> list[Locator]:
    """Ordered locator candidates for ``control``: label, role_name, attr(id/name), table_cell.

    A candidate is kept only if it matches exactly this one control across the
    whole multi-frame Observation and none of its persisted fields contain a
    known sensitive runtime literal. ``href`` is policy metadata and is never a
    candidate.

    ``table_cell`` is proposed only for a control that has neither a label nor
    an id/name attr: it is structural identity for value cells (the sensitive
    Savings cell, anchored on its non-sensitive row label). A labelled input
    never gets one, because the same row-anchored cell locator can resolve to a
    different element on another page (on Member Detail, ``Member ID | <id>``
    is a static cell, not the input), and a frozen postcondition target must
    not drift like that.
    """
    literals = [lit for lit in sensitive_literals if lit]
    proposed: list[Locator] = []
    if control.label:
        proposed.append(LabelLocator(kind="label", text=normalize(control.label)))
    if control.name:
        proposed.append(RoleNameLocator(kind="role_name", role=control.role, name=normalize(control.name)))
    for attr in ("id", "name"):
        value = control.attrs.get(attr)
        if value:
            proposed.append(AttrLocator(kind="attr", attr=attr, value=value))
    has_named_identity = any(isinstance(loc, (LabelLocator, AttrLocator)) for loc in proposed)
    if (
        not has_named_identity
        and control.table_index is not None
        and control.row_index is not None
        and control.col_index is not None
    ):
        anchors = [
            c
            for c in observation.controls
            if c is not control
            and c.frame_path == control.frame_path
            and c.table_index == control.table_index
            and c.row_index == control.row_index
            and c.col_index is not None
            and c.col_index < control.col_index
            and normalize(c.text)
        ]
        anchors.sort(key=lambda c: -c.col_index)  # nearest left cell first
        for anchor in anchors:
            proposed.append(
                TableCellLocator(
                    kind="table_cell",
                    row_anchor=normalize(anchor.text),
                    col_offset=control.col_index - anchor.col_index,
                )
            )

    kept: list[Locator] = []
    for locator in proposed:
        persisted = [str(v) for v in locator.model_dump().values()]
        if _contains_sensitive(persisted, literals):
            continue  # discarded before any leak scan
        matches = match_locator(locator, observation)
        if len(matches) == 1 and matches[0].ref == control.ref:
            kept.append(locator)
    return kept
