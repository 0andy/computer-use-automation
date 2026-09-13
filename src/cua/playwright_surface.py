"""Sync Playwright implementation of the Surface protocol (docs/spec.md 6.1-6.4).

This is the only module that touches Playwright for CUA logic. It returns and
accepts only ``cua.models`` types; no Page/Frame/ElementHandle escapes.

observe():
  iterates ``page.frames``, evaluates snapshot.js in each, records
  ``frame_path -> URL``, converts every frame-relative bbox to page-relative
  (top-level viewport) coordinates, and merges all controls into one
  Observation. Refs are ``"<observation seq>:<frame index>:<element index>"``
  and are valid for that Observation only: the next observe() drops them.

act():
  passes the single authorization/ownership gate, resolves the ref back to the
  in-frame ``window.__cuaRefs`` element, and performs click / fill / read /
  startup navigate. A ref from an older Observation, or an element that is no
  longer connected, is rejected with STALE_REF: stale refs are never guessed
  through.

human_events:
  ``PlaywrightHumanCapture`` - pull-based, sanitized capture of the human's
  clicks / input-or-change occurrences / navigations during a HITL handoff,
  bound to this same page (spec 15.6).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Page, sync_playwright

from cua.models import (
    ActionResult,
    BBox,
    Control,
    Observation,
    PolicyDecision,
    RunControl,
    RuntimeAction,
    RuntimeClick,
    RuntimeFill,
    RuntimeNavigate,
    RuntimeRead,
)
from cua.resolver import normalize
from cua.surface import ACTION_FAILED, STALE_REF, authorization_rejection, rejected

SNAPSHOT_JS = (Path(__file__).with_name("snapshot.js")).read_text(encoding="utf-8")
HUMAN_RECORDER_JS = (Path(__file__).with_name("human_recorder.js")).read_text(encoding="utf-8")
TOP_FRAME_PATH = "top"
DEFAULT_ACTION_TIMEOUT_MS = 5000

HUMAN_EVENTS_KEY = "__cuaHumanEvents"
HUMAN_CAPTURE_FLAG = "__cuaHumanCapture"
_ARM_JS = f"() => {{ try {{ sessionStorage.setItem('{HUMAN_CAPTURE_FLAG}', '1'); return true; }} catch (e) {{ return false; }} }}"
_DISARM_JS = f"() => {{ try {{ sessionStorage.removeItem('{HUMAN_CAPTURE_FLAG}'); return true; }} catch (e) {{ return false; }} }}"
_DRAIN_JS = (
    f"() => {{ try {{ const raw = sessionStorage.getItem('{HUMAN_EVENTS_KEY}');"
    f" sessionStorage.removeItem('{HUMAN_EVENTS_KEY}'); return raw; }} catch (e) {{ return null; }} }}"
)

_REF_ELEMENT_JS = "i => (window.__cuaRefs || [])[i] || null"
_IS_CONNECTED_JS = "e => e.isConnected"
_READ_JS = (
    "e => (e.tagName === 'INPUT' || e.tagName === 'TEXTAREA' || e.tagName === 'SELECT')"
    " ? String(e.value) : (e.textContent || '')"
)
_FRAME_OFFSET_JS = (
    "e => { const r = e.getBoundingClientRect();"
    " return { x: r.left + e.clientLeft, y: r.top + e.clientTop }; }"
)


class SurfaceError(RuntimeError):
    """Observation could not be taken (for example a frame kept navigating)."""


class _FrameLeaving(Exception):
    """Internal: a frame's document has a form submission in flight (pending navigation)."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


LEAVING_POLL_S = 0.05


def to_page_relative(bbox: dict[str, float], offset: tuple[float, float]) -> BBox:
    """Frame-relative bbox + the frame's page-relative origin -> page-relative bbox."""
    return BBox(
        x=float(bbox["x"]) + offset[0],
        y=float(bbox["y"]) + offset[1],
        width=float(bbox["width"]),
        height=float(bbox["height"]),
    )


