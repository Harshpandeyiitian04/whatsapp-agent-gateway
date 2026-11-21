import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.llm.anthropic_client import from_anthropic_response, to_anthropic_messages
from app.agent.llm.base import ToolCall, Usage
from app.agent.llm.mock import MockLLM
from app.agent.llm.openai_compat import from_openai_response, to_openai_messages
from app.agent.llm.pricing import cost_usd
from app.agent.prompts import build_system_prompt
from app.agent.tools import TOOLS, ToolContext, ToolExecutor, TurnEffects
from app.obs.logs import mask_phone

TODAY = date(2026, 11, 20)
OFFERED = [
    {"slot_id": "S-20261121-0800", "time": "Sat 21 Nov, 08:00"},
    {"slot_id": "S-20261121-0830", "time": "Sat 21 Nov, 08:30"},
]


def system(context: dict[str, Any]) -> str:
    return build_system_prompt("Lab", TODAY, "COLLECTING", context)


async def decide(
    text: str, context: dict[str, Any] | None = None, tool_results: list[tuple[str, dict[str, Any]]] | None = None
) -> tuple[str, list[ToolCall]]:
    msgs: list[dict[str, Any]] = [{"role": "user", "content": text}]
    for name, result in tool_results or []:
        msgs.append({"role": "tool", "tool_call_id": "x", "name": name, "content": json.dumps(result)})
    resp = await MockLLM("claude-haiku-5-5").complete(system(context or {}), msgs, TOOLS)
    return resp.text, resp.tool_calls


async def test_mock_checks_slots_for_test_and_date() -> None:
    _, [call] = await decide("I want a thyroid test tomorrow")
    assert call.name == "check_slots"
    assert call.arguments == {"test_code": "TSH", "date": "2026-11-21"}


async def test_mock_books_offered_slot_by_index() -> None:
    _, [call] = await decide("slot 2, my name is asha rao", {"test_code": "CBC", "offered_slots": OFFERED})
    assert call.name == "book_test"
    assert call.arguments == {"test_code": "CBC", "slot_id": "S-20261121-0830", "patient_name": "Asha Rao"}


async def test_mock_never_books_unrequested_day_when_list_is_empty() -> None:
    text, calls = await decide("Slot 3, my name is Asha Rao", {"test_code": "CBC", "offered_slots": []})
    assert calls == [] and "Which day" in text


async def test_mock_out_of_range_slot_asks_instead_of_searching() -> None:
    text, calls = await decide("Slot 2, my name is Asha Rao", {"test_code": "CBC", "offered_slots": OFFERED[:1]})
    assert calls == [] and "Please pick one of these" in text


async def test_mock_thanks_does_not_search_again() -> None:
    text, calls = await decide("Thanks!", {"test_code": "CBC", "booking": {"slot_id": "S-20261121-0800"}})
    assert calls == [] and "welcome" in text


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("What does my report result mean?", "medical_question"),
        ("This is the worst service!!!", "angry_user"),
        ("Can I talk to a human", "user_request"),
    ],
)
async def test_mock_hands_off(text: str, reason: str) -> None:
    _, [call] = await decide(text)
    assert call.name == "handoff_to_human" and call.arguments["reason"] == reason


async def test_mock_lists_slots_after_tool_result() -> None:
    text, calls = await decide("CBC please", tool_results=[("check_slots", {"ok": True, "slots": OFFERED})])
    assert not calls and "1) Sat 21 Nov, 08:00" in text


async def test_mock_usage_is_deterministic_estimate() -> None:
    llm = MockLLM("claude-haiku-5-5")
    a = await llm.complete(system({}), [{"role": "user", "content": "hi"}], TOOLS)
    b = await llm.complete(system({}), [{"role": "user", "content": "hi"}], TOOLS)
    assert a.usage == b.usage and a.usage.input_tokens > 100
    assert a.model == "mock:claude-haiku-5-5"


def test_pricing_known_and_unknown() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert cost_usd("claude-haiku-5-5", usage) == Decimal("0.61")
    assert cost_usd("mock:claude-haiku-5-5", usage) == Decimal("0.61")
    assert cost_usd("some-new-model", usage) is None  # unknown is unknown, never 0


