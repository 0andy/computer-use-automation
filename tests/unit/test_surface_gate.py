"""The authorization/ownership gate every Surface.act call passes (docs/spec.md 7, 15.1)."""

from __future__ import annotations

import pytest

from cua.models import Owner, PolicyDecision, RunControl, RuntimeClick, RuntimeFill
from cua.surface import (
    AUTHORIZATION_MISMATCH,
    MISSING_AUTHORIZATION,
    OWNER_NOT_AUTOMATION,
    POLICY_BLOCKED,
    authorization_rejection,
)

CLICK = RuntimeClick(kind="click", ref="1:1:3")


def test_allowed_decision_for_same_action_under_automation_passes() -> None:
    decision = PolicyDecision(allowed=True, reason="allowed", action=CLICK)
    assert authorization_rejection(CLICK, decision, RunControl()) is None


def test_missing_authorization_is_rejected() -> None:
    assert authorization_rejection(CLICK, None, RunControl()) == MISSING_AUTHORIZATION


def test_denied_authorization_is_rejected() -> None:
    decision = PolicyDecision(allowed=False, reason="Close Account is irreversible", action=CLICK)
    assert authorization_rejection(CLICK, decision, RunControl()) == POLICY_BLOCKED


def test_authorization_for_a_different_action_is_rejected() -> None:
    other = PolicyDecision(allowed=True, reason="allowed", action=RuntimeClick(kind="click", ref="1:1:4"))
    assert authorization_rejection(CLICK, other, RunControl()) == AUTHORIZATION_MISMATCH
    fill_a = RuntimeFill(kind="fill", ref="1:1:2", value="12345")
    fill_b = RuntimeFill(kind="fill", ref="1:1:2", value="99999")
    decision = PolicyDecision(allowed=True, reason="allowed", action=fill_a)
    assert authorization_rejection(fill_b, decision, RunControl()) == AUTHORIZATION_MISMATCH


@pytest.mark.parametrize("owner", [Owner.NEEDS_HUMAN, Owner.HUMAN, Owner.COMPLETED])
def test_automation_rejected_unless_owner_is_automation(owner: Owner) -> None:
    decision = PolicyDecision(allowed=True, reason="allowed", action=CLICK)
    assert authorization_rejection(CLICK, decision, RunControl(owner=owner)) == OWNER_NOT_AUTOMATION
