"""LLM Discovery: the only model-decision loop inside CUA (docs/spec.md 8, 9, 12).

    observe -> LLM decide -> validate tool proposal -> Policy -> Surface.act
            -> verify / observe -> repeat / done / give_up

What lives here:

* CLI-facing validation that runs BEFORE any browser or model starts:
  capability id, ``--param name:type=value`` parsing, goal placeholders.
* The exact five model-facing tool schemas (spec 8.3) and the model prompt,
  which carries the goal with placeholders plus parameter names/types - never a
  raw parameter value. Every string shown to the model is sanitized.
* Validation order for every proposal, before any physical action:
  schema -> sensitive-literal check on expect/done strings -> model-facing
  condition vocabulary (text_visible/text_absent/url_matches/all only)
  -> Policy.check -> Surface.act.
* The in-memory ``DiscoveryRecord``: every actually executed physical action in
  order with its pre-action Observation and verification result, parameter
  bindings, read captures (raw values runtime-only), the accepted Done
  condition, model-call audit metadata, and the current sensitive literals.
* Automatic postconditions: fill -> value_equals_parameter(target, param);
  read -> output_present(capture_as); only click carries a model ``expect``.
* Executed-action rule: an action that reached Surface.act and executed stays
  recorded even when its expect fails (result=executed, expect_verified=false).
* Bounded Policy rejection: first rejected proposal returns a sanitized reason
  to the model; a second one stops the run with ``policy_blocked``.

Discovery is NOT wired to human handoff (spec 8.8): ``give_up`` ends the run.
Compilation into an artifact is Phase 3 and is not done here.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from cua.conditions import Bindings, evaluate
from cua.config import AppConfig
from cua.evidence import Literals, find_sensitive, flat_literals, sanitize, sanitize_value
from cua.model_client import ModelClient, ModelReply, ToolCall
from cua.models import (
    ActionResult,
    AllCondition,
    Condition,
    Control,
    Observation,
    OutputPresent,
    ParameterValue,
    PolicyDecision,
    RuntimeAction,
    RuntimeClick,
    RuntimeFill,
    RuntimeNavigate,
    RuntimeRead,
    Target,
    ValueEqualsParameter,
)
from cua.policy import Policy
from cua.resolver import locator_candidates, normalize
from cua.surface import Surface

CAPABILITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
PARAM_ARG_RE = re.compile(r"^(?P<name>[^:=]+):(?P<type>[^=]+)=(?P<value>.*)$", re.DOTALL)

REASON_MAX_LEN = 120
MODEL_CONDITION_KINDS = frozenset({"text_visible", "text_absent", "url_matches", "all"})
GIVE_UP_CODES = ("goal_unreachable", "blocked_by_unknown_state", "missing_information")
STOP_REASONS = ("goal_completed", "max_steps", "timeout", "dead_end", "give_up", "policy_blocked")

DEAD_END_ROUNDS = 3  # consecutive rounds with no executed progress and no Observation change
POLICY_REJECTION_LIMIT = 2  # second policy-rejected proposal stops the run
SETTLE_TIMEOUT_S = 5.0  # bounded wait for a click's expect after the click executed
SETTLE_POLL_S = 0.25


# --------------------------------------------------------------------------- #
# Pre-flight validation (spec 3.3, 8.1) - runs before browser/model startup
# --------------------------------------------------------------------------- #


class DiscoveryInputError(ValueError):
    """Bad CLI input: rejected before any browser or model starts."""


class DiscoveryParam(BaseModel):
    """One ``--param name:type=value``: typed input contract + runtime binding."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["string"]
    value: str = Field(repr=False)  # raw runtime binding; memory-only
    sensitive: bool = True  # demo banking params are sensitive by default


def validate_capability_id(capability_id: str) -> str:
    if not CAPABILITY_ID_RE.match(capability_id or ""):
        raise DiscoveryInputError(f"capability id {capability_id!r} must match [a-z][a-z0-9_]*")
    return capability_id


def parse_param(arg: str) -> DiscoveryParam:
    match = PARAM_ARG_RE.match(arg)
    if not match:
        raise DiscoveryInputError(f"--param must look like name:type=value, got {arg!r}")
    name, type_, value = match.group("name").strip(), match.group("type").strip(), match.group("value")
    if not PARAM_NAME_RE.match(name):
        raise DiscoveryInputError(f"parameter name {name!r} must match [a-z][a-z0-9_]*")
    if value == "":
        raise DiscoveryInputError(f"parameter {name!r} has an empty value")
    try:
        return DiscoveryParam(name=name, type=type_, value=value)  # type: ignore[arg-type]
    except ValidationError:
        raise DiscoveryInputError(f"parameter {name!r} has unsupported type {type_!r}; only 'string'") from None


