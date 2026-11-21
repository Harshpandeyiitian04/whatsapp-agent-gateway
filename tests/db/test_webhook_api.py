"""HTTP-level webhook contract (ASGI in-process, real Postgres)."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from app.api import admin, metrics, webhook
from app.api.state import AppState
from app.api.timing import WebhookTimingMiddleware
from app.clock import FixedClock
from app.db import Pool
from app.whatsapp.signature import sign
from scripts import wa
from tests.helpers import make_settings

SECRET = "test-secret"


@pytest.fixture
async def client(pool: Pool, clock: FixedClock) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.add_middleware(WebhookTimingMiddleware)
    for r in (webhook.router, metrics.router, admin.router):
        app.include_router(r)
    app.state.app_state = AppState(make_settings(whatsapp_verify_token="vt", max_body_bytes=4096), pool, clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_verify_challenge(client: httpx.AsyncClient) -> None:
    ok = await client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
    bad = await client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "1"})
    assert (ok.status_code, ok.text, bad.status_code) == (200, "42", 403)


async def test_signed_message_accepted_and_duplicate_acknowledged(client: httpx.AsyncClient) -> None:
    raw, headers = wa.encode(wa.payload([wa.message("919999900001", "hi", "wamid.X")]), SECRET)
    first = await client.post("/webhook", content=raw, headers=headers)
    second = await client.post("/webhook", content=raw, headers=headers)
    assert first.json() == {"status": "ok", "accepted": 1, "duplicates": 0, "rate_limited": 0}
    assert second.status_code == 200 and second.json()["duplicates"] == 1


async def test_bad_signature_rejected_before_parsing(client: httpx.AsyncClient) -> None:
    resp = await client.post("/webhook", content=b"not even json", headers={"X-Hub-Signature-256": "sha256=00"})
    assert resp.status_code == 401


async def test_valid_signature_invalid_json_is_400(client: httpx.AsyncClient) -> None:
    raw = b"{oops"
    resp = await client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": sign(SECRET, raw)})
    assert resp.status_code == 400


async def test_body_cap(client: httpx.AsyncClient) -> None:
    raw, headers = wa.encode(wa.payload([wa.message("919999900001", "x" * 5000)]), SECRET)
    assert (await client.post("/webhook", content=raw, headers=headers)).status_code == 413


async def test_admin_requires_token_and_metrics_exposed(client: httpx.AsyncClient) -> None:
    assert (await client.get("/admin/dlq")).status_code == 401
    assert (await client.get("/admin/dlq", headers={"Authorization": "Bearer test-admin"})).status_code == 200
    raw, headers = wa.encode(wa.payload([wa.message("919999900001", "hi")]), SECRET)
    await client.post("/webhook", content=raw, headers=headers)
    body = (await client.get("/metrics")).text
    for series in (
        "webhook_ack_seconds_bucket",
        "dedupe_hits_total",
        "queue_depth",
        "llm_cost_inr_total",
        "double_bookings",
        "webhook_ack_recent_seconds",
    ):
        assert series in body
    assert "9199999" not in body  # phones never appear in metrics
