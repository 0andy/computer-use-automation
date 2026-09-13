"""App config loading and the test base-URL override (docs/spec.md 7.1, 17.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cua.config import CONFIG_DIR, AppConfig, load_app_config

FIXTURE_BASE = "http://127.0.0.1:54321"


@pytest.fixture
def no_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOCKBANK_BASE_URL", raising=False)


def test_loads_authored_mockbank_config(no_env_override: None) -> None:
    config = load_app_config("mockbank")
    assert config.app == "mockbank"
    assert config.entry_url == "http://localhost:8000/"
    assert config.allowlist.origin == "http://localhost:8000"
    assert "/settings" in config.allowlist.denied_routes
    assert config.allowed_actions == ["navigate", "click", "fill", "read"]
    assert config.risk_rules[0].match.name == "Close Account"
    assert config.risk_rules[0].decision == "block"
    assert config.business_outcomes[0].code == "MEMBER_NOT_FOUND"
    assert config.business_outcomes[0].when.kind == "text_visible"
    assert config.recoveries[0].code == "DISMISS_SYSTEM_NOTICE"
    assert config.recoveries[0].action.kind == "click"
    assert config.recoveries[0].target.locators[0].kind == "role_name"


def test_env_override_replaces_entry_url_and_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCKBANK_BASE_URL", FIXTURE_BASE)
    config = load_app_config("mockbank")
    assert config.entry_url == FIXTURE_BASE + "/"
    assert config.allowlist.origin == FIXTURE_BASE
    # Everything authored stays as it was.
    assert config.allowlist.allowed_routes == ["/", "/members", "/member", "/notice", "/override"]
    assert config.allowlist.denied_routes == ["/settings"]


def test_explicit_base_url_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCKBANK_BASE_URL", "http://127.0.0.1:1")
    config = load_app_config("mockbank", base_url="http://127.0.0.1:2/")
    assert config.entry_url == "http://127.0.0.1:2/"
    assert config.allowlist.origin == "http://127.0.0.1:2"


def test_app_config_constructible_directly_with_base_url(no_env_override: None) -> None:
    authored = load_app_config("mockbank")
    rebased = authored.with_base_url(FIXTURE_BASE)
    assert rebased.entry_url == FIXTURE_BASE + "/"
    assert rebased.allowlist.origin == FIXTURE_BASE
    assert authored.entry_url == "http://localhost:8000/"  # original untouched
    with pytest.raises(ValueError):
        authored.with_base_url("not-a-url")


def test_config_is_closed_and_has_no_capability_definitions(tmp_path: Path) -> None:
    text = (CONFIG_DIR / "mockbank.yaml").read_text(encoding="utf-8")
    authored = yaml.safe_load(text)
    assert set(authored) == {
        "app", "entry_url", "allowlist", "allowed_actions", "risk_rules", "business_outcomes", "recoveries"
    }
    bad = tmp_path / "mockbank.yaml"
    bad.write_text(text + "\ncapabilities:\n  - lookup_member_balance\n", encoding="utf-8")
    with pytest.raises(Exception):
        load_app_config("mockbank", config_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        load_app_config("otherbank", config_dir=tmp_path)
    with pytest.raises(ValueError):
        load_app_config("Not Valid")
    assert isinstance(AppConfig.model_validate_json(load_app_config("mockbank").model_dump_json()), AppConfig)
