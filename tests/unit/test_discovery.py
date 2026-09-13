"""Discovery loop contracts over hand-built Observations, a FakeSurface and a FakeModel (docs/spec.md 8, 9).

No browser and no API key. Every automated action still crosses Policy.check -> Surface.act.
"""

from __future__ import annotations

import json
import time

import pytest

from cua.config import load_app_config
from cua.discovery import (
    TOOLS,
    Discovery,
    DiscoveryInputError,
    DiscoveryParam,
    parse_money,
    parse_param,
    parse_params,
    render_initial_prompt,
    validate_capability_id,
    validate_goal,
)
from cua.compiler import compile_record, write_artifact
from cua.evidence import EvidenceWriter, sanitize
from cua.models import OutputPresent, RuntimeClick, ValueEqualsParameter
from cua.policy import Policy
from tests.fake_model import FakeModel, click_search, done, fill_member_id, give_up, read_savings, ref_for
from tests.fake_surface import FakeSurface
from tests.unit.obs import BALANCE, MEMBER_ID, member_detail_page, members_page
from tests.wallclock import assert_no_wall_clock

GOAL = "Look up the member identified by {member_id} and read the current savings balance"
PARAMS = {"member_id": DiscoveryParam(name="member_id", type="string", value=MEMBER_ID)}
SEARCH_REF = "1:1:3"  # Search button in members_page()
CLOSE_ACCOUNT_REF = "2:1:9"  # Close Account button in member_detail_page()
SAVINGS_PARSED = "1234.56"


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch) -> Policy:
    monkeypatch.delenv("MOCKBANK_BASE_URL", raising=False)
    return Policy(load_app_config("mockbank"))


def make_surface() -> FakeSurface:
    surface = FakeSurface(members_page())
    surface.transitions[SEARCH_REF] = member_detail_page()
    return surface


def run(policy: Policy, surface: FakeSurface, script, **kw) -> tuple[Discovery, FakeModel]:
    model = FakeModel(script, repeat_last=kw.pop("repeat_last", False))
    discovery = Discovery(
        config=policy.config,
        policy=policy,
        surface=surface,
        model=model,
        capability_id="lookup_member_balance",
        goal=GOAL,
        params=dict(PARAMS),
        settle_timeout_s=0.0,
        settle_poll_s=0.0,
        **kw,
    )
    discovery.run()
    return discovery, model


def persisted_text(record, tmp_path) -> str:
    writer = EvidenceWriter(tmp_path / "ev", record.literals)
    meta = writer.write_meta(record.meta())
    events = writer.write_events(record.events)
    return meta.read_text(encoding="utf-8") + events.read_text(encoding="utf-8")


# --- pre-flight validation (before browser/model) -----------------------------


def test_unknown_placeholder_rejected_before_browser_or_model(policy: Policy) -> None:
    with pytest.raises(DiscoveryInputError, match="account_no"):
        validate_goal("Look up {account_no}", PARAMS)

    class Untouchable:
        def __getattr__(self, name):  # any use is a test failure
            raise AssertionError(f"{name} used before validation passed")

    with pytest.raises(DiscoveryInputError):
        Discovery(
            config=policy.config, policy=policy, surface=Untouchable(), model=Untouchable(),
            capability_id="lookup_member_balance", goal="Find {account_no}", params=dict(PARAMS),
        )


@pytest.mark.parametrize("bad", ["", "Lookup", "1abc", "look-up", "look up", "lookUp"])
def test_capability_id_validation_rejects(bad: str) -> None:
    with pytest.raises(DiscoveryInputError):
        validate_capability_id(bad)


def test_capability_id_validation_accepts() -> None:
    assert validate_capability_id("lookup_member_balance") == "lookup_member_balance"
    assert validate_capability_id("a1_b2") == "a1_b2"


