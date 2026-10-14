"""Inbound Meta messaging: recognizing a message and normalizing it.

The payload shape below is what Meta documents for the Messenger Platform `messages` field
and for the equivalent Instagram `messages` field (verified 2026-09-28):

    {"object": "page" | "instagram",
     "entry": [{"id": "<PAGE_OR_IG_ID>", "time": <ms>,
       "messaging": [{
         "sender":    {"id": "<PSID | IGSID>"},
         "recipient": {"id": "<PAGE_ID>"},
         "timestamp": <ms>,
         "message":   {"mid": "mid....", "text": "hello",
                       "attachments": [...], "quick_reply": {...}}}]}]}

Two things that are easy to get wrong, and are handled explicitly:

**There is no `thread` object.** An earlier assumption in this file's place was that
Meta supplies a thread id. It does not. For a given page, one sender id *is* one thread,
so the participant id is the conversation key - and it is also the `recipient.id` of every
outbound send.

**Ads referral data arrives in two different places.** For the `messages` field it is
inside `message.referral`; for the separate `messaging_referrals` field it is a sibling of
`message` at `messaging[].referral`. Both are read, because a business subscribing only to
`messages` gets the first and one subscribing to referrals gets the second.

Non-text messages (images, audio, stickers) carry no `text`. They are still stored, with
their attachment *types* and no URLs: Meta's attachment URLs expire, and they can point at
personal media, so retaining them would be storing a link to someone's photo for a few
hours. That is a retention decision, not an oversight.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.services.channels import WHATSAPP_OBJECT

log = logging.getLogger("app.meta.messaging")

# Meta's documented text limit for a single send.
MAX_TEXT_LENGTH = 1000


class MalformedMessage(Exception):
    """The payload is not a messaging event we can attribute to a thread."""


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One `messaging[]` entry, already attributed to a person."""

    participant_id: str
    meta_message_id: str
    source_id: str | None
    text: str | None
    message_type: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    referral_ad_id: str | None = None
    referral_ref: str | None = None
    sent_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class InboundStatus:
    """A provider's report about a message *we* sent (Phase 10-O.3, WhatsApp).

    Separate from `InboundMessage` because the two travel together in one Cloud API
    delivery and mean opposite things: one is a customer speaking, the other is Meta
    reporting on our own outbound message.
    """

    meta_message_id: str
    status: str
    recipient_id: str | None = None
    occurred_at: datetime | None = None
    #: Provider's numeric failure code on a `failed` status. For the log, never stored as
    #: the message's `failure_reason` - see `_error_code`.
    error_code: int | None = None


def extract_inbound_messages(payload: dict[str, Any]) -> list[InboundMessage]:
    """Every inbound message in one delivery. Empty means "not a messaging event".

    Meta documents `messaging` as an array that currently holds a single object, but it is
    an array, and a delivery is stored as one durable event, so all of them are returned
    and the caller persists each. That keeps the event/row split the same as Phase 3's
    batched leadgen delivery.

    **Dispatched on `object`, not on shape.** Phase 10-O.3 added the WhatsApp Cloud API,
    whose envelope is a different shape entirely (`entry[].changes[].value.messages[]`,
    not `entry[].messaging[]`). Both are normalized to the same `InboundMessage` here, so
    the caller in `meta_processor` and the conversation engine below it are unchanged - the
    adapter decides the channel, this decides the shape. Guessing a surface from the
    payload's structure instead of its declared `object` would be the kind of cleverness
    that starts ingesting a leadgen change as a message.
    """
    if str(payload.get("object") or "") == WHATSAPP_OBJECT:
        return _extract_whatsapp_messages(payload)

    messages: list[InboundMessage] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        source_id = str(entry["id"]) if entry.get("id") is not None else None
        for item in entry.get("messaging") or []:
            if not isinstance(item, dict):
                continue
            parsed = _parse_messaging_item(item, source_id)
            if parsed is not None:
                messages.append(parsed)
    return messages


