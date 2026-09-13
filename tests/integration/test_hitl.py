"""Same-session HITL handoff against the live MockBank fixture (docs/spec.md 15, 17.4).

Test seam (spec 15.3): a HEADLESS BrowserContext with ``handoff_enabled=True`` and a
ScriptedOperator whose callable entries act on the very same live page the way a
human would. This drives the real RunControl / Continue / Retry / Abort machinery
and the pull-based human event capture without a visible browser. It is not a real
interactive session; that is the manual headed demo (``cua replay ... --headed``).

No API key; evidence is written only to tmp_path; the golden artifact is read-only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import Browser, BrowserContext, Page

from cua import replay as replay_module
from cua.config import load_app_config
from cua.hitl import Intervention, OperatorDecision, ScriptedOperator, sensitive_controls
from cua.models import CapabilityArtifact, Owner, RunControl, RuntimeClick
from cua.playwright_surface import PlaywrightSurface
from cua.policy import Policy
from cua.replay import FINAL_CHECKPOINT_FAILED, LOCATOR_NOT_FOUND, POSTCONDITION_FAILED, Replay, load_capability, write_evidence
from cua.surface import OWNER_NOT_AUTOMATION
from mockbank import faults
from tests.wallclock import assert_no_wall_clock

MEMBER_ID = "12345"
KNOWN_SAVINGS = "$1,234.56"
KNOWN_SAVINGS_PARSED = "1234.56"
RAW_FORMS = (MEMBER_ID, KNOWN_SAVINGS, KNOWN_SAVINGS_PARSED, "1,234.56")

OTHER_MEMBER_ID = "24680"  # second synthetic member (mockbank.app.MEMBERS)
OTHER_SAVINGS = "$88.20"
OTHER_FORMS = (OTHER_MEMBER_ID, OTHER_SAVINGS, "88.20")

HITL_FILES = {"meta.json", "events.jsonl", "result.json", "intervention.json", "human-events.jsonl", "masked.png"}
TEXT_FILES = ("meta.json", "events.jsonl", "result.json", "intervention.json", "human-events.jsonl")

FAST_TIMEOUT_S = 2.0  # the override page renders well within this; keeps the bounded waits short


@pytest.fixture(autouse=True)
def fast_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replay_module, "STEP_TIMEOUT_S", FAST_TIMEOUT_S)


@pytest.fixture
def policy(mockbank_server) -> Policy:
    return Policy(load_app_config("mockbank", base_url=mockbank_server.base_url))


@pytest.fixture
def artifact() -> CapabilityArtifact:
    return load_capability("mockbank", "lookup_member_balance")


class Session:
    """One Replay over one live page with handoff enabled (the test seam)."""

    def __init__(self, policy: Policy, page: Page, artifact: CapabilityArtifact, operator: ScriptedOperator, member_id: str = MEMBER_ID) -> None:
        self.page = page
        self.run_control = RunControl()
        self.surface = PlaywrightSurface(page, run_control=self.run_control)
        self.operator = operator
        self.engine = Replay(
            artifact=artifact,
            config=policy.config,
            policy=policy,
            surface=self.surface,
            params={"member_id": member_id},
            handoff_enabled=True,
            operator=operator,
            human_capture=self.surface.human_events,
            run_control=self.run_control,
        )

    def run(self, out: Path | None = None):
        result = self.engine.run()
        if out is not None:
            write_evidence(self.engine, result, out)
        return result

    @property
    def main(self):
        return self.page.frame(name="main")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def assert_no_raw(out: Path, forms=RAW_FORMS) -> None:
    for name in TEXT_FILES:
        text = (out / name).read_text(encoding="utf-8")
        for raw in forms:
            assert raw not in text, f"{name} leaks {raw!r}"


def ownership(events: list[dict]) -> list[tuple[str, str]]:
    return [(e["from"], e["to"]) for e in events if e.get("event") == "ownership"]


def human_events(out: Path, kind: str | None = None, source: str | None = None) -> list[dict]:
    events = read_jsonl(out / "human-events.jsonl")
    return [e for e in events if (kind is None or e["kind"] == kind) and (source is None or e["source"] == source)]


# --- headed-style stuck run: NEEDS_HUMAN -> HUMAN on the same session -> Continue -> success ------


def test_stuck_replay_hands_off_the_same_session_and_continue_revalidates(
    browser: Browser, context: BrowserContext, page: Page, policy, artifact, tmp_path: Path
) -> None:
    faults.arm(faults.UNKNOWN)  # "Supervisor override required": not a known recovery
    contexts_before = len(browser.contexts)
    seen: dict[str, object] = {}

    def human(iv: Intervention) -> str:
        session_ = seen["session"]
        assert isinstance(session_, Session)
        seen["owner"] = session_.run_control.owner
        seen["page_is_same"] = session_.surface.page is page
        seen["context_is_same"] = page.context is context
        seen["pages"] = list(context.pages)
        seen["contexts"] = len(browser.contexts)
        seen["intervention"] = iv
        assert session_.main.url.endswith("/override")
        # automation is locked out while the human owns the session
        obs = session_.surface.observe()
        ack = next(c for c in obs.controls if c.role == "button" and c.name == "Acknowledge")
        click = RuntimeClick(kind="click", ref=ack.ref)
        seen["automation_attempt"] = session_.surface.act(click, policy.check(click, obs))
        # the human acknowledges in the live page (same page object) and waits for Member Detail
        session_.main.get_by_role("button", name="Acknowledge").click()
        session_.main.wait_for_url("**/member")
        return "continue"

    session = Session(policy, page, artifact, ScriptedOperator([human]))
    seen["session"] = session
    out = tmp_path / "replay-hitl"
    result = session.run(out)

    assert result.kind == "success" and result.outputs == {"savings_balance": KNOWN_SAVINGS_PARSED}
    assert result.failure is None and result.llm_calls == 0 and result.recovery_count == 0
    assert seen["owner"] is Owner.HUMAN and session.run_control.owner is Owner.COMPLETED
    assert seen["page_is_same"] is True and seen["context_is_same"] is True
    assert seen["pages"] == [page] and context.pages == [page] and seen["contexts"] == contexts_before == len(browser.contexts)
    attempt = seen["automation_attempt"]
    assert attempt.executed is False and attempt.error == OWNER_NOT_AUTOMATION

    events = read_jsonl(out / "events.jsonl")
    assert ownership(events) == [("AUTOMATION", "NEEDS_HUMAN"), ("NEEDS_HUMAN", "HUMAN"), ("HUMAN", "AUTOMATION"), ("AUTOMATION", "COMPLETED")]
    search_clicks = [e for e in events if e.get("action") == "click" and e.get("step_id") == "s2"]
    assert len(search_clicks) == 1  # Continue never repeated the business action
    settles = [e for e in events if e.get("event") == "settle" and e["step_id"] == "s2" and e["phase"] == "after"]
    assert [e["outcome"] for e in settles] == ["unresolved", "advance"]
    assert any(e.get("event") == "handoff" and e["code"] == POSTCONDITION_FAILED for e in events)
    assert any(e.get("event") == "decision" and e["decision"] == "continue" for e in events)

    iv = seen["intervention"]
    assert isinstance(iv, Intervention)
    assert (iv.app, iv.capability, iv.step_id, iv.phase, iv.code, iv.owner) == ("mockbank", "lookup_member_balance", "s2", "after", POSTCONDITION_FAILED, Owner.HUMAN)
    assert iv.expected == artifact.steps[1].postcondition and "Supervisor override required" in iv.observed_summary
    persisted = json.loads((out / "intervention.json").read_text(encoding="utf-8"))
    assert len(persisted) == 1 and persisted[0]["decision"] == "continue" and persisted[0]["masked_screenshot"] == "masked.png"
    assert persisted[0]["human_events"] >= 3

    # human evidence: the click recorded in the /override document survived the navigation to /member
    clicks = human_events(out, "click", "page")
    assert [c["control"]["label"] for c in clicks] == ["Acknowledge"] and clicks[0]["path"] == "/override" and clicks[0]["frame"] == "main"
    in_page_nav = human_events(out, "navigation", "page")
    assert in_page_nav and in_page_nav[-1]["url"].endswith("/member")
    python_nav = human_events(out, "navigation", "python")  # independent Playwright framenavigated record
    assert any(n["frame"] == "main" and n["url"].endswith("/member") for n in python_nav)
    assert all(e["owner"] == "HUMAN" and e["actor"] == "human" for e in read_jsonl(out / "human-events.jsonl"))

    assert {p.name for p in out.iterdir()} == HITL_FILES
    assert (out / "masked.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["handoff_enabled"] is True and meta["handoffs"] == 1 and meta["final_owner"] == "COMPLETED" and meta["llm_calls"] == 0
    assert_no_raw(out)
    # deterministic evidence: the in-page recorder and the replay write no wall-clock time (only seq / elapsed_ms)
    assert_no_wall_clock(out, set(TEXT_FILES))
    assert isinstance(meta["elapsed_ms"], int) and meta["elapsed_ms"] >= 0


# --- Retry: re-resolve, Policy, re-execute, settle ---------------------------------------------------


def test_retry_re_executes_the_step_action_through_policy(page: Page, policy, artifact, tmp_path: Path) -> None:
    faults.arm(faults.UNKNOWN)

    def human(iv: Intervention) -> str:
        assert iv.code == POSTCONDITION_FAILED and iv.step_id == "s2"
        page.get_by_role("link", name="Members").click()  # back to the search form (top-frame menu link)
        session.main.wait_for_url("**/members")
        session.main.locator("input[name=member_id]").fill(MEMBER_ID)  # the human re-enters the member id
        return "retry"

    session = Session(policy, page, artifact, ScriptedOperator([human]))
    out = tmp_path / "replay-hitl-retry"
    result = session.run(out)

    assert result.kind == "success" and result.outputs == {"savings_balance": KNOWN_SAVINGS_PARSED}
    events = read_jsonl(out / "events.jsonl")
    search_clicks = [e for e in events if e.get("action") == "click" and e.get("step_id") == "s2"]
    assert [e["result"] for e in search_clicks] == ["executed", "executed"]  # Retry re-executed the action
    befores = [e for e in events if e.get("event") == "settle" and e["step_id"] == "s2" and e["phase"] == "before"]
    assert [e["outcome"] for e in befores] == ["advance", "advance"]  # target re-resolved on the live page
    assert ownership(events)[:3] == [("AUTOMATION", "NEEDS_HUMAN"), ("NEEDS_HUMAN", "HUMAN"), ("HUMAN", "AUTOMATION")]
    assert [c["control"]["label"] for c in human_events(out, "click", "page")] == ["Members"]
    typed = human_events(out, "input", "page")
    assert typed and typed[0]["control"]["name"] == "member_id" and typed[0]["value"] is None and typed[0]["redacted"] is True
    assert any(n["url"].endswith("/members") for n in human_events(out, "navigation", "python"))
    assert_no_raw(out)


# --- Abort ------------------------------------------------------------------------------------------


def test_abort_returns_aborted_and_leaves_the_page_alone(page: Page, policy, artifact, tmp_path: Path) -> None:
    faults.arm(faults.UNKNOWN)
    session = Session(policy, page, artifact, ScriptedOperator([OperatorDecision.ABORT]))
    out = tmp_path / "replay-hitl-abort"
    result = session.run(out)
    assert result.kind == "aborted" and result.failure is None and result.outputs == {}
    assert session.main.url.endswith("/override")  # not guessed through
    persisted = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert persisted["kind"] == "aborted" and persisted["failure"] is None
    assert json.loads((out / "intervention.json").read_text(encoding="utf-8"))[0]["decision"] == "abort"
    assert ownership(read_jsonl(out / "events.jsonl")) == [("AUTOMATION", "NEEDS_HUMAN"), ("NEEDS_HUMAN", "HUMAN"), ("HUMAN", "AUTOMATION"), ("AUTOMATION", "COMPLETED")]


# --- human input values never persist ----------------------------------------------------------------


def test_human_input_values_never_persist(page: Page, policy, artifact, tmp_path: Path) -> None:
    faults.arm(faults.UNKNOWN)
    secret = "777777"  # not a known runtime literal: only structural non-capture can keep it out

    def human(iv: Intervention) -> str:
        page.get_by_role("link", name="Members").click()
        session.main.wait_for_url("**/members")
        box = session.main.locator("input[name=member_id]")
        box.type(secret)
        box.press("Tab")  # change event
        return "abort"

    session = Session(policy, page, artifact, ScriptedOperator([human]))
    out = tmp_path / "replay-hitl-input"
    assert session.run(out).kind == "aborted"
    typed = human_events(out, "input")
    assert len(typed) == 1  # keystroke burst coalesced into one occurrence
    assert typed[0]["control"] == {"tag": "input", "role": "textbox", "name": "member_id", "label": "Member ID"}
    assert typed[0]["value"] is None and typed[0]["redacted"] is True
    changed = human_events(out, "change")
    assert changed and changed[0]["value"] is None and changed[0]["redacted"] is True
    for name in TEXT_FILES:
        assert secret not in (out / name).read_text(encoding="utf-8"), name
    assert_no_raw(out)


# --- masked screenshot: opaque over known sensitive regions, raw never written ------------------------


def test_masked_screenshot_covers_sensitive_regions(page: Page, policy, artifact, tmp_path: Path) -> None:
    raw = json.loads(artifact.model_dump_json())
    unresolvable = {
        "id": "s2b",
        "description": "Click Open Checking",
        "action": {"kind": "click"},
        "target": {"locators": [{"kind": "role_name", "role": "button", "name": "Open Checking"}]},
        "postcondition": None,
    }
    raw["steps"].insert(2, unresolvable)  # never resolves -> stuck (LOCATOR_NOT_FOUND) on Member Detail
    tampered = CapabilityArtifact.model_validate(raw)
    seen: dict[str, object] = {}

    def human(iv: Intervention) -> str:
        assert iv.code == LOCATOR_NOT_FOUND and iv.step_id == "s2b" and iv.phase == "before"
        obs = session.surface.observe()
        refs = sensitive_controls(tampered, obs, session.engine.literals)
        seen["boxes"] = [c.bbox for c in obs.controls if c.ref in refs]
        seen["names"] = sorted(refs.values())
        seen["raw"] = Image.open(__import__("io").BytesIO(page.screenshot(type="png"))).convert("RGB")  # test-only reference
        header = next(c for c in obs.controls if c.text == "Member Detail")
        seen["header"] = header.bbox
        return "abort"

    session = Session(policy, page, tampered, ScriptedOperator([human]))
    out = tmp_path / "replay-hitl-mask"
    assert session.run(out).kind == "aborted"
    assert seen["names"] == ["member_id", "savings_balance"]
    boxes = seen["boxes"]
    assert len(boxes) == 2 and all(b.width > 0 and b.height > 0 for b in boxes)

    assert [p.name for p in out.iterdir() if p.suffix == ".png"] == ["masked.png"]
    masked = Image.open(out / "masked.png").convert("RGB")
    reference = seen["raw"]
    assert masked.size == reference.size
    for box in boxes:
        for x in range(int(box.x), int(box.x + box.width)):
            for y in range(int(box.y), int(box.y + box.height)):
                assert masked.getpixel((x, y)) == (0, 0, 0), (x, y)
    header = seen["header"]
    hx, hy = int(header.x + header.width / 2), int(header.y + header.height / 2)
    assert masked.getpixel((hx, hy)) == reference.getpixel((hx, hy))  # untouched outside the masks
    assert "[REDACTED:member_id]" in json.loads((out / "intervention.json").read_text(encoding="utf-8"))[0]["observed_summary"]
    assert_no_raw(out)


# --- wrong-member human navigation -> FINAL_CHECKPOINT_FAILED, no second handoff ---------------------


def test_wrong_member_human_navigation_yields_final_checkpoint_failed(page: Page, policy, artifact, tmp_path: Path) -> None:
    faults.arm(faults.UNKNOWN)

    def human(iv: Intervention) -> str:
        page.get_by_role("link", name="Members").click()
        session.main.wait_for_url("**/members")
        session.main.locator("input[name=member_id]").fill(OTHER_MEMBER_ID)  # a different member
        session.main.get_by_role("button", name="Search").click()
        session.main.wait_for_url("**/member")
        assert "Savings" in session.main.content()
        return "continue"

    operator = ScriptedOperator([human])
    session = Session(policy, page, artifact, operator)
    out = tmp_path / "replay-hitl-diverged"
    result = session.run(out)

    assert result.kind == "failure"
    assert result.failure.code == FINAL_CHECKPOINT_FAILED and result.failure.step_id is None
    assert result.failure.escalation is None
    assert result.failure.expected == artifact.success  # the compiled member_id anchor is what fails
    assert operator.calls == 1 and session.engine.handoffs == 1  # no second handoff loop
    assert result.outputs == {"savings_balance": "88.20"}  # what was actually read, raw, at runtime only
    events = read_jsonl(out / "events.jsonl")
    assert ownership(events) == [("AUTOMATION", "NEEDS_HUMAN"), ("NEEDS_HUMAN", "HUMAN"), ("HUMAN", "AUTOMATION"), ("AUTOMATION", "COMPLETED")]
    assert [e["outcome"] for e in events if e.get("event") == "settle" and e["phase"] == "final"] == ["unresolved"]
    persisted = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert persisted["failure"]["code"] == FINAL_CHECKPOINT_FAILED and persisted["failure"]["escalation"] is None
    assert "[REDACTED:member_id]" in persisted["failure"]["observed_summary"]
    assert persisted["outputs"] == {"savings_balance": {"redacted": True, "type": "money"}}
    assert_no_raw(out)
    assert_no_raw(out, OTHER_FORMS)  # the other member's values are unknown literals: kept out structurally


# --- normal headless CLI: handoff disabled -> unavailable_headless -------------------------------------


def test_cli_headless_stuck_run_returns_unavailable_headless(mockbank_server, tmp_path: Path) -> None:
    faults.arm(faults.UNKNOWN)  # the CLI subprocess talks to this process's server
    out = tmp_path / "cli-stuck"
    env = {**os.environ, "MOCKBANK_BASE_URL": mockbank_server.base_url, "PYTHONUTF8": "1"}
    env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.run(
        [
            sys.executable, "-m", "cua.cli", "replay",
            "--app", "mockbank", "--capability", "lookup_member_balance",
            "--param", f"member_id={MEMBER_ID}", "--evidence-dir", str(out),
        ],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 1, proc.stderr
    assert "kind: failure" in proc.stdout and '"escalation": "unavailable_headless"' in proc.stdout
    assert "handoffs: 0  owner: COMPLETED" in proc.stdout
    assert {p.name for p in out.iterdir()} == {"meta.json", "events.jsonl", "result.json"}  # no HITL files
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["handoff_enabled"] is False and meta["headed"] is False and meta["operator"] is None
    assert faults.snapshot()[faults.UNKNOWN] is False
