"""Dedupe, rate limit and enqueue, through the same SQL path the webhook uses."""

import asyncio
from datetime import UTC, datetime, timedelta

from app.clock import FixedClock
from app.db import Pool
from app.ingest.service import ingest
from app.whatsapp.payload import InboundMessage
from tests.helpers import make_settings

PHONE = "919999900001"


def msg(i: int | str, phone: str = PHONE, body: str = "hi") -> InboundMessage:
    return InboundMessage(f"wamid.{i}", phone, "text", body, datetime(2026, 11, 20, tzinfo=UTC))


async def scalar(pool: Pool, sql: str) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(sql)  # type: ignore[arg-type]
        row = await cur.fetchone()
        assert row is not None
        return int(next(iter(row.values())))


async def test_concurrent_identical_ids_stored_once(pool: Pool, clock: FixedClock) -> None:
    settings = make_settings()
    results = await asyncio.gather(*(ingest(pool, settings, clock, [msg("same")]) for _ in range(20)))
    outcomes = [o for r in results for o, n in r.counts.items() for _ in range(n)]
    assert outcomes.count("queued") == 1
    assert outcomes.count("duplicate") == 19
    assert await scalar(pool, "SELECT count(*) FROM inbound_messages") == 1
    assert await scalar(pool, "SELECT count(*) FROM duplicate_deliveries") == 19
    assert await scalar(pool, "SELECT count(*) FROM conversations WHERE has_pending") == 1


async def test_rate_limit_bucket_allows_capacity_then_limits_then_refills(pool: Pool, clock: FixedClock) -> None:
    settings = make_settings(rate_limit_capacity=6, rate_limit_refill_per_sec=0.1)
    outcomes = []
    for i in range(10):
        r = await ingest(pool, settings, clock, [msg(i)])
        outcomes += list(r.counts)
    assert outcomes == ["queued"] * 6 + ["rate_limited"] * 4
    assert await scalar(pool, "SELECT min(tokens)::int FROM rate_limits") == 0  # never negative
    # exactly one friendly notice for the window
    assert await scalar(pool, "SELECT count(*) FROM outbound_messages WHERE dedupe_key LIKE 'ratelimit:%'") == 1

    clock.advance(10)  # +1 token
    assert list((await ingest(pool, settings, clock, [msg("after")])).counts) == ["queued"]
    assert list((await ingest(pool, settings, clock, [msg("after2")])).counts) == ["rate_limited"]
    clock.advance(61)  # new notice window
    await ingest(pool, settings, clock, [msg(f"burst{i}") for i in range(8)])
    assert await scalar(pool, "SELECT count(*) FROM outbound_messages WHERE dedupe_key LIKE 'ratelimit:%'") == 2


async def test_duplicates_do_not_consume_rate_limit(pool: Pool, clock: FixedClock) -> None:
    settings = make_settings(rate_limit_capacity=2, rate_limit_refill_per_sec=0)
    for _ in range(5):
        await ingest(pool, settings, clock, [msg("dup")])
    assert list((await ingest(pool, settings, clock, [msg("fresh")])).counts) == ["queued"]


async def test_clock_going_backwards_never_adds_tokens(pool: Pool) -> None:
    settings = make_settings(rate_limit_capacity=1, rate_limit_refill_per_sec=1)
    start = datetime(2026, 11, 20, tzinfo=UTC)
    await ingest(pool, settings, FixedClock(start), [msg(1)])
    r = await ingest(pool, settings, FixedClock(start - timedelta(seconds=30)), [msg(2)])
    assert list(r.counts) == ["rate_limited"]


async def test_message_during_handoff_is_held(pool: Pool, clock: FixedClock) -> None:
    settings = make_settings()
    await ingest(pool, settings, clock, [msg(1)])
    async with pool.connection() as conn:
        await conn.execute("UPDATE conversations SET state='HANDOFF', has_pending=false")
    assert list((await ingest(pool, settings, clock, [msg(2)])).counts) == ["held"]
