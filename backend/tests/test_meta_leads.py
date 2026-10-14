"""Lead Ads ingestion: from a durable event to a tenant-scoped Lead.

The database is real; only the Meta HTTP boundary is faked. The properties that matter
here cannot be proven with a mocked database at all, because they *are* database
behaviour: the unique constraint doing the deduplication, RLS deciding whose leads these
are, and `SKIP LOCKED` deciding that two workers never take one event.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.crypto import decrypt, encrypt
from app.db.tenant_context import set_current_tenant
from app.models.lead import Lead, LeadStatus
from app.models.membership import Role
from app.models.meta_connection import MetaConnection, MetaConnectionStatus
from app.models.meta_webhook_event import MetaWebhookEvent, MetaWebhookEventStatus
from app.repositories import leads as lead_repo
from app.repositories import meta_webhook_events as event_repo
from app.services.meta_graph import MetaAuthError
from app.services.meta_leads import (
    InvalidLeadPayload,
    extract_leadgen_events,
    normalize_lead,
)
from app.services.meta_processor import MetaWebhookEventProcessor
from tests.conftest import TEST_META_TOKEN_KEY
from tests.factories import add_member, create_meta_connection, create_tenant, create_user
from tests.fakes import PAGE_TOKEN, transient_error
from tests.meta_fixtures import (
    AD_A,
    FORM_A,
    LEADGEN_A,
    PAGE_A,
    body,
    lead_graph_payload,
    leadgen_payload,
    multi_leadgen_payload,
    page_post_payload,
    signed_headers,
)


@pytest.fixture
async def world(owner, db):
    """Two tenants, each with an authorized (token-bearing) Meta connection."""
    acme = await create_tenant(owner, name="Acme", slug="acme")
    globex = await create_tenant(owner, name="Globex", slug="globex")
    admin = await create_user(owner, email="admin@acme.test")
    await add_member(owner, tenant_id=acme, user_id=admin, role=Role.OWNER)

    acme_conn = await create_authorized_connection(owner, acme, PAGE_A)
    globex_conn = await create_authorized_connection(owner, globex, "2000000000000002")
    return {
        "acme": acme,
        "globex": globex,
        "user_id": admin,
        "acme_conn": acme_conn,
        "globex_conn": globex_conn,
    }


async def create_authorized_connection(
    factory, tenant_id, page_id, *, token=PAGE_TOKEN
) -> uuid.UUID:
    """A connection created through the owner role, as OAuth would leave it."""
    async with factory() as session, session.begin():
        connection = MetaConnection(
            tenant_id=tenant_id,
            page_id=page_id,
            access_token_encrypted=encrypt(token, key=TEST_META_TOKEN_KEY),
            app_id="1234567890",
            scopes=["leads_retrieval", "ads_management"],
            connected_at=datetime.now(UTC),
            status=MetaConnectionStatus.ACTIVE,
        )
        session.add(connection)
        await session.flush()
        return connection.id


async def _store_event(db, tenant_id, connection_id, payload, *, status=None):
    """Persist an event the way `ingest()` does, so the processor sees a real row."""
    from app.services.meta_webhook import compute_deduplication_key, describe_event

    object_type, event_type, meta_event_id = describe_event(payload)
    key = compute_deduplication_key(payload, meta_event_id)
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        event, _ = await event_repo.insert_if_new(
            session,
            tenant_id=tenant_id,
            meta_connection_id=connection_id,
            deduplication_key=key,
            meta_event_id=meta_event_id,
            object_type=object_type,
            event_type=event_type,
            source_identifier=PAGE_A,
            payload=payload,
            headers={},
            received_at=datetime.now(UTC),
        )
        if status is not None:
            event.status = status
            await session.flush()
    return event.id


async def _process_once(db, tenant_id, fake_graph, settings=None):
    """Claim one event and handle it, in two transactions like a real worker."""
    from app.core.config import get_settings

    settings = settings or get_settings()
    async with db() as session, session.begin():
        processor = MetaWebhookEventProcessor(
            session, tenant_id, graph=fake_graph, settings=settings
        )
        event = await processor.claim_next()
    if event is None:
        return None, None
    async with db() as session, session.begin():
        processor = MetaWebhookEventProcessor(
            session, tenant_id, graph=fake_graph, settings=settings
        )
        await processor.handle(event)
    return event.id, event


async def _lead_rows(db, tenant_id):
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        rows = (await session.execute(select(Lead).order_by(Lead.meta_leadgen_id))).scalars().all()
        return [
            {
                "id": r.id,
                "leadgen_id": r.meta_leadgen_id,
                "name": r.name,
                "email": r.email,
                "phone": r.phone,
                "form_id": r.form_id,
                "ad_id": r.ad_id,
                "field_data": r.field_data,
                "status": r.status,
                "connection": r.meta_connection_id,
                "tenant_id": r.tenant_id,
            }
            for r in rows
        ]


# -- recognizing a leadgen event ---------------------------------------------


def test_a_real_leadgen_webhook_yields_its_leadgen_id():
    """Meta documents `entry[].changes[]` for Page leadgen notifications."""
    references = extract_leadgen_events(leadgen_payload(PAGE_A, LEADGEN_A))
    assert len(references) == 1
    assert references[0].leadgen_id == LEADGEN_A
    assert references[0].form_id == FORM_A
    assert references[0].ad_id == AD_A
    assert references[0].page_id == PAGE_A


def test_one_delivery_carrying_two_leads_yields_two_references():
    """Meta batches leads into a single delivery; keying on one id would drop the other."""
    references = extract_leadgen_events(multi_leadgen_payload(PAGE_A))
    assert [r.leadgen_id for r in references] == [
        LEADGEN_A,
        "5000000000000009",
    ]


@pytest.mark.parametrize(
    "payload",
    [page_post_payload(), {"object": "page", "entry": []}, {}, {"entry": [{"id": "1"}]}],
)
def test_unrelated_events_are_not_lead_events(payload):
    assert extract_leadgen_events(payload) == []


def test_a_leadgen_change_without_an_id_is_skipped_not_fatal():
    payload = {
        "object": "page",
        "entry": [
            {
                "id": PAGE_A,
                "changes": [
                    {"field": "leadgen", "value": {"form_id": FORM_A}},
                    {"field": "leadgen", "value": {"leadgen_id": LEADGEN_A}},
                ],
            }
        ],
    }
    references = extract_leadgen_events(payload)
    assert [r.leadgen_id for r in references] == [LEADGEN_A]


# -- normalization ------------------------------------------------------------


def test_graph_fields_are_normalized_into_contact_columns():
    reference = extract_leadgen_events(leadgen_payload())[0]
    lead = normalize_lead(lead_graph_payload(), reference=reference)

    assert lead.leadgen_id == LEADGEN_A
    assert lead.name == "Ada Lovelace"
    assert lead.email == "ada@ex.test"
    assert lead.phone == "+15550100"
    assert lead.form_id == FORM_A
    assert lead.ad_id == AD_A
    assert lead.field_data["car_make"] == "Honda"
    assert lead.provider_created_at is not None


def test_a_missing_id_is_an_invalid_payload():
    reference = extract_leadgen_events(leadgen_payload())[0]
    with pytest.raises(InvalidLeadPayload, match="no id"):
        normalize_lead({"field_data": []}, reference=reference)


def test_a_non_object_response_is_invalid():
    reference = extract_leadgen_events(leadgen_payload())[0]
    with pytest.raises(InvalidLeadPayload, match="not an object"):
        normalize_lead(["not", "an", "object"], reference=reference)


def test_metas_offset_time_format_is_parsed():
    reference = extract_leadgen_events(leadgen_payload())[0]
    lead = normalize_lead(lead_graph_payload(), reference=reference)
    # "+0000" is not ISO-8601 as fromisoformat reads it, so this would silently be None.
    assert lead.provider_created_at == datetime(2015, 2, 28, 8, 49, 14, tzinfo=UTC)


def test_alternate_field_names_still_reach_name_email_and_phone():
    reference = extract_leadgen_events(leadgen_payload())[0]
    lead = normalize_lead(
        {
            "id": LEADGEN_A,
            "field_data": [
                {"name": "first_name", "values": ["Ada"]},
                {"name": "email_address", "values": ["a@b.test"]},
                {"name": "phone", "values": ["555"]},
            ],
        },
        reference=reference,
    )
    assert lead.name == "Ada"
    assert lead.email == "a@b.test"
    assert lead.phone == "555"


def test_a_typed_custom_question_answer_is_flattened():
    reference = extract_leadgen_events(leadgen_payload())[0]
    lead = normalize_lead(
        {
            "id": LEADGEN_A,
            "field_data": [
                {"name": "budget", "values": [{"text": "10000"}]},
                {"name": "blank", "values": []},
            ],
        },
        reference=reference,
    )
    assert lead.field_data["budget"] == "10000"
    assert "blank" not in lead.field_data


# -- the vertical slice -------------------------------------------------------


async def test_a_leadgen_event_becomes_a_tenant_scoped_lead(db, world, fake_graph):
    payload = leadgen_payload(PAGE_A, LEADGEN_A)
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload(LEADGEN_A))
    event_id = await _store_event(db, world["acme"], world["acme_conn"], payload)

    processed, _ = await _process_once(db, world["acme"], fake_graph)
    assert processed == event_id

    leads = await _lead_rows(db, world["acme"])
    assert len(leads) == 1
    lead = leads[0]
    assert lead["leadgen_id"] == LEADGEN_A
    assert lead["name"] == "Ada Lovelace"
    assert lead["email"] == "ada@ex.test"
    assert lead["phone"] == "+15550100"
    assert lead["status"] is LeadStatus.NEW
    # The tenant and the connection both come from resolution, never from the payload.
    assert lead["tenant_id"] == world["acme"]
    assert lead["connection"] == world["acme_conn"]


async def test_the_graph_call_receives_the_decrypted_token(db, world, fake_graph):
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload())
    await _store_event(db, world["acme"], world["acme_conn"], leadgen_payload())
    await _process_once(db, world["acme"], fake_graph)

    assert fake_graph.lead_calls, "the Graph API should have been called"
    assert fake_graph.tokens_seen_by_lead_retrieval == {PAGE_TOKEN}
    assert "v1." not in next(iter(fake_graph.tokens_seen_by_lead_retrieval))
    # The raw user token from the OAuth exchange is never used for lead retrieval.
    assert fake_graph.accounts_calls == []


async def test_the_token_is_encrypted_at_rest(db, world, fake_graph, owner):
    """The value in the database must not be the token, and must decrypt back to it."""
    async with owner() as session, session.begin():
        connection = await session.get(MetaConnection, world["acme_conn"])
        stored = connection.access_token_encrypted

    assert stored is not None
    assert PAGE_TOKEN not in stored
    assert decrypt(stored, key=TEST_META_TOKEN_KEY) == PAGE_TOKEN

    # And the encrypted value is what makes ingestion work at all.
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload())
    await _store_event(db, world["acme"], world["acme_conn"], leadgen_payload())
    await _process_once(db, world["acme"], fake_graph)
    assert (await _lead_rows(db, world["acme"]))[0]["leadgen_id"] == LEADGEN_A


async def test_the_event_is_marked_processed_after_a_successful_ingest(db, world, fake_graph):
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload())
    event_id = await _store_event(db, world["acme"], world["acme_conn"], leadgen_payload())
    await _process_once(db, world["acme"], fake_graph)

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        event = await event_repo.get(session, event_id)
    assert event.status.value == "PROCESSED"
    assert event.processed_at is not None


async def test_a_batched_delivery_creates_every_lead(db, world, fake_graph):
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload(LEADGEN_A))
    fake_graph.program_lead(
        "5000000000000009", lead_graph_payload("5000000000000009", name="Grace Hopper")
    )
    await _store_event(db, world["acme"], world["acme_conn"], multi_leadgen_payload())
    await _process_once(db, world["acme"], fake_graph)

    leads = await _lead_rows(db, world["acme"])
    assert [lead["leadgen_id"] for lead in leads] == [LEADGEN_A, "5000000000000009"]
    assert [lead["name"] for lead in leads] == ["Ada Lovelace", "Grace Hopper"]


# -- deduplication ------------------------------------------------------------


async def test_a_second_event_carrying_the_same_lead_does_not_duplicate(db, world, fake_graph):
    """A retry or a manual replay of the lead must converge on one row.

    Two genuinely distinct events (different delivery, so a different dedupe key) that
    name the same lead.
    """
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload())
    await _store_event(db, world["acme"], world["acme_conn"], leadgen_payload())
    await _process_once(db, world["acme"], fake_graph)

    replay = leadgen_payload(PAGE_A, LEADGEN_A)
    replay["entry"][0]["time"] = 1730000999  # a different delivery
    await _store_event(db, world["acme"], world["acme_conn"], replay)
    await _process_once(db, world["acme"], fake_graph)

    leads = await _lead_rows(db, world["acme"])
    assert len(leads) == 1
    # The Graph API was asked both times: deduplication is the database's job, so the
    # processor may do the work and still converge on one row.
    assert fake_graph.leadgen_ids_seen.count(LEADGEN_A) == 2


async def test_concurrent_processing_of_one_lead_yields_exactly_one_row(db, world, fake_graph):
    """The PRD's rule under the condition that makes a check-then-insert unsafe.

    Two workers, same tenant, same leadgen_id, in separate sessions and transactions.
    Only the unique index can settle this.
    """
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload())
    await _store_event(db, world["acme"], world["acme_conn"], leadgen_payload())

    results = await asyncio.gather(
        *(
            _insert_lead_concurrently(db, world["acme"], world["acme_conn"], LEADGEN_A)
            for _ in range(5)
        ),
        return_exceptions=True,
    )
    created = [r for r in results if r is True]
    failures = [r for r in results if isinstance(r, Exception)]

    assert not failures, f"no insert should have raised: {failures}"
    assert len(created) == 1, "exactly one concurrent insert may report created"
    assert len(await _lead_rows(db, world["acme"])) == 1


async def _insert_lead_concurrently(factory, tenant_id, connection_id, leadgen_id):
    from app.services.meta_leads import normalize_lead

    reference = extract_leadgen_events(leadgen_payload())[0]
    lead = normalize_lead(lead_graph_payload(leadgen_id), reference=reference)
    async with factory() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        _row, created = await lead_repo.insert_if_new(
            session, tenant_id=tenant_id, meta_connection_id=connection_id, lead=lead
        )
        return created


# -- retry safety and failure handling ---------------------------------------


async def test_a_transient_graph_failure_leaves_the_event_retryable(db, world, fake_graph):
    """A lost lead is worse than a redelivered one, so the event must not be consumed."""
    fake_graph.program_lead_error(LEADGEN_A, transient_error())
    event_id = await _store_event(db, world["acme"], world["acme_conn"], leadgen_payload())
    await _process_once(db, world["acme"], fake_graph)

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        event = await event_repo.get(session, event_id)

    assert event.status.value == "FAILED"
    assert "meta_api_unavailable" in (event.error_message or "")
    assert await _lead_rows(db, world["acme"]) == []


async def test_retrying_a_transient_failure_recovers_the_lead(db, world, fake_graph):
    """The retry path, end to end. The unique constraint makes the replay idempotent."""
    fake_graph.program_lead_error(LEADGEN_A, transient_error())
    event_id = await _store_event(db, world["acme"], world["acme_conn"], leadgen_payload())
    await _process_once(db, world["acme"], fake_graph)
    assert await _lead_rows(db, world["acme"]) == []

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        assert await event_repo.requeue_failed(session, event_id, max_attempts=5) is True

    fake_graph.program_lead(LEADGEN_A, lead_graph_payload())
    await _process_once(db, world["acme"], fake_graph)

    leads = await _lead_rows(db, world["acme"])
    assert [lead["leadgen_id"] for lead in leads] == [LEADGEN_A]
    # Retrying again must not produce a second lead. The refusal here is because the event
    # is PROCESSED, not because the budget ran out - a generous budget is passed precisely
    # so the two reasons cannot be confused.
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        assert await event_repo.requeue_failed(session, event_id, max_attempts=5) is False
        event = await session.get(MetaWebhookEvent, event_id)
        assert event.status is MetaWebhookEventStatus.PROCESSED
    assert len(await _lead_rows(db, world["acme"])) == 1


async def test_a_permanent_failure_is_not_requeueable(db, world, fake_graph):
    """Retrying a revoked token would spin a worker forever; a human must intervene."""
    fake_graph.program_lead_error(LEADGEN_A, MetaAuthError("token rejected", code=190))
    event_id = await _store_event(db, world["acme"], world["acme_conn"], leadgen_payload())
    await _process_once(db, world["acme"], fake_graph)

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        assert await event_repo.requeue_failed(session, event_id, max_attempts=5) is False

    # Still FAILED, and still not claimable.
    await _process_once(db, world["acme"], fake_graph)
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        assert (await event_repo.get(session, event_id)).status.value == "FAILED"


async def test_a_revoked_token_is_a_permanent_failure_not_a_retry_loop(db, world, fake_graph):
    fake_graph.program_lead_error(LEADGEN_A, MetaAuthError("token rejected", code=190))
    event_id = await _store_event(db, world["acme"], world["acme_conn"], leadgen_payload())
    await _process_once(db, world["acme"], fake_graph)

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        event = await event_repo.get(session, event_id)

    assert event.status.value == "FAILED"
    assert "permanent" in (event.error_message or "")
    assert await _lead_rows(db, world["acme"]) == []


async def test_an_invalid_lead_payload_is_handled_safely(db, world, fake_graph):
    fake_graph.program_lead(LEADGEN_A, {"field_data": []})  # no id
    event_id = await _store_event(db, world["acme"], world["acme_conn"], leadgen_payload())
    await _process_once(db, world["acme"], fake_graph)

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        event = await event_repo.get(session, event_id)

    assert event.status.value == "FAILED"
    assert "lead_payload_invalid" in (event.error_message or "")
    assert await _lead_rows(db, world["acme"]) == []


async def test_an_unrelated_event_is_marked_processed_without_creating_a_lead(
    db, world, fake_graph
):
    """A `feed` change on a Page reaches the Messenger channel but has no message in it.

    Phase 4 changed the recorded note from `not_a_lead_event` to
    `messaging_event_without_message`, which is the more accurate statement: routing now
    resolves the Page to a messaging channel and finds nothing to store.
    """
    event_id = await _store_event(db, world["acme"], world["acme_conn"], page_post_payload())
    await _process_once(db, world["acme"], fake_graph)

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        event = await event_repo.get(session, event_id)

    assert event.status.value == "PROCESSED"
    assert event.error_message == "messaging_event_without_message"
    assert await _lead_rows(db, world["acme"]) == []
    assert fake_graph.lead_calls == [], "no Graph call for an event that is not a lead"


async def test_an_event_for_a_surface_we_do_not_handle_is_left_alone(db, world, fake_graph):
    """An unroutable surface must not be coerced into a channel we support.

    Phase 10-O.3 made `whatsapp_business_account` a *supported* surface, so this can no
    longer use it as the example - an unknown or future object type is the honest one. The
    rule is unchanged: store it, account for it, create nothing.
    """
    from app.services import meta_processor

    unsupported = {
        "object": "ads_management",
        "entry": [
            {
                "id": "9990000000000009",
                "messaging": [
                    {
                        "sender": {"id": "X-1"},
                        "recipient": {"id": "X-BIZ"},
                        "timestamp": 1730000300000,
                        "message": {"mid": "x.mid.1", "text": "hello"},
                    }
                ],
            }
        ],
    }
    event_id = await _store_event(db, world["acme"], world["acme_conn"], unsupported)
    await _process_once(db, world["acme"], fake_graph)

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        event = await event_repo.get(session, event_id)

    assert event.status.value == "PROCESSED"
    assert event.error_message == meta_processor.UNROUTED


async def test_a_disconnected_connection_creates_no_lead(db, world, fake_graph, owner):
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload())
    await _store_event(db, world["acme"], world["acme_conn"], leadgen_payload())

    async with owner() as session, session.begin():
        connection = await session.get(MetaConnection, world["acme_conn"])
        connection.status = MetaConnectionStatus.INACTIVE
        await session.flush()

    await _process_once(db, world["acme"], fake_graph)
    assert await _lead_rows(db, world["acme"]) == []
    assert fake_graph.lead_calls == []


async def test_a_connection_without_a_credential_creates_no_lead(db, world, fake_graph, owner):
    """A hand-configured Phase 2 connection has identifiers but no access token."""
    connection_id = await create_meta_connection(
        owner, tenant_id=world["acme"], page_id="4444444444444444"
    )
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload())
    await _store_event(db, world["acme"], connection_id, leadgen_payload())
    await _process_once(db, world["acme"], fake_graph)

    assert fake_graph.lead_calls == []
    assert await _lead_rows(db, world["acme"]) == []


async def test_a_worker_with_no_graph_client_fails_loudly(db, world):
    """A misconfigured worker must not quietly mark events processed."""
    from app.core.config import get_settings

    await _store_event(db, world["acme"], world["acme_conn"], leadgen_payload())
    with pytest.raises(RuntimeError, match="MetaGraphClient"):
        await _process_once(db, world["acme"], None, settings=get_settings())


# -- tenant isolation --------------------------------------------------------


async def test_leads_are_isolated_by_tenant(db, world, fake_graph):
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload())
    await _store_event(db, world["acme"], world["acme_conn"], leadgen_payload())
    await _process_once(db, world["acme"], fake_graph)

    assert len(await _lead_rows(db, world["acme"])) == 1
    assert await _lead_rows(db, world["globex"]) == []


async def test_a_lead_cannot_be_read_from_another_tenant(db, world, fake_graph):
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload())
    await _store_event(db, world["acme"], world["acme_conn"], leadgen_payload())
    await _process_once(db, world["acme"], fake_graph)
    acme_leads = await _lead_rows(db, world["acme"])
    acme_lead_id = acme_leads[0]["id"]

    async with db() as session, session.begin():
        await set_current_tenant(session, world["globex"])
        assert await lead_repo.get(session, acme_lead_id) is None


async def test_a_lead_cannot_be_inserted_for_another_tenant(db, world, fake_graph):
    from sqlalchemy.exc import ProgrammingError

    reference = extract_leadgen_events(leadgen_payload())[0]
    lead = normalize_lead(lead_graph_payload(), reference=reference)
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with db() as session, session.begin():
            await set_current_tenant(session, world["acme"])
            await lead_repo.insert_if_new(
                session,
                tenant_id=world["globex"],
                meta_connection_id=world["acme_conn"],
                lead=lead,
            )


async def test_two_tenants_may_each_hold_the_same_leadgen_id(db, world, fake_graph):
    """Uniqueness is per tenant: Meta ids are global but isolation wins."""
    reference = extract_leadgen_events(leadgen_payload())[0]
    for tenant_id, connection_id in (
        (world["acme"], world["acme_conn"]),
        (world["globex"], world["globex_conn"]),
    ):
        lead = normalize_lead(lead_graph_payload(), reference=reference)
        async with db() as session, session.begin():
            await set_current_tenant(session, tenant_id)
            _row, created = await lead_repo.insert_if_new(
                session, tenant_id=tenant_id, meta_connection_id=connection_id, lead=lead
            )
            assert created is True

    assert len(await _lead_rows(db, world["acme"])) == 1
    assert len(await _lead_rows(db, world["globex"])) == 1


# -- the webhook path is untouched -------------------------------------------


async def test_the_webhook_endpoint_acks_without_calling_meta(api, db, world, fake_graph):
    """The ACK must not wait on the Graph API. Nothing here should reach it."""
    payload = leadgen_payload(PAGE_A, LEADGEN_A)
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload())
    raw = body(payload)

    async with db() as session, session.begin():
        from app.services.meta_webhook import ingest

        await ingest(session, raw_body=raw, request_headers=signed_headers(raw))

    assert fake_graph.lead_calls == []


async def test_the_real_leadgen_payload_routes_by_page_and_dedupes(db, world):
    from app.services.meta_webhook import ingest

    raw = body(leadgen_payload(PAGE_A, LEADGEN_A))
    headers = signed_headers(raw)
    async with db() as session, session.begin():
        first = await ingest(session, raw_body=raw, request_headers=headers)
    async with db() as session, session.begin():
        again = await ingest(session, raw_body=raw, request_headers=headers)

    assert first.created is True
    assert first.tenant_id == world["acme"]
    assert first.meta_connection_id == world["acme_conn"]
    assert again.duplicate is True
    assert again.event_id == first.event_id
