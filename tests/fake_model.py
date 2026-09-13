"""Scripted fake of the ``ModelClient`` seam (docs/spec.md 17.2: no key needed).

A ``FakeModel`` returns scripted tool calls. Each script step is either a
``{"name": ..., "input": {...}}`` dict, ``None`` (a reply with no tool call),
or a callable that receives the latest user text (the same sanitized text a
real model would see, refs included) and returns such a dict. The fake records
every request so tests can assert what the model was shown.

Helpers parse refs out of the rendered observation exactly the way a model
would have to: from the ``[ref] role ...`` lines.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from cua.model_client import ModelReply, ToolCall

Step = dict[str, Any] | None | Callable[[str], dict[str, Any] | None]

_LINE_RE = re.compile(r"^\s*(?P<frame>\S+)\s+\[(?P<ref>[^\]]+)\]\s+(?P<role>\S+)(?P<rest>.*)$")


def last_user_text(messages: list[dict[str, Any]]) -> str:
    """The text of the latest user turn (plain text or the first tool_result content)."""
    for message in reversed(messages):
        if message["role"] != "user":
            continue
        content = message["content"]
        if isinstance(content, str):
            return content
        for block in content:
            if block.get("type") == "tool_result":
                return str(block.get("content", ""))
        return ""
    return ""


def controls_in(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        match = _LINE_RE.match(line)
        if match:
            rows.append({"frame": match["frame"], "ref": match["ref"], "role": match["role"], "rest": match["rest"]})
    return rows


def ref_for(text: str, role: str, **fields: str) -> str:
    """Ref of the unique control line with this role whose rest contains every ``key='value'``."""
    hits = [
        row["ref"]
        for row in controls_in(text)
        if row["role"] == role and all(f"{key}={value!r}" in row["rest"] for key, value in fields.items())
    ]
    assert len(hits) == 1, f"expected one {role} {fields}, found {hits} in:\n{text}"
    return hits[0]


def table_cell_ref(text: str, row_anchor: str, col_offset: int = 1) -> str:
    """Ref of the cell ``col_offset`` to the right of the cell whose text is ``row_anchor``."""
    anchor = ref_for(text, "cell", name=row_anchor)
    rows = {row["ref"]: row for row in controls_in(text)}
    table = re.search(r"table=(\d+)/(\d+)/(\d+)", rows[anchor]["rest"])
    assert table, f"anchor {row_anchor!r} is not a table cell"
    t, r, c = (int(g) for g in table.groups())
    want = f"table={t}/{r}/{c + col_offset}"
    hits = [ref for ref, row in rows.items() if row["role"] == "cell" and want in row["rest"]]
    assert len(hits) == 1, f"expected one cell at {want}, found {hits}"
    return hits[0]


class FakeModel:
    model = "fake-model"

    def __init__(self, script: list[Step], repeat_last: bool = False) -> None:
        self.script = list(script)
        self.repeat_last = repeat_last
        self.requests: list[dict[str, Any]] = []  # every (system, messages, tools) the loop sent
        self.calls = 0

    def create(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        self.requests.append({"system": system, "messages": [dict(m) for m in messages], "tools": tools})
        index = self.calls
        self.calls += 1
        if index >= len(self.script):
            if not (self.repeat_last and self.script):
                raise AssertionError(f"FakeModel script exhausted after {len(self.script)} calls")
            index = len(self.script) - 1
        step = self.script[index]
        if callable(step):
            step = step(last_user_text(messages))
        calls: list[ToolCall] = []
        if step is not None:
            calls.append(ToolCall(id=f"toolu_fake_{self.calls}", name=step["name"], input=dict(step.get("input", {}))))
        return ModelReply(
            message_id=f"msg_fake_{self.calls}",
            model=self.model,
            input_tokens=100 + self.calls,
            output_tokens=10 + self.calls,
            stop_reason="tool_use" if calls else "end_turn",
            tool_calls=calls,
            content=None,  # the loop builds the echoed assistant content itself
        )

    def all_text_shown(self) -> str:
        """Everything the model was ever shown (system prompt + every user turn)."""
        parts: list[str] = []
        for request in self.requests:
            parts.append(request["system"])
            for message in request["messages"]:
                content = message["content"]
                if isinstance(content, str):
                    parts.append(content)
                else:
                    for block in content:
                        if isinstance(block, dict) and "content" in block:
                            parts.append(str(block["content"]))
        return "\n".join(parts)


# -- convenient scripted steps for MockBank -------------------------------------


def fill_member_id(reason: str = "Enter the member identifier for the lookup.") -> Callable[[str], dict[str, Any]]:
    return lambda text: {
        "name": "fill_ref",
        "input": {"ref": ref_for(text, "textbox", label="Member ID"), "from_param": "member_id", "reason": reason},
    }


def click_search(expect: dict[str, Any] | None = None, reason: str = "Submit the member search.") -> Callable[[str], dict[str, Any]]:
    return lambda text: {
        "name": "click_ref",
        "input": {
            "ref": ref_for(text, "button", name="Search"),
            "expect": expect or {"kind": "text_visible", "text": "Member Detail"},
            "reason": reason,
        },
    }


def read_savings(reason: str = "Capture the current savings balance.") -> Callable[[str], dict[str, Any]]:
    return lambda text: {
        "name": "read_ref",
        "input": {"ref": table_cell_ref(text, "Savings", 1), "capture_as": "savings_balance", "parser": "money", "reason": reason},
    }


def done(condition: dict[str, Any] | None = None, reason: str = "Member Detail shows the savings balance.") -> dict[str, Any]:
    return {"name": "done", "input": {"condition": condition or {"kind": "text_visible", "text": "Savings"}, "reason": reason}}


def give_up(code: str = "goal_unreachable", reason: str = "Cannot proceed.") -> dict[str, Any]:
    return {"name": "give_up", "input": {"reason_code": code, "reason": reason}}
