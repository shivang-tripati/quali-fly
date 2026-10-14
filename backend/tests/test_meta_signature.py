"""Unit tests for signature handling and event identity. No database needed."""

import json

import pytest

from app.services.meta_signature import (
    SignatureError,
    hash_verify_token,
    parse_signature_header,
    verify_signature,
    verify_token_matches,
)
from app.services.meta_webhook import (
    MalformedWebhook,
    compute_deduplication_key,
    describe_event,
    identify_source,
    parse_envelope,
    safe_headers,
)
from tests.meta_fixtures import IG_A, PAGE_A, instagram_payload, leadgen_payload, messaging_payload

SECRET = "unit-test-secret"


def test_a_real_hmac_signature_round_trips():
    raw = b'{"object":"page","entry":[]}'
    import hashlib
    import hmac

    header = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()

    verify_signature(SECRET, raw, header)  # does not raise


def test_whitespace_added_after_signing_is_rejected():
    """The body is signed as sent, so a trailing byte breaks verification."""
    import hashlib
    import hmac

    raw = b'{"a":1}'
    header = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()

    with pytest.raises(SignatureError):
        verify_signature(SECRET, raw + b" ", header)


def test_key_reordering_after_signing_is_rejected():
    """Proves validation never re-serialises: byte order matters, JSON order does not."""
    import hashlib
    import hmac

    signed = b'{"a":1,"b":2}'
    header = "sha256=" + hmac.new(SECRET.encode(), signed, hashlib.sha256).hexdigest()
    reordered = b'{"b":2,"a":1}'

    with pytest.raises(SignatureError):
        verify_signature(SECRET, reordered, header)


def test_unconfigured_app_secret_fails_closed():
    with pytest.raises(SignatureError):
        verify_signature(None, b"{}", "sha256=" + "0" * 64)


@pytest.mark.parametrize("value", [None, "", "sha1=" + "0" * 40, "sha256=abc", "garbage"])
def test_unsupported_signature_forms_are_rejected(value):
    with pytest.raises(SignatureError):
        parse_signature_header(value)


def test_verify_token_is_stored_as_a_digest():
    token = "a-verification-token"

    digest = hash_verify_token(token)

    assert token not in digest
    assert len(digest) == 64
    assert verify_token_matches(token, digest)
    assert not verify_token_matches("other", digest)
    assert not verify_token_matches(token, None)
    assert not verify_token_matches(None, digest)


# ------------------------------------------------------------- event identity


def test_source_is_read_from_entry_id_per_object_type():
    assert identify_source(leadgen_payload("123")).page_id == "123"
    assert identify_source(instagram_payload(IG_A)).instagram_business_id == IG_A
    assert (
        identify_source(
            {"object": "whatsapp_business_account", "entry": [{"id": "999"}]}
        ).whatsapp_business_account_id
        == "999"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"entry": []},
        {"entry": [{}]},
        {"entry": [1, 2]},
        {"entry": "nope"},
    ],
)
def test_unroutable_payloads_raise(payload):
    with pytest.raises(MalformedWebhook):
        identify_source(payload)


def test_unknown_inner_fields_do_not_break_identification():
    """Forward compatibility: a future event shape still routes."""
    payload = {
        "object": "page",
        "entry": [{"id": PAGE_A, "some_future_field": {"nested": True}}],
    }

    assert identify_source(payload).page_id == PAGE_A


def test_event_description_prefers_the_meta_message_id():
    object_type, event_type, meta_event_id = describe_event(messaging_payload(PAGE_A, "mid.$x"))

    assert (object_type, event_type, meta_event_id) == ("page", "messaging", "mid.$x")


def test_event_description_falls_back_to_changed_fields():
    object_type, event_type, meta_event_id = describe_event(leadgen_payload(PAGE_A))

    assert (object_type, event_type, meta_event_id) == ("page", "leadgen", None)


def test_dedup_key_prefers_the_message_id():
    key = compute_deduplication_key(messaging_payload(PAGE_A, "mid.$y"), "mid.$y")

    assert key == "meta:mid:mid.$y"


def test_dedup_key_is_stable_across_key_ordering_but_the_message_id_wins():
    first = compute_deduplication_key({"a": 1, "b": 2}, None)
    second = compute_deduplication_key({"b": 2, "a": 1}, None)

    assert first == second
    assert first.startswith("meta:sha256:")


def test_dedup_key_differs_for_different_payloads():
    assert compute_deduplication_key(leadgen_payload("1"), None) != (
        compute_deduplication_key(leadgen_payload("2"), None)
    )


def test_dedup_key_never_uses_a_timestamp_or_random_value():
    """Two deliveries seconds apart must produce the same key."""
    payload = leadgen_payload(PAGE_A)
    assert compute_deduplication_key(payload, None) == compute_deduplication_key(payload, None)
    assert "1730000000" not in compute_deduplication_key(payload, None)


def test_parse_envelope_rejects_non_json():
    with pytest.raises(MalformedWebhook):
        parse_envelope(b"not json")
    with pytest.raises(MalformedWebhook):
        parse_envelope(b"[1,2,3]")


def test_parse_envelope_accepts_an_object():
    assert parse_envelope(b'{"object":"page"}') == {"object": "page"}


def test_safe_headers_drops_the_signature_and_unknown_headers():
    class Headers(dict):
        def __contains__(self, key):
            return dict.__contains__(self, key)

    headers = Headers(
        {
            "user-agent": "facebookplatform/1.0",
            "content-type": "application/json",
            "x-hub-signature-256": "sha256=deadbeef",
            "authorization": "Bearer nope",
        }
    )

    kept = safe_headers(headers)

    assert set(kept) == {"user-agent", "content-type"}
    assert "deadbeef" not in json.dumps(kept)
