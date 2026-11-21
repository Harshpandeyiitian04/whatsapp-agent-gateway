"""Webhook ack latency: measured from request arrival to the moment the response starts."""

import time
from collections import deque

from prometheus_client import Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

WEBHOOK_ACK = Histogram(
    "webhook_ack_seconds",
    "Time from webhook request arrival to response start",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0, 2.5),
)


class RecentLatencies:
    """Bounded window for exact recent percentiles (Prometheus histograms only approximate)."""

    def __init__(self, maxlen: int = 20000) -> None:
        self._values: deque[float] = deque(maxlen=maxlen)

    def add(self, seconds: float) -> None:
        self._values.append(seconds)

    def clear(self) -> None:
        self._values.clear()

    def percentile(self, q: float) -> float | None:
        if not self._values:
            return None
        ordered = sorted(self._values)
        idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
        return ordered[idx]

    def __len__(self) -> int:
        return len(self._values)


RECENT = RecentLatencies()


class WebhookTimingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != "/webhook" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()

        async def timed_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                elapsed = time.perf_counter() - start
                WEBHOOK_ACK.observe(elapsed)
                RECENT.add(elapsed)
            await send(message)

        await self.app(scope, receive, timed_send)