def test_param_parsing() -> None:
    param = parse_param("member_id:string=12345")
    assert (param.name, param.type, param.value, param.sensitive) == ("member_id", "string", "12345", True)
    assert "12345" not in repr(param)  # raw binding is not shown by repr
    assert parse_param("k:string=a=b").value == "a=b"
    for bad in ["member_id=12345", "member_id:int=1", "Member:string=1", "member_id:string=", "x"]:
        with pytest.raises(DiscoveryInputError):
            parse_param(bad)
    with pytest.raises(DiscoveryInputError, match="twice"):
        parse_params(["member_id:string=1", "member_id:string=2"])


# --- tools and prompt ---------------------------------------------------------


def test_exact_tool_schemas() -> None:
    assert [t["name"] for t in TOOLS] == ["click_ref", "fill_ref", "read_ref", "done", "give_up"]
    by_name = {t["name"]: t["input_schema"] for t in TOOLS}
    assert by_name["click_ref"]["required"] == ["ref", "expect", "reason"]
    assert by_name["fill_ref"]["required"] == ["ref", "from_param", "reason"]
    assert by_name["read_ref"]["required"] == ["ref", "capture_as", "parser", "reason"]
    assert by_name["read_ref"]["properties"]["parser"]["enum"] == ["money"]
    assert by_name["done"]["required"] == ["condition", "reason"]
    assert by_name["give_up"]["properties"]["reason_code"]["enum"] == [
        "goal_unreachable", "blocked_by_unknown_state", "missing_information",
    ]
    for schema in by_name.values():
        assert schema["properties"]["reason"]["maxLength"] == 120
        assert schema["additionalProperties"] is False
    assert by_name["click_ref"]["properties"]["expect"]["properties"]["kind"]["enum"] == [
        "all", "text_absent", "text_visible", "url_matches",
    ]


def test_model_prompt_omits_raw_member_id(policy: Policy) -> None:
    literals = {"member_id": [MEMBER_ID]}
    prompt = render_initial_prompt(GOAL, PARAMS, members_page(), literals)
    assert "12345" not in prompt
    assert "{member_id}" in prompt and "member_id: string" in prompt

    # Even once the page shows the member id (Member Detail), the model sees a token, not the value.
    surface = make_surface()
    discovery, model = run(policy, surface, [fill_member_id(), click_search(), give_up()])
    shown = model.all_text_shown()
    assert "12345" not in shown
    assert "[REDACTED:member_id]" in shown  # the Member ID cell on Member Detail
    assert "{member_id}" in shown


# --- proposal validation order ----------------------------------------------


def test_fill_requires_from_param(policy: Policy) -> None:
    surface = make_surface()
    bad_schema = lambda text: {"name": "fill_ref", "input": {"ref": ref_for(text, "textbox", label="Member ID"), "reason": "x"}}
    undeclared = lambda text: {
        "name": "fill_ref",
        "input": {"ref": ref_for(text, "textbox", label="Member ID"), "from_param": "account_no", "reason": "x"},
    }
    discovery, model = run(policy, surface, [bad_schema, undeclared, give_up()])
    assert surface.acts[1:] == []  # only the startup navigate reached the Surface
    assert discovery.record.actions == []
    rejected = [e for e in discovery.record.events if e.get("result") == "rejected"]
    assert len(rejected) == 2 and all(e["action"] == "fill" for e in rejected)
    assert "account_no" in rejected[1]["error"]


def test_fill_auto_postcondition_is_value_equals_parameter(policy: Policy) -> None:
    surface = make_surface()
    discovery, _ = run(policy, surface, [fill_member_id(), give_up()])
    [action] = discovery.record.actions
    assert action.kind == "fill" and action.from_param == "member_id" and action.result == "executed"
    assert isinstance(action.postcondition, ValueEqualsParameter)
    assert action.postcondition.param == "member_id"
    assert action.postcondition.target.locators  # regenerated from the pre-action Observation
    assert action.postcondition.target.locators[0].model_dump() == {"kind": "label", "text": "Member ID"}
    assert action.postcondition_verified is True
    assert action.pre_observation.find(action.ref).input_value == ""


