"""Deterministic Replay: execute a frozen capability artifact with zero LLM decisions (docs/spec.md 14).

    Upstream agent chooses (app, capability_id, params)
        -> exact artifact lookup (never natural language)
        -> startup navigate through Policy
        -> for each step: settle(target resolvable) -> Policy -> Surface.act -> settle(postcondition)
        -> settle(final success condition)
        -> ReplayResult: success | business_outcome | failure | aborted

The core is the normative ``settle`` loop of spec 14.2, implemented literally with
its ordering business-outcome / ok / known-recovery / deadline. A recovery
repairs the current state and then the loop re-observes and re-assesses; it
never re-executes the previous business action. Waiting is bounded by the
constants below, which are defined here exactly once.

Nothing in this module imports a model client. There is no LLM seam, no
natural-language routing and no fallback: an unknown state simply fails to
settle and is reported as a structured, escalation-eligible failure.

Transient frame states: an Observation taken while a frame is still in flight
(URL empty or ``about:``) or an observe() that fails because a frame is
navigating cannot be assessed - conditions such as ``text_absent`` would be
spuriously true and Policy would deny the frame. Such a tick is "not settled
yet": it counts against the deadline and is polled again. It is never turned
into POLICY_BLOCKED.

HITL (spec 14.5, 15): a step-level unresolved code in HANDOFF_ELIGIBLE either
returns ``failure`` with ``escalation="unavailable_headless"`` (handoff
disabled - every normal headless CLI run) or, when ``handoff_enabled`` and an
Operator is present, hands the *same* live session to the human:

    AUTOMATION -> NEEDS_HUMAN -> HUMAN -> AUTOMATION   (COMPLETED at run end)

    Continue = re-run the same settle (revalidation; the business action is not repeated)
    Retry    = re-resolve the target, Policy, re-execute the step action, then settle
    Abort    = result kind aborted

The handoff loop reuses ``settle`` unchanged. POLICY_BLOCKED never escalates and
FINAL_CHECKPOINT_FAILED is a structured hard failure that never starts a handoff.
Human UI events are captured pull-based while owner == HUMAN and persisted
sanitized (never an input value); the handoff screenshot is persisted only after
opaque masking of known sensitive controls.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cua.catalog import load_artifact
from cua.compiler import CAPABILITIES_DIR, artifact_path
from cua.conditions import Bindings, evaluate
from cua.config import AppConfig
from cua.discovery import PARSERS, money_literal_forms
from cua.evidence import EvidenceWriter, sanitize, sanitize_value
from cua.hitl import (
    HumanEventCapture,
    Intervention,
    NoHumanCapture,
    Operator,
    OperatorDecision,
    Phase,
    mask_screenshot,
    sensitive_controls,
)
from cua.models import (
    Action,
    CapabilityArtifact,
    ClickAction,
    Condition,
    Control,
    ControlRef,
    FillAction,
    Observation,
    Owner,
    PolicyDecision,
    ReadAction,
    Recovery,
    ReplayFailure,
    ReplayResult,
    RunControl,
    RuntimeAction,
    RuntimeClick,
    RuntimeFill,
    RuntimeNavigate,
    RuntimeRead,
    Step,
    Target,
)
from cua.playwright_surface import SurfaceError
from cua.policy import Policy
from cua.resolver import LOCATOR_NOT_FOUND, ResolveFailure, normalize, resolve
from cua.surface import ACTION_FAILED, POLICY_BLOCKED, STALE_REF, Surface

# --------------------------------------------------------------------------- #
# Normative settle constants (spec 14.1) - defined exactly once
# --------------------------------------------------------------------------- #

STEP_TIMEOUT_S = 5.0
POLL_S = 0.25
MAX_RECOVERIES_PER_STEP = 2

# Step-level unresolved codes (spec 14.2, 14.5)
POSTCONDITION_FAILED = "POSTCONDITION_FAILED"
RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"
FINAL_CHECKPOINT_FAILED = "FINAL_CHECKPOINT_FAILED"
HANDOFF_ELIGIBLE = frozenset({LOCATOR_NOT_FOUND, POSTCONDITION_FAILED, RECOVERY_EXHAUSTED})
UNAVAILABLE_HEADLESS = "unavailable_headless"

PARAM_ARG_RE = re.compile(r"^(?P<name>[^=]+)=(?P<value>.*)$", re.DOTALL)
PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SUMMARY_TEXTS = 20


# --------------------------------------------------------------------------- #
# Inputs: exact artifact lookup and parameter binding
# --------------------------------------------------------------------------- #


class ReplayInputError(ValueError):
    """Bad invocation: rejected before any browser starts."""


def load_capability(app: str, capability_id: str, root: Path = CAPABILITIES_DIR) -> CapabilityArtifact:
    """Exact ``(app, capability_id)`` -> ``<root>/<app>/<capability_id>.json``. No search, no intent."""
    if not PARAM_NAME_RE.match(app or "") or not PARAM_NAME_RE.match(capability_id or ""):
        raise ReplayInputError(f"app {app!r} and capability {capability_id!r} must match [a-z][a-z0-9_]*")
    path = artifact_path(app, capability_id, root)
    if not path.is_file():
        raise ReplayInputError(f"no capability {capability_id!r} for app {app!r} (expected {path})")
    try:
        artifact = load_artifact(path)
    except ValueError as exc:
        raise ReplayInputError(str(exc)) from exc
    if artifact.app != app or artifact.capability_id != capability_id:
        raise ReplayInputError(
            f"{path} declares ({artifact.app!r}, {artifact.capability_id!r}), expected ({app!r}, {capability_id!r})"
        )
    return artifact


def parse_replay_params(args: list[str]) -> dict[str, str]:
    """``--param name=value`` (repeatable) -> raw runtime bindings (memory-only)."""
    params: dict[str, str] = {}
    for arg in args:
        match = PARAM_ARG_RE.match(arg)
        if not match:
            raise ReplayInputError(f"--param must look like name=value, got {arg!r}")
        name, value = match.group("name").strip(), match.group("value")
        if not PARAM_NAME_RE.match(name):
            raise ReplayInputError(f"parameter name {name!r} must match [a-z][a-z0-9_]*")
        if name in params:
            raise ReplayInputError(f"parameter {name!r} is given twice")
        params[name] = value
    return params


def bind_params(artifact: CapabilityArtifact, params: dict[str, str]) -> dict[str, str]:
    """Validate bindings against the artifact's typed inputs; every required input, nothing undeclared."""
    unknown = sorted(set(params) - set(artifact.inputs))
    if unknown:
        raise ReplayInputError(f"undeclared parameter(s) {unknown}; declared: {sorted(artifact.inputs)}")
    bound: dict[str, str] = {}
    for name, spec in artifact.inputs.items():
        if name not in params:
            if spec.required:
                raise ReplayInputError(f"missing required parameter {name!r}")
            continue
        value = params[name]
        if spec.type == "string" and value == "":
            raise ReplayInputError(f"parameter {name!r} has an empty value")
        bound[name] = value
    return bound


