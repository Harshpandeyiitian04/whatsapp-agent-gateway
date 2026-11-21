"""Outbound WhatsApp delivery. Mock mode logs; Graph mode posts to the Cloud API (not exercised here)."""

import logging
from typing import Protocol

import httpx

from app.obs.logs import mask_phone

log = logging.getLogger("sender")
GRAPH_URL = "https://graph.facebook.com/v21.0/{phone_number_id}/messages"


class SendError(Exception):
    pass


class Sender(Protocol):
    async def send(self, phone: str, body: str, dedupe_key: str) -> None: ...


class MockSender:
    async def send(self, phone: str, body: str, dedupe_key: str) -> None:
        log.info("outbound (mock)", extra={"phone": mask_phone(phone), "chars": len(body), "dedupe_key": dedupe_key})


class GraphSender:
    def __init__(self, http: httpx.AsyncClient, access_token: str, phone_number_id: str) -> None:
        self.http = http
        self.url = GRAPH_URL.format(phone_number_id=phone_number_id)
        self.headers = {"Authorization": f"Bearer {access_token}"}

    async def send(self, phone: str, body: str, dedupe_key: str) -> None:
        payload = {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": body}}
        try:
            resp = await self.http.post(self.url, json=payload, headers=self.headers, timeout=10)
        except httpx.HTTPError as exc:
            raise SendError(type(exc).__name__) from exc
        if resp.status_code >= 300:
            raise SendError(f"graph status {resp.status_code}")
