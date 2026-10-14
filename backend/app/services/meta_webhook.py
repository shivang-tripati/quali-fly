"""Meta webhook ingestion: source identification, deduplication, durable storage.

The whole synchronous path is deliberately small:

    raw body -> HMAC check -> JSON parse -> source identifiers -> connection+tenant
    -> SET LOCAL app.current_tenant -> one idempotent INSERT -> ACK

No network call, no Meta Graph API, no lead/conversation work. Everything downstream
is a separate processor reading `meta_webhook_events`.
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tenant_context import set_current_tenant
from app.models.meta_connection import MetaProvider
from app.models.meta_webhook_event import MetaWebhookEventStatus
from app.repositories import meta_connections as connection_repo
from app.repositories import meta_webhook_events as event_repo

log = logging.getLogger("app.meta.webhook")

# Headers worth keeping. Anything not on this list - notably X-Hub-Signature-256 -
# is never written to the database.
SAFE_HEADERS = ("user-agent", "content-type", "x-hub-app-id")

_META = "meta"


class MalformedWebhook(Exception):
    """The body is not a Meta webhook envelope we can identify a source from."""


@dataclass(frozen=True, slots=True)
class MetaSource:
    """The external identifiers a payload can be routed by."""

    page_id: str | None = None
    instagram_business_id: str | None = None
    whatsapp_business_account_id: str | None = None

    def is_empty(self) -> bool:
        return not (self.page_id or self.instagram_business_id or self.whatsapp_business_account_id)


@dataclass(frozen=True, slots=True)
class IngestResult:
    event_id: uuid.UUID
    tenant_id: uuid.UUID
    meta_connection_id: uuid.UUID
    status: MetaWebhookEventStatus
    created: bool

    @property
    def duplicate(self) -> bool:
        return not self.created


def parse_envelope(raw_body: bytes) -> dict[str, Any]:
    """Parse the already signature-verified body. Never called before verification."""
    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        raise MalformedWebhook("body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise MalformedWebhook("body is not a JSON object")
    return payload


def identify_source(payload: dict[str, Any]) -> MetaSource:
    """Extract routing identifiers.

    Moderate validation on purpose: unknown inner fields must not make a legitimate
    future Meta event variant fail. Only the structure needed to find a source is
    required.
    """
    entries = payload.get("entry")
    if not isinstance(entries, list) or not entries:
        raise MalformedWebhook("payload has no entry list")

    object_type = str(payload.get("object") or "")
    ids = [
        str(entry["id"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("id") is not None
    ]
    if not ids:
        raise MalformedWebhook("payload carries no routable Meta identifier")

    # `object` says which namespace entry[].id belongs to.
    if object_type == "whatsapp_business_account":
        return MetaSource(whatsapp_business_account_id=ids[0])
    if object_type == "instagram":
        return MetaSource(instagram_business_id=ids[0])
    return MetaSource(page_id=ids[0])


def describe_event(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(object_type, event_type, meta_event_id) for observability and Phase 3+.

    Page webhooks report changes as `entry[].changes[].field` - a Page leadgen
    notification arrives as `{"field": "leadgen", "value": {...}}` (verified against
    Meta's leadgen webhook documentation on 2026-09-28). Instagram instead uses
    `entry[].changed_fields`, and Messenger uses `entry[].messaging`. All three are
    read, because a payload's shape is determined by its surface, not by us.
    """
    object_type = payload.get("object")
    event_type: str | None = None
    meta_event_id: str | None = None

    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if isinstance(change, dict) and isinstance(change.get("field"), str):
                candidate = change["field"].strip()
                if candidate and event_type is None:
                    event_type = candidate
        for changed in entry.get("changed_fields") or []:
            candidate = str(changed).strip()
            if candidate and event_type is None:
                event_type = candidate
        for message in entry.get("messaging") or []:
            if not isinstance(message, dict):
                continue
            body = message.get("message")
            mid = body.get("mid") if isinstance(body, dict) else None
            if mid and meta_event_id is None:
                meta_event_id = str(mid)
        if meta_event_id is None:
            # Phase 10-O.3. The Cloud API names its own message id but hides it two levels
            # down, and the Page branches above cannot see it. Without this, a WhatsApp
            # delivery's dedupe key falls back to hashing the whole envelope - which works
            # for a byte-identical retry but is one re-serialized field away from failing,
            # and the per-message unique index would then be the only thing preventing a
            # duplicate row. Same "first id wins" rule as the Page surface.
            meta_event_id = _first_whatsapp_message_id(entry)
        if event_type is None and meta_event_id is not None:
            event_type = "messaging"
        if event_type is None and entry.get("standby"):
            event_type = "standby"

    return (
        str(object_type) if object_type is not None else None,
        event_type,
        meta_event_id,
    )


