"""Policy boundary over hand-built Observations (docs/spec.md 7, 7.1, 7.2)."""

from __future__ import annotations

import pytest

from cua.config import load_app_config
from cua.models import RuntimeClick, RuntimeFill, RuntimeNavigate, RuntimeRead
from cua.policy import Policy
from tests.unit.obs import BASE, control, member_detail_page, members_page


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch) -> Policy:
    monkeypatch.delenv("MOCKBANK_BASE_URL", raising=False)
    return Policy(load_app_config("mockbank"))


def test_startup_navigate_to_entry_url_is_allowed(policy: Policy) -> None:
    nav = RuntimeNavigate(kind="navigate", url=policy.config.entry_url)
    decision = policy.check(nav, None)
    assert decision.allowed and decision.action == nav


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/settings",
        f"{BASE}/settings?x=1",
        f"{BASE}/settings/",
        "http://evil.example/",
        "http://localhost:9000/",
        "https://localhost:8000/",
        f"{BASE}/admin",
        "about:blank",
    ],
)
def test_navigate_outside_allowlist_is_blocked(policy: Policy, url: str) -> None:
    decision = policy.check(RuntimeNavigate(kind="navigate", url=url), None)
    assert not decision.allowed
    assert decision.reason


def test_click_fill_read_allowed_on_members_page(policy: Policy) -> None:
    obs = members_page()
    for action in (
        RuntimeFill(kind="fill", ref="1:1:2", value="12345"),
        RuntimeClick(kind="click", ref="1:1:3"),
        RuntimeRead(kind="read", ref="1:1:1"),
    ):
        decision = policy.check(action, obs)
        assert decision.allowed, decision.reason
        assert decision.action == action


def test_settings_frame_blocks_every_automated_action(policy: Policy) -> None:
    obs = members_page()
    obs.frames["top/main"] = f"{BASE}/settings"  # the top-level URL alone still looks fine
    decision = policy.check(RuntimeClick(kind="click", ref="1:1:3"), obs)
    assert not decision.allowed
    assert "top/main" in decision.reason and "/settings" in decision.reason


def test_any_forbidden_frame_route_blocks(policy: Policy) -> None:
    obs = members_page()
    obs.frames["top/side"] = f"{BASE}/admin"
    assert not policy.check(RuntimeRead(kind="read", ref="1:1:1"), obs).allowed
    obs.frames["top/side"] = "http://other.example/members"
    assert not policy.check(RuntimeRead(kind="read", ref="1:1:1"), obs).allowed
    obs.frames["top/side"] = f"{BASE}/notice"
    assert policy.check(RuntimeRead(kind="read", ref="1:1:1"), obs).allowed


def test_denied_wins_over_allowed(policy: Policy) -> None:
    config = policy.config.model_copy(deep=True)
    config.allowlist.allowed_routes.append("/settings")
    both = Policy(config)
    assert not both.check(RuntimeNavigate(kind="navigate", url=f"{BASE}/settings"), None).allowed


def test_close_account_is_blocked_as_irreversible(policy: Policy) -> None:
    obs = member_detail_page()
    decision = policy.check(RuntimeClick(kind="click", ref="2:1:9"), obs)
    assert not decision.allowed
    assert "Close Account" in decision.reason and "irreversible" in decision.reason
    # reading the same page is fine; only the irreversible control is blocked
    assert policy.check(RuntimeRead(kind="read", ref="2:1:8"), obs).allowed


def test_action_kind_allowlist(policy: Policy) -> None:
    config = policy.config.model_copy(update={"allowed_actions": ["navigate", "click", "read"]})
    no_fill = Policy(config)
    decision = no_fill.check(RuntimeFill(kind="fill", ref="1:1:2", value="x"), members_page())
    assert not decision.allowed and "fill" in decision.reason


def test_unknown_ref_or_missing_observation_is_denied(policy: Policy) -> None:
    assert not policy.check(RuntimeClick(kind="click", ref="9:9:9"), members_page()).allowed
    assert not policy.check(RuntimeClick(kind="click", ref="1:1:3"), None).allowed


def test_link_href_is_preflighted_but_is_not_identity(policy: Policy) -> None:
    obs = member_detail_page()
    assert policy.check(RuntimeClick(kind="click", ref="2:1:10"), obs).allowed  # /members link
    obs.controls.append(control("2:1:20", "link", name="Settings", text="Settings", href=f"{BASE}/settings"))
    decision = policy.check(RuntimeClick(kind="click", ref="2:1:20"), obs)
    assert not decision.allowed and "link preflight" in decision.reason
    obs.controls.append(control("2:1:21", "link", name="Away", text="Away", href="http://evil.example/"))
    assert not policy.check(RuntimeClick(kind="click", ref="2:1:21"), obs).allowed
    # href is only consulted for clicks; reading a link's text is fine
    assert policy.check(RuntimeRead(kind="read", ref="2:1:20"), obs).allowed
