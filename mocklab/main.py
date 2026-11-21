"""Mock lab booking API: slow (300-1500 ms), flaky (20% failures), deliberately naive by default.

With LAB_ENFORCE_IDEMPOTENCY=false and LAB_ENFORCE_CAPACITY=false the lab writes whatever it is
told, so the gateway alone must prevent double bookings; /admin/double-bookings proves it did.
"""

import asyncio
import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.clock import SystemClock
from mocklab.chaos import Chaos, Fault
from mocklab.store import TEST_CODES, TESTS, LabStore, booking_json


def _flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes"}


CLOCK = SystemClock()
STORE = LabStore()
CHAOS = Chaos(
    failure_rate=float(os.environ.get("LAB_FAILURE_RATE", "0.2")),
    latency_min_ms=int(os.environ.get("LAB_LATENCY_MIN_MS", "300")),
    latency_max_ms=int(os.environ.get("LAB_LATENCY_MAX_MS", "1500")),
    seed=int(os.environ.get("LAB_SEED", "42")),
)
ENFORCE_IDEMPOTENCY = _flag("LAB_ENFORCE_IDEMPOTENCY")
ENFORCE_CAPACITY = _flag("LAB_ENFORCE_CAPACITY")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "replace-with-long-random-admin-token")
LOCK = asyncio.Lock()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    STORE.reset(CLOCK)
    yield


app = FastAPI(title="Mock Lab API", lifespan=lifespan)


def require_admin(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="admin token required")


async def _pre_effect() -> Fault:
    """Latency for every call, then decide the fault. Raises/hangs for pre-effect faults."""
    await asyncio.sleep(CHAOS.latency_s())
    fault = CHAOS.draw()
    if fault is Fault.ERROR_BEFORE:
        raise HTTPException(status_code=500, detail="lab internal error")
    if fault is Fault.HANG_BEFORE:
        await asyncio.sleep(CHAOS.hang_seconds)
        raise HTTPException(status_code=504, detail="lab gateway timeout")
    return fault


async def _lose_response() -> None:
    if CHAOS.lost_response_hangs():
        await asyncio.sleep(CHAOS.hang_seconds)
    raise HTTPException(status_code=500, detail="lab internal error after commit")


class BookingRequest(BaseModel):
    test_code: str
    slot_id: str
    patient_name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=6, max_length=20)


class ChaosUpdate(BaseModel):
    failure_rate: float | None = Field(default=None, ge=0, le=1)
    latency_min_ms: int | None = Field(default=None, ge=0)
    latency_max_ms: int | None = Field(default=None, ge=0)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/tests")
async def tests() -> list[dict[str, Any]]:
    return TESTS


@app.get("/slots")
async def slots(
    day: Annotated[date | None, Query(alias="date")] = None, part_of_day: str = "any", limit: int = 5
) -> dict[str, Any]:
    fault = await _pre_effect()
    if fault is Fault.LOST_AFTER:
        await _lose_response()
    return {"slots": STORE.available(day, part_of_day, max(1, min(limit, 20)))}


@app.post("/bookings", status_code=201)
async def create_booking(body: BookingRequest, idempotency_key: str = Header(min_length=8)) -> JSONResponse:
    if body.test_code not in TEST_CODES or body.slot_id not in STORE.slots:
        raise HTTPException(status_code=422, detail="unknown test or slot")
    fault = await _pre_effect()
    async with LOCK:
        existing = STORE.find_by_key(idempotency_key)
        if ENFORCE_IDEMPOTENCY and existing:
            return JSONResponse(booking_json(existing[0]), status_code=200)
        if ENFORCE_CAPACITY and body.slot_id in STORE.booked_slot_ids():
            return JSONResponse({"error": "slot_taken"}, status_code=409)
        booking = STORE.create(
            idempotency_key, body.test_code, body.slot_id, body.patient_name, body.phone, CLOCK.now()
        )
    if fault is Fault.LOST_AFTER:
        await _lose_response()
    return JSONResponse(booking_json(booking), status_code=201)


@app.get("/bookings")
async def find_bookings(idempotency_key: str) -> dict[str, Any]:
    fault = await _pre_effect()
    if fault is Fault.LOST_AFTER:
        await _lose_response()
    return {"bookings": [booking_json(b) for b in STORE.find_by_key(idempotency_key)]}


@app.get("/admin/double-bookings", dependencies=[Depends(require_admin)])
async def double_bookings() -> dict[str, Any]:
    return STORE.double_bookings()


@app.get("/admin/chaos", dependencies=[Depends(require_admin)])
async def get_chaos() -> dict[str, Any]:
    return {
        "failure_rate": CHAOS.failure_rate,
        "latency_min_ms": CHAOS.latency_min_ms,
        "latency_max_ms": CHAOS.latency_max_ms,
        "enforce_idempotency": ENFORCE_IDEMPOTENCY,
        "enforce_capacity": ENFORCE_CAPACITY,
    }


@app.post("/admin/chaos", dependencies=[Depends(require_admin)])
async def set_chaos(update: ChaosUpdate) -> dict[str, Any]:
    for key, value in update.model_dump(exclude_none=True).items():
        setattr(CHAOS, key, value)
    return await get_chaos()


@app.post("/admin/reset", dependencies=[Depends(require_admin)])
async def reset() -> dict[str, Any]:
    async with LOCK:
        STORE.reset(CLOCK)
        CHAOS.reseed()
    return {"reset": True, "slots": len(STORE.slots)}
