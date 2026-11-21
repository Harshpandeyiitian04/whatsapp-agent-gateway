"""Singleton background duties, run by whichever worker holds the advisory lock:
outbox delivery, reconciliation of 'unknown' bookings, and a sweep for stranded messages."""

import asyncio
import json
import logging
from typing import Any

import psycopg

from app.aio import sleep_or_stop
from app.config import Settings
from app.db import Pool
from app.lab.booking import BookingService, BookingStatus
from app.lab.retry import backoff_delay
from app.whatsapp.sender import Sender, SendError

log = logging.getLogger("leader")
LEADER_LOCK_ID = 727001
MAX_SEND_ATTEMPTS = 8


async def send_pending(pool: Pool, sender: Sender, batch: int = 50) -> int:
    """Deliver pending outbox rows. Row locks + SKIP LOCKED make concurrent senders safe; the unique
    dedupe_key guarantees each logical reply exists once."""
    sent = 0
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT id, phone, body, dedupe_key, attempts FROM outbound_messages "
            "WHERE status='pending' AND next_attempt_at <= now() ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
            (batch,),
        )
        for row in await cur.fetchall():
            try:
                await sender.send(row["phone"], row["body"], row["dedupe_key"])
            except SendError as exc:
                attempts = row["attempts"] + 1
                await conn.execute(
                    "UPDATE outbound_messages SET attempts=%s, status=%s, "
                    "next_attempt_at = now() + make_interval(secs => %s) WHERE id=%s",
                    (
                        attempts,
                        "failed" if attempts >= MAX_SEND_ATTEMPTS else "pending",
                        backoff_delay(attempts, 1.0, 60.0),
                        row["id"],
                    ),
                )
                log.warning("send failed", extra={"outbound_id": row["id"], "error": str(exc)})
                continue
            await conn.execute("UPDATE outbound_messages SET status='sent', sent_at=now() WHERE id=%s", (row["id"],))
            sent += 1
    return sent


async def reconcile_unknown(pool: Pool, bookings: BookingService, settings: Settings) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT * FROM bookings WHERE status='unknown' AND updated_at < now() - interval '5 seconds' "
            "ORDER BY updated_at LIMIT 20"
        )
        rows = list(await cur.fetchall())
    for row in rows:
        result = await bookings.resolve(row, lookup_first=True)
        await _notify(pool, row, result.status, settings)
    return len(rows)


async def _notify(pool: Pool, row: dict[str, Any], status: BookingStatus, settings: Settings) -> None:
    cid, key = row["conversation_id"], f"{row['idempotency_key']}:{row['attempt_no']}"
    async with pool.connection() as conn, conn.transaction():
        if status is BookingStatus.CONFIRMED:
            body = f"Your {row['test_code']} booking for slot {row['slot_id']} is confirmed."
            await conn.execute(
                "UPDATE conversations SET state = CASE WHEN state='BOOKING_PENDING' THEN 'BOOKED' ELSE state END, "
                "context = CASE WHEN context ? 'booking' THEN jsonb_set(context, '{booking}', "
                "(context->'booking') - 'pending') ELSE context END, updated_at=now() WHERE id=%s",
                (cid,),
            )
        elif status is BookingStatus.UNKNOWN:
            await conn.execute(
                "WITH stale AS (SELECT 1 FROM bookings WHERE idempotency_key=%s "
                "  AND created_at < now() - make_interval(secs => %s)) "
                "INSERT INTO handoffs (conversation_id, reason, summary) "
                "SELECT %s, 'booking_unconfirmed', %s FROM stale WHERE NOT EXISTS ("
                "  SELECT 1 FROM handoffs WHERE conversation_id=%s AND reason='booking_unconfirmed' "
                "  AND released_at IS NULL)",
                (
                    row["idempotency_key"],
                    settings.booking_unknown_handoff_s,
                    cid,
                    f"booking for {row['slot_id']} still unconfirmed",
                    cid,
                ),
            )
            return
        else:
            body = (
                f"We couldn't confirm your {row['test_code']} booking for slot {row['slot_id']}. "
                "Please pick another time."
            )
            await conn.execute(
                "UPDATE conversations SET state = CASE WHEN state='BOOKING_PENDING' THEN 'COLLECTING' ELSE state END, "
                "context = context - 'booking', updated_at=now() WHERE id=%s",
                (cid,),
            )
        await conn.execute(
            "INSERT INTO outbound_messages (conversation_id, phone, body, dedupe_key) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (dedupe_key) DO NOTHING",
            (cid, row["phone"], body, f"booking:{key}:{status.value}"),
        )


async def sweep_stranded(pool: Pool) -> int:
    """Defence in depth: re-flag conversations whose queued messages lost their pending flag."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE conversations c SET has_pending=true, pending_since=now() "
            "WHERE NOT has_pending AND state <> 'HANDOFF' AND EXISTS (SELECT 1 FROM inbound_messages m "
            "WHERE m.conversation_id=c.id AND m.status='queued' AND m.received_at < now() - interval '30 seconds')"
        )
        if cur.rowcount:
            await conn.execute(
                "INSERT INTO events (kind, detail) VALUES ('stranded_requeued', %s)",
                (json.dumps({"conversations": cur.rowcount}),),
            )
        return cur.rowcount


class Leader:
    def __init__(
        self, settings: Settings, pool: Pool, sender: Sender, bookings: BookingService, stop: asyncio.Event
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.sender = sender
        self.bookings = bookings
        self.stop = stop
        self.is_leader = False

    async def run(self) -> None:
        while not self.stop.is_set():
            try:
                await self._try_lead()
            except Exception:  # lock connection lost, DB down, ...: step down and retry later
                log.exception("leader stepped down")
            self.is_leader = False
            await sleep_or_stop(self.stop, 5)

    async def _try_lead(self) -> None:
        async with await psycopg.AsyncConnection.connect(self.settings.database_url, autocommit=True) as lock_conn:
            cur = await lock_conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LEADER_LOCK_ID,))
            row = await cur.fetchone()
            if not row or not row[0]:
                return
            self.is_leader = True
            log.info("became leader")
            async with asyncio.TaskGroup() as tg:  # a failure in any duty cancels the others
                tg.create_task(self._send_loop())
                tg.create_task(self._reconcile_loop())
                tg.create_task(self._hold_lock(lock_conn))

    async def _hold_lock(self, lock_conn: psycopg.AsyncConnection[tuple[Any, ...]]) -> None:
        """The advisory lock lives with this session; if it dies, raise so we stop leading."""
        while not self.stop.is_set():
            await lock_conn.execute("SELECT 1")
            await sleep_or_stop(self.stop, 2)

    async def _send_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await send_pending(self.pool, self.sender)
            except psycopg.Error:
                log.exception("outbox send failed")
            await sleep_or_stop(self.stop, 0.5)

    async def _reconcile_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await reconcile_unknown(self.pool, self.bookings, self.settings)
                await sweep_stranded(self.pool)
            except psycopg.Error:
                log.exception("reconcile failed")
            await sleep_or_stop(self.stop, self.settings.reconcile_interval_s)
