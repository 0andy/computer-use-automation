"""Surface protocol and the authorization gate every automated action must pass.

Core engine code talks only to this protocol and the models in ``cua.models``;
Playwright objects live inside ``cua.playwright_surface`` and never escape it.
"""

from __future__ import annotations

from typing import Protocol

from cua.models import ActionResult, Observation, Owner, PolicyDecision, RunControl, RuntimeAction

# Rejection codes returned by Surface.act as ActionResult.error
MISSING_AUTHORIZATION = "MISSING_AUTHORIZATION"
POLICY_BLOCKED = "POLICY_BLOCKED"
AUTHORIZATION_MISMATCH = "AUTHORIZATION_MISMATCH"
OWNER_NOT_AUTOMATION = "OWNER_NOT_AUTOMATION"
STALE_REF = "STALE_REF"
ACTION_FAILED = "ACTION_FAILED"


class Surface(Protocol):
    def observe(self) -> Observation: ...

    def act(self, action: RuntimeAction, authorization: PolicyDecision) -> ActionResult: ...

    def screenshot(self) -> bytes: ...


def authorization_rejection(
    action: RuntimeAction,
    authorization: PolicyDecision | None,
    run_control: RunControl,
) -> str | None:
    """Return the rejection code for an automated action, or None if it may run.

    This is the single ownership + authorization gate (spec 7 and 15.1):
    the decision must exist, must be allowed, must have been made for exactly
    this action, and the session must currently be owned by AUTOMATION.
    """
    if authorization is None:
        return MISSING_AUTHORIZATION
    if not authorization.allowed:
        return POLICY_BLOCKED
    if authorization.action != action:
        return AUTHORIZATION_MISMATCH
    if run_control.owner is not Owner.AUTOMATION:
        return OWNER_NOT_AUTOMATION
    return None


def rejected(code: str) -> ActionResult:
    return ActionResult(executed=False, error=code)
