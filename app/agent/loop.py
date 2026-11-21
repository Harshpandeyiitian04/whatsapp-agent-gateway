"""One agent turn: (coalesced) user text in -> reply + state changes out. No DB commit happens here;
the worker applies the result atomically. Side effects (bookings) are idempotent, so a turn that is
re-run after a crash converges to the same outcome."""

import copy
import logging
from dataclasses import dataclass
from typing import Any

from app.agent.llm.base import LLMClient, Message
from app.agent.llm.budget import BudgetExceeded, check_budget, record_usage
from app.agent.prompts import build_system_prompt
from app.agent.tools import TOOLS, ToolContext, ToolExecutor, TurnEffects, tool_result_json
from app.clock import Clock, today_ist
from app.config import Settings
from app.db import Pool

log = logging.getLogger("agent")

HANDOFF_REPLY = "I've passed this to our team. A staff member will reply here shortly."
FALLBACK_REPLY = "Sorry, I didn't get that. Which test would you like to book, and for which day?"


@dataclass
class TurnResult:
    reply: str
    state: str
    context: dict[str, Any]
    history: list[Message]
    effects: TurnEffects
    llm_calls: int


class Agent:
    def __init__(self, llm: LLMClient, executor: ToolExecutor, pool: Pool, settings: Settings, clock: Clock) -> None:
        self.llm = llm
        self.executor = executor
        self.pool = pool
        self.settings = settings
        self.clock = clock

    async def run_turn(
        self,
        conversation_id: int,
        phone: str,
        state: str,
        context: dict[str, Any],
        history: list[Message],
        user_text: str,
    ) -> TurnResult:
        context = copy.deepcopy(context)
        effects = TurnEffects()
        ctx = ToolContext(conversation_id, phone, context, effects)
        if state in ("NEW", "BOOKED"):
            state = "COLLECTING"  # a new message starts a fresh request; tools move the state on
        window = history[-2 * self.settings.llm_history_turns :]
        messages: list[Message] = [*window, {"role": "user", "content": user_text}]
        reply = ""
        calls = 0

        for _ in range(self.settings.llm_max_calls_per_turn):
            try:
                await check_budget(self.pool, self.llm.mode, self.settings.llm_budget_usd)
            except BudgetExceeded:
                effects.handoff = ("other", "llm_budget")
                effects.notes.append("budget_refused")
                break
            system = build_system_prompt(self.settings.lab_name, today_ist(self.clock), state, context)
            resp = await self.llm.complete(system, messages, TOOLS)
            calls += 1
            await record_usage(self.pool, conversation_id, self.llm.mode, resp)
            if resp.refused:
                effects.handoff = ("other", "model_refusal")
                break
            if not resp.tool_calls:
                reply = resp.text
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": resp.text,
                    "tool_calls": [c.as_dict() for c in resp.tool_calls],
                    "raw": resp.raw,
                }
            )
            for call in resp.tool_calls:
                result = await self.executor.execute(call, ctx)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": tool_result_json(result)}
                )
            if effects.handoff:
                break
        else:
            effects.handoff = ("other", "agent_loop_limit")
            effects.notes.append("loop_limit")

        if effects.lab_outage and effects.handoff is None and effects.state != "BOOKING_PENDING":
            effects.handoff = ("lab_outage", "lab API unavailable (retries exhausted or circuit open)")
        if effects.handoff:
            reply, state = HANDOFF_REPLY, "HANDOFF"
        else:
            reply = reply or FALLBACK_REPLY
            state = effects.state or state

        new_history = [*history, {"role": "user", "content": user_text}, {"role": "assistant", "content": reply}][
            -2 * self.settings.llm_history_turns :
        ]
        return TurnResult(reply, state, context, new_history, effects, calls)