def _first_whatsapp_message_id(entry: dict[str, Any]) -> str | None:
    """The first Cloud API `wamid` in one entry, if any."""
    for change in entry.get("changes") or []:
        if not isinstance(change, dict):
            continue
        value = change.get("value")
        if not isinstance(value, dict):
            continue
        for message in value.get("messages") or []:
            if isinstance(message, dict) and isinstance(message.get("id"), str):
                if message["id"]:
                    return message["id"]
    return None


def compute_deduplication_key(payload: dict[str, Any], meta_event_id: str | None) -> str:
    """A deterministic identity for the event. Never a timestamp or a random value.

    Meta's `messaging[].message.mid` is globally unique and stable, so it is preferred.
    Otherwise the canonical form of the envelope is hashed: a Meta retry is
    byte-identical, and two genuinely different events differ.
    """
    if meta_event_id:
        return f"{_META}:mid:{meta_event_id}"
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"{_META}:sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def safe_headers(request_headers) -> dict[str, str]:
    """Allowlist only. The signature and any credential-bearing header are dropped."""
    return {name: request_headers[name] for name in SAFE_HEADERS if name in request_headers}


async def ingest(
    session: AsyncSession,
    *,
    raw_body: bytes,
    request_headers,
) -> IngestResult:
    """Identify, resolve, deduplicate and store one delivery.

    `session` is inside the request transaction, so the INSERT commits with it.
    """
    payload = parse_envelope(raw_body)
    source = identify_source(payload)
    object_type, event_type, meta_event_id = describe_event(payload)
    dedupe_key = compute_deduplication_key(payload, meta_event_id)

    # Pre-tenant, trusted lookup. This is the one place `meta_connections` is read
    # without `app.current_tenant`, via the SECURITY DEFINER function.
    resolved = await connection_repo.resolve_active_connection(
        session,
        page_id=source.page_id,
        instagram_business_id=source.instagram_business_id,
        whatsapp_business_account_id=source.whatsapp_business_account_id,
    )
    if resolved is None:
        log.warning("webhook_rejected", extra={"reason": "unknown_or_inactive_source"})
        raise LookupError("unknown_meta_source")

    # From here the event is an ordinary tenant-scoped write.
    await set_current_tenant(session, resolved.tenant_id)
    received_at = datetime.now(UTC)

    event, created = await event_repo.insert_if_new(
        session,
        tenant_id=resolved.tenant_id,
        meta_connection_id=resolved.connection_id,
        deduplication_key=dedupe_key,
        meta_event_id=meta_event_id,
        object_type=object_type,
        event_type=event_type,
        source_identifier=source.page_id or source.instagram_business_id,
        payload=payload,
        headers=safe_headers(request_headers),
        received_at=received_at,
    )

    if created:
        await connection_repo.mark_webhook_received(session, resolved.connection_id, received_at)
        log.info(
            "webhook_persisted",
            extra={
                "meta_connection_id": str(resolved.connection_id),
                "meta_event_id": meta_event_id,
                "event_type": event_type,
                "provider": resolved.provider.value,
                "status": event.status.value,
            },
        )
    else:
        log.info(
            "webhook_duplicate",
            extra={
                "meta_connection_id": str(resolved.connection_id),
                "meta_event_id": meta_event_id,
                "event_type": event_type,
                "status": event.status.value,
            },
        )

    return IngestResult(
        event_id=event.id,
        tenant_id=resolved.tenant_id,
        meta_connection_id=resolved.connection_id,
        status=event.status,
        created=created,
    )


__all__ = [
    "IngestResult",
    "MalformedWebhook",
    "MetaProvider",
    "MetaSource",
    "compute_deduplication_key",
    "describe_event",
    "identify_source",
    "ingest",
    "parse_envelope",
    "safe_headers",
]
