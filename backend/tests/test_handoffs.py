"""Handoff domain and service behaviour: creation, the state machine, and idempotency.

Everything here runs against real PostgreSQL with the unprivileged app role, because the
guarantees under test *are* database guarantees - the partial unique index and the
conditional `UPDATE` in the claim. A mocked repository would test the mock.
"""

import uuid

import pytest
from sqlalchemy import select

from app.db.tenant_context import set_current_tenant
from app.models.conversation import Conversation, Message, MessageDirection
from app.models.human_handoff import HandoffReason, HandoffStatus
from app.models.internal_notification import NotificationType
from app.models.lead import Lead, LeadStatus
from app.models.membership import Role
from app.repositories import handoffs as handoff_repo
from app.services.handoffs import (
    HandoffAlreadyAssigned,
    HandoffAssignmentNotAllowed,
    HandoffInvalidState,
    HandoffNotFound,
    HandoffResolutionNotAllowed,
)
from tests.fakes import FakeGraph
from tests.follow_up_fixtures import make_conversation
from tests.handoff_fixtures import (
    T0,
    active_handoff,
    count_active,
    handoff_count,
    ho_settings,
    make_handoff,
    make_user,
    notifications_for,
    service,
)

# -- creation ----------------------------------------------------------------


async def test_escalating_a_conversation_creates_one_handoff(db, conversation, clock):
    result = await service(db, conversation["tenant_id"], clock).create_or_get_active_handoff(
        conversation["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )

    assert result.created is True
    assert result.handoff.status is HandoffStatus.OPEN
    assert result.handoff.reason is HandoffReason.HIGH_INTENT
    assert result.handoff.conversation_id == conversation["conversation_id"]
    assert result.handoff.lead_id == conversation["lead_id"]
    # Highest-priority reason sorts first; derived from the reason rather than guessed.
    assert result.handoff.priority == 200


async def test_creating_a_handoff_marks_the_lead(db, conversation, clock):
    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        lead = await session.get(Lead, conversation["lead_id"])
    assert lead.status is LeadStatus.NEW, "the fixture must start a lead at NEW"

    await service(db, conversation["tenant_id"], clock).create_or_get_active_handoff(
        conversation["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        lead = await session.get(Lead, conversation["lead_id"])
    assert lead.status is LeadStatus.HUMAN_HANDOFF


async def test_a_closed_lead_is_not_reopened_by_an_escalation(db, owner, tenant_row, clock):
    """A late escalation must not resurrect a dead deal."""
    world = await make_conversation(owner, tenant_row, lead_status=LeadStatus.CLOSED)

    result = await service(db, tenant_row, clock).create_or_get_active_handoff(
        world["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )

    assert result.created is True, "the handoff itself is still legitimate work"
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        lead = await session.get(Lead, world["lead_id"])
    assert lead.status is LeadStatus.CLOSED


async def test_a_conversation_with_no_lead_can_still_be_escalated(db, owner, tenant_row, clock):
    """Someone messaging a page without submitting a form is an ordinary case."""
    world = await make_conversation(owner, tenant_row, with_lead=False)
    assert world["lead_id"] is None

    result = await service(db, tenant_row, clock).create_or_get_active_handoff(
        world["conversation_id"], reason=HandoffReason.EXPLICIT_HUMAN_REQUEST
    )

    assert result.created is True
    assert result.handoff.lead_id is None


async def test_each_escalation_cause_persists_its_own_reason(db, owner, tenant_row, clock):
    """All four reasons survive a round trip, and priority follows from the reason."""
    from app.db.tenant_context import set_current_tenant as _set

    for reason, expected_priority in (
        (HandoffReason.EXPLICIT_HUMAN_REQUEST, 300),
        (HandoffReason.HIGH_INTENT, 200),
        (HandoffReason.OTHER, 100),
        (HandoffReason.AI_FAILURE, 50),
    ):
        world = await make_conversation(owner, tenant_row)
        result = await service(db, tenant_row, clock).create_or_get_active_handoff(
            world["conversation_id"], reason=reason, detail=f"because {reason.value}"
        )
        assert result.handoff.reason is reason
        assert result.handoff.priority == expected_priority

        async with db() as session, session.begin():
            await _set(session, tenant_row)
            reread = await handoff_repo.get(
                session, tenant_id=tenant_row, handoff_id=result.handoff.id
            )
        assert reread.reason is reason
        assert reread.detail == f"because {reason.value}"


async def test_creation_cancels_pending_follow_ups(db, owner, tenant_row, clock):
    """Handoff supersedes automation, in the same transaction as the insert."""
    from app.models.scheduled_task import TaskStatus
    from app.repositories import scheduled_tasks as task_repo
    from app.services.follow_ups import FollowUpService

    world = await make_conversation(owner, tenant_row)
    svc = service(db, tenant_row, clock)
    # Arm the sequence the way the AI pipeline would.
    await FollowUpService(
        db, tenant_row, settings=ho_settings(), clock=clock, graph=FakeGraph()
    ).on_reply_sent(world["conversation_id"])

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        before = await task_repo.get_by_conversation(
            session, tenant_id=tenant_row, conversation_id=world["conversation_id"]
        )
    assert [t.status for t in before] == [TaskStatus.PENDING], "fixture must arm a real task"

    result = await svc.create_or_get_active_handoff(
        world["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )

    assert result.follow_ups_cancelled == 1
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        after = await task_repo.get_by_conversation(
            session, tenant_id=tenant_row, conversation_id=world["conversation_id"]
        )
    assert [t.status for t in after] == [TaskStatus.CANCELLED]


# -- idempotency -------------------------------------------------------------


async def test_a_second_escalation_returns_the_first(db, conversation, clock):
    svc = service(db, conversation["tenant_id"], clock)
    first = await svc.create_or_get_active_handoff(
        conversation["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )
    second = await svc.create_or_get_active_handoff(
        conversation["conversation_id"], reason=HandoffReason.EXPLICIT_HUMAN_REQUEST
    )

    assert second.created is False
    assert second.handoff.id == first.handoff.id
    # The original reason is kept: a later, weaker signal must not rewrite why the
    # conversation was escalated, or the audit trail stops meaning anything.
    assert second.handoff.reason is HandoffReason.HIGH_INTENT
    assert await handoff_count(db, conversation["tenant_id"]) == 1


async def test_repeated_escalation_produces_one_notification(db, conversation, clock):
    svc = service(db, conversation["tenant_id"], clock)
    results = [
        await svc.create_or_get_active_handoff(
            conversation["conversation_id"], reason=HandoffReason.HIGH_INTENT
        )
        for _ in range(5)
    ]

    handoff = results[0].handoff
    assert sum(r.created for r in results) == 1
    notes = await notifications_for(db, conversation["tenant_id"], handoff.id)
    assert [n.type for n in notes] == [NotificationType.HANDOFF_CREATED]


async def test_disabled_handoffs_create_nothing(db, conversation, clock):
    svc = service(db, conversation["tenant_id"], clock, handoff_enabled=False)

    result = await svc.create_or_get_active_handoff(
        conversation["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )

    assert result is None
    assert await handoff_count(db, conversation["tenant_id"]) == 0
    assert (
        await service(db, conversation["tenant_id"], clock).has_active(
            conversation["conversation_id"]
        )
        is False
    )


async def test_an_unknown_conversation_is_reported_as_no_handoff(db, tenant_row, clock):
    result = await service(db, tenant_row, clock).create_or_get_active_handoff(
        uuid.uuid4(), reason=HandoffReason.HIGH_INTENT
    )

    assert result is None
    assert await handoff_count(db, tenant_row) == 0


# -- acknowledgement ---------------------------------------------------------


async def test_the_acknowledgement_is_sent_to_the_lead(db, conversation, clock):
    graph = FakeGraph()
    svc, creation, _ = await make_handoff(db, conversation["tenant_id"], conversation, graph=graph)

    assert len(graph.sends) == 1
    assert "team" in graph.sends[0]["text"]

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        handoff = await handoff_repo.get(
            session, tenant_id=conversation["tenant_id"], handoff_id=creation.handoff.id
        )
    assert handoff.acknowledged_at is not None
    assert handoff.acknowledgement_source == "TEMPLATE"
    assert handoff.acknowledgement_failure is None


async def test_the_ai_reply_is_accepted_as_the_acknowledgement(db, conversation, clock):
    """No second message when the AI's own reply already reached the lead."""
    graph = FakeGraph()
    svc = service(db, conversation["tenant_id"], clock)
    creation = await svc.create_or_get_active_handoff(
        conversation["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )
    conversation_model = await _conversation(db, conversation)
    await svc.acknowledge(
        creation.handoff.id,
        conversation=conversation_model,
        graph=graph,
        already_delivered=True,
    )

    assert graph.sends == [], "the AI's message is the acknowledgement; do not duplicate it"
    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        handoff = await handoff_repo.get(
            session, tenant_id=conversation["tenant_id"], handoff_id=creation.handoff.id
        )
    assert handoff.acknowledged_at is not None
    assert handoff.acknowledgement_source == "AI_REPLY"


async def test_a_failed_acknowledgement_keeps_the_handoff_and_notifies(db, conversation, clock):
    from app.services.meta_graph import MetaTransientError

    # Programmed as a statement, not chained: `program_send_error` returns None, so
    # `FakeGraph().program_send_error(...)` would leave `graph` as None.
    graph = FakeGraph()
    graph.program_send_error(MetaTransientError("upstream down"))
    svc = service(db, conversation["tenant_id"], clock)
    creation = await svc.create_or_get_active_handoff(
        conversation["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )
    conversation_model = await _conversation(db, conversation)

    delivered = await svc.acknowledge(
        creation.handoff.id, conversation=conversation_model, graph=graph, already_delivered=False
    )

    assert delivered is False
    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        handoff = await handoff_repo.get(
            session, tenant_id=conversation["tenant_id"], handoff_id=creation.handoff.id
        )
    # The human still has work to do...
    assert handoff.status is HandoffStatus.OPEN
    assert handoff.acknowledged_at is None
    # ... and can see that the lead was not told.
    assert handoff.acknowledgement_failure is not None
    notes = await notifications_for(db, conversation["tenant_id"], handoff.id)
    assert NotificationType.HANDOFF_ACK_FAILED in [n.type for n in notes]


async def test_a_duplicate_escalation_sends_no_second_acknowledgement(db, conversation, clock):
    """The rule the whole design turns on, tested the way the pipeline applies it.

    `acknowledge` itself is a low-level send and does not pretend to be idempotent - it is
    only ever called by the thread that created the handoff. The idempotency lives one level
    up, in `create_or_get_active_handoff` returning `created=False`, which is exactly how
    `AIConversationService._escalate` decides whether to acknowledge at all.
    """
    graph = FakeGraph()
    svc = service(db, conversation["tenant_id"], clock)
    conversation_model = await _conversation(db, conversation)

    for _ in range(4):
        creation = await svc.create_or_get_active_handoff(
            conversation["conversation_id"], reason=HandoffReason.HIGH_INTENT
        )
        if creation.created:
            await svc.acknowledge(
                creation.handoff.id,
                conversation=conversation_model,
                graph=graph,
                already_delivered=False,
            )

    assert len(graph.sends) == 1, "one lead, one acknowledgement"
    notes = await notifications_for(db, conversation["tenant_id"], creation.handoff.id)
    assert len(notes) == 1, "one handoff, one notification"


# -- state machine -----------------------------------------------------------


async def test_claim_assigns_the_caller(db, owner, tenant_row, conversation, clock):
    agent = await make_user(owner, role=Role.MEMBER, tenant_id=tenant_row)
    _, creation, _ = await make_handoff(db, tenant_row, conversation)

    claimed = await service(db, tenant_row, clock).claim(creation.handoff.id, agent.id)

    assert claimed.status is HandoffStatus.ASSIGNED
    assert claimed.assigned_user_id == agent.id
    assert claimed.assigned_at is not None


async def test_a_second_claim_is_refused_and_names_the_winner(
    db, owner, tenant_row, conversation, clock
):
    first_agent = await make_user(owner, role=Role.MEMBER, tenant_id=tenant_row)
    second_agent = await make_user(owner, role=Role.MEMBER, tenant_id=tenant_row)
    _, creation, _ = await make_handoff(db, tenant_row, conversation)
    svc = service(db, tenant_row, clock)

    await svc.claim(creation.handoff.id, first_agent.id)
    with pytest.raises(HandoffAlreadyAssigned) as taken:
        await svc.claim(creation.handoff.id, second_agent.id)

    assert taken.value.assigned_user_id == first_agent.id


async def test_assign_puts_a_handoff_on_a_named_agent(db, owner, tenant_row, conversation, clock):
    agent = await make_user(owner, role=Role.MEMBER, tenant_id=tenant_row)
    _, creation, _ = await make_handoff(db, tenant_row, conversation)

    assigned = await service(db, tenant_row, clock).assign(creation.handoff.id, agent.id)

    assert assigned.status is HandoffStatus.ASSIGNED
    assert assigned.assigned_user_id == agent.id


async def test_assign_rejects_a_user_from_another_tenant(
    db, owner, tenant_row, conversation, clock
):
    """`users` is global, so a valid id from another tenant exists and must be refused."""
    other_tenant = await create_tenant_for_test(owner)
    stranger = await make_user(owner, role=Role.ADMIN, tenant_id=other_tenant)
    _, creation, _ = await make_handoff(db, tenant_row, conversation)

    with pytest.raises(HandoffAssignmentNotAllowed):
        await service(db, tenant_row, clock).assign(creation.handoff.id, stranger.id)

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        handoff = await handoff_repo.get(
            session, tenant_id=tenant_row, handoff_id=creation.handoff.id
        )
    assert handoff.assigned_user_id is None, "a rejected assignment must not half-apply"


async def test_start_moves_to_in_progress(db, owner, tenant_row, conversation, clock):
    agent = await make_user(owner, role=Role.MEMBER, tenant_id=tenant_row)
    _, creation, _ = await make_handoff(db, tenant_row, conversation)
    svc = service(db, tenant_row, clock)
    await svc.claim(creation.handoff.id, agent.id)

    started = await svc.start(creation.handoff.id)

    assert started.status is HandoffStatus.IN_PROGRESS


async def test_resolve_works_from_every_active_state(db, owner, tenant_row, clock):
    """Not forced through the UI's buttons: a handoff can be closed without being claimed."""
    agent = await make_user(owner, role=Role.MEMBER, tenant_id=tenant_row)
    for claimed, started in ((False, False), (True, False), (True, True)):
        world = await make_conversation(owner, tenant_row)
        _, creation, _ = await make_handoff(db, tenant_row, world)
        svc = service(db, tenant_row, clock)
        if claimed:
            await svc.claim(creation.handoff.id, agent.id)
        if started:
            await svc.start(creation.handoff.id)

        resolved = await svc.resolve(creation.handoff.id, note="handled by phone")

        assert resolved.status is HandoffStatus.RESOLVED
        assert resolved.resolved_at is not None
        assert resolved.resolution_note == "handled by phone"


async def test_a_resolved_handoff_cannot_be_reopened(db, owner, tenant_row, conversation, clock):
    agent = await make_user(owner, role=Role.MEMBER, tenant_id=tenant_row)
    _, creation, _ = await make_handoff(db, tenant_row, conversation)
    svc = service(db, tenant_row, clock)
    await svc.resolve(creation.handoff.id)

    with pytest.raises(HandoffResolutionNotAllowed):
        await svc.resolve(creation.handoff.id)
    with pytest.raises(HandoffInvalidState):
        await svc.start(creation.handoff.id)
    with pytest.raises(HandoffInvalidState):
        await svc.claim(creation.handoff.id, agent.id)
    with pytest.raises(HandoffInvalidState):
        await svc.assign(creation.handoff.id, agent.id)


async def test_a_new_escalation_is_allowed_after_a_resolved_one(db, owner, tenant_row, clock):
    """The difference from Phase 7's permanent uniqueness, and the reason it is partial.

    A resolved escalation is history. A later one is new work for a human, and a permanent
    unique key would silently swallow it.
    """
    first = await make_conversation(owner, tenant_row)
    _, creation, _ = await make_handoff(db, tenant_row, first)
    svc = service(db, tenant_row, clock)
    await svc.resolve(creation.handoff.id)

    second = await make_conversation(owner, tenant_row)
    again = await svc.create_or_get_active_handoff(
        second["conversation_id"], reason=HandoffReason.EXPLICIT_HUMAN_REQUEST
    )

    assert again.created is True
    assert again.handoff.id != creation.handoff.id
    assert await handoff_count(db, tenant_row) == 2
    assert await count_active(db, tenant_row, second["conversation_id"]) == 1


async def test_resolving_a_missing_handoff_raises_not_found(db, tenant_row, clock):
    with pytest.raises(HandoffNotFound):
        await service(db, tenant_row, clock).resolve(uuid.uuid4())


# -- notification content ----------------------------------------------------


async def test_the_notification_carries_enough_to_triage(db, conversation, clock):
    _, creation, _ = await make_handoff(db, conversation["tenant_id"], conversation)
    notes = await notifications_for(db, conversation["tenant_id"], creation.handoff.id)
    created = next(n for n in notes if n.type is NotificationType.HANDOFF_CREATED)

    payload = created.payload
    assert payload["lead_name"] == "Dana Buyer"
    assert payload["conversation_id"] == str(conversation["conversation_id"])
    assert payload["reason"] == HandoffReason.HIGH_INTENT.value
    assert payload["lead_identifier"], "the agent needs a way to identify the lead"
    assert payload["latest_inbound_text"], "and to see what they said"
    assert payload["assigned_user_id"] is None
    assert created.title and created.message


async def test_the_notification_contains_no_secrets(db, conversation, clock):
    _, creation, _ = await make_handoff(db, conversation["tenant_id"], conversation)
    notes = await notifications_for(db, conversation["tenant_id"], creation.handoff.id)

    blob = repr([(n.title, n.message, n.payload) for n in notes])
    for forbidden in ("access_token", "encrypted", "api_key", "Bearer", "secret", "embedding"):
        assert forbidden not in blob


async def test_a_long_inbound_message_is_truncated_in_the_preview(db, owner, tenant_row, clock):
    world = await make_conversation(owner, tenant_row, text="x" * 5000)
    result = await service(db, tenant_row, clock).create_or_get_active_handoff(
        world["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )

    assert len(result.handoff.latest_inbound_text) <= ho_settings().handoff_preview_chars


# -- helpers -----------------------------------------------------------------


async def _conversation(db, world):
    async with db() as session:
        await set_current_tenant(session, world["tenant_id"])
        return await session.scalar(
            select(Conversation).where(
                Conversation.id == world["conversation_id"],
                Conversation.tenant_id == world["tenant_id"],
            )
        )


async def create_tenant_for_test(owner):
    from tests.factories import create_tenant

    return await create_tenant(owner, name="Initech", slug=f"initech-{uuid.uuid4().hex[:8]}")


async def test_a_human_reply_does_not_trigger_the_ai(db, owner, tenant_row, clock):
    """Spec §16: a human outbound message must not re-enter the AI path.

    Structurally true today - the send endpoint calls `ConversationEngine.send_outbound`
    and nothing else - and asserted here so a future "reply and also let the AI weigh in"
    convenience cannot be added without this failing.
    """
    from sqlalchemy import func
    from sqlalchemy import select as sa_select

    from app.models.ai_message_run import AIMessageRun
    from app.services.conversations import ConversationEngine

    world = await make_conversation(owner, tenant_row)
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        conversation = await session.get(Conversation, world["conversation_id"])
        engine = ConversationEngine(session, tenant_row, graph=FakeGraph(), settings=ho_settings())
        sent = await engine.send_outbound(conversation, text="Hi, this is Sam from the team.")

    assert sent.delivery.value == "SENT"
    assert sent.ai_model is None, "a human message is not AI-attributed"

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        runs = await session.scalar(
            sa_select(func.count())
            .select_from(AIMessageRun)
            .where(AIMessageRun.conversation_id == world["conversation_id"])
        )
    assert runs == 0, "an outbound human message creates no AI run"


async def test_the_latest_inbound_is_kept_on_the_handoff(db, owner, tenant_row, clock):
    from app.models.conversation import Channel, MessageDelivery

    world = await make_conversation(owner, tenant_row)
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        session.add(
            Message(
                tenant_id=tenant_row,
                conversation_id=world["conversation_id"],
                channel=Channel.MESSENGER,
                direction=MessageDirection.INBOUND,
                delivery=MessageDelivery.SENT,
                meta_message_id=f"mid.{uuid.uuid4().hex[:10]}",
                message_type="text",
                text="actually, I want to talk to a person",
                sent_at=T0,
            )
        )

    result = await service(db, tenant_row, clock).create_or_get_active_handoff(
        world["conversation_id"], reason=HandoffReason.EXPLICIT_HUMAN_REQUEST
    )

    assert result.handoff.latest_inbound_text == "actually, I want to talk to a person"
    assert result.handoff.latest_inbound_at is not None


async def test_has_active_reflects_the_lifecycle(db, owner, tenant_row, clock):
    world = await make_conversation(owner, tenant_row)
    svc = service(db, tenant_row, clock)
    conversation_id = world["conversation_id"]

    assert await svc.has_active(conversation_id) is False
    creation = await svc.create_or_get_active_handoff(
        conversation_id, reason=HandoffReason.HIGH_INTENT
    )
    assert await svc.has_active(conversation_id) is True

    await svc.resolve(creation.handoff.id)
    assert await svc.has_active(conversation_id) is False
    assert await active_handoff(db, tenant_row, conversation_id) is None
