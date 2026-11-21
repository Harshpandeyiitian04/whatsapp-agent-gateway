"""No double booking under every lab failure mode, including 'committed but response lost'."""

import asyncio

from app.db import Pool
from app.lab.booking import BookingService, BookingStatus
from tests.fakelab import FakeLab, lab_client, no_sleep
from tests.helpers import make_settings

SLOT = "S-20261121-0800"


async def _conversation(pool: Pool, phone: str) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("INSERT INTO conversations (phone) VALUES (%s) RETURNING id", (phone,))
        row = await cur.fetchone()
        assert row is not None
        return int(row["id"])


def _service(pool: Pool, fake: FakeLab) -> BookingService:
    settings = make_settings()
    return BookingService(pool, lab_client(fake, settings, pool), settings, sleep=no_sleep)


async def _statuses(pool: Pool) -> list[tuple[str, str]]:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT slot_id, status FROM bookings ORDER BY id")
        return [(r["slot_id"], r["status"]) for r in await cur.fetchall()]


async def test_happy_path(pool: Pool) -> None:
    fake = FakeLab()
    cid = await _conversation(pool, "919999900001")
    res = await _service(pool, fake).book(cid, "919999900001", "CBC", SLOT, "Asha Rao")
    assert res.status is BookingStatus.CONFIRMED and res.lab_booking_id == "LB-000001"
    assert len(fake.bookings) == 1


async def test_commit_then_timeout_reconciles_without_second_booking(pool: Pool) -> None:
    fake = FakeLab()
    fake.post_script.extend(["commit_then_timeout"])
    cid = await _conversation(pool, "919999900001")
    res = await _service(pool, fake).book(cid, "919999900001", "CBC", SLOT, "Asha Rao")
    assert res.status is BookingStatus.CONFIRMED
    assert fake.posts == 1, "must look the booking up instead of re-POSTing"
    assert len(fake.bookings) == 1


async def test_commit_then_500_reconciles(pool: Pool) -> None:
    fake = FakeLab()
    fake.post_script.extend(["commit_then_500"])
    cid = await _conversation(pool, "919999900001")
    res = await _service(pool, fake).book(cid, "919999900001", "CBC", SLOT, "Asha Rao")
    assert res.status is BookingStatus.CONFIRMED and len(fake.bookings) == 1


async def test_failures_before_effect_are_retried(pool: Pool) -> None:
    fake = FakeLab()
    fake.post_script.extend(["error_before", "timeout_before", "ok"])
    cid = await _conversation(pool, "919999900001")
    res = await _service(pool, fake).book(cid, "919999900001", "CBC", SLOT, "Asha Rao")
    assert res.status is BookingStatus.CONFIRMED
    assert fake.posts == 3 and len(fake.bookings) == 1


async def test_lookup_unavailable_leaves_unknown_then_reconciler_confirms(pool: Pool) -> None:
    fake = FakeLab()
    fake.post_script.extend(["commit_then_timeout"])
    fake.get_script.extend(["error"] * 4)  # every lookup attempt fails
    cid = await _conversation(pool, "919999900001")
    svc = _service(pool, fake)
    res = await svc.book(cid, "919999900001", "CBC", SLOT, "Asha Rao")
    assert res.status is BookingStatus.UNKNOWN
    assert await _statuses(pool) == [(SLOT, "unknown")]  # slot stays held

    # A second patient cannot take the held slot.
    other = await _conversation(pool, "919999900002")
    assert (await svc.book(other, "919999900002", "CBC", SLOT, "Ravi K")).status is BookingStatus.SLOT_TAKEN

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM bookings WHERE status='unknown'")
        row = await cur.fetchone()
    assert row is not None
    res = await svc.resolve(row, lookup_first=True)
    assert res.status is BookingStatus.CONFIRMED
    assert len(fake.bookings) == 1 and fake.posts == 1