def parse_params(args: list[str]) -> dict[str, DiscoveryParam]:
    params: dict[str, DiscoveryParam] = {}
    for arg in args:
        param = parse_param(arg)
        if param.name in params:
            raise DiscoveryInputError(f"parameter {param.name!r} is declared twice")
        params[param.name] = param
    return params


def validate_goal(goal: str, params: dict[str, DiscoveryParam]) -> str:
    """Every ``{placeholder}`` in the goal must name a declared parameter."""
    if not goal or not goal.strip():
        raise DiscoveryInputError("goal must not be empty")
    for placeholder in PLACEHOLDER_RE.findall(goal):
        if placeholder not in params:
            raise DiscoveryInputError(
                f"goal placeholder {{{placeholder}}} is not a declared parameter; declared: {sorted(params) or 'none'}"
            )
    return goal


# --------------------------------------------------------------------------- #
# Model-facing tool schemas (spec 8.3) - exactly these five
# --------------------------------------------------------------------------- #

_MODEL_CONDITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "A condition over the current page. Kinds: text_visible{text}, text_absent{text}, "
        "url_matches{pattern} (regex over every frame URL), all{conditions}. "
        "Never put a parameter value or a captured output value into a condition."
    ),
    "properties": {
        "kind": {"type": "string", "enum": sorted(MODEL_CONDITION_KINDS)},
        "text": {"type": "string"},
        "pattern": {"type": "string"},
        "conditions": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["kind"],
}

_REASON_SCHEMA: dict[str, Any] = {
    "type": "string",
    "maxLength": REASON_MAX_LEN,
    "description": f"Concise action justification, at most {REASON_MAX_LEN} characters. Not reasoning.",
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "click_ref",
        "description": "Click the control with this ref. Supply the condition you expect to hold afterwards.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "A ref from the current observation."},
                "expect": _MODEL_CONDITION_SCHEMA,
                "reason": _REASON_SCHEMA,
            },
            "required": ["ref", "expect", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fill_ref",
        "description": "Fill the input control with this ref using the value of a declared parameter.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "A ref from the current observation."},
                "from_param": {"type": "string", "description": "Name of a declared parameter."},
                "reason": _REASON_SCHEMA,
            },
            "required": ["ref", "from_param", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_ref",
        "description": "Read the text of the control with this ref, parse it, and capture it as a named output.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "A ref from the current observation."},
                "capture_as": {"type": "string", "description": "Output name, e.g. savings_balance."},
                "parser": {"type": "string", "enum": ["money"]},
                "reason": _REASON_SCHEMA,
            },
            "required": ["ref", "capture_as", "parser", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "done",
        "description": (
            "Declare the goal accomplished. The condition must already be true on the current page "
            "and must be reusable for any parameter value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"condition": _MODEL_CONDITION_SCHEMA, "reason": _REASON_SCHEMA},
            "required": ["condition", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "give_up",
        "description": "Stop: the goal cannot be accomplished from here.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason_code": {"type": "string", "enum": list(GIVE_UP_CODES)},
                "reason": _REASON_SCHEMA,
            },
            "required": ["reason_code", "reason"],
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = tuple(tool["name"] for tool in TOOLS)


class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(max_length=REASON_MAX_LEN)


class ClickRefInput(_ToolInput):
    ref: str
    expect: dict[str, Any]


class FillRefInput(_ToolInput):
    ref: str
    from_param: str


class ReadRefInput(_ToolInput):
    ref: str
    capture_as: str
    parser: Literal["money"]


class DoneInput(_ToolInput):
    condition: dict[str, Any]


class GiveUpInput(_ToolInput):
    reason_code: Literal["goal_unreachable", "blocked_by_unknown_state", "missing_information"]


_TOOL_INPUTS: dict[str, type[_ToolInput]] = {
    "click_ref": ClickRefInput,
    "fill_ref": FillRefInput,
    "read_ref": ReadRefInput,
    "done": DoneInput,
    "give_up": GiveUpInput,
}

_CONDITION_ADAPTER: TypeAdapter[Condition] = TypeAdapter(Condition)


class ProposalRejected(Exception):
    """A model proposal failed validation; the (sanitized) message goes back to the model."""


def _condition_kinds(condition: Condition) -> list[str]:
    kinds = [condition.kind]
    if isinstance(condition, AllCondition):
        for child in condition.conditions:
            kinds.extend(_condition_kinds(child))
    return kinds


def _condition_strings(condition: Condition) -> list[str]:
    strings: list[str] = []
    for value in condition.model_dump().values():
        if isinstance(value, str):
            strings.append(value)
    if isinstance(condition, AllCondition):
        for child in condition.conditions:
            strings.extend(_condition_strings(child))
    return strings


def validate_model_condition(raw: Any, literals: Literals, what: str) -> Condition:
    """Model-proposed condition: schema -> sensitive-literal check -> model-facing vocabulary (spec 8.5)."""
    try:
        condition = _CONDITION_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        raise ProposalRejected(f"{what} is not a valid condition: {_first_error(exc)}") from None
    for text in _condition_strings(condition):
        hit = find_sensitive(text, literals)
        if hit is not None:
            raise ProposalRejected(
                f"{what} contains the sensitive value of {hit!r}; conditions must not embed parameter or output values"
            )
    for kind in _condition_kinds(condition):
        if kind not in MODEL_CONDITION_KINDS:
            raise ProposalRejected(
                f"{what} uses condition kind {kind!r}; only {', '.join(sorted(MODEL_CONDITION_KINDS))} may be proposed"
            )
    return condition


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ()))
    return f"{loc}: {err.get('msg')}" if loc else str(err.get("msg"))


