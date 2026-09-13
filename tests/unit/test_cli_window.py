"""``--window-size`` / ``--window-position`` parsers (demo-recording window placement): no browser."""

from __future__ import annotations

import pytest

from cua.cli import parse_window_options, parse_window_position, parse_window_size


def test_parse_window_size_accepts_wxh() -> None:
    assert parse_window_size("1280x900") == (1280, 900)
    assert parse_window_size(" 800x600 ") == (800, 600)


@pytest.mark.parametrize("bad", ["", "1280", "1280x", "x900", "1280x900x1", "1280,900", "abcx900", "1280x9.5", "0x900", "1280x-1"])
def test_parse_window_size_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError, match="--window-size"):
        parse_window_size(bad)


def test_parse_window_position_accepts_xy() -> None:
    assert parse_window_position("640,0") == (640, 0)
    assert parse_window_position("-1920,0") == (-1920, 0)  # a monitor left of the primary
    assert parse_window_position(" 10 , 20 ") == (10, 20)


@pytest.mark.parametrize("bad", ["", "640", "640,", ",0", "640,0,0", "640x0", "a,0", "640,0.5"])
def test_parse_window_position_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError, match="--window-position"):
        parse_window_position(bad)


def test_parse_window_options_each_optional() -> None:
    assert parse_window_options(None, None) == (None, None)
    assert parse_window_options("1280x900", None) == ((1280, 900), None)
    assert parse_window_options(None, "640,0") == (None, (640, 0))
    assert parse_window_options("1280x900", "640,0") == ((1280, 900), (640, 0))
    with pytest.raises(ValueError):
        parse_window_options("bad", "640,0")