def sensitive_literals(artifact: CapabilityArtifact, params: dict[str, str]) -> dict[str, list[str]]:
    return {name: [value] for name, value in params.items() if artifact.inputs[name].sensitive}


# --------------------------------------------------------------------------- #
# settle outcomes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Advance:
    pass


@dataclass(frozen=True)
class BusinessOutcomeHit:
    code: str


@dataclass(frozen=True)
class Unresolved:
    code: str
    detail: str | None = None


Settled = Advance | BusinessOutcomeHit | Unresolved

RETRY = "retry"
ADVANCE = "advance"


class _Stop(Exception):
    """Internal unwinding: the run has a final result."""

    def __init__(self, result: ReplayResult) -> None:
        super().__init__(result.kind)
        self.result = result


def in_flight(observation: Observation) -> bool:
    """True if any frame (top-level included) is still navigating / has no committed document."""
    urls = [observation.url, *observation.frames.values()]
    return any(not url or url.startswith("about:") for url in urls)


def observed_summary(observation: Observation | None, redact: dict[ControlRef, str] | None = None) -> str:
    """Compact, structured description of what was on screen (sanitized again when persisted).

    ``redact`` maps refs of known sensitive controls to the token shown instead of
    their text, so a value the sanitizer does not know (for example another
    member's ID after a human navigated away) still never reaches evidence.
    """
    if observation is None:
        return "no observation available"
    texts: list[str] = []
    for control in observation.controls:
        if redact and control.ref in redact:
            text = redact[control.ref]
        else:
            text = normalize(control.text) or normalize(control.name)
        if text and text not in texts:
            texts.append(text)
    frames = ", ".join(f"{path}={url}" for path, url in observation.frames.items())
    shown = " | ".join(texts[:SUMMARY_TEXTS]) + (" | ..." if len(texts) > SUMMARY_TEXTS else "")
    return f"url={observation.url}; frames=[{frames}]; controls={len(observation.controls)}; texts=[{shown}]"