class _NoLab:
    async def get_slots(self, *a: object, **k: object) -> list[dict[str, str]]:
        raise AssertionError("must not be called")


async def _execute(call: ToolCall, context: dict[str, Any]) -> tuple[dict[str, Any], TurnEffects]:
    executor = ToolExecutor(_NoLab(), None, max_failed_bookings=3)  # type: ignore[arg-type]
    effects = TurnEffects()
    result = await executor.execute(call, ToolContext(1, "919999900001", context, effects))
    return result, effects


async def test_book_rejects_slot_not_offered() -> None:
    result, effects = await _execute(
        ToolCall("1", "book_test", {"test_code": "CBC", "slot_id": "S-20261125-0800", "patient_name": "Asha"}),
        {"offered_slots": OFFERED},
    )
    assert result == {"ok": False, "error": "slot_not_offered", "hint": "call check_slots first"}
    assert effects.failed_bookings == 1


async def test_book_rejects_bad_args_and_ignores_phone_argument() -> None:
    result, _ = await _execute(
        ToolCall(
            "1",
            "book_test",
            {"test_code": "XRAY", "slot_id": "S-20261121-0800", "patient_name": "Asha", "phone": "910000000000"},
        ),
        {"offered_slots": OFFERED},
    )
    assert result["error"] == "invalid_args"


async def test_third_failed_booking_triggers_handoff() -> None:
    ctx = {"offered_slots": OFFERED, "failed_booking_attempts": 2}
    _, effects = await _execute(ToolCall("1", "book_test", {"test_code": "CBC", "slot_id": "bad"}), ctx)
    assert effects.handoff == ("booking_failed", "3 failed booking attempts")


async def test_check_slots_validates_date() -> None:
    result, _ = await _execute(ToolCall("1", "check_slots", {"test_code": "CBC", "date": "21/11"}), {})
    assert result == {"ok": False, "error": "invalid_args", "field": "date"}


def test_anthropic_tool_results_grouped_into_one_user_message() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a", "name": "check_slots", "arguments": {}},
                {"id": "b", "name": "check_slots", "arguments": {}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "name": "check_slots", "content": "{}"},
        {"role": "tool", "tool_call_id": "b", "name": "check_slots", "content": "{}"},
    ]
    out = to_anthropic_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["a", "b"]


def test_anthropic_response_parsed_with_cache_tokens() -> None:
    blocks = [
        SimpleNamespace(type="text", text="Checking", model_dump=lambda **_: {"type": "text"}),
        SimpleNamespace(
            type="tool_use",
            id="t1",
            name="check_slots",
            input={"test_code": "CBC"},
            model_dump=lambda **_: {"type": "tool_use"},
        ),
    ]
    resp = SimpleNamespace(
        content=blocks,
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=900, output_tokens=40, cache_read_input_tokens=300, cache_creation_input_tokens=0
        ),
    )
    out = from_anthropic_response(resp, "claude-haiku-5-5")
    assert out.tool_calls == [ToolCall("t1", "check_slots", {"test_code": "CBC"})]
    assert out.usage == Usage(900, 40, 300, 0)


def test_openai_roundtrip_subtracts_cached_tokens() -> None:
    msgs = to_openai_messages(
        "sys",
        [{"role": "assistant", "content": "", "tool_calls": [{"id": "c", "name": "book_test", "arguments": {"x": 1}}]}],
    )
    assert msgs[1]["tool_calls"][0]["function"]["arguments"] == '{"x": 1}'
    out = from_openai_response(
        {
            "choices": [{"message": {"content": "ok", "tool_calls": None}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 400}},
        },
        "gpt-5-mini",
    )
    assert out.usage == Usage(600, 50, 400, 0) and out.text == "ok"


def test_phone_masking() -> None:
    assert mask_phone("919999900012") == "+91 99999 0****"


def test_prompt_keeps_rules_added_after_real_model_demo() -> None:
    # Regression guard for docs/REAL_LLM_DEMO.md findings: the real model booked 09:00 for "the next slot
    # after 08:00" and promised a further confirmation after an already-confirmed booking.
    prompt = system({})
    assert "Resolve relative picks" in prompt and "time order" in prompt
    assert "Do not promise any" in prompt
    assert "Politely decline anything unrelated" in prompt