# --------------------------------------------------------------------------- #
# Parsers (spec 8.3) - only ``money``
# --------------------------------------------------------------------------- #

_MONEY_RE = re.compile(r"^(?P<sign>[-+])?\$?\s*(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)$")


def parse_money(text: str) -> str:
    """``"$1,234.56"`` -> ``"1234.56"`` (canonical decimal string). Raises ValueError otherwise."""
    match = _MONEY_RE.match(normalize(text))
    if not match:
        raise ValueError("not a money amount")
    number = match.group("num").replace(",", "")
    try:
        value = Decimal(number)
    except InvalidOperation:  # pragma: no cover - regex already guarantees a decimal
        raise ValueError("not a money amount") from None
    if match.group("sign") == "-":
        value = -value
    return format(value.quantize(Decimal("0.01")), "f")


PARSERS = {"money": parse_money}


def money_literal_forms(raw_text: str, parsed: str) -> list[str]:
    """Every textual form of a captured money value that must never persist."""
    forms = {normalize(raw_text), parsed}
    stripped = normalize(raw_text).lstrip("$+-").strip()
    forms.add(stripped)
    forms.add(stripped.replace(",", ""))
    return [f for f in forms if f and any(ch.isdigit() for ch in f)]


# --------------------------------------------------------------------------- #
# DiscoveryRecord (spec 8.6, 9.1, prompt 2.11) - in memory only
# --------------------------------------------------------------------------- #


@dataclass
class ModelCallAudit:
    message_id: str
    model: str
    input_tokens: int
    output_tokens: int


@dataclass
class ExecutedAction:
    """One physical action that reached Surface.act and executed."""

    index: int
    kind: Literal["click", "fill", "read"]
    ref: str  # ephemeral; never enters an artifact
    control: Control  # snapshot of the acted-on control from the pre-action Observation
    pre_observation: Observation
    post_observation: Observation
    reason: str  # raw model text; sanitized on persist
    result: Literal["executed"] = "executed"
    # click
    expect: Condition | None = None
    expect_verified: bool | None = None
    # fill
    from_param: str | None = None
    postcondition: Condition | None = None  # runtime-created (spec 8.4)
    postcondition_verified: bool | None = None
    # read
    capture_as: str | None = None
    parser: str | None = None
    output_captured: bool | None = None


