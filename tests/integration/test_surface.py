"""PlaywrightSurface + Policy against the live MockBank fixture (docs/spec.md 6, 7, 11).

Every automated action here goes Policy.check -> PolicyDecision -> Surface.act;
the only direct Playwright calls are test-side measurements and the out-of-band
"human" navigation of the iframe to /settings.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from playwright.sync_api import Page

from cua.conditions import Bindings, evaluate
from cua.config import AppConfig, load_app_config
from cua.models import (
    LabelLocator,
    Observation,
    Owner,
    PolicyDecision,
    RoleNameLocator,
    RunControl,
    RuntimeAction,
    RuntimeClick,
    RuntimeFill,
    RuntimeNavigate,
    RuntimeRead,
    TableCellLocator,
    Target,
    TextVisible,
    UrlMatches,
    ValueEqualsParameter,
)
from cua.playwright_surface import PlaywrightSurface
from cua.policy import Policy
from cua.resolver import ResolveFailure, locator_candidates, resolve
from cua.surface import (
    AUTHORIZATION_MISMATCH,
    MISSING_AUTHORIZATION,
    OWNER_NOT_AUTOMATION,
    POLICY_BLOCKED,
    STALE_REF,
)
from mockbank import faults

MEMBER_ID = "12345"
KNOWN_SAVINGS = "$1,234.56"
SAFE_ATTRS = {"id", "name", "type", "aria-label", "aria-labelledby"}
WAIT_TIMEOUT_S = 5.0
POLL_S = 0.25

MEMBER_ID_TARGET = Target(locators=[LabelLocator(kind="label", text="Member ID")])
SEARCH_TARGET = Target(locators=[RoleNameLocator(kind="role_name", role="button", name="Search")])
SAVINGS_TARGET = Target(locators=[TableCellLocator(kind="table_cell", row_anchor="Savings", col_offset=1)])
MEMBER_ID_CELL_TARGET = Target(locators=[TableCellLocator(kind="table_cell", row_anchor="Member ID", col_offset=1)])


@pytest.fixture
def config(mockbank_server) -> AppConfig:
    return load_app_config("mockbank", base_url=mockbank_server.base_url)


@pytest.fixture
def policy(config: AppConfig) -> Policy:
    return Policy(config)


@pytest.fixture
def surface(page: Page) -> PlaywrightSurface:
    return PlaywrightSurface(page)


def act(surface: PlaywrightSurface, policy: Policy, action: RuntimeAction, observation: Observation | None):
    decision = policy.check(action, observation)
    assert decision.allowed, decision.reason
    result = surface.act(action, decision)
    assert result.executed, result.error
    return result


def open_entry(surface: PlaywrightSurface, policy: Policy) -> Observation:
    act(surface, policy, RuntimeNavigate(kind="navigate", url=policy.config.entry_url), None)
    return surface.observe()


def ref_of(target: Target, observation: Observation) -> str:
    ref = resolve(target, observation)
    assert not isinstance(ref, ResolveFailure), ref
    return ref


def wait_for(surface: PlaywrightSurface, ok: Callable[[Observation], bool]) -> Observation:
    deadline = time.monotonic() + WAIT_TIMEOUT_S
    while True:
        observation = surface.observe()
        if ok(observation):
            return observation
        if time.monotonic() >= deadline:
            pytest.fail(f"condition not met within {WAIT_TIMEOUT_S}s; frames={observation.frames}")
        time.sleep(POLL_S)


def search(surface: PlaywrightSurface, policy: Policy, member_id: str) -> Observation:
    obs = open_entry(surface, policy)
    act(surface, policy, RuntimeFill(kind="fill", ref=ref_of(MEMBER_ID_TARGET, obs), value=member_id), obs)
    obs = surface.observe()
    act(surface, policy, RuntimeClick(kind="click", ref=ref_of(SEARCH_TARGET, obs)), obs)
    return wait_for(surface, lambda o: evaluate(UrlMatches(kind="url_matches", pattern=r"/member$"), o, Bindings()))


# --- Observation ---------------------------------------------------------


def test_all_frames_merge_into_one_observation(surface, policy, mockbank_server) -> None:
    obs = open_entry(surface, policy)
    base = mockbank_server.base_url
    assert obs.url == base + "/"
    assert obs.frames == {"top": base + "/", "top/main": base + "/members"}

    by_frame = {path: [c for c in obs.controls if c.frame_path == path] for path in obs.frames}
    top_link = [c for c in by_frame["top"] if c.role == "link"]
    assert [c.name for c in top_link] == ["Members"]
    child_roles = {c.role for c in by_frame["top/main"]}
    assert {"textbox", "button", "cell", "paragraph"} <= child_roles
    member_input = obs.find(ref_of(MEMBER_ID_TARGET, obs))
    assert member_input.frame_path == "top/main"
    assert member_input.attrs == {"name": "member_id", "type": "text"}
    assert member_input.ancestor_roles[-3:] == ["table", "row", "cell"]
    assert "form" in member_input.ancestor_roles
    assert member_input.table_index == 1 and member_input.row_index == 0 and member_input.col_index == 1
    assert len({c.ref for c in obs.controls}) == len(obs.controls)


def test_refs_are_observation_scoped_and_no_dom_ref_attribute(surface, policy, page: Page) -> None:
    first = open_entry(surface, policy)
    search_ref = ref_of(SEARCH_TARGET, first)
    click = RuntimeClick(kind="click", ref=search_ref)
    decision = policy.check(click, first)  # authorized against the Observation the ref came from
    assert decision.allowed
    second = surface.observe()
    assert ref_of(SEARCH_TARGET, second) != search_ref
    assert not ({c.ref for c in first.controls} & {c.ref for c in second.controls})

    stale = surface.act(click, decision)  # the Surface itself refuses a ref from an older Observation
    assert not stale.executed and stale.error == STALE_REF
    # Policy independently refuses a ref that is not in the current Observation.
    assert not policy.check(click, second).allowed
    assert page.frame(name="main").url.endswith("/members")  # nothing was clicked

    for frame in page.frames:
        assert frame.evaluate("() => document.querySelectorAll('[data-cua-ref]').length") == 0
        assert frame.evaluate("() => Array.isArray(window.__cuaRefs) && window.__cuaRefs.length > 0")
        assert frame.evaluate("() => document.documentElement.outerHTML.includes('data-cua')") is False


def test_bbox_is_page_relative(surface, policy, page: Page) -> None:
    obs = open_entry(surface, policy)
    member_input = obs.find(ref_of(MEMBER_ID_TARGET, obs))
    iframe = page.main_frame.evaluate(
        "() => { const e = document.querySelector('iframe[name=main]'); const r = e.getBoundingClientRect();"
        " return {x: r.left + e.clientLeft, y: r.top + e.clientTop}; }"
    )
    inner = page.frame(name="main").evaluate(
        "() => { const r = document.querySelector('input[name=member_id]').getBoundingClientRect();"
        " return {x: r.left, y: r.top, width: r.width, height: r.height}; }"
    )
    assert iframe["x"] > 100  # the menu column sits to the left of the iframe
    assert member_input.bbox.x == pytest.approx(iframe["x"] + inner["x"], abs=0.5)
    assert member_input.bbox.y == pytest.approx(iframe["y"] + inner["y"], abs=0.5)
    assert member_input.bbox.width == pytest.approx(inner["width"], abs=0.5)
    assert member_input.bbox.height == pytest.approx(inner["height"], abs=0.5)

    top_link = next(c for c in obs.controls if c.frame_path == "top" and c.role == "link")
    raw = page.main_frame.evaluate("() => document.querySelector('a').getBoundingClientRect().left")
    assert top_link.bbox.x == pytest.approx(raw, abs=0.5)


def test_href_is_separate_policy_metadata_never_a_safe_attr(surface, policy, mockbank_server) -> None:
    obs = open_entry(surface, policy)
    link = next(c for c in obs.controls if c.role == "link")
    assert link.href == mockbank_server.base_url + "/members"
    assert "href" not in link.attrs
    for c in obs.controls:
        assert set(c.attrs) <= SAFE_ATTRS, c
        assert "data-testid" not in c.attrs
    for candidate in locator_candidates(link, obs):
        assert "href" not in candidate.model_dump() and link.href not in candidate.model_dump_json()


def test_label_derivation_order_and_visibility(surface, page: Page) -> None:
    page.set_content(
        """
        <h1>Form</h1>
        <p>Intro <b>paragraph</b></p>
        <label for="a">For label</label>
        <label id="lb">Labelled by</label>
        <table>
          <tr><td>Left A</td><td><input id="a" name="a" aria-label="Aria A"></td></tr>
          <tr><td>Left B</td><td><label>Wrapping B <input name="b" aria-label="Aria B"></label></td></tr>
          <tr><td>Left C</td><td><input name="c" aria-label="Aria C"></td></tr>
          <tr><td>Left D</td><td><input name="d" aria-labelledby="lb"></td></tr>
          <tr><td>Left E</td><td><input name="e"></td></tr>
          <tr><td>Left F</td><td><input name="f" style="display:none"></td></tr>
          <tr style="display:none"><td>Hidden row</td><td><input name="g"></td></tr>
          <tr><td style="visibility:hidden">Invisible cell</td><td><input name="h" type="hidden"></td></tr>
        </table>
        <input name="z">
        <a href="/x">Link</a><a>Anchor without href</a>
        """
    )
    obs = surface.observe()
    by_name = {c.attrs.get("name"): c for c in obs.controls if c.role == "textbox"}
    assert by_name["a"].label == "For label"  # 1. <label for>
    assert by_name["b"].label == "Wrapping B"  # 2. wrapping <label>
    assert by_name["c"].label == "Aria C"  # 3. aria-label
    assert by_name["d"].label == "Labelled by"  # 3. aria-labelledby
    assert by_name["e"].label == "Left E"  # 4. left-adjacent table cell
    assert by_name["z"].label is None
    assert set(by_name) == {"a", "b", "c", "d", "e", "z"}  # f, g (display:none) and h (hidden) skipped

    texts = {c.text for c in obs.controls if c.role in ("heading", "paragraph", "cell", "text")}
    assert {"Form", "Intro paragraph", "For label", "Labelled by", "Left A", "Left E"} <= texts
    assert "Hidden row" not in texts and "Invisible cell" not in texts
    assert "Wrapping B" not in texts  # the wrapping label holds a control: emitted as the input, not as text
    links = [c for c in obs.controls if c.role == "link"]
    assert len(links) == 1 and links[0].href.endswith("/x") and links[0].attrs == {}
    assert all(c.bbox.width > 0 for c in obs.controls)


# --- Surface.act gate ------------------------------------------------------


def test_surface_act_rejects_missing_denied_mismatched_and_non_automation(surface, policy, page: Page) -> None:
    obs = open_entry(surface, policy)
    fill = RuntimeFill(kind="fill", ref=ref_of(MEMBER_ID_TARGET, obs), value=MEMBER_ID)
    click = RuntimeClick(kind="click", ref=ref_of(SEARCH_TARGET, obs))
    allowed = policy.check(fill, obs)

    assert surface.act(fill, None).error == MISSING_AUTHORIZATION
    assert surface.act(fill, PolicyDecision(allowed=False, reason="denied", action=fill)).error == POLICY_BLOCKED
    assert surface.act(click, allowed).error == AUTHORIZATION_MISMATCH
    surface.run_control.owner = Owner.HUMAN
    assert surface.act(fill, allowed).error == OWNER_NOT_AUTOMATION
    surface.run_control.owner = Owner.NEEDS_HUMAN
    assert surface.act(fill, allowed).error == OWNER_NOT_AUTOMATION
    surface.run_control.owner = Owner.COMPLETED
    assert surface.act(fill, allowed).error == OWNER_NOT_AUTOMATION

    assert page.frame(name="main").evaluate("() => document.querySelector('input[name=member_id]').value") == ""
    assert page.frame(name="main").url.endswith("/members")

    surface.run_control.owner = Owner.AUTOMATION
    assert surface.act(fill, allowed).executed
    after = surface.observe()
    assert after.find(ref_of(MEMBER_ID_TARGET, after)).input_value == MEMBER_ID


def test_shared_run_control_gates_the_surface(page: Page, policy) -> None:
    control = RunControl(owner=Owner.HUMAN)
    surface = PlaywrightSurface(page, run_control=control)
    nav = RuntimeNavigate(kind="navigate", url=policy.config.entry_url)
    assert surface.act(nav, policy.check(nav, None)).error == OWNER_NOT_AUTOMATION
    assert page.url == "about:blank"
    control.owner = Owner.AUTOMATION
    assert surface.act(nav, policy.check(nav, None)).executed


# --- Policy on the live app ------------------------------------------------


def test_settings_frame_is_blocked_by_frame_aware_allowlist(surface, policy, page: Page, mockbank_server) -> None:
    open_entry(surface, policy)
    page.frame(name="main").goto(mockbank_server.base_url + "/settings")  # out-of-band, like a human would
    obs = surface.observe()
    assert obs.url == mockbank_server.base_url + "/"  # top-level URL alone looks harmless
    assert obs.frames["top/main"].endswith("/settings")

    arm = ref_of(Target(locators=[RoleNameLocator(kind="role_name", role="button", name="Arm interstitial=once")]), obs)
    click = RuntimeClick(kind="click", ref=arm)
    decision = policy.check(click, obs)
    assert not decision.allowed and "/settings" in decision.reason
    assert surface.act(click, decision).error == POLICY_BLOCKED
    assert faults.snapshot() == {"interstitial": False, "unknown": False}
    # Even a harmless read is blocked while a denied frame is present.
    assert not policy.check(RuntimeRead(kind="read", ref=obs.controls[0].ref), obs).allowed


def test_close_account_is_blocked_as_irreversible(surface, policy) -> None:
    obs = search(surface, policy, MEMBER_ID)
    close = ref_of(Target(locators=[RoleNameLocator(kind="role_name", role="button", name="Close Account")]), obs)
    click = RuntimeClick(kind="click", ref=close)
    decision = policy.check(click, obs)
    assert not decision.allowed and "irreversible" in decision.reason
    assert surface.act(click, decision).error == POLICY_BLOCKED
    assert evaluate(TextVisible(kind="text_visible", text="Member Detail"), surface.observe(), Bindings())


def test_stale_ref_after_navigation_is_not_guessed_through(surface, policy) -> None:
    obs = open_entry(surface, policy)
    input_ref = ref_of(MEMBER_ID_TARGET, obs)
    search(surface, policy, MEMBER_ID)
    read = RuntimeRead(kind="read", ref=input_ref)
    result = surface.act(read, PolicyDecision(allowed=True, reason="test", action=read))
    assert not result.executed and result.error == STALE_REF


# --- The whole Phase 1 seam on the real UI --------------------------------


def test_fill_search_read_savings_through_policy_and_pure_resolver(surface, policy) -> None:
    obs = open_entry(surface, policy)
    fill = RuntimeFill(kind="fill", ref=ref_of(MEMBER_ID_TARGET, obs), value=MEMBER_ID)
    act(surface, policy, fill, obs)
    obs = surface.observe()
    bindings = Bindings(params={"member_id": MEMBER_ID})
    assert evaluate(
        ValueEqualsParameter(kind="value_equals_parameter", target=MEMBER_ID_TARGET, param="member_id"), obs, bindings
    )

    act(surface, policy, RuntimeClick(kind="click", ref=ref_of(SEARCH_TARGET, obs)), obs)
    obs = wait_for(surface, lambda o: evaluate(TextVisible(kind="text_visible", text="Member Detail"), o, Bindings()))
    assert obs.frames["top/main"].endswith("/member")
    assert MEMBER_ID not in obs.url and MEMBER_ID not in obs.frames["top/main"]

    savings = obs.find(ref_of(SAVINGS_TARGET, obs))
    assert savings.text == KNOWN_SAVINGS and savings.role == "cell"
    result = act(surface, policy, RuntimeRead(kind="read", ref=savings.ref), obs)
    assert result.value == KNOWN_SAVINGS
    assert evaluate(
        ValueEqualsParameter(kind="value_equals_parameter", target=MEMBER_ID_CELL_TARGET, param="member_id"), obs, bindings
    )
    assert not evaluate(
        ValueEqualsParameter(kind="value_equals_parameter", target=MEMBER_ID_CELL_TARGET, param="member_id"),
        obs,
        Bindings(params={"member_id": "54321"}),
    )
    assert [c.model_dump() for c in locator_candidates(savings, obs, [MEMBER_ID, KNOWN_SAVINGS])] == [
        {"kind": "table_cell", "row_anchor": "Savings", "col_offset": 1}
    ]


def test_not_found_is_visible_as_text_and_savings_does_not_resolve(surface, policy) -> None:
    obs = search(surface, policy, "99999")
    assert evaluate(TextVisible(kind="text_visible", text="No member found"), obs, Bindings())
    assert isinstance(resolve(SAVINGS_TARGET, obs), ResolveFailure)


def test_screenshot_returns_png_bytes(surface, policy) -> None:
    open_entry(surface, policy)
    data = surface.screenshot()
    assert isinstance(data, bytes) and data[:8] == b"\x89PNG\r\n\x1a\n"
