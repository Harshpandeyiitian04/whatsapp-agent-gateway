"""Deterministic rule-based stand-in for the LLM (LLM_MODE=mock, zero cost).

It reads exactly what a real model reads (system prompt incl. TODAY/CONTEXT_JSON, messages, tool
results) and returns the same response shape, so the agent loop, tools, and validation are exercised
end-to-end. Token counts are estimated as ceil(chars / 4) and priced as `mock_price_model`.
"""

import json
import math
import re
from datetime import date, timedelta
from typing import Any

from app.agent.llm.base import LLMResponse, Message, ToolCall, ToolSpec, Usage

TEST_PATTERNS = [
    ("CBC", r"\bcbc\b|blood count"),
    ("LIPID", r"lipid|cholesterol"),
    ("TSH", r"\btsh\b|thyroid"),
    ("HBA1C", r"hba1c|sugar|diabetes"),
    ("VITD", r"vitamin\s*d|\bvit\s*d\b|\bvitd\b"),
]
MEDICAL = re.compile(r"\b(pain|fever|result|report|dose|dosage|medicine|pregnan\w*|symptom\w*|side effect)\b", re.I)
ANGRY = re.compile(r"\b(angry|worst|useless|terrible|complaint|refund|ridiculous)\b|!!!", re.I)
HUMAN = re.compile(r"\b(human|agent|real person|call me|talk to someone)\b", re.I)
SLOT_ID = re.compile(r"\bS-\d{8}-\d{4}\b")
SLOT_INDEX = re.compile(r"\b(?:slot|option|number|no\.?)\s*#?\s*([1-9])\b|^\s*([1-9])\b", re.I)
NAME = re.compile(r"(?:my name is|name is|name:|i am|i'm|this is)\s+([A-Za-z][A-Za-z .'-]{1,60})", re.I)
ISO_DATE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def _context(system: str) -> tuple[dict[str, Any], date | None]:
    ctx: dict[str, Any] = {}
    today: date | None = None
    for line in system.splitlines():
        if line.startswith("CONTEXT_JSON: "):
            ctx = json.loads(line.removeprefix("CONTEXT_JSON: "))
        elif line.startswith("TODAY: "):
            today = date.fromisoformat(line.removeprefix("TODAY: ").strip())
    return ctx, today


def _test_code(text: str) -> str | None:
    for code, pattern in TEST_PATTERNS:
        if re.search(pattern, text, re.I):
            return code
    return None


def _date(text: str, today: date | None) -> str | None:
    if m := ISO_DATE.search(text):
        return m.group(1)
    if today and re.search(r"day after tomorrow", text, re.I):
        return (today + timedelta(days=2)).isoformat()
    if today and re.search(r"\btomorrow\b", text, re.I):
        return (today + timedelta(days=1)).isoformat()
    return None


def _name(text: str) -> str | None:
    if m := NAME.search(text):
        return m.group(1).strip(" .").title()[:80]
    return None