async def test_replay_same_intent_does_not_call_lab_again(pool: Pool) -> None:
    fake = FakeLab()
    cid = await _conversation(pool, "919999900001")
    svc = _service(pool, fake)
    first = await svc.book(cid, "919999900001", "CBC", SLOT, "Asha Rao")
    again = await svc.book(cid, "919999900001", "CBC", SLOT, "Asha Rao")
    assert first.status is again.status is BookingStatus.CONFIRMED
    assert fake.posts == 1


async def test_patient_cannot_hold_two_active_bookings_for_same_test(pool: Pool) -> None:
    fake = FakeLab()
    cid = await _conversation(pool, "919999900001")
    svc = _service(pool, fake)
    await svc.book(cid, "919999900001", "CBC", SLOT, "Asha Rao")
    res = await svc.book(cid, "919999900001", "CBC", "S-20261121-0830", "Asha Rao")
    assert res.status is BookingStatus.ALREADY_BOOKED and res.slot_id == SLOT
    assert len(fake.bookings) == 1


async def test_slot_race_exactly_one_winner(pool: Pool) -> None:
    fake = FakeLab()
    svc = _service(pool, fake)
    phones = [f"9199999{i:05d}" for i in range(30)]
    cids = [await _conversation(pool, p) for p in phones]
    results = await asyncio.gather(
        *(svc.book(c, p, "CBC", SLOT, "Patient Name") for c, p in zip(cids, phones, strict=True))
    )
    statuses = [r.status for r in results]
    assert statuses.count(BookingStatus.CONFIRMED) == 1
    assert statuses.count(BookingStatus.SLOT_TAKEN) == 29
    assert len(fake.bookings) == 1 and fake.duplicates() == []


async def test_reopen_after_definite_failure(pool: Pool) -> None:
    fake = FakeLab()
    fake.post_script.extend(["conflict"])
    cid = await _conversation(pool, "919999900001")
    svc = _service(pool, fake)
    first = await svc.book(cid, "919999900001", "CBC", SLOT, "Asha Rao")
    assert first.status is BookingStatus.SLOT_TAKEN
    assert await _statuses(pool) == [(SLOT, "failed")]
    second = await svc.book(cid, "919999900001", "CBC", SLOT, "Asha Rao")  # same key → re-open, not a new row
    assert second.status is BookingStatus.CONFIRMED
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) AS n, max(attempt_no) AS a FROM bookings")
        row = await cur.fetchone()
    assert row == {"n": 1, "a": 2}
    assert fake.bookings[0]["idempotency_key"].endswith(":2")


async def test_reopen_blocked_when_slot_now_taken(pool: Pool) -> None:
    fake = FakeLab()
    fake.post_script.extend(["conflict"])
    svc = _service(pool, fake)
    a = await _conversation(pool, "919999900001")
    b = await _conversation(pool, "919999900002")
    await svc.book(a, "919999900001", "CBC", SLOT, "Asha Rao")  # failed
    await svc.book(b, "919999900002", "CBC", SLOT, "Ravi K")  # confirmed, holds slot
    res = await svc.book(a, "919999900001", "CBC", SLOT, "Asha Rao")  # re-open must fail
    assert res.status is BookingStatus.SLOT_TAKEN
    assert sorted(s for _, s in await _statuses(pool)) == ["confirmed", "failed"]


async def test_circuit_open_fails_fast_and_releases_slot(pool: Pool) -> None:
    from app.lab.circuit import CircuitBreaker

    fake = FakeLab()
    breaker = CircuitBreaker(1, 60)
    breaker.record_failure()  # open
    settings = make_settings()
    svc = BookingService(pool, lab_client(fake, settings, pool, breaker), settings, sleep=no_sleep)
    cid = await _conversation(pool, "919999900001")
    res = await svc.book(cid, "919999900001", "CBC", SLOT, "Asha Rao")
    assert res.status is BookingStatus.FAILED and res.lab_unavailable
    assert fake.posts == 0
    assert await _statuses(pool) == [(SLOT, "failed")]
