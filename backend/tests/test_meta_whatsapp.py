"""WhatsApp Business: inbound normalization, outbound transport, statuses, setup state.

Phase 10-O.3 adds a third transport. The thing worth testing is that it is *only* a third
transport: the same conversation engine, the same AI service, the same follow-up and
handoff rules. So these tests deliberately reach through the real engine and the real
webhook path rather than asserting on a WhatsApp-shaped object in isolation, because a
dedicated WhatsApp pipeline is precisely the thing that must not exist.

The payload fixtures are transcribed from Meta's WhatsApp Cloud API webhook and
WhatsAPI references (2026-10-02), so a failure here means our reading of the contract
drifted - which is the signal worth having, given no real account can be called.

No real credentials, no real network: the Meta HTTP boundary is the only thing
substituted, and the database is never mocked.
"""

import pytest

from app.core.config import get_settings
from app.core.crypto import encrypt
from app.db.tenant_context import set_current_tenant
from app.models.membership import Role
from app.models.meta_connection import MetaConnection
from app.services.meta_graph import MetaTransientError
from app.services.meta_messaging import (
    extract_inbound_messages,
    extract_whatsapp_statuses,
)
from tests.factories import (
    add_member,
    create_meta_connection,
    create_tenant,
    create_user,
    issue_session,
)
from tests.fakes import FakeGraph
from tests.meta_fixtures import PAGE_A

CONNECTS = "/api/v1/meta/connections"
SESSION_COOKIE = "qf_session"

WABA_A = "7000000000000007"
WABA_B = "8000000000000008"
# Bound to nobody, so a rebind test is not fighting the unique index.
WABA_C = "8500000000000012"
PHONE_A = "7100000000000009"
PHONE_B = "7200000000000010"
PHONE_FOREIGN = "7300000000000011"

PAGE_TOKEN = "page-token-for-the-journey"
WA_RECIPIENT = "447700900123"
WAID = "wamid.HBgNNTU1MTk5OTk1MBcARjI0NzY4MDAxMjM0Njc4OTA="

# -- payloads transcribed from Meta's Cloud API webhook reference ---------------

WHATSAPP_TEXT = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": WABA_A,
            "time": 1757000000,
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "44 7700 900123",
                            "phone_number_id": PHONE_A,
                        },
                        "messages": [
                            {
                                "from": WA_RECIPIENT,
                                "id": WAID,
                                "timestamp": "1757000000",
                                "type": "text",
                                "text": {"body": "do you have any SUVs in stock?"},
                            }
                        ],
                    },
                }
            ],
        }
    ],
}

WHATSAPP_IMAGE = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": WABA_A,
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"phone_number_id": PHONE_A},
                        "messages": [
                            {
                                "from": WA_RECIPIENT,
                                "id": "wamid-image-1",
                                "timestamp": "1757000100",
                                "type": "image",
                                "image": {
                                    "id": "media-123",
                                    "mime_type": "image/jpeg",
                                    "sha256": "abc",
                                },
                            }
                        ],
                    },
                }
            ],
        }
    ],
}

WHATSAPP_STATUS = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": WABA_A,
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"phone_number_id": PHONE_A},
                        "statuses": [
                            {
                                "id": WAID,
                                "status": "delivered",
                                "timestamp": "1757000200",
                                "recipient_id": WA_RECIPIENT,
                                "conversation": {"id": "conv-1", "origin": {"type": "user"}},
                            }
                        ],
                    },
                }
            ],
        }
    ],
}

WHATSAPP_FAILED_STATUS = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": WABA_A,
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "statuses": [
                            {
                                "id": WAID,
                                "status": "failed",
                                "timestamp": "1757000300",
                                "recipient_id": WA_RECIPIENT,
                                "errors": [
                                    {
                                        "code": 131049,
                                        "title": "Message undeliverable",
                                        "error_data": {"details": "recipient blocked us"},
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        }
    ],
}

#: Both halves in one delivery, which Meta sends and which must not lose either.
WHATSAPP_MIXED = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": WABA_A,
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"phone_number_id": PHONE_A},
                        "messages": [
                            {
                                "from": WA_RECIPIENT,
                                "id": "wamid-mixed-1",
                                "timestamp": "1757000400",
                                "type": "text",
                                "text": {"body": "hello there"},
                            }
                        ],
                        "statuses": [
                            {
                                "id": "wamid-prior-send",
                                "status": "read",
                                "timestamp": "1757000450",
                                "recipient_id": WA_RECIPIENT,
                            }
                        ],
                    },
                }
            ],
        }
    ],
}


