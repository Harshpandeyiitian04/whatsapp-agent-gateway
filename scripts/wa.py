"""Build and sign synthetic WhatsApp Cloud API webhooks (shared by smoke test, load test, demo, tests).

CLI:  python -m scripts.wa --phone 919999900001 "Hi, I want a CBC test tomorrow"
"""

import argparse
import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any

import httpx

PHONE_NUMBER_ID = "106540352242922"


def message(
    phone: str, text: str | None, msg_id: str | None = None, msg_type: str = "text", ts: int | None = None
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "from": phone,
        "id": msg_id or f"wamid.{uuid.uuid4().hex}",
        "timestamp": str(ts or int(time.time())),
        "type": msg_type,
    }
    if msg_type == "text":
        msg["text"] = {"body": text or ""}
    else:
        msg[msg_type] = {"id": uuid.uuid4().hex, "mime_type": "image/jpeg"}
    return msg


def payload(messages: list[dict[str, Any]], statuses: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550783881", "phone_number_id": PHONE_NUMBER_ID},
    }
    if messages:
        value["contacts"] = [{"profile": {"name": "Synthetic User"}, "wa_id": messages[0]["from"]}]
        value["messages"] = messages
    if statuses:
        value["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "102290129340398", "changes": [{"field": "messages", "value": value}]}],
    }


def status_payload(phone: str) -> dict[str, Any]:
    return payload(
        [],
        [
            {
                "id": f"wamid.{uuid.uuid4().hex}",
                "status": "delivered",
                "timestamp": str(int(time.time())),
                "recipient_id": phone,
            }
        ],
    )


def encode(body: dict[str, Any], secret: str) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"Content-Type": "application/json", "X-Hub-Signature-256": sig}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--phone", default="919999900001")
    ap.add_argument("--id", default=None, help="reuse a message id to simulate a duplicate delivery")
    ap.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8000"))
    args = ap.parse_args()
    secret = os.environ.get("WHATSAPP_APP_SECRET", "replace-with-meta-app-secret")
    raw, headers = encode(payload([message(args.phone, args.text, args.id)]), secret)
    resp = httpx.post(f"{args.api}/webhook", content=raw, headers=headers, timeout=10)
    print(resp.status_code, resp.text)


if __name__ == "__main__":
    main()
