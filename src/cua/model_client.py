"""Narrow, injectable model client seam for Discovery (docs/spec.md 8.2, 9.3).

Discovery talks to exactly one method:

    ModelClient.create(system, messages, tools) -> ModelReply

``AnthropicModelClient`` is the real implementation over the direct Anthropic
SDK (no agent framework). ``anthropic`` is imported lazily inside it, so
importing this module - and the whole test suite - works without an API key.
Tests inject a fake client that returns scripted ``ModelReply`` objects.

``ModelReply`` carries the audit metadata evidence may persist (message id,
actual model id, token usage, stop reason), the parsed tool calls, and the
opaque assistant content the loop echoes back on the next turn. The content
stays in runtime memory only; it is never persisted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
CUA_MODEL = "CUA_MODEL"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 8000


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ModelReply:
    message_id: str
    model: str  # the actual model id reported by the API
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    tool_calls: list[ToolCall]
    content: Any = field(default=None, repr=False)  # opaque; echoed back next turn, never persisted

    def audit(self) -> dict[str, Any]:
        """The only per-call data evidence persists (spec 9.3)."""
        return {
            "message_id": self.message_id,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class ModelClient(Protocol):
    @property
    def model(self) -> str: ...

    def create(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply: ...


class ModelConfigError(RuntimeError):
    """The real client cannot be constructed (for example no API key)."""


def configured_model() -> str:
    return os.environ.get(CUA_MODEL) or DEFAULT_MODEL


class AnthropicModelClient:
    """Direct Anthropic Messages API client; one call per Discovery round."""

    def __init__(self, model: str | None = None, api_key: str | None = None, max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        key = api_key or os.environ.get(ANTHROPIC_API_KEY)
        if not key:
            raise ModelConfigError(f"{ANTHROPIC_API_KEY} is not set; genuine Discovery needs it")
        import anthropic  # lazy: nothing else in the suite needs the SDK

        self._model = model or configured_model()
        self._max_tokens = max_tokens
        self._client = anthropic.Anthropic(api_key=key)

    @property
    def model(self) -> str:
        return self._model

    def create(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        )
        calls = [
            ToolCall(id=block.id, name=block.name, input=dict(block.input or {}))
            for block in response.content
            if block.type == "tool_use"
        ]
        return ModelReply(
            message_id=response.id,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
            tool_calls=calls,
            content=response.content,
        )