@pytest.fixture
async def world(owner):
    tenant_a = await create_tenant(owner, name="Acme", slug="acme")
    tenant_b = await create_tenant(owner, name="Globex", slug="globex")
    admin = await create_user(owner, email="admin@dev.local", name="Admin")
    member = await create_user(owner, email="member@dev.local", name="Member")
    b_admin = await create_user(owner, email="b-admin@dev.local", name="B Admin")
    await add_member(owner, tenant_id=tenant_a, user_id=admin, role=Role.OWNER)
    await add_member(owner, tenant_id=tenant_a, user_id=member, role=Role.MEMBER)
    await add_member(owner, tenant_id=tenant_b, user_id=b_admin, role=Role.OWNER)

    conn_a = await create_meta_connection(
        owner, tenant_id=tenant_a, page_id=PAGE_A, whatsapp_business_account_id=WABA_A
    )
    conn_b = await create_meta_connection(
        owner, tenant_id=tenant_b, whatsapp_business_account_id=WABA_B
    )

    key = get_settings().meta_token_encryption_key
    async with owner() as session, session.begin():
        await set_current_tenant(session, tenant_a)
        connection = await session.get(MetaConnection, conn_a)
        connection.access_token_encrypted = encrypt(PAGE_TOKEN, key=key)
        connection.display_name = "Acme Auto Sales"
        await set_current_tenant(session, tenant_b)
        other = await session.get(MetaConnection, conn_b)
        other.access_token_encrypted = encrypt("globex-token", key=key)

    return {
        "a": tenant_a,
        "b": tenant_b,
        "admin": admin,
        "member": member,
        "b_admin": b_admin,
        "conn_a": conn_a,
        "conn_b": conn_b,
    }


async def _as(api, owner, user_id, tenant_id):
    api.cookies.set(SESSION_COOKIE, await issue_session(owner, user_id=user_id))
    return {"X-Tenant-ID": str(tenant_id)}


@pytest.fixture
def wa_graph(monkeypatch):
    """Point the Graph boundary at a `FakeGraph` carrying WhatsApp answers.

    Substituted at the constructor, so decryption, validation and persistence are the real
    code paths.
    """
    from app.services import meta_whatsapp

    graph = FakeGraph()
    monkeypatch.setattr(meta_whatsapp, "MetaGraphClient", lambda **_kwargs: graph)
    return graph


def _with_numbers(graph: FakeGraph, waba_id: str, *phone_numbers) -> FakeGraph:
    from app.services.meta_graph import WhatsAppPhoneNumber

    graph.phone_numbers[waba_id] = [
        WhatsAppPhoneNumber(
            phone_number_id=number.phone_number_id,
            display_number=number.display_number,
            verified_name=number.verified_name,
            is_verified=number.is_verified,
        )
        for number in phone_numbers
    ]
    return graph


def _phone(phone_number_id: str, display: str, name: str, verified: bool = True):
    from app.services.meta_graph import WhatsAppPhoneNumber

    return WhatsAppPhoneNumber(
        phone_number_id=phone_number_id,
        display_number=display,
        verified_name=name,
        is_verified=verified,
    )


def _account(waba_id: str, name: str):
    from app.services.meta_graph import WhatsAppBusinessAccount

    return WhatsAppBusinessAccount(waba_id=waba_id, name=name)


async def _select(api, world, headers, phone_number_id: str):
    """Choose a number the way the settings screen does, so callers stay about their own step."""
    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp",
        json={"phone_number_id": phone_number_id},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response


# ------------------------------------------------ inbound normalization


def test_a_cloud_api_text_message_normalizes_onto_the_shared_shape():
    messages = extract_inbound_messages(WHATSAPP_TEXT)

    assert len(messages) == 1
    message = messages[0]
    assert message.participant_id == WA_RECIPIENT
    assert message.meta_message_id == WAID
    assert message.source_id == WABA_A
    assert message.text == "do you have any SUVs in stock?"
    assert message.message_type == "text"
    # Seconds, not milliseconds. Getting this wrong dates the message to 1970 and closes
    # the 24-hour window permanently, so it is asserted rather than assumed.
    assert message.sent_at is not None
    assert message.sent_at.year == 2025
    assert message.sent_at.timestamp() == 1757000000


