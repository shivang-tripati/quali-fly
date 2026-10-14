"""Shared Meta webhook fixtures and payload builders.

Signatures are produced from the real `META_APP_SECRET` and the *exact bytes* that
will be sent, so these tests exercise the same raw-body path Meta uses.

Payload shapes were checked against Meta's developer documentation on 2026-09-28. That
matters more than it looks: a Page leadgen notification arrives under
`entry[].changes[]`, *not* under Instagram's `changed_fields` and not under Messenger's
`messaging`. An earlier version of this file used the Instagram shape for leadgen,
which made the ingestion tests pass against a payload Meta never sends.
"""

import hashlib
import hmac
import json

import pytest

from app.services.meta_signature import compute_signature

META_APP_SECRET = "test-meta-app-secret"
SIGNATURE_HEADER = "X-Hub-Signature-256"

VERIFY_TOKEN = "acme-verify-token-0123456789"
OTHER_VERIFY_TOKEN = "globex-verify-token-9876543210"

PAGE_A = "1000000000000001"
PAGE_B = "2000000000000002"
IG_A = "3000000000000003"

LEADGEN_A = "5000000000000005"
FORM_A = "6000000000000006"
AD_A = "7000000000000007"


def body(payload: dict) -> bytes:
    """Serialise exactly as a client would, so the signature matches these bytes."""
    return json.dumps(payload).encode()


def signature_for(raw_body: bytes, secret: str = META_APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def signed_headers(raw_body: bytes, **extra: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature_for(raw_body),
        "User-Agent": "facebookplatform/1.0",
        **extra,
    }


def leadgen_change(leadgen_id: str = LEADGEN_A, page_id: str = PAGE_A) -> dict:
    """The `changes[]` entry Meta documents for a Page leadgen notification."""
    return {
        "field": "leadgen",
        "value": {
            "leadgen_id": leadgen_id,
            "page_id": page_id,
            "form_id": FORM_A,
            "adgroup_id": "8000000000000008",
            "ad_id": AD_A,
            "created_time": 1730000100,
        },
    }


def leadgen_payload(page_id: str = PAGE_A, leadgen_id: str = LEADGEN_A) -> dict:
    """A real single-lead leadgen delivery, in Meta's documented shape."""
    return {
        "object": "page",
        "entry": [{"id": page_id, "time": 1730000100, "changes": [leadgen_change(leadgen_id)]}],
    }


def multi_leadgen_payload(page_id: str = PAGE_A) -> dict:
    """One delivery carrying two leads. Meta batches these; both must become leads."""
    return {
        "object": "page",
        "entry": [
            {
                "id": page_id,
                "time": 1730000100,
                "changes": [
                    leadgen_change(LEADGEN_A, page_id),
                    leadgen_change("5000000000000009", page_id),
                ],
            }
        ],
    }


def page_post_payload(page_id: str = PAGE_A) -> dict:
    """An unrelated Page event. Must never be treated as a lead."""
    return {
        "object": "page",
        "entry": [
            {
                "id": page_id,
                "time": 1730000200,
                "changes": [{"field": "feed", "value": {"from": {"id": "999"}}}],
            }
        ],
    }


def changed_fields_payload(page_id: str = PAGE_A) -> dict:
    """Instagram's shape, where the field name is a bare string in `changed_fields`."""
    return {
        "object": "instagram",
        "entry": [{"id": IG_A, "time": 1730000002, "changed_fields": ["comments"]}],
    }


def lead_graph_payload(
    leadgen_id: str = LEADGEN_A, *, name: str = "Ada Lovelace", email: str | None = "ada@ex.test"
) -> dict:
    """A Graph API lead response, in the documented `field_data` shape."""
    answers = [
        {"name": "full_name", "values": [name]},
        {"name": "car_make", "values": ["Honda"]},
    ]
    if email is not None:
        answers.append({"name": "email", "values": [email]})
    answers.append({"name": "phone_number", "values": ["+15550100"]})
    return {
        "id": leadgen_id,
        "created_time": "2015-02-28T08:49:14+0000",
        "ad_id": AD_A,
        "form_id": FORM_A,
        "field_data": answers,
    }


def messaging_payload(page_id: str = PAGE_A, mid: str = "mid.$abc123") -> dict:
    return {
        "object": "page",
        "entry": [
            {
                "id": page_id,
                "time": 1730000001,
                "messaging": [
                    {
                        "sender": {"id": "999"},
                        "recipient": {"id": page_id},
                        "timestamp": 1730000001,
                        "message": {"mid": mid, "text": "hello"},
                    }
                ],
            }
        ],
    }


def instagram_payload(ig_business_id: str = IG_A) -> dict:
    return {
        "object": "instagram",
        "entry": [
            {
                "id": ig_business_id,
                "time": 1730000002,
                "messaging": [
                    {
                        "sender": {"id": "888"},
                        "recipient": {"id": ig_business_id},
                        "timestamp": 1730000002,
                        "message": {"mid": "mid.$ig999", "text": "hi"},
                    }
                ],
            }
        ],
    }


def raw_signature_is_over_the_sent_bytes() -> None:
    """Guard: the fixture helpers must agree with the implementation."""
    raw = body(leadgen_payload())
    assert "sha256=" + compute_signature(META_APP_SECRET, raw).hex() == signature_for(raw)


@pytest.fixture
def verify_token() -> str:
    return VERIFY_TOKEN


@pytest.fixture
def other_verify_token() -> str:
    return OTHER_VERIFY_TOKEN
