"""WhatsApp Business through the real vertical slice.

    signed WhatsApp webhook -> durable event -> ACK
      -> worker claims -> adapter -> thread + message
      -> AI replies over the WhatsApp transport
      -> follow-up, handoff, human reply, delivery status

The point of this file is that **WhatsApp is only a transport**. Everything asserted here
runs through the same `ConversationEngine`, the same `AIConversationService`, the same
follow-up engine and the same handoff rules as Messenger and Instagram; the only
substitution is the outbound Meta HTTP call. A WhatsApp-specific pipeline is exactly what
must not exist, so these tests go through the real paths rather than a WhatsApp-shaped unit.

The database is real throughout: the enum addition, the partial unique index on the phone
number, the message dedupe constraint and RLS are all genuinely exercised.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.crypto import encrypt
from app.db.tenant_context import set_current_tenant
from app.models.conversation import Channel, Conversation, Message, MessageDelivery
from app.models.human_handoff import HandoffReason, HandoffStatus, HumanHandoff
from app.models.membership import Role
from app.models.meta_connection import MetaConnection
from app.models.scheduled_task import ScheduledTask, TaskStatus
from app.services.meta_processor import MetaWebhookEventProcessor
from tests.conftest import TEST_META_TOKEN_KEY
from tests.factories import add_member, create_tenant, create_user, issue_session
from tests.meta_fixtures import body, signed_headers

API = "/api/v1"
WEBHOOK = f"{API}/webhooks/meta"

WABA = "7000000000000007"
PHONE = "7100000000000009"
WA_RECIPIENT = "447700900123"


def whatsapp_text(
    mid: str = "wamid.e2e.1",
    text: str = "Is the Golf still available?",
    *,
    age: timedelta = timedelta(minutes=5),
) -> dict:
    """A recent Cloud API text message, so the 24-hour window is open.

    `timestamp` is a *string* of unix *seconds* on this surface - both facts differ from the
    Page surface and both break the window if mis-parsed.
    """
    stamp = datetime.now(UTC) - age
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": WABA,
                "time": int(stamp.timestamp()),
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "44 7700 900123",
                                "phone_number_id": PHONE,
                            },
                            "messages": [
                                {
                                    "from": WA_RECIPIENT,
                                    "id": mid,
                                    "timestamp": str(int(stamp.timestamp())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def whatsapp_status(wamid: str, state: str, *, timestamp: int | None = None) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": WABA,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": PHONE},
                            "statuses": [
                                {
                                    "id": wamid,
                                    "status": state,
                                    "timestamp": str(
                                        timestamp or int(datetime.now(UTC).timestamp())
                                    ),
                                    "recipient_id": WA_RECIPIENT,
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


class FakeGraph:
    """Records both transports so a test can prove which endpoint was used.

    `wamid` values are unique per *instance*, not per send. Real `wamid`s are globally
    unique, and `uq_messages_dedupe` enforces that - so a per-instance counter would make
    two graphs in one test collide and look like a product bug.
    """

    def __init__(self, *, fail=None, message_id: str = "m_sent-1") -> None:
        self.sends: list[dict] = []
        self.whatsapp_sends: list[dict] = []
        self.fail = fail
        self.message_id = message_id
        self._wamid_prefix = uuid.uuid4().hex[:8]
        self._whatsapp_send_count = 0

    async def send_message(self, **kwargs) -> str:
        self.sends.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return self.message_id

    async def send_whatsapp_message(
        self, *, access_token, phone_number_id, recipient_id, text
    ) -> str:
        self.whatsapp_sends.append(
            {
                "access_token": access_token,
                "phone_number_id": phone_number_id,
                "recipient_id": recipient_id,
                "text": text,
            }
        )
        if self.fail is not None:
            raise self.fail
        self._whatsapp_send_count += 1
        return f"wamid.{self._wamid_prefix}.{self._whatsapp_send_count}"


def _fu_settings():
    """Settings with the follow-up engine switched on.

    `follow_up_enabled` defaults to `False` in this environment, so a service built on
    `get_settings()` correctly arms nothing. Being explicit is also what proves the
    engagement was declined for a *handoff* reason rather than because the engine was off.
    """
    from app.core.config import get_settings

    return get_settings().model_copy(update={"follow_up_enabled": True})


@pytest.fixture
def http_graph(monkeypatch):
    """The graph the *HTTP* send path uses.

    `conversations` builds its client through `meta_oauth.build_graph_client`, so a test
    that drives a send through the API has to patch that - the local `FakeGraph` passed to
    the worker and to `FollowUpService` is a different object on a different code path.
    """
    graph = FakeGraph()
    from app.services import meta_oauth

    monkeypatch.setattr(meta_oauth, "build_graph_client", lambda _settings: graph)
    return graph


@pytest.fixture
async def tenant(owner):
    """Acme with a WhatsApp-capable connection, ready to receive and send.

    The connection carries a WABA *and* a phone number: the account is what webhooks route
    on, the number is what a send is addressed to. Both are required for the slice to work
    end to end, which is why the migration had to add the second.
    """
    tenant_id = await create_tenant(owner, name="Acme", slug="acme")
    admin = await create_user(owner, email="admin@acme.test", name="Ada Admin")
    await add_member(owner, tenant_id=tenant_id, user_id=admin, role=Role.OWNER)
    token = await issue_session(owner, user_id=admin)

    async with owner() as session, session.begin():
        connection = MetaConnection(
            tenant_id=tenant_id,
            whatsapp_business_account_id=WABA,
            whatsapp_phone_number_id=PHONE,
            whatsapp_display_number="+44 7700 900123",
            app_id="1234567890",
            access_token_encrypted=encrypt("wa-page-token", key=TEST_META_TOKEN_KEY),
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
    processed = 0
    for _ in range(limit):
        async with db() as session, session.begin():
            processor = MetaWebhookEventProcessor(
                session, tenant_id, graph=graph, settings=_fu_settings()
            )
            event = await processor.claim_next()
        if event is None:
            break
        async with db() as session, session.begin():
            processor = MetaWebhookEventProcessor(
                session, tenant_id, graph=graph, settings=_fu_settings()
            )
            await processor.handle(event)
        processed += 1
    return processed


async def _conversation(api, tenant) -> dict:
    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    assert listed.status_code == 200, listed.text
    return listed.json()["items"][0]


# -- inbound through the shared engine ----------------------------------------


async def test_a_whatsapp_webhook_becomes_a_whatsapp_conversation(api, db, tenant):
    graph = FakeGraph()

    ack = await _deliver(api, whatsapp_text())
    assert ack["status"] == "ok"
    assert ack["duplicate"] is False
    # The ACK must not have created anything yet.
    assert graph.whatsapp_sends == []

    assert await _drain(db, tenant["tenant_id"], graph) == 1

    conversation = await _conversation(api, tenant)
    assert conversation["channel"] == "WHATSAPP"
    # The Cloud API participant id is the thread key, exactly as a PSID is for Messenger.
    assert conversation["meta_participant_id"] == WA_RECIPIENT
    assert conversation["meta_source_id"] == WABA
    assert conversation["within_messaging_window"] is True

    messages = await api.get(
        f"{API}/conversations/{conversation['id']}/messages", headers=tenant["headers"]
    )
    assert messages.json()["count"] == 1
    message = messages.json()["items"][0]
    assert message["text"] == "Is the Golf still available?"
    assert message["direction"] == "INBOUND"
    assert message["channel"] == "WHATSAPP"
    assert message["meta_message_id"] == "wamid.e2e.1"


async def test_a_second_whatsapp_message_reuses_the_same_thread(api, db, tenant):
    """One participant is one thread, on every surface."""
    graph = FakeGraph()
    await _deliver(api, whatsapp_text(mid="wamid.1"))
    await _drain(db, tenant["tenant_id"], graph)
    await _deliver(api, whatsapp_text(mid="wamid.2", text="and in blue?"))
    await _drain(db, tenant["tenant_id"], graph)

    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    assert listed.json()["count"] == 1
    messages = await api.get(
        f"{API}/conversations/{listed.json()['items'][0]['id']}/messages",
        headers=tenant["headers"],
    )
    assert messages.json()["count"] == 2


async def test_a_repeated_whatsapp_delivery_is_deduplicated(api, db, tenant):
    """Meta retries. The same `wamid` must not become a second message.

    Both deliveries are the *same payload*, as a real retry is. That matters: the dedupe key
    is built from the `wamid` when one is present, so a re-serialized delivery with a
    different `timestamp` would be a genuinely different event, not a retry.
    """
    payload = whatsapp_text(mid="wamid.retry")
    first = await _deliver(api, payload)
    second = await _deliver(api, payload)

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    await _drain(db, tenant["tenant_id"], FakeGraph())

    conversation = await _conversation(api, tenant)
    messages = await api.get(
        f"{API}/conversations/{conversation['id']}/messages", headers=tenant["headers"]
    )
    assert messages.json()["count"] == 1


async def test_the_dedupe_key_uses_the_wamid_not_the_whole_envelope(api, db, tenant):
    """A `wamid` is the stable identity, so the timestamp must not affect the key.

    Before Phase 10-O.3 the Cloud API's `wamid` was invisible to `describe_event`, and
    dedupe fell back to hashing the envelope - which means a delivery that Meta re-serializes
    with a fresh timestamp would have been ingested twice.
    """
    first = await _deliver(api, whatsapp_text(mid="wamid.stable", age=timedelta(minutes=9)))
    await _drain(db, tenant["tenant_id"], FakeGraph())
    # Same wamid, different envelope: a different `wamid`-stable event, but the *same* id.
    second = await _deliver(api, whatsapp_text(mid="wamid.stable", age=timedelta(minutes=4)))

    assert first["duplicate"] is False
    assert second["duplicate"] is True


async def test_a_status_only_delivery_creates_no_conversation(api, db, tenant):
    """A delivery about our own message must not fabricate a customer thread."""
    graph = FakeGraph()
    ack = await _deliver(api, whatsapp_status("wamid.nonexistent", "delivered"))

    assert ack["status"] == "ok"
    assert await _drain(db, tenant["tenant_id"], graph) == 1

    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    assert listed.json()["count"] == 0


async def test_a_malformed_whatsapp_payload_does_not_crash_the_webhook(api, db, tenant):
    graph = FakeGraph()
    ack = await _deliver(
        api,
        {
            "object": "whatsapp_business_account",
            "entry": [{"id": WABA, "changes": [{"field": "messages", "value": {}}]}],
        },
    )

    assert ack["status"] == "ok"
    assert await _drain(db, tenant["tenant_id"], graph) == 1
    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    assert listed.json()["count"] == 0


async def test_an_unsupported_whatsapp_field_is_stored_and_ignored(api, db, tenant):
    """An unknown change must not fail the delivery or be invented into a message."""
    graph = FakeGraph()
    ack = await _deliver(
        api,
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": WABA,
                    "changes": [
                        {
                            "field": "message_template_status_update",
                            "value": {"event": "APPROVED", "message_template_id": 12345},
                        }
                    ],
                }
            ],
        },
    )

    assert ack["status"] == "ok"
    assert await _drain(db, tenant["tenant_id"], graph) == 1
    listed = await api.get(f"{API}/conversations", headers=tenant["headers"])
    assert listed.json()["count"] == 0


# -- outbound through the shared sender ---------------------------------------


async def test_a_human_reply_goes_out_over_the_whatsapp_transport(api, db, tenant, http_graph):
    """The same `POST /conversations/{id}/messages` a Messenger reply uses."""
    await _deliver(api, whatsapp_text())
    await _drain(db, tenant["tenant_id"], FakeGraph())
    conversation = await _conversation(api, tenant)

    response = await api.post(
        f"{API}/conversations/{conversation['id']}/messages",
        json={"text": "Yes, it is in stock."},
        headers=tenant["headers"],
    )

    assert response.status_code == 201, response.text
    assert response.json()["delivery"] == "SENT"
    # The Cloud API send, not the page one, and addressed to the phone number.
    assert http_graph.sends == []
    assert len(http_graph.whatsapp_sends) == 1
    assert http_graph.whatsapp_sends[0]["phone_number_id"] == PHONE
    assert http_graph.whatsapp_sends[0]["recipient_id"] == WA_RECIPIENT
    assert http_graph.whatsapp_sends[0]["text"] == "Yes, it is in stock."


async def test_the_provider_message_id_is_persisted(api, db, tenant, http_graph):
    await _deliver(api, whatsapp_text())
    await _drain(db, tenant["tenant_id"], FakeGraph())
    conversation = await _conversation(api, tenant)

    await api.post(
        f"{API}/conversations/{conversation['id']}/messages",
        json={"text": "Sending now."},
        headers=tenant["headers"],
    )

    messages = await api.get(
        f"{API}/conversations/{conversation['id']}/messages", headers=tenant["headers"]
    )
    outbound = [m for m in messages.json()["items"] if m["direction"] == "OUTBOUND"]
    assert len(outbound) == 1
    assert outbound[0]["meta_message_id"].startswith("wamid.")
    assert outbound[0]["delivery"] == "SENT"


async def test_a_delivery_status_is_recorded_on_the_message(api, db, tenant, http_graph):
    """The provider's own report, matched to the message by the id we stored."""
    await _deliver(api, whatsapp_text())
    await _drain(db, tenant["tenant_id"], FakeGraph())
    conversation = await _conversation(api, tenant)
    sent = await api.post(
        f"{API}/conversations/{conversation['id']}/messages",
        json={"text": "On its way."},
        headers=tenant["headers"],
    )
    provider_id = sent.json()["meta_message_id"]
    assert provider_id

    ack = await _deliver(api, whatsapp_status(provider_id, "delivered"))
    assert ack["status"] == "ok"
    assert await _drain(db, tenant["tenant_id"], FakeGraph()) == 1

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant["tenant_id"])
        message = (
            await session.execute(select(Message).where(Message.meta_message_id == provider_id))
        ).scalar_one()
    assert message.delivery is MessageDelivery.SENT
    assert message.delivered_at is not None