def extract_whatsapp_statuses(payload: dict[str, Any]) -> list[InboundStatus]:
    """Delivery/status reports in a WhatsApp delivery, normalized.

    Separate from `extract_inbound_messages` because a status is not a conversation: it
    describes a message *we* sent. Returning them from the same function would put
    provider bookkeeping into the inbound path and risk creating a customer message row
    with the customer's own `wa_id` as a sender.

    `status` is left as Meta's string rather than mapped to a delivery enum here, because
    the mapping is a product decision (what does `read` mean for a business that only
    cares about delivery?) and the caller that owns `Message.delivery` should make it.
    """
    if str(payload.get("object") or "") != WHATSAPP_OBJECT:
        return []

    statuses: list[InboundStatus] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for status in value.get("statuses") or []:
                if not isinstance(status, dict):
                    continue
                message_id = status.get("id")
                state = status.get("status")
                if not isinstance(message_id, str) or not message_id:
                    continue
                if not isinstance(state, str) or not state:
                    continue
                statuses.append(
                    InboundStatus(
                        meta_message_id=message_id,
                        status=state,
                        recipient_id=(
                            str(status["recipient_id"])
                            if status.get("recipient_id") is not None
                            else None
                        ),
                        occurred_at=_from_epoch_seconds(status.get("timestamp")),
                        error_code=_error_code(status.get("errors")),
                    )
                )
    return statuses


def _extract_whatsapp_messages(payload: dict[str, Any]) -> list[InboundMessage]:
    """Cloud API text messages, normalized onto the shared `InboundMessage`.

    The documented shape (verified against Meta's WhatsApp Cloud API webhook reference on
    2026-10-02):

        {"object": "whatsapp_business_account",
         "entry": [{"id": "<WABA_ID>", "changes": [{
            "field": "messages",
            "value": {"messaging_product": "whatsapp",
                      "metadata": {"phone_number_id": "...", "display_phone_number": "..."},
                      "messages": [{"from": "<wa_id>", "id": "wamid...",
                                    "timestamp": "<unix seconds>",
                                    "type": "text", "text": {"body": "hello"}}]}}]}]}

    Two things differ from the Page surface and both are load-bearing:

    **The timestamp is unix *seconds*, not milliseconds.** The Page surface uses
    milliseconds. Reusing `_from_epoch_ms` here would date every WhatsApp message to 1970
    and make every conversation look decades stale, which the 24-hour window would then
    treat as permanently closed.

    **`from` is the participant id, not `sender.id`.** It is the same kind of value in
    practice - a WhatsApp-scoped id, stable per person - so the conversation key, the
    dedupe key and the outbound recipient all work unchanged.
    """
    messages: list[InboundMessage] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        source_id = str(entry["id"]) if entry.get("id") is not None else None
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for item in value.get("messages") or []:
                if not isinstance(item, dict):
                    continue
                parsed = _parse_whatsapp_message(item, source_id)
                if parsed is not None:
                    messages.append(parsed)
    return messages


def _parse_whatsapp_message(item: dict[str, Any], source_id: str | None) -> InboundMessage | None:
    sender = item.get("from")
    message_id = item.get("id")
    if sender is None or not isinstance(message_id, str) or not message_id:
        # Statuses and echoes arrive in the same array and carry no `from`/`id` pair to
        # attribute; they are read by `extract_whatsapp_statuses`, not here.
        log.debug("whatsapp_message_skipped", extra={"reason": "no_from_or_id"})
        return None

    message_type = item.get("type") if isinstance(item.get("type"), str) else "text"
    text_body = item.get("text")
    text = text_body.get("body") if isinstance(text_body, dict) else None
    if not isinstance(text, str) or not text.strip():
        # A non-text message (image, audio, document, location) still belongs in the thread
        # so the customer is not left talking to nobody, but it carries no URL: Cloud API
        # media links are authenticated and expire, exactly like the Page surface's
        # attachments, so the same retention decision applies.
        text = None

    return InboundMessage(
        participant_id=str(sender),
        meta_message_id=message_id,
        source_id=source_id,
        text=text.strip() if text else None,
        message_type=message_type,
        attachments=[{"type": message_type}] if message_type != "text" else [],
        sent_at=_from_epoch_seconds(item.get("timestamp")),
    )


