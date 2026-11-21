"""Tool definitions and the server-side executor.

The model only proposes calls. Everything that matters is validated here: argument schema, the slot
must have been offered in this conversation, and the phone number always comes from the conversation.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.agent.llm.base import ToolCall, ToolSpec
from app.clock import IST
from app.lab.booking import BookingService, BookingStatus
from app.lab.client import LabClient, LabUnavailable

TEST_CODES = ["CBC", "LIPID", "TSH", "HBA1C", "VITD"]
SLOT_RE = re.compile(r"^S-\d{8}-\d{4}$")
HANDOFF_REASONS = ["medical_question", "angry_user", "booking_failed", "lab_outage", "user_request", "other"]
MAX_OFFERED = 15

TOOLS: list[ToolSpec] = [
    ToolSpec(
        "check_slots",
        "List available collection slots for a lab test. Returns at most 5 slots.",
        {
            "type": "object",
            "properties": {
                "test_code": {"type": "string", "enum": TEST_CODES},
                "date": {"type": "string", "description": "YYYY-MM-DD; omit for the next available days"},
                "part_of_day": {"type": "string", "enum": ["early", "late", "any"]},
            },
            "required": ["test_code"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "book_test",
        "Book a slot the user explicitly chose. Only use a slot_id returned by check_slots in this conversation.",
        {
            "type": "object",
            "properties": {
                "test_code": {"type": "string", "enum": TEST_CODES},
                "slot_id": {"type": "string", "pattern": SLOT_RE.pattern},
                "patient_name": {"type": "string", "minLength": 2, "maxLength": 80},
            },
            "required": ["test_code", "slot_id", "patient_name"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "handoff_to_human",
        "Transfer the conversation to lab staff: medical questions, complaints or angry users, "
        "requests for a person, or repeated booking failures.",
        {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "enum": HANDOFF_REASONS},
                "summary": {"type": "string", "maxLength": 300},
            },
            "required": ["reason", "summary"],
            "additionalProperties": False,
        },
    ),
]


@dataclass
class TurnEffects:
    """What tools did during a turn; applied in the worker's commit transaction."""

    state: str | None = None
    handoff: tuple[str, str] | None = None  # (reason, summary)
    lab_outage: bool = False
    failed_bookings: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class ToolContext:
    conversation_id: int
    phone: str
    context: dict[str, Any]
    effects: TurnEffects


def slot_label(start_iso: str) -> str:
    start = datetime.fromisoformat(start_iso).astimezone(IST)
    return start.strftime("%a %d %b, %H:%M")


def _err(code: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": code, **extra}


class ToolExecutor:
    def __init__(self, lab: LabClient, bookings: BookingService, max_failed_bookings: int) -> None:
        self.lab = lab
        self.bookings = bookings
        self.max_failed_bookings = max_failed_bookings

    async def execute(self, call: ToolCall, ctx: ToolContext) -> dict[str, Any]:
        args = call.arguments
        if call.name == "check_slots":
            return await self._check_slots(args, ctx)
        if call.name == "book_test":
            return await self._book(args, ctx)
        if call.name == "handoff_to_human":
            return self._handoff(args, ctx)
        return _err("unknown_tool")

    async def _check_slots(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        test_code = args.get("test_code")
        if test_code not in TEST_CODES:
            return _err("invalid_args", field="test_code")
        day: date | None = None
        if args.get("date"):
            try:
                day = date.fromisoformat(str(args["date"]))
            except ValueError:
                return _err("invalid_args", field="date")
        part = args.get("part_of_day", "any")
        if part not in ("early", "late", "any"):
            return _err("invalid_args", field="part_of_day")
        try:
            raw = await self.lab.get_slots(day, part, limit=8)
        except LabUnavailable:
            ctx.effects.lab_outage = True
            return _err("lab_unavailable")
        held = await self._locally_held([s["slot_id"] for s in raw])
        slots = [{"slot_id": s["slot_id"], "time": slot_label(s["start"])} for s in raw if s["slot_id"] not in held][:5]
        offered = [s for s in ctx.context.get("offered_slots", []) if s["slot_id"] not in {x["slot_id"] for x in slots}]
        ctx.context["offered_slots"] = (slots + offered)[:MAX_OFFERED]
        ctx.context["test_code"] = test_code
        if slots:
            ctx.effects.state = "SLOTS_OFFERED"
        return {"ok": True, "test_code": test_code, "slots": slots}

    async def _locally_held(self, slot_ids: list[str]) -> set[str]:
        if not slot_ids:
            return set()
        async with self.bookings.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT slot_id FROM bookings WHERE slot_id = ANY(%s) AND status IN ('pending','confirmed','unknown')",
                (slot_ids,),
            )
            return {r["slot_id"] for r in await cur.fetchall()}

    async def _book(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        test_code, slot_id = args.get("test_code"), str(args.get("slot_id", ""))
        name = str(args.get("patient_name", "")).strip()
        if test_code not in TEST_CODES or not SLOT_RE.match(slot_id) or not 2 <= len(name) <= 80:
            return self._booking_failed(ctx, _err("invalid_args"))
        offered = {s["slot_id"] for s in ctx.context.get("offered_slots", [])}
        if slot_id not in offered:
            return self._booking_failed(ctx, _err("slot_not_offered", hint="call check_slots first"))
        ctx.context["patient_name"] = name
        result = await self.bookings.book(ctx.conversation_id, ctx.phone, test_code, slot_id, name)
        label = next((s["time"] for s in ctx.context.get("offered_slots", []) if s["slot_id"] == slot_id), slot_id)
        if result.status is BookingStatus.CONFIRMED:
            ctx.context["booking"] = {
                "test_code": test_code,
                "slot_id": slot_id,
                "time": label,
                "lab_booking_id": result.lab_booking_id,
            }
            ctx.context["offered_slots"] = []
            ctx.effects.state = "BOOKED"
            return {
                "ok": True,
                "status": "confirmed",
                "booking_id": result.lab_booking_id,
                "test_code": test_code,
                "time": label,
            }
        if result.status is BookingStatus.UNKNOWN:
            ctx.context["booking"] = {"test_code": test_code, "slot_id": slot_id, "time": label, "pending": True}
            ctx.effects.state = "BOOKING_PENDING"
            return {
                "ok": True,
                "status": "confirming",
                "time": label,
                "message": "booking is being confirmed with the lab; the user will be notified",
            }
        if result.lab_unavailable:
            ctx.effects.lab_outage = True
        ctx.context["offered_slots"] = [s for s in ctx.context.get("offered_slots", []) if s["slot_id"] != slot_id]
        return self._booking_failed(ctx, _err(result.status.value, slot_id=result.slot_id))

    def _booking_failed(self, ctx: ToolContext, result: dict[str, Any]) -> dict[str, Any]:
        ctx.effects.failed_bookings += 1
        total = int(ctx.context.get("failed_booking_attempts", 0)) + 1
        ctx.context["failed_booking_attempts"] = total
        if total >= self.max_failed_bookings and ctx.effects.handoff is None:
            ctx.effects.handoff = ("booking_failed", f"{total} failed booking attempts")
        return result

    def _handoff(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        reason = args.get("reason") if args.get("reason") in HANDOFF_REASONS else "other"
        summary = str(args.get("summary", ""))[:300]
        ctx.effects.handoff = (str(reason), summary)
        return {"ok": True, "handed_off": True}


def tool_result_json(result: dict[str, Any]) -> str:
    return json.dumps(result, separators=(",", ":"))
