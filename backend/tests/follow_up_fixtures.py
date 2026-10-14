"""Shared Phase 7 fixtures: a tenant, a conversation, a lead, and a controllable clock.

The `FixedClock` is the point of this file. Phase 7's correctness is a function of time - a
task due at +4h, a window closing at 24h, a lease expiring at 5 minutes - and testing it
with `asyncio.sleep` would mean a suite that takes a week and is never run. Every test here
advances the clock explicitly instead.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import get_settings
from app.core.crypto import encrypt
from app.models.conversation import (
    Channel,
    Conversation,
    Message,
    MessageDelivery,
    MessageDirection,
)
from app.models.lead import Lead, LeadSource, LeadStatus
from app.models.meta_connection import MetaConnection
from tests.conftest import TEST_META_TOKEN_KEY
from tests.fakes import PAGE_TOKEN

PAGE = "1000000000000001"
PARTICIPANT = "1000000001"

#: A fixed point in the past so "4 hours ago" arithmetic is obvious in a failure message.
T0 = datetime(2026, 9, 28, 10, 0, 0, tzinfo=UTC)


def fu_settings(**overrides):
    base = {
        "follow_up_enabled": True,
        "follow_up_max_attempts": 3,
        "follow_up_retry_base_seconds": 60,
        "follow_up_processing_lease_seconds": 300,
        "follow_up_batch_size": 20,
        "follow_up_ghosted_days": 7,
        # AI enhancement off by default: most tests are about the state machine, and
        # turning it on would make every one of them depend on a provider.
        "follow_up_ai_enhance": False,
        "ai_enabled": True,
        "groq_api_key": "gsk-test",
        "ai_max_backoff_seconds": 0.01,
        "embedding_enabled": False,
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


async def make_conversation(
    owner,
    tenant_id: uuid.UUID,
    *,
    with_lead: bool = True,
    lead_status: LeadStatus = LeadStatus.NEW,
    lead_name: str | None = "Dana Buyer",
    last_inbound_at: datetime | None = None,
    page: str | None = None,
    participant: str | None = None,
    text: str = "Hi, I need pricing for three vans.",
) -> dict:
    """A connection, a conversation, an inbound message, and optionally a lead.

    Every test needing "a conversation awaiting a follow-up" needs the same six things,
    and building them inline in each test is how fixtures drift apart.

    `page` and `participant` default to unique values, not constants. Meta connections have
    a partial unique index on `(page_id) WHERE status = ACTIVE`, so a test that creates two
    conversations for one tenant must not reuse a page id - the second raises a
    UniqueViolation that has nothing to do with what the test is checking.

    The page id carries the *whole* suffix, not a prefix of it. Truncating it looked fine
    because a single test file creates a handful of connections, but 4096 distinct values
    across a full suite of 800+ tests collides by birthday arithmetic roughly every hundred
    tests - and the collision surfaces as an unrelated `UniqueViolation` in whichever test
    happened to lose, which is close to undebuggable.

    `text` is the inbound body. Phase 8 asserts on it, because the handoff's queue preview
    is the lead's last message and a fixture that always says "hello" cannot test that.
    """
    inbound_at = last_inbound_at or T0
    # Distinct per call, so several conversations can coexist for one tenant.
    suffix = uuid.uuid4().hex[:12]
    page = page or f"100000000{suffix}"
    participant = participant or f"10{suffix}"
    async with owner() as session, session.begin():
        connection = MetaConnection(
            tenant_id=tenant_id,
            page_id=page,
            access_token_encrypted=encrypt(PAGE_TOKEN, key=TEST_META_TOKEN_KEY),
            connected_at=T0 - timedelta(days=30),
        )
        session.add(connection)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant_id,
            meta_connection_id=connection.id,
            channel=Channel.MESSENGER,
            meta_participant_id=participant,
            last_inbound_at=inbound_at,
            last_message_at=inbound_at,
        )
        session.add(conversation)
        await session.flush()
        message = Message(
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            channel=Channel.MESSENGER,
            direction=MessageDirection.INBOUND,
            delivery=MessageDelivery.SENT,
            meta_message_id=f"mid.{uuid.uuid4().hex[:12]}",
            message_type="text",
            text=text,
            sent_at=inbound_at,
        )
        session.add(message)
        await session.flush()
        lead_id = None
        if with_lead:
            lead = Lead(
                tenant_id=tenant_id,
                meta_connection_id=connection.id,
                meta_leadgen_id=f"lg.{uuid.uuid4().hex[:12]}",
                source=LeadSource.META_LEAD_ADS,
                status=lead_status,
                name=lead_name,
            )
            session.add(lead)
            await session.flush()
            conversation.lead_id = lead.id
            lead_id = lead.id
        return {
            "tenant_id": tenant_id,
            "connection_id": connection.id,
            "conversation_id": conversation.id,
            "inbound_message_id": message.id,
            "lead_id": lead_id,
            # Returned so a test that delivers another inbound message targets *this*
            # thread. The participant is unique per call, so hardcoding one would silently
            # open a second conversation and the follow-up would never be cancelled.
            "participant_id": participant,
        }


@pytest.fixture
async def conversation(owner, tenant_row):
    """A page connection, a conversation, an inbound message, and optionally a lead."""
    return await make_conversation(owner, tenant_row)


def service(
    db, tenant_id, clock, *, registry=None, graph=None, context=None, worker=None, **overrides
):
    """A `FollowUpService` wired like the poller's.

    `worker` is the identity whose lease `execute` will verify. It defaults to
    `worker_identity()` - the same value the poller would use - so a test that goes through
    `claim_and_execute` needs nothing. A test that claims under a named worker and then
    executes directly must pass that name, because executing as a *different* worker is
    exactly the stale-lease case the service is meant to refuse.
    """
    from app.services.follow_ups import FollowUpService

    return FollowUpService(
        db,
        tenant_id,
        clock=clock,
        registry=registry,
        context_provider=context,
        graph=graph,
        settings=fu_settings(**overrides),
        worker=worker,
    )


def scheduler(db, tenant_id, clock, *, graph=None, registry=None, context=None, **overrides):
    from app.services.follow_up_scheduler import FollowUpScheduler

    return FollowUpScheduler(
        db,
        tenant_id,
        clock=clock,
        graph=graph,
        registry=registry,
        context_provider=context,
        settings=fu_settings(**overrides),
    )


async def claim_and_execute(
    db, tenant_id, clock, *, graph=None, registry=None, context=None, **overrides
):
    """Claim the due tasks and run them, exactly as the poller does.

    `FollowUpService.execute` deliberately refuses a task that has not been claimed - only
    the poller may claim, and only a claim authorises a send. So a test that wants to watch
    the sequence advance has to go through claiming, the same as production.

    `registry` and `context` are threaded through because the poller constructs its own
    `FollowUpService` per tenant, which is correct in production (no provider state is
    carried between tenants) but means a test cannot inject them anywhere else.
    """
    return await scheduler(
        db,
        tenant_id,
        clock,
        graph=graph,
        registry=registry,
        context=context,
        **overrides,
    ).poll_once()


__all__ = [
    "PAGE",
    "PARTICIPANT",
    "T0",
    "claim_and_execute",
    "fu_settings",
    "make_conversation",
    "service",
    "scheduler",
]
