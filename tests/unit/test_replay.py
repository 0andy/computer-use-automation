"""Replay engine contracts over scripted Observations (docs/spec.md 14): no browser, no model.

The golden artifact is read-only input. Tampered variants are built in memory.
Settle constants are shrunk via monkeypatch so bounded waits stay fast; they
are still the module constants the engine reads.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cua import replay as replay_module
from cua.config import load_app_config
from cua.models import (
    AllCondition,
    CapabilityArtifact,
    ClickAction,
    Control,
    Observation,
    ReplayResult,
    RoleNameLocator,
    RuntimeClick,
    Step,
    Target,
    TextVisible,
)
from cua.policy import Policy
from cua.replay import (
    FINAL_CHECKPOINT_FAILED,
    HANDOFF_ELIGIBLE,
    LOCATOR_NOT_FOUND,
    MAX_RECOVERIES_PER_STEP,
    POLICY_BLOCKED,
    POSTCONDITION_FAILED,
    RECOVERY_EXHAUSTED,
    RecoveryBudget,
    Replay,
    ReplayInputError,
    Unresolved,
    bind_params,
    in_flight,
    load_capability,
    parse_replay_params,
    write_evidence,
)
from tests.fake_surface import FakeSurface
from tests.unit.obs import BALANCE, BASE, MEMBER_ID, control, member_detail_page, members_page

FAST_TIMEOUT_S = 0.3
FAST_POLL_S = 0.01

MEMBER_INPUT = "1:1:2"
SEARCH = "1:1:3"
SAVINGS_CELL = "2:1:8"
CLOSE_ACCOUNT = "2:1:9"
CONTINUE = "3:1:2"


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


def notice_page(aux_frame_url: str | None = None) -> Observation:
    """MockBank 'System notice' interstitial; optionally with an extra frame (in-flight tests)."""
    frames = {"top": f"{BASE}/", "top/main": f"{BASE}/notice"}
    if aux_frame_url is not None:
        frames["top/aux"] = aux_frame_url
    return Observation(
        url=f"{BASE}/",
        frames=frames,
        controls=[
            control("3:0:0", "link", "top", name="Members", text="Members", href=f"{BASE}/members"),
            control("3:1:0", "cell", name="System notice", text="System notice", table_index=0, row_index=0, col_index=0),
            control("3:1:1", "cell", name="Scheduled maintenance is in progress.", text="Scheduled maintenance is in progress.", table_index=1, row_index=0, col_index=0),
            control(CONTINUE, "button", name="Continue", attrs={"type": "submit"}, table_index=1, row_index=1, col_index=0),
        ],
    )


def notfound_page() -> Observation:
    return Observation(
        url=f"{BASE}/",
        frames={"top": f"{BASE}/", "top/main": f"{BASE}/member"},
        controls=[
            control("4:0:0", "link", "top", name="Members", text="Members", href=f"{BASE}/members"),
            control("4:1:0", "cell", name="Member Detail", text="Member Detail", table_index=0, row_index=0, col_index=0),
            control("4:1:1", "paragraph", name="No member found.", text="No member found."),
            control("4:1:2", "link", name="New search", text="New search", href=f"{BASE}/members"),
        ],
    )


def surface_for(after_search: Observation) -> FakeSurface:
    surface = FakeSurface(members_page())
    surface.transitions[SEARCH] = after_search
    return surface


def run(artifact: CapabilityArtifact, policy: Policy, surface: FakeSurface, member_id: str = MEMBER_ID, **kw) -> tuple[Replay, ReplayResult]:
    engine = Replay(artifact=artifact, config=policy.config, policy=policy, surface=surface, params={"member_id": member_id}, **kw)
    return engine, engine.run()


def clicks_on(surface: FakeSurface, ref: str) -> list[RuntimeClick]:
    return [a for a in surface.acts if isinstance(a, RuntimeClick) and a.ref == ref]


def click_events(engine: Replay, name: str) -> list[dict]:
    return [e for e in engine.events if e.get("action") == "click" and (e.get("control") or {}).get("name") == name]


# --- exact lookup + params ---------------------------------------------------------


def test_catalog_resolves_exact_app_capability(tmp_path: Path) -> None:
    golden = load_capability("mockbank", "lookup_member_balance")
    assert (golden.app, golden.capability_id, golden.revision) == ("mockbank", "lookup_member_balance", 1)
    with pytest.raises(ReplayInputError, match="no capability"):
        load_capability("mockbank", "lookup_member_balanc")
    with pytest.raises(ReplayInputError, match="no capability"):
        load_capability("otherbank", "lookup_member_balance")
    with pytest.raises(ReplayInputError):
        load_capability("mockbank", "Lookup Member Balance")

    # a file at the exact path must declare the same identity
    mismatched = golden.model_copy(update={"capability_id": "something_else"})
    path = tmp_path / "mockbank" / "lookup_member_balance.json"
    path.parent.mkdir(parents=True)
    path.write_text(mismatched.model_dump_json(), encoding="utf-8")
    with pytest.raises(ReplayInputError, match="declares"):
        load_capability("mockbank", "lookup_member_balance", tmp_path)


def test_params_validate(artifact: CapabilityArtifact) -> None:
    assert parse_replay_params(["member_id=12345"]) == {"member_id": "12345"}
    assert parse_replay_params(["member_id=a=b"]) == {"member_id": "a=b"}
    for bad in (["member_id"], ["Member-Id=1"], ["member_id=1", "member_id=2"]):
        with pytest.raises(ReplayInputError):
            parse_replay_params(bad)
    assert bind_params(artifact, {"member_id": "12345"}) == {"member_id": "12345"}
    with pytest.raises(ReplayInputError, match="missing required"):
        bind_params(artifact, {})
    with pytest.raises(ReplayInputError, match="undeclared"):
        bind_params(artifact, {"member_id": "1", "account": "x"})
    with pytest.raises(ReplayInputError, match="empty"):
        bind_params(artifact, {"member_id": ""})


# --- the happy path and the business outcome ---------------------------------------


def test_success_returns_raw_runtime_balance_and_persists_redacted(artifact, policy, tmp_path: Path) -> None:
    surface = surface_for(member_detail_page())
    engine, result = run(artifact, policy, surface)

    assert result.kind == "success"
    assert result.outputs == {"savings_balance": "1234.56"}  # raw, for the caller
    assert result.business_outcome is None and result.failure is None
    assert result.llm_calls == 0 and result.recovery_count == 0
    assert [a.kind for a in surface.executed] == ["navigate", "fill", "click", "read"]
    assert surface.executed[1].value == MEMBER_ID  # raw binding reaches the surface only

    out = write_evidence(engine, result, tmp_path / "replay-success")
    persisted = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert persisted["outputs"] == {"savings_balance": {"redacted": True, "type": "money"}}
    assert persisted["kind"] == "success" and persisted["llm_calls"] == 0
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["llm_calls"] == 0 and meta["model_call_count"] == 0 and meta["model"] is None
    assert meta["params"] == {"member_id": {"type": "string", "sensitive": True}}
    for name in ("result.json", "meta.json", "events.jsonl"):
        text = (out / name).read_text(encoding="utf-8")
        assert MEMBER_ID not in text and BALANCE not in text and "1234.56" not in text
    events = [json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    fill = next(e for e in events if e.get("action") == "fill")
    assert fill["value"] == {"kind": "parameter", "name": "member_id"}
    read = next(e for e in events if e.get("action") == "read")
    assert read["value"] is None and read["redacted"] is True and read["output_captured"] is True


def test_99999_is_business_outcome_not_failure(artifact, policy) -> None:
    engine, result = run(artifact, policy, surface_for(notfound_page()), member_id="99999")
    assert result.kind == "business_outcome"
    assert result.business_outcome == "MEMBER_NOT_FOUND"
    assert result.failure is None and result.outputs == {}
    assert [a.kind for a in surface_acts(engine)] == ["navigate", "fill", "click"]  # no read attempted
    assert any(e.get("event") == "business_outcome" and e["code"] == "MEMBER_NOT_FOUND" for e in engine.events)


def surface_acts(engine: Replay):
    return engine.surface.executed  # type: ignore[attr-defined]


# --- resolver exact-only / bounded polling ---------------------------------------


def test_ambiguous_locator_never_drives_an_action(artifact, policy) -> None:
    start = members_page()
    twin = start.find(SEARCH).model_copy(update={"ref": "1:1:9"})
    start.controls.append(twin)  # two exact "Search" buttons -> zero-or-one rule fails
    surface = FakeSurface(start)
    engine, result = run(artifact, policy, surface)
    assert result.kind == "failure"
    assert result.failure.code == LOCATOR_NOT_FOUND and result.failure.step_id == "s2"
    assert result.failure.escalation == "unavailable_headless"
    assert not clicks_on(surface, SEARCH) and not clicks_on(surface, "1:1:9")


def test_bounded_polling_until_deadline(artifact, policy) -> None:
    surface = FakeSurface(members_page())  # Search click changes nothing: postcondition never true
    before = surface.observe_count
    started = time.monotonic()
    engine, result = run(artifact, policy, surface)
    elapsed = time.monotonic() - started
    assert result.kind == "failure"
    assert result.failure.code == POSTCONDITION_FAILED and result.failure.step_id == "s2"
    assert result.failure.expected == TextVisible(kind="text_visible", text="Savings")
    assert result.failure.escalation == "unavailable_headless"
    assert FAST_TIMEOUT_S <= elapsed < FAST_TIMEOUT_S * 4
    settle = next(e for e in engine.events if e.get("event") == "settle" and e["step_id"] == "s2" and e["phase"] == "after")
    assert settle["outcome"] == "unresolved" and settle["polls"] > 3
    assert surface.observe_count - before >= settle["polls"]
    assert len(clicks_on(surface, SEARCH)) == 1  # never re-clicked while waiting


# --- stale ref: exactly one re-observe + re-resolve -----------------------------------


def test_stale_ref_gets_exactly_one_reobserve_and_reresolve(artifact, policy) -> None:
    surface = surface_for(member_detail_page())
    surface.stale_once.add(SEARCH)
    engine, result = run(artifact, policy, surface)
    assert result.kind == "success"
    search_acts = clicks_on(surface, SEARCH)
    assert len(search_acts) == 2  # first stale, second executed
    assert len([a for a in surface.executed if isinstance(a, RuntimeClick)]) == 1
    stale = [e for e in engine.events if e.get("event") == "stale_ref"]
    assert [e["result"] for e in stale] == ["re-observe"]


def test_stale_ref_twice_is_unresolved_without_further_retry(artifact, policy) -> None:
    surface = surface_for(member_detail_page())
    surface.failures[SEARCH] = "STALE_REF"
    observes_before = surface.observe_count
    engine, result = run(artifact, policy, surface)
    assert result.kind == "failure"
    assert result.failure.code == LOCATOR_NOT_FOUND and result.failure.step_id == "s2"
    assert len(clicks_on(surface, SEARCH)) == 2  # act, one retry, stop
    assert [e["result"] for e in engine.events if e.get("event") == "stale_ref"] == ["re-observe", "unresolved"]


# --- recovery: re-assess, never redo Search; bounded ------------------------------


def test_interstitial_recovery_does_not_redo_search(artifact, policy) -> None:
    surface = surface_for(notice_page())
    surface.transitions[CONTINUE] = member_detail_page()
    engine, result = run(artifact, policy, surface)
    assert result.kind == "success" and result.outputs == {"savings_balance": "1234.56"}
    assert result.recovery_count == 1
    assert len(clicks_on(surface, SEARCH)) == 1 and len(clicks_on(surface, CONTINUE)) == 1
    assert len(click_events(engine, "Search")) == 1
    recoveries = [e for e in engine.events if e.get("event") == "recovery"]
    assert len(recoveries) == 1 and recoveries[0]["code"] == "DISMISS_SYSTEM_NOTICE"
    assert recoveries[0]["result"] == "executed" and recoveries[0]["step_id"] == "s2"
    kinds = [a.kind for a in surface.executed]
    assert kinds == ["navigate", "fill", "click", "click", "read"]


def test_recovery_is_bounded_by_max_recoveries_per_step(artifact, policy) -> None:
    surface = surface_for(notice_page())
    surface.transitions[CONTINUE] = notice_page()  # the notice keeps coming back
    engine, result = run(artifact, policy, surface)
    assert result.kind == "failure"
    assert result.failure.code == RECOVERY_EXHAUSTED and result.failure.step_id == "s2"
    assert result.failure.escalation == "unavailable_headless"
    assert result.recovery_count == MAX_RECOVERIES_PER_STEP == 2
    assert len(clicks_on(surface, CONTINUE)) == MAX_RECOVERIES_PER_STEP
    assert len(clicks_on(surface, SEARCH)) == 1
    assert [e["phase"] for e in engine.events if e.get("event") == "recovery"] == ["after", "after"]


def test_recovery_budget_is_shared_by_the_settle_calls_of_one_step(artifact, policy) -> None:
    """MAX_RECOVERIES_PER_STEP is per step attempt: two settle calls with one budget spend it once."""
    surface = FakeSurface(notice_page())
    surface.transitions[CONTINUE] = notice_page()
    engine = Replay(artifact=artifact, config=policy.config, policy=policy, surface=surface, params={"member_id": MEMBER_ID})
    step = artifact.steps[1]
    never = lambda obs: False  # noqa: E731

    budget = RecoveryBudget()
    first = engine.settle(step, never, POSTCONDITION_FAILED, phase="before", budget=budget)
    assert first == Unresolved(RECOVERY_EXHAUSTED) and budget.used == MAX_RECOVERIES_PER_STEP
    assert len(clicks_on(surface, CONTINUE)) == MAX_RECOVERIES_PER_STEP

    second = engine.settle(step, never, POSTCONDITION_FAILED, phase="after", budget=budget)  # same attempt
    assert second == Unresolved(RECOVERY_EXHAUSTED)
    assert len(clicks_on(surface, CONTINUE)) == MAX_RECOVERIES_PER_STEP  # nothing more was clicked

    third = engine.settle(step, never, POSTCONDITION_FAILED, phase="before", budget=RecoveryBudget())  # fresh attempt
    assert third == Unresolved(RECOVERY_EXHAUSTED)
    assert len(clicks_on(surface, CONTINUE)) == 2 * MAX_RECOVERIES_PER_STEP


# --- transient in-flight frame is "not settled yet", never POLICY_BLOCKED ---------------


def test_in_flight_frame_is_polled_not_policy_blocked(artifact, policy) -> None:
    transient = notice_page(aux_frame_url="about:blank")
    assert in_flight(transient) and not in_flight(notice_page())
    assert not policy.check(RuntimeClick(kind="click", ref=CONTINUE), transient).allowed  # Policy would deny it

    class TransientSurface(FakeSurface):
        """After the Search click, three mid-navigation observations precede the settled notice."""

        def act(self, action, authorization):
            result = super().act(action, authorization)
            if isinstance(action, RuntimeClick) and action.ref == SEARCH and result.executed:
                self.queue = [transient, transient, transient]
            return result

    surface = TransientSurface(members_page())
    surface.transitions[SEARCH] = notice_page()
    surface.transitions[CONTINUE] = member_detail_page()
    engine, result = run(artifact, policy, surface)
    assert result.kind == "success", result
    assert result.failure is None
    assert all(e.get("failure_code") != POLICY_BLOCKED for e in engine.events)
    settle = next(e for e in engine.events if e.get("event") == "settle" and e["step_id"] == "s2" and e["phase"] == "after")
    assert settle["in_flight_polls"] >= 3 and settle["outcome"] == "advance" and settle["recoveries"] == 1
    assert len(clicks_on(surface, SEARCH)) == 1


def test_frame_that_never_settles_times_out_as_unresolved_not_policy_blocked(artifact, policy) -> None:
    surface = surface_for(notice_page(aux_frame_url="about:blank"))
    engine, result = run(artifact, policy, surface)
    assert result.kind == "failure"
    assert result.failure.code == POSTCONDITION_FAILED and result.failure.code != POLICY_BLOCKED
    assert result.failure.escalation == "unavailable_headless"
    assert not clicks_on(surface, CONTINUE)  # no action was attempted against an in-flight page
    settle = next(e for e in engine.events if e.get("event") == "settle" and e["step_id"] == "s2" and e["phase"] == "after")
    assert settle["in_flight_polls"] == settle["polls"] > 1


# --- structured direct failures --------------------------------------------------


def test_policy_blocked_is_direct_failure_never_escalated(artifact, policy) -> None:
    close_step = Step(
        id="s4",
        description="Click Close Account",
        action=ClickAction(kind="click"),
        target=Target(locators=[RoleNameLocator(kind="role_name", role="button", name="Close Account")]),
        postcondition=None,
    )
    tampered = artifact.model_copy(update={"steps": [*artifact.steps, close_step]})
    surface = surface_for(member_detail_page())
    engine, result = run(tampered, policy, surface)
    assert result.kind == "failure"
    assert result.failure.code == POLICY_BLOCKED and result.failure.step_id == "s4"
    assert result.failure.escalation is None and POLICY_BLOCKED not in HANDOFF_ELIGIBLE
    assert "irreversible" in result.failure.observed_summary
    assert not clicks_on(surface, CLOSE_ACCOUNT)  # rejected before Surface.act
    assert result.outputs == {"savings_balance": "1234.56"}  # what was captured before the block


def test_final_checkpoint_failed_is_structured(artifact, policy) -> None:
    tampered = artifact.model_copy(
        update={
            "success": AllCondition(
                kind="all", conditions=[artifact.success, TextVisible(kind="text_visible", text="Checking balance")]
            )
        }
    )
    engine, result = run(tampered, policy, surface_for(member_detail_page()))
    assert result.kind == "failure"
    assert result.failure.step_id is None
    assert result.failure.code == FINAL_CHECKPOINT_FAILED
    assert result.failure.expected == tampered.success
    assert result.failure.escalation is None
    assert "Member Detail" in result.failure.observed_summary
    persisted = engine.persisted_result(result)
    assert "[REDACTED:member_id]" in persisted["failure"]["observed_summary"]
    assert MEMBER_ID not in persisted["failure"]["observed_summary"]
    assert "[REDACTED:savings_balance]" in persisted["failure"]["observed_summary"]


def test_result_kinds_are_exactly_four() -> None:
    kinds = ReplayResult.model_fields["kind"].annotation.__args__
    assert set(kinds) == {"success", "business_outcome", "failure", "aborted"}
    assert ReplayResult.model_fields["llm_calls"].annotation.__args__ == (0,)
