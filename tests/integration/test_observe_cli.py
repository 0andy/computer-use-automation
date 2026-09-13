"""``cua observe --app mockbank`` prints a compact semantic Observation with redacted input values."""

from __future__ import annotations

import os
import subprocess
import sys

from cua.cli import REDACTED_VALUE, format_control, format_observation
from cua.models import BBox, Control, Observation


def test_format_redacts_input_values_and_keeps_semantics() -> None:
    obs = Observation(
        url="http://localhost:8000/",
        frames={"top": "http://localhost:8000/", "top/main": "http://localhost:8000/members"},
        controls=[
            Control(
                ref="1:1:2",
                role="textbox",
                label="Member ID",
                input_value="12345",
                attrs={"name": "member_id", "type": "text"},
                frame_path="top/main",
                bbox=BBox(x=1, y=2, width=3, height=4),
                table_index=1,
                row_index=0,
                col_index=1,
            ),
            Control(ref="1:0:1", role="link", name="Members", text="Members", href="http://localhost:8000/members", frame_path="top", bbox=BBox(x=0, y=0, width=1, height=1)),
        ],
    )
    text = format_observation(obs)
    assert "12345" not in text
    assert f"value={REDACTED_VALUE}" in text
    assert "label='Member ID'" in text and "name='member_id'" in text and "table=1/0/1" in text
    assert "href='http://localhost:8000/members'" in text
    assert "top/main: http://localhost:8000/members" in text
    assert "value=" not in format_control(obs.controls[1])  # no input value -> no marker


def test_observe_command_prints_compact_observation(mockbank_server) -> None:
    env = {**os.environ, "PYTHONUTF8": "1", "MOCKBANK_BASE_URL": mockbank_server.base_url}
    proc = subprocess.run(
        [sys.executable, "-m", "cua.cli", "observe", "--app", "mockbank"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert out.startswith(f"url: {mockbank_server.base_url}/")
    assert f"top/main: {mockbank_server.base_url}/members" in out
    assert "textbox" in out and "label='Member ID'" in out and f"value={REDACTED_VALUE}" in out
    assert "button name='Search'" in out
    assert "link name='Members'" in out