class MockLLM:
    mode = "mock"

    def __init__(self, price_model: str) -> None:
        self.model = f"mock:{price_model}"
        self._calls = 0

    async def complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        text, calls = self._decide(system, messages)
        self._calls += 1
        prompt = system + json.dumps(messages, default=str) + json.dumps([t.input_schema for t in tools])
        output = text + json.dumps([c.as_dict() for c in calls])
        return LLMResponse(text, calls, Usage(estimate_tokens(prompt), estimate_tokens(output)), self.model)

    def _call(self, name: str, args: dict[str, Any]) -> tuple[str, list[ToolCall]]:
        return "", [ToolCall(f"mock_{self._calls}_{name}", name, args)]

    def _decide(self, system: str, messages: list[Message]) -> tuple[str, list[ToolCall]]:
        ctx, today = _context(system)
        last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
        user_text = messages[last_user]["content"] if last_user >= 0 else ""
        results = [(m["name"], json.loads(m["content"])) for m in messages[last_user + 1 :] if m["role"] == "tool"]
        done = {name for name, _ in results}

        if "handoff_to_human" not in done:
            for pattern, reason in ((MEDICAL, "medical_question"), (ANGRY, "angry_user"), (HUMAN, "user_request")):
                if pattern.search(user_text):
                    return self._call("handoff_to_human", {"reason": reason, "summary": user_text[:200]})

        test = _test_code(user_text) or ctx.get("test_code")
        name = _name(user_text) or ctx.get("patient_name")
        offered: list[dict[str, str]] = list(ctx.get("offered_slots", []))
        for tool_name, result in results:
            if tool_name == "check_slots" and result.get("ok"):
                offered = result["slots"] + [s for s in offered if s not in result["slots"]]

        if results and results[-1][0] == "book_test":
            return self._after_booking(results[-1][1]), []

        wanted = self._wanted_slot(user_text, offered)
        day = _date(user_text, today)
        asks_search = bool(_test_code(user_text) or day) and "check_slots" not in done
        if wanted is None and SLOT_INDEX.search(user_text) and not asks_search:
            # A slot reference we cannot resolve: ask; never silently search a day the user did not ask for.
            if offered:
                return f"Please pick one of these: {self._list(offered[:5])}", []
            return "I don't have open slots listed for you right now. Which day would you like?", []
        if wanted is not None and test and name:
            if wanted.get("slot_id") in {s["slot_id"] for s in offered}:
                return self._call("book_test", {"test_code": test, "slot_id": wanted["slot_id"], "patient_name": name})
            if "check_slots" not in done:
                return self._call(
                    "check_slots",
                    {
                        "test_code": test,
                        "date": wanted["slot_id"][2:6] + "-" + wanted["slot_id"][6:8] + "-" + wanted["slot_id"][8:10],
                    },
                )
            return f"Sorry, that slot is no longer available. {self._list(offered[:5])}", []
        if wanted is not None and not name:
            return "Please share the patient's full name to confirm that slot (e.g. 'my name is Asha Rao').", []

        if results and results[-1][0] == "check_slots":
            res = results[-1][1]
            if not res.get("ok"):
                return "Our booking system is slow right now. Please try again in a few minutes.", []
            return (self._list(res["slots"]) if res["slots"] else "No free slots that day. Try another date?"), []

        if test and asks_search:
            args: dict[str, Any] = {"test_code": test}
            if day:
                args["date"] = day
            return self._call("check_slots", args)
        if ctx.get("booking"):
            return "You're welcome! Reply here any time if you need another test.", []
        return ("Hi! I can book CBC, LIPID, TSH, HBA1C or VITD collection slots. Which test, and which day?"), []

    @staticmethod
    def _wanted_slot(text: str, offered: list[dict[str, str]]) -> dict[str, str] | None:
        if m := SLOT_ID.search(text):
            return next((s for s in offered if s["slot_id"] == m.group(0)), {"slot_id": m.group(0)})
        if (m := SLOT_INDEX.search(text)) and offered:
            idx = int(m.group(1) or m.group(2)) - 1
            if idx < len(offered):
                return offered[idx]
        return None

    @staticmethod
    def _list(slots: list[dict[str, str]]) -> str:
        if not slots:
            return "No free slots right now. Try another date?"
        options = "; ".join(f"{i}) {s['time']}" for i, s in enumerate(slots, 1))
        return f"Available: {options}. Reply with a slot number and the patient's name."

    @staticmethod
    def _after_booking(result: dict[str, Any]) -> str:
        if result.get("status") == "confirmed":
            return f"Booked! {result['test_code']} on {result['time']}. Booking id {result['booking_id']}."
        if result.get("status") == "confirming":
            return f"We're confirming your {result['time']} slot with the lab and will message you shortly."
        error = result.get("error")
        if error == "slot_taken":
            return "Sorry, that slot was just taken. Want me to check other times?"
        if error == "already_booked":
            return "You already have an active booking for this test."
        if error == "slot_not_offered":
            return "Please pick one of the slots I listed."
        return "Sorry, I couldn't book that right now. Please try another slot."
