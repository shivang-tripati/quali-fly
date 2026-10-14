"""Phase 5 -> Phase 8: the handoff signal, AI suppression, and the late-result guard.

These drive the *whole* pipeline - `AIConversationService.process_inbound_message` - rather
than the handoff service directly, because the integration point is the thing most likely
to be wrong and the thing no unit test can see. Three behaviours are load-bearing:

  * an escalation creates a handoff and does *not* arm a follow-up sequence
  * a conversation already in human hands gets no autonomous reply
  * an AI run that started before a handoff and finished after it sends nothing
"""

import uuid

from app.core.clock import FixedClock
from app.db.tenant_context import set_current_tenant
from app.models.ai_message_run import AIMessageRun, AIRunStatus
from app.models.human_handoff import HandoffReason, HandoffStatus
from app.models.lead import Lead, LeadStatus
from app.models.scheduled_task import TaskStatus, TaskType
from app.repositories import handoffs as handoff_repo
from app.services.ai_qualification import REASON_HANDOFF_ACTIVE, AIConversationService
from app.services.handoffs import HumanHandoffService
from tests.fakes import FakeGraph, FakeRegistry
from tests.follow_up_fixtures import make_conversation
from tests.handoff_fixtures import (
    T0,
    count_active,
    handoff_count,
    ho_settings,
    make_handoff,
)

# A total above the default 75 threshold, or an explicit `human_handoff`, depending on
# which test is exercising which path.
HIGH = dict(budget=20, timeline=20, fit=20, authority=20)
ASKING = dict(
    budget=2, timeline=2, fit=2, authority=1, human_handoff=True, handoff_reason="wants a call"
)
CALM = dict(budget=2, timeline=2, fit=2, authority=1)


def ai_service(db, tenant_id, registry, graph=None, clock=None):
    return AIConversationService(
        db,
        tenant_id,
        registry=registry,
        graph=graph or FakeGraph(),
        settings=ho_settings(),
        clock=clock or FixedClock(T0),
    )


async def _tasks(db, tenant_id, conversation_id):
    from app.repositories import scheduled_tasks as task_repo

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        return await task_repo.get_by_conversation(
            session, tenant_id=tenant_id, conversation_id=conversation_id
        )


# -- the three escalation sources --------------------------------------------


async def test_high_intent_creates_a_handoff(db, owner, tenant_row):
    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()

    result = await ai_service(
        db, tenant_row, FakeRegistry().program_response(**HIGH), graph
    ).process_inbound_message(world["inbound_message_id"])

    assert result.human_handoff_required is True
    handoff = await _active(db, tenant_row, world["conversation_id"])
    assert handoff is not None
    assert handoff.reason is HandoffReason.HIGH_INTENT
    assert handoff.qualification_total == 80
    assert handoff.detail and "threshold" in handoff.detail


async def test_an_explicit_request_creates_a_handoff(db, owner, tenant_row):
    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()

    await ai_service(
        db, tenant_row, FakeRegistry().program_response(**ASKING), graph
    ).process_inbound_message(world["inbound_message_id"])

    handoff = await _active(db, tenant_row, world["conversation_id"])
    assert handoff.reason is HandoffReason.HIGH_INTENT
    assert handoff.detail == "wants a call"


async def test_an_explicit_request_is_attributed_to_the_ask(db, owner, tenant_row):
    """The AI's own `handoff_reason` is preserved as detail; the machine reason is ours.

    The pipeline only tells us `human_handoff` is true, not *why* the model set it, and
    Phase 5 does not distinguish "the model asked" from "the score crossed". It attributes
    to HIGH_INTENT for both, because the observable signal is the same and inventing a
    distinction the pipeline does not make would be a fiction in the audit trail.
    """
    world = await make_conversation(owner, tenant_row)
    await ai_service(
        db, tenant_row, FakeRegistry().program_response(**ASKING), FakeGraph()
    ).process_inbound_message(world["inbound_message_id"])

    handoff = await _active(db, tenant_row, world["conversation_id"])
    assert handoff.reason is HandoffReason.HIGH_INTENT
    assert handoff.detail == "wants a call"


async def test_an_ai_failure_creates_a_handoff_with_no_ai_reply(db, owner, tenant_row):
    from app.services.llm_base import LLMTransientError

    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()
    registry = FakeRegistry().program(LLMTransientError("upstream timeout"))

    result = await ai_service(db, tenant_row, registry, graph).process_inbound_message(
        world["inbound_message_id"]
    )

    assert result.status == AIRunStatus.FAILED
    handoff = await _active(db, tenant_row, world["conversation_id"])
    assert handoff.reason is HandoffReason.AI_FAILURE
    # The failure *category*, not a provider payload - the same discipline Phase 5 uses
    # for `ai_message_runs.error_category`.
    assert handoff.detail == "ai transient"
    # No AI reply, and exactly one message to the lead: the deterministic acknowledgement.
    assert len(graph.sends) == 1
    assert "team" in graph.sends[0]["text"]
    assert handoff.acknowledgement_source == "TEMPLATE"


