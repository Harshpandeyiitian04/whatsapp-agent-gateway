"""Postgres queue operations. The conversation is the unit of work; its lease is the visibility
timeout. Every write that finishes a turn is guarded by the lease token, so a worker that lost its
lease (stalled, then another worker took over) can never commit stale results."""

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import DictRow

from app.db import Pool

CLAIM_SQL = """
UPDATE conversations c
   SET lease_token = %(token)s, lease_until = now() + make_interval(secs => %(lease)s)
 WHERE c.id = (
        SELECT id FROM conversations
         WHERE has_pending AND state <> 'HANDOFF'
           AND (lease_until IS NULL OR lease_until < now())
           AND next_attempt_at <= now()
         ORDER BY pending_since
         LIMIT 1
         FOR UPDATE SKIP LOCKED)
RETURNING c.id, c.phone, c.state, c.context, c.history
"""

# After a turn: recompute the queue flags from the messages themselves. Run only after the row lock
# is held (see _lock), so READ COMMITTED gives this statement a snapshot that includes any message an
# ingest transaction committed while we were waiting for the lock.
FINISH_SQL = """
UPDATE conversations c
   SET state = %(state)s, context = %(context)s, history = %(history)s,
       lease_token = NULL, lease_until = NULL, next_attempt_at = %(next_attempt)s,
       pending_since = q.oldest, has_pending = q.oldest IS NOT NULL, updated_at = now()
  FROM (SELECT min(received_at) AS oldest FROM inbound_messages
         WHERE conversation_id = %(cid)s AND status = 'queued') q
 WHERE c.id = %(cid)s
"""


@dataclass(frozen=True, slots=True)
class Outbound:
    body: str
    dedupe_key: str


async def claim(pool: Pool, token: UUID, lease_seconds: float) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        cur = await conn.execute(CLAIM_SQL, {"token": token, "lease": lease_seconds})
        return await cur.fetchone()


async def load_queued(pool: Pool, conversation_id: int, limit: int = 20) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, wa_message_id, msg_type, body, attempts FROM inbound_messages "
            "WHERE conversation_id=%s AND status='queued' ORDER BY wa_timestamp, id LIMIT %s",
            (conversation_id, limit),
        )
        return list(await cur.fetchall())


async def heartbeat(pool: Pool, conversation_id: int, token: UUID, lease_seconds: float) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE conversations SET lease_until = now() + make_interval(secs => %s) "
            "WHERE id=%s AND lease_token=%s RETURNING id",
            (lease_seconds, conversation_id, token),
        )
        return await cur.fetchone() is not None


async def _lock(conn: AsyncConnection[DictRow], conversation_id: int, token: UUID) -> bool:
    cur = await conn.execute(
        "SELECT id FROM conversations WHERE id=%s AND lease_token=%s FOR UPDATE", (conversation_id, token)
    )
    return await cur.fetchone() is not None


async def _event(conn: AsyncConnection[DictRow], kind: str, conversation_id: int, detail: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO events (kind, conversation_id, detail) VALUES (%s,%s,%s)",
        (kind, conversation_id, json.dumps(detail)),
    )


async def _outbox(conn: AsyncConnection[DictRow], conversation_id: int, phone: str, out: Outbound) -> None:
    await conn.execute(
        "INSERT INTO outbound_messages (conversation_id, phone, body, dedupe_key) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (dedupe_key) DO NOTHING",
        (conversation_id, phone, out.body, out.dedupe_key),
    )


async def _handoff(conn: AsyncConnection[DictRow], conversation_id: int, reason: str, summary: str) -> None:
    await conn.execute(
        "INSERT INTO handoffs (conversation_id, reason, summary) VALUES (%s,%s,%s)", (conversation_id, reason, summary)
    )
    # Anything still queued now waits for staff instead of the bot.
    await conn.execute(
        "UPDATE inbound_messages SET status='held' WHERE conversation_id=%s AND status='queued'", (conversation_id,)
    )


async def commit_turn(
    pool: Pool,
    conversation_id: int,
    phone: str,
    token: UUID,
    message_ids: list[int],
    state: str,
    context: dict[str, Any],
    history: list[dict[str, Any]],
    outbound: list[Outbound],
    handoff: tuple[str, str] | None,
    events: list[str],
) -> bool:
    """Atomically: messages done + replies into outbox + state saved + lease released."""
    async with pool.connection() as conn, conn.transaction():
        if not await _lock(conn, conversation_id, token):
            return False
        await conn.execute(
            "UPDATE inbound_messages SET status='done', processed_at=now() WHERE id = ANY(%s) AND status='queued'",
            (message_ids,),
        )
        for out in outbound:
            await _outbox(conn, conversation_id, phone, out)
        if handoff:
            await _handoff(conn, conversation_id, *handoff)
        for kind in events:
            await _event(conn, kind, conversation_id, {})
        await conn.execute(
            FINISH_SQL,
            {
                "state": state,
                "context": json.dumps(context),
                "history": json.dumps(history),
                "next_attempt": "-infinity",
                "cid": conversation_id,
            },
        )
    return True


async def release_empty(pool: Pool, conversation_id: int, token: UUID) -> None:
    async with pool.connection() as conn, conn.transaction():
        if await _lock(conn, conversation_id, token):
            await conn.execute(
                "UPDATE conversations c SET lease_token=NULL, lease_until=NULL, "
                "has_pending = EXISTS (SELECT 1 FROM inbound_messages m WHERE m.conversation_id=c.id "
                "AND m.status='queued') WHERE id=%s",
                (conversation_id,),
            )


async def fail_turn(
    pool: Pool,
    conversation_id: int,
    phone: str,
    token: UUID,
    message_ids: list[int],
    error: str,
    max_attempts: int,
    retry_in_s: float,
    apology: str,
) -> str:
    """Record a failed attempt. Returns 'retry', 'dead' (moved to DLQ + handoff) or 'lease_lost'."""
    async with pool.connection() as conn, conn.transaction():
        if not await _lock(conn, conversation_id, token):
            return "lease_lost"
        cur = await conn.execute(
            "UPDATE inbound_messages SET attempts=attempts+1, last_error=%s "
            "WHERE id = ANY(%s) AND status='queued' RETURNING attempts",
            (error[:500], message_ids),
        )
        attempts = max((r["attempts"] for r in await cur.fetchall()), default=0)
        if attempts >= max_attempts:
            await conn.execute(
                "UPDATE inbound_messages SET status='dead' WHERE id = ANY(%s) AND status='queued'", (message_ids,)
            )
            await _event(conn, "dlq", conversation_id, {"messages": message_ids, "error": error[:200]})
            await _handoff(conn, conversation_id, "processing_failure", error[:300])
            await _outbox(conn, conversation_id, phone, Outbound(apology, f"dlq:{conversation_id}:{max(message_ids)}"))
            await conn.execute(
                "UPDATE conversations SET state='HANDOFF', lease_token=NULL, lease_until=NULL, "
                "has_pending=false, pending_since=NULL, updated_at=now() WHERE id=%s",
                (conversation_id,),
            )
            return "dead"
        await conn.execute(
            "UPDATE conversations SET lease_token=NULL, lease_until=NULL, "
            "next_attempt_at = now() + make_interval(secs => %s) WHERE id=%s",
            (retry_in_s, conversation_id),
        )
        return "retry"