class PlaywrightSurface:
    def __init__(
        self,
        page: Page,
        run_control: RunControl | None = None,
        action_timeout_ms: int = DEFAULT_ACTION_TIMEOUT_MS,
    ) -> None:
        self._page = page
        self.run_control = run_control if run_control is not None else RunControl()
        self._timeout = action_timeout_ms
        self._seq = 0
        self._refs: dict[str, tuple[Frame, int]] = {}  # current Observation only
        self._human_events: PlaywrightHumanCapture | None = None

    @property
    def page(self) -> Page:
        """The one live page this surface drives (for tests/CLI diagnostics; core engine code never uses it)."""
        return self._page

    @property
    def human_events(self) -> PlaywrightHumanCapture:
        """Pull-based human event capture bound to the same live page (spec 15.6)."""
        if self._human_events is None:
            self._human_events = PlaywrightHumanCapture(self._page)
        return self._human_events

    # -- observe -------------------------------------------------------------

    def observe(self) -> Observation:
        """Snapshot every frame. Never returns a document that is on its way out.

        A frame whose document has a form submission in flight (the navigation
        request was sent but has not committed, so the old page is still fully
        rendered) is mid-navigation: observe() waits for it, bounded by the action
        timeout, before snapshotting. A frame that navigates mid-snapshot is
        retried once. Still unsettled after the bound -> SurfaceError.
        """
        deadline = time.monotonic() + self._timeout / 1000.0
        errors = 0
        while True:
            try:
                return self._observe_once()
            except _FrameLeaving as leaving:
                if time.monotonic() >= deadline:
                    raise SurfaceError(f"observe failed: frame {leaving.path!r} has a pending navigation") from None
                time.sleep(LEAVING_POLL_S)
            except PlaywrightError as exc:
                errors += 1
                if errors > 1:
                    raise SurfaceError(f"observe failed: {_first_line(str(exc))}") from None

    def _observe_once(self) -> Observation:
        self._seq += 1
        refs: dict[str, tuple[Frame, int]] = {}
        frames: dict[str, str] = {}
        controls: list[Control] = []
        paths = self._frame_paths()
        for frame_index, frame in enumerate(self._page.frames):
            path = paths[frame]
            offset = self._frame_offset(frame)
            data = frame.evaluate(SNAPSHOT_JS)
            leaving_ms = data.get("leaving_ms")
            if leaving_ms is not None and leaving_ms < self._timeout:
                raise _FrameLeaving(path)  # a submitted form's navigation has not committed yet
            frames[path] = data["url"]
            for raw in data["controls"]:
                ref = f"{self._seq}:{frame_index}:{raw['index']}"
                refs[ref] = (frame, raw["index"])
                controls.append(_control(ref, path, raw, offset))
        self._refs = refs
        return Observation(url=self._page.url, frames=frames, controls=controls)

    def _frame_paths(self) -> dict[Frame, str]:
        paths: dict[Frame, str] = {self._page.main_frame: TOP_FRAME_PATH}

        def path_of(frame: Frame) -> str:
            if frame in paths:
                return paths[frame]
            parent = frame.parent_frame
            assert parent is not None
            siblings = parent.child_frames
            name = frame.name or f"frame[{siblings.index(frame)}]"
            paths[frame] = f"{path_of(parent)}/{name}"
            return paths[frame]

        for frame in self._page.frames:
            path_of(frame)
        return paths

    def _frame_offset(self, frame: Frame) -> tuple[float, float]:
        """Page-relative origin of ``frame``'s viewport (iframe box + border, recursively)."""
        parent = frame.parent_frame
        if parent is None:
            return (0.0, 0.0)
        element = frame.frame_element()
        try:
            rect = element.evaluate(_FRAME_OFFSET_JS)
        finally:
            element.dispose()
        px, py = self._frame_offset(parent)
        return (px + float(rect["x"]), py + float(rect["y"]))

    # -- act ---------------------------------------------------------------

    def act(self, action: RuntimeAction, authorization: PolicyDecision) -> ActionResult:
        code = authorization_rejection(action, authorization, self.run_control)
        if code is not None:
            return rejected(code)
        try:
            if isinstance(action, RuntimeNavigate):
                self._refs = {}
                self._page.goto(action.url, wait_until="load", timeout=self._timeout)
                return ActionResult(executed=True)

            entry = self._refs.get(action.ref)
            if entry is None:
                return rejected(STALE_REF)
            frame, index = entry
            handle = frame.evaluate_handle(_REF_ELEMENT_JS, index)
            element = handle.as_element()
            if element is None:
                handle.dispose()
                return rejected(STALE_REF)
            try:
                if not element.evaluate(_IS_CONNECTED_JS):
                    return rejected(STALE_REF)
                if isinstance(action, RuntimeClick):
                    element.click(timeout=self._timeout)
                    return ActionResult(executed=True)
                if isinstance(action, RuntimeFill):
                    element.fill(action.value, timeout=self._timeout)
                    return ActionResult(executed=True)
                if isinstance(action, RuntimeRead):
                    value = element.evaluate(_READ_JS)
                    return ActionResult(executed=True, value=normalize(str(value)))
                raise TypeError(f"unknown runtime action {action!r}")  # unreachable: closed union
            finally:
                element.dispose()
        except PlaywrightError as exc:
            return ActionResult(executed=False, error=f"{ACTION_FAILED}: {_first_line(str(exc))}")

    # -- screenshot --------------------------------------------------------

    def screenshot(self) -> bytes:
        return self._page.screenshot(type="png")


