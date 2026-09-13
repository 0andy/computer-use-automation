"""Resolver is pure over Observation and exact-only (docs/spec.md 11.1, 12.1)."""

from __future__ import annotations

import ast
from pathlib import Path

import cua.conditions
import cua.resolver
from cua.models import AttrLocator, LabelLocator, RoleNameLocator, TableCellLocator, Target
from cua.resolver import LOCATOR_NOT_FOUND, ResolveFailure, locator_candidates, match_locator, resolve
from tests.unit.obs import BALANCE, MEMBER_ID, control, member_detail_page, members_page


def _imports(module) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_resolver_and_conditions_do_not_import_playwright() -> None:
    for module in (cua.resolver, cua.conditions):
        assert not any(name.startswith("playwright") for name in _imports(module))
        assert "cua.playwright_surface" not in _imports(module)


def test_label_locator_resolves_member_id_input() -> None:
    obs = members_page()
    ref = resolve(Target(locators=[LabelLocator(kind="label", text="Member ID")]), obs)
    assert ref == "1:1:2"
    assert obs.find(ref).role == "textbox"


def test_exact_only_no_substring_or_case_fuzz() -> None:
    obs = members_page()
    assert match_locator(LabelLocator(kind="label", text="Member"), obs) == []
    assert match_locator(LabelLocator(kind="label", text="member id"), obs) == []
    assert match_locator(RoleNameLocator(kind="role_name", role="button", name="Sear"), obs) == []
    assert match_locator(RoleNameLocator(kind="role_name", role="link", name="Search"), obs) == []
    assert len(match_locator(LabelLocator(kind="label", text="  Member   ID "), obs)) == 1  # whitespace-normalized


def test_zero_and_many_fall_through_to_next_candidate() -> None:
    obs = members_page()
    obs.controls.append(control("1:1:9", "textbox", label="Member ID", attrs={"name": "other"}))  # now 2 label matches
    target = Target(
        locators=[
            LabelLocator(kind="label", text="Member ID"),  # 2 matches -> skip
            RoleNameLocator(kind="role_name", role="textbox", name="Nope"),  # 0 matches -> skip
            AttrLocator(kind="attr", attr="name", value="member_id"),  # exactly one
        ]
    )
    assert resolve(target, obs) == "1:1:2"


def test_exhausted_candidates_is_locator_not_found() -> None:
    obs = members_page()
    obs.controls.append(control("1:1:9", "textbox", label="Member ID"))
    failure = resolve(
        Target(locators=[LabelLocator(kind="label", text="Member ID"), LabelLocator(kind="label", text="Zip")]), obs
    )
    assert isinstance(failure, ResolveFailure)
    assert failure.code == LOCATOR_NOT_FOUND
    assert "label:2" in failure.detail and "label:0" in failure.detail
    assert resolve(Target(locators=[]), obs).code == LOCATOR_NOT_FOUND


def test_savings_resolves_through_structural_table_cell() -> None:
    obs = member_detail_page()
    ref = resolve(Target(locators=[TableCellLocator(kind="table_cell", row_anchor="Savings", col_offset=1)]), obs)
    assert obs.find(ref).text == BALANCE
    # Anchor lookup is exact on the anchor cell text, in the same table and row only.
    assert match_locator(TableCellLocator(kind="table_cell", row_anchor="Sav", col_offset=1), obs) == []
    assert match_locator(TableCellLocator(kind="table_cell", row_anchor="Savings", col_offset=5), obs) == []
    member_ref = resolve(Target(locators=[TableCellLocator(kind="table_cell", row_anchor="Member ID", col_offset=1)]), obs)
    assert obs.find(member_ref).text == MEMBER_ID


def test_table_cell_requires_unique_result() -> None:
    obs = member_detail_page()
    obs.controls.append(control("2:1:11", "cell", text="Savings", name="Savings", table_index=1, row_index=5, col_index=0))
    obs.controls.append(control("2:1:12", "cell", text="$9.99", name="$9.99", table_index=1, row_index=5, col_index=1))
    locator = TableCellLocator(kind="table_cell", row_anchor="Savings", col_offset=1)
    assert len(match_locator(locator, obs)) == 2
    assert isinstance(resolve(Target(locators=[locator]), obs), ResolveFailure)


