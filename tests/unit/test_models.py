"""Closed model vocabulary (docs/spec.md section 10): discriminated unions, no extras."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from cua.models import (
    ActionResult,
    CapabilityArtifact,
    Condition,
    Locator,
    Owner,
    ReplayResult,
    RunControl,
    RuntimeAction,
    Step,
)

ARTIFACT = {
    "schema_version": "1.0",
    "revision": 1,
    "capability_id": "lookup_member_balance",
    "app": "mockbank",
    "description": "Look up a member and return the current savings balance.",
    "inputs": {"member_id": {"type": "string"}},
    "outputs": {"savings_balance": {"type": "money", "from_step": "s3", "capture_as": "savings_balance"}},
    "steps": [
        {
            "id": "s1",
            "description": "Fill Member ID with {member_id}",
            "action": {"kind": "fill", "value": {"kind": "parameter", "name": "member_id"}},
            "target": {"locators": [{"kind": "label", "text": "Member ID"}, {"kind": "attr", "attr": "name", "value": "member_id"}]},
            "postcondition": {
                "kind": "value_equals_parameter",
                "target": {"locators": [{"kind": "label", "text": "Member ID"}]},
                "param": "member_id",
            },
        },
        {
            "id": "s2",
            "description": "Click Search",
            "action": {"kind": "click"},
            "target": {"locators": [{"kind": "role_name", "role": "button", "name": "Search"}]},
            "postcondition": None,
        },
        {
            "id": "s3",
            "description": "Read Savings balance",
            "action": {"kind": "read", "capture_as": "savings_balance", "parser": "money"},
            "target": {"locators": [{"kind": "table_cell", "row_anchor": "Savings", "col_offset": 1}]},
            "postcondition": {"kind": "output_present", "name": "savings_balance"},
        },
    ],
    "business_outcomes": [{"code": "MEMBER_NOT_FOUND", "when": {"kind": "text_visible", "text": "No member found"}}],
    "recoveries": [
        {
            "code": "DISMISS_SYSTEM_NOTICE",
            "when": {"kind": "text_visible", "text": "System notice"},
            "action": {"kind": "click"},
            "target": {"locators": [{"kind": "role_name", "role": "button", "name": "Continue"}]},
        }
    ],
    "success": {
        "kind": "all",
        "conditions": [
            {"kind": "text_visible", "text": "Member Detail"},
            {"kind": "url_matches", "pattern": "/member$"},
            {"kind": "text_absent", "text": "No member found"},
            {"kind": "output_present", "name": "savings_balance"},
        ],
    },
}


def test_artifact_round_trips_with_schema_version() -> None:
    artifact = CapabilityArtifact.model_validate(ARTIFACT)
    assert artifact.schema_version == "1.0"
    assert artifact.revision == 1
    assert [s.action.kind for s in artifact.steps] == ["fill", "click", "read"]
    assert artifact.steps[1].postcondition is None
    dumped = artifact.model_dump(mode="json")
    assert "schema" not in dumped and "schema_version" in dumped
    assert CapabilityArtifact.model_validate(dumped) == artifact


def test_schema_field_is_not_accepted() -> None:
    bad = {**ARTIFACT}
    bad["schema"] = bad.pop("schema_version")
    with pytest.raises(ValidationError):
        CapabilityArtifact.model_validate(bad)


@pytest.mark.parametrize(
    "field", ["state", "hash", "tenant_id", "approval", "provenance", "created_at"]
)
def test_artifact_rejects_lifecycle_and_provenance_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        CapabilityArtifact.model_validate({**ARTIFACT, field: "x"})


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "css", "selector": "#x"},
        {"kind": "label"},
        {"kind": "attr", "attr": "data-testid", "value": "x"},
        {"kind": "attr", "attr": "href", "value": "/members"},
        {"kind": "role_name", "role": "button", "name": "Search", "href": "/x"},
    ],
)
def test_locator_union_is_closed(payload: dict) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Locator).validate_python(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "role_exists", "role": "dialog"},
        {"kind": "dialog_visible"},
        {"kind": "text_visible", "text": "x", "frame": "top"},
        {"kind": "all", "conditions": [{"kind": "nope"}]},
    ],
)
def test_condition_union_is_closed(payload: dict) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Condition).validate_python(payload)


def test_all_condition_nests() -> None:
    cond = TypeAdapter(Condition).validate_python(
        {"kind": "all", "conditions": [{"kind": "all", "conditions": [{"kind": "text_visible", "text": "a"}]}]}
    )
    assert cond.conditions[0].conditions[0].text == "a"


def test_step_action_union_is_closed() -> None:
    with pytest.raises(ValidationError):
        Step.model_validate(
            {"id": "s", "description": "d", "action": {"kind": "navigate", "url": "http://x"}, "target": None, "postcondition": None}
        )
    with pytest.raises(ValidationError):
        Step.model_validate(
            {"id": "s", "description": "d", "action": {"kind": "fill", "value": "12345"}, "target": None, "postcondition": None}
        )
    with pytest.raises(ValidationError):
        Step.model_validate(
            {"id": "s", "description": "d", "action": {"kind": "read", "capture_as": "x", "parser": "regex"}, "target": None, "postcondition": None}
        )


def test_runtime_actions_are_closed_and_navigate_is_runtime_only() -> None:
    adapter = TypeAdapter(RuntimeAction)
    nav = adapter.validate_python({"kind": "navigate", "url": "http://localhost:8000/"})
    assert nav.kind == "navigate"
    fill = adapter.validate_python({"kind": "fill", "ref": "1:1:2", "value": "12345"})
    assert fill.value == "12345"
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "click", "x": 1, "y": 2})
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "click", "ref": "1:1:2", "selector": "#x"})


def test_action_result_and_replay_result_shapes() -> None:
    assert ActionResult(executed=False, error="POLICY_BLOCKED").value is None
    result = ReplayResult(
        kind="failure",
        outputs={},
        business_outcome=None,
        failure={
            "step_id": "s2",
            "code": "POSTCONDITION_FAILED",
            "expected": {"kind": "text_visible", "text": "Member Detail"},
            "observed_summary": "System notice",
            "escalation": "unavailable_headless",
        },
        recovery_count=0,
    )
    assert result.llm_calls == 0
    with pytest.raises(ValidationError):
        ReplayResult(kind="recovered", outputs={}, business_outcome=None, failure=None, recovery_count=1)
    with pytest.raises(ValidationError):
        ReplayResult(kind="success", outputs={}, business_outcome=None, failure=None, recovery_count=0, llm_calls=1)


def test_run_control_owner_enum() -> None:
    assert [o.value for o in Owner] == ["AUTOMATION", "NEEDS_HUMAN", "HUMAN", "COMPLETED"]
    assert RunControl().owner is Owner.AUTOMATION