def test_a_non_text_message_is_stored_without_a_media_url():
    """Cloud API media links are authenticated and expire, exactly like Page attachments."""
    messages = extract_inbound_messages(WHATSAPP_IMAGE)

    assert len(messages) == 1
    assert messages[0].message_type == "image"
    assert messages[0].text is None
    assert messages[0].attachments == [{"type": "image"}]
    assert "media-123" not in str(messages[0].attachments)


def test_status_reports_are_not_returned_as_inbound_messages():
    """A status is Meta talking about *our* message, not a customer speaking.

    Returning these from the inbound path would create a customer message row with the
    customer's own id as sender.
    """
    assert extract_inbound_messages(WHATSAPP_STATUS) == []
    assert len(extract_whatsapp_statuses(WHATSAPP_STATUS)) == 1


def test_a_delivery_carrying_both_halves_yields_both():
    messages = extract_inbound_messages(WHATSAPP_MIXED)
    statuses = extract_whatsapp_statuses(WHATSAPP_MIXED)

    assert [m.text for m in messages] == ["hello there"]
    assert [s.status for s in statuses] == ["read"]


def test_a_malformed_change_is_skipped_rather_than_raising():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {"id": WABA_A, "changes": [None, {}, {"value": "not-an-object"}, {"value": {}}]},
            "not-a-dict",
        ],
    }
    assert extract_inbound_messages(payload) == []
    assert extract_whatsapp_statuses(payload) == []


def test_a_message_with_no_text_or_id_is_not_invented():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": WABA_A,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                {"from": WA_RECIPIENT, "type": "text"},  # no id
                                {"id": "wamid-x", "type": "text"},  # no from
                            ]
                        },
                    }
                ],
            }
        ],
    }
    assert extract_inbound_messages(payload) == []


def test_statuses_are_ignored_on_a_non_whatsapp_surface():
    """One status path, not two: a Page delivery has none of this."""
    page_payload = {
        "object": "page",
        "entry": [
            {
                "id": PAGE_A,
                "changes": [
                    {
                        "field": "messages",
                        "value": {"statuses": [{"id": "x", "status": "delivered"}]},
                    }
                ],
            }
        ],
    }
    assert extract_whatsapp_statuses(page_payload) == []


def test_a_status_error_code_is_read_but_never_the_text():
    statuses = extract_whatsapp_statuses(WHATSAPP_FAILED_STATUS)

    assert len(statuses) == 1
    assert statuses[0].status == "failed"
    assert statuses[0].error_code == 131049
    # The human-readable detail must not travel with it: it can contain customer content.
    assert "recipient blocked us" not in str(statuses[0])


# ------------------------------------------------ number discovery + selection


async def test_the_numbers_meta_reports_are_offered(api, owner, world, wa_graph):
    _with_numbers(wa_graph, WABA_A, _phone(PHONE_A, "+44 7700 900123", "Acme Auto Sales"))
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(f"{CONNECTS}/{world['conn_a']}/whatsapp-numbers", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["whatsapp_business_account_id"] == WABA_A
    assert body["connected_id"] is None
    assert body["numbers"] == [
        {
            "phone_number_id": PHONE_A,
            "display_number": "+44 7700 900123",
            "verified_name": "Acme Auto Sales",
            "is_verified": True,
            "label": "Acme Auto Sales · +44 7700 900123",
        }
    ]
    assert wa_graph.phone_number_calls == [{"waba_id": WABA_A, "access_token": PAGE_TOKEN}]
    assert PAGE_TOKEN not in response.text


async def test_an_account_with_no_number_is_a_200_with_nothing_offered(api, owner, world, wa_graph):
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(f"{CONNECTS}/{world['conn_a']}/whatsapp-numbers", headers=headers)

    assert response.status_code == 200
    assert response.json()["numbers"] == []


async def test_a_connection_with_no_whatsapp_account_says_so(api, owner, world, wa_graph):
    # A distinct page id: the fixture's connection already holds PAGE_A, and the
    # partial unique index on ACTIVE page_id is real.
    conn = await create_meta_connection(owner, tenant_id=world["a"], page_id="1000000000000088")
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(f"{CONNECTS}/{conn}/whatsapp-numbers", headers=headers)

    assert response.status_code == 422
    assert "WhatsApp Business Account" in response.json()["error"]["message"]
    assert wa_graph.phone_number_calls == []


async def test_a_selected_number_is_persisted_with_its_readable_form(api, owner, world, wa_graph):
    _with_numbers(wa_graph, WABA_A, _phone(PHONE_A, "+44 7700 900123", "Acme Auto Sales"))
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp",
        json={"phone_number_id": PHONE_A},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["whatsapp_phone_number_id"] == PHONE_A
    assert body["whatsapp_display_number"] == "+44 7700 900123"
    assert body["whatsapp_business_account_id"] == WABA_A
    assert PAGE_TOKEN not in response.text

    async with owner() as session, session.begin():
        await set_current_tenant(session, world["a"])
        connection = await session.get(MetaConnection, world["conn_a"])
        assert connection.whatsapp_phone_number_id == PHONE_A


async def test_an_invented_number_id_is_refused(api, owner, world, wa_graph):
    """The browser's number is a proposal. This is the whole security point of the phase."""
    _with_numbers(wa_graph, WABA_A, _phone(PHONE_A, "+44 7700 900123", "Acme Auto Sales"))
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp",
        json={"phone_number_id": PHONE_FOREIGN},
        headers=headers,
    )

    assert response.status_code == 422
    async with owner() as session, session.begin():
        await set_current_tenant(session, world["a"])
        assert (await session.get(MetaConnection, world["conn_a"])).whatsapp_phone_number_id is None