@dataclass
class DiscoveryRecord:
    app: str
    capability_id: str
    goal: str
    params: dict[str, DiscoveryParam]
    model: str
    max_steps: int
    timeout_s: float
    bindings: Bindings = field(default_factory=Bindings)
    actions: list[ExecutedAction] = field(default_factory=list)
    output_literal_forms: dict[str, list[str]] = field(default_factory=dict)
    read_raw_text: dict[str, str] = field(default_factory=dict)  # capture_as -> raw text (runtime only)
    model_calls: list[ModelCallAudit] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    done_condition: Condition | None = None  # accepted Done, already ANDed with output_present
    final_observation: Observation | None = None
    stop_reason: str | None = None
    stop_detail: str | None = None
    give_up_code: str | None = None
    rounds: int = 0
    elapsed_ms: int | None = None  # whole-run duration, relative (no wall-clock time is persisted)

    @property
    def literals(self) -> Literals:
        """Current known sensitive literals: every sensitive param value and every captured output."""
        known: dict[str, list[str]] = {}
        for name, param in self.params.items():
            if param.sensitive:
                known[name] = [param.value]
        for name, forms in self.output_literal_forms.items():
            known[name] = list(forms)
        return known

    @property
    def outputs(self) -> dict[str, str]:
        """Raw captured outputs (runtime memory / return value only)."""
        return dict(self.bindings.outputs)

    def executed_actions(self) -> list[ExecutedAction]:
        return list(self.actions)

    def record_event(self, event: dict[str, Any]) -> None:
        self.events.append(sanitize_value({"seq": len(self.events) + 1, **event}, self.literals))

    def meta(self) -> dict[str, Any]:
        """Discovery meta.json content (spec 16.2): proves the model calls without a transcript."""
        return {
            "app": self.app,
            "capability_id": self.capability_id,
            "goal": self.goal,
            "params": {name: {"type": p.type, "sensitive": p.sensitive} for name, p in self.params.items()},
            "outputs": {name: {"parser": a.parser} for name, a in self._captures().items()},
            "model": self.model,
            "model_call_count": len(self.model_calls),
            "message_ids": [call.message_id for call in self.model_calls],
            "usage": {
                "input_tokens": sum(call.input_tokens for call in self.model_calls),
                "output_tokens": sum(call.output_tokens for call in self.model_calls),
            },
            "model_calls": [vars(call) for call in self.model_calls],
            "rounds": self.rounds,
            "max_steps": self.max_steps,
            "timeout_s": self.timeout_s,
            "executed_actions": len(self.actions),
            "stop_reason": self.stop_reason,
            "stop_detail": self.stop_detail,
            "give_up_code": self.give_up_code,
            "elapsed_ms": self.elapsed_ms,
        }

    def _captures(self) -> dict[str, ExecutedAction]:
        return {a.capture_as: a for a in self.actions if a.kind == "read" and a.output_captured and a.capture_as}


# --------------------------------------------------------------------------- #
# Model-facing rendering (spec 3.2, 8.1): names/types, never raw values
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You are the discovery agent of a computer-use automation system. You operate a legacy web application through a semantic observation of its controls, one tool call per turn, to accomplish a goal that will later be replayed deterministically for other parameter values.

