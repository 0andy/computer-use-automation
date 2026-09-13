"""HITL state machine over scripted Observations (docs/spec.md 15): no browser, no console, no model.

The golden artifact is read-only input; tampered variants are built in memory.
A FakeSurface + FakeHumanCapture stand in for the live page; ScriptedOperator
stands in for the human. Settle constants are shrunk via monkeypatch.
"""

from __future__ import annotations

import json
import re
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from cua import replay as replay_module
from cua.cli import replay_session_options
from cua.config import load_app_config
from cua.hitl import (
    ConsoleOperator,
    Intervention,
    OperatorDecision,
    ScriptedOperator,
    ScriptExhausted,
    mask_screenshot,
    parse_decision,
    sensitive_bboxes,
    sensitive_controls,
)
from cua.models import (
    AllCondition,
    BBox,
    CapabilityArtifact,
    ClickAction,
    Observation,
    Owner,
    PolicyDecision,
    RoleNameLocator,
    RunControl,
    RuntimeClick,
    Step,
    Target,
    TextVisible,
)
from cua.policy import Policy
from cua.replay import (
    FINAL_CHECKPOINT_FAILED,
    LOCATOR_NOT_FOUND,
    MAX_RECOVERIES_PER_STEP,
    POLICY_BLOCKED,
    POSTCONDITION_FAILED,
    RECOVERY_EXHAUSTED,
    Replay,
    load_capability,
    write_evidence,
)
from cua.surface import OWNER_NOT_AUTOMATION
from tests.conftest import CONSOLE_INPUT_GUARD
from tests.fake_surface import FakeSurface
from tests.unit.obs import BALANCE, BASE, MEMBER_ID, control, member_detail_page, members_page
from tests.wallclock import assert_no_wall_clock

FAST_TIMEOUT_S = 0.3
FAST_POLL_S = 0.01

MEMBER_INPUT = "1:1:2"
SEARCH = "1:1:3"
MEMBER_ID_CELL = "2:1:2"
SAVINGS_CELL = "2:1:8"
ACKNOWLEDGE = "5:1:2"
CONTINUE = "3:1:2"

OTHER_MEMBER_ID = "54321"
OTHER_BALANCE = "$9.99"


@pytest.fixture(autouse=True)
def fast_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replay_module, "STEP_TIMEOUT_S", FAST_TIMEOUT_S)
    monkeypatch.setattr(replay_module, "POLL_S", FAST_POLL_S)


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch) -> Policy:
    monkeypatch.delenv("MOCKBANK_BASE_URL", raising=False)
    return Policy(load_app_config("mockbank"))


@pytest.fixture
def artifact() -> CapabilityArtifact:
    return load_capability("mockbank", "lookup_member_balance")


def override_page() -> Observation:
    """MockBank 'Supervisor override required' page: unknown to the artifact, so Replay gets stuck."""
    return Observation(
        url=f"{BASE}/",
        frames={"top": f"{BASE}/", "top/main": f"{BASE}/override"},
        controls=[
            control("5:0:0", "link", "top", name="Members", text="Members", href=f"{BASE}/members"),
            control("5:1:0", "cell", name="Supervisor override required", text="Supervisor override required", table_index=0, row_index=0, col_index=0),
            control("5:1:1", "cell", name="This request has been flagged for supervisor review.", text="This request has been flagged for supervisor review.", table_index=1, row_index=0, col_index=0),
            control(ACKNOWLEDGE, "button", name="Acknowledge", attrs={"type": "submit"}, table_index=1, row_index=1, col_index=0),
        ],
    )


class FakeHumanCapture:
    """Records the owner at begin/end and hands back scripted 'captured' events."""

    def __init__(self, run_control: RunControl, events: list[dict] | None = None) -> None:
        self.run_control = run_control
        self.scripted = events or []
        self.owner_at_begin: list[Owner] = []
        self.owner_at_end: list[Owner] = []
        self.active = False

    def begin(self) -> None:
        self.owner_at_begin.append(self.run_control.owner)
        self.active = True

    def end(self) -> list[dict]:
        self.owner_at_end.append(self.run_control.owner)
        self.active = False
        return [dict(e) for e in self.scripted]