async def test_a_number_belonging_to_another_account_is_refused(api, owner, world, wa_graph):
    _with_numbers(wa_graph, WABA_A, _phone(PHONE_A, "+44 7700 900123", "Acme"))
    _with_numbers(wa_graph, WABA_B, _phone(PHONE_B, "+44 7700 900124", "Globex"))
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp",
        json={"phone_number_id": PHONE_B},
        headers=headers,
    )

    assert response.status_code == 422
    # The lookup went to this tenant's account, not the one that owns PHONE_B.
    assert [call["waba_id"] for call in wa_graph.phone_number_calls] == [WABA_A]


async def test_a_second_tenant_using_the_same_number_gets_a_conflict(api, owner, world, wa_graph):
    _with_numbers(wa_graph, WABA_A, _phone(PHONE_A, "+44 7700 900123", "Acme"))
    _with_numbers(wa_graph, WABA_B, _phone(PHONE_A, "+44 7700 900123", "Acme"))
    headers_a = await _as(api, owner, world["admin"], world["a"])
    assert (
        await api.post(
            f"{CONNECTS}/{world['conn_a']}/whatsapp",
            json={"phone_number_id": PHONE_A},
            headers=headers_a,
        )
    ).status_code == 200

    headers_b = await _as(api, owner, world["b_admin"], world["b"])
    conflict = await api.post(
        f"{CONNECTS}/{world['conn_b']}/whatsapp",
        json={"phone_number_id": PHONE_A},
        headers=headers_b,
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "META_IDENTIFIER_CONFLICT"


async def test_the_generic_connection_api_cannot_aim_a_send(api, owner, world, wa_graph):
    """Neither the phone number nor the account may be set from a request body.

    Both are trusted provider ids: the account is what inbound webhook routing keys on, and
    the number is the address sends are POSTed to. Both now only move through endpoints that
    ask Meta first.
    """
    headers = await _as(api, owner, world["admin"], world["a"])

    created = await api.post(
        CONNECTS,
        json={
            "whatsapp_business_account_id": WABA_A,
            "whatsapp_phone_number_id": PHONE_FOREIGN,
        },
        headers=headers,
    )
    patched_account = await api.patch(
        f"{CONNECTS}/{world['conn_a']}",
        json={"whatsapp_business_account_id": WABA_B},
        headers=headers,
    )
    patched_number = await api.patch(
        f"{CONNECTS}/{world['conn_a']}",
        json={"whatsapp_phone_number_id": PHONE_FOREIGN},
        headers=headers,
    )

    assert created.status_code == 422
    assert patched_account.status_code == 422
    assert patched_number.status_code == 422
    async with owner() as session, session.begin():
        await set_current_tenant(session, world["a"])
        connection = await session.get(MetaConnection, world["conn_a"])
        assert connection.whatsapp_phone_number_id is None
        assert connection.whatsapp_business_account_id == WABA_A


# ------------------------------------------------ verified account association


async def test_an_account_meta_confirms_is_persisted(api, owner, world, wa_graph):
    wa_graph.business_accounts[WABA_A] = _account(WABA_A, "Acme Auto Sales")
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp/business-account",
        json={"whatsapp_business_account_id": WABA_A},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["whatsapp_business_account_id"] == WABA_A
    assert wa_graph.account_calls[-1]["waba_id"] == WABA_A
    # Confirmed with the connection's own decrypted credential, never the user token.
    assert wa_graph.account_calls[-1]["access_token"] == PAGE_TOKEN


async def test_an_account_meta_will_not_confirm_is_refused(api, owner, world, wa_graph):
    """The point of the call: an id the server never offered cannot be bound."""
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp/business-account",
        json={"whatsapp_business_account_id": WABA_B},
        headers=headers,
    )

    assert response.status_code == 422, response.text
    # One refusal for every reason, and no hint about what the id turned out to be.
    assert "did not confirm" in response.text
    assert WABA_B not in response.text