def _control(ref: str, frame_path: str, raw: dict[str, Any], offset: tuple[float, float]) -> Control:
    return Control(
        ref=ref,
        role=raw["role"],
        name=raw.get("name"),
        label=raw.get("label"),
        text=raw.get("text"),
        input_value=raw.get("input_value"),
        attrs=dict(raw.get("attrs") or {}),
        href=raw.get("href"),
        frame_path=frame_path,
        ancestor_roles=list(raw.get("ancestor_roles") or []),
        bbox=to_page_relative(raw["bbox"], offset),
        table_index=raw.get("table_index"),
        row_index=raw.get("row_index"),
        col_index=raw.get("col_index"),
    )


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else text


class PlaywrightHumanCapture:
    """Pull-based capture of sanitized human UI events on the live page (spec 15.6).

    ``begin()`` installs the in-page recorder (``human_recorder.js``) as a page init
    script - so the browser itself re-installs it in every document the human
    navigates to, with no Python callback involved - evaluates it in the documents
    that already exist, arms the sessionStorage capture flag in every frame, and
    starts a Python-side ``page.on("framenavigated")`` log. ``end()`` drains the
    in-page buffers (sessionStorage survives same-origin navigation, so events
    recorded before a navigation are still there), disarms, removes the listener
    and returns everything captured. Nothing is captured outside begin/end, which
    Replay calls exactly while ``RunControl.owner == HUMAN``.
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        self._installed = False
        self._active = False
        self._navigations: list[dict[str, Any]] = []

    @property
    def active(self) -> bool:
        return self._active

    def begin(self) -> None:
        if self._active:
            raise RuntimeError("human event capture is already active")
        if not self._installed:
            self._page.add_init_script(HUMAN_RECORDER_JS + "();")  # the browser runs it in every new document
            self._installed = True
        self._each_frame(HUMAN_RECORDER_JS)  # documents that already exist (idempotent)
        self._each_frame(_ARM_JS)
        self._navigations = []
        self._page.on("framenavigated", self._on_framenavigated)
        self._active = True

    def end(self) -> list[dict[str, Any]]:
        if not self._active:
            return []
        events = self._drain()  # a Playwright round-trip: pending framenavigated callbacks are dispatched here
        self._each_frame(_DISARM_JS)
        self._page.remove_listener("framenavigated", self._on_framenavigated)
        self._active = False
        events.extend(self._navigations)
        self._navigations = []
        return events

    # -- internals ----------------------------------------------------------

    def _on_framenavigated(self, frame: Frame) -> None:
        name = TOP_FRAME_PATH if frame == self._page.main_frame else (frame.name or "frame")
        self._navigations.append({"source": "python", "kind": "navigation", "frame": name, "url": frame.url})

    def _each_frame(self, script: str) -> None:
        for frame in list(self._page.frames):
            try:
                frame.evaluate(script)
            except PlaywrightError:
                continue  # frame detached / mid-navigation: the init script covers its next document

    def _drain(self) -> list[dict[str, Any]]:
        """In-page events from every frame. Same-origin frames share one buffer, so no duplicates."""
        events: list[dict[str, Any]] = []
        for frame in list(self._page.frames):
            raw: Any = None
            for _attempt in range(2):
                try:
                    raw = frame.evaluate(_DRAIN_JS)
                    break
                except PlaywrightError:
                    continue
            if not raw:
                continue
            try:
                items = json.loads(raw)
            except ValueError:
                continue
            for item in items:
                if isinstance(item, dict) and item.get("kind"):
                    events.append({"source": "page", **item})
        return events


@contextmanager
def launch_surface(
    headless: bool = True,
    run_control: RunControl | None = None,
    window_size: tuple[int, int] | None = None,
    window_position: tuple[int, int] | None = None,
) -> Iterator[PlaywrightSurface]:
    """Own a Chromium browser/context/page for the lifetime of the block (used by the CLI).

    ``window_size`` / ``window_position`` only apply to a headed window: they are
    passed to Chromium as ``--window-size`` / ``--window-position`` (Playwright uses
    a fresh temporary profile each run, so nothing is remembered between runs), and
    the context viewport is set to ``window_size`` so the page fills the window.
    """
    args: list[str] = []
    context_kwargs: dict[str, Any] = {}
    if not headless:
        if window_size is not None:
            args.append(f"--window-size={window_size[0]},{window_size[1]}")
            context_kwargs["viewport"] = {"width": window_size[0], "height": window_size[1]}
        if window_position is not None:
            args.append(f"--window-position={window_position[0]},{window_position[1]}")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless, args=args)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        try:
            yield PlaywrightSurface(page, run_control)
        finally:
            context.close()
            browser.close()