class PngSurface(FakeSurface):
    """FakeSurface whose screenshot is a real white PNG (so masking can be checked pixel by pixel)."""

    SIZE = (200, 100)

    def screenshot(self) -> bytes:
        buf = BytesIO()
        Image.new("RGB", self.SIZE, (255, 255, 255)).save(buf, format="PNG")
        return buf.getvalue()


def stuck_surface(after_search: Observation | None = override_page(), cls=FakeSurface) -> tuple[FakeSurface, RunControl]:
    rc = RunControl()
    surface = cls(members_page(), run_control=rc)
    if after_search is not None:
        surface.transitions[SEARCH] = after_search
    return surface, rc


def run(artifact, policy, surface, rc, operator, capture=None, member_id: str = MEMBER_ID, handoff_enabled: bool = True):
    engine = Replay(
        artifact=artifact,
        config=policy.config,
        policy=policy,
        surface=surface,
        params={"member_id": member_id},
        handoff_enabled=handoff_enabled,
        operator=operator,
        human_capture=capture,
        run_control=rc,
    )
    return engine, engine.run()


def clicks_on(surface: FakeSurface, ref: str) -> list[RuntimeClick]:
    return [a for a in surface.acts if isinstance(a, RuntimeClick) and a.ref == ref]


def ownership(engine: Replay) -> list[tuple[str, str]]:
    return [(e["from"], e["to"]) for e in engine.events if e.get("event") == "ownership"]


def settle_events(engine: Replay, step_id: str, phase: str) -> list[dict]:
    return [e for e in engine.events if e.get("event") == "settle" and e["step_id"] == step_id and e["phase"] == phase]


def intervention(code: str = POSTCONDITION_FAILED) -> Intervention:
    return Intervention(
        app="mockbank",
        capability="lookup_member_balance",
        step_id="s2",
        step_description="Click Search",
        phase="after",
        code=code,
        expected=TextVisible(kind="text_visible", text="Savings"),
        observed_summary="url=...; texts=[Supervisor override required]",
        masked_screenshot="masked.png",
        owner=Owner.HUMAN,
    )


# --- RunControl state machine (spec 15.1) ---------------------------------------------


def test_run_control_transitions_follow_the_spec_state_machine() -> None:
    rc = RunControl()
    assert rc.transfer(Owner.NEEDS_HUMAN) is Owner.AUTOMATION
    assert rc.transfer(Owner.HUMAN) is Owner.NEEDS_HUMAN
    assert rc.transfer(Owner.AUTOMATION) is Owner.HUMAN
    assert rc.transfer(Owner.COMPLETED) is Owner.AUTOMATION
    for start, bad in [
        (Owner.AUTOMATION, Owner.HUMAN),
        (Owner.NEEDS_HUMAN, Owner.AUTOMATION),
        (Owner.HUMAN, Owner.COMPLETED),
        (Owner.COMPLETED, Owner.AUTOMATION),
    ]:
        with pytest.raises(ValueError, match="ownership cannot move"):
            RunControl(owner=start).transfer(bad)
    with pytest.raises(ValueError, match="must be AUTOMATION"):
        Replay(
            artifact=load_capability("mockbank", "lookup_member_balance"),
            config=load_app_config("mockbank"),
            policy=Policy(load_app_config("mockbank")),
            surface=FakeSurface(members_page(), run_control=RunControl(owner=Owner.COMPLETED)),
            params={"member_id": MEMBER_ID},
        )


# --- Operators (spec 15.2) --------------------------------------------------------------


