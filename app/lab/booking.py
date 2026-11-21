"""Idempotent booking against a flaky lab.

Invariants (enforced by Postgres, see migrations/001_init.sql):
  * one row per idempotency key            -> UNIQUE(idempotency_key)
  * one active booking per slot            -> partial UNIQUE(slot_id)
  * one active booking per (patient, test) -> partial UNIQUE(phone, test_code)
'active' = pending | confirmed | unknown, so a booking we cannot disprove keeps its slot held.

Flow: claim the slot locally first, then talk to the lab. An ambiguous lab failure (5xx or no
response) is always reconciled by looking the booking up by its key before any re-POST.
"""

import asyncio
import hashlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import psycopg

from app.config import Settings
from app.db import Pool
from app.lab.client import LabClient, LabUnavailable, Outcome
from app.lab.retry import backoff_delay

log = logging.getLogger("booking")


class BookingStatus(StrEnum):
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"  # could not confirm or disprove; slot stays held, reconciler continues
    SLOT_TAKEN = "slot_taken"
    ALREADY_BOOKED = "already_booked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BookingResult:
    status: BookingStatus
    slot_id: str
    lab_booking_id: str | None = None
    lab_unavailable: bool = False


def booking_key(phone: str, test_code: str, slot_id: str) -> str:
    return hashlib.sha256(f"{phone}|{test_code}|{slot_id}".encode()).hexdigest()


def lab_key(key: str, attempt_no: int) -> str:
    return f"{key[:40]}:{attempt_no}"


ACTIVE = ("pending", "confirmed", "unknown")