def test_read_auto_postcondition_is_output_present(policy: Policy) -> None:
    surface = make_surface()
    discovery, _ = run(policy, surface, [fill_member_id(), click_search(), read_savings(), give_up()])
    read = discovery.record.actions[-1]
    assert read.kind == "read" and read.capture_as == "savings_balance" and read.parser == "money"
    assert read.postcondition == OutputPresent(kind="output_present", name="savings_balance")
    assert read.postcondition_verified is True
    assert discovery.record.outputs == {"savings_balance": SAVINGS_PARSED}


def test_policy_blocked_proposal_never_reaches_surface_act(policy: Policy) -> None:
    surface = FakeSurface(member_detail_page())
    close_account = lambda text: {
        "name": "click_ref",
        "input": {"ref": ref_for(text, "button", name="Close Account"), "expect": {"kind": "text_visible", "text": "Closed"}, "reason": "x"},
    }
    discovery, model = run(policy, surface, [close_account, give_up()])
    assert not any(isinstance(a, RuntimeClick) for a in surface.acts)
    assert discovery.record.actions == []
    [event] = [e for e in discovery.record.events if e.get("result") == "policy_rejected"]
    assert "irreversible" in event["error"]
    assert "irreversible" in model.requests[1]["messages"][-1]["content"][0]["content"]
    assert discovery.record.stop_reason == "give_up"


def test_second_policy_rejected_proposal_stops_with_policy_blocked(policy: Policy) -> None:
    surface = FakeSurface(member_detail_page())
    close_account = lambda text: {
        "name": "click_ref",
        "input": {"ref": ref_for(text, "button", name="Close Account"), "expect": {"kind": "text_visible", "text": "Closed"}, "reason": "x"},
    }
    discovery, model = run(policy, surface, [close_account, close_account, give_up()])
    assert discovery.record.stop_reason == "policy_blocked"
    assert model.calls == 2
    assert not any(isinstance(a, RuntimeClick) for a in surface.acts)


def test_model_facing_condition_vocabulary_rejects_runtime_only_kinds(policy: Policy) -> None:
    surface = make_surface()
    runtime_only = lambda text: {
        "name": "click_ref",
        "input": {"ref": ref_for(text, "button", name="Search"), "expect": {"kind": "output_present", "name": "savings_balance"}, "reason": "x"},
    }
    unknown_kind = lambda text: {
        "name": "click_ref",
        "input": {"ref": ref_for(text, "button", name="Search"), "expect": {"kind": "dialog_visible"}, "reason": "x"},
    }
    nested = lambda text: {
        "name": "click_ref",
        "input": {
            "ref": ref_for(text, "button", name="Search"),
            "expect": {"kind": "all", "conditions": [{"kind": "text_visible", "text": "Member Detail"},
                                                     {"kind": "value_equals_parameter", "param": "member_id",
                                                      "target": {"locators": [{"kind": "label", "text": "Member ID"}]}}]},
            "reason": "x",
        },
    }
    discovery, _ = run(policy, surface, [runtime_only, unknown_kind, nested, give_up()], max_steps=10)
    assert not any(isinstance(a, RuntimeClick) for a in surface.acts)
    errors = [e["error"] for e in discovery.record.events if e.get("result") == "rejected"]
    assert "output_present" in errors[0] and "value_equals_parameter" in errors[2]
    assert "not a valid condition" in errors[1]


def test_sensitive_literal_click_expect_is_rejected_before_action(policy: Policy) -> None:
    surface = make_surface()
    leaky = lambda text: {
        "name": "click_ref",
        "input": {"ref": ref_for(text, "button", name="Search"), "expect": {"kind": "text_visible", "text": f"Member {MEMBER_ID}"}, "reason": "x"},
    }
    discovery, model = run(policy, surface, [leaky, give_up()])
    assert not any(isinstance(a, RuntimeClick) for a in surface.acts)
    assert discovery.record.actions == []
    [event] = [e for e in discovery.record.events if e.get("result") == "rejected"]
    assert "sensitive value of 'member_id'" in event["error"]
    assert MEMBER_ID not in json.dumps(discovery.record.events)
    assert MEMBER_ID not in model.all_text_shown()


