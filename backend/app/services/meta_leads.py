"""Lead Ads events: recognizing them, and normalizing what Meta returns.

Two separate shapes are handled here, and the difference is not cosmetic:

    the webhook tells us a lead *exists*      entry[].changes[].value.leadgen_id
    the Graph API tells us what it *contains*  field_data: [{name, values}]

Meta sends `entry[].changes` (an array) for Page webhooks, and a single delivery can
carry several leads at once. So a delivery is one durable event that may fan out into
several leads - which is why event deduplication and lead deduplication are separate
layers, keyed differently on purpose:

    meta_webhook_events : UNIQUE(meta_connection_id, deduplication_key)  one per delivery
    leads               : UNIQUE(tenant_id, meta_leadgen_id)               one per lead

Keying the event on a leadgen_id would collapse a batched delivery into one row and
silently drop the other leads.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("app.meta.leads")

# `changes[].field` value Meta documents for a lead notification.
LEADGEN_FIELD = "leadgen"

# Form answers are keyed by whatever the tenant named the question, so these are
# conventions rather than a contract. A form that calls it "phone" or "phone_number"
# should both land in `phone`.
_NAME_KEYS = ("full_name", "name", "first_name", "last_name")
_EMAIL_KEYS = ("email", "email_address")
_PHONE_KEYS = ("phone_number", "phone", "telephone", "mobile")


class InvalidLeadPayload(Exception):
    """Meta's response is missing the parts a Lead cannot exist without."""


@dataclass(frozen=True, slots=True)
class LeadgenReference:
    """One `leadgen` change from a webhook delivery."""

    leadgen_id: str
    form_id: str | None = None
    ad_id: str | None = None
    page_id: str | None = None
    created_time: int | None = None


@dataclass(frozen=True, slots=True)
class NormalizedLead:
    """A Lead ready to insert. Plain data: no ORM, no provider payload."""

    leadgen_id: str
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    form_id: str | None = None
    ad_id: str | None = None
    provider_created_at: datetime | None = None
    field_data: dict[str, Any] = field(default_factory=dict)


def extract_leadgen_events(payload: dict[str, Any]) -> list[LeadgenReference]:
    """Every lead referenced by one delivery. Empty means "not a lead event".

    Being strict about `field == "leadgen"` is what keeps unrelated Meta events - page
    posts, permission changes, Instagram comments - from being treated as leads.
    """
    references: list[LeadgenReference] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        entry_page_id = str(entry["id"]) if entry.get("id") is not None else None
        for change in entry.get("changes") or []:
            if not isinstance(change, dict) or change.get("field") != LEADGEN_FIELD:
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            leadgen_id = value.get("leadgen_id")
            if leadgen_id is None:
                # A leadgen change with no id is unusable; log and skip rather than
                # fail the whole delivery, which may carry other valid leads.
                log.warning("leadgen_change_without_id", extra={"page_id": entry_page_id})
                continue
            created = value.get("created_time")
            references.append(
                LeadgenReference(
                    leadgen_id=str(leadgen_id),
                    form_id=_optional_str(value.get("form_id")),
                    ad_id=_optional_str(value.get("ad_id")),
                    page_id=_optional_str(value.get("page_id")) or entry_page_id,
                    created_time=created if isinstance(created, int) else None,
                )
            )
    return references


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _first_value(answer: dict[str, Any]) -> str | None:
    """Meta answers are `{"name": ..., "values": [...]}`. Some are typed objects."""
    values = answer.get("values")
    if not isinstance(values, list) or not values:
        return None
    first = values[0]
    if isinstance(first, str):
        stripped = first.strip()
        return stripped or None
    # Custom question types answer with objects such as {"text": "..."} or a dict of
    # selections. Keep it useful without pretending to understand every shape.
    if isinstance(first, dict):
        for key in ("text", "name", "value"):
            candidate = first.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def normalize_lead(payload: dict[str, Any], *, reference: LeadgenReference) -> NormalizedLead:
    """Turn a Graph lead response into a `NormalizedLead`.

    The response's own `id` is preferred over the webhook's `leadgen_id`, because the
    Graph response is the authority on what the lead is. They are expected to match; if
    they ever do not, the response wins and the caller stores the response's id, which
    is the value the unique constraint must key on.

    Only a *controlled* subset of the provider payload is kept: the flattened answers
    plus the ids worth filtering on. The untouched response already lives on the webhook
    event, so duplicating it here would double the personal data we retain.
    """
    if not isinstance(payload, dict):
        raise InvalidLeadPayload("lead response is not an object")

    leadgen_id = payload.get("id")
    if not isinstance(leadgen_id, str) or not leadgen_id:
        raise InvalidLeadPayload("lead response has no id")

    answers: dict[str, Any] = {}
    field_data = payload.get("field_data")
    if isinstance(field_data, list):
        for answer in field_data:
            if not isinstance(answer, dict):
                continue
            name = answer.get("name")
            if not isinstance(name, str) or not name:
                continue
            value = _first_value(answer)
            if value is not None:
                answers[name] = value

    return NormalizedLead(
        leadgen_id=leadgen_id,
        name=_pick(answers, _NAME_KEYS),
        email=_pick(answers, _EMAIL_KEYS),
        phone=_pick(answers, _PHONE_KEYS),
        form_id=_optional_str(payload.get("form_id")) or reference.form_id,
        ad_id=_optional_str(payload.get("ad_id")) or reference.ad_id,
        provider_created_at=_parse_created_time(payload.get("created_time"))
        or _from_epoch(reference.created_time),
        field_data=answers,
    )


def _pick(answers: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = answers.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_created_time(value: Any) -> datetime | None:
    """Meta returns ISO-8601 with a `+0000` offset, which fromisoformat rejects."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    # Normalize `+0000` / `-0500` to `+00:00` / `-05:00`.
    if len(text) > 5 and text[-5] in "+-" and text[-3] != ":":
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _from_epoch(value: int | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


__all__ = [
    "InvalidLeadPayload",
    "LEADGEN_FIELD",
    "LeadgenReference",
    "NormalizedLead",
    "extract_leadgen_events",
    "normalize_lead",
]
