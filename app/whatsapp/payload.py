"""WhatsApp Cloud API webhook payload (only the fields we use; everything else is ignored)."""

from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class TextBody(_Model):
    body: str = ""


class WAMessage(_Model):
    from_: str = Field(alias="from")
    id: str = Field(min_length=1, max_length=256)
    timestamp: str
    type: str
    text: TextBody | None = None


class ChangeValue(_Model):
    messaging_product: str | None = None
    messages: list[WAMessage] = []
    statuses: list[dict[str, object]] = []


class Change(_Model):
    field: str = "messages"
    value: ChangeValue


class Entry(_Model):
    id: str | None = None
    changes: list[Change] = []


class WebhookPayload(_Model):
    object: str
    entry: list[Entry] = []


@dataclass(frozen=True, slots=True)
class InboundMessage:
    wa_message_id: str
    phone: str
    msg_type: str
    body: str
    wa_timestamp: datetime


def extract_messages(payload: WebhookPayload) -> list[InboundMessage]:
    """Flatten entry[].changes[].value.messages[]; status callbacks are ignored."""
    out: list[InboundMessage] = []
    for entry in payload.entry:
        for change in entry.changes:
            if change.field != "messages":
                continue
            for msg in change.value.messages:
                try:
                    ts = datetime.fromtimestamp(int(msg.timestamp), UTC)
                except (ValueError, OverflowError):
                    ts = datetime.now(UTC)
                body = msg.text.body if msg.type == "text" and msg.text else ""
                out.append(InboundMessage(msg.id, msg.from_, msg.type, body[:4096], ts))
    return out
