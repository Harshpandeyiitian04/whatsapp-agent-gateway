"""Leases (visibility timeout), single holder per conversation, DLQ, outbox, budget guard, loop guard."""

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from app.agent.llm.base import LLMResponse, ToolCall, Usage
from app.agent.llm.budget import BudgetExceeded, check_budget, record_usage
from app.agent.loop import HANDOFF_REPLY, Agent
from app.agent.tools import ToolExecutor
from app.clock import FixedClock
from app.db import Pool
from app.ingest.service import ingest
from app.worker import queue
from app.worker.leader import send_pending
from app.worker.queue import Outbound
from tests.db.test_ingest import msg, scalar
from tests.helpers import make_settings


async def _seed(pool: Pool, clock: FixedClock, phones: int = 1) -> None:
    settings = make_settings()
    for p in range(phones):
        await ingest(pool, settings, clock, [msg(f"{p}-1", phone=f"9199999{p:05d}")])


async def test_single_holder_per_conversation(pool: Pool, clock: FixedClock) -> None:
    await _seed(pool, clock, phones=1)
    claims = await asyncio.gather(*(queue.claim(pool, uuid4(), 60) for _ in range(10)))
    assert sum(c is not None for c in claims) == 1


async def test_concurrent_claims_spread_across_conversations(pool: Pool, clock: FixedClock) -> None:
    await _seed(pool, clock, phones=5)
    claims = await asyncio.gather(*(queue.claim(pool, uuid4(), 60) for _ in range(10)))
    ids = [c["id"] for c in claims if c]
    assert sorted(ids) == [1, 2, 3, 4, 5]


async def test_expired_lease_reclaimed_and_stale_commit_rejected(pool: Pool, clock: FixedClock) -> None:
    await _seed(pool, clock)
    stale = uuid4()
    conv = await queue.claim(pool, stale, 60)
    assert conv is not None
    assert await queue.claim(pool, uuid4(), 60) is None  # held
    async with pool.connection() as conn:  # simulate a crashed worker
        await conn.execute("UPDATE conversations SET lease_until = now() - interval '1 second'")
    fresh = uuid4()
    assert (await queue.claim(pool, fresh, 60)) is not None  # visibility timeout → re-delivered
    msgs = await queue.load_queued(pool, conv["id"])
    args: tuple[Any, ...] = (conv["id"], conv["phone"])
    ok_stale = await queue.commit_turn(
        pool, *args, stale, [m["id"] for m in msgs], "COLLECTING", {}, [], [Outbound("stale", "k-stale")], None, []
    )
    ok_fresh = await queue.commit_turn(
        pool, *args, fresh, [m["id"] for m in msgs], "COLLECTING", {}, [], [Outbound("fresh", "k-fresh")], None, []
    )
    assert (ok_stale, ok_fresh) == (False, True)
    assert await scalar(pool, "SELECT count(*) FROM outbound_messages") == 1
    assert await scalar(pool, "SELECT count(*) FROM conversations WHERE has_pending OR lease_token IS NOT NULL") == 0


async def test_message_arriving_mid_turn_keeps_conversation_pending(pool: Pool, clock: FixedClock) -> None:
    await _seed(pool, clock)
    token = uuid4()
    conv = await queue.claim(pool, token, 60)
    assert conv is not None
    ids = [m["id"] for m in await queue.load_queued(pool, conv["id"])]
    await ingest(pool, make_settings(), clock, [msg("late")])
    assert await queue.commit_turn(pool, conv["id"], conv["phone"], token, ids, "COLLECTING", {}, [], [], None, [])
    assert await scalar(pool, "SELECT count(*) FROM conversations WHERE has_pending") == 1


async def test_dlq_after_max_attempts(pool: Pool, clock: FixedClock) -> None:
    await _seed(pool, clock)
    outcomes = []
    for _ in range(3):
        async with pool.connection() as conn:
            await conn.execute("UPDATE conversations SET next_attempt_at='-infinity'")
        token = uuid4()
        conv = await queue.claim(pool, token, 60)
        assert conv is not None
        ids = [m["id"] for m in await queue.load_queued(pool, conv["id"])]
        outcomes.append(await queue.fail_turn(pool, conv["id"], conv["phone"], token, ids, "boom", 3, 0, "sorry"))
    assert outcomes == ["retry", "retry", "dead"]
    assert await scalar(pool, "SELECT count(*) FROM inbound_messages WHERE status='dead'") == 1
    assert await scalar(pool, "SELECT count(*) FROM handoffs WHERE reason='processing_failure'") == 1
    assert await scalar(pool, "SELECT count(*) FROM conversations WHERE state='HANDOFF'") == 1


class _RecordingSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, phone: str, body: str, dedupe_key: str) -> None:
        self.sent.append(dedupe_key)


async def test_outbox_sends_each_reply_once(pool: Pool, clock: FixedClock) -> None:
    await _seed(pool, clock)
    async with pool.connection() as conn:
        for _ in range(2):  # a redelivered turn tries to enqueue the same reply twice
            await conn.execute(
                "INSERT INTO outbound_messages (conversation_id, phone, body, dedupe_key) "
                "VALUES (1, '919999900000', 'hi', 'reply:1:1') ON CONFLICT DO NOTHING"
            )
    sender = _RecordingSender()
    await asyncio.gather(send_pending(pool, sender), send_pending(pool, sender))
    await send_pending(pool, sender)
    assert sender.sent == ["reply:1:1"]


async def test_budget_guard_and_ledger_survive_reset(pool: Pool) -> None:
    resp = LLMResponse("x", [], Usage(10_000_000, 0), "claude-haiku-5-5")  # $1.00
    await record_usage(pool, None, "real", resp)
    await record_usage(pool, None, "mock", LLMResponse("x", [], Usage(10_000_000, 0), "mock:claude-haiku-5-5"))
    spent = Decimal(await scalar(pool, "SELECT (sum(cost_usd)*100)::int FROM llm_spend_ledger")) / 100
    assert spent >= Decimal("1.00")  # ledger holds only real rows (earlier tests may add more)
    with pytest.raises(BudgetExceeded):
        await check_budget(pool, "real", float(spent))
    await check_budget(pool, "mock", 0)  # mock never limited
    async with pool.connection() as conn:
        with pytest.raises(Exception, match="append-only"):
            await conn.execute("TRUNCATE llm_spend_ledger")
        with pytest.raises(Exception, match="append-only"):
            await conn.execute("DELETE FROM llm_spend_ledger")


class _LoopingLLM:
    mode, model = "mock", "mock:claude-haiku-5-5"

    async def complete(self, system: str, messages: list[Any], tools: list[Any]) -> LLMResponse:
        return LLMResponse(
            "", [ToolCall("t", "check_slots", {"test_code": "CBC", "date": "bad"})], Usage(10, 5), self.model
        )


async def test_tool_loop_guard_hands_off(pool: Pool, clock: FixedClock) -> None:
    await _seed(pool, clock)
    settings = make_settings(llm_max_calls_per_turn=4)
    agent = Agent(_LoopingLLM(), ToolExecutor(None, None, 3), pool, settings, clock)  # type: ignore[arg-type]
    result = await agent.run_turn(1, "919999900000", "NEW", {}, [], "hello")
    assert result.llm_calls == 4
    assert result.effects.handoff == ("other", "agent_loop_limit")
    assert result.reply == HANDOFF_REPLY and result.state == "HANDOFF"
    assert await scalar(pool, "SELECT count(*) FROM llm_usage WHERE conversation_id=1") == 4
