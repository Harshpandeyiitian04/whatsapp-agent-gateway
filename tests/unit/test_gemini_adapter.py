"""OpenAI-compatible adapter in Gemini mode: retries, Retry-After, id fallback, raw replay, key handling."""

import json
from typing import Any

import httpx
import pytest

from app.agent.llm.base import LLMError
from app.agent.llm.openai_compat import OpenAICompatLLM, simplify_schema, to_openai_messages
from app.agent.tools import TOOLS
from app.config import Settings

SECRET_KEY = "test-key-value-that-must-never-leak"
OK_BODY = {
    "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
}


def _llm(handler: Any, sleeps: list[float], model: str = "gemini-3.1-flash-lite") -> OpenAICompatLLM:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://gemini.test/v1beta/openai",
        headers={"Authorization": f"Bearer {SECRET_KEY}"},
    )

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    return OpenAICompatLLM("", SECRET_KEY, model, 256, 5, gemini=True, max_retries=3, http=http, sleep=fake_sleep)


async def test_429_honours_retry_after_then_succeeds() -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"message": "quota"}})
        return httpx.Response(200, json=OK_BODY)

    sleeps: list[float] = []
    resp = await _llm(handler, sleeps).complete("sys", [{"role": "user", "content": "x"}], TOOLS)
    assert resp.text == "hi" and len(calls) == 3
    assert all(7 <= s <= 8 for s in sleeps)  # Retry-After + jitter


async def test_persistent_failure_raises_without_leaking_key() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    with pytest.raises(LLMError) as err:
        await _llm(handler, []).complete("sys", [{"role": "user", "content": "x"}], TOOLS)
    assert "503" in str(err.value) and SECRET_KEY not in str(err.value)


async def test_non_retryable_400_fails_fast() -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json=[{"error": {"message": "bad schema"}}])

    with pytest.raises(LLMError, match="bad schema"):
        await _llm(handler, []).complete("sys", [{"role": "user", "content": "x"}], TOOLS)
    assert len(calls) == 1


async def test_gemini_body_and_missing_tool_ids_and_raw_replay() -> None:
    seen: list[dict[str, Any]] = []
    reply = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "extra_content": {"google": {"thought_signature": "sig"}},
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": "check_slots", "arguments": json.dumps({"test_code": "CBC"})},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 600, "completion_tokens": 40},
    }

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=reply)

    llm = _llm(handler, [])
    resp = await llm.complete("sys", [{"role": "user", "content": "CBC"}], TOOLS)
    body = seen[0]
    assert body["reasoning_effort"] == "minimal" and body["max_tokens"] == 256 and body["tool_choice"] == "auto"
    assert "max_completion_tokens" not in body
    assert body["tools"][1]["function"]["parameters"]["properties"]["slot_id"]["pattern"]  # strict by default
    assert resp.tool_calls[0].id == "call_0"
    replayed = to_openai_messages(
        "sys",
        [{"role": "assistant", "content": "", "raw": resp.raw, "tool_calls": [c.as_dict() for c in resp.tool_calls]}],
    )[1]
    assert replayed["extra_content"] == {"google": {"thought_signature": "sig"}}  # provider fields survive
    assert replayed["tool_calls"][0]["id"] == "call_0"


def test_gemini_25_disables_thinking() -> None:
    body = _llm(lambda r: httpx.Response(200, json=OK_BODY), [], model="gemini-2.5-flash-lite").build_body("s", [], [])
    assert body["reasoning_effort"] == "none"


def test_simplify_schema_strips_unsupported_keywords() -> None:
    out = simplify_schema(TOOLS[1].input_schema)
    text = json.dumps(out)
    for key in ("pattern", "additionalProperties", "minLength", "maxLength"):
        assert key not in text
    assert out["required"] == ["test_code", "slot_id", "patient_name"]


def test_api_key_alias_and_repr_hides_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("API_KEY", SECRET_KEY)
    settings = Settings()
    assert settings.llm_api_key == SECRET_KEY
    assert SECRET_KEY not in repr(settings) and SECRET_KEY not in str(settings)
