"""WhatsApp Cloud API webhook: GET verification and POST ingest."""

import json
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import Counter
from pydantic import ValidationError

from app.api.state import AppState
from app.ingest.service import ingest
from app.whatsapp.payload import WebhookPayload, extract_messages
from app.whatsapp.signature import verify

router = APIRouter()
log = logging.getLogger("webhook")
WEBHOOK_REQUESTS = Counter("webhook_requests_total", "Webhook POSTs by result", ["result"])


class BodyTooLarge(Exception):
    pass


async def _read_capped(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise BodyTooLarge
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/webhook")
async def verify_subscription(request: Request) -> Response:
    state: AppState = request.app.state.app_state
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == state.settings.whatsapp_verify_token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    return PlainTextResponse("forbidden", status_code=403)


@router.post("/webhook")
async def receive(request: Request) -> Response:
    state: AppState = request.app.state.app_state
    try:
        raw = await _read_capped(request, state.settings.max_body_bytes)
    except BodyTooLarge:
        WEBHOOK_REQUESTS.labels("too_large").inc()
        return JSONResponse({"error": "body too large"}, status_code=413)

    # Signature first: nothing unauthenticated is ever parsed.
    if not verify(state.settings.whatsapp_app_secret, raw, request.headers.get("x-hub-signature-256")):
        WEBHOOK_REQUESTS.labels("bad_signature").inc()
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        payload = WebhookPayload.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        WEBHOOK_REQUESTS.labels("bad_payload").inc()
        return JSONResponse({"error": "invalid payload"}, status_code=400)

    messages = extract_messages(payload)
    result = await ingest(state.pool, state.settings, state.clock, messages)
    WEBHOOK_REQUESTS.labels("ok").inc()
    return JSONResponse(
        {
            "status": "ok",
            "accepted": result.counts["queued"] + result.counts["held"],
            "duplicates": result.counts["duplicate"],
            "rate_limited": result.counts["rate_limited"],
        }
    )