def test_sensitive_literal_done_condition_is_rejected(policy: Policy) -> None:
    surface = make_surface()
    leaky_done = done({"kind": "all", "conditions": [{"kind": "text_visible", "text": "Savings"}, {"kind": "text_visible", "text": MEMBER_ID}]})
    discovery, _ = run(policy, surface, [fill_member_id(), click_search(), read_savings(), leaky_done, give_up()])
    assert discovery.record.done_condition is None
    assert discovery.record.stop_reason == "give_up"
    [event] = [e for e in discovery.record.events if e.get("result") == "rejected"]
    assert event["action"] == "done" and "sensitive value" in event["error"]

    # After the read, the balance is a known literal too.
    surface = make_surface()
    leaky_balance = done({"kind": "text_visible", "text": BALANCE})
    discovery, _ = run(policy, surface, [fill_member_id(), click_search(), read_savings(), leaky_balance, give_up()])
    [event] = [e for e in discovery.record.events if e.get("result") == "rejected"]
    assert "savings_balance" in event["error"] and BALANCE not in json.dumps(discovery.record.events)


def test_done_condition_must_currently_evaluate_true(policy: Policy) -> None:
    surface = make_surface()
    premature = done({"kind": "text_visible", "text": "Savings"})  # not on the Members page
    discovery, model = run(policy, surface, [premature, give_up()])
    assert discovery.record.done_condition is None and discovery.record.stop_reason == "give_up"
    assert model.calls == 2  # the rejected done consumed a round
    [event] = [e for e in discovery.record.events if e.get("result") == "rejected"]
    assert "does not currently hold" in event["error"]


def test_accepted_done_is_anded_with_output_present(policy: Policy) -> None:
    surface = make_surface()
    discovery, _ = run(policy, surface, [fill_member_id(), click_search(), read_savings(), done()])
    assert discovery.record.stop_reason == "goal_completed"
    condition = discovery.record.done_condition
    assert condition.kind == "all"
    assert condition.conditions[0].model_dump() == {"kind": "text_visible", "text": "Savings"}
    assert condition.conditions[1] == OutputPresent(kind="output_present", name="savings_balance")


# --- executed action rule ------------------------------------------------------


def test_executed_click_remains_in_record_when_expect_fails(policy: Policy) -> None:
    surface = make_surface()
    wrong_expect = click_search(expect={"kind": "text_visible", "text": "Search results"})
    discovery, model = run(policy, surface, [fill_member_id(), wrong_expect, give_up()])
    click = discovery.record.actions[-1]
    assert click.kind == "click" and click.result == "executed" and click.expect_verified is False
    assert click.expect.model_dump() == {"kind": "text_visible", "text": "Search results"}
    assert click.pre_observation.frames["top/main"].endswith("/members")
    assert click.post_observation.frames["top/main"].endswith("/member")
    event = [e for e in discovery.record.events if e.get("action") == "click"][-1]
    assert event["result"] == "executed" and event["expect_verified"] is False
    # the new Observation plus the verification failure went back to the model
    feedback = model.requests[2]["messages"][-1]["content"][0]["content"]
    assert "did NOT hold" in feedback and "Member Detail" in feedback and "Savings" in feedback


def test_not_executed_action_is_not_recorded(policy: Policy) -> None:
    surface = make_surface()
    surface.failures[SEARCH_REF] = "ACTION_FAILED: element detached"
    discovery, _ = run(policy, surface, [click_search(), give_up()])
    assert discovery.record.actions == []
    [event] = [e for e in discovery.record.events if e.get("result") == "not_executed"]
    assert "ACTION_FAILED" in event["error"]


# --- persistence / sanitization -----------------------------------------------