def test_console_operator_maps_decisions_and_reprompts_on_junk() -> None:
    def console(answers: list[str]) -> tuple[ConsoleOperator, list[str]]:
        shown: list[str] = []
        it = iter(answers)
        return ConsoleOperator(input_fn=lambda prompt: next(it), output_fn=shown.append), shown

    for answer, expected in [("r", OperatorDecision.RETRY), ("C", OperatorDecision.CONTINUE), ("abort", OperatorDecision.ABORT), (" Retry ", OperatorDecision.RETRY)]:
        operator, shown = console([answer])
        assert operator.take_control(intervention()) is expected
        assert any("HUMAN INTERVENTION REQUIRED" in line and POSTCONDITION_FAILED in line for line in shown)

    operator, shown = console(["x", "", "a"])
    assert operator.take_control(intervention()) is OperatorDecision.ABORT
    assert sum("unrecognized choice" in line for line in shown) == 2

    def eof(prompt: str) -> str:
        raise EOFError

    assert ConsoleOperator(input_fn=eof, output_fn=lambda s: None).take_control(intervention()) is OperatorDecision.ABORT
    assert parse_decision("nope") is None


def test_no_test_can_reach_console_input() -> None:
    """The conftest guard makes builtins.input raise for every test; ConsoleOperator looks it up at call time."""
    operator = ConsoleOperator(output_fn=lambda s: None)
    with pytest.raises(AssertionError, match=re.escape(CONSOLE_INPUT_GUARD)):
        operator.take_control(intervention())


def test_scripted_operator_never_blocks_and_fails_loudly_when_exhausted() -> None:
    operator = ScriptedOperator(["continue", OperatorDecision.RETRY, lambda i: "abort"])
    assert operator.take_control(intervention()) is OperatorDecision.CONTINUE
    assert operator.take_control(intervention()) is OperatorDecision.RETRY
    assert operator.take_control(intervention(LOCATOR_NOT_FOUND)) is OperatorDecision.ABORT
    assert operator.calls == 3 and [i.code for i in operator.interventions][-1] == LOCATOR_NOT_FOUND
    with pytest.raises(ScriptExhausted):
        operator.take_control(intervention())
    with pytest.raises(ValueError):
        ScriptedOperator(["skip"]).take_control(intervention())


# --- handoff eligibility --------------------------------------------------------------------


def test_handoff_disabled_returns_unavailable_headless_and_requires_operator_when_enabled(artifact, policy) -> None:
    surface, rc = stuck_surface()
    engine, result = run(artifact, policy, surface, rc, operator=None, handoff_enabled=False)
    assert result.kind == "failure"
    assert result.failure.code == POSTCONDITION_FAILED and result.failure.escalation == "unavailable_headless"
    assert engine.handoffs == 0 and engine.interventions == [] and rc.owner is Owner.COMPLETED
    assert ownership(engine) == [("AUTOMATION", "COMPLETED")]
    with pytest.raises(ValueError, match="requires an Operator"):
        Replay(artifact=artifact, config=policy.config, policy=policy, surface=FakeSurface(members_page()), params={"member_id": MEMBER_ID}, handoff_enabled=True)


def test_final_checkpoint_failed_never_starts_a_handoff(artifact, policy) -> None:
    impossible = AllCondition(kind="all", conditions=[artifact.success, TextVisible(kind="text_visible", text="Checking balance")])
    tampered = artifact.model_copy(update={"success": impossible})
    surface, rc = stuck_surface(member_detail_page())
    operator = ScriptedOperator(["continue", "continue"])
    engine, result = run(tampered, policy, surface, rc, operator)
    assert result.kind == "failure"
    assert result.failure.code == FINAL_CHECKPOINT_FAILED and result.failure.step_id is None
    assert result.failure.escalation is None
    assert operator.calls == 0 and engine.handoffs == 0 and engine.interventions == []
    assert ownership(engine) == [("AUTOMATION", "COMPLETED")]  # never NEEDS_HUMAN


