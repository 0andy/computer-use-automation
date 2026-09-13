"""The one condition evaluator over the closed vocabulary (docs/spec.md 6.2, 10.3, 11.2)."""

from __future__ import annotations

from pydantic import TypeAdapter

from cua.conditions import Bindings, evaluate
from cua.models import (
    AllCondition,
    Condition,
    LabelLocator,
    OutputPresent,
    TableCellLocator,
    Target,
    TextAbsent,
    TextVisible,
    UrlMatches,
    ValueEqualsParameter,
)
from tests.unit.obs import BALANCE, MEMBER_ID, control, member_detail_page, members_page

NONE = Bindings()


def test_text_visible_and_absent_across_all_frames() -> None:
    obs = members_page()
    assert evaluate(TextVisible(kind="text_visible", text="Member ID"), obs, NONE)  # child frame cell
    assert evaluate(TextVisible(kind="text_visible", text="MockBank Online"), obs, NONE)  # top frame
    assert evaluate(TextVisible(kind="text_visible", text="Search"), obs, NONE)  # button name, no text
    assert not evaluate(TextVisible(kind="text_visible", text="No member found"), obs, NONE)
    assert evaluate(TextAbsent(kind="text_absent", text="No member found"), obs, NONE)
    assert not evaluate(TextAbsent(kind="text_absent", text="Member ID"), obs, NONE)
    assert not evaluate(TextVisible(kind="text_visible", text=""), obs, NONE)


def test_text_visible_is_whitespace_normalized_substring() -> None:
    obs = members_page()
    obs.controls.append(control("1:1:8", "paragraph", text="Scheduled   maintenance\n is in progress."))
    assert evaluate(TextVisible(kind="text_visible", text="maintenance is in"), obs, NONE)
    assert evaluate(TextVisible(kind="text_visible", text="  maintenance   is   in  "), obs, NONE)
    assert not evaluate(TextVisible(kind="text_visible", text="MAINTENANCE"), obs, NONE)


def test_text_visible_ignores_input_values_and_hrefs() -> None:
    obs = members_page()
    obs.find("1:1:2").input_value = MEMBER_ID
    assert not evaluate(TextVisible(kind="text_visible", text=MEMBER_ID), obs, NONE)
    assert not evaluate(TextVisible(kind="text_visible", text="/members"), obs, NONE)


def test_url_matches_over_every_frame_url() -> None:
    obs = member_detail_page()  # top stays "/", only the child frame is at /member
    assert evaluate(UrlMatches(kind="url_matches", pattern=r"/member$"), obs, NONE)
    assert not evaluate(UrlMatches(kind="url_matches", pattern=r"/members$"), obs, NONE)
    assert not evaluate(UrlMatches(kind="url_matches", pattern=r"/settings"), obs, NONE)
    top_only = members_page()
    top_only.frames = {"top": top_only.url}
    assert evaluate(UrlMatches(kind="url_matches", pattern=r"^http://localhost:8000/$"), top_only, NONE)


def test_value_equals_parameter_prefers_input_value_then_text() -> None:
    obs = members_page()
    target = Target(locators=[LabelLocator(kind="label", text="Member ID")])
    cond = ValueEqualsParameter(kind="value_equals_parameter", target=target, param="member_id")
    bindings = Bindings(params={"member_id": MEMBER_ID})
    assert not evaluate(cond, obs, bindings)  # input still empty
    obs.find("1:1:2").input_value = MEMBER_ID
    assert evaluate(cond, obs, bindings)
    assert not evaluate(cond, obs, Bindings(params={"member_id": "99999"}))
    assert not evaluate(cond, obs, NONE)  # unbound parameter is never equal

    detail = member_detail_page()
    structural = Target(locators=[TableCellLocator(kind="table_cell", row_anchor="Member ID", col_offset=1)])
    anchor = ValueEqualsParameter(kind="value_equals_parameter", target=structural, param="member_id")
    assert evaluate(anchor, detail, bindings)  # static cell: falls back to text
    assert not evaluate(anchor, member_detail_page(member_id="54321"), bindings)


def test_value_equals_parameter_is_false_when_target_does_not_resolve() -> None:
    obs = members_page()
    cond = ValueEqualsParameter(
        kind="value_equals_parameter", target=Target(locators=[LabelLocator(kind="label", text="Nope")]), param="member_id"
    )
    assert not evaluate(cond, obs, Bindings(params={"member_id": MEMBER_ID}))


def test_output_present_reads_runtime_outputs_only() -> None:
    obs = member_detail_page()
    cond = OutputPresent(kind="output_present", name="savings_balance")
    assert not evaluate(cond, obs, NONE)  # the balance being on screen is not an output
    assert evaluate(cond, obs, Bindings(outputs={"savings_balance": BALANCE}))


def test_all_condition_is_conjunction_and_nests() -> None:
    obs = member_detail_page()
    bindings = Bindings(params={"member_id": MEMBER_ID}, outputs={"savings_balance": BALANCE})
    success = AllCondition(
        kind="all",
        conditions=[
            TextVisible(kind="text_visible", text="Member Detail"),
            UrlMatches(kind="url_matches", pattern=r"/member$"),
            AllCondition(
                kind="all",
                conditions=[
                    TextAbsent(kind="text_absent", text="No member found"),
                    OutputPresent(kind="output_present", name="savings_balance"),
                    ValueEqualsParameter(
                        kind="value_equals_parameter",
                        target=Target(locators=[TableCellLocator(kind="table_cell", row_anchor="Member ID", col_offset=1)]),
                        param="member_id",
                    ),
                ],
            ),
        ],
    )
    assert evaluate(success, obs, bindings)
    assert not evaluate(success, obs, Bindings(params={"member_id": MEMBER_ID}))  # no output yet
    assert evaluate(AllCondition(kind="all", conditions=[]), obs, NONE)


def test_evaluator_covers_exactly_the_closed_vocabulary() -> None:
    kinds = {"text_visible", "text_absent", "url_matches", "value_equals_parameter", "output_present", "all"}
    schema = TypeAdapter(Condition).json_schema()
    assert set(schema["discriminator"]["mapping"]) == kinds
