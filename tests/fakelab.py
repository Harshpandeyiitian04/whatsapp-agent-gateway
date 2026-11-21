"""Scriptable in-process lab for tests (httpx.MockTransport). Deliberately naive like the real mock:
it never enforces idempotency or capacity, so any gateway bug shows up as a duplicate here."""

import json
from collections import deque
from typing import Any

import httpx

from app.config import Settings
from app.lab.circuit import CircuitBreaker
from app.lab.client import LabClient

# Scripted behaviours for POST /bookings, consumed in order (default: "ok").
#   ok | error_before | timeout_before | commit_then_timeout | commit_then_500 | conflict
# Scripted behaviours for GET /bookings: ok | error


class FakeLab:
    def __init__(self) -> None:
        self.bookings: list[dict[str, Any]] = []
        self.post_script: deque[str] = deque()
        self.get_script: deque[str] = deque()
        self.slots_script: deque[str] = deque()
        self.posts = 0

    def _book(self, req: httpx.Request) -> dict[str, Any]:
        body = json.loads(req.content)
        booking = {
            "booking_id": f"LB-{len(self.bookings) + 1:06d}",
            "idempotency_key": req.headers["Idempotency-Key"],
            **body,
        }
        self.bookings.append(booking)
        return booking

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/bookings":
            self.posts += 1
            mode = self.post_script.popleft() if self.post_script else "ok"
            if mode == "error_before":
                return httpx.Response(500)
            if mode == "timeout_before":
                raise httpx.ReadTimeout("timeout", request=req)
            if mode == "conflict":
                return httpx.Response(409, json={"error": "slot_taken"})
            booking = self._book(req)
            if mode == "commit_then_timeout":
                raise httpx.ReadTimeout("timeout after commit", request=req)
            if mode == "commit_then_500":
                return httpx.Response(500)
            return httpx.Response(201, json=booking)
        if req.method == "GET" and req.url.path == "/bookings":
            if self.get_script and self.get_script.popleft() == "error":
                return httpx.Response(500)
            key = req.url.params["idempotency_key"]
            return httpx.Response(200, json={"bookings": [b for b in self.bookings if b["idempotency_key"] == key]})
        if req.method == "GET" and req.url.path == "/slots":
            if self.slots_script and self.slots_script.popleft() == "error":
                return httpx.Response(500)
            return httpx.Response(
                200,
                json={
                    "slots": [
                        {"slot_id": "S-20261121-0800", "start": "2026-11-21T08:00:00+05:30"},
                        {"slot_id": "S-20261121-0830", "start": "2026-11-21T08:30:00+05:30"},
                    ]
                },
            )
        return httpx.Response(404)

    def duplicates(self) -> list[str]:
        slots = [b["slot_id"] for b in self.bookings]
        return sorted({s for s in slots if slots.count(s) > 1})


async def _no_sleep(_: float) -> None:
    return None


def lab_client(fake: FakeLab, settings: Settings, pool: Any, breaker: CircuitBreaker | None = None) -> LabClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), base_url="http://lab")
    # High default threshold so scripted failures exercise retries, not the breaker (tested separately).
    return LabClient(http, breaker or CircuitBreaker(100, 30), pool, settings, sleep=_no_sleep)


no_sleep = _no_sleep
