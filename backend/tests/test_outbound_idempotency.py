"""Outbound idempotency and the ambiguity that cannot be engineered away.

What is guaranteed here, precisely:

* **One successful outbound per scheduled task, enforced by the database.** A partial unique
  index on `messages` means a duplicate send cannot be *recorded*, so a service-layer
  regression surfaces as a constraint violation rather than a second message to a customer.
* **A crashed worker's send is not repeated.** The message row is written before the call and
  attributed to the task, so a replacement worker can tell "already sent" from "never sent".
* **A timeout is terminal; a rate limit is not.** These were one exception type and are now
  two, because they mean opposite things for a resend.

What is NOT guaranteed, and is asserted here so nobody assumes it:

* **Remote exactly-once.** Meta's Send API takes no idempotency key. If a request times out
  and Meta accepted it, the only truthful state is "unknown", and the task is left FAILED for
  reconciliation rather than blindly resent. A duplicate message to a real prospect is worse
  than a missed follow-up, and that is a deliberate trade.
"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.tenant_context import set_current_tenant
from app.models.conversation import (
    Channel,
    Conversation,
    Message,
    MessageDelivery,
    MessageDirection,
)
from app.models.scheduled_task import ScheduledTask, TaskStatus, TaskType
from app.repositories import conversations as conversation_repo
from app.services.conversations import (
    REASON_META_UNAVAILABLE,
    REASON_OUTCOME_UNKNOWN,
    ConversationEngine,
)
from app.services.meta_graph import MetaTransientError, MetaUnknownOutcomeError
from tests.fakes import FakeGraph
from tests.follow_up_fixtures import claim_and_execute, service

pytestmark = pytest.mark.integration

LEASE = 60


async def _arm(db, world, clock, **overrides) -> FakeGraph:
    graph = FakeGraph()
    await service(db, world["tenant_id"], clock, graph=graph, **overrides).on_reply_sent(
        world["conversation_id"]
    )
    return graph


async def _step_one(db, world) -> ScheduledTask:
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant_id"])
        return await session.scalar(
            select(ScheduledTask).where(
                ScheduledTask.tenant_id == world["tenant_id"],
                ScheduledTask.task_type == TaskType.FOLLOW_UP,
                ScheduledTask.sequence_step == 1,
            )
        )


async def _claim(db, world, clock, worker: str) -> list:
    from app.repositories import scheduled_tasks as task_repo

    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant_id"])
        return await task_repo.claim_due(
            session,
            tenant_id=world["tenant_id"],
            now=clock.now(),
            limit=10,
            worker=worker,
            lease_seconds=LEASE,
        )


# -- one successful send per task, enforced by the database -------------------


async def test_the_database_refuses_a_second_successful_outbound_for_one_task(
    db, conversation, clock
):
    """The guarantee is a constraint, not a convention."""
    await _arm(db, conversation, clock)
    task = await _step_one(db, conversation)

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        first = await conversation_repo.create_outbound_pending(
            session,
            tenant_id=conversation["tenant_id"],
            conversation_id=conversation["conversation_id"],
            channel=Channel.MESSENGER,
            text="just checking in",
            sent_at=clock.now(),
            scheduled_task_id=task.id,
        )
        await conversation_repo.mark_message_sent(
            session, first, meta_message_id="mid.1", at=clock.now()
        )

    # A second, successful outbound for the same task cannot be written.
    with pytest.raises(IntegrityError):
        async with db() as session, session.begin():
            await set_current_tenant(session, conversation["tenant_id"])
            second = await conversation_repo.create_outbound_pending(
                session,
                tenant_id=conversation["tenant_id"],
                conversation_id=conversation["conversation_id"],
                channel=Channel.MESSENGER,
                text="just checking in",
                sent_at=clock.now(),
                scheduled_task_id=task.id,
            )
            await conversation_repo.mark_message_sent(
                session, second, meta_message_id="mid.2", at=clock.now()
            )


async def test_failed_attempts_do_not_block_a_retry(db, conversation, clock):
    """Several failures then one success is a legitimate sequence, not a duplicate."""
    await _arm(db, conversation, clock)
    task = await _step_one(db, conversation)

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        for index in range(2):
            failed = await conversation_repo.create_outbound_pending(
                session,
                tenant_id=conversation["tenant_id"],
                conversation_id=conversation["conversation_id"],
                channel=Channel.MESSENGER,
                text="attempt",
                sent_at=clock.now() + timedelta(seconds=index),
                scheduled_task_id=task.id,
            )
            await conversation_repo.mark_message_failed(session, failed, reason="timeout")
        ok = await conversation_repo.create_outbound_pending(
            session,
            tenant_id=conversation["tenant_id"],
            conversation_id=conversation["conversation_id"],
            channel=Channel.MESSENGER,
            text="attempt",
            sent_at=clock.now(),
            scheduled_task_id=task.id,
        )
        await conversation_repo.mark_message_sent(
            session, ok, meta_message_id="mid.ok", at=clock.now()
        )


async def test_a_pending_attempt_is_not_evidence_of_delivery(db, conversation, clock):
    """A worker that died after recording intent must still be allowed to try again."""
    await _arm(db, conversation, clock)
    task = await _step_one(db, conversation)

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        await conversation_repo.create_outbound_pending(
            session,
            tenant_id=conversation["tenant_id"],
            conversation_id=conversation["conversation_id"],
            channel=Channel.MESSENGER,
            text="attempt",
            sent_at=clock.now(),
            scheduled_task_id=task.id,
        )
        found = await conversation_repo.task_already_sent(
            session, tenant_id=conversation["tenant_id"], scheduled_task_id=task.id
        )
    assert found is None, "a PENDING row must not be read as a delivery"


# -- crash after the send ----------------------------------------------------


async def test_a_task_whose_send_already_went_out_is_not_sent_again(db, conversation, clock):
    """The worker died after Meta accepted the message and before the task completed.

    The replacement worker must complete the task from the durable record rather than
    messaging the lead a second time.
    """
    graph = await _arm(db, conversation, clock)
    task = await _step_one(db, conversation)
    clock.advance(hours=4)
    settings = {"follow_up_processing_lease_seconds": LEASE}
    svc = service(db, conversation["tenant_id"], clock, graph=graph, worker="worker-a", **settings)

    assert task.id in await _claim(db, conversation, clock, "worker-a")
    first = await svc.execute(task.id)
    assert first.status == "COMPLETED"
    assert len(graph.sends) == 1

    # Simulate the crash the ownership check cannot cover: the task is forced back to
    # PENDING as if the completion had never been written, and a new worker picks it up.
    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        await session.execute(
            ScheduledTask.__table__.update()
            .where(ScheduledTask.id == task.id)
            .values(status=TaskStatus.PENDING, completed_at=None, sent_text=None)
        )

    clock.advance(hours=4)
    recovered = service(
        db, conversation["tenant_id"], clock, graph=graph, worker="worker-b", **settings
    )
    # B claims and executes in the poller's own order.
    assert task.id in await _claim(db, conversation, clock, "worker-b")
    outcome = await recovered.execute(task.id)

    assert len(graph.sends) == 1, "the lead received the follow-up twice"
    assert outcome.status == "COMPLETED"
    assert outcome.reason == "already_sent"

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        row = await session.get(ScheduledTask, task.id)
    assert row.status is TaskStatus.COMPLETED


# -- the two meanings of "Meta did not answer well" --------------------------


async def test_a_timeout_is_recorded_as_an_unknown_outcome_not_a_transient(db, conversation, clock):
    """Ambiguous delivery gets its own reason so callers cannot retry it by accident."""
    graph = FakeGraph()
    graph.program_send_error(MetaUnknownOutcomeError("timed out"))
    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        engine = ConversationEngine(session, conversation["tenant_id"], graph=graph)
        conv = await session.get(Conversation, conversation["conversation_id"])
        message = await engine.send_outbound(conv, text="just checking in")

    assert message.delivery is MessageDelivery.FAILED
    assert message.failure_reason == REASON_OUTCOME_UNKNOWN


async def test_a_rate_limit_is_recorded_as_retryable_unavailability(db, conversation, clock):
    graph = FakeGraph()
    graph.program_send_error(MetaTransientError("rate limited"))
    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        engine = ConversationEngine(session, conversation["tenant_id"], graph=graph)
        conv = await session.get(Conversation, conversation["conversation_id"])
        message = await engine.send_outbound(conv, text="just checking in")

    assert message.failure_reason == REASON_META_UNAVAILABLE
    assert message.failure_reason != REASON_OUTCOME_UNKNOWN


async def test_an_unknown_outcome_is_never_retried_by_the_follow_up_engine(db, conversation, clock):
    """End to end: a timed-out follow-up goes terminal, it does not come back round."""
    graph = FakeGraph()
    graph.program_send_error(MetaUnknownOutcomeError("timed out"))
    await service(
        db, conversation["tenant_id"], clock, graph=graph, follow_up_ai_enhance=False
    ).on_reply_sent(conversation["conversation_id"])
    clock.advance(hours=4)

    result = await claim_and_execute(
        db, conversation["tenant_id"], clock, graph=graph, follow_up_ai_enhance=False
    )

    assert result.claimed == 1
    assert result.completed == 0, "an unknown outcome must not be recorded as delivered"
    assert result.failed == 1

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        # The durable record, not the fake's call log: the attempt *was* made, so `sends` is
        # non-empty, but nothing was delivered and that is what must be true in the database.
        delivered = await session.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.tenant_id == conversation["tenant_id"],
                # Outbound only: an *inbound* message is also SENT - it arrived successfully -
                # and counting it would make this assertion pass or fail for the wrong reason.
                Message.direction == MessageDirection.OUTBOUND,
                Message.delivery == MessageDelivery.SENT,
            )
        )
        assert delivered == 0, "a timed-out send must not be recorded as delivered"
        row = await session.scalar(
            select(ScheduledTask).where(
                ScheduledTask.tenant_id == conversation["tenant_id"],
                ScheduledTask.task_type == TaskType.FOLLOW_UP,
            )
        )
    assert row.status is TaskStatus.FAILED, "a timeout must be terminal, not retried"
    assert row.last_error == REASON_OUTCOME_UNKNOWN


async def test_a_rate_limit_follow_up_is_retried_with_backoff(db, conversation, clock):
    """The other side of the split: a definite non-delivery *is* retried, and bounded."""
    graph = FakeGraph()
    graph.program_send_error(MetaTransientError("rate limited"))
    await service(
        db, conversation["tenant_id"], clock, graph=graph, follow_up_ai_enhance=False
    ).on_reply_sent(conversation["conversation_id"])
    clock.advance(hours=4)

    await claim_and_execute(
        db, conversation["tenant_id"], clock, graph=graph, follow_up_ai_enhance=False
    )

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        row = await session.scalar(
            select(ScheduledTask).where(
                ScheduledTask.tenant_id == conversation["tenant_id"],
                ScheduledTask.task_type == TaskType.FOLLOW_UP,
            )
        )
        before = (row.status, row.scheduled_at, row.attempt_count)

    assert before[0] is TaskStatus.PENDING, "a rate limit is safely retryable"
    assert before[2] == 1
    assert before[1] > clock.now(), "the retry must be pushed into the future, not hot-looped"
