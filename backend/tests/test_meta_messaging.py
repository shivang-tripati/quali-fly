"""Messaging payload extraction, the channel registry, and outbound text validation.

The payload shapes here are transcribed from Meta's Messenger Platform `messages` and
`messaging_referrals` references (verified 2026-09-28), so a test failing here means our
understanding of the contract drifted, not that Meta changed - which is exactly the
signal worth having.
"""

import pytest

from app.models.conversation import Channel
from app.services.channels import (
    WHATSAPP_PRODUCT,
    InstagramAdapter,
    MessengerAdapter,
    SendRequest,
    UnknownRecipient,
    WhatsAppAdapter,
    channel_for_payload,
    describe,
)
from app.services.meta_messaging import (
    MAX_TEXT_LENGTH,
    MalformedMessage,
    extract_inbound_messages,
    normalize_outbound,
)

# -- fixtures transcribed from Meta's documentation ----------------------------

MESSENGER_TEXT = {
    "object": "page",
    "entry": [
        {
            "id": "PAGE1",
            "time": 1458692752478,
            "messaging": [
                {
                    "sender": {"id": "PSID-1"},
                    "recipient": {"id": "PAGE1"},
                    "timestamp": 1458692752478,
                    "message": {"mid": "mid.1457764197618:41d1", "text": "hello, world!"},
                }
            ],
        }
    ],
}

MESSENGER_ATTACHMENT = {
    "object": "page",
    "entry": [
        {
            "id": "PAGE1",
            "time": 1518479195594,
            "messaging": [
                {
                    "sender": {"id": "PSID-1"},
                    "recipient": {"id": "PAGE1"},
                    "timestamp": 1518479195308,
                    "message": {
                        "mid": "mid.$cAAJdkrCd2ORnva8ErFhjGm0X_Q_c",
                        "attachments": [
                            {"type": "image", "payload": {"url": "https://cdn.test/x.jpg"}}
                        ],
                    },
                }
            ],
        }
    ],
}

# `messages` field: the referral sits *inside* `message`.
ADS_REFERRAL_IN_MESSAGE = {
    "object": "page",
    "entry": [
        {
            "id": "PAGE1",
            "time": 1458692752478,
            "messaging": [
                {
                    "sender": {"id": "PSID-1"},
                    "recipient": {"id": "PAGE1"},
                    "timestamp": 1458692752478,
                    "message": {
                        "mid": "mid.ads",
                        "text": "hi from the ad",
                        "referral": {
                            "ref": "promo1",
                            "ad_id": "AD-77",
                            "source": "ADS",
                            "type": "OPEN_THREAD",
                        },
                    },
                }
            ],
        }
    ],
}

# `messaging_referrals` field: the referral is a *sibling* of `message`.
ADS_REFERRAL_SIBLING = {
    "object": "page",
    "entry": [
        {
            "id": "PAGE1",
            "time": 1458692752478,
            "messaging": [
                {
                    "sender": {"id": "PSID-1"},
                    "recipient": {"id": "PAGE1"},
                    "timestamp": 1458692752478,
                    "referral": {
                        "ref": "promo2",
                        "ad_id": "AD-88",
                        "source": "ADS",
                        "type": "OPEN_THREAD",
                    },
                    "message": {"mid": "mid.ref", "text": "second message"},
                }
            ],
        }
    ],
}

INSTAGRAM_TEXT = {
    "object": "instagram",
    "entry": [
        {
            "id": "IGBUSINESS1",
            "time": 1518479195594,
            "messaging": [
                {
                    "sender": {"id": "IGSID-9"},
                    "recipient": {"id": "IGBUSINESS1"},
                    "timestamp": 1518479195308,
                    "message": {"mid": "mid.ig1", "text": "hello from instagram"},
                }
            ],
        }
    ],
}

# A batched delivery. Meta documents `messaging` as a single-element array, but it is an
# array and the whole delivery is stored as one event.
TWO_MESSAGES = {
    "object": "page",
    "entry": [
        {
            "id": "PAGE1",
            "time": 1,
            "messaging": [
                {
                    "sender": {"id": "PSID-1"},
                    "recipient": {"id": "PAGE1"},
                    "timestamp": 1000,
                    "message": {"mid": "mid.a", "text": "first"},
                },
                {
                    "sender": {"id": "PSID-2"},
                    "recipient": {"id": "PAGE1"},
                    "timestamp": 2000,
                    "message": {"mid": "mid.b", "text": "second"},
                },
            ],
        }
    ],
}


# -- extraction ---------------------------------------------------------------


