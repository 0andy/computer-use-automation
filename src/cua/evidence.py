"""Global sanitizer and evidence writer (docs/spec.md 9.2, 9.3, 16).

``sanitize(text, literals)`` is the ONE place a known sensitive runtime literal
is turned into a readable token such as ``[REDACTED:member_id]``. Every string
that is persisted (reasons, observed summaries, URLs, frame URLs, errors, event
text, metadata) and every string shown to the model passes through it.

``literals`` maps a name (parameter or output name) to the raw runtime forms
that must never be persisted, e.g. ``{"member_id": ["12345"],
"savings_balance": ["$1,234.56", "1,234.56", "1234.56"]}``. Raw forms live
only in runtime memory (spec 9.1).

``EvidenceWriter`` writes ``meta.json`` and ``events.jsonl`` into one evidence
directory; every string inside the structures it receives is sanitized again
at write time, so nothing reaches disk unsanitized even if a caller forgot.

Persisted evidence carries no wall-clock timestamps: two runs of the same
capability against the same app should differ only in run-specific data, so
files are ordered by ``seq`` and timed by relative ``elapsed_ms`` only. The
run timestamp appears only in the default evidence directory name (spec 16.1).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

Literals = Mapping[str, Sequence[str]]  # name -> raw runtime forms

DEFAULT_EVIDENCE_ROOT = Path(".cua-out")


def redaction_token(name: str) -> str:
    return f"[REDACTED:{name}]"


def _pairs(literals: Literals) -> list[tuple[str, str]]:
    """(raw form, name) pairs, longest form first so partial overlaps resolve safely."""
    pairs = [(form, name) for name, forms in literals.items() for form in forms if form]
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def flat_literals(literals: Literals) -> list[str]:
    """All raw forms as a flat list (the shape ``resolver.locator_candidates`` takes)."""
    return [form for form, _name in _pairs(literals)]


def sanitize(text: str, literals: Literals) -> str:
    """Replace every known sensitive literal in ``text`` with ``[REDACTED:<name>]``."""
    if not text:
        return text
    out = text
    for form, name in _pairs(literals):
        if form in out:
            out = out.replace(form, redaction_token(name))
    return out


def find_sensitive(text: str, literals: Literals) -> str | None:
    """Name of the first known sensitive literal contained in ``text``, or None."""
    if not text:
        return None
    for form, name in _pairs(literals):
        if form in text:
            return name
    return None


def sanitize_value(value: Any, literals: Literals) -> Any:
    """Recursively sanitize every string inside dicts/lists/tuples; other values pass through."""
    if isinstance(value, str):
        return sanitize(value, literals)
    if isinstance(value, Mapping):
        return {str(k): sanitize_value(v, literals) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_value(v, literals) for v in value]
    return value


def default_evidence_dir(root: Path = DEFAULT_EVIDENCE_ROOT) -> Path:
    """``.cua-out/<timestamp>/`` (spec 16.1)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return root / stamp


class EvidenceWriter:
    """Sanitizing writer for one evidence directory (``meta.json`` + ``events.jsonl``)."""

    def __init__(self, directory: Path, literals: Literals) -> None:
        self.directory = Path(directory)
        self.literals = literals
        self.directory.mkdir(parents=True, exist_ok=True)
        self._events_path = self.directory / "events.jsonl"
        self._meta_path = self.directory / "meta.json"

    def write_meta(self, meta: Mapping[str, Any]) -> Path:
        clean = sanitize_value(dict(meta), self.literals)
        self._meta_path.write_text(json.dumps(clean, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return self._meta_path

    def write_events(self, events: Iterable[Mapping[str, Any]]) -> Path:
        with self._events_path.open("w", encoding="utf-8") as fh:
            for event in events:
                clean = sanitize_value(dict(event), self.literals)
                fh.write(json.dumps(clean, sort_keys=True) + "\n")
        return self._events_path
