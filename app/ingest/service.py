"""Webhook ingest: dedupe + rate limit + enqueue in a single transaction (no outbound I/O)."""

from collections import Counter
from dataclasses import dataclass

from app.clock import Clock
from app.config import Settings
from app.db import Pool
from app.whatsapp.payload import InboundMessage

RATE_LIMIT_NOTICE = "You're sending messages quickly. Please wait a minute and try again."


@dataclass(frozen=True, slots=True)
class IngestResult:
    counts: Counter[str]


async def ingest(pool: Pool, settings: Settings, clock: Clock, messages: list[InboundMessage]) -> IngestResult:
    counts: Counter[str] = Counter()
    if not messages:
        return IngestResult(counts)
    now = clock.now()
    # Stable sort by phone: a consistent lock order across concurrent webhooks avoids deadlocks,
    # while each phone's messages keep their original order.
    ordered = sorted(messages, key=lambda m: m.phone)
    async with pool.connection() as conn, conn.transaction():
        for m in ordered:
            cur = await conn.execute(
                "SELECT outcome FROM ingest_message(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    m.wa_message_id,
                    m.phone,
                    m.msg_type,
                    m.body,
                    m.wa_timestamp,
                    now,
                    settings.rate_limit_capacity,
                    settings.rate_limit_refill_per_sec,
                    settings.rate_limit_notice_window_s,
                    RATE_LIMIT_NOTICE,
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            counts[str(row["outcome"])] += 1
    return IngestResult(counts)