def test_policy_blocked_never_escalates_even_with_handoff_enabled(artifact, policy) -> None:
    close_step = Step(
        id="s4",
        description="Click Close Account",
        action=ClickAction(kind="click"),
        target=Target(locators=[RoleNameLocator(kind="role_name", role="button", name="Close Account")]),
        postcondition=None,
    )
    tampered = artifact.model_copy(update={"steps": [*artifact.steps, close_step]})
    surface, rc = stuck_surface(member_detail_page())
    operator = ScriptedOperator(["continue"])
    engine, result = run(tampered, policy, surface, rc, operator)
    assert result.kind == "failure"
    assert result.failure.code == POLICY_BLOCKED and result.failure.escalation is None
    assert operator.calls == 0 and engine.handoffs == 0


# --- ownership: no automation while NEEDS_HUMAN/HUMAN; explicit transitions -------------------


def test_no_automation_while_human_owns_and_transitions_are_recorded(artifact, policy) -> None:
    surface, rc = stuck_surface()
    capture = FakeHumanCapture(rc)
    seen: dict[str, object] = {}

    def human(iv: Intervention) -> str:
        seen["owner"] = rc.owner
        seen["capture_active"] = capture.active
        seen["intervention"] = iv
        click = RuntimeClick(kind="click", ref=ACKNOWLEDGE)
        allowed = PolicyDecision(allowed=True, reason="allowed", action=click)
        seen["automation_attempt"] = surface.act(click, allowed)  # an automated action during HUMAN ownership
        surface.observation = member_detail_page()  # the human fixes the state
        return "continue"

    operator = ScriptedOperator([human])
    engine, result = run(artifact, policy, surface, rc, operator, capture)

    assert seen["owner"] is Owner.HUMAN and seen["capture_active"] is True
    assert seen["automation_attempt"].executed is False and seen["automation_attempt"].error == OWNER_NOT_AUTOMATION
    assert not clicks_on(surface, ACKNOWLEDGE) or all(a not in surface.executed for a in clicks_on(surface, ACKNOWLEDGE))
    assert result.kind == "success" and result.outputs == {"savings_balance": "1234.56"}
    assert ownership(engine) == [
        ("AUTOMATION", "NEEDS_HUMAN"),
        ("NEEDS_HUMAN", "HUMAN"),
        ("HUMAN", "AUTOMATION"),
        ("AUTOMATION", "COMPLETED"),
    ]
    assert rc.owner is Owner.COMPLETED
    assert capture.owner_at_begin == [Owner.HUMAN] and capture.owner_at_end == [Owner.HUMAN]
    iv = seen["intervention"]
    assert isinstance(iv, Intervention)
    assert (iv.app, iv.capability, iv.step_id, iv.phase, iv.code) == ("mockbank", "lookup_member_balance", "s2", "after", POSTCONDITION_FAILED)
    assert iv.expected == artifact.steps[1].postcondition and iv.owner is Owner.HUMAN
    assert "Supervisor override required" in iv.observed_summary
    assert engine.interventions[0]["decision"] == "continue" and engine.interventions[0]["attempt"] == 1


# --- Continue / Retry / Abort semantics (spec 15.7) --------------------------------------------


def test_continue_resettles_without_repeating_the_business_action(artifact, policy) -> None:
    surface, rc = stuck_surface()

    def human(iv: Intervention) -> str:
        surface.observation = member_detail_page()  # human acknowledged the override on the live page
        return "continue"

    engine, result = run(artifact, policy, surface, rc, ScriptedOperator([human]))
    assert result.kind == "success" and result.outputs == {"savings_balance": "1234.56"}
    assert result.recovery_count == 0 and result.failure is None
    assert len(clicks_on(surface, SEARCH)) == 1  # Continue never re-clicked Search
    assert [a.kind for a in surface.executed] == ["navigate", "fill", "click", "read"]
    after = settle_events(engine, "s2", "after")
    assert [e["outcome"] for e in after] == ["unresolved", "advance"]  # another settle of the same contract
    assert [e["outcome"] for e in settle_events(engine, "s2", "before")] == ["advance"]  # target was not re-resolved
    order = [e.get("event") or e.get("action") for e in engine.events]
    assert order.index("handoff") < order.index("decision") < order.index("read")
    assert engine.handoffs == 1