class BookingService:
    def __init__(
        self,
        pool: Pool,
        lab: LabClient,
        settings: Settings,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.pool = pool
        self.lab = lab
        self.settings = settings
        self.sleep = sleep
        self.rng = rng or random.Random()

    async def book(
        self, conversation_id: int, phone: str, test_code: str, slot_id: str, patient_name: str
    ) -> BookingResult:
        key = booking_key(phone, test_code, slot_id)
        claimed = await self._claim(key, conversation_id, phone, test_code, slot_id, patient_name)
        if isinstance(claimed, BookingResult):
            return claimed
        row, fresh = claimed
        if row["status"] == "confirmed":
            return BookingResult(BookingStatus.CONFIRMED, slot_id, row["lab_booking_id"])
        # An existing pending/unknown row means an earlier attempt may have reached the lab.
        return await self.resolve(row, lookup_first=not fresh)

    async def _claim(
        self, key: str, conversation_id: int, phone: str, test_code: str, slot_id: str, patient_name: str
    ) -> tuple[dict[str, Any], bool] | BookingResult:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO bookings (idempotency_key, conversation_id, phone, test_code, slot_id, "
                "patient_name, status) VALUES (%s,%s,%s,%s,%s,%s,'pending') "
                "ON CONFLICT DO NOTHING RETURNING *",
                (key, conversation_id, phone, test_code, slot_id, patient_name),
            )
            row = await cur.fetchone()
            if row is not None:
                return row, True
            cur = await conn.execute("SELECT * FROM bookings WHERE idempotency_key=%s", (key,))
            existing = await cur.fetchone()
            if existing is not None and existing["status"] != "failed":
                return existing, False
            if existing is not None:
                # Re-attempt after a definite failure: reopen the same row, new lab attempt key.
                try:
                    cur = await conn.execute(
                        "UPDATE bookings SET status='pending', attempt_no=attempt_no+1, patient_name=%s, "
                        "last_error=NULL, updated_at=now() WHERE idempotency_key=%s AND status='failed' "
                        "RETURNING *",
                        (patient_name, key),
                    )
                except psycopg.errors.UniqueViolation as exc:
                    return self._conflict(exc.diag.constraint_name, slot_id)
                reopened = await cur.fetchone()
                if reopened is not None:
                    return reopened, True
                cur = await conn.execute("SELECT * FROM bookings WHERE idempotency_key=%s", (key,))
                raced = await cur.fetchone()
                assert raced is not None
                return raced, False
            # No row for this key: the insert hit a partial unique index.
            cur = await conn.execute(
                "SELECT slot_id FROM bookings WHERE phone=%s AND test_code=%s AND status = ANY(%s)",
                (phone, test_code, list(ACTIVE)),
            )
            mine = await cur.fetchone()
            if mine is not None:
                return BookingResult(BookingStatus.ALREADY_BOOKED, mine["slot_id"])
            return BookingResult(BookingStatus.SLOT_TAKEN, slot_id)

    @staticmethod
    def _conflict(constraint: str | None, slot_id: str) -> BookingResult:
        if constraint == "bookings_active_patient_uq":
            return BookingResult(BookingStatus.ALREADY_BOOKED, slot_id)
        return BookingResult(BookingStatus.SLOT_TAKEN, slot_id)

    async def resolve(self, row: dict[str, Any], lookup_first: bool) -> BookingResult:
        """Drive one booking row to confirmed / failed / unknown. Safe to call repeatedly."""
        key: str = row["idempotency_key"]
        slot_id: str = row["slot_id"]
        lkey = lab_key(key, row["attempt_no"])
        body = {
            "test_code": row["test_code"],
            "slot_id": slot_id,
            "patient_name": row["patient_name"],
            "phone": row["phone"],
        }

        if lookup_first:
            outcome = await self._lookup(key, lkey, slot_id)
            if outcome is not None:
                return outcome

        max_attempts = self.settings.lab_max_attempts
        for attempt in range(1, max_attempts + 1):
            resp = await self.lab.post_booking(lkey, body, attempt)
            if resp.outcome is Outcome.OK and resp.data:
                return await self._confirm(key, slot_id, str(resp.data.get("booking_id")))
            if resp.outcome is Outcome.DEFINITE_ERROR:
                taken = resp.status == 409
                await self._mark(key, "failed", ("pending", "unknown"), "slot_taken" if taken else "rejected")
                return BookingResult(BookingStatus.SLOT_TAKEN if taken else BookingStatus.FAILED, slot_id)
            if resp.outcome is Outcome.SHORT_CIRCUITED:
                # Nothing was sent on this attempt, and every earlier ambiguous attempt was
                # disproved by a lookup, so the lab holds no booking for this key.
                await self._mark(key, "failed", ("pending", "unknown"), "lab_unavailable")
                return BookingResult(BookingStatus.FAILED, slot_id, lab_unavailable=True)
            if resp.outcome is Outcome.NO_RESPONSE:
                # The request may still be in flight: let it land before we look.
                elapsed = time.monotonic() - resp.sent_at
                wait = self.settings.lab_timeout_s + self.settings.lab_ambiguous_margin_s - elapsed
                if wait > 0:
                    await self.sleep(wait)
            outcome = await self._lookup(key, lkey, slot_id)
            if outcome is not None:
                return outcome
            if attempt < max_attempts:
                await self.sleep(
                    backoff_delay(attempt, self.settings.lab_backoff_base_s, self.settings.lab_backoff_cap_s, self.rng)
                )
        await self._mark(key, "failed", ("pending", "unknown"), "lab_unavailable")
        return BookingResult(BookingStatus.FAILED, slot_id, lab_unavailable=True)

    async def _lookup(self, key: str, lkey: str, slot_id: str) -> BookingResult | None:
        """Confirmed if the lab has it; UNKNOWN if we cannot ask; None if definitely absent."""
        try:
            found = await self.lab.find_booking(lkey)
        except LabUnavailable:
            await self._mark(key, "unknown", ("pending",), "lookup_unavailable")
            return BookingResult(BookingStatus.UNKNOWN, slot_id, lab_unavailable=True)
        if found is not None:
            return await self._confirm(key, slot_id, str(found.get("booking_id")))
        return None

    async def _confirm(self, key: str, slot_id: str, lab_booking_id: str) -> BookingResult:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE bookings SET status='confirmed', lab_booking_id=%s, last_error=NULL, updated_at=now() "
                "WHERE idempotency_key=%s AND status IN ('pending','unknown')",
                (lab_booking_id, key),
            )
        return BookingResult(BookingStatus.CONFIRMED, slot_id, lab_booking_id)

    async def _mark(self, key: str, status: str, from_states: tuple[str, ...], error: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE bookings SET status=%s, last_error=%s, updated_at=now() "
                "WHERE idempotency_key=%s AND status = ANY(%s)",
                (status, error, key, list(from_states)),
            )
