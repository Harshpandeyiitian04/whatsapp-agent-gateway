"""HTTP client for the lab API: classification, circuit breaker, retries for idempotent reads,
and one `lab_calls` row per attempt (the source of retry/outcome metrics)."""

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

import httpx

from app.config import Settings
from app.db import Pool
from app.lab.circuit import CircuitBreaker
from app.lab.retry import backoff_delay

log = logging.getLogger("lab")


class Outcome(StrEnum):
    OK = "ok"
    DEFINITE_ERROR = "definite_error"  # 4xx: the lab understood and refused; never retried
    SERVER_ERROR = "server_error"  # 5xx: response received, effect unknown
    NO_RESPONSE = "no_response"  # timeout / connection error: request may still be in flight
    SHORT_CIRCUITED = "short_circuited"  # circuit open, nothing sent


@dataclass(frozen=True, slots=True)
class LabResponse:
    outcome: Outcome
    status: int | None
    data: dict[str, Any] | None
    sent_at: float  # monotonic

    @property
    def ambiguous(self) -> bool:
        return self.outcome in (Outcome.SERVER_ERROR, Outcome.NO_RESPONSE)


class LabUnavailable(Exception):
    """Retries exhausted or circuit open."""


class LabClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        breaker: CircuitBreaker,
        pool: Pool | None,
        settings: Settings,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.http = http
        self.breaker = breaker
        self.pool = pool
        self.settings = settings
        self.sleep = sleep
        self.rng = rng or random.Random()

    async def _call(
        self, method: str, path: str, endpoint: str, attempt: int, key: str | None = None, **kwargs: Any
    ) -> LabResponse:
        sent_at = time.monotonic()
        if not self.breaker.allow():
            await self._record(endpoint, key, attempt, Outcome.SHORT_CIRCUITED, None, 0)
            return LabResponse(Outcome.SHORT_CIRCUITED, None, None, sent_at)
        status: int | None = None
        data: dict[str, Any] | None = None
        try:
            resp = await self.http.request(method, path, timeout=self.settings.lab_timeout_s, **kwargs)
            status = resp.status_code
            if 200 <= status < 300:
                outcome = Outcome.OK
                data = resp.json()
            elif 400 <= status < 500:
                outcome = Outcome.DEFINITE_ERROR
                data = _safe_json(resp)
            else:
                outcome = Outcome.SERVER_ERROR
        except (httpx.TimeoutException, httpx.TransportError):
            outcome = Outcome.NO_RESPONSE
        latency_ms = int((time.monotonic() - sent_at) * 1000)

        if outcome in (Outcome.OK, Outcome.DEFINITE_ERROR):
            self.breaker.record_success()
        elif self.breaker.record_failure():
            log.warning("lab circuit opened", extra={"endpoint": endpoint})
            await self._event("circuit_opened", {"endpoint": endpoint})
        await self._record(endpoint, key, attempt, outcome, status, latency_ms)
        return LabResponse(outcome, status, data, sent_at)

    async def _with_retries(self, endpoint: str, call: Callable[[int], Awaitable[LabResponse]]) -> LabResponse:
        """Retry wrapper for idempotent reads."""
        for attempt in range(1, self.settings.lab_max_attempts + 1):
            resp = await call(attempt)
            if resp.outcome in (Outcome.OK, Outcome.DEFINITE_ERROR):
                return resp
            if resp.outcome is Outcome.SHORT_CIRCUITED:
                break
            if attempt < self.settings.lab_max_attempts:
                await self.sleep(
                    backoff_delay(attempt, self.settings.lab_backoff_base_s, self.settings.lab_backoff_cap_s, self.rng)
                )
        raise LabUnavailable(endpoint)

    async def get_slots(self, day: date | None, part_of_day: str, limit: int) -> list[dict[str, str]]:
        params: dict[str, str | int] = {"part_of_day": part_of_day, "limit": limit}
        if day is not None:
            params["date"] = day.isoformat()
        resp = await self._with_retries("slots", lambda a: self._call("GET", "/slots", "slots", a, params=params))
        if resp.outcome is not Outcome.OK or resp.data is None:
            raise LabUnavailable("slots")
        return list(resp.data.get("slots", []))

    async def find_booking(self, lab_key: str) -> dict[str, Any] | None:
        resp = await self._with_retries(
            "find_booking",
            lambda a: self._call(
                "GET", "/bookings", "find_booking", a, key=lab_key, params={"idempotency_key": lab_key}
            ),
        )
        if resp.outcome is not Outcome.OK or resp.data is None:
            raise LabUnavailable("find_booking")
        found = resp.data.get("bookings", [])
        return dict(found[0]) if found else None

    async def post_booking(self, lab_key: str, body: dict[str, str], attempt: int) -> LabResponse:
        """Single attempt; the booking service owns retry + reconciliation for this non-idempotent call."""
        return await self._call(
            "POST", "/bookings", "create_booking", attempt, key=lab_key, json=body, headers={"Idempotency-Key": lab_key}
        )

    async def _record(
        self, endpoint: str, key: str | None, attempt: int, outcome: Outcome, status: int | None, latency_ms: int
    ) -> None:
        if self.pool is None:
            return
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO lab_calls (endpoint, idempotency_key, attempt, outcome, http_status, latency_ms) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (endpoint, key, attempt, outcome.value, status, latency_ms),
            )

    async def _event(self, kind: str, detail: dict[str, Any]) -> None:
        if self.pool is None:
            return
        async with self.pool.connection() as conn:
            await conn.execute("INSERT INTO events (kind, detail) VALUES (%s, %s)", (kind, json.dumps(detail)))


def _safe_json(resp: httpx.Response) -> dict[str, Any] | None:
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
