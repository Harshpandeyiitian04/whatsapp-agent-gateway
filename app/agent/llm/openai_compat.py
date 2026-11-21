"""OpenAI-compatible /chat/completions adapter (any provider exposing that API via LLM_BASE_URL).

Gemini profile (`gemini=True`), per https://aistudio.google.com/docs/openai (read 2026-11-21):
  * base URL https://generativelanguage.googleapis.com/v1beta/openai/, Bearer auth with the Gemini key
  * `reasoning_effort` controls thinking: "none" on 2.5 models, "minimal" (lowest) on Gemini 3+
  * `max_tokens` is sent (the doc does not mention `max_completion_tokens`)
  * live-verified with gemini-3.1-flash-lite: tool ids round-trip, multi-turn tool calls work when the
    assistant message is replayed verbatim, and the full tool JSON schemas are accepted
Tool-call ids are generated if a provider omits them, and the provider's assistant message is
replayed verbatim within a turn so any provider-specific fields (e.g. thought signatures) survive.
"""

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.agent.llm.base import LLMError, LLMResponse, Message, ToolCall, ToolSpec, Usage

log = logging.getLogger("llm")
RETRYABLE = {408, 429, 500, 502, 503, 504}
# JSON-schema keywords outside the subset Gemini documents for function declarations
# (type/properties/required/description/enum/items). Server-side validation stays strict regardless.
GEMINI_UNSUPPORTED_KEYS = {"format", "pattern", "additionalProperties", "default", "minLength", "maxLength"}


def simplify_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: simplify_schema(v) for k, v in schema.items() if k not in GEMINI_UNSUPPORTED_KEYS}
    if isinstance(schema, list):
        return [simplify_schema(v) for v in schema]
    return schema


def to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            if isinstance(m.get("raw"), dict):
                out.append(m["raw"])  # provider's own message, unchanged
                continue
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                    }
                    for tc in m["tool_calls"]
                ]
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
    return out


def from_openai_response(data: dict[str, Any], model: str) -> LLMResponse:
    choice = data["choices"][0]
    msg = dict(choice["message"])
    calls: list[ToolCall] = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        if not tc.get("id"):
            tc["id"] = f"call_{i}"  # keep the replayed message and our tool result consistent
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_invalid_json": tc["function"].get("arguments")}
        calls.append(ToolCall(tc["id"], tc["function"]["name"], args if isinstance(args, dict) else {}))
    u = data.get("usage") or {}
    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    usage = Usage(
        input_tokens=int(u.get("prompt_tokens") or 0) - cached,  # prompt_tokens includes cached ones
        output_tokens=int(u.get("completion_tokens") or 0),
        cache_read_tokens=cached,
    )
    refused = bool(msg.get("refusal")) or choice.get("finish_reason") == "content_filter"
    raw = {k: v for k, v in msg.items() if v is not None}
    raw.setdefault("role", "assistant")
    return LLMResponse((msg.get("content") or "").strip(), calls, usage, model, refused=refused, raw=raw)


def retry_after_s(resp: httpx.Response, attempt: int, rng: random.Random) -> float:
    """Honour Retry-After (seconds) when present, else exponential backoff with full jitter (cap 30 s)."""
    header = resp.headers.get("retry-after", "")
    if header.replace(".", "", 1).isdigit():
        return min(60.0, float(header)) + rng.uniform(0, 1)
    return rng.uniform(0, min(30.0, 2.0 * 2 ** (attempt - 1)))


class OpenAICompatLLM:
    mode = "real"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        max_tokens: int,
        timeout_s: float,
        gemini: bool = False,
        max_retries: int = 4,
        simplify_schemas: bool = False,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.gemini = gemini
        # Live check 2026-11-21: gemini-3.1-flash-lite accepted the full schemas (format/pattern/
        # additionalProperties/minLength), so simplification is opt-in for providers that reject them.
        self.simplify_schemas = bool(simplify_schemas)
        self.max_retries = max_retries
        self.sleep = sleep
        self.rng = random.Random()
        self._http = http or httpx.AsyncClient(
            base_url=base_url.rstrip("/") or "https://api.openai.com/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )

    def build_body(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> dict[str, Any]:
        params = simplify_schema if self.simplify_schemas else (lambda s: s)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(system, messages),
            "tools": [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": params(t.input_schema)},
                }
                for t in tools
            ],
            "tool_choice": "auto",
        }
        if self.gemini:
            body["max_tokens"] = self.max_tokens
            # Short booking chat needs little thinking: "none" turns it off on 2.5; Gemini 3+ cannot turn it
            # off, "minimal" is the lowest level (aistudio.google.com/docs/openai, read 2026-11-21).
            body["reasoning_effort"] = "none" if self.model.startswith("gemini-2.5") else "minimal"
        else:
            body["max_completion_tokens"] = self.max_tokens
        return body

    async def complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        body = self.build_body(system, messages, tools)
        for attempt in range(1, self.max_retries + 2):
            try:
                resp = await self._http.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:  # message never includes headers, so the key cannot leak
                if attempt > self.max_retries:
                    raise LLMError(f"llm transport error: {type(exc).__name__}") from None
                await self.sleep(self.rng.uniform(0, min(30.0, 2.0 * 2 ** (attempt - 1))))
                continue
            if resp.status_code < 300:
                return from_openai_response(resp.json(), self.model)
            if resp.status_code not in RETRYABLE or attempt > self.max_retries:
                raise LLMError(f"llm http {resp.status_code}: {_error_summary(resp)}")
            delay = retry_after_s(resp, attempt, self.rng)
            log.warning("llm call retrying", extra={"status": resp.status_code, "attempt": attempt, "delay_s": delay})
            await self.sleep(delay)
        raise LLMError("unreachable")


def _error_summary(resp: httpx.Response) -> str:
    """Provider error message only (status/message fields), truncated; never request headers."""
    try:
        err = resp.json()
        if isinstance(err, list) and err:
            err = err[0]
        detail = err.get("error", err) if isinstance(err, dict) else err
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("status") or "")[:300]
        return str(detail)[:300]
    except ValueError:
        return resp.text[:300]
