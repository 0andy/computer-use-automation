"""Deterministic Replay of the committed golden artifact against the live MockBank fixture
(docs/spec.md 14, 16, 17.4). No API key; a guard proves no model is ever touched.

Evidence is written only to tmp_path. The golden artifact is read-only input; the
hard-failure and policy tests use tampered copies written to tmp_path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from playwright.sync_api import Page

from cua.compiler import CAPABILITIES_DIR
from cua.config import load_app_config
from cua.models import CapabilityArtifact
from cua.playwright_surface import PlaywrightSurface
from cua.policy import Policy
from cua.replay import (
    FINAL_CHECKPOINT_FAILED,
    POLICY_BLOCKED,
    POSTCONDITION_FAILED,
    Replay,
    load_capability,
    write_evidence,
)
from mockbank import faults

MEMBER_ID = "12345"
KNOWN_SAVINGS = "$1,234.56"
KNOWN_SAVINGS_PARSED = "1234.56"
RAW_FORMS = (MEMBER_ID, KNOWN_SAVINGS, KNOWN_SAVINGS_PARSED, "1,234.56")


@pytest.fixture
def policy(mockbank_server) -> Policy:
    return Policy(load_app_config("mockbank", base_url=mockbank_server.base_url))


@pytest.fixture
def artifact() -> CapabilityArtifact:
    return load_capability("mockbank", "lookup_member_balance")


def replay(policy: Policy, page: Page, artifact: CapabilityArtifact, member_id: str, out: Path):
    engine = Replay(
        artifact=artifact,
        config=policy.config,
        policy=policy,
        surface=PlaywrightSurface(page),
        params={"member_id": member_id},
    )
    result = engine.run()
    write_evidence(engine, result, out)
    return engine, result


def read_events(out: Path) -> list[dict]:
    return [json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def assert_no_raw_literals(out: Path) -> None:
    for name in ("meta.json", "events.jsonl", "result.json"):
        text = (out / name).read_text(encoding="utf-8")
        for raw in RAW_FORMS:
            assert raw not in text, f"{name} leaks {raw!r}"


def tampered_artifact(artifact: CapabilityArtifact, tmp_path: Path, **update) -> CapabilityArtifact:
    """A modified copy written to tmp_path and re-loaded through the exact-lookup path."""
    raw = json.loads(artifact.model_dump_json())
    raw.update(update)
    path = tmp_path / "capabilities" / "mockbank" / "lookup_member_balance.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return load_capability("mockbank", "lookup_member_balance", tmp_path / "capabilities")


# --- success / business outcome ------------------------------------------------------


def test_replay_success_returns_runtime_balance_and_redacts_persisted(policy, page, artifact, tmp_path) -> None:
    out = tmp_path / "replay-success"
    engine, result = replay(policy, page, artifact, MEMBER_ID, out)

    assert result.kind == "success"
    assert result.outputs == {"savings_balance": KNOWN_SAVINGS_PARSED}  # raw runtime return value
    assert result.business_outcome is None and result.failure is None
    assert result.llm_calls == 0 and result.recovery_count == 0
    assert page.frame(name="main").url.endswith("/member")

    persisted = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert persisted["outputs"] == {"savings_balance": {"redacted": True, "type": "money"}}
    assert persisted["kind"] == "success" and persisted["llm_calls"] == 0
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["model_call_count"] == 0 and meta["llm_calls"] == 0 and meta["result_kind"] == "success"
    events = read_events(out)
    assert [e["action"] for e in events if "action" in e] == ["navigate", "fill", "click", "read"]
    assert not any(e.get("event") == "recovery" for e in events)
    assert_no_raw_literals(out)


def test_99999_returns_member_not_found_business_outcome(policy, page, artifact, tmp_path) -> None:
    out = tmp_path / "replay-notfound"
    engine, result = replay(policy, page, artifact, "99999", out)
    assert result.kind == "business_outcome"
    assert result.business_outcome == "MEMBER_NOT_FOUND"
    assert result.failure is None and result.outputs == {}
    persisted = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert persisted["kind"] == "business_outcome" and persisted["business_outcome"] == "MEMBER_NOT_FOUND"
    events = read_events(out)
    assert [e["action"] for e in events if "action" in e] == ["navigate", "fill", "click"]
    assert "99999" not in (out / "events.jsonl").read_text(encoding="utf-8")


# --- known deterministic recovery ------------------------------------------------------


def test_interstitial_recovery_succeeds_without_replaying_search(policy, page, artifact, tmp_path) -> None:
    faults.arm(faults.INTERSTITIAL)
    out = tmp_path / "replay-interstitial"
    engine, result = replay(policy, page, artifact, MEMBER_ID, out)

    assert result.kind == "success"  # recovery is an event, not a result kind
    assert result.outputs == {"savings_balance": KNOWN_SAVINGS_PARSED}
    assert result.recovery_count == 1
    assert faults.snapshot()[faults.INTERSTITIAL] is False  # consumed by the one Search

    events = read_events(out)
    search_clicks = [e for e in events if e.get("action") == "click" and e["control"]["name"] == "Search"]
    assert len(search_clicks) == 1 and search_clicks[0]["step_id"] == "s2"
    recoveries = [e for e in events if e.get("event") == "recovery"]
    assert len(recoveries) == 1
    assert recoveries[0]["code"] == "DISMISS_SYSTEM_NOTICE" and recoveries[0]["result"] == "executed"
    assert recoveries[0]["control"]["name"] == "Continue" and recoveries[0]["step_id"] == "s2"
    assert recoveries[0]["phase"] == "after"  # the notice can only appear once Search has been submitted
    continue_acts = [e for e in events if (e.get("control") or {}).get("name") == "Continue"]
    assert len(continue_acts) == 1  # across the whole run, Continue is acted on exactly once
    # ordering: Search click -> recovery -> settle advances -> read; nothing clicked Search again
    order = [e.get("event") or e.get("action") for e in events]
    assert order.index("recovery") > order.index("click")
    assert order.count("click") == 1
    settle_after = next(e for e in events if e.get("event") == "settle" and e["step_id"] == "s2" and e["phase"] == "after")
    assert settle_after["outcome"] == "advance" and settle_after["recoveries"] == 1
    assert_no_raw_literals(out)


def test_recovery_is_not_refired_while_its_navigation_is_pending(monkeypatch, policy, page, artifact, tmp_path) -> None:
    """Regression: a slow Member Detail response keeps the 'System notice' document alive after Continue
    was clicked. Re-observing that document must not re-fire the recovery (blind re-execution, spec 14.3):
    the surface reports the submitted document as mid-navigation until the navigation commits."""
    import mockbank.app as mockbank_app

    original_render = mockbank_app._render

    def slow_render(template: str, status_code: int = 200, **context):
        if template == "member.html":
            time.sleep(0.8)  # the POST /member response is pending; the notice page is still on screen
        return original_render(template, status_code, **context)

    monkeypatch.setattr(mockbank_app, "_render", slow_render)
    faults.arm(faults.INTERSTITIAL)
    out = tmp_path / "replay-interstitial-slow"
    engine, result = replay(policy, page, artifact, MEMBER_ID, out)

    assert result.kind == "success" and result.outputs == {"savings_balance": KNOWN_SAVINGS_PARSED}
    assert result.recovery_count == 1
    events = read_events(out)
    continue_acts = [e for e in events if (e.get("control") or {}).get("name") == "Continue"]
    assert len(continue_acts) == 1
    assert len([e for e in events if e.get("action") == "click" and e["control"]["name"] == "Search"]) == 1
    settle_after = next(e for e in events if e.get("event") == "settle" and e["step_id"] == "s2" and e["phase"] == "after")
    assert settle_after["outcome"] == "advance" and settle_after["recoveries"] == 1


# --- structured failures ------------------------------------------------------------------


def test_hard_failure_from_tampered_artifact_is_structured(policy, page, artifact, tmp_path) -> None:
    success = json.loads(artifact.model_dump_json())["success"]
    impossible = {"kind": "all", "conditions": [success, {"kind": "text_visible", "text": "Checking balance"}]}
    tampered = tampered_artifact(artifact, tmp_path, success=impossible)
    out = tmp_path / "replay-hard-failure"
    engine, result = replay(policy, page, tampered, MEMBER_ID, out)

    assert result.kind == "failure"
    failure = result.failure
    assert failure.step_id is None
    assert failure.code == FINAL_CHECKPOINT_FAILED
    assert failure.expected == tampered.success
    assert failure.escalation is None  # a hard failure never starts a handoff loop
    assert "Member Detail" in failure.observed_summary
    assert result.outputs == {"savings_balance": KNOWN_SAVINGS_PARSED}  # captured before the checkpoint

    persisted = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert persisted["failure"]["code"] == FINAL_CHECKPOINT_FAILED
    assert persisted["failure"]["expected"]["kind"] == "all"
    assert "[REDACTED:member_id]" in persisted["failure"]["observed_summary"]
    assert "[REDACTED:savings_balance]" in persisted["failure"]["observed_summary"]
    assert persisted["outputs"] == {"savings_balance": {"redacted": True, "type": "money"}}
    assert_no_raw_literals(out)
    assert load_capability("mockbank", "lookup_member_balance", CAPABILITIES_DIR) == artifact  # committed artifact untouched


def test_policy_blocked_is_direct_failure(policy, page, artifact, tmp_path) -> None:
    steps = json.loads(artifact.model_dump_json())["steps"]
    steps.append(
        {
            "id": "s4",
            "description": "Click Close Account",
            "action": {"kind": "click"},
            "target": {"locators": [{"kind": "role_name", "role": "button", "name": "Close Account"}]},
            "postcondition": None,
        }
    )
    tampered = tampered_artifact(artifact, tmp_path, steps=steps)
    out = tmp_path / "replay-policy-blocked"
    engine, result = replay(policy, page, tampered, MEMBER_ID, out)
    assert result.kind == "failure"
    assert result.failure.code == POLICY_BLOCKED and result.failure.step_id == "s4"
    assert result.failure.escalation is None
    assert "irreversible" in result.failure.observed_summary
    assert page.frame(name="main").url.endswith("/member")  # nothing happened to the account
    rejected = [e for e in read_events(out) if e.get("step_id") == "s4" and "action" in e]
    assert rejected and rejected[0]["result"] == "rejected"


def test_headless_stuck_replay_returns_unavailable_headless(policy, page, artifact, tmp_path) -> None:
    faults.arm(faults.UNKNOWN)  # "Supervisor override required" is not a known recovery
    engine, result = replay(policy, page, artifact, MEMBER_ID, tmp_path / "replay-stuck")
    assert result.kind == "failure"
    assert result.failure.code == POSTCONDITION_FAILED and result.failure.step_id == "s2"
    assert result.failure.escalation == "unavailable_headless"
    assert "Supervisor override required" in result.failure.observed_summary
    assert result.recovery_count == 0
    assert engine.surface.observe().frames["top/main"].endswith("/override")  # not guessed through


# --- zero LLM decisions --------------------------------------------------------------------


class _ModelGuard:
    """Any attribute access or call means Replay tried to use a model: fail immediately."""

    def __getattr__(self, name: str):
        raise AssertionError(f"Replay attempted model access: {name}")

    def __call__(self, *args, **kwargs):
        raise AssertionError("Replay attempted to construct a model client")


class _GuardModule(types.ModuleType):
    def __getattr__(self, name: str):
        raise AssertionError(f"Replay attempted to use anthropic.{name}")


def test_replay_makes_zero_model_calls(monkeypatch: pytest.MonkeyPatch, policy, page, artifact, tmp_path) -> None:
    import cua.model_client as model_client

    monkeypatch.setattr(model_client, "AnthropicModelClient", _ModelGuard())
    monkeypatch.setattr(model_client, "configured_model", _ModelGuard())
    monkeypatch.setitem(sys.modules, "anthropic", _GuardModule("anthropic"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "guard-must-never-be-used")

    out = tmp_path / "replay-zero-llm"
    engine, result = replay(policy, page, artifact, MEMBER_ID, out)
    assert result.kind == "success"
    assert result.llm_calls == 0
    assert result.outputs == {"savings_balance": KNOWN_SAVINGS_PARSED}
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["llm_calls"] == 0 and meta["model_call_count"] == 0 and meta["model"] is None
    assert json.loads((out / "result.json").read_text(encoding="utf-8"))["llm_calls"] == 0


# --- CLI ------------------------------------------------------------------------------------


def test_cli_replay_writes_evidence_and_prints_runtime_result(mockbank_server, tmp_path) -> None:
    out = tmp_path / "cli-evidence"
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
    assert proc.returncode == 0, proc.stderr
    assert "kind: success" in proc.stdout
    assert f'"savings_balance": "{KNOWN_SAVINGS_PARSED}"' in proc.stdout  # raw to the caller
    assert "llm_calls: 0" in proc.stdout
    assert {p.name for p in out.iterdir()} == {"meta.json", "events.jsonl", "result.json"}
    assert_no_raw_literals(out)

    bad = subprocess.run(
        [sys.executable, "-m", "cua.cli", "replay", "--app", "mockbank", "--capability", "lookup_member_balance"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert bad.returncode == 2 and "missing required parameter" in bad.stderr