def test_resolver_does_not_mutate_observation() -> None:
    obs = members_page()
    before = obs.model_dump()
    resolve(Target(locators=[LabelLocator(kind="label", text="Member ID")]), obs)
    locator_candidates(obs.find("1:1:2"), obs, [MEMBER_ID])
    assert obs.model_dump() == before


# --- candidate generation (prompt 1.8): order, uniqueness, sensitivity, href ---


def test_member_id_and_search_candidates_in_order() -> None:
    obs = members_page()
    member_id = locator_candidates(obs.find("1:1:2"), obs, sensitive_literals=[MEMBER_ID])
    # A labelled input never gets a table_cell candidate: on Member Detail the same
    # row-anchored locator would resolve to the static "Member ID | <id>" cell instead.
    assert [c.model_dump() for c in member_id] == [
        {"kind": "label", "text": "Member ID"},
        {"kind": "attr", "attr": "name", "value": "member_id"},
    ]
    search = locator_candidates(obs.find("1:1:3"), obs, sensitive_literals=[MEMBER_ID])
    assert [c.model_dump() for c in search] == [{"kind": "role_name", "role": "button", "name": "Search"}]


def test_table_cell_is_only_for_controls_without_label_or_attr_identity() -> None:
    obs = members_page()
    textbox = obs.find("1:1:2")
    assert textbox.table_index is not None  # it sits in a table row, yet gets no structural candidate
    assert not any(c.kind == "table_cell" for c in locator_candidates(textbox, obs))
    unlabelled = textbox.model_copy(update={"label": None, "attrs": {"type": "text"}})
    obs.controls[obs.controls.index(textbox)] = unlabelled
    assert [c.model_dump() for c in locator_candidates(unlabelled, obs)] == [
        {"kind": "table_cell", "row_anchor": "Member ID", "col_offset": 1}
    ]


def test_savings_candidate_is_structural_never_balance_text() -> None:
    obs = member_detail_page()
    savings = locator_candidates(obs.find("2:1:8"), obs, sensitive_literals=[MEMBER_ID, BALANCE])
    assert [c.model_dump() for c in savings] == [{"kind": "table_cell", "row_anchor": "Savings", "col_offset": 1}]
    for candidate in savings:
        assert BALANCE not in candidate.model_dump_json()


def test_sensitive_candidate_is_discarded_before_uniqueness() -> None:
    obs = member_detail_page()
    cell = obs.find("2:1:2")  # the "12345" cell: unique role_name would exist, but it is sensitive
    without_literals = locator_candidates(cell, obs)
    assert {"kind": "role_name", "role": "cell", "name": MEMBER_ID} in [c.model_dump() for c in without_literals]
    with_literals = locator_candidates(cell, obs, sensitive_literals=[MEMBER_ID])
    assert [c.model_dump() for c in with_literals] == [{"kind": "table_cell", "row_anchor": "Member ID", "col_offset": 1}]


def test_non_unique_candidate_is_not_generated() -> None:
    obs = members_page()
    obs.controls.append(control("1:1:9", "button", name="Search", attrs={"type": "submit"}))
    assert locator_candidates(obs.find("1:1:3"), obs) == []


def test_href_is_never_a_locator_candidate() -> None:
    obs = member_detail_page()
    link = obs.find("2:1:10")
    assert link.href is not None
    candidates = locator_candidates(link, obs)
    assert candidates == [RoleNameLocator(kind="role_name", role="link", name="New search")]
    assert all("href" not in c.model_dump_json() and link.href not in c.model_dump_json() for c in candidates)
    # A control identified only by its href has no candidates at all.
    bare = control("2:1:99", "link", href="http://localhost:8000/x")
    obs.controls.append(bare)
    assert locator_candidates(bare, obs) == []
