"""Anthropic Messages API adapter (official SDK)."""

from typing import Any

import anthropic

from app.agent.llm.base import LLMError, LLMResponse, Message, ToolCall, ToolSpec, Usage


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Neutral -> Anthropic. Consecutive tool results go back as ONE user message (keeps parallel calls)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            if m.get("raw"):
                out.append({"role": "assistant", "content": m["raw"]})  # replay blocks unchanged
                continue
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["arguments"]})
            out.append({"role": "assistant", "content": blocks or m.get("content", "")})
        elif m["role"] == "tool":
            result = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(result)
            else:
                out.append({"role": "user", "content": [result]})
    return out


def from_anthropic_response(resp: Any, model: str) -> LLMResponse:
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in resp.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            calls.append(ToolCall(block.id, block.name, dict(block.input or {})))
    u = resp.usage
    usage = Usage(
        input_tokens=int(u.input_tokens or 0),
        output_tokens=int(u.output_tokens or 0),
        cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
    )
    raw = [b.model_dump(exclude_none=True) for b in resp.content]
    return LLMResponse(
        "\n".join(text_parts).strip(), calls, usage, model, refused=resp.stop_reason == "refusal", raw=raw
    )


class AnthropicLLM:
    mode = "real"

    def __init__(self, api_key: str, model: str, max_tokens: int, timeout_s: float) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=2)

    async def complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        try:
            resp = await self._client.messages.create(  # type: ignore[call-overload]
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=to_anthropic_messages(messages),
                tools=[{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools],
                output_config={"effort": "low"},  # short chat turns: low effort keeps tokens down
            )
        except anthropic.APIError as exc:
            raise LLMError(f"anthropic: {type(exc).__name__}") from exc
        return from_anthropic_response(resp, self.model)
