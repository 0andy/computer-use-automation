"""Compiler contracts over a DiscoveryRecord produced by the fake-model Discovery path
against the live MockBank fixture (docs/spec.md 12, 13; phase prompt 3.1-3.4).

No API key and no hand-written record fixtures: the record is exactly what the
Phase 2 Discovery loop produces (Surface, Policy and MockBank are real; only the
model is scripted). Artifacts are written to tmp_path only.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page

from cua import catalog
from cua.compiler import CompileError, artifact_path, compile_record, default_description, serialize, write_artifact
from tests.wallclock import assert_no_wall_clock
from cua.conditions import Bindings, evaluate
from cua.config import AppConfig, load_app_config
from cua.discovery import Discovery, DiscoveryParam, DiscoveryRecord
from cua.evidence import EvidenceWriter
from cua.models import (
    AllCondition,
    CapabilityArtifact,
    FillAction,
    OutputPresent,
    ParameterValue,
    ReadAction,
    TableCellLocator,
    TextVisible,
    ValueEqualsParameter,
)
from cua.playwright_surface import PlaywrightSurface
from cua.policy import Policy
from tests.fake_model import FakeModel, click_search, done, fill_member_id, read_savings

MEMBER_ID = "12345"
BALANCE_FORMS = ("$1,234.56", "1,234.56", "1234.56")
GOAL = "Look up the member identified by {member_id} and read the current savings balance"
CAPABILITY = "lookup_member_balance"
DESCRIPTION = "Look up member balance."  # derived from the capability id, never the goal


def discover(config: AppConfig, page: Page, script, **kw) -> DiscoveryRecord:
    discovery = Discovery(
        config=config,
        policy=Policy(config),
        surface=PlaywrightSurface(page),
        model=FakeModel(script),
        capability_id=CAPABILITY,
        goal=GOAL,
        params={"member_id": DiscoveryParam(name="member_id", type="string", value=MEMBER_ID)},
        max_steps=12,
        timeout_s=60,
        **kw,
    )
    return discovery.run()


@pytest.fixture(scope="module")
def config(mockbank_server) -> AppConfig:
    return load_app_config("mockbank", base_url=mockbank_server.base_url)


@pytest.fixture(scope="module")
def golden(config: AppConfig, browser: Browser) -> Iterator[tuple[DiscoveryRecord, CapabilityArtifact]]:
    """One clean fake-model Discovery (fill, click, read, done) and its compiled artifact."""
    context = browser.new_context()
    try:
        record = discover(config, context.new_page(), [fill_member_id(), click_search(), read_savings(), done()])
        assert record.stop_reason == "goal_completed"
        yield record, compile_record(record, config)
    finally:
        context.close()


def dumped(artifact: CapabilityArtifact) -> dict:
    return json.loads(serialize(artifact))


def all_keys(value, out: set[str] | None = None) -> set[str]:
    out = set() if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(k)
            all_keys(v, out)
    elif isinstance(value, list):
        for v in value:
            all_keys(v, out)
    return out


# --- steps ----------------------------------------------------------------------


def test_written_artifact_and_discovery_evidence_carry_no_wall_clock_fields(golden, tmp_path: Path) -> None:
    record, artifact = golden
    out = tmp_path / "discovery"
    writer = EvidenceWriter(out, record.literals)
    writer.write_meta(record.meta())
    writer.write_events(record.events)
    write_artifact(artifact, out / "artifact.json")
    assert_no_wall_clock(out, {"meta.json", "events.jsonl", "artifact.json"})
    assert isinstance(record.meta()["elapsed_ms"], int) and record.meta()["elapsed_ms"] >= 0


def test_compiler_preserves_executed_action_order_with_deterministic_descriptions(golden) -> None:
    record, artifact = golden
    assert [a.kind for a in record.actions] == ["fill", "click", "read"]
    assert [s.action.kind for s in artifact.steps] == ["fill", "click", "read"]
    assert [s.id for s in artifact.steps] == ["s1", "s2", "s3"]
    assert [s.description for s in artifact.steps] == [
        "Fill Member ID with {member_id}",
        "Click Search",
        "Read Savings balance",
    ]


def test_fill_parameterizes_and_gets_value_equals_parameter(golden) -> None:
    _, artifact = golden
    fill = artifact.steps[0]
    assert fill.action == FillAction(kind="fill", value=ParameterValue(name="member_id"))
    assert fill.target is not None
    assert fill.target.locators[0].model_dump() == {"kind": "label", "text": "Member ID"}
    assert {"kind": "attr", "attr": "name", "value": "member_id"} in [loc.model_dump() for loc in fill.target.locators]
    assert isinstance(fill.postcondition, ValueEqualsParameter)
    assert fill.postcondition.param == "member_id"
    assert fill.postcondition.target == fill.target  # regenerated from the pre-action Observation
    # a labelled input is never identified structurally: the same row-anchored table_cell would
    # resolve to the static "Member ID | <id>" cell on Member Detail when the postcondition re-settles
    assert [loc.kind for loc in fill.target.locators] == ["label", "attr"]
    assert "table_cell" not in json.dumps(fill.model_dump(mode="json"))
    assert dumped(artifact)["inputs"] == {"member_id": {"type": "string", "required": True, "sensitive": True}}


def test_verified_click_expect_is_frozen_as_postcondition(golden) -> None:
    record, artifact = golden
    click = artifact.steps[1]
    assert record.actions[1].expect_verified is True
    assert click.target is not None
    assert click.target.locators[0].model_dump() == {"kind": "role_name", "role": "button", "name": "Search"}
    assert click.postcondition == TextVisible(kind="text_visible", text="Member Detail")


def test_failed_expect_executed_click_compiles_with_null_postcondition(config: AppConfig, page: Page) -> None:
    wrong = click_search(expect={"kind": "text_visible", "text": "Search results"})
    record = discover(config, page, [fill_member_id(), wrong, read_savings(), done()], settle_timeout_s=1.0)
    assert record.stop_reason == "goal_completed"
    assert record.actions[1].result == "executed" and record.actions[1].expect_verified is False

    artifact = compile_record(record, config)
    click = artifact.steps[1]
    assert click.action.kind == "click" and click.description == "Click Search"
    assert click.postcondition is None  # executed click kept; no condition derived or invented
    assert [s.action.kind for s in artifact.steps] == ["fill", "click", "read"]
    assert "Search results" not in serialize(artifact)


def test_read_produces_money_output_and_output_present(golden) -> None:
    _, artifact = golden
    read = artifact.steps[2]
    assert read.action == ReadAction(kind="read", capture_as="savings_balance", parser="money")
    assert read.postcondition == OutputPresent(kind="output_present", name="savings_balance")
    assert dumped(artifact)["outputs"] == {
        "savings_balance": {"type": "money", "from_step": "s3", "capture_as": "savings_balance", "sensitive": True}
    }


def test_savings_locator_is_row_anchored_table_cell_never_balance_text(golden) -> None:
    _, artifact = golden
    read = artifact.steps[2]
    assert read.target is not None
    assert [loc.model_dump() for loc in read.target.locators] == [
        {"kind": "table_cell", "row_anchor": "Savings", "col_offset": 1}
    ]
    assert all(isinstance(loc, TableCellLocator) for loc in read.target.locators)
    for form in BALANCE_FORMS:
        assert form not in json.dumps(read.model_dump(mode="json"))


# --- locator safety -------------------------------------------------------------


def test_refs_never_enter_artifact(golden) -> None:
    record, artifact = golden
    payload = dumped(artifact)
    assert "ref" not in all_keys(payload)
    text = serialize(artifact)
    for action in record.actions:
        assert action.ref not in text


def test_sensitive_locator_candidate_is_removed(golden) -> None:
    """The final-Observation member-id cell is identified structurally, never by its text."""
    record, artifact = golden
    anchors = [c for c in artifact.success.conditions if isinstance(c, ValueEqualsParameter)]
    assert len(anchors) == 1
    assert [loc.model_dump() for loc in anchors[0].target.locators] == [
        {"kind": "table_cell", "row_anchor": "Member ID", "col_offset": 1}
    ]
    cell = next(c for c in record.final_observation.controls if (c.text or "") == MEMBER_ID)
    assert cell.name == MEMBER_ID  # a role_name candidate existed and was dropped as sensitive


def test_non_unique_locator_cannot_compile(golden, config: AppConfig) -> None:
    record, _ = golden
    click = record.actions[1]
    twin = click.control.model_copy(update={"ref": click.control.ref + ":twin"})
    ambiguous = click.pre_observation.model_copy(update={"controls": [*click.pre_observation.controls, twin]})
    leaky = dataclasses.replace(record, actions=[record.actions[0], dataclasses.replace(click, pre_observation=ambiguous), record.actions[2]])
    with pytest.raises(CompileError) as exc:
        compile_record(leaky, config)
    assert "s2" in str(exc.value) and "locator" in str(exc.value)


# --- success --------------------------------------------------------------------


def test_final_success_is_done_and_output_present_and_parameter_anchor(golden) -> None:
    record, artifact = golden
    success = artifact.success
    assert isinstance(success, AllCondition)
    kinds = [c.kind for c in success.conditions]
    assert kinds == ["text_visible", "output_present", "value_equals_parameter"]
    assert success.conditions[0] == TextVisible(kind="text_visible", text="Savings")
    assert success.conditions[1] == OutputPresent(kind="output_present", name="savings_balance")
    anchor = success.conditions[2]
    assert anchor.param == "member_id"

    bindings = Bindings(params={"member_id": MEMBER_ID}, outputs=record.outputs)
    assert evaluate(success, record.final_observation, bindings) is True
    other = Bindings(params={"member_id": "99999"}, outputs=record.outputs)
    assert evaluate(success, record.final_observation, other) is False  # anchored to the invoked member


def test_business_outcome_and_recovery_copied_from_app_config(golden, config: AppConfig) -> None:
    _, artifact = golden
    assert artifact.business_outcomes == config.business_outcomes
    assert artifact.recoveries == config.recoveries
    assert [b.code for b in artifact.business_outcomes] == ["MEMBER_NOT_FOUND"]
    assert [r.code for r in artifact.recoveries] == ["DISMISS_SYSTEM_NOTICE"]


def test_schema_version_and_revision(golden) -> None:
    _, artifact = golden
    payload = dumped(artifact)
    assert list(payload)[:5] == ["schema_version", "revision", "capability_id", "app", "description"]
    assert payload["schema_version"] == "1.0" and payload["revision"] == 1
    assert payload["capability_id"] == CAPABILITY and payload["app"] == "mockbank"
    assert payload["description"] == DESCRIPTION
    assert payload["description"] != GOAL and "{" not in payload["description"]


# --- leak scan and serialization --------------------------------------------------


def test_artifact_contains_no_sensitive_literal(golden) -> None:
    _, artifact = golden
    text = serialize(artifact)
    for literal in (MEMBER_ID, *BALANCE_FORMS):
        assert literal not in text
    assert "[REDACTED" not in text  # nothing needed redacting: values are references, not literals


def test_sensitive_literal_scan_blocks_leaks(golden, config: AppConfig) -> None:
    record, _ = golden
    click = record.actions[1]
    # a verified expect carrying the raw balance would be frozen as the click postcondition
    leaky_expect = TextVisible(kind="text_visible", text="Savings $1,234.56")
    leaky = dataclasses.replace(
        record, actions=[record.actions[0], dataclasses.replace(click, expect=leaky_expect, expect_verified=True), record.actions[2]]
    )
    with pytest.raises(CompileError) as exc:
        compile_record(leaky, config)
    message = str(exc.value)
    assert "savings_balance" in message
    for form in BALANCE_FORMS:
        assert form not in message

    # an authored description carrying the raw member id is caught the same way
    with pytest.raises(CompileError) as exc:
        compile_record(record, config, description=f"Look up member {MEMBER_ID}.")
    assert "member_id" in str(exc.value) and MEMBER_ID not in str(exc.value)


def test_description_is_authored_or_derived_never_the_goal(golden, config: AppConfig) -> None:
    record, artifact = golden
    assert artifact.description == default_description(CAPABILITY) == DESCRIPTION
    assert default_description("close_account") == "Close account."
    authored = compile_record(record, config, description="  Look up a member and return the current savings balance. ")
    assert authored.description == "Look up a member and return the current savings balance."
    assert authored.model_copy(update={"description": artifact.description}) == artifact
    with pytest.raises(CompileError):
        compile_record(record, config, description="   ")
    assert GOAL not in serialize(artifact) and GOAL not in serialize(authored)


def test_compile_refuses_a_discovery_that_did_not_complete(golden, config: AppConfig) -> None:
    record, _ = golden
    incomplete = dataclasses.replace(record, stop_reason="give_up", done_condition=None)
    with pytest.raises(CompileError):
        compile_record(incomplete, config)


def test_artifact_path_is_deterministic_and_serialization_round_trips(golden, tmp_path: Path) -> None:
    _, artifact = golden
    root = tmp_path / "capabilities"
    path = artifact_path("mockbank", CAPABILITY, root)
    assert path == root / "mockbank" / f"{CAPABILITY}.json"
    written = write_artifact(artifact, path)
    assert written == path and path.is_file()
    assert serialize(artifact) == serialize(artifact) == path.read_text(encoding="utf-8")
    assert CapabilityArtifact.model_validate(json.loads(path.read_text(encoding="utf-8"))) == artifact


# --- catalog over the emitted artifact -------------------------------------------


def _cli(args: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONUTF8": "1"}
    return subprocess.run(
        [sys.executable, "-m", "cua.cli", "capabilities", *args],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120,
    )


def test_catalog_lists_emitted_capability_with_matching_metadata(golden, tmp_path: Path) -> None:
    _, artifact = golden
    root = tmp_path / "capabilities"
    write_artifact(artifact, artifact_path("mockbank", CAPABILITY, root))

    entries = catalog.scan(root)
    assert [e.model_dump() for e in entries] == [
        {
            "app": "mockbank",
            "capability_id": CAPABILITY,
            "description": DESCRIPTION,
            "revision": 1,
            "inputs": {"member_id": "string"},
            "outputs": {"savings_balance": "money"},
        }
    ]
    assert catalog.find("mockbank", CAPABILITY, root) == entries[0]
    assert catalog.scan(root, app="otherapp") == []

    listed = _cli(["list", "--capabilities-dir", str(root)])
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.strip() == f"mockbank  {CAPABILITY}  rev=1  inputs=member_id:string  outputs=savings_balance:money  {DESCRIPTION}"
    assert _cli(["list", "--app", "otherapp", "--capabilities-dir", str(root)]).stdout.strip() == "no capabilities found"

    shown = _cli(["show", "--app", "mockbank", "--capability", CAPABILITY, "--capabilities-dir", str(root)])
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout) == entries[0].model_dump()
    for literal in (MEMBER_ID, *BALANCE_FORMS):
        assert literal not in listed.stdout + shown.stdout