def test_read_raw_value_stays_runtime_only(policy: Policy, tmp_path) -> None:
    surface = make_surface()
    discovery, model = run(policy, surface, [fill_member_id(), click_search(), read_savings(), done()])
    record = discovery.record
    assert record.outputs["savings_balance"] == SAVINGS_PARSED  # runtime return value
    assert record.bindings.outputs["savings_balance"] == SAVINGS_PARSED
    text = persisted_text(record, tmp_path)
    for literal in (BALANCE, SAVINGS_PARSED, "1,234.56", MEMBER_ID):
        assert literal not in text
    assert "[REDACTED:savings_balance]" not in text or True  # tokens may appear; raw values never
    # once captured, the balance is redacted for the model too
    assert BALANCE not in model.requests[-1]["messages"][-1]["content"][0]["content"]


def test_persisted_fill_uses_parameter_reference(policy: Policy) -> None:
    surface = make_surface()
    discovery, _ = run(policy, surface, [fill_member_id(), give_up()])
    [event] = [e for e in discovery.record.events if e.get("action") == "fill"]
    assert event["actor"] == "model"
    assert event["value"] == {"kind": "parameter", "name": "member_id"}
    assert event["result"] == "executed" and event["postcondition_verified"] is True
    assert event["reason"] == "Enter the member identifier for the lookup."
    assert MEMBER_ID not in json.dumps(event)


def test_persisted_read_is_redacted(policy: Policy) -> None:
    surface = make_surface()
    discovery, _ = run(policy, surface, [fill_member_id(), click_search(), read_savings(), done()])
    [event] = [e for e in discovery.record.events if e.get("action") == "read"]
    assert event["capture_as"] == "savings_balance" and event["parser"] == "money"
    assert event["value"] is None and event["redacted"] is True and event["result"] == "executed"
    assert BALANCE not in json.dumps(event) and SAVINGS_PARSED not in json.dumps(event)


def test_reason_is_bounded_and_sanitized(policy: Policy) -> None:
    surface = make_surface()
    too_long = fill_member_id(reason="x" * 121)
    leaky = fill_member_id(reason=f"Type member {MEMBER_ID} into the field")
    discovery, _ = run(policy, surface, [too_long, leaky, give_up()])
    rejected = [e for e in discovery.record.events if e.get("result") == "rejected"]
    assert len(rejected) == 1 and "reason" in rejected[0]["error"]
    [fill] = [e for e in discovery.record.events if e.get("action") == "fill" and e["result"] == "executed"]
    assert fill["reason"] == "Type member [REDACTED:member_id] into the field"
    assert discovery.record.actions[0].reason.endswith("12345 into the field")  # raw only in memory


def test_message_id_and_usage_audit_shape(policy: Policy, tmp_path) -> None:
    surface = make_surface()
    discovery, model = run(policy, surface, [fill_member_id(), click_search(), read_savings(), done()])
    record = discovery.record
    assert len(record.model_calls) == 4 == model.calls == record.rounds
    assert [c.message_id for c in record.model_calls] == ["msg_fake_1", "msg_fake_2", "msg_fake_3", "msg_fake_4"]
    assert all(c.model == "fake-model" for c in record.model_calls)
    meta = record.meta()
    assert meta["model_call_count"] == 4 and meta["message_ids"] == [c.message_id for c in record.model_calls]
    assert meta["usage"] == {"input_tokens": 101 + 102 + 103 + 104, "output_tokens": 11 + 12 + 13 + 14}
    assert meta["stop_reason"] == "goal_completed" and meta["model"] == "fake-model"
    assert meta["params"] == {"member_id": {"type": "string", "sensitive": True}}
    assert meta["outputs"] == {"savings_balance": {"parser": "money"}}
    audit_events = [e for e in record.events if e.get("event") == "model_call"]
    assert [set(e) - {"seq", "actor", "event", "round"} for e in audit_events] == [
        {"message_id", "model", "input_tokens", "output_tokens"}] * 4
    # no transcript: the prompt / observation text is not in evidence
    text = persisted_text(record, tmp_path)
    assert "Current observation" not in text and "controls (" not in text and "You are the discovery agent" not in text