def test_retry_re_resolves_and_re_executes_the_action_through_policy(artifact, policy) -> None:
    surface, rc = stuck_surface(after_search=None)  # Search does nothing: postcondition never true
    checks: list[str] = []
    original_check = policy.check

    def counting_check(action, observation):
        checks.append(action.kind)
        return original_check(action, observation)

    policy.check = counting_check  # type: ignore[method-assign]

    def human(iv: Intervention) -> str:
        assert iv.code == POSTCONDITION_FAILED and iv.step_id == "s2"
        surface.transitions[SEARCH] = member_detail_page()  # the human repaired the app; the click will work now
        return "retry"

    engine, result = run(artifact, policy, surface, rc, ScriptedOperator([human]))
    assert result.kind == "success" and result.outputs == {"savings_balance": "1234.56"}
    searches = clicks_on(surface, SEARCH)
    assert len(searches) == 2 and all(a in surface.executed for a in searches)
    assert checks.count("click") == 2  # both executions crossed Policy
    assert [e["outcome"] for e in settle_events(engine, "s2", "before")] == ["advance", "advance"]  # re-resolved
    assert [e["outcome"] for e in settle_events(engine, "s2", "after")] == ["unresolved", "advance"]
    click_events = [e for e in engine.events if e.get("action") == "click" and e.get("step_id") == "s2"]
    assert [e["result"] for e in click_events] == ["executed", "executed"]
    assert [a.kind for a in surface.executed] == ["navigate", "fill", "click", "click", "read"]


def notice_page() -> Observation:
    """MockBank 'System notice' interstitial (the artifact's known recovery: click Continue)."""
    return Observation(
        url=f"{BASE}/",
        frames={"top": f"{BASE}/", "top/main": f"{BASE}/notice"},
        controls=[
            control("3:0:0", "link", "top", name="Members", text="Members", href=f"{BASE}/members"),
            control("3:1:0", "cell", name="System notice", text="System notice", table_index=0, row_index=0, col_index=0),
            control(CONTINUE, "button", name="Continue", attrs={"type": "submit"}, table_index=1, row_index=1, col_index=0),
        ],
    )


def test_retry_resets_the_recovery_budget_as_a_fresh_attempt(artifact, policy) -> None:
    surface, rc = stuck_surface(notice_page())
    surface.transitions[CONTINUE] = notice_page()  # the notice keeps coming back: RECOVERY_EXHAUSTED

    def human(iv: Intervention) -> str:
        assert iv.code == RECOVERY_EXHAUSTED and iv.step_id == "s2"
        surface.observation = members_page()  # the human went back to the search form ...
        surface.transitions[CONTINUE] = member_detail_page()  # ... and repaired the app: Continue works now
        return "retry"

    operator = ScriptedOperator([human])
    engine, result = run(artifact, policy, surface, rc, operator)
    assert result.kind == "success" and operator.calls == 1
    assert result.recovery_count == 3  # 2 in the exhausted attempt + 1 in the fresh attempt after Retry
    assert len(clicks_on(surface, CONTINUE)) == 3 and len(clicks_on(surface, SEARCH)) == 2
    assert [e["outcome"] for e in settle_events(engine, "s2", "after")] == ["unresolved", "advance"]


def test_continue_keeps_the_exhausted_recovery_budget(artifact, policy) -> None:
    surface, rc = stuck_surface(notice_page())
    surface.transitions[CONTINUE] = notice_page()
    operator = ScriptedOperator(["continue", "abort"])  # Continue without fixing anything, then give up
    engine, result = run(artifact, policy, surface, rc, operator)
    assert result.kind == "aborted" and operator.calls == 2
    assert [iv.code for iv in operator.interventions] == [RECOVERY_EXHAUSTED, RECOVERY_EXHAUSTED]
    assert result.recovery_count == MAX_RECOVERIES_PER_STEP  # Continue did not re-fire the exhausted recovery
    assert len(clicks_on(surface, CONTINUE)) == MAX_RECOVERIES_PER_STEP and len(clicks_on(surface, SEARCH)) == 1


