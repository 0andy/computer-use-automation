"""Capability Catalog: a metadata projection over capability artifacts (docs/spec.md 3.4, 13).

It scans ``capabilities/**/*.json``, validates every file as a
``CapabilityArtifact`` and exposes metadata only:

    app, capability_id, description, revision, inputs {name: type}, outputs {name: type}

The artifact JSON is the source of truth. There is no second registry, no
YAML index and no risk-summary field. An upstream agent reads this projection
to choose a capability; Replay then loads the exact artifact by
``(app, capability_id)``.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from cua.compiler import CAPABILITIES_DIR
from cua.models import CapabilityArtifact


class CatalogError(ValueError):
    """A file under the capabilities root is not a valid CapabilityArtifact."""


class CatalogEntry(BaseModel):
    """Exactly the metadata the Catalog exposes; nothing else."""

    model_config = ConfigDict(extra="forbid")

    app: str
    capability_id: str
    description: str
    revision: int
    inputs: dict[str, str]  # name -> type
    outputs: dict[str, str]  # name -> type


def project(artifact: CapabilityArtifact) -> CatalogEntry:
    return CatalogEntry(
        app=artifact.app,
        capability_id=artifact.capability_id,
        description=artifact.description,
        revision=artifact.revision,
        inputs={name: spec.type for name, spec in artifact.inputs.items()},
        outputs={name: spec.type for name, spec in artifact.outputs.items()},
    )


def load_artifact(path: Path) -> CapabilityArtifact:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return CapabilityArtifact.model_validate(raw)
    except (OSError, ValueError, ValidationError) as exc:
        raise CatalogError(f"{path} is not a valid capability artifact: {exc}") from exc


def scan(root: Path = CAPABILITIES_DIR, app: str | None = None) -> list[CatalogEntry]:
    """Every artifact under ``root`` (recursively), projected to metadata, ordered by (app, capability_id)."""
    root = Path(root)
    entries: list[CatalogEntry] = []
    if not root.is_dir():
        return entries
    for path in sorted(root.rglob("*.json")):
        entry = project(load_artifact(path))
        if app is None or entry.app == app:
            entries.append(entry)
    entries.sort(key=lambda e: (e.app, e.capability_id))
    return entries


def find(app: str, capability_id: str, root: Path = CAPABILITIES_DIR) -> CatalogEntry | None:
    for entry in scan(root, app=app):
        if entry.capability_id == capability_id:
            return entry
    return None
