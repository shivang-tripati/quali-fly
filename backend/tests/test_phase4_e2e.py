"""The conversation vertical slice, through the real HTTP surface.

    signed Meta webhook -> durable event -> ACK
      -> worker claims -> channel adapter -> thread + message
      -> tenant reads it back, and sends a reply

Everything is the real implementation except the outbound Meta HTTP call, which cannot
be exercised without a live Meta app. The database is real, so the thread uniqueness, the
message deduplication and the tenant FK constraints being relied on are genuine.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.crypto import encrypt
from app.db.tenant_context import set_current_tenant
from app.models.conversation import Conversation, Message, MessageDelivery, MessageDirection
from app.models.membership import Role
from app.models.meta_connection import MetaConnection
from app.services.meta_processor import MetaWebhookEventProcessor
from tests.conftest import TEST_META_TOKEN_KEY
from tests.factories import add_member, create_tenant, create_user, issue_session
from tests.fakes import PAGE_TOKEN
from tests.meta_fixtures import body, signed_headers

API = "/api/v1"
WEBHOOK = f"{API}/webhooks/meta"
PAGE = "1000000000000001"
PSID = "10083363554430212"


def messenger_text(
    mid: str = "mid.e2e.1",
    text: str = "Is the car still available?",
    *,
    age: timedelta = timedelta(minutes=5),
) -> dict:
    """A recent message by default, so the 24-hour window is open.

    The timestamp is epoch *milliseconds* and the value matters: a stale one puts the
    thread outside the window and changes what the send path is allowed to do.
    """
    stamp = datetime.now(UTC) - age
    return {
        "object": "page",
        "entry": [
            {
                "id": PAGE,
                "time": int(stamp.timestamp()),
                "messaging": [
                    {
                        "sender": {"id": PSID},
                        "recipient": {"id": PAGE},
                        "timestamp": int(stamp.timestamp() * 1000),
                        "message": {"mid": mid, "text": text},
                    }
                ],
            }
        ],
    }


def instagram_text(mid: str = "mid.e2e.ig", text: str = "hello from instagram") -> dict:
    return {
        "object": "instagram",
        "entry": [
            {
                "id": "3000000000000003",
                "time": 1730000300,
                "messaging": [
                    {
                        "sender": {"id": "IGSID-42"},
                        "recipient": {"id": "3000000000000003"},
                        "timestamp": 1730000300000,
                        "message": {"mid": mid, "text": text},
                    }
                ],
            }
        ],
    }


class FakeGraph:
    def __init__(self, *, fail=None, message_id: str = "m_sent-1") -> None:
        self.sends: list[dict] = []
        self.fail = fail
        self.message_id = message_id

    async def send_message(self, **kwargs) -> str:
        self.sends.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return self.message_id


@pytest.fixture
async def tenant(owner):
    """Acme, with a session and one authorized connection.

    A single Facebook Page linked to an Instagram professional account is one
    connection carrying both identifiers, which is why there is not a separate
    Instagram connection here - and why the partial unique index on active `page_id`
    would refuse one.
    """
    tenant_id = await create_tenant(owner, name="Acme", slug="acme")
    admin = await create_user(owner, email="admin@acme.test", name="Ada Admin")
    await add_member(owner, tenant_id=tenant_id, user_id=admin, role=Role.OWNER)
    token = await issue_session(owner, user_id=admin)

    async with owner() as session, session.begin():
        connection = MetaConnection(
            tenant_id=tenant_id,
            page_id=PAGE,
            instagram_business_id="3000000000000003",
            app_id="1234567890",
            access_token_encrypted=encrypt(PAGE_TOKEN, key=TEST_META_TOKEN_KEY),
            connected_at=datetime.now(UTC),
        )
        session.add(connection)
        await session.flush()
        connection_id = connection.id

    return {
        "tenant_id": tenant_id,
        "token": token,
        "connection": connection_id,
        "headers": {"X-Tenant-ID": str(tenant_id), "Cookie": f"qf_session={token}"},
    }


async def _deliver(api, payload) -> dict:
    raw = body(payload)
    response = await api.post(WEBHOOK, content=raw, headers=signed_headers(raw))
    assert response.status_code == 200, response.text
    return response.json()


async def _drain(db, tenant_id, graph, limit: int = 5) -> int:
    from app.core.config import get_settings

    processed = 0
    for _ in range(limit):
        async with db() as session, session.begin():
            processor = MetaWebhookEventProcessor(
                session, tenant_id, graph=graph, settings=get_settings()
            )
            event = await processor.claim_next()
        if event is None:
            break
        async with db() as session, session.begin():
            processor = MetaWebhookEventProcessor(
                session, tenant_id, graph=graph, settings=get_settings()
            )
            await processor.handle(event)
        processed += 1
    return processed


# -- inbound ------------------------------------------------------------------


async def test_a_messenger_webhook_becomes_a_conversation_and_a_message(api, db, tenant):
    graph = FakeGraph()

    ack = await _deliver(api, messenger_text())
    assert ack["status"] == "ok"
    assert ack["duplicate"] is False

    # The ACK must not have created anything yet.
    assert graph.sends == []

    assert await _drain(db, tenant["tenant_id"], graph) == 1

    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    assert listed.status_code == 200
    body_ = listed.json()
    assert body_["count"] == 1
    conversation = body_["items"][0]
    assert conversation["channel"] == "MESSENGER"
    assert conversation["meta_participant_id"] == PSID
    assert conversation["meta_source_id"] == PAGE
    assert conversation["lead_id"] is None
    assert conversation["within_messaging_window"] is True

    messages = await api.get(
        f"{API}/conversations/{conversation['id']}/messages", headers=tenant["headers"]
    )
    assert messages.json()["count"] == 1
    message = messages.json()["items"][0]
    assert message["text"] == "Is the car still available?"
    assert message["direction"] == "INBOUND"
    assert message["channel"] == "MESSENGER"
    assert message["meta_message_id"] == "mid.e2e.1"
    # Declared by the PRD, never written by Phase 4.
    assert message["ai_model"] is None
    assert message["ai_prompt_tokens"] is None


async def test_an_instagram_webhook_becomes_an_instagram_conversation(api, db, tenant):
    graph = FakeGraph()
    await _deliver(api, instagram_text())
    assert await _drain(db, tenant["tenant_id"], graph) == 1

    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    conversation = listed.json()["items"][0]
    assert conversation["channel"] == "INSTAGRAM"
    assert conversation["meta_participant_id"] == "IGSID-42"

    messages = await api.get(
        f"{API}/conversations/{conversation['id']}/messages", headers=tenant["headers"]
    )
    assert messages.json()["items"][0]["channel"] == "INSTAGRAM"


async def test_a_replayed_webhook_does_not_duplicate_the_message(api, db, tenant):
    graph = FakeGraph()
    payload = messenger_text()

    first = await _deliver(api, payload)
    await _drain(db, tenant["tenant_id"], graph)
    second = await _deliver(api, payload)

    assert second["duplicate"] is True
    assert second["event_id"] == first["event_id"]
    assert await _drain(db, tenant["tenant_id"], graph) == 0

    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    assert listed.json()["count"] == 1
    messages = await api.get(
        f"{API}/conversations/{listed.json()['items'][0]['id']}/messages",
        headers=tenant["headers"],
    )
    assert messages.json()["count"] == 1


async def test_two_people_in_one_delivery_make_two_threads(api, db, tenant):
    graph = FakeGraph()
    payload = {
        "object": "page",
        "entry": [
            {
                "id": PAGE,
                "time": 1,
                "messaging": [
                    {
                        "sender": {"id": "PSID-A"},
                        "recipient": {"id": PAGE},
                        "timestamp": 1000,
                        "message": {"mid": "mid.a", "text": "first"},
                    },
                    {
                        "sender": {"id": "PSID-B"},
                        "recipient": {"id": PAGE},
                        "timestamp": 2000,
                        "message": {"mid": "mid.b", "text": "second"},
                    },
                ],
            }
        ],
    }
    await _deliver(api, payload)
    await _drain(db, tenant["tenant_id"], graph)

    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    assert listed.json()["count"] == 2


async def test_an_unrelated_event_still_creates_no_conversation(api, db, tenant):
    graph = FakeGraph()
    raw = body(
        {"object": "page", "entry": [{"id": PAGE, "changes": [{"field": "feed", "value": {}}]}]}
    )
    response = await api.post(WEBHOOK, content=raw, headers=signed_headers(raw))
    assert response.status_code == 200

    assert await _drain(db, tenant["tenant_id"], graph) == 1
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant["tenant_id"])
        assert (await session.execute(select(Conversation))).scalars().all() == []


async def test_an_unsigned_delivery_creates_nothing(api, db, tenant):
    raw = body(messenger_text())
    response = await api.post(
        WEBHOOK,
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert response.status_code == 403

    assert await _drain(db, tenant["tenant_id"], FakeGraph()) == 0
    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    assert listed.json()["count"] == 0


# -- outbound -----------------------------------------------------------------


async def test_a_reply_is_sent_to_meta_and_recorded(api, db, tenant, monkeypatch):
    from app.services import meta_oauth

    graph = FakeGraph(message_id="m_reply-42")
    monkeypatch.setattr(meta_oauth, "build_graph_client", lambda _s: graph)

    await _deliver(api, messenger_text())
    await _drain(db, tenant["tenant_id"], FakeGraph())

    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    conversation_id = listed.json()["items"][0]["id"]

    response = await api.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"text": "Yes! Two in stock. Shall we book you one?"},
        headers=tenant["headers"],
    )

    assert response.status_code == 201, response.text
    body_ = response.json()
    assert body_["delivery"] == "SENT"
    assert body_["meta_message_id"] == "m_reply-42"
    assert body_["direction"] == "OUTBOUND"
    assert body_["failure_reason"] is None

    assert len(graph.sends) == 1
    send = graph.sends[0]
    assert send["recipient_id"] == PSID
    assert send["page_id"] == PAGE
    assert send["access_token"] == PAGE_TOKEN
    assert send["messaging_type"] == "RESPONSE"

    # The thread now reads as a conversation, and the message is in it.
    messages = await api.get(
        f"{API}/conversations/{conversation_id}/messages", headers=tenant["headers"]
    )
    assert messages.json()["count"] == 2
    assert messages.json()["items"][-1]["direction"] == "OUTBOUND"


async def test_a_window_closed_reply_is_recorded_as_failed_not_raised(api, db, tenant, monkeypatch):
    """The 24-hour window is a product constraint, not a crash."""
    from app.services import meta_oauth
    from app.services.meta_graph import MetaMessageWindowClosed

    graph = FakeGraph(fail=MetaMessageWindowClosed("closed"))
    monkeypatch.setattr(meta_oauth, "build_graph_client", lambda _s: graph)

    # Two days old: outside the 24-hour window.
    old = messenger_text("mid.old", "hello", age=timedelta(days=2))
    await _deliver(api, old)
    await _drain(db, tenant["tenant_id"], FakeGraph())

    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    conversation = listed.json()["items"][0]
    assert conversation["within_messaging_window"] is False

    response = await api.post(
        f"{API}/conversations/{conversation['id']}/messages",
        json={"text": "still there?"},
        headers=tenant["headers"],
    )
    assert response.status_code == 201
    assert response.json()["delivery"] == "FAILED"
    assert response.json()["failure_reason"] == "messaging_window_closed"


async def test_a_temporarily_unavailable_meta_is_recorded_not_rolled_back(
    api, db, tenant, monkeypatch
):
    """The attempt must survive, so the endpoint answers 201 with `delivery=FAILED`.

    An earlier version raised 503 here, which rolled back the request transaction and
    deleted the very row the PENDING-first design exists to keep - a failed send would
    have left no trace at all.
    """
    from app.services import meta_oauth
    from app.services.meta_graph import MetaTransientError

    graph = FakeGraph(fail=MetaTransientError("rate limited"))
    monkeypatch.setattr(meta_oauth, "build_graph_client", lambda _s: graph)

    await _deliver(api, messenger_text())
    await _drain(db, tenant["tenant_id"], FakeGraph())
    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    conversation_id = listed.json()["items"][0]["id"]

    response = await api.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"text": "hello"},
        headers=tenant["headers"],
    )
    assert response.status_code == 201
    assert response.json()["delivery"] == "FAILED"
    assert response.json()["failure_reason"] == "meta_unavailable"

    # And the attempt is on the record, which is the point of writing it first.
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant["tenant_id"])
        failed = (
            await session.execute(
                select(Message).where(Message.direction == MessageDirection.OUTBOUND)
            )
        ).scalar_one()
    assert failed.delivery is MessageDelivery.FAILED
    assert failed.failure_reason == "meta_unavailable"


async def test_sending_to_an_unknown_conversation_is_a_404(api, tenant):
    response = await api.post(
        f"{API}/conversations/{uuid.uuid4()}/messages",
        json={"text": "hi"},
        headers=tenant["headers"],
    )
    assert response.status_code == 404


async def test_a_reply_on_a_disconnected_connection_is_refused(api, db, tenant, owner, monkeypatch):
    from sqlalchemy import update as sa_update

    from app.models.meta_connection import MetaConnectionStatus
    from app.services import meta_oauth

    graph = FakeGraph()
    monkeypatch.setattr(meta_oauth, "build_graph_client", lambda _s: graph)

    await _deliver(api, messenger_text())
    await _drain(db, tenant["tenant_id"], FakeGraph())
    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    conversation_id = listed.json()["items"][0]["id"]

    async with owner() as session, session.begin():
        await session.execute(
            sa_update(MetaConnection)
            .where(MetaConnection.id == tenant["connection"])
            .values(status=MetaConnectionStatus.INACTIVE)
        )

    response = await api.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"text": "hello"},
        headers=tenant["headers"],
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MESSAGE_NOT_SENDABLE"
    assert graph.sends == []


async def test_a_member_can_send(api, db, tenant, owner, monkeypatch):
    """Phase 10-O.1 §4 corrected this. It used to assert the opposite.

    The old assertion was "a MEMBER cannot send", justified as "sending is an
    outward-facing act, so it needs ADMIN". That reasoning was applied to the wrong baseline:
    `api/handoffs.py` deliberately lets a MEMBER list, claim, start and resolve a handoff, so
    the same MEMBER could take ownership of a customer conversation and then be refused the
    only endpoint that answers them - a dead end in the exact flow the product is built
    around.

    Sending still requires a real membership in the tenant; it no longer requires ADMIN.
    `tests/test_reply_authorization.py` covers the isolation half of that claim: a non-member,
    a cross-tenant user, an inactive user and an unauthenticated caller are all still
    refused, and the admin-only conversation endpoints are untouched.
    """
    from app.services import meta_oauth

    graph = FakeGraph()
    monkeypatch.setattr(meta_oauth, "build_graph_client", lambda _s: graph)

    member = await create_user(owner, email="member@acme.test")
    await add_member(owner, tenant_id=tenant["tenant_id"], user_id=member, role=Role.MEMBER)
    member_token = await issue_session(owner, user_id=member)

    await _deliver(api, messenger_text())
    await _drain(db, tenant["tenant_id"], FakeGraph())
    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    conversation_id = listed.json()["items"][0]["id"]

    response = await api.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"text": "hi"},
        headers={
            "X-Tenant-ID": str(tenant["tenant_id"]),
            "Cookie": f"qf_session={member_token}",
        },
    )
    assert response.status_code == 201, response.text
    assert len(graph.sends) == 1, "the member's reply never reached the provider"
    assert graph.sends[0]["text"] == "hi"


# -- isolation ----------------------------------------------------------------


async def test_conversations_are_isolated_by_tenant(api, db, tenant, owner):
    from tests.meta_fixtures import PAGE_B

    globex = await create_tenant(owner, name="Globex", slug="globex")
    carol = await create_user(owner, email="carol@globex.test")
    await add_member(owner, tenant_id=globex, user_id=carol, role=Role.OWNER)
    carol_token = await issue_session(owner, user_id=carol)

    async with owner() as session, session.begin():
        session.add(
            MetaConnection(
                tenant_id=globex,
                page_id=PAGE_B,
                access_token_encrypted=encrypt("globex-token", key=TEST_META_TOKEN_KEY),
            )
        )
        await session.flush()

    await _deliver(api, messenger_text())
    await _drain(db, tenant["tenant_id"], FakeGraph())

    acme_list = await api.get(f"{API}/conversations", headers=tenant["headers"])
    assert acme_list.json()["count"] == 1

    globex_headers = {
        "X-Tenant-ID": str(globex),
        "Cookie": f"qf_session={carol_token}",
    }
    globex_list = await api.get(f"{API}/conversations", headers=globex_headers)
    assert globex_list.json()["count"] == 0

    acme_conversation_id = acme_list.json()["items"][0]["id"]
    by_id = await api.get(f"{API}/conversations/{acme_conversation_id}", headers=globex_headers)
    assert by_id.status_code == 404
    by_id = await api.get(
        f"{API}/conversations/{acme_conversation_id}/messages", headers=globex_headers
    )
    assert by_id.status_code == 404

    # And the tenant header cannot be swapped to reach it.
    swapped = await api.get(
        f"{API}/conversations", headers={**globex_headers, "X-Tenant-ID": str(tenant["tenant_id"])}
    )
    assert swapped.status_code == 403


async def test_the_channels_endpoint_reports_meta_permissions(api, tenant):
    response = await api.get(f"{API}/conversations/channels", headers=tenant["headers"])
    assert response.status_code == 200
    by_channel = {c["channel"]: c for c in response.json()}
    assert by_channel["MESSENGER"]["required_permission"] == "pages_messaging"
    assert by_channel["INSTAGRAM"]["required_permission"] == "instagram_manage_messages"
    assert by_channel["MESSENGER"]["payload_objects"] == ["page"]
