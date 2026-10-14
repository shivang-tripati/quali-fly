"""The async-processing seam: claim, complete, fail.

Phase 2 stops at the durable boundary. What is proven here is that a future worker can
safely take a `RECEIVED` event without holding a database lock across its own work, and
that the state transitions are recorded.
"""

import asyncio
from datetime import UTC, datetime

import pytest

from app.db.tenant_context import set_current_tenant
from app.models.meta_webhook_event import MetaWebhookEvent, MetaWebhookEventStatus
from app.services.meta_processor import RETRYABLE, UNROUTED, MetaWebhookEventProcessor
from tests.factories import create_meta_connection, create_tenant
from tests.meta_fixtures import PAGE_A


async def _seed(db, tenant_id, connection_id, key, status=MetaWebhookEventStatus.RECEIVED):
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        event = MetaWebhookEvent(
            tenant_id=tenant_id,
            meta_connection_id=connection_id,
            deduplication_key=key,
            payload={"object": "page"},
            headers={},
            status=status,
            received_at=datetime.now(UTC),
        )
        session.add(event)
        await session.flush()
        return event.id


@pytest.fixture
async def world(owner):
    tenant = await create_tenant(owner, name="Acme", slug="acme")
    connection = await create_meta_connection(owner, tenant_id=tenant, page_id=PAGE_A)
    return {"tenant": tenant, "connection": connection}


async def test_received_events_are_claimed_and_moved_to_processing(db, world):
    event_id = await _seed(db, world["tenant"], world["connection"], "k-1")

    async with db() as session, session.begin():
        processor = MetaWebhookEventProcessor(session, world["tenant"])
        claimed = await processor.claim_next()

    assert claimed is not None
    assert claimed.id == event_id
    assert claimed.status is MetaWebhookEventStatus.PROCESSING

    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        stored = await session.get(MetaWebhookEvent, event_id)
        assert stored.status is MetaWebhookEventStatus.PROCESSING


async def test_nothing_to_claim_returns_none(db, world):
    async with db() as session, session.begin():
        processor = MetaWebhookEventProcessor(session, world["tenant"])
        assert await processor.claim_next() is None


async def test_an_event_is_claimed_only_once(db, world):
    await _seed(db, world["tenant"], world["connection"], "k-1")

    async with db() as first, first.begin():
        processor = MetaWebhookEventProcessor(first, world["tenant"])
        claimed = await processor.claim_next()
    assert claimed is not None

    async with db() as second, second.begin():
        processor = MetaWebhookEventProcessor(second, world["tenant"])
        assert await processor.claim_next() is None


async def test_concurrent_workers_never_claim_the_same_event(db, world):
    for index in range(4):
        await _seed(db, world["tenant"], world["connection"], f"k-{index}")

    claimed = []

    async def worker() -> None:
        async with db() as session, session.begin():
            processor = MetaWebhookEventProcessor(session, world["tenant"])
            event = await processor.claim_next()
            if event is not None:
                claimed.append(event.id)

    await asyncio.gather(*(worker() for _ in range(4)))

    assert len(claimed) == len(set(claimed)), "an event was claimed twice"


async def test_handle_marks_the_event_processed(db, world):
    """The seeded payload carries no leadgen change and no routable surface.

    Phase 4 routes messaging events too, so the note distinguishes the two ways an event
    produces nothing: `unrouted_event` when we cannot tell which surface it belongs to
    (this seed has no `object_type`), and `messaging_event_without_message` when it
    reached a channel but held no message. Both are distinguishable from success.
    """
    event_id = await _seed(db, world["tenant"], world["connection"], "k-1")

    async with db() as session, session.begin():
        processor = MetaWebhookEventProcessor(session, world["tenant"])
        claimed = await processor.claim_next()
        await processor.handle(claimed)

    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        stored = await session.get(MetaWebhookEvent, event_id)
        assert stored.status is MetaWebhookEventStatus.PROCESSED
        assert stored.processed_at is not None
        assert stored.error_message == UNROUTED


async def test_a_failed_event_records_the_reason(db, world):
    event_id = await _seed(db, world["tenant"], world["connection"], "k-1")

    async with db() as session, session.begin():
        processor = MetaWebhookEventProcessor(session, world["tenant"])
        claimed = await processor.claim_next()
        await processor.mark_failed(claimed, "meta graph api timed out", category=RETRYABLE)

    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        stored = await session.get(MetaWebhookEvent, event_id)
        assert stored.status is MetaWebhookEventStatus.FAILED
        # The category is part of the message: `requeue_failed` keys off it to decide
        # whether a second attempt is worth anything.
        assert stored.error_message == "meta_api_unavailable:meta graph api timed out"
        assert stored.processed_at is not None


async def test_a_worker_cannot_claim_another_tenants_events(db, owner):
    tenant_a = await create_tenant(owner, name="Acme", slug="acme")
    tenant_b = await create_tenant(owner, name="Globex", slug="globex")
    conn_b = await create_meta_connection(owner, tenant_id=tenant_b, page_id="222")
    await _seed(db, tenant_b, conn_b, "b-only")

    async with db() as session, session.begin():
        processor = MetaWebhookEventProcessor(session, tenant_a)
        assert await processor.claim_next() is None


async def test_active_tenants_lists_only_active_tenants(db, owner):
    from app.db.session import get_session_factory
    from app.models.tenant import TenantStatus
    from app.services.meta_processor import active_tenants

    active = await create_tenant(owner, name="Active Co", slug="active-co")
    suspended = await create_tenant(
        owner, name="Gone Co", slug="gone-co", status=TenantStatus.SUSPENDED
    )

    listed = set(await active_tenants(get_session_factory()))

    assert active in listed
    assert suspended not in listed
