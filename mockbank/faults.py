"""Process-wide, one-shot fault control for MockBank.

The armed state lives only in this module's memory. It is deliberately NOT stored
in a cookie, a session, or any per-client/per-BrowserContext scope: arming a fault
from any client affects the next member-search POST from any client. A fault is
consumed (disarmed) at the moment the application applies it.

Faults:
  interstitial  -> the search POST is answered by a "System notice" page with a
                   Continue button (a known, authored recovery for Replay).
  unknown       -> the search POST is answered by a "Supervisor override required"
                   page with an Acknowledge button (unknown to Replay; HITL when headed).

If several faults are armed at once, each search POST applies exactly one of them,
in the precedence order of ``FAULTS``.
"""

from __future__ import annotations

INTERSTITIAL = "interstitial"
UNKNOWN = "unknown"
FAULTS: tuple[str, ...] = (INTERSTITIAL, UNKNOWN)  # precedence order
MODE_ONCE = "once"

_armed: dict[str, bool] = {name: False for name in FAULTS}


def arm(name: str, mode: str = MODE_ONCE) -> None:
    """Arm ``name`` for one application. Only the ``once`` mode exists."""
    if name not in _armed:
        raise ValueError(f"unknown fault {name!r}; expected one of {FAULTS}")
    if mode != MODE_ONCE:
        raise ValueError(f"unsupported fault mode {mode!r}; only {MODE_ONCE!r} is supported")
    _armed[name] = True


def consume_next() -> str | None:
    """Return the first armed fault in precedence order and disarm it, or None."""
    for name in FAULTS:
        if _armed[name]:
            _armed[name] = False
            return name
    return None


def snapshot() -> dict[str, bool]:
    """Current armed state (a copy), for the /settings page and tests."""
    return dict(_armed)


def reset() -> None:
    """Disarm everything."""
    for name in FAULTS:
        _armed[name] = False
