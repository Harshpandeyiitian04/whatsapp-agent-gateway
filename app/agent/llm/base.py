"""Provider-neutral LLM types. Adapters translate to/from these.

Neutral message shapes (plain dicts so they serialise into conversation history):
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [ToolCall-as-dict], "raw": <provider blocks>}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str (JSON)}
`raw` lets an adapter replay its own content blocks unchanged within a turn (e.g. thinking blocks).
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

Message = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(slots=True)
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    model: str
    refused: bool = False
    raw: Any = field(default=None)


class LLMError(Exception):
    """Provider call failed (after the SDK's own retries)."""


class LLMClient(Protocol):
    mode: str  # "mock" | "real"
    model: str  # label recorded in usage rows; also the pricing key

    async def complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse: ...
