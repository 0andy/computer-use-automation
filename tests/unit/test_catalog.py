"""Catalog is a metadata projection over artifact JSON files; nothing else exists (docs/spec.md 3.4, 13)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cua import catalog
from cua.catalog import CatalogEntry, CatalogError, project, scan
from cua.compiler import CAPABILITIES_DIR
from cua.config import CONFIG_DIR
from cua.models import CapabilityArtifact
from tests.unit.test_models import ARTIFACT

PROJECTED_FIELDS = ["app", "capability_id", "description", "revision", "inputs", "outputs"]


def write(root: Path, app: str, capability_id: str, payload: dict) -> Path:
    path = root / app / f"{capability_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_catalog_projects_metadata_only_and_has_no_risk_summary() -> None:
    entry = project(CapabilityArtifact.model_validate(ARTIFACT))
    assert list(CatalogEntry.model_fields) == PROJECTED_FIELDS
    assert entry.model_dump() == {
        "app": "mockbank",
        "capability_id": "lookup_member_balance",
        "description": "Look up a member and return the current savings balance.",
        "revision": 1,
        "inputs": {"member_id": "string"},
        "outputs": {"savings_balance": "money"},
    }
    assert "risk" not in json.dumps(entry.model_dump()).lower()
    assert "steps" not in entry.model_dump() and "success" not in entry.model_dump()


def test_scan_validates_each_file_as_capability_artifact(tmp_path: Path) -> None:
    root = tmp_path / "capabilities"
    write(root, "mockbank", "lookup_member_balance", ARTIFACT)
    assert [e.capability_id for e in scan(root)] == ["lookup_member_balance"]

    write(root, "mockbank", "broken", {**ARTIFACT, "capability_id": "broken", "schema_version": "2.0"})
    with pytest.raises(CatalogError) as exc:
        scan(root)
    assert "broken.json" in str(exc.value)


def test_scan_is_recursive_ordered_and_filterable(tmp_path: Path) -> None:
    root = tmp_path / "capabilities"
    write(root, "zeta", "b_cap", {**ARTIFACT, "app": "zeta", "capability_id": "b_cap"})
    write(root, "mockbank", "lookup_member_balance", ARTIFACT)
    write(root, "mockbank", "a_cap", {**ARTIFACT, "capability_id": "a_cap"})
    assert [(e.app, e.capability_id) for e in scan(root)] == [
        ("mockbank", "a_cap"),
        ("mockbank", "lookup_member_balance"),
        ("zeta", "b_cap"),
    ]
    assert [e.capability_id for e in scan(root, app="zeta")] == ["b_cap"]
    assert catalog.find("zeta", "b_cap", root).app == "zeta"
    assert catalog.find("zeta", "missing", root) is None
    assert scan(tmp_path / "does-not-exist") == []


def test_no_second_capability_registry() -> None:
    """Artifacts are the only capability definitions: no YAML index under capabilities/ and none in app config."""
    assert CAPABILITIES_DIR.name == "capabilities"
    assert not list(CAPABILITIES_DIR.rglob("*.yaml")) and not list(CAPABILITIES_DIR.rglob("*.yml"))
    for config_file in CONFIG_DIR.glob("*.yaml"):
        text = config_file.read_text(encoding="utf-8")
        assert "capabilities:" not in text and "steps:" not in text
