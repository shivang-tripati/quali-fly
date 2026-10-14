"""Phase 10-D: end-to-end replay idempotency, event claim ownership, and the boundaries
downstream of them.

Everything here runs against real PostgreSQL, because every property under test is a
property of the database rather than of Python: a unique index deciding a race, a
conditional `UPDATE` rejecting a stale writer, RLS refusing a cross-tenant read. A mock
would assert that the code called the right methods, which is the one thing that was
already true.

Meta's Graph API is the only mocked boundary, and it is mocked in exactly two places -
a lead retrieval that fails transiently, and one that fails permanently - because those
are the two error classes the retry policy turns on and neither can be produced by a
success response.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.tenant_context import set_current_tenant
from app.models.ai_message_run import AIMessageRun
from app.models.conversation import Conversation, Message, MessageDirection
from app.models.human_handoff import HumanHandoff
from app.models.internal_notification import InternalNotification
from app.models.lead import Lead, LeadStatus
from app.models.meta_webhook_event import MetaWebhookEvent, MetaWebhookEventStatus
from app.models.scheduled_task import ScheduledTask, TaskStatus
from app.repositories import meta_webhook_events as event_repo
from app.services.ai_qualification import AIConversationService
from app.services.meta_graph import MetaApiError, MetaAuthError
from app.services.meta_processor import (
    PERMANENT,
    RETRYABLE,
    MetaWebhookEventProcessor,
    active_tenants,
)
from tests.factories import create_tenant
from tests.fakes import FakeGraph, FakeRegistry
from tests.meta_fixtures import PAGE_A, PAGE_B

# The webhook worker has no lease today. 10-D.3 requires one, so the tests name a lease
# the way the settings will.
LEASE_SECONDS = 300


@pytest.fixture
async def world(owner):
    """A tenant with a *usable* Meta connection.

    The access token is encrypted into the row because the processor refuses to call the
    Graph API without one, marking the event `permanent:credential_unavailable` instead.
    Without it, every lead-ingestion test below would pass for a reason that has nothing to
    do with replay.
    """
    tenant = await create_tenant(owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    connection = await _connection_with_token(owner, tenant, PAGE_A)
    return {"tenant": tenant, "connection": connection}


async def _connection_with_token(owner, tenant_id, page_id: str) -> uuid.UUID:
    from app.core.crypto import encrypt
    from app.models.meta_connection import MetaConnection
    from tests.conftest import TEST_META_TOKEN_KEY
    from tests.fakes import PAGE_TOKEN

    async with owner() as session, session.begin():
        connection = MetaConnection(
            tenant_id=tenant_id,
            page_id=page_id,
            access_token_encrypted=encrypt(PAGE_TOKEN, key=TEST_META_TOKEN_KEY),
            connected_at=datetime.now(UTC),
        )
        session.add(connection)
        await session.flush()
        return connection.id


async def _seed_event(
    db,
    tenant_id,
    connection_id,
    key: str,
    *,
    status: MetaWebhookEventStatus = MetaWebhookEventStatus.RECEIVED,
    payload: dict | None = None,
    object_type: str | None = None,
):
    """Seed a durable event directly, as the webhook route would have left it.

    `object_type` is a separate column rather than something derived from `payload`, and
    that matters: `channel_for_payload` routes on the column, so a seeded event with a
    complete messaging payload but no `object_type` is correctly treated as unroutable and
    would fail every messaging test for a reason that has nothing to do with replay.
    """
    body = payload if payload is not None else {"object": "page"}
    async with _tx(db, tenant_id) as session:
        event = MetaWebhookEvent(
            tenant_id=tenant_id,
            meta_connection_id=connection_id,
            deduplication_key=key,
            object_type=object_type if object_type is not None else body.get("object"),
            event_type="messaging" if body.get("entry", [{}])[0].get("messaging") else "leadgen",
            payload=body,
            headers={},
            status=status,
            received_at=datetime.now(UTC),
        )
        session.add(event)
        await session.flush()
        return event.id


async def _event(db, tenant_id, event_id) -> MetaWebhookEvent:
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        return await session.get(MetaWebhookEvent, event_id)


async def _count(db, model, tenant_id) -> int:
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        return await session.scalar(
            select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
        )


async def _count_where(db, model, tenant_id, *conditions) -> int:
    """Count rows matching extra predicates.

    Needed because a `Message` count alone is ambiguous: one inbound plus one AI reply is
    already two, so "the replay created a second message" cannot be asserted on the total.
    """
    async with _tx(db, tenant_id) as session:
        statement = select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
        for condition in conditions:
            statement = statement.where(condition)
        return await session.scalar(statement)


@asynccontextmanager
async def _tx(db, tenant_id):
    """A transaction with the tenant context set, which RLS requires for every write.

    The claim repository is deliberately not exempt: a claim is a tenant-scoped write, and
    a caller that forgot the context should get a policy violation rather than a claim that
    quietly affects nobody.
    """
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        yield session


def _transient_api_error() -> MetaApiError:
    """A retryable Graph failure. `retryable` is a class attribute, not a constructor
    argument - passing it to `__init__` raises, so the subclass is the honest way to say it.
    """

    class _Transient(MetaApiError):
        retryable = True

    return _Transient("temporarily unavailable", code=2)


# ============================================================== 10-D.3 event claim
#
# The gap: `claim_next_received` did `SELECT ... FOR UPDATE SKIP LOCKED` and set
# `status = PROCESSING`, but recorded *who* claimed it and *when*. Three consequences:
#
#   1. A worker that died mid-processing left the row PROCESSING forever. Nothing ever
#      moved it back to RECEIVED, so the event was invisible to every future claim.
#   2. `mark_processed` / `mark_failed` filtered on `id` alone, so a worker whose lease had
#      been reclaimed could still overwrite the new owner's terminal state - turning a
#      genuine success back into a failure, or the reverse.
#   3. There was no attempt counter, so "bounded retries" was not a property of anything.


async def test_a_crashed_workers_claim_is_recovered_after_the_lease_expires(db, world):
    """A worker that dies holding a claim must not strand the event forever.

    The event is left in PROCESSING exactly as a crash would leave it, with a lease that
    has already lapsed. Without recovery the row is unreachable: `claim_next_received`
    only ever selects RECEIVED.
    """
    claimed_at = datetime.now(UTC)
    event_id = await _seed_event(db, world["tenant"], world["connection"], "k-crash")
    async with _tx(db, world["tenant"]) as session:
        event = await session.get(MetaWebhookEvent, event_id)
        event.status = MetaWebhookEventStatus.PROCESSING
        event.locked_by = "worker-a"
        event.locked_at = claimed_at - timedelta(seconds=LEASE_SECONDS + 1)
        event.attempt_count = 1
    assert (await _event(db, world["tenant"], event_id)).status is MetaWebhookEventStatus.PROCESSING

    async with _tx(db, world["tenant"]) as session:
        reclaimed = await event_repo.claim_next_received(
            session,
            worker="worker-b",
            now=claimed_at,
            lease_seconds=LEASE_SECONDS,
        )

    assert reclaimed is not None, "an expired claim was never recovered"
    assert reclaimed.id == event_id
    stored = await _event(db, world["tenant"], event_id)
    assert stored.locked_by == "worker-b"
    assert stored.attempt_count == 2, "the reclaim did not count as a second attempt"


async def test_an_unexpired_claim_is_not_stolen(db, world):
    """Recovery must not reach into a claim that is still alive, or two workers would run
    the same event concurrently - the precise thing SKIP LOCKED was preventing."""
    now = datetime.now(UTC)
    event_id = await _seed_event(db, world["tenant"], world["connection"], "k-live")
    async with _tx(db, world["tenant"]) as session:
        event = await session.get(MetaWebhookEvent, event_id)
        event.status = MetaWebhookEventStatus.PROCESSING
        event.locked_by = "worker-a"
        event.locked_at = now

    async with _tx(db, world["tenant"]) as session:
        assert (
            await event_repo.claim_next_received(
                session, worker="worker-b", now=now, lease_seconds=LEASE_SECONDS
            )
            is None
        )


async def test_a_stale_worker_cannot_overwrite_a_newer_workers_success(db, world):
    """The core of 10-D.3: a worker must prove it still owns the claim before committing.

    Worker A claims, stalls past its lease, worker B reclaims and succeeds. When A finally
    finishes it must not be able to record its own failure over B's result - that is how a
    delivered lead ends up marked as failed and retried forever.
    """
    now = datetime.now(UTC)
    event_id = await _seed_event(db, world["tenant"], world["connection"], "k-stale")
    async with _tx(db, world["tenant"]) as session:
        event = await session.get(MetaWebhookEvent, event_id)
        event.status = MetaWebhookEventStatus.PROCESSING
        event.locked_by = "worker-a"
        event.locked_at = now - timedelta(seconds=LEASE_SECONDS + 1)

    # B reclaims and succeeds.
    async with _tx(db, world["tenant"]) as session:
        assert await event_repo.claim_next_received(
            session, worker="worker-b", now=now, lease_seconds=LEASE_SECONDS
        )
        await event_repo.mark_processed(session, event_id, worker="worker-b", note="ok")

    # A wakes up believing it still owns the event, and tries to record a failure.
    async with _tx(db, world["tenant"]) as session:
        accepted = await event_repo.mark_failed(
            session, event_id, worker="worker-a", error_message="permanent:too_late"
        )

    assert accepted is False, "a stale worker wrote to an event it no longer owned"
    stored = await _event(db, world["tenant"], event_id)
    assert stored.status is MetaWebhookEventStatus.PROCESSED
    assert stored.error_message == "ok"


async def test_a_stale_worker_cannot_overwrite_a_newer_workers_failure_either(db, world):
    """The mirror image, and the more dangerous one: turning a genuine failure into a
    success would consume an event whose business effect never happened."""
    now = datetime.now(UTC)
    event_id = await _seed_event(db, world["tenant"], world["connection"], "k-stale2")
    async with _tx(db, world["tenant"]) as session:
        event = await session.get(MetaWebhookEvent, event_id)
        event.status = MetaWebhookEventStatus.PROCESSING
        event.locked_by = "worker-a"
        event.locked_at = now - timedelta(seconds=LEASE_SECONDS + 1)

    async with _tx(db, world["tenant"]) as session:
        assert await event_repo.claim_next_received(
            session, worker="worker-b", now=now, lease_seconds=LEASE_SECONDS
        )
        await event_repo.mark_failed(
            session, event_id, worker="worker-b", error_message="meta_api_unavailable:boom"
        )

    async with _tx(db, world["tenant"]) as session:
        accepted = await event_repo.mark_processed(session, event_id, worker="worker-a")

    assert accepted is False
    stored = await _event(db, world["tenant"], event_id)
    assert stored.status is MetaWebhookEventStatus.FAILED
    assert stored.error_message == "meta_api_unavailable:boom"


async def test_two_workers_competing_for_one_event_produce_one_owner(db, world):
    now = datetime.now(UTC)
    event_id = await _seed_event(db, world["tenant"], world["connection"], "k-race")
    results = []

    async def worker(name: str) -> None:
        async with _tx(db, world["tenant"]) as session:
            claimed = await event_repo.claim_next_received(
                session, worker=name, now=now, lease_seconds=LEASE_SECONDS
            )
            results.append(claimed.id if claimed else None)

    await asyncio.gather(*(worker(f"w{i}") for i in range(8)))

    won = [r for r in results if r is not None]
    assert won == [event_id], f"expected exactly one winner, got {won}"
    assert (await _event(db, world["tenant"], event_id)).locked_by in {f"w{i}" for i in range(8)}


async def test_renewing_a_lease_defeats_a_reclaim(db, world):
    """A worker about to do slow external work pushes its expiry out, so a slow-but-alive
    worker is never mistaken for a dead one."""
    now = datetime.now(UTC)
    event_id = await _seed_event(db, world["tenant"], world["connection"], "k-renew")
    async with _tx(db, world["tenant"]) as session:
        event = await session.get(MetaWebhookEvent, event_id)
        event.status = MetaWebhookEventStatus.PROCESSING
        event.locked_by = "worker-a"
        event.locked_at = now - timedelta(seconds=LEASE_SECONDS - 30)

    async with _tx(db, world["tenant"]) as session:
        assert await event_repo.renew_lease(session, event_id, worker="worker-a", now=now)
        assert (
            await event_repo.claim_next_received(
                session, worker="worker-b", now=now, lease_seconds=LEASE_SECONDS
            )
            is None
        )


async def test_a_non_owner_cannot_renew_or_complete(db, world):
    now = datetime.now(UTC)
    event_id = await _seed_event(db, world["tenant"], world["connection"], "k-nonowner")
    async with _tx(db, world["tenant"]) as session:
        event = await session.get(MetaWebhookEvent, event_id)
        event.status = MetaWebhookEventStatus.PROCESSING
        event.locked_by = "worker-a"
        event.locked_at = now

    async with _tx(db, world["tenant"]) as session:
        assert not await event_repo.renew_lease(session, event_id, worker="worker-b", now=now)
        assert not await event_repo.mark_processed(session, event_id, worker="worker-b")
        assert not await event_repo.mark_failed(
            session, event_id, worker="worker-b", error_message="x"
        )


async def test_a_terminal_event_is_never_reclaimed(db, world):
    """Terminal means terminal. A duplicate delivery, a reclaim sweep or a requeue must not
    resurrect a PROCESSED event."""
    now = datetime.now(UTC)
    for key, status in (
        ("k-term-a", MetaWebhookEventStatus.PROCESSED),
        ("k-term-b", MetaWebhookEventStatus.FAILED),
    ):
        event_id = await _seed_event(db, world["tenant"], world["connection"], key, status=status)
        async with db() as session, session.begin():
            await set_current_tenant(session, world["tenant"])
            event = await session.get(MetaWebhookEvent, event_id)
            # A terminal row that also carries a long-expired claim: the sweep must still
            # leave it alone, or a crash-then-recover bug would loop forever.
            event.locked_by = "worker-z"
            event.locked_at = now - timedelta(days=7)

    async with _tx(db, world["tenant"]) as session:
        assert (
            await event_repo.claim_next_received(
                session, worker="worker-b", now=now, lease_seconds=LEASE_SECONDS
            )
            is None
        )


async def test_requeue_refuses_once_the_attempt_budget_is_spent(db, world):
    """10-D.3: retries are bounded. A permanently-broken event must stop, not spin."""
    event_id = await _seed_event(
        db,
        world["tenant"],
        world["connection"],
        "k-budget",
        status=MetaWebhookEventStatus.FAILED,
    )
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        event = await session.get(MetaWebhookEvent, event_id)
        event.error_message = f"{RETRYABLE}:graph_down"
        event.attempt_count = 3

    async with _tx(db, world["tenant"]) as session:
        assert await event_repo.requeue_failed(session, event_id, max_attempts=3) is False, (
            "an event past its attempt budget was requeued"
        )
    assert (await _event(db, world["tenant"], event_id)).status is MetaWebhookEventStatus.FAILED


async def test_requeue_allows_a_retry_below_the_budget(db, world):
    event_id = await _seed_event(
        db,
        world["tenant"],
        world["connection"],
        "k-budget2",
        status=MetaWebhookEventStatus.FAILED,
    )
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        event = await session.get(MetaWebhookEvent, event_id)
        event.error_message = f"{RETRYABLE}:graph_down"
        event.attempt_count = 1

    async with _tx(db, world["tenant"]) as session:
        assert await event_repo.requeue_failed(session, event_id, max_attempts=3) is True

    stored = await _event(db, world["tenant"], event_id)
    assert stored.status is MetaWebhookEventStatus.RECEIVED
    assert stored.attempt_count == 1, "requeue consumed an attempt it never made"


async def test_requeue_still_refuses_permanent_failures_at_any_budget(db, world):
    """The category check is independent of the budget: a revoked token is not retried
    however many attempts remain."""
    event_id = await _seed_event(
        db,
        world["tenant"],
        world["connection"],
        "k-perm",
        status=MetaWebhookEventStatus.FAILED,
    )
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        event = await session.get(MetaWebhookEvent, event_id)
        event.error_message = f"{PERMANENT}:credential_rejected"
        event.attempt_count = 0

    async with _tx(db, world["tenant"]) as session:
        assert await event_repo.requeue_failed(session, event_id, max_attempts=10) is False


# ============================================================ 10-D.2 replay of an event


async def test_a_duplicate_delivery_never_resets_a_processed_event(db, world):
    """10-D.2: a duplicate must not reset a terminal event to PENDING/RECEIVED.

    The dedupe insert is `ON CONFLICT DO NOTHING` and does not touch status, so this holds
    even when the duplicate arrives long after processing finished.
    """
    event_id = await _seed_event(
        db,
        world["tenant"],
        world["connection"],
        "meta:sha256:replay1",
        status=MetaWebhookEventStatus.PROCESSED,
    )

    async with _tx(db, world["tenant"]) as session:
        await set_current_tenant(session, world["tenant"])
        event, created = await event_repo.insert_if_new(
            session,
            tenant_id=world["tenant"],
            meta_connection_id=world["connection"],
            deduplication_key="meta:sha256:replay1",
            meta_event_id=None,
            object_type="page",
            event_type="leadgen",
            source_identifier=PAGE_A,
            payload={"object": "page"},
            headers={},
            received_at=datetime.now(UTC),
        )

    assert created is False
    assert event.id == event_id
    stored = await _event(db, world["tenant"], event_id)
    assert stored.status is MetaWebhookEventStatus.PROCESSED, "a duplicate reset a terminal event"
    assert stored.payload == {"object": "page"}, "a duplicate overwrote the stored payload"


async def test_a_duplicate_delivery_does_not_overwrite_a_failure_with_a_stale_payload(db, world):
    event_id = await _seed_event(
        db,
        world["tenant"],
        world["connection"],
        "meta:sha256:replay2",
        status=MetaWebhookEventStatus.FAILED,
    )
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        event = await session.get(MetaWebhookEvent, event_id)
        event.error_message = f"{PERMANENT}:credential_rejected"

    async with _tx(db, world["tenant"]) as session:
        _, created = await event_repo.insert_if_new(
            session,
            tenant_id=world["tenant"],
            meta_connection_id=world["connection"],
            deduplication_key="meta:sha256:replay2",
            meta_event_id=None,
            object_type="page",
            event_type="leadgen",
            source_identifier=PAGE_A,
            # A different body under the same key: what a naive upsert would write over.
            payload={"object": "page", "stale": True},
            headers={"user-agent": "different"},
            received_at=datetime.now(UTC),
        )

    assert created is False
    stored = await _event(db, world["tenant"], event_id)
    assert stored.status is MetaWebhookEventStatus.FAILED
    assert stored.error_message == f"{PERMANENT}:credential_rejected"
    assert stored.payload == {"object": "page"}


async def test_two_connections_may_receive_the_same_dedupe_key(db, owner):
    """The unique index is `(meta_connection_id, deduplication_key)`, not the key alone.

    Two tenants connected to the same Page both receive a delivery; collapsing them would
    silently deny one tenant a lead.
    """
    tenant_a = await create_tenant(owner, name="A", slug=f"a-{uuid.uuid4().hex[:8]}")
    tenant_b = await create_tenant(owner, name="B", slug=f"b-{uuid.uuid4().hex[:8]}")
    conn_a = await _connection_with_token(owner, tenant_a, PAGE_A)
    conn_b = await _connection_with_token(owner, tenant_b, PAGE_B)

    first = await _seed_event(db, tenant_a, conn_a, "meta:mid:shared")
    second = await _seed_event(db, tenant_b, conn_b, "meta:mid:shared")

    assert first != second
    assert await _count(db, MetaWebhookEvent, tenant_a) == 1
    assert await _count(db, MetaWebhookEvent, tenant_b) == 1


async def test_repeat_deliveries_do_not_bypass_the_claim(db, world):
    """10-D.3: a duplicate delivery creates no new row, so it cannot create a second unit
    of work for the worker to claim."""
    for _ in range(5):
        async with _tx(db, world["tenant"]) as session:
            await event_repo.insert_if_new(
                session,
                tenant_id=world["tenant"],
                meta_connection_id=world["connection"],
                deduplication_key="meta:mid:repeat",
                meta_event_id="repeat",
                object_type="page",
                event_type="messaging",
                source_identifier=PAGE_A,
                payload={"object": "page"},
                headers={},
                received_at=datetime.now(UTC),
            )
    assert await _count(db, MetaWebhookEvent, world["tenant"]) == 1

    async with db() as session, session.begin():
        processor = MetaWebhookEventProcessor(session, world["tenant"])
        first = await processor.claim_next()
    assert first is not None
    async with db() as session, session.begin():
        processor = MetaWebhookEventProcessor(session, world["tenant"])
        assert await processor.claim_next() is None


# ================================================================ 10-D.4 lead replay


LEADGEN_PAYLOAD = {
    "object": "page",
    "entry": [
        {
            "id": PAGE_A,
            "changes": [
                {
                    "field": "leadgen",
                    "value": {
                        "leadgen_id": "LEAD-1",
                        "ad_id": "AD-1",
                        "form_id": "FORM-1",
                        "created_time": 1767225600,
                        "field_data": [
                            {"name": "full_name", "values": ["Dana Buyer"]},
                            {"name": "email", "values": ["dana@example.test"]},
                        ],
                    },
                }
            ],
        }
    ],
}


def _graph_with_lead(lead: dict) -> FakeGraph:
    graph = FakeGraph()
    graph.leads = {"LEAD-1": lead}
    return graph


async def _process_one(db, world, graph, *, key="k-lead", payload=LEADGEN_PAYLOAD):
    """Claim and handle one event the way the worker does, in two transactions."""
    await _seed_event(db, world["tenant"], world["connection"], key, payload=payload)
    async with db() as session, session.begin():
        processor = MetaWebhookEventProcessor(
            session, world["tenant"], graph=graph, session_factory=db
        )
        claimed = await processor.claim_next()
        if claimed is not None:
            await processor.handle(claimed)
    return claimed


async def test_duplicate_lead_ingestion_preserves_existing_lead_state(db, world):
    """10-D.4: a replay must not reset qualification, assignment or user-maintained fields.

        The lead is advanced to QUALIFIED and handed to a human first, exactly as the product
        would after a real reply. The replay then arrives and the insert is a no-op, so all of
        that has to survive - the alternative is a replay quietly resurrecting a closed lead as
        a brand-new
    EW` one.
    """
    graph = _graph_with_lead(
        {
            "id": "LEAD-1",
            "name": "Dana Buyer",
            "email": "dana@example.test",
            "phone": None,
            "created_time": 1767225600,
            "field_data": [{"name": "full_name", "values": ["Dana Buyer"]}],
        }
    )
    await _process_one(db, world, graph)

    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        lead = (
            await session.execute(select(Lead).where(Lead.meta_leadgen_id == "LEAD-1"))
        ).scalar_one()
        lead.status = LeadStatus.QUALIFIED
        lead.qualification = {"total": 88, "budget": 20, "fit": 20}
        lead_id = lead.id

    # Meta redelivers the same event under a new dedupe key - a fresh receipt of the same
    # business fact, which is the realistic replay.
    await _process_one(db, world, graph, key="k-lead-replay")

    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        leads = (
            (await session.execute(select(Lead).where(Lead.meta_leadgen_id == "LEAD-1")))
            .scalars()
            .all()
        )

    assert len(leads) == 1, "the replay created a second lead"
    assert leads[0].id == lead_id
    assert leads[0].status is LeadStatus.QUALIFIED, "the replay reset the lifecycle state"
    assert leads[0].qualification == {"total": 88, "budget": 20, "fit": 20}, (
        "the replay reset the AI qualification the product had already paid for"
    )


async def test_concurrent_ingestion_of_the_same_lead_yields_one_lead(db, world):
    graph = _graph_with_lead(
        {
            "id": "LEAD-1",
            "name": "Dana Buyer",
            "email": "dana@example.test",
            "phone": None,
            "created_time": 1767225600,
            "field_data": [],
        }
    )
    for index in range(5):
        await _seed_event(
            db,
            world["tenant"],
            world["connection"],
            f"k-concurrent-{index}",
            payload=LEADGEN_PAYLOAD,
        )

    async def worker() -> None:
        async with db() as session, session.begin():
            processor = MetaWebhookEventProcessor(session, world["tenant"], graph=graph)
            claimed = await processor.claim_next()
            if claimed is not None:
                await processor.handle(claimed)

    await asyncio.gather(*(worker() for _ in range(5)))

    assert await _count(db, Lead, world["tenant"]) == 1


async def test_a_transient_graph_failure_leaves_the_event_retryable(db, world):

    graph = FakeGraph()
    graph.leads = {"LEAD-1": _transient_api_error()}
    await _process_one(db, world, graph)

    event_id = await _recent_event(db, world["tenant"])
    stored = await _event(db, world["tenant"], event_id)
    assert stored.status is MetaWebhookEventStatus.FAILED
    assert stored.error_message.startswith(RETRYABLE), stored.error_message
    # And the lead is absent, so the retry has real work to do.
    assert await _count(db, Lead, world["tenant"]) == 0

    async with _tx(db, world["tenant"]) as session:
        assert await event_repo.requeue_failed(session, event_id, max_attempts=3) is True


async def test_a_retry_after_a_transient_failure_lands_the_lead(db, world):
    """10-D.4: a retry after the API recovers must succeed, and must not double up."""

    graph = FakeGraph()
    graph.leads = {"LEAD-1": _transient_api_error()}
    await _process_one(db, world, graph)
    event_id = await _recent_event(db, world["tenant"])

    async with _tx(db, world["tenant"]) as session:
        assert await event_repo.requeue_failed(session, event_id, max_attempts=3)

    # Meta comes back.

    graph.leads = {
        "LEAD-1": {
            "id": "LEAD-1",
            "name": "Dana Buyer",
            "email": "dana@example.test",
            "phone": None,
            "created_time": 1767225600,
            "field_data": [],
        }
    }
    async with db() as session, session.begin():
        processor = MetaWebhookEventProcessor(session, world["tenant"], graph=graph)
        claimed = await processor.claim_next()
        assert claimed is not None
        await processor.handle(claimed)

    assert await _count(db, Lead, world["tenant"]) == 1
    assert (await _event(db, world["tenant"], event_id)).status is MetaWebhookEventStatus.PROCESSED


async def test_a_permanent_graph_failure_is_not_retryable_forever(db, world):

    graph = FakeGraph()
    graph.leads = {"LEAD-1": MetaAuthError("token revoked")}
    await _process_one(db, world, graph)

    event_id = await _recent_event(db, world["tenant"])
    stored = await _event(db, world["tenant"], event_id)
    assert stored.status is MetaWebhookEventStatus.FAILED
    assert stored.error_message.startswith(PERMANENT), stored.error_message

    async with db() as session, session.begin():
        # Refused even with a generous budget, and refused every time it is asked.
        for _ in range(3):
            assert await event_repo.requeue_failed(session, event_id, max_attempts=10) is False
    assert (await _event(db, world["tenant"], event_id)).status is MetaWebhookEventStatus.FAILED


async def test_a_failed_ingestion_never_marks_the_event_processed(db, world):
    """10-D.4: the event must not read as successful when its business effect failed -
    otherwise the lead is silently consumed and nothing retries it."""

    graph = FakeGraph()
    graph.leads = {"LEAD-1": _transient_api_error()}
    await _process_one(db, world, graph)
    event_id = await _recent_event(db, world["tenant"])

    assert (await _event(db, world["tenant"], event_id)).status is not (
        MetaWebhookEventStatus.PROCESSED
    )
    assert await _count(db, Lead, world["tenant"]) == 0


async def _recent_event(db, tenant_id) -> uuid.UUID:
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        return (
            await session.execute(
                select(MetaWebhookEvent.id)
                .where(MetaWebhookEvent.tenant_id == tenant_id)
                .order_by(MetaWebhookEvent.received_at.desc())
                .limit(1)
            )
        ).scalar_one()


# ================================================= 10-D.5 / .6 / .7 inbound replay


def _now_ms() -> int:
    """A `timestamp` that is *now*, in Meta's milliseconds.

    The follow-up sequence refuses to arm outside Meta's 7-day tagged-message allowance, so a
    fixed literal in a fixture date would make the eligibility check fail for reasons that
    have nothing to do with replay. Derived from the clock instead, so the window is open
    whenever the test runs.
    """
    return int(datetime.now(UTC).timestamp() * 1000)


def _messaging_payload(mid: str = "mid-abc", text: str = "Is this still available?") -> dict:
    """A messaging event stamped now. A fresh dict per call, so callers can mutate safely."""
    return {
        "object": "page",
        "entry": [
            {
                "id": PAGE_A,
                "messaging": [
                    {
                        "sender": {"id": "PSID-1"},
                        "recipient": {"id": PAGE_A},
                        "timestamp": _now_ms(),
                        "message": {"mid": mid, "text": text, "is_echo": False},
                    }
                ],
            }
        ],
    }


async def test_duplicate_inbound_message_creates_one_message(db, world):
    graph = FakeGraph()
    await _process_one(db, world, graph, key="k-msg-1", payload=_messaging_payload())
    await _process_one(db, world, graph, key="k-msg-2", payload=_messaging_payload())

    assert await _count(db, Conversation, world["tenant"]) == 1
    assert await _count(db, Message, world["tenant"]) == 1


async def test_duplicate_inbound_message_does_not_move_last_inbound_at(db, world):
    """A replay must not look like a fresh reply. Moving `last_inbound_at` would push the
    24-hour messaging window forward and restart the quiet-lead clock."""
    graph = FakeGraph()
    await _process_one(db, world, graph, key="k-msg-a", payload=_messaging_payload())
    async with _tx(db, world["tenant"]) as session:
        conversation = (await session.execute(select(Conversation))).scalar_one()
        # Qualification lives on the lead, not the conversation, so the field that a replay
        # could plausibly clobber is asserted there. Here we only need the inbound clock.
        conversation.last_message_at = conversation.last_inbound_at

    later = datetime.now(UTC) + timedelta(hours=5)
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        conversation = (await session.execute(select(Conversation))).scalar_one()
        conversation.last_inbound_at = later
        await session.flush()

    await _process_one(db, world, graph, key="k-msg-b", payload=_messaging_payload())

    async with _tx(db, world["tenant"]) as session:
        conversation = (await session.execute(select(Conversation))).scalar_one()

    assert conversation.last_inbound_at == later, "a duplicate inbound moved last_inbound_at"
    assert conversation.last_message_at < later, "a duplicate inbound moved last_message_at as well"


async def test_duplicate_inbound_message_does_not_create_a_second_ai_run(db, world):
    """10-D.6: `UNIQUE(inbound_message_id)` is the claim. One message, one run.

    The same inbound message is processed twice, by two independent deliveries of the same
    webhook. The second attempt to claim is refused by the constraint, so the pipeline
    never reaches the provider a second time - which is why the registry is programmed with
    exactly one response and an extra call would raise rather than pass quietly.
    """
    graph = FakeGraph()
    registry = FakeRegistry().program_response()
    settings_ai = _ai_settings()

    await _process_one_with_ai(db, world, graph, settings_ai, registry, key="k-ai-1")
    first_calls = len(registry.requests)
    await _process_one_with_ai(db, world, graph, settings_ai, registry, key="k-ai-2")

    assert (
        await _count_where(
            db, Message, world["tenant"], Message.direction == MessageDirection.INBOUND
        )
        == 1
    )
    assert await _count(db, AIMessageRun, world["tenant"]) == 1
    assert len(registry.requests) == first_calls, "the replay called the AI provider again"


async def test_duplicate_inbound_message_does_not_send_a_second_reply(db, world):
    """The customer-visible boundary. A second response to the same message is the failure
    a customer actually notices."""
    graph = FakeGraph()
    registry = FakeRegistry().program_response()
    await _process_one_with_ai(db, world, graph, _ai_settings(), registry, key="k-ai-3")
    await _process_one_with_ai(db, world, graph, _ai_settings(), registry, key="k-ai-4")

    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        outbound = (
            (
                await session.execute(
                    select(Message).where(Message.direction == MessageDirection.OUTBOUND)
                )
            )
            .scalars()
            .all()
        )
    assert len(outbound) == 1, "the reply was sent more than once"
    assert len(graph.sends) == 1, "the replay sent to Meta again"


async def test_duplicate_inbound_creates_no_duplicate_handoff_or_notification(db, world):
    """10-D.7: a replay must not escalate the same conversation to a human twice."""
    graph = FakeGraph()
    registry = FakeRegistry().program_response(
        budget=20, timeline=20, fit=20, authority=20, human_handoff=True, handoff_reason="call"
    )
    settings_ai = _ai_settings()

    await _process_one_with_ai(db, world, graph, settings_ai, registry, key="k-handoff-1")
    after_first = (
        await _count(db, HumanHandoff, world["tenant"]),
        await _count(db, InternalNotification, world["tenant"]),
    )
    await _process_one_with_ai(db, world, graph, settings_ai, registry, key="k-handoff-2")
    after_second = (
        await _count(db, HumanHandoff, world["tenant"]),
        await _count(db, InternalNotification, world["tenant"]),
    )

    assert after_first[0] == 1, "the handoff escalation did not happen at all"
    assert after_second == after_first, (
        f"the replay changed handoff/notification counts: {after_first} -> {after_second}"
    )


async def test_a_genuinely_new_message_does_cancel_pending_followups(db, world):
    """The Phase 7 invariant must survive all of this: a real reply cancels.

    The mirror test below is the one about replay: a *duplicate* is not a new reply, and
    re-running the cancellation on every redelivery would let a retry tear down a sequence
    for a lead who never spoke again.
    """
    graph = FakeGraph()
    await _process_one(db, world, graph, key="k-follow-1", payload=_messaging_payload())

    conversation_id = await _conversation_id(db, world["tenant"])
    await _arm_followups(db, world["tenant"], conversation_id)
    armed = await _task_statuses(db, world["tenant"], conversation_id)
    assert armed, "the fixture armed nothing"
    assert all(s is TaskStatus.PENDING for s in armed), armed

    await _process_one(
        db, world, graph, key="k-follow-2", payload=_messaging_payload("mid-second", "Yes please")
    )
    cancelled = await _task_statuses(db, world["tenant"], conversation_id)
    assert all(s is TaskStatus.CANCELLED for s in cancelled), cancelled


async def test_a_replayed_message_does_not_re_arm_or_re_cancel(db, world):
    """A duplicate delivery must not drive follow-up state.

    The cancellation is an idempotent `UPDATE`, so re-running it is harmless *today*. The
    test pins that, because the failure mode this guards is someone later "fixing" the
    replay path to re-arm a sequence - which would be a duplicate chain, not a duplicate
    update.
    """
    graph = FakeGraph()
    await _process_one(db, world, graph, key="k-replay-1", payload=_messaging_payload())
    conversation_id = await _conversation_id(db, world["tenant"])

    await _arm_followups(db, world["tenant"], conversation_id)
    armed = await _task_statuses(db, world["tenant"], conversation_id)
    assert all(s is TaskStatus.PENDING for s in armed), armed

    for index in range(3):
        await _process_one(
            db, world, graph, key=f"k-replay-dup-{index}", payload=_messaging_payload()
        )

    after = await _task_statuses(db, world["tenant"], conversation_id)

    assert after == armed, f"a replay changed follow-up state: {armed} -> {after}"
    assert len(after) == len(armed), "a replay created extra follow-up tasks"


async def _task_statuses(db, tenant_id, conversation_id) -> list:
    async with _tx(db, tenant_id) as session:
        tasks = (
            (
                await session.execute(
                    select(ScheduledTask).where(ScheduledTask.conversation_id == conversation_id)
                )
            )
            .scalars()
            .all()
        )
        return [t.status for t in tasks]


async def _conversation_id(db, tenant_id) -> uuid.UUID:
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        return (await session.execute(select(Conversation.id))).scalar_one()


async def _arm_followups(db, tenant_id, conversation_id) -> None:
    """Arm step 1 of the sequence the way the AI pipeline's success path does.

    `FollowUpService` opens its own transaction via its factory, so it is constructed with
    `db` and must not be handed a session already inside one.
    """
    from app.services.follow_ups import FollowUpService

    service = FollowUpService(db, tenant_id, settings=_follow_up_settings())
    task = await service.on_reply_sent(conversation_id)
    assert task is not None, "the follow-up sequence refused to arm"


def _follow_up_settings():
    """Follow-ups on. Off by default, which would make every assertion below vacuous."""
    from app.core.config import get_settings

    return get_settings().model_copy(update={"follow_up_enabled": True})


def _ai_settings():
    """AI on, with a key and a negligible backoff.

    `groq_api_key` is set because the service is *skipped* without it, which would make
    every assertion below pass for the wrong reason. The backoff is floored at a
    millisecond so a test is not waiting on a retry sleep.
    """
    from app.core.config import get_settings

    return get_settings().model_copy(
        update={
            "ai_enabled": True,
            "groq_api_key": "gsk-test",
            "ai_max_backoff_seconds": 0.01,
        }
    )


async def _process_one_with_ai(db, world, graph, settings_obj, registry, *, key: str) -> None:
    """Process a messaging event and run the AI stage, as the worker does post-commit.

    The AI stage is driven through `AIConversationService` rather than
    `MetaWebhookEventProcessor.run_ai_stage`, because the processor builds its own service
    and would reach the real `LLMRegistry`. Calling the service directly is the same call
    with the one boundary that cannot be controlled - the provider HTTP call - substituted.
    """
    await _seed_event(db, world["tenant"], world["connection"], key, payload=_messaging_payload())
    async with db() as session, session.begin():
        processor = MetaWebhookEventProcessor(
            session,
            world["tenant"],
            graph=graph,
            settings=settings_obj,
            session_factory=db,
        )
        claimed = await processor.claim_next()
        if claimed is not None:
            await processor.handle(claimed)

    # Post-commit, in its own sessions, exactly as documented on `run_ai_stage`.
    for inbound_id in await _inbound_ids(db, world["tenant"]):
        service = AIConversationService(
            db,
            world["tenant"],
            registry=registry,
            graph=graph,
            settings=settings_obj,
        )
        await service.process_inbound_message(inbound_id)


async def _inbound_ids(db, tenant_id) -> list[uuid.UUID]:
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        rows = (
            (
                await session.execute(
                    select(Message.id).where(Message.direction == MessageDirection.INBOUND)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


# ============================================================= 10-D.16 tenant isolation


async def test_a_worker_cannot_claim_or_complete_another_tenants_event(db, owner):
    """Tenant context is the guard, and it is the same for a claim as for a read."""
    tenant_a = await create_tenant(owner, name="A", slug=f"a-{uuid.uuid4().hex[:8]}")
    tenant_b = await create_tenant(owner, name="B", slug=f"b-{uuid.uuid4().hex[:8]}")
    conn_a = await _connection_with_token(owner, tenant_a, PAGE_A)
    event_id = await _seed_event(db, tenant_a, conn_a, "k-iso")

    async with db() as session, session.begin():
        processor = MetaWebhookEventProcessor(session, tenant_b)
        assert await processor.claim_next() is None

    async with _tx(db, tenant_a) as session:
        assert not await event_repo.mark_processed(session, event_id, worker="intruder")
        assert not await event_repo.mark_failed(
            session, event_id, worker="intruder", error_message="x"
        )

    assert (await _event(db, tenant_a, event_id)).status is MetaWebhookEventStatus.RECEIVED


async def test_active_tenants_is_unchanged_by_replay_hardening(db, owner):
    tenants = await active_tenants(db)
    assert tenants == ()