async def test_binding_an_account_forgets_the_previous_number(api, owner, world, wa_graph):
    """A number chosen under the old account is not a send address under the new one."""
    wa_graph.business_accounts[WABA_C] = _account(WABA_C, "Other Co")
    _with_numbers(wa_graph, WABA_A, _phone(PHONE_A, "+44 7700 900123", "Acme"))
    headers = await _as(api, owner, world["admin"], world["a"])
    await _select(api, world, headers, PHONE_A)

    rebound = await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp/business-account",
        json={"whatsapp_business_account_id": WABA_C},
        headers=headers,
    )

    assert rebound.status_code == 200, rebound.text
    assert rebound.json()["whatsapp_business_account_id"] == WABA_C
    assert rebound.json()["whatsapp_phone_number_id"] is None
    assert rebound.json()["whatsapp_display_number"] is None


async def test_an_account_already_bound_elsewhere_is_a_conflict(api, owner, world, wa_graph):
    """One account, one workspace - or inbound routing has two possible answers."""
    wa_graph.business_accounts[WABA_B] = _account(WABA_B, "Globex")
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp/business-account",
        json={"whatsapp_business_account_id": WABA_B},
        headers=headers,
    )

    assert response.status_code == 409, response.text
    async with owner() as session, session.begin():
        await set_current_tenant(session, world["a"])
        connection = await session.get(MetaConnection, world["conn_a"])
        assert connection.whatsapp_business_account_id == WABA_A


async def test_another_tenant_cannot_bind_an_account(api, owner, world, wa_graph):
    wa_graph.business_accounts[WABA_B] = _account(WABA_B, "Other Co")
    headers = await _as(api, owner, world["b_admin"], world["b"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp/business-account",
        json={"whatsapp_business_account_id": WABA_B},
        headers=headers,
    )

    assert response.status_code == 404
    assert not wa_graph.account_calls, "a credential was spent on another tenant's id"
    async with owner() as session, session.begin():
        await set_current_tenant(session, world["a"])
        connection = await session.get(MetaConnection, world["conn_a"])
        assert connection.whatsapp_business_account_id == WABA_A


async def test_a_member_cannot_bind_an_account(api, owner, world, wa_graph):
    wa_graph.business_accounts[WABA_B] = _account(WABA_B, "Other Co")
    headers = await _as(api, owner, world["member"], world["a"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp/business-account",
        json={"whatsapp_business_account_id": WABA_B},
        headers=headers,
    )

    assert response.status_code == 403


async def test_a_meta_outage_is_not_reported_as_a_bad_account(api, owner, world, wa_graph):
    """Refusing a *transient* failure as "that is not your account" would be a lie.

    The tenant would go looking for a typo in an id that was correct all along.
    """
    wa_graph.account_error = MetaTransientError("Meta temporarily unavailable (code 2)")
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp/business-account",
        json={"whatsapp_business_account_id": WABA_A},
        headers=headers,
    )

    assert response.status_code == 503
    assert "temporarily unavailable" in response.text.lower()


async def test_another_tenant_neither_reads_nor_selects(api, owner, world, wa_graph):
    _with_numbers(wa_graph, WABA_A, _phone(PHONE_A, "+44 7700 900123", "Acme"))
    headers = await _as(api, owner, world["b_admin"], world["b"])

    listed = await api.get(f"{CONNECTS}/{world['conn_a']}/whatsapp-numbers", headers=headers)
    selected = await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp",
        json={"phone_number_id": PHONE_A},
        headers=headers,
    )

    assert listed.status_code == 404
    assert selected.status_code == 404
    assert wa_graph.phone_number_calls == []