def test_retry_after_stale_ref_exhaustion_is_a_step_level_handoff(artifact, policy) -> None:
    surface, rc = stuck_surface(member_detail_page())
    surface.failures[SEARCH] = "STALE_REF"  # ref stale on act and again after the single re-resolve

    def human(iv: Intervention) -> str:
        assert iv.code == LOCATOR_NOT_FOUND and iv.phase == "act"
        del surface.failures[SEARCH]
        return "retry"

    operator = ScriptedOperator([human])
    engine, result = run(artifact, policy, surface, rc, operator)
    assert result.kind == "success" and operator.calls == 1
    assert [e["result"] for e in engine.events if e.get("event") == "stale_ref"] == ["re-observe", "unresolved"]


def test_abort_returns_aborted(artifact, policy, tmp_path: Path) -> None:
    surface, rc = stuck_surface()
    operator = ScriptedOperator(["abort"])
    engine, result = run(artifact, policy, surface, rc, operator)
    assert result.kind == "aborted"
    assert result.failure is None and result.business_outcome is None and result.outputs == {}
    assert result.llm_calls == 0
    assert [a.kind for a in surface.executed] == ["navigate", "fill", "click"]  # nothing after the handoff
    assert any(e.get("event") == "aborted" and e["step_id"] == "s2" and e["code"] == POSTCONDITION_FAILED for e in engine.events)
    assert rc.owner is Owner.COMPLETED
    assert engine.interventions[0]["decision"] == "abort"
    out = write_evidence(engine, result, tmp_path / "replay-aborted")
    persisted = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert persisted["kind"] == "aborted" and persisted["failure"] is None
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["handoff_enabled"] is True and meta["handoffs"] == 1 and meta["final_owner"] == "COMPLETED"
    assert meta["operator"] == "ScriptedOperator" and meta["llm_calls"] == 0


# --- diverged binding (spec 15.7 / 5.11): the compiled parameter anchor catches it ---------------


def test_wrong_member_human_navigation_yields_final_checkpoint_failed(artifact, policy, tmp_path: Path) -> None:
    surface, rc = stuck_surface()

    def human(iv: Intervention) -> str:
        surface.observation = member_detail_page(OTHER_MEMBER_ID, OTHER_BALANCE)  # navigated to another member
        return "continue"

    operator = ScriptedOperator([human])
    engine, result = run(artifact, policy, surface, rc, operator)
    assert result.kind == "failure"
    assert result.failure.code == FINAL_CHECKPOINT_FAILED and result.failure.step_id is None
    assert result.failure.escalation is None
    assert result.failure.expected == artifact.success  # the golden success condition, incl. the member_id anchor
    assert operator.calls == 1 and engine.handoffs == 1  # no second handoff loop
    assert result.outputs == {"savings_balance": "9.99"}  # runtime value of what was actually read
    assert ownership(engine)[-2:] == [("HUMAN", "AUTOMATION"), ("AUTOMATION", "COMPLETED")]

    out = write_evidence(engine, result, tmp_path / "replay-diverged")
    persisted = json.loads((out / "result.json").read_text(encoding="utf-8"))
    summary = persisted["failure"]["observed_summary"]
    assert "[REDACTED:member_id]" in summary and "[REDACTED:savings_balance]" in summary  # structural redaction
    for name in ("result.json", "meta.json", "events.jsonl", "intervention.json", "human-events.jsonl"):
        text = (out / name).read_text(encoding="utf-8")
        for raw in (OTHER_MEMBER_ID, OTHER_BALANCE, "9.99", MEMBER_ID, BALANCE, "1234.56"):
            assert raw not in text, f"{name} leaks {raw!r}"


# --- human events: sanitized, HUMAN-owner only, never a value ------------------------------------