async def test_a_healthy_ai_reply_creates_no_handoff(db, owner, tenant_row):
    world = await make_conversation(owner, tenant_row)

    result = await ai_service(
        db, tenant_row, FakeRegistry().program_response(**CALM), FakeGraph()
    ).process_inbound_message(world["inbound_message_id"])

    assert result.human_handoff_required is False
    assert await handoff_count(db, tenant_row) == 0


async def test_the_ai_reply_is_the_acknowledgement_so_only_one_message_is_sent(
    db, owner, tenant_row
):
    """A handoff must not produce a second "we'll be in touch" on top of the AI's reply."""
    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()

    await ai_service(
        db, tenant_row, FakeRegistry().program_response(**HIGH), graph
    ).process_inbound_message(world["inbound_message_id"])

    assert len(graph.sends) == 1, "the AI's reply served as the acknowledgement"
    handoff = await _active(db, tenant_row, world["conversation_id"])
    assert handoff.acknowledgement_source == "AI_REPLY"
    assert handoff.acknowledged_at is not None


# -- follow-up interaction ---------------------------------------------------


async def test_an_escalation_does_not_arm_a_follow_up_sequence(db, owner, tenant_row):
    """Handoff replaces the sequence rather than sitting beside it."""
    world = await make_conversation(owner, tenant_row)

    await ai_service(
        db, tenant_row, FakeRegistry().program_response(**HIGH), FakeGraph()
    ).process_inbound_message(world["inbound_message_id"])

    assert await _tasks(db, tenant_row, world["conversation_id"]) == []


async def test_an_escalation_cancels_a_sequence_that_was_already_armed(db, owner, tenant_row):
    """The AI answered, armed a sequence, and a *second* message escalated it."""
    from app.services.follow_ups import FollowUpService

    world = await make_conversation(owner, tenant_row, with_lead=True)
    await FollowUpService(
        db, tenant_row, settings=ho_settings(), clock=FixedClock(T0), graph=FakeGraph()
    ).on_reply_sent(world["conversation_id"])
    assert [t.status for t in await _tasks(db, tenant_row, world["conversation_id"])] == [
        TaskStatus.PENDING
    ]

    await HumanHandoffService(
        db, tenant_row, settings=ho_settings(), clock=FixedClock(T0)
    ).create_or_get_active_handoff(world["conversation_id"], reason=HandoffReason.HIGH_INTENT)

    tasks = await _tasks(db, tenant_row, world["conversation_id"])
    assert [t.status for t in tasks] == [TaskStatus.CANCELLED]


# -- AI suppression during an active handoff ---------------------------------


async def test_a_new_inbound_during_a_handoff_gets_no_autonomous_reply(db, owner, tenant_row):
    """Spec §17: the message is stored, the AI stands down, the human sees it."""
    from app.models.conversation import Channel, Message, MessageDelivery, MessageDirection

    world = await make_conversation(owner, tenant_row)
    await make_handoff(db, tenant_row, world, graph=FakeGraph())
    graph = FakeGraph()
    registry = FakeRegistry().program_response(**CALM)

    # A second message arrives while the handoff is open.
    new_id = uuid.uuid4()
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        session.add(
            Message(
                id=new_id,
                tenant_id=tenant_row,
                conversation_id=world["conversation_id"],
                channel=Channel.MESSENGER,
                direction=MessageDirection.INBOUND,
                delivery=MessageDelivery.SENT,
                meta_message_id=f"mid.{uuid.uuid4().hex[:12]}",
                message_type="text",
                text="are you there?",
                sent_at=T0,
            )
        )

    result = await ai_service(db, tenant_row, registry, graph).process_inbound_message(new_id)

    assert result.status == AIRunStatus.SKIPPED
    assert result.reason == REASON_HANDOFF_ACTIVE
    assert graph.sends == [], "the AI must not answer a conversation a human owns"
    assert registry.requests == [], "and must not spend a provider call doing it"
    # The inbound is still stored, so the human can see it.
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        stored = await session.get(Message, new_id)
    assert stored is not None
    # ... and the run is recorded, so the suppression is auditable rather than invisible.
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        run = (
            (
                await session.execute(
                    __import__("sqlalchemy")
                    .select(AIMessageRun)
                    .where(AIMessageRun.inbound_message_id == new_id)
                )
            )
            .scalars()
            .one()
        )
    assert run.status.value == "SKIPPED"
    assert run.error_category == REASON_HANDOFF_ACTIVE


