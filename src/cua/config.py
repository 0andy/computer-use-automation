"""Typed app config loaded from ``config/apps/<app>.yaml`` (docs/spec.md section 7.1).

The YAML holds authored app/runtime material only: entry URL, origin/route
allowlist, allowed action kinds, risk rules, business outcomes and known
recoveries. No capability definitions live here.

Base-URL override (spec 17.1): if ``<APP>_BASE_URL`` (``MOCKBANK_BASE_URL`` for
mockbank) is set, it replaces both ``entry_url`` and ``allowlist.origin`` so a
test server on 127.0.0.1 with a random port passes the origin allowlist. The same
override is available programmatically through ``AppConfig.with_base_url``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cua.models import BusinessOutcome, Recovery

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "apps"
APP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

ActionKind = Literal["navigate", "click", "fill", "read"]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Allowlist(_Closed):
    origin: str
    allowed_routes: list[str]
    denied_routes: list[str] = Field(default_factory=list)


class RiskMatch(_Closed):
    role: str
    name: str


class RiskRule(_Closed):
    match: RiskMatch
    effect: Literal["irreversible"]
    decision: Literal["block"]


class AppConfig(_Closed):
    app: str
    entry_url: str
    allowlist: Allowlist
    allowed_actions: list[ActionKind]
    risk_rules: list[RiskRule] = Field(default_factory=list)
    business_outcomes: list[BusinessOutcome] = Field(default_factory=list)
    recoveries: list[Recovery] = Field(default_factory=list)

    def with_base_url(self, base_url: str) -> AppConfig:
        """Copy with ``entry_url`` and ``allowlist.origin`` re-based onto ``base_url``.

        The entry path from the authored config is kept (``/`` for MockBank);
        only scheme/host/port change.
        """
        base = urlsplit(base_url)
        if not base.scheme or not base.netloc:
            raise ValueError(f"base URL must be absolute, got {base_url!r}")
        origin = urlunsplit((base.scheme, base.netloc, "", "", ""))
        entry = urlsplit(self.entry_url)
        entry_url = urlunsplit((base.scheme, base.netloc, entry.path or "/", entry.query, ""))
        return self.model_copy(
            update={
                "entry_url": entry_url,
                "allowlist": self.allowlist.model_copy(update={"origin": origin}),
            }
        )


def base_url_env_var(app: str) -> str:
    return f"{app.upper()}_BASE_URL"


def load_app_config(
    app: str,
    base_url: str | None = None,
    config_dir: Path = CONFIG_DIR,
) -> AppConfig:
    """Load ``<config_dir>/<app>.yaml``.

    ``base_url`` (explicit argument) wins over the ``<APP>_BASE_URL`` environment
    variable, which wins over the authored YAML values.
    """
    if not APP_ID_RE.match(app):
        raise ValueError(f"invalid app id {app!r}")
    path = config_dir / f"{app}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"no app config for {app!r} at {path}")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    config = AppConfig.model_validate(raw)
    if config.app != app:
        raise ValueError(f"{path} declares app {config.app!r}, expected {app!r}")

    override = base_url if base_url is not None else os.environ.get(base_url_env_var(app))
    if override:
        config = config.with_base_url(override)
    return config