def test_a_messenger_message_is_extracted_with_its_mid_and_participant():
    messages = extract_inbound_messages(MESSENGER_TEXT)
    assert len(messages) == 1
    message = messages[0]
    assert message.meta_message_id == "mid.1457764197618:41d1"
    assert message.participant_id == "PSID-1"
    assert message.text == "hello, world!"
    assert message.message_type == "text"
    assert message.source_id == "PAGE1"


def test_the_timestamp_is_milliseconds_not_seconds():
    """Meta sends epoch milliseconds; reading them as seconds lands in 1970."""
    message = extract_inbound_messages(MESSENGER_TEXT)[0]
    assert message.sent_at is not None
    assert message.sent_at.year == 2016
    assert message.sent_at.timestamp() == pytest.approx(1458692752.478, abs=1)


def test_an_instagram_message_is_extracted_the_same_way():
    message = extract_inbound_messages(INSTAGRAM_TEXT)[0]
    assert message.participant_id == "IGSID-9"
    assert message.source_id == "IGBUSINESS1"
    assert message.message_type == "text"


def test_an_attachment_message_has_no_text_but_keeps_its_type():
    message = extract_inbound_messages(MESSENGER_ATTACHMENT)[0]
    assert message.text is None
    assert message.message_type == "image"
    assert message.attachments == [{"type": "image"}]


def test_attachment_urls_are_deliberately_not_retained():
    """They expire and can point at personal media; the type is the useful part."""
    message = extract_inbound_messages(MESSENGER_ATTACHMENT)[0]
    assert "cdn.test" not in str(message.attachments)


def test_a_typed_attachment_keeps_its_title_but_not_its_url():
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "PAGE1",
                "messaging": [
                    {
                        "sender": {"id": "PSID-1"},
                        "recipient": {"id": "PAGE1"},
                        "timestamp": 1,
                        "message": {
                            "mid": "mid.file",
                            "attachments": [
                                {
                                    "type": "file",
                                    "payload": {
                                        "url": "https://cdn.test/secret.pdf",
                                        "title": "quote.pdf",
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    attachments = extract_inbound_messages(payload)[0].attachments
    assert attachments == [{"type": "file", "title": "quote.pdf"}]


def test_a_referral_inside_message_is_read():
    message = extract_inbound_messages(ADS_REFERRAL_IN_MESSAGE)[0]
    assert message.referral_ad_id == "AD-77"
    assert message.referral_ref == "promo1"


def test_a_referral_beside_message_is_also_read():
    """The `messaging_referrals` field puts it in a different place. Both are handled."""
    message = extract_inbound_messages(ADS_REFERRAL_SIBLING)[0]
    assert message.referral_ad_id == "AD-88"
    assert message.referral_ref == "promo2"


def test_a_batched_delivery_yields_every_message():
    messages = extract_inbound_messages(TWO_MESSAGES)
    assert [m.meta_message_id for m in messages] == ["mid.a", "mid.b"]
    assert [m.participant_id for m in messages] == ["PSID-1", "PSID-2"]


def test_a_message_with_no_mid_is_skipped():
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "PAGE1",
                "messaging": [{"sender": {"id": "P"}, "recipient": {"id": "PAGE1"}, "message": {}}],
            }
        ],
    }
    assert extract_inbound_messages(payload) == []


def test_events_without_a_message_object_are_not_messages():
    """Postbacks, read receipts and typing indicators share `messaging[]`."""
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "PAGE1",
                "messaging": [
                    {
                        "sender": {"id": "PSID-1"},
                        "recipient": {"id": "PAGE1"},
                        "postback": {"payload": "GET_STARTED"},
                    }
                ],
            }
        ],
    }
    assert extract_inbound_messages(payload) == []


def test_an_entry_without_a_sender_is_skipped():
    payload = {
        "object": "page",
        "entry": [{"id": "P", "messaging": [{"message": {"mid": "m", "text": "x"}}]}],
    }
    assert extract_inbound_messages(payload) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"object": "page"},
        {"object": "page", "entry": []},
        {"object": "page", "entry": [{"id": "P"}]},
        {"entry": [{"id": "P", "messaging": "not-a-list"}]},
        {"object": "page", "entry": "not-a-list"},
    ],
)
def test_non_messaging_payloads_yield_nothing(payload):
    assert extract_inbound_messages(payload) == []


def test_blank_text_becomes_none():
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "P",
                "messaging": [
                    {
                        "sender": {"id": "PSID-1"},
                        "recipient": {"id": "P"},
                        "timestamp": 1,
                        "message": {"mid": "mid.blank", "text": "   "},
                    }
                ],
            }
        ],
    }
    assert extract_inbound_messages(payload)[0].text is None


