"""The conversation engine against a real database.

The database is never mocked, because the properties that matter here *are* database
behaviour: the unique index deciding that one person has one thread, the `mid` index
deciding that a retried delivery is one message, and RLS deciding whose messages these
are. Only the outbound HTTP call is faked.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.crypto import encrypt
from app.db.tenant_context import set_current_tenant
from app.models.conversation import (
    Channel,
    Conversation,
    Message,
    MessageDelivery,
    MessageDirection,
)
from app.models.meta_connection import MetaConnection, MetaConnectionStatus
from app.repositories import conversations as conversation_repo
from app.services import conversations as engine
from app.services.conversations import ConversationEngine, ConversationNotSendable
from app.services.meta_graph import (
    MetaApiError,
    MetaAuthError,
    MetaMessageWindowClosed,
    MetaTransientError,
)
from app.services.meta_messaging import InboundMessage
from tests.conftest import TEST_META_TOKEN_KEY
from tests.fakes import PAGE_TOKEN

PAGE = "1000000000000001"
IG = "3000000000000003"


@pytest.fixture
async def world(owner, db):
    acme = await _tenant(owner, "acme")
    globex = await _tenant(owner, "globex")
    conn = await _connection(owner, acme, PAGE, with_token=True)
    return {"acme": acme, "globex": globex, "conn": conn}


async def _tenant(factory, slug: str) -> uuid.UUID:
    from tests.factories import create_tenant

    return await create_tenant(factory, name=slug.title(), slug=slug)


async def _connection(
    factory, tenant_id, page_id, *, with_token: bool, token: str = PAGE_TOKEN
) -> uuid.UUID:
    async with factory() as session, session.begin():
        connection = MetaConnection(
            tenant_id=tenant_id,
            page_id=page_id,
            status=MetaConnectionStatus.ACTIVE,
        )
        if with_token:
            connection.access_token_encrypted = encrypt(token, key=TEST_META_TOKEN_KEY)
        session.add(connection)
        await session.flush()
        return connection.id


def inbound(
    *,
    mid: str = "mid.1",
    participant: str = "PSID-1",
    text: str = "hello",
    at: datetime | None = None,
    ad_id: str | None = None,
    message_type: str = "text",
) -> InboundMessage:
    return InboundMessage(
        participant_id=participant,
        meta_message_id=mid,
        source_id=PAGE,
        text=text,
        message_type=message_type,
        referral_ad_id=ad_id,
        sent_at=at or datetime.now(UTC),
    )


class FakeGraph:
    """Records sends; can be told to fail a specific way."""

    def __init__(self) -> None:
        self.sends: list[dict] = []
        self.error: Exception | None = None
        self.next_message_id = "m_ag-sent-1"

    async def send_message(self, **kwargs) -> str:
        self.sends.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.next_message_id

    async def aclose(self) -> None:
        return None


# -- inbound ------------------------------------------------------------------


async def test_a_message_creates_a_thread_and_a_message(db, world):
    async with db() as session, session.begin():
        engine_ = ConversationEngine(session, world["acme"])
        await engine_.ingest_inbound(
            meta_connection_id=world["conn"],
            channel=Channel.MESSENGER,
            messages=[inbound(text="hi there")],
        )

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        rows = (await session.execute(select(Conversation))).scalars().all()
        messages = (await session.execute(select(Message))).scalars().all()

    assert len(rows) == 1
    assert rows[0].channel is Channel.MESSENGER
    assert rows[0].meta_participant_id == "PSID-1"
    assert rows[0].meta_source_id == PAGE
    assert rows[0].tenant_id == world["acme"]
    assert rows[0].lead_id is None, "Phase 4 does not guess the lead"
    assert len(messages) == 1
    assert messages[0].text == "hi there"
    assert messages[0].direction is MessageDirection.INBOUND
    assert messages[0].channel is Channel.MESSENGER
    assert messages[0].delivery is MessageDelivery.SENT


async def test_a_second_message_reuses_the_thread(db, world):
    async with db() as session, session.begin():
        engine_ = ConversationEngine(session, world["acme"])
        for mid in ("mid.1", "mid.2"):
            await engine_.ingest_inbound(
                meta_connection_id=world["conn"],
                channel=Channel.MESSENGER,
                messages=[inbound(mid=mid)],
            )

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        threads = (await session.execute(select(Conversation))).scalars().all()
        messages = (await session.execute(select(Message))).scalars().all()

    assert len(threads) == 1
    assert len(messages) == 2


async def test_a_retried_delivery_does_not_duplicate_the_message(db, world):
    async with db() as session, session.begin():
        engine_ = ConversationEngine(session, world["acme"])
        await engine_.ingest_inbound(
            meta_connection_id=world["conn"],
            channel=Channel.MESSENGER,
            messages=[inbound(mid="mid.retry")],
        )
    async with db() as session, session.begin():
        engine_ = ConversationEngine(session, world["acme"])
        await engine_.ingest_inbound(
            meta_connection_id=world["conn"],
            channel=Channel.MESSENGER,
            messages=[inbound(mid="mid.retry")],
        )

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        messages = (await session.execute(select(Message))).scalars().all()
        threads = (await session.execute(select(Conversation))).scalars().all()

    assert len(messages) == 1
    assert len(threads) == 1


async def test_the_same_participant_on_two_channels_is_two_threads(db, world):
    """Messenger and Instagram are different surfaces; a shared id is not a shared thread."""
    async with db() as session, session.begin():
        engine_ = ConversationEngine(session, world["acme"])
        await engine_.ingest_inbound(
            meta_connection_id=world["conn"],
            channel=Channel.MESSENGER,
            messages=[inbound(participant="SAME", mid="mid.m")],
        )
        await engine_.ingest_inbound(
            meta_connection_id=world["conn"],
            channel=Channel.INSTAGRAM,
            messages=[inbound(participant="SAME", mid="mid.i")],
        )

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        threads = (await session.execute(select(Conversation))).scalars().all()
    assert {t.channel for t in threads} == {Channel.MESSENGER, Channel.INSTAGRAM}
    assert len(threads) == 2


async def test_two_people_make_two_threads(db, world):
    async with db() as session, session.begin():
        engine_ = ConversationEngine(session, world["acme"])
        await engine_.ingest_inbound(
            meta_connection_id=world["conn"],
            channel=Channel.MESSENGER,
            messages=[
                inbound(mid="mid.a", participant="PSID-A"),
                inbound(mid="mid.b", participant="PSID-B"),
            ],
        )

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        threads = (await session.execute(select(Conversation))).scalars().all()
    assert len(threads) == 2


async def test_a_referral_is_recorded_on_the_thread(db, world):
    async with db() as session, session.begin():
        engine_ = ConversationEngine(session, world["acme"])
        await engine_.ingest_inbound(
            meta_connection_id=world["conn"],
            channel=Channel.MESSENGER,
            messages=[inbound(mid="mid.ads", ad_id="AD-77")],
        )

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        thread = (await session.execute(select(Conversation))).scalar_one()
    assert thread.meta_referral_ad_id == "AD-77"


async def test_a_batch_of_messages_is_stored(db, world):
    async with db() as session, session.begin():
        engine_ = ConversationEngine(session, world["acme"])
        await engine_.ingest_inbound(
            meta_connection_id=world["conn"],
            channel=Channel.MESSENGER,
            messages=[inbound(mid="mid.1"), inbound(mid="mid.2", participant="PSID-2")],
        )

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        assert len((await session.execute(select(Message))).scalars().all()) == 2


async def test_the_thread_recency_and_window_follow_the_last_inbound(db, world):
    at = datetime.now(UTC) - timedelta(hours=2)
    async with db() as session, session.begin():
        engine_ = ConversationEngine(session, world["acme"])
        await engine_.ingest_inbound(
            meta_connection_id=world["conn"],
            channel=Channel.MESSENGER,
            messages=[inbound(at=at)],
        )

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        thread = (await session.execute(select(Conversation))).scalar_one()

    assert thread.last_inbound_at == at
    assert thread.last_message_at == at
    engine_ = ConversationEngine(db(), world["acme"])
    assert engine_.messaging_window_expires_at(thread) == at + timedelta(hours=24)
    assert engine_.is_within_messaging_window(thread) is True


async def test_concurrent_first_messages_from_one_person_create_one_thread(db, world):
    """The unique index is the only thing that can settle this."""
    results = await asyncio.gather(
        *(
            _ingest_one(db, world["acme"], world["conn"], mid)
            for mid in (f"mid.{i}" for i in range(5))
        ),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, failures

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        threads = (await session.execute(select(Conversation))).scalars().all()
        messages = (await session.execute(select(Message))).scalars().all()

    assert len(threads) == 1
    assert len(messages) == 5


async def _ingest_one(factory, tenant_id, connection_id, mid):
    async with factory() as session, session.begin():
        engine_ = ConversationEngine(session, tenant_id)
        await engine_.ingest_inbound(
            meta_connection_id=connection_id,
            channel=Channel.MESSENGER,
            messages=[inbound(mid=mid)],
        )


async def test_concurrent_identical_mids_still_produce_one_message(db, world):
    results = await asyncio.gather(
        *(_ingest_one(db, world["acme"], world["conn"], "mid.same") for _ in range(5)),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, failures

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        assert len((await session.execute(select(Message))).scalars().all()) == 1
        assert len((await session.execute(select(Conversation))).scalars().all()) == 1


# -- outbound -----------------------------------------------------------------


async def _thread_with_message(db, world, *, channel=Channel.MESSENGER) -> uuid.UUID:
    async with db() as session, session.begin():
        engine_ = ConversationEngine(session, world["acme"])
        await engine_.ingest_inbound(
            meta_connection_id=world["conn"],
            channel=channel,
            messages=[inbound()],
        )
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        return (await session.execute(select(Conversation))).scalar_one().id


async def _load_thread(db, tenant_id, conversation_id) -> Conversation:
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        return await conversation_repo.get(session, conversation_id)


async def test_sending_a_message_calls_meta_and_records_it_as_sent(db, world):
    conversation_id = await _thread_with_message(db, world)
    graph = FakeGraph()

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        thread = await conversation_repo.get(session, conversation_id)
        engine_ = ConversationEngine(session, world["acme"], graph=graph)
        message = await engine_.send_outbound(thread, text="thanks for getting in touch")

    assert message.delivery is MessageDelivery.SENT
    assert message.meta_message_id == "m_ag-sent-1"
    assert message.direction is MessageDirection.OUTBOUND
    assert message.text == "thanks for getting in touch"
    assert message.failure_reason is None
    assert message.delivered_at is not None

    assert len(graph.sends) == 1
    send = graph.sends[0]
    assert send["page_id"] == PAGE
    assert send["recipient_id"] == "PSID-1"
    assert send["access_token"] == PAGE_TOKEN
    assert send["messaging_type"] == "RESPONSE"


async def test_the_token_is_decrypted_only_for_the_send(db, world):
    conversation_id = await _thread_with_message(db, world)
    graph = FakeGraph()
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        thread = await conversation_repo.get(session, conversation_id)
        await ConversationEngine(session, world["acme"], graph=graph).send_outbound(
            thread, text="hi"
        )
    assert graph.sends[0]["access_token"] == PAGE_TOKEN
    assert "v1." not in graph.sends[0]["access_token"]


async def test_an_outbound_message_never_advances_the_messaging_window(db, world):
    """Answering inside the window must not extend it, or we would answer forever."""
    at = datetime.now(UTC) - timedelta(hours=23)
    async with db() as session, session.begin():
        engine_ = ConversationEngine(session, world["acme"])
        await engine_.ingest_inbound(
            meta_connection_id=world["conn"],
            channel=Channel.MESSENGER,
            messages=[inbound(at=at)],
        )
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        conversation_id = (await session.execute(select(Conversation))).scalar_one().id

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        thread = await conversation_repo.get(session, conversation_id)
        await ConversationEngine(session, world["acme"], graph=FakeGraph()).send_outbound(
            thread, text="still here"
        )

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        refreshed = await conversation_repo.get(session, conversation_id)
    assert refreshed.last_inbound_at == at
    assert refreshed.last_message_at > at, "but the thread did move"


async def test_an_outbound_message_moves_thread_recency(db, world):
    conversation_id = await _thread_with_message(db, world)
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        thread = await conversation_repo.get(session, conversation_id)
        before = thread.last_message_at
        await ConversationEngine(session, world["acme"], graph=FakeGraph()).send_outbound(
            thread, text="hello"
        )
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        refreshed = await conversation_repo.get(session, conversation_id)
    assert refreshed.last_message_at > before


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (MetaMessageWindowClosed("closed"), engine.REASON_WINDOW_CLOSED),
        (MetaAuthError("bad token"), engine.REASON_CREDENTIAL),
        (MetaTransientError("rate limited"), engine.REASON_META_UNAVAILABLE),
        (MetaApiError("nope"), engine.REASON_REJECTED),
    ],
)
async def test_a_failed_send_is_recorded_not_raised(db, world, error, reason):
    """The attempt is always a row; the outcome is the `delivery` and `failure_reason`."""
    conversation_id = await _thread_with_message(db, world)
    graph = FakeGraph()
    graph.error = error

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        thread = await conversation_repo.get(session, conversation_id)
        message = await ConversationEngine(session, world["acme"], graph=graph).send_outbound(
            thread, text="hello"
        )

    assert message.delivery is MessageDelivery.FAILED
    assert message.failure_reason == reason
    assert message.meta_message_id is None


async def test_a_transient_send_failure_stays_reported_as_unavailable(db, world):
    conversation_id = await _thread_with_message(db, world)
    graph = FakeGraph()
    graph.error = MetaTransientError("rate limited")
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        thread = await conversation_repo.get(session, conversation_id)
        message = await ConversationEngine(session, world["acme"], graph=graph).send_outbound(
            thread, text="hello"
        )
    assert message.failure_reason == engine.REASON_META_UNAVAILABLE
    # A retry is a normal operation, not an error path.
    graph.error = None
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        thread = await conversation_repo.get(session, conversation_id)
        retried = await ConversationEngine(session, world["acme"], graph=graph).send_outbound(
            thread, text="hello again"
        )
    assert retried.delivery is MessageDelivery.SENT


async def test_empty_text_never_reaches_meta(db, world):
    conversation_id = await _thread_with_message(db, world)
    graph = FakeGraph()
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        thread = await conversation_repo.get(session, conversation_id)
        with pytest.raises(ConversationNotSendable) as caught:
            await ConversationEngine(session, world["acme"], graph=graph).send_outbound(
                thread, text="   "
            )
    assert caught.value.reason == engine.REASON_UNSENDABLE
    assert graph.sends == []
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        assert (await session.execute(select(Message))).scalars().all(), "no row for a bad send"


async def test_sending_without_a_credential_is_refused_locally(db, owner, world):
    conn = await _connection(owner, world["acme"], "999", with_token=False)
    async with db() as session, session.begin():
        engine_ = ConversationEngine(session, world["acme"])
        await engine_.ingest_inbound(
            meta_connection_id=conn,
            channel=Channel.MESSENGER,
            messages=[inbound(participant="PSID-NOCRED")],
        )
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        thread = (
            await session.execute(
                select(Conversation).where(Conversation.meta_participant_id == "PSID-NOCRED")
            )
        ).scalar_one()
        with pytest.raises(ConversationNotSendable) as caught:
            await ConversationEngine(session, world["acme"], graph=FakeGraph()).send_outbound(
                thread, text="hi"
            )
    assert caught.value.reason == engine.REASON_CREDENTIAL


async def test_sending_on_a_disconnected_connection_is_refused(db, owner, world):
    from sqlalchemy import update as sa_update

    conversation_id = await _thread_with_message(db, world)
    async with owner() as session, session.begin():
        await session.execute(
            sa_update(MetaConnection)
            .where(MetaConnection.id == world["conn"])
            .values(status=MetaConnectionStatus.INACTIVE)
        )
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        thread = await conversation_repo.get(session, conversation_id)
        with pytest.raises(ConversationNotSendable) as caught:
            await ConversationEngine(session, world["acme"], graph=FakeGraph()).send_outbound(
                thread, text="hi"
            )
    assert caught.value.reason == engine.REASON_CREDENTIAL


async def test_a_worker_without_a_graph_client_fails_loudly(db, world):
    conversation_id = await _thread_with_message(db, world)
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        thread = await conversation_repo.get(session, conversation_id)
        with pytest.raises(RuntimeError, match="MetaGraphClient"):
            await ConversationEngine(session, world["acme"]).send_outbound(thread, text="hi")


# -- tenant isolation ---------------------------------------------------------


async def test_conversations_are_isolated_by_tenant(db, world):
    conversation_id = await _thread_with_message(db, world)
    async with db() as session, session.begin():
        await set_current_tenant(session, world["globex"])
        assert await conversation_repo.get(session, conversation_id) is None
        assert (await session.execute(select(Conversation))).scalars().all() == []
        assert (await session.execute(select(Message))).scalars().all() == []


async def test_a_message_cannot_be_written_into_another_tenants_thread(db, world):
    """The cross-tenant hole this phase found and closed.

    RLS checks the *child* row's `tenant_id`, so an insert that correctly carries
    Globex's own tenant id passes every policy - while pointing `conversation_id` at
    Acme's thread. Migration 0005 replaced the single-column foreign key with a composite
    one so the database itself refuses the mismatch. This test is that refusal.
    """
    from sqlalchemy.exc import IntegrityError

    await _thread_with_message(db, world)
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        acme_thread = (await session.execute(select(Conversation))).scalar_one()

    with pytest.raises(IntegrityError):
        async with db() as session, session.begin():
            await set_current_tenant(session, world["globex"])
            await conversation_repo.insert_inbound_if_new(
                session,
                tenant_id=world["globex"],
                conversation_id=acme_thread.id,
                channel=Channel.MESSENGER,
                meta_message_id="mid.cross",
                message_type="text",
                text="planted",
                attachments=[],
                sent_at=datetime.now(UTC),
            )


async def test_a_conversation_cannot_reference_another_tenants_lead(db, world, owner):
    """The same guarantee one level up, on the `conversations -> leads` reference."""
    from sqlalchemy.exc import IntegrityError

    from app.models.lead import Lead

    async with owner() as session, session.begin():
        lead = Lead(tenant_id=world["acme"], meta_leadgen_id="7000000000000007")
        session.add(lead)
        await session.flush()
        acme_lead_id = lead.id

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        conversation = Conversation(
            tenant_id=world["acme"],
            meta_connection_id=world["conn"],
            channel=Channel.MESSENGER,
            meta_participant_id="PSID-X",
            lead_id=acme_lead_id,
        )
        session.add(conversation)
        await session.flush()

    with pytest.raises(IntegrityError):
        async with db() as session, session.begin():
            await set_current_tenant(session, world["globex"])
            session.add(
                Conversation(
                    tenant_id=world["globex"],
                    meta_connection_id=world["conn"],
                    channel=Channel.MESSENGER,
                    meta_participant_id="PSID-Y",
                    # A lead that belongs to Acme.
                    lead_id=acme_lead_id,
                )
            )
            await session.flush()


async def test_one_tenant_cannot_see_another_tenants_thread_from_the_list(db, world):
    await _thread_with_message(db, world)
    async with db() as session, session.begin():
        await set_current_tenant(session, world["globex"])
        assert await conversation_repo.list_conversations(session) == []
        assert await conversation_repo.count_conversations(session) == 0


async def test_message_channel_always_agrees_with_its_conversation(db, world):
    """The denormalized column is only useful if it cannot drift."""
    async with db() as session, session.begin():
        engine_ = ConversationEngine(session, world["acme"])
        await engine_.ingest_inbound(
            meta_connection_id=world["conn"],
            channel=Channel.INSTAGRAM,
            messages=[inbound(participant="IG-1", mid="mid.ig")],
        )
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        message = (await session.execute(select(Message))).scalar_one()
        thread = await conversation_repo.get(session, message.conversation_id)
    assert message.channel is thread.channel is Channel.INSTAGRAM


# -- list ordering ------------------------------------------------------------


async def test_conversations_are_listed_most_recently_active_first(db, world):
    now = datetime.now(UTC)
    for index, offset in enumerate((timedelta(hours=3), timedelta(hours=1), timedelta(hours=2))):
        async with db() as session, session.begin():
            await ConversationEngine(session, world["acme"]).ingest_inbound(
                meta_connection_id=world["conn"],
                channel=Channel.MESSENGER,
                messages=[inbound(mid=f"mid.{index}", at=now - offset)],
            )
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        rows = await conversation_repo.list_conversations(session)
    conversations = [row for row, _name in rows]
    assert [r.last_message_at for r in conversations] == sorted(
        (r.last_message_at for r in conversations), reverse=True
    )
