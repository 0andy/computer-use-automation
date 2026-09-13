"""``cua`` command-line entry point.

``cua observe --app <app>`` (Phase 1): open the app's entry URL through
Policy + Surface and print a compact semantic Observation. Input control values
are never printed raw (they may be sensitive); they are shown as a redacted
marker.

``cua discover ...`` (Phase 2): genuine LLM Discovery against the live UI.
Input validation (capability id, params, goal placeholders) happens before any
browser or model starts; sanitized evidence (meta.json, events.jsonl) is written
to ``--evidence-dir`` or ``.cua-out/<timestamp>/``.

Phase 3: a Discovery that stops with ``goal_completed`` is compiled immediately,
from the in-memory DiscoveryRecord, into ``capabilities/<app>/<capability>.json``
(a copy goes to the evidence dir as ``artifact.json``). ``cua capabilities
list|show`` is the Catalog: a metadata projection over those artifacts.

``cua replay --app --capability --param name=value [--headed] [--evidence-dir]``
(Phase 4/5): deterministic execution of the exact artifact with zero LLM
decisions. ``--headed`` sets ``headless=False`` AND ``handoff_enabled=True``:
a handoff-eligible stuck state hands the same live browser to a human through
the ConsoleOperator ([R]etry / [C]ontinue / [A]bort). A normal headless run has
handoff disabled and returns ``failure`` with ``escalation=unavailable_headless``
instead. Runtime outputs are printed raw to the caller; persisted ``result.json``
redacts them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer

from cua.config import load_app_config
from cua.models import Control, Observation, RuntimeNavigate

app = typer.Typer(
    name="cua",
    help="Computer-use automation: LLM discovery, capability catalog, deterministic replay.",
    no_args_is_help=True,
    add_completion=False,
)
capabilities_app = typer.Typer(help="Capability Catalog: metadata projected from capability artifacts.", no_args_is_help=True)
app.add_typer(capabilities_app, name="capabilities")

REDACTED_VALUE = "[REDACTED]"

CAPABILITIES_DIR_OPTION = typer.Option(
    None, "--capabilities-dir", help="Capabilities root (default: the project capabilities/ directory)"
)


WINDOW_SIZE_OPTION = typer.Option(
    None, "--window-size", help="Headed window size as WxH, e.g. 1280x900 (only with --headed)"
)
WINDOW_POSITION_OPTION = typer.Option(
    None, "--window-position", help="Headed window position as X,Y, e.g. 640,0 (only with --headed)"
)


@app.callback()
def main() -> None:
    """Computer-use automation CLI."""


def parse_window_size(value: str) -> tuple[int, int]:
    """``"WxH"`` -> ``(W, H)``; both positive integers. ValueError with a clear message otherwise."""
    return _parse_int_pair(value, "x", "--window-size", "WxH, e.g. 1280x900", positive=True)


def parse_window_position(value: str) -> tuple[int, int]:
    """``"X,Y"`` -> ``(X, Y)``; integers (negative allowed: a secondary monitor may sit left of the primary)."""
    return _parse_int_pair(value, ",", "--window-position", "X,Y, e.g. 640,0", positive=False)


def _parse_int_pair(value: str, sep: str, option: str, expected: str, positive: bool) -> tuple[int, int]:
    parts = value.strip().split(sep)
    if len(parts) != 2:
        raise ValueError(f"{option} must be {expected} (got {value!r})")
    try:
        first, second = (int(part.strip()) for part in parts)
    except ValueError:
        raise ValueError(f"{option} must be {expected} (got {value!r})") from None
    if positive and (first <= 0 or second <= 0):
        raise ValueError(f"{option} values must be positive integers (got {value!r})")
    return (first, second)


def parse_window_options(
    window_size: Optional[str], window_position: Optional[str]
) -> tuple[Optional[tuple[int, int]], Optional[tuple[int, int]]]:
    """Parse both window options (each may be unset). Raises ValueError on a malformed value."""
    size = parse_window_size(window_size) if window_size is not None else None
    position = parse_window_position(window_position) if window_position is not None else None
    return (size, position)


def format_control(control: Control) -> str:
    parts = [f"[{control.ref}]", control.role]
    if control.name is not None:
        parts.append(f"name={control.name!r}")
    if control.label is not None:
        parts.append(f"label={control.label!r}")
    if control.text is not None and control.text != control.name:
        parts.append(f"text={control.text!r}")
    if control.input_value is not None:
        parts.append(f"value={REDACTED_VALUE}")
    if control.attrs:
        parts.append("attrs=" + ",".join(f"{k}={v!r}" for k, v in control.attrs.items()))
    if control.href is not None:
        parts.append(f"href={control.href!r}")
    if control.table_index is not None:
        parts.append(f"table={control.table_index}/{control.row_index}/{control.col_index}")
    b = control.bbox
    parts.append(f"bbox=({b.x:.0f},{b.y:.0f},{b.width:.0f},{b.height:.0f})")
    return " ".join(parts)


def format_observation(observation: Observation) -> str:
    lines = [f"url: {observation.url}", "frames:"]
    for path, url in observation.frames.items():
        lines.append(f"  {path}: {url}")
    lines.append(f"controls ({len(observation.controls)}):")
    for control in observation.controls:
        lines.append(f"  {control.frame_path}  {format_control(control)}")
    return "\n".join(lines)


@app.command()
def observe(
    app_id: str = typer.Option(..., "--app", help="Target app id, e.g. mockbank"),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Override the app base URL"),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window"),
) -> None:
    """Open the app entry URL and print a compact semantic Observation."""
    from cua.playwright_surface import launch_surface
    from cua.policy import Policy

    config = load_app_config(app_id, base_url=base_url)
    policy = Policy(config)
    navigate = RuntimeNavigate(kind="navigate", url=config.entry_url)
    decision = policy.check(navigate, None)
    if not decision.allowed:
        typer.echo(f"POLICY_BLOCKED: {decision.reason}", err=True)
        raise typer.Exit(code=2)
    with launch_surface(headless=not headed) as surface:
        result = surface.act(navigate, decision)
        if not result.executed:
            typer.echo(f"navigation failed: {result.error}", err=True)
            raise typer.Exit(code=1)
        observation = surface.observe()
    typer.echo(format_observation(observation))


@app.command()
def discover(
    app_id: str = typer.Option(..., "--app", help="Target app id, e.g. mockbank"),
    capability: str = typer.Option(..., "--capability", help="Stable capability id: lowercase letters, digits, underscores"),
    goal: str = typer.Option(..., "--goal", help="Natural-language goal with {param} placeholders"),
    description: Optional[str] = typer.Option(
        None, "--description", help="Plain sentence the Catalog shows (default: derived from the capability id)"
    ),
    params: List[str] = typer.Option([], "--param", help="Typed input + runtime binding: name:type=value (repeatable)"),
    max_steps: int = typer.Option(12, "--max-steps", help="Maximum model rounds"),
    timeout: float = typer.Option(60.0, "--timeout", help="Overall run timeout in seconds"),
    evidence_dir: Optional[Path] = typer.Option(None, "--evidence-dir", help="Evidence directory (default .cua-out/<timestamp>/)"),
    capabilities_dir: Optional[Path] = CAPABILITIES_DIR_OPTION,
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Override the app base URL"),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window"),
    window_size: Optional[str] = WINDOW_SIZE_OPTION,
    window_position: Optional[str] = WINDOW_POSITION_OPTION,
) -> None:
    """Run genuine LLM Discovery of a capability against the live app and compile the artifact."""
    from cua.compiler import CAPABILITIES_DIR, CompileError, artifact_path, compile_record, write_artifact
    from cua.discovery import Discovery, DiscoveryInputError, parse_params, validate_capability_id, validate_goal
    from cua.evidence import EvidenceWriter, default_evidence_dir
    from cua.model_client import AnthropicModelClient, ModelConfigError

    # 1. Validate every input before any browser or model starts.
    try:
        validate_capability_id(capability)
        declared = parse_params(params)
        validate_goal(goal, declared)
        if description is not None and not description.strip():
            raise DiscoveryInputError("--description must not be blank")
        if max_steps < 1:
            raise DiscoveryInputError("--max-steps must be at least 1")
        if timeout <= 0:
            raise DiscoveryInputError("--timeout must be positive")
        size, position = parse_window_options(window_size, window_position)
        config = load_app_config(app_id, base_url=base_url)
    except (DiscoveryInputError, ValueError, FileNotFoundError) as exc:
        typer.echo(f"invalid input: {exc}", err=True)
        raise typer.Exit(code=2)

    # 2. Model client (needs ANTHROPIC_API_KEY; CUA_MODEL selects the model).
    try:
        model = AnthropicModelClient()
    except ModelConfigError as exc:
        typer.echo(f"model configuration: {exc}", err=True)
        raise typer.Exit(code=2)

    from cua.playwright_surface import launch_surface
    from cua.policy import Policy

    out_dir = evidence_dir if evidence_dir is not None else default_evidence_dir()
    policy = Policy(config)
    with launch_surface(headless=not headed, window_size=size, window_position=position) as surface:
        discovery = Discovery(
            config=config,
            policy=policy,
            surface=surface,
            model=model,
            capability_id=capability,
            goal=goal,
            params=declared,
            max_steps=max_steps,
            timeout_s=timeout,
        )
        try:
            record = discovery.run()
        finally:
            record = discovery.record
            writer = EvidenceWriter(out_dir, record.literals)
            writer.write_meta(record.meta())
            writer.write_events(record.events)

    typer.echo(f"stop_reason: {record.stop_reason}")
    typer.echo(f"model: {record.model}  calls: {len(record.model_calls)}  rounds: {record.rounds}")
    typer.echo(f"executed actions: {len(record.actions)}")
    typer.echo(f"outputs captured: {sorted(record.outputs) or 'none'}")
    typer.echo(f"evidence: {out_dir}")
    if record.stop_reason != "goal_completed":
        raise typer.Exit(code=1)

    # 3. Compile the in-memory record straight away (spec 12): no historical compile exists.
    try:
        artifact = compile_record(record, config, description=description)
    except CompileError as exc:
        typer.echo(f"compile failed: {exc}", err=True)
        raise typer.Exit(code=1)
    path = write_artifact(artifact, artifact_path(record.app, record.capability_id, capabilities_dir or CAPABILITIES_DIR))
    write_artifact(artifact, out_dir / "artifact.json")
    typer.echo(f"artifact: {path}")
    typer.echo(f"steps: {len(artifact.steps)}  outputs: {sorted(artifact.outputs) or 'none'}")


def replay_session_options(headed: bool) -> tuple[bool, bool]:
    """``--headed`` -> (headless, handoff_enabled). Headed = visible browser + human handoff enabled (spec 15.3)."""
    return (not headed, headed)


@app.command()
def replay(
    app_id: str = typer.Option(..., "--app", help="Target app id, e.g. mockbank"),
    capability: str = typer.Option(..., "--capability", help="Exact capability id to execute"),
    params: List[str] = typer.Option([], "--param", help="Runtime binding: name=value (repeatable)"),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window and enable same-session human handoff"),
    evidence_dir: Optional[Path] = typer.Option(None, "--evidence-dir", help="Evidence directory (default .cua-out/<timestamp>/)"),
    capabilities_dir: Optional[Path] = CAPABILITIES_DIR_OPTION,
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Override the app base URL"),
    window_size: Optional[str] = WINDOW_SIZE_OPTION,
    window_position: Optional[str] = WINDOW_POSITION_OPTION,
) -> None:
    """Execute a capability artifact deterministically (0 LLM decisions) and classify the outcome."""
    from cua.compiler import CAPABILITIES_DIR
    from cua.evidence import default_evidence_dir, sanitize_value
    from cua.replay import Replay, ReplayInputError, bind_params, load_capability, parse_replay_params, write_evidence

    # 1. Exact lookup + parameter validation before any browser starts.
    try:
        artifact = load_capability(app_id, capability, capabilities_dir or CAPABILITIES_DIR)
        bound = bind_params(artifact, parse_replay_params(params))
        size, position = parse_window_options(window_size, window_position)
        config = load_app_config(app_id, base_url=base_url)
    except (ReplayInputError, ValueError, FileNotFoundError) as exc:
        typer.echo(f"invalid input: {exc}", err=True)
        raise typer.Exit(code=2)

    from cua.hitl import ConsoleOperator
    from cua.models import RunControl
    from cua.playwright_surface import launch_surface
    from cua.policy import Policy

    out_dir = evidence_dir if evidence_dir is not None else default_evidence_dir()
    headless, handoff_enabled = replay_session_options(headed)
    run_control = RunControl()  # shared by the Surface gate and the engine's ownership transitions
    with launch_surface(
        headless=headless, run_control=run_control, window_size=size, window_position=position
    ) as surface:
        run = Replay(
            artifact=artifact,
            config=config,
            policy=Policy(config),
            surface=surface,
            params=bound,
            headed=headed,
            handoff_enabled=handoff_enabled,
            operator=ConsoleOperator() if handoff_enabled else None,
            human_capture=surface.human_events,
            run_control=run_control,
        )
        result = run.run()
    write_evidence(run, result, out_dir)

    typer.echo(f"kind: {result.kind}")
    typer.echo(f"business_outcome: {result.business_outcome}")
    typer.echo(f"outputs: {json.dumps(result.outputs)}")  # raw runtime return value for the caller
    if result.failure is not None:
        failure = sanitize_value(result.failure.model_dump(mode="json"), run.literals)
        typer.echo(f"failure: {json.dumps(failure)}")
    typer.echo(f"recovery_count: {result.recovery_count}")
    typer.echo(f"handoffs: {run.handoffs}  owner: {run.run_control.owner.value}")
    typer.echo(f"llm_calls: {result.llm_calls}")
    typer.echo(f"evidence: {out_dir}")
    if result.kind not in ("success", "business_outcome"):
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# cua capabilities list | show  (spec 3.4, 13): metadata projection only
# --------------------------------------------------------------------------- #


def format_entry(entry) -> str:
    inputs = ", ".join(f"{k}:{v}" for k, v in entry.inputs.items()) or "-"
    outputs = ", ".join(f"{k}:{v}" for k, v in entry.outputs.items()) or "-"
    return f"{entry.app}  {entry.capability_id}  rev={entry.revision}  inputs={inputs}  outputs={outputs}  {entry.description}"


@capabilities_app.command("list")
def capabilities_list(
    app_id: Optional[str] = typer.Option(None, "--app", help="Only capabilities of this app"),
    capabilities_dir: Optional[Path] = CAPABILITIES_DIR_OPTION,
) -> None:
    """List available capabilities (metadata projected from the artifacts)."""
    from cua.catalog import CatalogError, scan
    from cua.compiler import CAPABILITIES_DIR

    try:
        entries = scan(capabilities_dir or CAPABILITIES_DIR, app=app_id)
    except CatalogError as exc:
        typer.echo(f"catalog error: {exc}", err=True)
        raise typer.Exit(code=1)
    if not entries:
        typer.echo("no capabilities found")
        return
    for entry in entries:
        typer.echo(format_entry(entry))


@capabilities_app.command("show")
def capabilities_show(
    app_id: str = typer.Option(..., "--app", help="Target app id, e.g. mockbank"),
    capability: str = typer.Option(..., "--capability", help="Capability id"),
    capabilities_dir: Optional[Path] = CAPABILITIES_DIR_OPTION,
) -> None:
    """Show one capability's metadata as JSON."""
    from cua.catalog import CatalogError, find
    from cua.compiler import CAPABILITIES_DIR

    try:
        entry = find(app_id, capability, capabilities_dir or CAPABILITIES_DIR)
    except CatalogError as exc:
        typer.echo(f"catalog error: {exc}", err=True)
        raise typer.Exit(code=1)
    if entry is None:
        typer.echo(f"no capability {capability!r} for app {app_id!r}", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(entry.model_dump(), indent=2))


if __name__ == "__main__":
    app()