Rules:
- Call exactly one tool per turn. Use only refs from the CURRENT observation; refs change every turn.
- fill_ref takes the value from a named parameter; you never see or type parameter values yourself.
- Only click_ref needs an expected condition. Choose conditions that hold for ANY parameter value (page headings, labels, fixed messages, URL patterns), never the specific member or amount.
- read_ref parses a value from a control and captures it as a named output. Read every value the goal asks for before calling done.
- Take the shortest path: no exploratory clicks, no repeated actions, no navigation away from the task.
- If an unknown page or dialog blocks you and no safe control leads onward, call give_up with the fitting reason_code.
- Never click controls that look irreversible (for example closing or deleting an account).
- reason is a short justification (max 120 characters), not your reasoning process."""


def render_params(params: dict[str, DiscoveryParam]) -> str:
    if not params:
        return "  (none)"
    return "\n".join(f"  {name}: {param.type}" + ("  (sensitive)" if param.sensitive else "") for name, param in params.items())


def render_control(control: Control) -> str:
    parts = [f"[{control.ref}]", control.role]
    if control.name is not None:
        parts.append(f"name={control.name!r}")
    if control.label is not None:
        parts.append(f"label={control.label!r}")
    if control.text is not None and control.text != control.name:
        parts.append(f"text={control.text!r}")
    if control.input_value is not None:
        parts.append(f"value={control.input_value!r}")
    for attr in ("id", "name", "type"):
        if control.attrs.get(attr):
            parts.append(f"{attr}={control.attrs[attr]!r}")
    if control.table_index is not None:
        parts.append(f"table={control.table_index}/{control.row_index}/{control.col_index}")
    return " ".join(parts)


def render_observation(observation: Observation, literals: Literals) -> str:
    """Compact model-facing Observation. Every string is sanitized against known literals."""
    lines = [f"url: {observation.url}", "frames:"]
    for path, url in observation.frames.items():
        lines.append(f"  {path}: {url}")
    lines.append(f"controls ({len(observation.controls)}):")
    for control in observation.controls:
        lines.append(f"  {control.frame_path}  {render_control(control)}")
    return sanitize("\n".join(lines), literals)


def render_initial_prompt(goal: str, params: dict[str, DiscoveryParam], observation: Observation, literals: Literals) -> str:
    """The first user turn: goal with placeholders + parameter names/types + observation. No raw values."""
    text = (
        f"Goal: {goal}\n\n"
        f"Parameters (name: type) - values are bound at runtime and are never shown to you:\n{render_params(params)}\n\n"
        f"Current observation:\n{render_observation(observation, literals)}"
    )
    return sanitize(text, literals)


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def observation_signature(observation: Observation) -> tuple:
    """Content of an Observation without ephemeral refs/bboxes, for change detection."""
    return (
        observation.url,
        tuple(sorted(observation.frames.items())),
        tuple(
            (
                c.frame_path,
                c.role,
                c.name,
                c.label,
                c.text,
                c.input_value,
                tuple(sorted(c.attrs.items())),
                c.table_index,
                c.row_index,
                c.col_index,
            )
            for c in observation.controls
        ),
    )


def _control_summary(control: Control) -> dict[str, Any]:
    return {"role": control.role, "name": control.name, "label": control.label, "frame_path": control.frame_path}


@dataclass
class _Outcome:
    """What one handled tool call sends back to the model."""

    text: str
    is_error: bool = False
    progress: bool = False  # an action executed and something meaningful changed
    stop: str | None = None  # a stop reason that ends the run


class Discovery:
    def __init__(
        self,
        *,
        config: AppConfig,
        policy: Policy,
        surface: Surface,
        model: ModelClient,
        capability_id: str,
        goal: str,
        params: dict[str, DiscoveryParam],
        max_steps: int = 12,
        timeout_s: float = 60.0,
        settle_timeout_s: float = SETTLE_TIMEOUT_S,
        settle_poll_s: float = SETTLE_POLL_S,
    ) -> None:
        validate_capability_id(capability_id)
        validate_goal(goal, params)
        if max_steps < 1:
            raise DiscoveryInputError("--max-steps must be at least 1")
        if timeout_s <= 0:
            raise DiscoveryInputError("--timeout must be positive")
        self.config = config
        self.policy = policy
        self.surface = surface
        self.model = model
        self.record = DiscoveryRecord(
            app=config.app,
            capability_id=capability_id,
            goal=goal,
            params=params,
            model=model.model,
            max_steps=max_steps,
            timeout_s=timeout_s,
            bindings=Bindings(params={name: p.value for name, p in params.items()}),
        )
        self._settle_timeout = settle_timeout_s
        self._settle_poll = settle_poll_s
        self._messages: list[dict[str, Any]] = []  # runtime memory only; never persisted
        self._policy_rejections = 0
        self._no_progress_rounds = 0
        self._observation: Observation | None = None

    # -- public -----------------------------------------------------------------

    def run(self) -> DiscoveryRecord:
        record = self.record
        started = time.monotonic()
        deadline = started + record.timeout_s
        try:
            self._startup()
            assert self._observation is not None
            self._messages.append(
                {"role": "user", "content": render_initial_prompt(record.goal, record.params, self._observation, record.literals)}
            )
            while True:
                if record.rounds >= record.max_steps:
                    self._stop("max_steps", f"{record.max_steps} model rounds used")
                    break
                if time.monotonic() >= deadline:
                    self._stop("timeout", f"{record.timeout_s:g}s elapsed")
                    break
                reply = self._decide()
                outcome = self._handle(reply)
                self._feedback(reply, outcome)
                if outcome.stop:
                    break
                if outcome.progress:
                    self._no_progress_rounds = 0
                else:
                    self._no_progress_rounds += 1
                    if self._no_progress_rounds >= DEAD_END_ROUNDS:
                        self._stop("dead_end", f"{DEAD_END_ROUNDS} consecutive rounds without progress")
                        break
        finally:
            record.final_observation = self._observation
            record.elapsed_ms = int((time.monotonic() - started) * 1000)
        return record

    # -- startup (spec 10.4): one Policy-authorized navigation to entry_url ---------

    def _startup(self) -> None:
        navigate = RuntimeNavigate(kind="navigate", url=self.config.entry_url)
        decision = self.policy.check(navigate, None)
        if not decision.allowed:
            self.record.record_event({"actor": "runtime", "action": "navigate", "result": "rejected", "error": decision.reason})
            raise RuntimeError(f"POLICY_BLOCKED: startup navigation: {decision.reason}")
        result = self.surface.act(navigate, decision)
        if not result.executed:
            self.record.record_event({"actor": "runtime", "action": "navigate", "result": "failed", "error": result.error})
            raise RuntimeError(f"startup navigation failed: {result.error}")
        self.record.record_event({"actor": "runtime", "action": "navigate", "url": self.config.entry_url, "result": "executed"})
        self._observation = self.surface.observe()

    # -- decide ---------------------------------------------------------------------

    def _decide(self) -> ModelReply:
        record = self.record
        reply = self.model.create(SYSTEM_PROMPT, self._messages, TOOLS)
        record.rounds += 1
        audit = ModelCallAudit(reply.message_id, reply.model, reply.input_tokens, reply.output_tokens)
        record.model_calls.append(audit)
        record.record_event({"actor": "runtime", "event": "model_call", "round": record.rounds, **vars(audit)})
        return reply

    def _feedback(self, reply: ModelReply, outcome: _Outcome) -> None:
        """Echo the assistant turn and the sanitized tool result; then the fresh observation."""
        content = reply.content if reply.content is not None else _fake_content(reply)
        self._messages.append({"role": "assistant", "content": content})
        text = sanitize(outcome.text, self.record.literals)
        if not outcome.stop and self._observation is not None:
            text += "\n\nCurrent observation:\n" + render_observation(self._observation, self.record.literals)
        if reply.tool_calls:
            results: list[dict[str, Any]] = []
            for i, call in enumerate(reply.tool_calls):
                if i == 0:
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": outcome.is_error})
                else:
                    results.append(
                        {"type": "tool_result", "tool_use_id": call.id, "content": "Ignored: call exactly one tool per turn.", "is_error": True}
                    )
            self._messages.append({"role": "user", "content": results})
        else:
            self._messages.append({"role": "user", "content": text})

    # -- handle one proposal --------------------------------------------------------

    def _handle(self, reply: ModelReply) -> _Outcome:
        if not reply.tool_calls:
            self.record.record_event({"actor": "model", "action": None, "result": "rejected", "error": "no tool call"})
            return _Outcome("You must call exactly one tool.", is_error=True)
        call = reply.tool_calls[0]
        try:
            return self._dispatch(call)
        except ProposalRejected as exc:
            reason = sanitize(str(exc), self.record.literals)
            self.record.record_event(
                {
                    "actor": "model",
                    "action": _action_name(call.name),
                    "reason": str(call.input.get("reason", ""))[:REASON_MAX_LEN] if isinstance(call.input, dict) else "",
                    "result": "rejected",
                    "error": reason,
                }
            )
            return _Outcome(f"Rejected: {reason}", is_error=True)

    def _dispatch(self, call: ToolCall) -> _Outcome:
        if call.name not in _TOOL_INPUTS:
            raise ProposalRejected(f"unknown tool {call.name!r}; available: {', '.join(TOOL_NAMES)}")
        try:
            data = _TOOL_INPUTS[call.name].model_validate(call.input)
        except ValidationError as exc:
            raise ProposalRejected(f"{call.name}: {_first_error(exc)}") from None
        if isinstance(data, ClickRefInput):
            return self._click(data)
        if isinstance(data, FillRefInput):
            return self._fill(data)
        if isinstance(data, ReadRefInput):
            return self._read(data)
        if isinstance(data, DoneInput):
            return self._done(data)
        assert isinstance(data, GiveUpInput)
        return self._give_up(data)

    def _current_control(self, ref: str) -> Control:
        assert self._observation is not None
        control = self._observation.find(ref)
        if control is None:
            raise ProposalRejected(f"ref {ref!r} is not in the current observation; use a ref from the latest observation")
        return control

    def _authorize(self, action: RuntimeAction, control: Control) -> PolicyDecision | _Outcome:
        """Policy.check; a denial is bounded (spec 8.6): the model-facing outcome is returned instead."""
        assert self._observation is not None
        decision = self.policy.check(action, self._observation)
        if decision.allowed:
            return decision
        self._policy_rejections += 1
        reason = sanitize(decision.reason, self.record.literals)
        self.record.record_event(
            {
                "actor": "model",
                "action": action.kind,
                "control": _control_summary(control),
                "result": "policy_rejected",
                "error": reason,
                "policy_rejections": self._policy_rejections,
            }
        )
        if self._policy_rejections >= POLICY_REJECTION_LIMIT:
            self._stop("policy_blocked", f"second policy-rejected proposal: {reason}")
            return _Outcome(f"Rejected by policy: {reason}. Discovery stopped.", is_error=True, stop="policy_blocked")
        return _Outcome(f"Rejected by policy: {reason}. Do not propose it again.", is_error=True)

    def _execute(self, action: RuntimeAction, decision: PolicyDecision) -> ActionResult | _Outcome:
        """Surface.act behind the decision just made; the model-facing outcome is returned if nothing executed."""
        result = self.surface.act(action, decision)
        if result.executed:
            return result
        error = sanitize(result.error or "not executed", self.record.literals)
        self.record.record_event({"actor": "model", "action": action.kind, "result": "not_executed", "error": error})
        return _Outcome(f"Action did not execute: {error}", is_error=True)

    # click_ref: expect is validated BEFORE Policy and BEFORE any physical action
    def _click(self, data: ClickRefInput) -> _Outcome:
        record = self.record
        expect = validate_model_condition(data.expect, record.literals, "click_ref.expect")
        control = self._current_control(data.ref)
        action = RuntimeClick(kind="click", ref=data.ref)
        decision = self._authorize(action, control)
        if isinstance(decision, _Outcome):
            return decision
        pre = self._observation
        assert pre is not None
        result = self._execute(action, decision)
        if isinstance(result, _Outcome):
            return result
        post, verified = self._settle(expect)
        self._observation = post
        record.actions.append(
            ExecutedAction(
                index=len(record.actions) + 1,
                kind="click",
                ref=data.ref,
                control=control,
                pre_observation=pre,
                post_observation=post,
                reason=data.reason,
                expect=expect,
                expect_verified=verified,
            )
        )
        record.record_event(
            {
                "actor": "model",
                "action": "click",
                "reason": data.reason,
                "control": _control_summary(control),
                "expect": expect.model_dump(),
                "result": "executed",
                "expect_verified": verified,
            }
        )
        changed = observation_signature(post) != observation_signature(pre)
        if verified:
            return _Outcome("Click executed; expected condition verified.", progress=changed)
        return _Outcome(
            "Click executed, but the expected condition did NOT hold afterwards. The click happened and is recorded; "
            "inspect the current observation and continue from the actual state.",
            is_error=False,
            progress=changed,
        )

    def _settle(self, expect: Condition) -> tuple[Observation, bool]:
        """Bounded wait for the click's expect; returns the last Observation and whether it held."""
        deadline = time.monotonic() + self._settle_timeout
        while True:
            observation = self.surface.observe()
            if evaluate(expect, observation, self.record.bindings):
                return observation, True
            if time.monotonic() >= deadline:
                return observation, False
            time.sleep(self._settle_poll)

    # fill_ref: value comes from the runtime binding; auto postcondition value_equals_parameter
    def _fill(self, data: FillRefInput) -> _Outcome:
        record = self.record
        param = record.params.get(data.from_param)
        if param is None:
            raise ProposalRejected(f"from_param {data.from_param!r} is not a declared parameter; declared: {sorted(record.params)}")
        control = self._current_control(data.ref)
        if control.role not in ("textbox", "searchbox", "combobox", "spinbutton"):
            raise ProposalRejected(f"ref {data.ref!r} is a {control.role}, not a fillable input")
        action = RuntimeFill(kind="fill", ref=data.ref, value=param.value)
        decision = self._authorize(action, control)
        if isinstance(decision, _Outcome):
            return decision
        pre = self._observation
        assert pre is not None
        result = self._execute(action, decision)
        if isinstance(result, _Outcome):
            return result
        post = self.surface.observe()
        self._observation = post
        target = Target(locators=locator_candidates(control, pre, flat_literals(record.literals)))
        postcondition = ValueEqualsParameter(kind="value_equals_parameter", target=target, param=param.name)
        verified = bool(target.locators) and evaluate(postcondition, post, record.bindings)
        record.actions.append(
            ExecutedAction(
                index=len(record.actions) + 1,
                kind="fill",
                ref=data.ref,
                control=control,
                pre_observation=pre,
                post_observation=post,
                reason=data.reason,
                from_param=param.name,
                postcondition=postcondition,
                postcondition_verified=verified,
            )
        )
        record.record_event(
            {
                "actor": "model",
                "action": "fill",
                "reason": data.reason,
                "control": _control_summary(control),
                "value": ParameterValue(name=param.name).model_dump(),
                "result": "executed",
                "postcondition_verified": verified,
            }
        )
        if verified:
            return _Outcome(f"Filled from parameter {param.name!r}; the field now holds the parameter value.", progress=True)
        return _Outcome(
            f"Fill from parameter {param.name!r} executed, but the field value could not be verified against the parameter.",
            progress=observation_signature(post) != observation_signature(pre),
        )

    # read_ref: parse immediately; raw value stays in runtime memory; auto postcondition output_present
    def _read(self, data: ReadRefInput) -> _Outcome:
        record = self.record
        if not PARAM_NAME_RE.match(data.capture_as):
            raise ProposalRejected(f"capture_as {data.capture_as!r} must match [a-z][a-z0-9_]*")
        if data.capture_as in record.params:
            raise ProposalRejected(f"capture_as {data.capture_as!r} collides with a parameter name")
        control = self._current_control(data.ref)
        action = RuntimeRead(kind="read", ref=data.ref)
        decision = self._authorize(action, control)
        if isinstance(decision, _Outcome):
            return decision
        pre = self._observation
        assert pre is not None
        result = self._execute(action, decision)
        if isinstance(result, _Outcome):
            return result
        raw_text = result.value or ""
        captured = False
        parse_error: str | None = None
        try:
            parsed = PARSERS[data.parser](raw_text)
        except ValueError:
            parse_error = f"the control text is not a {data.parser} value"
        else:
            captured = True
            record.bindings.outputs[data.capture_as] = parsed
            record.read_raw_text[data.capture_as] = raw_text
            record.output_literal_forms[data.capture_as] = money_literal_forms(raw_text, parsed)
        post = self.surface.observe()
        self._observation = post
        record.actions.append(
            ExecutedAction(
                index=len(record.actions) + 1,
                kind="read",
                ref=data.ref,
                control=control,
                pre_observation=pre,
                post_observation=post,
                reason=data.reason,
                capture_as=data.capture_as,
                parser=data.parser,
                postcondition=OutputPresent(kind="output_present", name=data.capture_as) if captured else None,
                postcondition_verified=captured,
                output_captured=captured,
            )
        )
        record.record_event(
            {
                "actor": "model",
                "action": "read",
                "reason": data.reason,
                "control": _control_summary(control),
                "capture_as": data.capture_as,
                "parser": data.parser,
                "value": None,
                "redacted": True,
                "result": "executed",
                "output_captured": captured,
                **({"error": parse_error} if parse_error else {}),
            }
        )
        if captured:
            return _Outcome(f"Read executed; output {data.capture_as!r} captured with parser {data.parser!r}.", progress=True)
        return _Outcome(f"Read executed, but {parse_error}; nothing was captured. Pick the control holding the value.", is_error=True)

    # done: validated, ANDed with output_present for captured outputs, must hold NOW
    def _done(self, data: DoneInput) -> _Outcome:
        record = self.record
        proposed = validate_model_condition(data.condition, record.literals, "done.condition")
        assert self._observation is not None
        required = [OutputPresent(kind="output_present", name=name) for name in record.bindings.outputs]
        condition: Condition = AllCondition(kind="all", conditions=[proposed, *required]) if required else proposed
        observation = self.surface.observe()
        self._observation = observation
        if not evaluate(condition, observation, record.bindings):
            raise ProposalRejected("done.condition does not currently hold on the page; done is accepted only when it is already true")
        record.done_condition = condition
        record.record_event(
            {"actor": "model", "action": "done", "reason": data.reason, "condition": condition.model_dump(), "result": "accepted"}
        )
        self._stop("goal_completed", data.reason)
        return _Outcome("Done accepted.", progress=True, stop="goal_completed")

    def _give_up(self, data: GiveUpInput) -> _Outcome:
        self.record.give_up_code = data.reason_code
        self.record.record_event({"actor": "model", "action": "give_up", "reason_code": data.reason_code, "reason": data.reason, "result": "accepted"})
        self._stop("give_up", f"{data.reason_code}: {data.reason}")
        return _Outcome("Discovery ended.", stop="give_up")

    def _stop(self, reason: str, detail: str) -> None:
        assert reason in STOP_REASONS
        self.record.stop_reason = reason
        self.record.stop_detail = detail
        self.record.record_event({"actor": "runtime", "event": "stop", "stop_reason": reason, "detail": detail})


def _action_name(tool_name: str) -> str | None:
    return {"click_ref": "click", "fill_ref": "fill", "read_ref": "read", "done": "done", "give_up": "give_up"}.get(tool_name)


def _fake_content(reply: ModelReply) -> list[dict[str, Any]]:
    """Assistant content for a client that did not supply one (scripted fakes)."""
    return [{"type": "tool_use", "id": call.id, "name": call.name, "input": call.input} for call in reply.tool_calls] or [
        {"type": "text", "text": "(no tool call)"}
    ]
