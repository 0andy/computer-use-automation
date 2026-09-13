"""In-memory Surface for pure unit tests of the Discovery loop (no browser).

It honours the same authorization/ownership gate as PlaywrightSurface (reusing
``cua.surface.authorization_rejection``) and records every ``act`` call so a
test can prove a rejected proposal never reached ``Surface.act``.

Behaviour is scripted per ref:
  * ``transitions[ref]`` - the Observation shown after that ref is clicked;
  * a fill updates the control's ``input_value`` in a copy of the Observation;
  * a read returns the control's ``input_value`` if present, else its ``text``;
  * ``failures[ref]`` - an error code returned instead of executing;
  * ``stale_once`` - refs that answer STALE_REF the first time they are acted on
    and work afterwards (the Replay stale-ref rule);
  * ``queue`` - Observations handed out by successive ``observe()`` calls before
    the steady ``observation`` (transient / in-flight states for Replay settle).
"""

from __future__ import annotations

from cua.models import (
    ActionResult,
    Observation,
    PolicyDecision,
    RunControl,
    RuntimeAction,
    RuntimeClick,
    RuntimeFill,
    RuntimeNavigate,
    RuntimeRead,
)
from cua.surface import authorization_rejection, rejected


class FakeSurface:
    def __init__(self, initial: Observation, run_control: RunControl | None = None) -> None:
        self.observation = initial
        self.run_control = run_control if run_control is not None else RunControl()
        self.transitions: dict[str, Observation] = {}
        self.failures: dict[str, str] = {}
        self.stale_once: set[str] = set()
        self.queue: list[Observation] = []
        self.acts: list[RuntimeAction] = []  # every action that reached act() (executed or not)
        self.executed: list[RuntimeAction] = []
        self.observe_count = 0

    def observe(self) -> Observation:
        self.observe_count += 1
        if self.queue:
            return self.queue.pop(0)
        return self.observation

    def act(self, action: RuntimeAction, authorization: PolicyDecision) -> ActionResult:
        self.acts.append(action)
        code = authorization_rejection(action, authorization, self.run_control)
        if code is not None:
            return rejected(code)
        if isinstance(action, RuntimeNavigate):
            self.executed.append(action)
            return ActionResult(executed=True)
        if action.ref in self.failures:
            return ActionResult(executed=False, error=self.failures[action.ref])
        if action.ref in self.stale_once:
            self.stale_once.discard(action.ref)
            return rejected("STALE_REF")
        control = self.observation.find(action.ref)
        if control is None:
            return rejected("STALE_REF")
        self.executed.append(action)
        if isinstance(action, RuntimeClick):
            if action.ref in self.transitions:
                self.observation = self.transitions[action.ref]
            return ActionResult(executed=True)
        if isinstance(action, RuntimeFill):
            controls = [c.model_copy(update={"input_value": action.value}) if c.ref == action.ref else c for c in self.observation.controls]
            self.observation = self.observation.model_copy(update={"controls": controls})
            return ActionResult(executed=True)
        if isinstance(action, RuntimeRead):
            value = control.input_value if control.input_value is not None else (control.text or "")
            return ActionResult(executed=True, value=value)
        raise TypeError(action)

    def screenshot(self) -> bytes:
        return b""