def test_human_events_are_persisted_without_values(artifact, policy, tmp_path: Path) -> None:
    surface, rc = stuck_surface()
    captured = [
        {"source": "page", "kind": "click", "frame": "main", "path": "/override", "control": {"tag": "button", "role": "button", "label": "Acknowledge"}},
        # a hostile/buggy buffer carrying a value must still never be persisted
        {"source": "page", "kind": "input", "frame": "main", "path": "/members", "control": {"tag": "input", "role": "textbox", "name": "member_id", "label": "Member ID"}, "value": "777777"},
        {"source": "page", "kind": "navigation", "frame": "main", "path": "/member", "url": f"{BASE}/member"},
        {"source": "python", "kind": "navigation", "frame": "main", "url": f"{BASE}/member"},
    ]
    capture = FakeHumanCapture(rc, captured)

    def human(iv: Intervention) -> str:
        surface.observation = member_detail_page()
        return "continue"

    engine, result = run(artifact, policy, surface, rc, ScriptedOperator([human]), capture)
    assert result.kind == "success"
    assert [e["kind"] for e in engine.human_events] == ["click", "input", "navigation", "navigation"]
    assert all(e["owner"] == "HUMAN" and e["actor"] == "human" and e["attempt"] == 1 for e in engine.human_events)
    typed = engine.human_events[1]
    assert typed["value"] is None and typed["redacted"] is True and typed["control"]["name"] == "member_id"
    assert "value" not in engine.human_events[0]
    assert engine.interventions[0]["human_events"] == 4

    out = write_evidence(engine, result, tmp_path / "replay-hitl")
    lines = (out / "human-events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4 and "777777" not in "".join(lines)
    assert [json.loads(l)["source"] for l in lines] == ["page", "page", "page", "python"]
    assert json.loads(lines[3])["url"].endswith("/member")
    assert {p.name for p in out.iterdir()} == {"meta.json", "events.jsonl", "result.json", "intervention.json", "human-events.jsonl"}


def test_persisted_evidence_is_deterministic_without_wall_clock_fields(artifact, policy, tmp_path: Path) -> None:
    """Every replay evidence file orders by seq/attempt and times by relative elapsed_ms only."""
    surface, rc = stuck_surface()
    captured = [
        {"source": "page", "kind": "click", "frame": "main", "path": "/override", "control": {"tag": "button", "role": "button", "label": "Acknowledge"}},
        {"source": "python", "kind": "navigation", "frame": "main", "url": f"{BASE}/member"},
    ]

    def human(iv: Intervention) -> str:
        surface.observation = member_detail_page()
        return "continue"

    engine, result = run(artifact, policy, surface, rc, ScriptedOperator([human]), FakeHumanCapture(rc, captured))
    assert result.kind == "success"
    out = write_evidence(engine, result, tmp_path / "replay-hitl")
    assert_no_wall_clock(out, {"meta.json", "events.jsonl", "result.json", "intervention.json", "human-events.jsonl"})

    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert isinstance(meta["elapsed_ms"], int) and meta["elapsed_ms"] >= 0
    assert "started_at" not in meta and "finished_at" not in meta
    events = [json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1)) and not any("at" in e for e in events)
    settles = [e for e in events if e.get("event") == "settle"]
    assert settles and all(isinstance(e["elapsed_ms"], int) for e in settles)
    human_events = [json.loads(line) for line in (out / "human-events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(e["seq"], e["attempt"]) for e in human_events] == [(1, 1), (2, 1)] and not any("at_ms" in e for e in human_events)


# --- screenshot masking (spec 9.4, 5.6): opaque, structural, masked copy only -------------------


def test_mask_covers_sensitive_bboxes_opaque_and_leaves_the_rest() -> None:
    buf = BytesIO()
    Image.new("RGB", (200, 100), (255, 255, 255)).save(buf, format="PNG")
    boxes = [BBox(x=10.4, y=10.6, width=50, height=20), BBox(x=190, y=90, width=30, height=30), BBox(x=5, y=5, width=0, height=8)]
    masked = mask_screenshot(buf.getvalue(), boxes)
    assert masked.size == (200, 100)
    for x, y in [(10, 10), (30, 20), (60, 30), (195, 95), (199, 99)]:
        assert masked.getpixel((x, y)) == (0, 0, 0), (x, y)
    for x, y in [(9, 9), (61, 31), (5, 5), (100, 50), (189, 89)]:
        assert masked.getpixel((x, y)) == (255, 255, 255), (x, y)


def test_sensitive_controls_are_structural_and_literal_based(artifact) -> None:
    literals = {"member_id": [MEMBER_ID]}
    assert sensitive_controls(artifact, members_page(), literals) == {MEMBER_INPUT: "member_id"}
    detail = sensitive_controls(artifact, member_detail_page(), literals)
    assert detail == {MEMBER_ID_CELL: "member_id", SAVINGS_CELL: "savings_balance"}
    # another member's page: the declared structural targets still resolve -> still masked
    other = sensitive_controls(artifact, member_detail_page(OTHER_MEMBER_ID, OTHER_BALANCE), literals)
    assert other == {MEMBER_ID_CELL: "member_id", SAVINGS_CELL: "savings_balance"}
    # a control that merely shows a known literal elsewhere is masked too
    obs = member_detail_page()
    obs.controls.append(control("2:1:11", "paragraph", name=f"Balance {BALANCE}", text=f"Balance {BALANCE}"))
    assert sensitive_controls(artifact, obs, {**literals, "savings_balance": [BALANCE]})["2:1:11"] == "savings_balance"
    assert len(sensitive_bboxes(artifact, obs, literals)) == 2


def test_handoff_persists_only_the_masked_screenshot(artifact, policy, tmp_path: Path) -> None:
    detail = member_detail_page()
    boxes = {MEMBER_ID_CELL: BBox(x=20, y=20, width=40, height=10), SAVINGS_CELL: BBox(x=20, y=60, width=40, height=10)}
    detail.controls = [c.model_copy(update={"bbox": boxes[c.ref]}) if c.ref in boxes else c for c in detail.controls]
    unresolvable = Step(
        id="s2b",
        description="Click Open Checking",
        action=ClickAction(kind="click"),
        target=Target(locators=[RoleNameLocator(kind="role_name", role="button", name="Open Checking")]),
        postcondition=None,
    )
    steps = [*artifact.steps[:2], unresolvable, artifact.steps[2]]
    tampered = artifact.model_copy(update={"steps": steps})  # s2b never resolves on Member Detail
    surface, rc = stuck_surface(detail, cls=PngSurface)
    operator = ScriptedOperator(["abort"])
    engine, result = run(tampered, policy, surface, rc, operator)
    assert result.kind == "aborted"
    assert operator.interventions[0].code == LOCATOR_NOT_FOUND and operator.interventions[0].phase == "before"
    assert operator.interventions[0].masked_screenshot == "masked.png"
    shot = next(e for e in engine.events if e.get("event") == "screenshot")
    assert shot["result"] == "masked" and shot["masked_regions"] == 2

    out = write_evidence(engine, result, tmp_path / "replay-hitl")
    assert [p.name for p in out.iterdir() if p.suffix == ".png"] == ["masked.png"]  # never a raw copy
    image = Image.open(out / "masked.png").convert("RGB")
    assert image.size == PngSurface.SIZE
    for x, y in [(20, 20), (59, 29), (40, 65)]:
        assert image.getpixel((x, y)) == (0, 0, 0)
    for x, y in [(5, 5), (100, 50), (40, 40), (61, 30)]:
        assert image.getpixel((x, y)) == (255, 255, 255)
    assert "[REDACTED:member_id]" in json.loads((out / "intervention.json").read_text(encoding="utf-8"))[0]["observed_summary"]


# --- CLI wiring (spec 15.3) --------------------------------------------------------------------------


def test_headed_flag_sets_headless_false_and_handoff_true() -> None:
    assert replay_session_options(True) == (False, True)
    assert replay_session_options(False) == (True, False)