async def test_a_member_cannot_list_or_select(api, owner, world, wa_graph):
    _with_numbers(wa_graph, WABA_A, _phone(PHONE_A, "+44 7700 900123", "Acme"))
    headers = await _as(api, owner, world["member"], world["a"])

    listed = await api.get(f"{CONNECTS}/{world['conn_a']}/whatsapp-numbers", headers=headers)
    selected = await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp",
        json={"phone_number_id": PHONE_A},
        headers=headers,
    )

    assert listed.status_code == 403
    assert selected.status_code == 403
    assert wa_graph.phone_number_calls == []


# ------------------------------------------------ control-plane honesty


async def test_the_control_plane_reflects_a_configured_number(api, owner, world, wa_graph):
    _with_numbers(wa_graph, WABA_A, _phone(PHONE_A, "+44 7700 900123", "Acme"))
    headers = await _as(api, owner, world["admin"], world["a"])
    await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp",
        json={"phone_number_id": PHONE_A},
        headers=headers,
    )

    response = await api.get("/api/v1/tenant/whatsapp", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] in {"configured", "ready"}
    assert body["phone_number_id"] == PHONE_A
    assert body["display_number"] == "+44 7700 900123"
    assert body["meta_connection_id"] == str(world["conn_a"])
    # Whatever the state, the response must not claim anything was delivered.
    assert "delivered" not in response.text


async def test_the_control_plane_says_awaiting_a_number_before_one_is_chosen(api, owner, world):
    headers = await _as(api, owner, world["admin"], world["a"])

    body = (await api.get("/api/v1/tenant/whatsapp", headers=headers)).json()

    assert body["state"] == "awaiting_number"
    assert body["ready"] is False
    assert body["phone_number_id"] is None


async def test_the_control_plane_says_awaiting_an_account_before_one_is_bound(api, owner):
    """A Meta connection with no account is mid-setup, not disconnected.

    Reporting `not_connected` here would tell the tenant to go and connect Meta - which they
    already did - instead of adding the account id.
    """
    tenant = await create_tenant(owner, name="Bare", slug="bare")
    admin = await create_user(owner, email="bare@dev.local", name="Bare")
    await add_member(owner, tenant_id=tenant, user_id=admin, role=Role.OWNER)
    await create_meta_connection(owner, tenant_id=tenant, page_id="1000000000000009")
    headers = await _as(api, owner, admin, tenant)

    body = (await api.get("/api/v1/tenant/whatsapp", headers=headers)).json()

    assert body["state"] == "awaiting_account"
    assert body["ready"] is False
    assert body["whatsapp_business_account_id"] is None


async def test_the_control_plane_says_not_connected_with_no_meta_connection(api, owner):
    tenant = await create_tenant(owner, name="Unconnected", slug="unconnected")
    admin = await create_user(owner, email="unconnected@dev.local", name="Unconnected")
    await add_member(owner, tenant_id=tenant, user_id=admin, role=Role.OWNER)
    headers = await _as(api, owner, admin, tenant)

    body = (await api.get("/api/v1/tenant/whatsapp", headers=headers)).json()

    assert body["state"] == "not_connected"
    assert body["ready"] is False
    assert body["meta_connection_id"] is None


async def test_ready_requires_a_platform_credential(api, owner, world, wa_graph):
    """`configured` and `ready` differ by one thing, and it is not the tenant's doing."""
    from app.services import meta_whatsapp

    _with_numbers(wa_graph, WABA_A, _phone(PHONE_A, "+44 7700 900123", "Acme"))
    headers = await _as(api, owner, world["admin"], world["a"])
    await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp",
        json={"phone_number_id": PHONE_A},
        headers=headers,
    )

    async with owner() as session, session.begin():
        await set_current_tenant(session, world["a"])
        connection = await session.get(MetaConnection, world["conn_a"])

    unconfigured = get_settings().model_copy(update={"meta_app_id": None})
    assert meta_whatsapp.setup_state(connection, unconfigured)["state"] == "configured"
    assert meta_whatsapp.setup_state(connection, unconfigured)["ready"] is False

    configured = get_settings().model_copy(update={"meta_app_id": "1234567890"})
    assert meta_whatsapp.setup_state(connection, configured)["state"] == "ready"
    assert meta_whatsapp.setup_state(connection, configured)["ready"] is True


