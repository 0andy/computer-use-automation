"""Shared assertion: persisted evidence carries no wall-clock time.

Evidence must be deterministic and diffable across runs of the same capability against
the same app. Ordering comes from ``seq`` / ``attempt``; timing from relative
``elapsed_ms``. Any absolute time (a key naming one, ISO-8601 text, or an epoch-sized
number such as ``Date.now()``) is a regression, so every evidence-writing test path runs
``assert_no_wall_clock`` over the files it produced.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# key names that denote an absolute time (durations such as timeout_s / elapsed_ms do not match)
WALL_CLOCK_KEY = re.compile(
    r"^(at|at_ms|.*_at|.*_at_ms|time|.*_time|timestamp|.*_timestamp|datetime|date|.*_date|ts|.*_ts|created|updated)$",
    re.IGNORECASE,
)
ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EPOCH_MIN = 10**9  # epoch seconds today are ~1.8e9, epoch milliseconds ~1.8e12; no counter gets near this

EVIDENCE_SUFFIXES = (".json", ".jsonl")


def _load(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return [json.loads(text)]


def _walk(value: Any, where: str) -> Iterator[tuple[str, str | None, Any]]:
    """(location, key, leaf) for every leaf; key is the dict key that holds the leaf, if any."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield f"{where}.{key}", key, inner
            yield from _walk(inner, f"{where}.{key}")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _walk(inner, f"{where}[{index}]")


def wall_clock_violations(path: Path) -> list[str]:
    found: list[str] = []
    for document in _load(path):
        for where, key, leaf in _walk(document, path.name):
            if key is not None and WALL_CLOCK_KEY.match(key):
                found.append(f"{where}: wall-clock key {key!r}")
            if isinstance(leaf, str) and (ISO_DATETIME.search(leaf) or ISO_DATE.match(leaf)):
                found.append(f"{where}: date/time text {leaf!r}")
            if isinstance(leaf, (int, float)) and not isinstance(leaf, bool) and leaf >= EPOCH_MIN:
                found.append(f"{where}: epoch-sized number {leaf!r}")
    return found


def assert_no_wall_clock(directory: Path, expected_files: set[str] | None = None) -> None:
    """Fail if any ``*.json`` / ``*.jsonl`` under ``directory`` carries a wall-clock field or value."""
    files = sorted(p for p in Path(directory).rglob("*") if p.suffix in EVIDENCE_SUFFIXES)
    assert files, f"no evidence files under {directory}"
    if expected_files is not None:
        assert expected_files <= {p.name for p in files}, f"missing evidence files under {directory}"
    violations = [v for path in files for v in wall_clock_violations(path)]
    assert not violations, "persisted evidence must be free of wall-clock time:\n" + "\n".join(violations)
