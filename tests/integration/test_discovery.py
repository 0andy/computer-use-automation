"""Fake-model Discovery against the live MockBank fixture (docs/spec.md 8, 9, 17.2).

No API key: the model seam is a scripted FakeModel; the Surface, Policy and
MockBank are real. Evidence is written only to tmp_path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from playwright.sync_api import Page

from cua.config import load_app_config
from cua.discovery import Discovery, DiscoveryParam
from cua.evidence import EvidenceWriter
from cua.models import OutputPresent, ValueEqualsParameter
from cua.playwright_surface import PlaywrightSurface
from cua.policy import Policy
from tests.fake_model import FakeModel, click_search, done, fill_member_id, give_up, read_savings
from mockbank import faults

MEMBER_ID = "12345"
KNOWN_SAVINGS = "$1,234.56"
KNOWN_SAVINGS_PARSED = "1234.56"
GOAL = "Look up the member identified by {member_id} and read the current savings balance"


@pytest.fixture
def policy(mockbank_server) -> Policy:
    return Policy(load_app_config("mockbank", base_url=mockbank_server.base_url))


def discover(policy: Policy, page: Page, script, **kw) -> tuple[Discovery, FakeModel]:
    model = FakeModel(script)
    discovery = Discovery(
        config=policy.config,
        policy=policy,
        surface=PlaywrightSurface(page),
        model=model,
        capability_id="lookup_member_balance",
        goal=GOAL,
        params={"member_id": DiscoveryParam(name="member_id", type="string", value=MEMBER_ID)},
        max_steps=12,
        timeout_s=60,
        **kw,
    )
    discovery.run()
    return discovery, model


def test_fake_model_discovery_completes_fixture_task(policy: Policy, page: Page, tmp_path) -> None:
    discovery, model = discover(policy, page, [fill_member_id(), click_search(), read_savings(), done()])
    record = discovery.record

    assert record.stop_reason == "goal_completed"
    assert [a.kind for a in record.actions] == ["fill", "click", "read"]
    fill, click, read = record.actions
    assert isinstance(fill.postcondition, ValueEqualsParameter) and fill.postcondition_verified is True
    assert fill.postcondition.target.locators[0].model_dump() == {"kind": "label", "text": "Member ID"}
    assert click.expect_verified is True and click.post_observation.frames["top/main"].endswith("/member")
    assert read.postcondition == OutputPresent(kind="output_present", name="savings_balance")
    assert record.outputs == {"savings_balance": KNOWN_SAVINGS_PARSED}  # raw runtime return value
    assert record.done_condition.kind == "all"
    assert model.calls == 4 == len(record.model_calls)

    # the model never saw the raw member id, and saw the balance only until it was captured
    shown = model.all_text_shown()
    assert MEMBER_ID not in shown and "[REDACTED:member_id]" in shown
    assert KNOWN_SAVINGS not in model.requests[-1]["messages"][-1]["content"][0]["content"]

    # evidence: sanitized, no transcript, audit metadata only
    out = tmp_path / "discovery"
    writer = EvidenceWriter(out, record.literals)
    writer.write_meta(record.meta())
    writer.write_events(record.events)
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    text = (out / "meta.json").read_text(encoding="utf-8") + (out / "events.jsonl").read_text(encoding="utf-8")
    for literal in (MEMBER_ID, KNOWN_SAVINGS, KNOWN_SAVINGS_PARSED, "1,234.56"):
        assert literal not in text
    assert meta["model_call_count"] == 4 and meta["stop_reason"] == "goal_completed"
    assert meta["message_ids"] == ["msg_fake_1", "msg_fake_2", "msg_fake_3", "msg_fake_4"]
    assert meta["usage"]["input_tokens"] > 0 and meta["usage"]["output_tokens"] > 0
    fill_event = next(e for e in events if e.get("action") == "fill")
    assert fill_event["value"] == {"kind": "parameter", "name": "member_id"}
    read_event = next(e for e in events if e.get("action") == "read")
    assert read_event["value"] is None and read_event["redacted"] is True
    assert "Current observation" not in text


def test_executed_click_with_failed_expect_is_recorded_and_page_change_is_returned(policy: Policy, page: Page) -> None:
    wrong = click_search(expect={"kind": "text_visible", "text": "Search results"})
    discovery, model = discover(policy, page, [fill_member_id(), wrong, read_savings(), done()], settle_timeout_s=1.0)
    record = discovery.record
    click = record.actions[1]
    assert click.result == "executed" and click.expect_verified is False
    assert click.post_observation.frames["top/main"].endswith("/member")
    feedback = model.requests[2]["messages"][-1]["content"][0]["content"]
    assert "did NOT hold" in feedback and "Member Detail" in feedback
    # the run still completes from the actual state
    assert record.stop_reason == "goal_completed" and record.outputs == {"savings_balance": KNOWN_SAVINGS_PARSED}


def test_unknown_interstitial_state_ends_with_give_up_not_handoff(policy: Policy, page: Page) -> None:
    faults.arm("unknown", "once")
    script = [fill_member_id(), click_search(), give_up("blocked_by_unknown_state", "Supervisor override page; no known path.")]
    discovery, _ = discover(policy, page, script, settle_timeout_s=1.0)
    record = discovery.record
    assert record.actions[1].expect_verified is False
    assert "Supervisor override required" in json.dumps(
        [c.text for c in record.actions[1].post_observation.controls]
    )
    assert record.stop_reason == "give_up" and record.give_up_code == "blocked_by_unknown_state"
    assert discovery.surface.run_control.owner.value == "AUTOMATION"


def _cli(args: list[str], mockbank_server, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONUTF8": "1", "MOCKBANK_BASE_URL": mockbank_server.base_url}
    env.pop("ANTHROPIC_API_KEY", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "cua.cli", "discover", *args],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120,
    )


def test_cli_rejects_unknown_placeholder_before_browser_or_model(mockbank_server, tmp_path) -> None:
    out = tmp_path / "ev"
    proc = _cli(
        ["--app", "mockbank", "--capability", "lookup_member_balance",
         "--goal", "Look up {account_no}", "--param", "member_id:string=12345", "--evidence-dir", str(out)],
        mockbank_server,
    )
    assert proc.returncode == 2
    assert "account_no" in proc.stderr and "ANTHROPIC_API_KEY" not in proc.stderr
    assert not out.exists()


def test_cli_rejects_bad_capability_id_and_param(mockbank_server, tmp_path) -> None:
    base = ["--app", "mockbank", "--goal", "Look up {member_id}", "--evidence-dir", str(tmp_path / "ev")]
    proc = _cli([*base, "--capability", "Lookup-Member", "--param", "member_id:string=12345"], mockbank_server)
    assert proc.returncode == 2 and "capability id" in proc.stderr
    proc = _cli([*base, "--capability", "lookup_member_balance", "--param", "member_id=12345"], mockbank_server)
    assert proc.returncode == 2 and "name:type=value" in proc.stderr
    assert not (tmp_path / "ev").exists()


def test_cli_without_api_key_stops_before_browser(mockbank_server, tmp_path) -> None:
    out = tmp_path / "ev"
    proc = _cli(
        ["--app", "mockbank", "--capability", "lookup_member_balance",
         "--goal", GOAL, "--param", "member_id:string=12345", "--evidence-dir", str(out)],
        mockbank_server,
    )
    assert proc.returncode == 2
    assert "ANTHROPIC_API_KEY" in proc.stderr
    assert not out.exists()