def _error_code(errors: Any) -> int | None:
    """The provider's numeric failure code on a `failed` status, when it sent one.

    Kept as an int and stored nowhere but the log: `Message.failure_reason` is a short
    category, never a provider payload, and Meta's error bodies can carry customer text.
    """
    if not isinstance(errors, list):
        return None
    for entry in errors:
        if isinstance(entry, dict) and isinstance(entry.get("code"), int):
            return entry["code"]
    return None


def _from_epoch_seconds(value: Any) -> datetime | None:
    """WhatsApp sends unix *seconds*, as a *string*. See `_parse_whatsapp_message`.

    The string form is not a hypothetical: Cloud API payloads carry
    `"timestamp": "1757000000"`, and an `isinstance(int)` guard would silently discard every
    real timestamp - dating every message to `None` and leaving the 24-hour window with
    nothing to measure from.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_messaging_item(item: dict[str, Any], source_id: str | None) -> InboundMessage | None:
    sender = item.get("sender")
    participant_id = sender.get("id") if isinstance(sender, dict) else None
    body = item.get("message")

    if participant_id is None or not isinstance(body, dict):
        # Postbacks, delivery receipts, typing indicators and read receipts all arrive in
        # `messaging[]` with no `message` object. They are not conversations.
        log.debug("messaging_item_skipped", extra={"reason": "no_sender_or_message"})
        return None

    mid = body.get("mid")
    if mid is None:
        log.debug("messaging_item_skipped", extra={"reason": "no_mid"})
        return None

    attachments = _normalize_attachments(body.get("attachments"))
    text = body.get("text")
    referral = _find_referral(item, body)

    return InboundMessage(
        participant_id=str(participant_id),
        meta_message_id=str(mid),
        source_id=source_id,
        # An attachment-only message has no text; message_type then carries the meaning.
        text=text.strip() if isinstance(text, str) and text.strip() else None,
        message_type=attachments[0].get("type", "text") if attachments else "text",
        attachments=attachments,
        referral_ad_id=referral.get("ad_id") if referral else None,
        referral_ref=referral.get("ref") if referral else None,
        sent_at=_from_epoch_ms(item.get("timestamp")),
    )


def _find_referral(item: dict[str, Any], body: dict[str, Any]) -> dict[str, Any] | None:
    """Referral data appears inside `message` or beside it, depending on the field."""
    for candidate in (body.get("referral"), item.get("referral")):
        if isinstance(candidate, dict):
            return candidate
    return None


def _normalize_attachments(raw: Any) -> list[dict[str, Any]]:
    """Types and titles only. URLs are dropped on purpose - see the module docstring."""
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {}
        if isinstance(item.get("type"), str):
            entry["type"] = item["type"]
        payload = item.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("title"), str):
            entry["title"] = payload["title"]
        if entry:
            normalized.append(entry)
    return normalized


def _from_epoch_ms(value: Any) -> datetime | None:
    """Meta sends epoch *milliseconds*, not seconds. Easy to get wrong by 1000x."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def normalize_outbound(text: str) -> str:
    """Validate text for a send, before it becomes a PENDING row.

    Cheap and local: Meta's limits are documented and stable, so rejecting an obviously
    too-long message here is better than a round trip that fails after we have already
    written a row. The 1000-byte limit is Meta's, applied to UTF-8 bytes rather than
    characters because that is what Meta measures.
    """
    trimmed = text.strip()
    if not trimmed:
        raise MalformedMessage("message text is empty")
    if len(trimmed.encode()) > MAX_TEXT_LENGTH:
        raise MalformedMessage(f"message text exceeds {MAX_TEXT_LENGTH} bytes")
    return trimmed


__all__ = [
    "MAX_TEXT_LENGTH",
    "InboundMessage",
    "InboundStatus",
    "MalformedMessage",
    "extract_inbound_messages",
    "extract_whatsapp_statuses",
    "normalize_outbound",
]