# -- channel registry ---------------------------------------------------------


def test_the_object_type_picks_the_channel():
    assert channel_for_payload("page").channel is Channel.MESSENGER
    assert channel_for_payload("instagram").channel is Channel.INSTAGRAM
    # Phase 10-O.3: the Cloud API names its object after the account, not the number.
    assert channel_for_payload("whatsapp_business_account").channel is Channel.WHATSAPP


@pytest.mark.parametrize("object_type", [None, "", "unknown", "waba", "phone_number"])
def test_an_unknown_object_type_is_not_routed(object_type):
    """Never guessed: an unroutable surface is left alone, not coerced to a channel."""
    assert channel_for_payload(object_type) is None


def test_messenger_and_instagram_send_through_the_same_endpoint_shape():
    """The reason those two share an adapter: only the recipient id type differs."""
    messenger = MessengerAdapter().build_send(participant_id="PSID-1", text="hi")
    instagram = InstagramAdapter().build_send(participant_id="IGSID-9", text="hi")
    assert isinstance(messenger, SendRequest) and isinstance(instagram, SendRequest)
    assert messenger.recipient_id == "PSID-1"
    assert instagram.recipient_id == "IGSID-9"
    assert messenger.messaging_type == instagram.messaging_type == "RESPONSE"
    # Neither opts into the Cloud API body, so every pre-Phase-10-O.3 send is unchanged.
    assert messenger.messaging_product is None
    assert instagram.messaging_product is None


def test_the_required_permissions_are_meta_documented_ones():
    assert MessengerAdapter().required_permission == "pages_messaging"
    assert InstagramAdapter().required_permission == "instagram_manage_messages"
    assert WhatsAppAdapter().required_permission == "whatsapp_business_messaging"


def test_the_registry_covers_every_declared_channel():
    """A new enum value with no adapter is a channel nothing can send on."""
    assert {c["channel"] for c in describe()} == {"MESSENGER", "INSTAGRAM", "WHATSAPP"}
    for entry in describe():
        assert entry["required_permission"]
        assert entry["payload_objects"]


def test_whatsapp_selects_a_different_transport_from_the_other_two():
    """Phase 10-O.3's one real difference, expressed in one field.

    WhatsApp posts to `/{phone_number_id}/messages` with a different body, so its
    `SendRequest` is marked as such - and its Page Messaging fields are deliberately not
    forwarded, because `messaging_type`/`tag` are not concepts the Cloud API has and Meta
    documents them as invalid there.
    """
    request = WhatsAppAdapter().build_send(
        participant_id="447700900123",
        text="hi",
        messaging_type="MESSAGE_TAG",
        tag="HUMAN_AGENT",
    )
    assert request.messaging_product == WHATSAPP_PRODUCT
    assert request.recipient_id == "447700900123"
    assert request.tag is None
    assert request.messaging_type == "text"


def test_a_surface_we_do_not_support_cannot_be_reached():
    """The enum refuses it before the registry is even consulted.

    Stronger than a missing key: a typo'd or future surface cannot be accidentally treated
    as a supported channel by any code path.
    """
    with pytest.raises(ValueError, match="not a valid Channel"):
        Channel("WHATSAPP_BUSINESS")
    with pytest.raises(ValueError, match="not a valid Channel"):
        Channel("SMS")


def test_building_a_send_without_a_participant_is_refused():
    for adapter in (MessengerAdapter(), InstagramAdapter(), WhatsAppAdapter()):
        with pytest.raises(UnknownRecipient):
            adapter.build_send(participant_id="", text="hi")


# -- outbound text validation -------------------------------------------------


def test_outbound_text_is_trimmed():
    assert normalize_outbound("  hello  ") == "hello"


@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
def test_empty_outbound_text_is_refused(bad):
    with pytest.raises(MalformedMessage, match="empty"):
        normalize_outbound(bad)


def test_text_over_metas_limit_is_refused_locally():
    """Meta's limit is in bytes, so multibyte text is measured the same way it is."""
    with pytest.raises(MalformedMessage, match="bytes"):
        normalize_outbound("x" * (MAX_TEXT_LENGTH + 1))


def test_a_thousand_ascii_characters_are_allowed():
    assert len(normalize_outbound("x" * MAX_TEXT_LENGTH)) == MAX_TEXT_LENGTH


def test_multibyte_text_is_measured_in_bytes_not_characters():
    # 400 three-byte characters is 1200 bytes: over the limit despite being short.
    with pytest.raises(MalformedMessage):
        normalize_outbound("你" * 400)
