from app.whatsapp.payload import WebhookPayload, extract_messages
from scripts import wa


def test_cloud_api_sample_parsed() -> None:
    body = wa.payload([wa.message("919999900012", "Hi, CBC tomorrow", "wamid.A", ts=1763643000)])
    [msg] = extract_messages(WebhookPayload.model_validate(body))
    assert msg.wa_message_id == "wamid.A"
    assert msg.phone == "919999900012"
    assert msg.body == "Hi, CBC tomorrow"
    assert int(msg.wa_timestamp.timestamp()) == 1763643000


def test_multiple_messages_in_one_webhook() -> None:
    body = wa.payload([wa.message("919999900012", "one", "wamid.1"), wa.message("919999900013", "two", "wamid.2")])
    assert [m.wa_message_id for m in extract_messages(WebhookPayload.model_validate(body))] == ["wamid.1", "wamid.2"]


def test_status_callbacks_ignored() -> None:
    assert extract_messages(WebhookPayload.model_validate(wa.status_payload("919999900012"))) == []


def test_non_text_message_kept_with_empty_body() -> None:
    body = wa.payload([wa.message("919999900012", None, "wamid.img", msg_type="image")])
    [msg] = extract_messages(WebhookPayload.model_validate(body))
    assert msg.msg_type == "image"
    assert msg.body == ""