def test_persisted_discovery_evidence_is_deterministic_without_wall_clock_fields(policy: Policy, tmp_path) -> None:
    """meta.json, events.jsonl and the compiled artifact carry seq / elapsed_ms only, never wall-clock time."""
    discovery, _model = run(policy, make_surface(), [fill_member_id(), click_search(), read_savings(), done()])
    record = discovery.record
    out = tmp_path / "discovery"
    writer = EvidenceWriter(out, record.literals)
    writer.write_meta(record.meta())
    writer.write_events(record.events)
    write_artifact(compile_record(record, policy.config), out / "artifact.json")
    assert_no_wall_clock(out, {"meta.json", "events.jsonl", "artifact.json"})

    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert isinstance(meta["elapsed_ms"], int) and meta["elapsed_ms"] >= 0
    assert "started_at" not in meta and "finished_at" not in meta
    events = [json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1)) and not any("at" in e for e in events)


# --- stop reasons ---------------------------------------------------------------


def test_max_steps_is_deterministic(policy: Policy) -> None:
    surface = make_surface()
    premature = done({"kind": "text_visible", "text": "Savings"})
    discovery, model = run(policy, surface, [premature], repeat_last=True, max_steps=2)
    assert discovery.record.stop_reason == "max_steps" and model.calls == 2 and discovery.record.rounds == 2


def test_dead_end_after_three_rounds_without_progress(policy: Policy) -> None:
    surface = make_surface()
    premature = done({"kind": "text_visible", "text": "Savings"})
    discovery, model = run(policy, surface, [premature], repeat_last=True, max_steps=12)
    assert discovery.record.stop_reason == "dead_end" and model.calls == 3

    # progress resets the counter: fill (progress) then 3 rejections -> dead_end at round 4
    surface = make_surface()
    discovery, model = run(policy, surface, [fill_member_id(), premature], repeat_last=True, max_steps=12)
    assert discovery.record.stop_reason == "dead_end" and model.calls == 4


def test_timeout_is_deterministic(policy: Policy) -> None:
    surface = make_surface()

    def slow(text):
        time.sleep(0.05)
        return fill_member_id()(text)

    discovery, model = run(policy, surface, [slow, click_search()], timeout_s=0.02)
    assert discovery.record.stop_reason == "timeout" and model.calls == 1
    assert len(discovery.record.actions) == 1  # the executed fill stays recorded


def test_give_up_ends_discovery_without_handoff(policy: Policy) -> None:
    surface = make_surface()
    discovery, _ = run(policy, surface, [give_up("blocked_by_unknown_state", "Unknown page.")])
    record = discovery.record
    assert record.stop_reason == "give_up" and record.give_up_code == "blocked_by_unknown_state"
    assert surface.run_control.owner.value == "AUTOMATION"  # no handoff was requested


def test_fake_model_discovery_completes_fixture_task(policy: Policy) -> None:
    surface = make_surface()
    discovery, model = run(policy, surface, [fill_member_id(), click_search(), read_savings(), done()])
    record = discovery.record
    assert record.stop_reason == "goal_completed"
    assert [a.kind for a in record.actions] == ["fill", "click", "read"]
    assert record.actions[1].expect_verified is True
    assert record.outputs == {"savings_balance": SAVINGS_PARSED}
    assert model.calls == 4


# --- helpers ----------------------------------------------------------------------


def test_parse_money() -> None:
    assert parse_money("$1,234.56") == "1234.56"
    assert parse_money(" $ 1,234.56 ") == "1234.56"
    assert parse_money("1234.5") == "1234.50"
    assert parse_money("-$12") == "-12.00"
    for bad in ["", "Savings", "12,34", "$1,234.56 USD"]:
        with pytest.raises(ValueError):
            parse_money(bad)


def test_sanitize_replaces_every_known_literal_with_readable_token() -> None:
    literals = {"member_id": ["12345"], "savings_balance": ["$1,234.56", "1,234.56", "1234.56"]}
    assert sanitize("Member 12345 has $1,234.56 (1234.56)", literals) == (
        "Member [REDACTED:member_id] has [REDACTED:savings_balance] ([REDACTED:savings_balance])"
    )
    assert sanitize("nothing here", literals) == "nothing here"
    assert sanitize("", literals) == ""