async def test_a_disconnected_connection_drops_out_of_ready(api, owner, world, wa_graph):

    _with_numbers(wa_graph, WABA_A, _phone(PHONE_A, "+44 7700 900123", "Acme"))
    headers = await _as(api, owner, world["admin"], world["a"])
    await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp",
        json={"phone_number_id": PHONE_A},
        headers=headers,
    )
    await api.delete(f"{CONNECTS}/{world['conn_a']}", headers=headers)

    body = (await api.get("/api/v1/tenant/whatsapp", headers=headers)).json()

    assert body["ready"] is False
    # A disconnect is the tenant's own action, so it reads as "not connected" rather than
    # "configured" - and with no connection left to call, no identifiers are reported either.
    # Reporting a number here would invite the tenant to think it is still in effect.
    assert body["state"] == "not_connected"
    assert body["phone_number_id"] is None
    assert body["meta_connection_id"] is None


async def test_a_disconnected_connection_is_not_reported_over_a_live_one(
    api, owner, world, wa_graph
):
    """The stale row must not win, or a reconnect looks like it did nothing."""
    _with_numbers(wa_graph, WABA_A, _phone(PHONE_A, "+44 7700 900123", "Acme"))
    headers = await _as(api, owner, world["admin"], world["a"])
    await _select(api, world, headers, PHONE_A)
    await api.delete(f"{CONNECTS}/{world['conn_a']}", headers=headers)

    live = await create_meta_connection(
        owner,
        tenant_id=world["a"],
        page_id="1000000000000042",
        whatsapp_business_account_id=WABA_C,
    )
    async with owner() as session, session.begin():
        await set_current_tenant(session, world["a"])
        connection = await session.get(MetaConnection, live)
        connection.access_token_encrypted = encrypt(
            PAGE_TOKEN, key=get_settings().meta_token_encryption_key
        )
    wa_graph.business_accounts[WABA_C] = _account(WABA_C, "Acme Two")
    _with_numbers(wa_graph, WABA_C, _phone(PHONE_B, "+44 7700 900999", "Acme Two"))
    await api.post(
        f"{CONNECTS}/{live}/whatsapp",
        json={"phone_number_id": PHONE_B},
        headers=headers,
    )

    body = (await api.get("/api/v1/tenant/whatsapp", headers=headers)).json()

    assert body["state"] == "ready"
    assert body["meta_connection_id"] == str(live)


async def test_only_an_admin_may_read_the_control_plane(api, owner, world):
    headers = await _as(api, owner, world["member"], world["a"])

    response = await api.get("/api/v1/tenant/whatsapp", headers=headers)

    assert response.status_code == 403


async def test_no_response_ever_contains_a_credential(api, owner, world, wa_graph):
    """The whole Settings surface, checked in one pass rather than field by field."""
    _with_numbers(wa_graph, WABA_A, _phone(PHONE_A, "+44 7700 900123", "Acme"))
    headers = await _as(api, owner, world["admin"], world["a"])
    await api.post(
        f"{CONNECTS}/{world['conn_a']}/whatsapp",
        json={"phone_number_id": PHONE_A},
        headers=headers,
    )

    for path in (
        f"{CONNECTS}/{world['conn_a']}",
        f"{CONNECTS}/{world['conn_a']}/whatsapp-numbers",
        "/api/v1/tenant/whatsapp",
        CONNECTS,
    ):
        response = await api.get(path, headers=headers)
        assert response.status_code == 200, path
        assert PAGE_TOKEN not in response.text, path
        body = response.json()
        if isinstance(body, list):
            for item in body:
                assert item["access_token_configured"] is True
                assert "access_token" not in item


# ------------------------------------------------ identifiers stay unique


def test_the_ids_used_by_this_module_are_unique_strings():
    """These constants go into rows with unique indexes, so they must not clash."""
    ids = [WABA_A, WABA_B, PHONE_A, PHONE_B, PHONE_FOREIGN, WA_RECIPIENT, WAID]
    assert len(set(ids)) == len(ids)
    assert all(isinstance(value, str) and value for value in ids)