def _control_summary(control: Control | None) -> dict[str, Any] | None:
    if control is None:
        return None
    return {"role": control.role, "name": control.name, "label": control.label, "frame_path": control.frame_path}


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #


@dataclass
class _SettleStats:
    polls: int = 0
    in_flight_polls: int = 0
    recoveries: int = 0


@dataclass
class RecoveryBudget:
    """Known-recovery budget of ONE step attempt (MAX_RECOVERIES_PER_STEP).

    Spec 14.2 writes ``used = 0`` inside ``settle``, but the constant is per step:
    the two settle calls of a step (target resolvable, then postcondition) share
    one budget. A fresh attempt of the step (a new step, or an operator Retry)
    gets a fresh budget; an operator Continue is a continuation and keeps it.
    """

    used: int = 0


class Replay:
    """One deterministic execution of ``artifact`` with ``params`` through Policy + Surface."""

    def __init__(
        self,
        artifact: CapabilityArtifact,
        config: AppConfig,
        policy: Policy,
        surface: Surface,
        params: dict[str, str],
        headed: bool = False,
        handoff_enabled: bool = False,
        operator: Operator | None = None,
        human_capture: HumanEventCapture | None = None,
        run_control: RunControl | None = None,
    ) -> None:
        """``run_control`` must be the one the Surface gates on; by default the surface's own is used.

        ``handoff_enabled`` requires an ``operator``. The CLI enables it only with
        ``--headed`` (ConsoleOperator); tests enable it on a headless surface with a
        ScriptedOperator, which exercises the same state machine and is not a real
        interactive session.
        """
        self.artifact = artifact
        self.config = config
        self.policy = policy
        self.surface = surface
        self.headed = headed
        self.handoff_enabled = handoff_enabled
        self.operator = operator
        if handoff_enabled and operator is None:
            raise ValueError("handoff_enabled requires an Operator")
        self.human_capture: HumanEventCapture = human_capture if human_capture is not None else NoHumanCapture()
        control = run_control if run_control is not None else getattr(surface, "run_control", None)
        self.run_control: RunControl = control if control is not None else RunControl()
        if self.run_control.owner is not Owner.AUTOMATION:
            raise ValueError(f"RunControl owner must be AUTOMATION to start a run, got {self.run_control.owner.value}")
        self.params = bind_params(artifact, params)
        self.bindings = Bindings(params=dict(self.params))
        self.literals: dict[str, list[str]] = sensitive_literals(artifact, self.params)
        self.events: list[dict[str, Any]] = []
        self.human_events: list[dict[str, Any]] = []  # sanitized human UI events (never input values)
        self.interventions: list[dict[str, Any]] = []  # sanitized Intervention records + operator decisions
        self.masked_screenshots: list[tuple[str, Any]] = []  # (file name, masked PIL image); raw never kept
        self.ownership: list[dict[str, str]] = []
        self.handoffs = 0
        self.recovery_count = 0
        self.result: ReplayResult | None = None
        self._started = time.monotonic()
        self.elapsed_ms: int | None = None  # whole-run duration, relative (no wall-clock time is persisted)
        self._observation: Observation | None = None  # latest assessable Observation

    # -- run ----------------------------------------------------------------

    def run(self) -> ReplayResult:
        try:
            self._startup()
            for step in self.artifact.steps:
                self._run_step(step)
            settled = self.settle(None, self._success_true, FINAL_CHECKPOINT_FAILED, phase="final")
            if isinstance(settled, BusinessOutcomeHit):
                raise _Stop(self._business_outcome(settled.code))
            if isinstance(settled, Unresolved):
                raise _Stop(self._failure(None, settled.code, self.artifact.success))
            result = ReplayResult(
                kind="success",
                outputs=dict(self.bindings.outputs),
                business_outcome=None,
                failure=None,
                recovery_count=self.recovery_count,
            )
        except _Stop as stop:
            result = stop.result
        self.result = result
        self.elapsed_ms = int((time.monotonic() - self._started) * 1000)
        self._transfer(Owner.COMPLETED, None, result.kind)
        self._event(
            {
                "actor": "replay",
                "event": "result",
                "kind": result.kind,
                "business_outcome": result.business_outcome,
                "failure_code": result.failure.code if result.failure else None,
                "escalation": result.failure.escalation if result.failure else None,
                "recovery_count": result.recovery_count,
                "handoffs": self.handoffs,
                "llm_calls": result.llm_calls,
            }
        )
        return result

    # -- startup navigate (spec 10.4): the only navigate, authorized by Policy ----

    def _startup(self) -> None:
        navigate = RuntimeNavigate(kind="navigate", url=self.config.entry_url)
        decision = self.policy.check(navigate, None)
        if not decision.allowed:
            self._event({"actor": "runtime", "action": "navigate", "result": "rejected", "error": decision.reason})
            raise _Stop(self._failure(None, POLICY_BLOCKED, None, summary=f"startup navigation: {decision.reason}"))
        result = self.surface.act(navigate, decision)
        if not result.executed:
            self._event({"actor": "runtime", "action": "navigate", "result": "failed", "error": result.error})
            raise _Stop(self._failure(None, ACTION_FAILED, None, summary=f"startup navigation: {result.error}"))
        self._event({"actor": "runtime", "action": "navigate", "url": self.config.entry_url, "result": "executed"})

    # -- one step: settle(target) -> Policy -> act -> settle(postcondition) ----

    def _run_step(self, step: Step) -> None:
        # Each pass = one attempt: settle(target) -> Policy -> act -> settle(postcondition), sharing one
        # recovery budget. An operator Retry starts a new attempt (fresh budget); Continue stays in the pass.
        while True:
            budget = RecoveryBudget()
            if self._settle_phase(step, "before", budget) == RETRY:
                continue
            unresolved = self._act_step(step)  # POLICY_BLOCKED = direct failure; never HITL
            if unresolved is not None:
                # stale ref exhausted (spec 14.4): step-level LOCATOR_NOT_FOUND. Continue and Retry
                # both mean "re-resolve the target, then act": the action never executed.
                self._escalate(step, "act", unresolved.code, None, unresolved.detail)
                continue
            if self._settle_phase(step, "after", budget) == RETRY:
                continue
            return

    def _settle_phase(self, step: Step, phase: Phase, budget: RecoveryBudget) -> str:
        """Settle one step contract; on a handoff-eligible unresolved code, hand off.

        Returns ADVANCE, or RETRY when the operator chose Retry. Continue simply
        re-runs the same settle (revalidation, never the business action).
        """
        if phase == "before":
            ok, code, expected = (lambda obs: self._target_resolvable(step, obs)), LOCATOR_NOT_FOUND, None
        else:
            ok, code, expected = (lambda obs: self._postcondition_true(step, obs)), POSTCONDITION_FAILED, step.postcondition
        while True:
            settled = self.settle(step, ok, code, phase=phase, budget=budget)
            if isinstance(settled, BusinessOutcomeHit):
                raise _Stop(self._business_outcome(settled.code))
            if isinstance(settled, Advance):
                return ADVANCE
            decision = self._escalate(step, phase, settled.code, expected)
            if decision is OperatorDecision.RETRY:
                return RETRY
            # CONTINUE: another settle of the same contract

    def _escalate(self, step: Step, phase: Phase, code: str, expected: Condition | None, summary: str | None = None) -> OperatorDecision:
        """Failure (escalation=unavailable_headless when eligible) unless handoff is enabled and eligible."""
        if not (self.handoff_enabled and code in HANDOFF_ELIGIBLE):
            raise _Stop(self._failure(step.id, code, expected, summary))
        decision = self._handoff(step, phase, code, expected, summary)
        if decision is OperatorDecision.ABORT:
            raise _Stop(self._aborted(step, code))
        return decision

    # -- the handoff itself (spec 15): same session, explicit ownership, sanitized evidence ----

    def _handoff(self, step: Step, phase: Phase, code: str, expected: Condition | None, summary: str | None) -> OperatorDecision:
        assert self.operator is not None
        self.handoffs += 1
        attempt = self.handoffs
        obs = self._observation
        observed = self._summary(obs)
        if summary:
            observed = f"{summary}; {observed}"

        # Masked screenshot: raw bytes live only in this frame; only the masked image is ever persisted.
        masked_name: str | None = None
        try:
            raw = self.surface.screenshot()
        except Exception as exc:  # noqa: BLE001 - evidence must not break the handoff
            self._event({"actor": "replay", "event": "screenshot", "attempt": attempt, "result": "failed", "error": str(exc)})
        else:
            if raw:
                bboxes = [c.bbox for c in obs.controls if c.ref in self._sensitive_refs(obs)] if obs else []
                masked_name = "masked.png" if attempt == 1 else f"masked-{attempt}.png"
                self.masked_screenshots.append((masked_name, mask_screenshot(raw, bboxes)))
                self._event(
                    {"actor": "replay", "event": "screenshot", "attempt": attempt, "result": "masked", "file": masked_name, "masked_regions": len(bboxes)}
                )
            del raw

        self._transfer(Owner.NEEDS_HUMAN, step, code)
        intervention = Intervention(
            app=self.artifact.app,
            capability=self.artifact.capability_id,
            step_id=step.id,
            step_description=step.description,
            phase=phase,
            code=code,
            expected=expected,
            observed_summary=sanitize(observed, self.literals),
            masked_screenshot=masked_name,
            owner=Owner.HUMAN,
        )
        self._event({"actor": "replay", "event": "handoff", "attempt": attempt, "step_id": step.id, "phase": phase, "code": code, "result": "requested"})

        self._transfer(Owner.HUMAN, step, code)
        self.human_capture.begin()
        captured: list[dict[str, Any]] = []
        try:
            decision = self.operator.take_control(intervention)
        finally:
            captured = self.human_capture.end()
            self._record_human_events(attempt, captured)
            self._transfer(Owner.AUTOMATION, step, code)
        self.interventions.append({**intervention.model_dump(mode="json"), "attempt": attempt, "decision": decision.value, "human_events": len(captured)})
        self._event({"actor": "operator", "event": "decision", "attempt": attempt, "step_id": step.id, "decision": decision.value, "human_events": len(captured)})
        return decision

    def _record_human_events(self, attempt: int, captured: list[dict[str, Any]]) -> None:
        for item in captured:
            event = {k: v for k, v in item.items() if k != "value"}  # structurally: no value field survives
            if item.get("kind") in ("input", "change"):
                event["value"] = None
                event["redacted"] = True
            self.human_events.append({"seq": len(self.human_events) + 1, "attempt": attempt, "actor": "human", "owner": Owner.HUMAN.value, **event})

    def _transfer(self, to: Owner, step: Step | None, reason: str) -> None:
        previous = self.run_control.transfer(to)
        record = {"from": previous.value, "to": to.value}
        self.ownership.append(record)
        self._event({"actor": "run_control", "event": "ownership", **record, "step_id": step.id if step else None, "reason": reason})

    def _sensitive_refs(self, obs: Observation) -> dict[ControlRef, str]:
        return sensitive_controls(self.artifact, obs, self.literals)

    def _summary(self, obs: Observation | None) -> str:
        redact = {ref: f"[REDACTED:{name}]" for ref, name in self._sensitive_refs(obs).items()} if obs else None
        return observed_summary(obs, redact)

    # -- the normative loop (spec 14.2) ---------------------------------------

    def settle(
        self,
        step: Step | None,
        ok: Callable[[Observation], bool],
        code: str,
        phase: str,
        budget: RecoveryBudget | None = None,
    ) -> Settled:
        """Bounded wait for ``ok`` with the ordering business-outcome / ok / recovery / deadline.

        ok(obs) before act = target has exactly one resolvable locator
        ok(obs) after act  = postcondition is true
        target None is resolvable; postcondition None is true

        ``budget`` is the step attempt's shared recovery budget (see RecoveryBudget);
        a settle without a step (the final checkpoint) gets its own.
        """
        started = time.monotonic()
        stats = _SettleStats()
        deadline = time.monotonic() + STEP_TIMEOUT_S
        if budget is None:
            budget = RecoveryBudget()
        while True:
            obs = self._observe()
            stats.polls += 1
            if obs is None:
                stats.in_flight_polls += 1  # not settled yet; never a policy denial
            else:
                if bo := self._match_business_outcomes(obs):
                    self._settle_event(step, phase, "business_outcome", stats, started, code=bo)
                    return BusinessOutcomeHit(bo)
                if ok(obs):
                    self._settle_event(step, phase, "advance", stats, started)
                    return Advance()
                if rec := self._match_recoveries(obs):
                    if budget.used == MAX_RECOVERIES_PER_STEP:
                        self._settle_event(step, phase, "unresolved", stats, started, code=RECOVERY_EXHAUSTED)
                        return Unresolved(RECOVERY_EXHAUSTED)
                    recovery, ref = rec
                    self._execute_recovery(step, recovery, ref, obs, phase)
                    budget.used += 1
                    stats.recoveries += 1
                    deadline = time.monotonic() + STEP_TIMEOUT_S
                    continue
            if time.monotonic() >= deadline:
                self._settle_event(step, phase, "unresolved", stats, started, code=code)
                return Unresolved(code)
            time.sleep(POLL_S)

    def _observe(self) -> Observation | None:
        """The current Observation, or None while the UI is mid-navigation (not assessable yet)."""
        try:
            obs = self.surface.observe()
        except SurfaceError:
            return None
        if in_flight(obs):
            return None
        self._observation = obs
        return obs

    def _match_business_outcomes(self, obs: Observation) -> str | None:
        for outcome in self.artifact.business_outcomes:
            if evaluate(outcome.when, obs, self.bindings):
                return outcome.code
        return None

    def _match_recoveries(self, obs: Observation) -> tuple[Recovery, str] | None:
        """First known recovery whose condition holds and whose control resolves exactly once."""
        for recovery in self.artifact.recoveries:
            if not evaluate(recovery.when, obs, self.bindings):
                continue
            if recovery.target is None:
                continue
            ref = resolve(recovery.target, obs)
            if isinstance(ref, ResolveFailure):
                continue
            return recovery, ref
        return None

    def _target_resolvable(self, step: Step, obs: Observation) -> bool:
        return step.target is None or not isinstance(resolve(step.target, obs), ResolveFailure)

    def _postcondition_true(self, step: Step, obs: Observation) -> bool:
        return step.postcondition is None or evaluate(step.postcondition, obs, self.bindings)

    def _success_true(self, obs: Observation) -> bool:
        return evaluate(self.artifact.success, obs, self.bindings)

    # -- recovery: repair state, then the loop re-observes; never redo the business action ----

    def _execute_recovery(self, step: Step | None, recovery: Recovery, ref: str, obs: Observation, phase: str) -> None:
        action = self._runtime_action(recovery.action, ref)
        control = obs.find(ref)
        decision = self.policy.check(action, obs)
        base = {
            "actor": "replay",
            "event": "recovery",
            "code": recovery.code,
            "step_id": step.id if step else None,
            "phase": phase,
            "action": recovery.action.kind,
            "control": _control_summary(control),
        }
        if not decision.allowed:
            self._event({**base, "result": "rejected", "error": decision.reason})
            raise _Stop(self._failure(step.id if step else None, POLICY_BLOCKED, None, summary=decision.reason))
        result = self.surface.act(action, decision)
        self.recovery_count += 1
        if result.executed:
            self._event({**base, "result": "executed"})
        else:
            self._event({**base, "result": "not_executed", "error": result.error})

    # -- step action with the stale-ref rule (spec 14.4) ------------------------

    def _act_step(self, step: Step) -> Unresolved | None:
        """Policy -> Surface.act for the step. Returns Unresolved(LOCATOR_NOT_FOUND) when the ref went stale
        and the single re-observe/re-resolve did not help (the action did not execute)."""
        obs = self._observation
        assert obs is not None  # settle(before) just returned Advance on it
        if step.target is None:
            raise _Stop(self._failure(step.id, ACTION_FAILED, None, summary="step has no target to act on"))
        reobserved = False
        while True:
            ref = resolve(step.target, obs)
            if isinstance(ref, ResolveFailure):
                self._event({"actor": "replay", "event": "stale_ref", "step_id": step.id, "result": "unresolved"})
                return Unresolved(LOCATOR_NOT_FOUND, ref.detail)
            action = self._runtime_action(step.action, ref)
            control = obs.find(ref)
            decision = self.policy.check(action, obs)
            if not decision.allowed:
                self._event(self._action_event(step, control, "rejected", error=decision.reason))
                raise _Stop(self._failure(step.id, POLICY_BLOCKED, None, summary=decision.reason))
            result = self.surface.act(action, decision)
            if result.executed:
                self._after_executed(step, control, result.value)
                return None
            if result.error == STALE_REF and not reobserved:
                reobserved = True  # exactly one re-observe + re-resolve
                self._event({"actor": "replay", "event": "stale_ref", "step_id": step.id, "result": "re-observe"})
                try:
                    obs = self.surface.observe()
                except SurfaceError as exc:
                    return Unresolved(LOCATOR_NOT_FOUND, str(exc))
                self._observation = obs
                continue
            if result.error == STALE_REF:
                self._event({"actor": "replay", "event": "stale_ref", "step_id": step.id, "result": "unresolved"})
                return Unresolved(LOCATOR_NOT_FOUND, "ref stale again after re-resolve")
            self._event(self._action_event(step, control, "failed", error=result.error))
            raise _Stop(self._failure(step.id, ACTION_FAILED, None, summary=result.error or "not executed"))

    def _runtime_action(self, action: Action, ref: str) -> RuntimeAction:
        if isinstance(action, ClickAction):
            return RuntimeClick(kind="click", ref=ref)
        if isinstance(action, FillAction):
            return RuntimeFill(kind="fill", ref=ref, value=self.params[action.value.name])
        if isinstance(action, ReadAction):
            return RuntimeRead(kind="read", ref=ref)
        raise TypeError(f"unknown action {action!r}")  # unreachable: closed union

    def _after_executed(self, step: Step, control: Control | None, raw_value: str | None) -> None:
        action = step.action
        if isinstance(action, ReadAction):
            raw_text = raw_value or ""
            try:
                parsed = PARSERS[action.parser](raw_text)
            except ValueError:
                self._event(
                    self._action_event(step, control, "executed", output_captured=False, error=f"not a {action.parser} value")
                )
                return
            self.bindings.outputs[action.capture_as] = parsed
            self.literals[action.capture_as] = money_literal_forms(raw_text, parsed)
            self._event(self._action_event(step, control, "executed", output_captured=True))
            return
        self._event(self._action_event(step, control, "executed"))

    def _action_event(self, step: Step, control: Control | None, result: str, **extra: Any) -> dict[str, Any]:
        event: dict[str, Any] = {
            "actor": "replay",
            "action": step.action.kind,
            "step_id": step.id,
            "description": step.description,
            "control": _control_summary(control),
            "result": result,
        }
        if isinstance(step.action, FillAction):
            event["value"] = {"kind": "parameter", "name": step.action.value.name}
        if isinstance(step.action, ReadAction):
            event.update(capture_as=step.action.capture_as, parser=step.action.parser, value=None, redacted=True)
        event.update(extra)
        return event

    # -- results ----------------------------------------------------------------

    def _business_outcome(self, code: str) -> ReplayResult:
        self._event({"actor": "replay", "event": "business_outcome", "code": code})
        return ReplayResult(
            kind="business_outcome",
            outputs=dict(self.bindings.outputs),
            business_outcome=code,
            failure=None,
            recovery_count=self.recovery_count,
        )

    def _failure(self, step_id: str | None, code: str, expected: Condition | None, summary: str | None = None) -> ReplayResult:
        observed = self._summary(self._observation)
        if summary:
            observed = f"{summary}; {observed}"
        # Eligible codes only reach here when handoff is disabled (headless run): say so.
        escalation = UNAVAILABLE_HEADLESS if code in HANDOFF_ELIGIBLE and not self.handoff_enabled else None
        return ReplayResult(
            kind="failure",
            outputs=dict(self.bindings.outputs),
            business_outcome=None,
            failure=ReplayFailure(step_id=step_id, code=code, expected=expected, observed_summary=observed, escalation=escalation),
            recovery_count=self.recovery_count,
        )

    def _aborted(self, step: Step, code: str) -> ReplayResult:
        """Operator chose Abort: result kind ``aborted``; the context lives in the intervention record."""
        self._event({"actor": "replay", "event": "aborted", "step_id": step.id, "code": code})
        return ReplayResult(
            kind="aborted",
            outputs=dict(self.bindings.outputs),
            business_outcome=None,
            failure=None,
            recovery_count=self.recovery_count,
        )

    # -- events / evidence --------------------------------------------------------

    def _event(self, event: dict[str, Any]) -> None:
        self.events.append({"seq": len(self.events) + 1, **event})

    def _settle_event(self, step: Step | None, phase: str, outcome: str, stats: _SettleStats, started: float, code: str | None = None) -> None:
        self._event(
            {
                "actor": "replay",
                "event": "settle",
                "step_id": step.id if step else None,
                "phase": phase,
                "outcome": outcome,
                "code": code,
                "polls": stats.polls,
                "in_flight_polls": stats.in_flight_polls,
                "recoveries": stats.recoveries,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        )

    def meta(self) -> dict[str, Any]:
        result = self.result
        return {
            "app": self.artifact.app,
            "capability_id": self.artifact.capability_id,
            "revision": self.artifact.revision,
            "description": self.artifact.description,
            "params": {name: {"type": spec.type, "sensitive": spec.sensitive} for name, spec in self.artifact.inputs.items()},
            "outputs": {name: {"type": spec.type, "sensitive": spec.sensitive} for name, spec in self.artifact.outputs.items()},
            "steps": [step.id for step in self.artifact.steps],
            "headed": self.headed,
            "handoff_enabled": self.handoff_enabled,
            "operator": type(self.operator).__name__ if self.operator is not None else None,
            "handoffs": self.handoffs,
            "ownership": list(self.ownership),
            "final_owner": self.run_control.owner.value,
            "human_event_count": len(self.human_events),
            "llm_calls": 0,
            "model": None,
            "model_call_count": 0,
            "constants": {
                "step_timeout_s": STEP_TIMEOUT_S,
                "poll_s": POLL_S,
                "max_recoveries_per_step": MAX_RECOVERIES_PER_STEP,
            },
            "result_kind": result.kind if result else None,
            "business_outcome": result.business_outcome if result else None,
            "failure_code": result.failure.code if result and result.failure else None,
            "recovery_count": self.recovery_count,
            "elapsed_ms": self.elapsed_ms,
        }

    def persisted_result(self, result: ReplayResult) -> dict[str, Any]:
        """``result.json`` content: sensitive outputs become structured markers; text is sanitized."""
        data = result.model_dump(mode="json")
        outputs: dict[str, Any] = {}
        for name, value in result.outputs.items():
            spec = self.artifact.outputs.get(name)
            if spec is None or spec.sensitive:
                outputs[name] = {"redacted": True, "type": spec.type if spec else "unknown"}
            else:
                outputs[name] = value
        data["outputs"] = outputs
        return sanitize_value(data, self.literals)


def write_evidence(replay: Replay, result: ReplayResult, directory: Path) -> Path:
    """``meta.json`` + ``events.jsonl`` + ``result.json`` into ``directory`` (all sanitized).

    A run that handed off also gets ``intervention.json``, ``human-events.jsonl`` and the
    masked screenshot(s) (``masked.png``); the raw screenshot is never written.
    """
    writer = EvidenceWriter(directory, replay.literals)
    writer.write_meta(replay.meta())
    writer.write_events(replay.events)
    result_path = writer.directory / "result.json"
    result_path.write_text(json.dumps(replay.persisted_result(result), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if replay.interventions:
        interventions = sanitize_value(replay.interventions, replay.literals)
        (writer.directory / "intervention.json").write_text(json.dumps(interventions, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with (writer.directory / "human-events.jsonl").open("w", encoding="utf-8") as fh:
            for event in replay.human_events:
                fh.write(json.dumps(sanitize_value(dict(event), replay.literals), sort_keys=True) + "\n")
        for name, image in replay.masked_screenshots:
            image.save(writer.directory / name, format="PNG")
    return writer.directory