async def test_a_late_ai_result_sends_nothing(db, owner, tenant_row):
    """Spec §24 case E: the run started before the handoff and finished after it.

    A `FakeRegistry` that creates the handoff *while it is generating* reproduces exactly
    that interleaving - the run has already passed the pre-provider check, and the human
    escalation lands while the "provider" is thinking.
    """
    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()
    service = ai_service(db, tenant_row, _EscalatingDuringGeneration(db, tenant_row, world), graph)

    result = await service.process_inbound_message(world["inbound_message_id"])

    assert result.status == AIRunStatus.SKIPPED
    assert result.reason == REASON_HANDOFF_ACTIVE
    assert graph.sends == [], "a late AI result must not answer over a live handoff"
    handoff = await _active(db, tenant_row, world["conversation_id"])
    assert handoff is not None and handoff.status is HandoffStatus.OPEN


async def test_a_late_ai_result_cannot_regress_the_lead_status(db, owner, tenant_row):
    world = await make_conversation(owner, tenant_row)
    service = ai_service(
        db, tenant_row, _EscalatingDuringGeneration(db, tenant_row, world), FakeGraph()
    )

    await service.process_inbound_message(world["inbound_message_id"])

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        lead = await session.get(Lead, world["lead_id"])
    assert lead.status is LeadStatus.HUMAN_HANDOFF, "a late AI result must not move it to REPLIED"
    assert lead.status is not LeadStatus.REPLIED
    assert lead.status is not LeadStatus.NEW


async def test_a_late_ai_result_does_not_create_a_second_handoff(db, owner, tenant_row):
    world = await make_conversation(owner, tenant_row)
    service = ai_service(
        db, tenant_row, _EscalatingDuringGeneration(db, tenant_row, world), FakeGraph()
    )

    await service.process_inbound_message(world["inbound_message_id"])

    assert await handoff_count(db, tenant_row) == 1
    assert await count_active(db, tenant_row, world["conversation_id"]) == 1


# -- regressions from earlier phases -----------------------------------------


async def test_a_handed_off_lead_is_not_promoted_to_replied(db, owner, tenant_row):
    """Phase 7's NEW -> REPLIED edge must not undo Phase 5's HUMAN_HANDOFF."""
    world = await make_conversation(owner, tenant_row, lead_status=LeadStatus.HUMAN_HANDOFF)

    await ai_service(
        db, tenant_row, FakeRegistry().program_response(**CALM), FakeGraph()
    ).process_inbound_message(world["inbound_message_id"])

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        lead = await session.get(Lead, world["lead_id"])
    assert lead.status is LeadStatus.HUMAN_HANDOFF


async def test_new_to_replied_still_works_without_a_handoff(db, owner, tenant_row):
    """The Phase 7 behaviour Phase 8 must not have broken."""
    world = await make_conversation(owner, tenant_row)

    await ai_service(
        db, tenant_row, FakeRegistry().program_response(**CALM), FakeGraph()
    ).process_inbound_message(world["inbound_message_id"])

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        lead = await session.get(Lead, world["lead_id"])
    assert lead.status is LeadStatus.REPLIED


async def test_a_normal_reply_still_arms_the_follow_up_sequence(db, owner, tenant_row):
    """The Phase 7 happy path, unchanged by Phase 8."""
    world = await make_conversation(owner, tenant_row)

    await ai_service(
        db, tenant_row, FakeRegistry().program_response(**CALM), FakeGraph()
    ).process_inbound_message(world["inbound_message_id"])

    tasks = await _tasks(db, tenant_row, world["conversation_id"])
    assert [(t.task_type, t.sequence_step, t.status) for t in tasks] == [
        (TaskType.FOLLOW_UP, 1, TaskStatus.PENDING)
    ]


# -- helpers -----------------------------------------------------------------


class _EscalatingDuringGeneration(FakeRegistry):
    """A registry that creates a handoff while it is 'thinking'.

    This is the whole point of the late-result guard: the conversation was not in human
    hands when the run started, and was by the time the answer came back.
    """

    def __init__(self, db, tenant_id, world):
        super().__init__()
        self._db = db
        self._tenant_id = tenant_id
        self._conversation_id = world["conversation_id"]
        self._hooked = False
        # A well-formed answer, so the run gets past validation and reaches the late-result
        # guard. An empty queue would fail the call instead, which is a different test.
        self.program_response(**CALM)

    async def generate_structured(self, request):
        if not self._hooked:
            self._hooked = True
            await HumanHandoffService(
                self._db, self._tenant_id, settings=ho_settings(), clock=FixedClock(T0)
            ).create_or_get_active_handoff(
                self._conversation_id, reason=HandoffReason.EXPLICIT_HUMAN_REQUEST
            )
        return await super().generate_structured(request)


async def _active(db, tenant_id, conversation_id):
    async with db() as session:
        await set_current_tenant(session, tenant_id)
        return await handoff_repo.get_active(
            session, tenant_id=tenant_id, conversation_id=conversation_id
        )