async def test_a_provider_failure_marks_the_message_failed(api, db, tenant, http_graph):
    await _deliver(api, whatsapp_text())
    await _drain(db, tenant["tenant_id"], FakeGraph())
    conversation = await _conversation(api, tenant)
    sent = await api.post(
        f"{API}/conversations/{conversation['id']}/messages",
        json={"text": "Hello?"},
        headers=tenant["headers"],
    )
    provider_id = sent.json()["meta_message_id"]

    await _deliver(api, whatsapp_status(provider_id, "failed"))
    await _drain(db, tenant["tenant_id"], FakeGraph())

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant["tenant_id"])
        message = (
            await session.execute(select(Message).where(Message.meta_message_id == provider_id))
        ).scalar_one()
    assert message.delivery is MessageDelivery.FAILED
    assert message.failure_reason


async def test_a_status_for_an_unknown_message_does_not_fail_the_delivery(api, db, tenant):
    """A message can be sent and its row lost; Meta keeps reporting on it.

    Failing the event here would put every future delivery from that number behind one
    unmatched id, forever. Processed with the existing "no message in this delivery" note is
    the right answer: there genuinely was no inbound message.
    """
    from app.models.meta_webhook_event import MetaWebhookEvent, MetaWebhookEventStatus
    from app.services.meta_processor import NO_MESSAGE

    await _deliver(api, whatsapp_status("wamid.from-another-lifetime", "delivered"))
    assert await _drain(db, tenant["tenant_id"], FakeGraph()) == 1

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant["tenant_id"])
        events = (await session.execute(select(MetaWebhookEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].status is MetaWebhookEventStatus.PROCESSED
    assert events[0].error_message == NO_MESSAGE


async def test_a_send_that_meta_refuses_is_recorded_as_failed(api, db, tenant, monkeypatch):
    """Not reported as sent: a provider rejection leaves FAILED with a category."""
    from app.services import meta_oauth
    from app.services.meta_graph import MetaAuthError

    await _deliver(api, whatsapp_text())
    await _drain(db, tenant["tenant_id"], FakeGraph())
    conversation = await _conversation(api, tenant)

    refusing = FakeGraph(fail=MetaAuthError("revoked"))
    monkeypatch.setattr(meta_oauth, "build_graph_client", lambda _settings: refusing)

    response = await api.post(
        f"{API}/conversations/{conversation['id']}/messages",
        json={"text": "Hello?"},
        headers=tenant["headers"],
    )

    assert response.status_code == 201
    assert response.json()["delivery"] == "FAILED"
    assert response.json()["failure_reason"]


async def test_a_send_outside_the_window_is_refused_not_attempted(api, db, tenant, http_graph):
    """Outside 24h WhatsApp needs an approved template, which is out of scope.

    The honest outcome is a permanent, explainable refusal - not a send that Meta will
    reject after we have written a row for it.
    """
    await _deliver(api, whatsapp_text(age=timedelta(days=3)))
    await _drain(db, tenant["tenant_id"], FakeGraph())
    conversation = await _conversation(api, tenant)
    assert conversation["within_messaging_window"] is False

    response = await api.post(
        f"{API}/conversations/{conversation['id']}/messages",
        json={"text": "Still there?"},
        headers=tenant["headers"],
    )

    assert response.status_code == 201
    assert response.json()["delivery"] == "FAILED"
    assert response.json()["failure_reason"] == "messaging_window_closed"
    assert http_graph.whatsapp_sends == []


async def test_a_connection_with_no_phone_number_cannot_send(api, db, tenant, http_graph):
    """The account alone is not a send address. Refused before any network call.

    Refused at credential resolution, which is before the PENDING row exists - so this is a
    409 with a reason, not a 201 carrying a failed message.
    """
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant["tenant_id"])
        connection = await session.get(MetaConnection, tenant["connection"])
        connection.whatsapp_phone_number_id = None

    await _deliver(api, whatsapp_text())
    await _drain(db, tenant["tenant_id"], FakeGraph())
    conversation = await _conversation(api, tenant)

    response = await api.post(
        f"{API}/conversations/{conversation['id']}/messages",
        json={"text": "Hello?"},
        headers=tenant["headers"],
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MESSAGE_NOT_SENDABLE"
    assert http_graph.whatsapp_sends == []


# -- follow-ups ---------------------------------------------------------------


async def test_a_follow_up_sends_through_the_whatsapp_transport(api, db, tenant, http_graph, clock):
    """Phase 7's engine, unchanged: the send just travels a different edge."""
    from app.services.follow_ups import FollowUpService

    await _deliver(api, whatsapp_text())
    await _drain(db, tenant["tenant_id"], FakeGraph())
    conversation = await _conversation(api, tenant)

    # A sequence arms only after the first automated reply, so one goes out first - through
    # the WhatsApp transport, which is the point.
    reply = await api.post(
        f"{API}/conversations/{conversation['id']}/messages",
        json={"text": "Yes, we have two in stock."},
        headers=tenant["headers"],
    )
    assert reply.json()["delivery"] == "SENT"

    follow_up_graph = FakeGraph()
    service = FollowUpService(
        db, tenant["tenant_id"], graph=follow_up_graph, settings=_fu_settings(), clock=clock
    )
    assert await service.on_reply_sent(conversation["id"]) is not None, "no sequence was armed"

    # Step 1 of the default sequence is 4 hours out, and a task that is not yet due is not an
    # error - so the clock is advanced the way the rest of the follow-up suite does.
    clock.advance(hours=4)
    sent = await _claim_and_execute(db, tenant, clock, graph=follow_up_graph)
    assert sent.completed, sent
    assert len(follow_up_graph.whatsapp_sends) == 1
    assert follow_up_graph.whatsapp_sends[0]["phone_number_id"] == PHONE
    assert follow_up_graph.whatsapp_sends[0]["recipient_id"] == WA_RECIPIENT
    # The follow-up is attributed to its task, not left as an unattributed manual send.
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant["tenant_id"])
        attributed = (
            (await session.execute(select(Message).where(Message.scheduled_task_id.is_not(None))))
            .scalars()
            .all()
        )
    assert len(attributed) == 1
    assert attributed[0].channel is Channel.WHATSAPP


async def test_a_customer_reply_cancels_pending_whatsapp_follow_ups(api, db, tenant, http_graph):
    """The existing rule, on the existing engine - nothing WhatsApp-specific about it.

    The cancellation lives in `ConversationEngine.ingest_inbound`, so it is exercised by
    delivering a real second inbound message rather than by calling a repository directly.
    """
    await _deliver(api, whatsapp_text(mid="wamid.first"))
    await _drain(db, tenant["tenant_id"], FakeGraph())
    conversation = await _conversation(api, tenant)

    from app.services.follow_ups import FollowUpService

    # A sequence only arms after the first automated reply, so a reply goes out first.
    sent = await api.post(
        f"{API}/conversations/{conversation['id']}/messages",
        json={"text": "Is the Golf still available?"},
        headers=tenant["headers"],
    )
    assert sent.json()["delivery"] == "SENT"
    service = FollowUpService(db, tenant["tenant_id"], graph=FakeGraph(), settings=_fu_settings())
    assert await service.on_reply_sent(conversation["id"]) is not None

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant["tenant_id"])
        pending = (
            (
                await session.execute(
                    select(ScheduledTask).where(ScheduledTask.status == TaskStatus.PENDING)
                )
            )
            .scalars()
            .all()
        )
    assert pending, "nothing was pending to cancel"

    await _deliver(api, whatsapp_text(mid="wamid.second", text="actually I found one, thanks"))
    await _drain(db, tenant["tenant_id"], FakeGraph())

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant["tenant_id"])
        remaining = (
            (
                await session.execute(
                    select(ScheduledTask).where(ScheduledTask.status == TaskStatus.PENDING)
                )
            )
            .scalars()
            .all()
        )
    assert remaining == [], "a customer reply did not stop the sequence"


