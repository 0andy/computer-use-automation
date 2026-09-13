"""Policy: the single boundary every automated physical action crosses (docs/spec.md 7).

    proposed RuntimeAction -> Policy.check(...) -> PolicyDecision -> Surface.act(..., authorization)

Checks, in order:
  1. action kind is in the app's ``allowed_actions``;
  2. navigate: the destination passes the origin/route allowlist;
     every other kind: the URL of EVERY frame in the current Observation passes it
     (a denied or out-of-origin frame blocks automated action; denied wins over allowed);
  3. the acted-on ref exists in the current Observation;
  4. risk rules: a control matching an irreversible rule is blocked;
  5. link preflight: a click on a control exposing ``href`` is blocked if the
     destination fails the allowlist. ``href`` is inspected here only; it is
     never a locator.

Pure over (AppConfig, RuntimeAction, Observation); no Playwright.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from cua.config import AppConfig
from cua.models import Control, Observation, PolicyDecision, RuntimeAction, RuntimeNavigate
from cua.resolver import normalize

ALLOWED = "allowed"


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), "", "", ""))


def _route_matches(path: str, route: str) -> bool:
    route = route.rstrip("/") or "/"
    if route == "/":
        return path == "/" or path == ""
    return path == route or path.startswith(route + "/")


class Policy:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    # -- allowlist ----------------------------------------------------------

    def url_denial(self, url: str) -> str | None:
        """Why ``url`` is not automation-allowed, or None if it is."""
        allow = self.config.allowlist
        if not url or url.startswith("about:"):
            return f"url {url!r} has no automation-allowed origin"
        if _origin(url) != _origin(allow.origin):
            return f"origin of {_origin(url)!r} is outside allowlist origin {allow.origin!r}"
        path = urlsplit(url).path or "/"
        for route in allow.denied_routes:
            if _route_matches(path, route):
                return f"route {path!r} is denied by rule {route!r}"
        if not any(_route_matches(path, route) for route in allow.allowed_routes):
            return f"route {path!r} is not in allowed_routes"
        return None

    def frame_denial(self, observation: Observation) -> str | None:
        """First frame (top-level included) whose URL is not allowed."""
        urls = [("top", observation.url), *observation.frames.items()]
        for frame_path, url in urls:
            why = self.url_denial(url)
            if why is not None:
                return f"frame {frame_path!r}: {why}"
        return None

    # -- risk rules -----------------------------------------------------------

    def risk_denial(self, control: Control) -> str | None:
        name = normalize(control.name)
        for rule in self.config.risk_rules:
            if control.role == rule.match.role and name == normalize(rule.match.name):
                return f"control {rule.match.role} {rule.match.name!r} is {rule.effect}: {rule.decision}"
        return None

    # -- the boundary -------------------------------------------------------

    def check(self, action: RuntimeAction, observation: Observation | None) -> PolicyDecision:
        def deny(reason: str) -> PolicyDecision:
            return PolicyDecision(allowed=False, reason=reason, action=action)

        if action.kind not in self.config.allowed_actions:
            return deny(f"action kind {action.kind!r} is not allowed for app {self.config.app!r}")

        if isinstance(action, RuntimeNavigate):
            why = self.url_denial(action.url)
            if why is not None:
                return deny(f"navigate: {why}")
            return PolicyDecision(allowed=True, reason=ALLOWED, action=action)

        if observation is None:
            return deny("no current Observation to authorize against")
        why = self.frame_denial(observation)
        if why is not None:
            return deny(why)

        control = observation.find(action.ref)
        if control is None:
            return deny(f"ref {action.ref!r} is not in the current Observation")

        why = self.risk_denial(control)
        if why is not None:
            return deny(why)

        if action.kind == "click" and control.href is not None:
            why = self.url_denial(control.href)
            if why is not None:
                return deny(f"link preflight: {why}")

        return PolicyDecision(allowed=True, reason=ALLOWED, action=action)
