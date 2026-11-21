"""End-to-end against the running compose stack (api + 2 workers + mocklab + postgres).

Run: docker compose run --rm tests pytest -m integration tests/integration
"""

import asyncio
import os
import time
from typing import Any

import httpx
import psycopg
import pytest

from scripts import wa

pytestmark = pytest.mark.integration

API = os.environ.get("API_URL", "http://localhost:8000")
LAB = os.environ.get("LAB_URL", "http://localhost:8100")
DSN = os.environ.get("DATABASE_URL", "postgresql://app:change-me-local-only@localhost:5455/gateway")
SECRET = os.environ.get("WHATSAPP_APP_SECRET", "replace-with-meta-app-secret")
ADMIN = {"Authorization": f"Bearer {os.environ.get('ADMIN_TOKEN', 'replace-with-long-random-admin-token')}"}


def _stack_up() -> bool:
    try:
        return httpx.get(f"{API}/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


async def _q(sql: str) -> list[tuple[Any, ...]]:
    async with await psycopg.AsyncConnection.connect(DSN) as conn:
        cur = await conn.execute(sql)  # type: ignore[arg-type]
        return list(await cur.fetchall())


async def _send(c: httpx.AsyncClient, phone: str, text: str, msg_id: str | None = None) -> httpx.Response:
    raw, headers = wa.encode(wa.payload([wa.message(phone, text, msg_id)]), SECRET)
    return await c.post(f"{API}/webhook", content=raw, headers=headers)


async def _drain(timeout_s: float = 150) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        [(queued, unknown, pending_out)] = await _q(
            "SELECT (SELECT count(*) FROM inbound_messages WHERE status='queued'),"
            " (SELECT count(*) FROM bookings WHERE status IN ('unknown','pending')),"
            " (SELECT count(*) FROM outbound_messages WHERE status='pending')"
        )
        if queued == unknown == pending_out == 0:
            return
        await asyncio.sleep(1)
    raise AssertionError("queue did not drain")


async def _chaos(c: httpx.AsyncClient, rate: float) -> None:
    (await c.post(f"{LAB}/admin/chaos", json={"failure_rate": rate}, headers=ADMIN)).raise_for_status()


@pytest.fixture
async def stack() -> Any:
    if not _stack_up():
        pytest.skip("compose stack not running")
    # Workers keep their circuit breakers in memory: if a previous run opened one, wait out the cool-down
    # (CB_OPEN_SECONDS=30) so this run starts from a closed circuit.
    [(age,)] = await _q("SELECT extract(epoch FROM now() - max(created_at)) FROM events WHERE kind='circuit_opened'")
    if age is not None and age < 35:
        await asyncio.sleep(35 - float(age))
    async with httpx.AsyncClient(timeout=10) as c:
        (await c.post(f"{API}/admin/reset-mock-data", headers=ADMIN)).raise_for_status()
        (await c.post(f"{LAB}/admin/reset", headers=ADMIN)).raise_for_status()
        yield c
        await _chaos(c, 0.2)
    if (await _q("SELECT count(*) FROM events WHERE kind='circuit_opened'"))[0][0]:
        await asyncio.sleep(35)  # leave the stack with closed circuits for whatever runs next


async def test_flaky_lab_duplicates_no_double_booking(stack: httpx.AsyncClient) -> None:
    c = stack
    await _chaos(c, 0.4)  # double the normal failure rate
    phones = [f"9199999{70000 + i}" for i in range(10)]
    first = await asyncio.gather(*(_send(c, p, "Hi, I want a CBC test tomorrow", f"wamid.it1.{p}") for p in phones))
    assert all(r.status_code == 200 for r in first)
    await asyncio.gather(*(_send(c, p, "dup", f"wamid.it1.{p}") for p in phones))  # duplicates
    await _drain()
    # everyone races for slot 1 (same slot), some pick slot 2
    await asyncio.gather(
        *(
            _send(c, p, f"Slot {1 if i % 2 else 2}, my name is Patient {i}", f"wamid.it2.{p}")
            for i, p in enumerate(phones)
        )
    )
    await asyncio.gather(*(_send(c, p, "again", f"wamid.it2.{p}") for p in phones))
    await _drain()

    lab = (await c.get(f"{LAB}/admin/double-bookings", headers=ADMIN)).json()
    assert lab["slot_violations"] == [] and lab["patient_violations"] == []
    [(dupes,)] = await _q("SELECT count(*) FROM duplicate_deliveries")
    [(retries,)] = await _q("SELECT count(*) FROM lab_calls WHERE attempt > 1")
    [(confirmed,)] = await _q("SELECT count(*) FROM bookings WHERE status='confirmed'")
    assert dupes == 20
    assert retries > 0, "40% failure rate must have produced retries"
    assert confirmed <= 2  # two contested slots: at most one winner each
    assert confirmed == lab["total_bookings"]  # gateway and lab agree


async def test_lab_outage_opens_circuit_and_hands_off(stack: httpx.AsyncClient) -> None:
    c = stack
    await _chaos(c, 1.0)
    phones = [f"9199999{80000 + i}" for i in range(6)]
    await asyncio.gather(*(_send(c, p, "Book a CBC test tomorrow") for p in phones))
    await _drain()
    [(opened,)] = await _q("SELECT count(*) FROM events WHERE kind='circuit_opened'")
    [(outage,)] = await _q("SELECT count(*) FROM handoffs WHERE reason='lab_outage'")
    [(short,)] = await _q("SELECT count(*) FROM lab_calls WHERE outcome='short_circuited'")
    assert opened >= 1
    assert outage == len(phones)
    assert short >= 1, "open circuit must fail fast without calling the lab"