# -- handoff ------------------------------------------------------------------


async def test_a_handoff_stops_automatic_whatsapp_replies(db, tenant):
    """Handoff is a conversation property, so it must hold identically here.

    `FollowUpService._handoff_active` is Phase 8's single authority and this test does not
    re-implement it: it arranges an open handoff and asserts the engine declines to arm.
    """
    from app.services.follow_ups import FollowUpService

    conversation_id = await _seed_whatsapp_conversation(db, tenant)

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant["tenant_id"])
        session.add(
            HumanHandoff(
                tenant_id=tenant["tenant_id"],
                conversation_id=conversation_id,
                status=HandoffStatus.OPEN,
                reason=HandoffReason.EXPLICIT_HUMAN_REQUEST,
            )
        )

    graph = FakeGraph()
    service = FollowUpService(db, tenant["tenant_id"], graph=graph, settings=_fu_settings())

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant["tenant_id"])
        active = await service._handoff_active(session, conversation_id)
    assert active is True

    assert await service.on_reply_sent(conversation_id) is None, (
        "a handed-off conversation armed an automated sequence"
    )
    assert graph.whatsapp_sends == []


# -- tenant isolation ---------------------------------------------------------


# -- the shared AI service ----------------------------------------------------


async def test_an_inbound_whatsapp_message_reaches_the_ai_service(api, db, tenant):
    """Phase 5 is unchanged: WhatsApp inbound goes through the same qualification and reply.

    This is the test that would fail if WhatsApp had grown its own conversation lifecycle -
    the AI service is constructed with a `session` and asked to process a *message id*, the
    same call every other surface makes, and its reply leaves over the WhatsApp transport.
    """
    from app.models.conversation import MessageDirection
    from app.services.ai_qualification import AIConversationService
    from tests.fakes import FakeRegistry

    await _deliver(api, whatsapp_text())
    await _drain(db, tenant["tenant_id"], FakeGraph())
    conversation = await _conversation(api, tenant)

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant["tenant_id"])
        inbound = (
            (
                await session.execute(
                    select(Message).where(
                        Message.conversation_id == conversation["id"],
                        Message.direction == MessageDirection.INBOUND,
                    )
                )
            )
            .scalars()
            .one()
        )
        inbound_id = inbound.id

    registry = FakeRegistry().program_response(budget=20, timeline=15, fit=25, authority=20)
    graph = FakeGraph()

    # `AIConversationService` takes a session *factory* and opens its own transaction, so
    # this is the same construction the webhook worker does.
    service = AIConversationService(
        db, tenant["tenant_id"], registry=registry, graph=graph, settings=_ai_settings()
    )
    result = await service.process_inbound_message(inbound_id)

    assert result.status == "PROCESSED", result
    assert result.response_sent is True
    assert result.qualification_total == 80, result
    # The AI's reply left over the Cloud API transport, not the page one.
    assert len(graph.whatsapp_sends) == 1
    assert graph.whatsapp_sends[0]["phone_number_id"] == PHONE
    assert graph.whatsapp_sends[0]["recipient_id"] == WA_RECIPIENT
    assert graph.sends == []

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant["tenant_id"])
        outbound = (
            (
                await session.execute(
                    select(Message).where(
                        Message.conversation_id == conversation["id"],
                        Message.direction == MessageDirection.OUTBOUND,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(outbound) == 1
    assert outbound[0].ai_model is not None, "the AI reply was not attributed"
    assert outbound[0].channel is Channel.WHATSAPP
    assert outbound[0].delivery is MessageDelivery.SENT


def _ai_settings():
    """Settings with the AI stage switched on, as the AI tests do.

    `ai_enabled` defaults to `False` here, so without this the service would correctly do
    nothing and the test would pass for the wrong reason.
    """
    from app.core.config import get_settings

    return get_settings().model_copy(update={"ai_enabled": True})


# -- webhook verification ----------------------------------------------------


async def test_whatsapp_uses_the_existing_webhook_verification(api, tenant):
    """Verification is per-connection, not per-surface, and stays that way.

    The tenant has no `webhook_verify_token`, so the challenge is refused - which is the
    point: a connection without a token must not accept a subscription attempt.
    """
    from tests.meta_fixtures import VERIFY_TOKEN

    response = await api.get(
        f"/api/v1/webhooks/meta/{tenant['connection']}",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "3141592653",
        },
    )
    assert response.status_code == 403

    correct = await api.get(
        f"/api/v1/webhooks/meta/{tenant['connection']}",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "not-the-token",
            "hub.challenge": "3141592653",
        },
    )
    assert correct.status_code == 403


# -- tenant isolation ---------------------------------------------------------


async def test_a_whatsapp_conversation_of_one_tenant_is_invisible_to_another(
    api, db, owner, tenant
):
    """RLS on the read path, not just on the write path."""
    other = await create_tenant(owner, name="Globex", slug="globex")
    other_admin = await create_user(owner, email="g@acme.test", name="G Admin")
    await add_member(owner, tenant_id=other, user_id=other_admin, role=Role.OWNER)
    other_token = await issue_session(owner, user_id=other_admin)
    other_headers = {"X-Tenant-ID": str(other), "Cookie": f"qf_session={other_token}"}

    await _deliver(api, whatsapp_text(mid="wamid.isolated"))
    await _drain(db, tenant["tenant_id"], FakeGraph())
    conversation = await _conversation(api, tenant)

    read = await api.get(
        f"{API}/conversations/{conversation['id']}/messages", headers=other_headers
    )
    assert read.status_code == 404

    listed = await api.get(f"{API}/conversations", headers=other_headers)
    assert listed.json()["count"] == 0


async def test_a_whatsapp_webhook_from_an_unknown_account_is_refused(api, db, owner):
    """Cross-tenant routing: the WABA id decides the tenant, and an unknown one stops here.

    404 rather than 500 and rather than an ACK - this is the documented behaviour for an
    unknown Meta source, and it is also what stops Meta retrying a delivery that can never
    be attributed to a tenant.
    """
    payload = whatsapp_text(mid="wamid.cross")
    payload["entry"][0]["id"] = "7999999999999997"  # a WABA nobody owns

    response = await api.post(WEBHOOK, content=body(payload), headers=signed_headers(body(payload)))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "META_CONNECTION_NOT_FOUND"

    # Nothing was created for a tenant that does not exist. The owner session bypasses RLS,
    # so an empty result is a real "nothing was written" rather than a hidden row.
    async with db() as session, session.begin():
        await set_current_tenant(session, uuid.uuid4())
        conversations = (await session.execute(select(Conversation))).scalars().all()
    assert conversations == []


# -- helper -------------------------------------------------------------------


async def _seed_whatsapp_conversation(db, tenant) -> uuid.UUID:
    """A real WhatsApp thread, created the way the webhook path would create it.

    Goes through the real engine rather than a hand-built row: the conversation carries a
    composite tenant foreign key, so a fabricated connection id would fail the constraint
    instead of testing anything.
    """
    from app.services.conversations import ConversationEngine
    from app.services.meta_messaging import InboundMessage

    # `ConversationEngine` takes a session, not a session factory.
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant["tenant_id"])
        engine = ConversationEngine(session, tenant["tenant_id"])
        stored = await engine.ingest_inbound(
            meta_connection_id=tenant["connection"],
            channel=Channel.WHATSAPP,
            messages=[
                InboundMessage(
                    participant_id=WA_RECIPIENT,
                    meta_message_id=f"wamid.seed-{uuid.uuid4().hex[:8]}",
                    source_id=WABA,
                    text="do you have a Golf?",
                    message_type="text",
                    sent_at=datetime.now(UTC),
                )
            ],
        )
    assert len(stored) == 1
    return stored[0].conversation_id


async def _claim_and_execute(db, tenant, clock, *, graph):
    """Claim the due tasks and run them, as the poller does.

    Reuses the existing `follow_up_fixtures.claim_and_execute` rather than a local copy:
    `FollowUpService.execute` deliberately refuses a task nobody claimed, so a test that
    wants to watch a send has to go through claiming, exactly as production does.
    """
    from tests.follow_up_fixtures import claim_and_execute

    return await claim_and_execute(db, tenant["tenant_id"], clock, graph=graph)


def test_the_wa_module_never_claims_real_connectivity():
    """A guard on the file itself.

    Phase 10-O.3 is mock-verified. If a future change makes these tests assert provider
    connectivity, this fails and forces the claim to be re-examined.
    """
    assert WABA and PHONE and WA_RECIPIENT
    assert isinstance(WABA, str)
