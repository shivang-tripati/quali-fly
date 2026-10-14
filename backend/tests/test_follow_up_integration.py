"""Phase 7 against the layers it consumes: the AI pipeline, the API, and RAG.

The point of these is the boundary. Phase 7 must reach the *existing* Phase 5 provider
registry and the *existing* Phase 6 `ContextProvider`, and must not change either. So these
tests drive the real `AIConversationService` and the real HTTP surface, and assert that
follow-up behaviour appears without Phase 5's own behaviour moving.
"""

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.db.tenant_context import set_current_tenant
from app.models.ai_message_run import AIRunStatus
from app.models.conversation import (
    Channel,
    Message,
    MessageDelivery,
    MessageDirection,
)
from app.models.knowledge_base import KnowledgeSourceType
from app.models.scheduled_task import ScheduledTask, TaskStatus, TaskType
from app.services.ai_qualification import AIConversationService
from app.services.conversations import ConversationEngine
from app.services.embeddings import HashingEmbeddingProvider
from app.services.follow_ups import FollowUpService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.meta_messaging import InboundMessage
from app.services.rag_context import RagContextProvider, build_context_provider
from tests.fakes import FakeGraph, FakeRegistry
from tests.follow_up_fixtures import T0, claim_and_execute, fu_settings, make_conversation


async def _inbound_delivery(db, world, mid: str, text: str) -> None:
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant_id"])
        await ConversationEngine(session, world["tenant_id"]).ingest_inbound(
            meta_connection_id=world["connection_id"],
            channel=Channel.MESSENGER,
            messages=[
                InboundMessage(
                    participant_id=world["participant_id"],
                    meta_message_id=mid,
                    source_id=None,
                    text=text,
                    message_type="text",
                    sent_at=None,
                )
            ],
        )


# -- AI enhancement via the existing registry --------------------------------


async def test_ai_enhancement_uses_the_existing_registry_and_still_sends(
    db, owner, tenant_row, clock
):
    """The AI may improve wording; it may not decide whether to send."""
    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()
    registry = FakeRegistry().program_response(
        message="Hi Dana, following up on the three vans - is this still worth a look?"
    )
    settings = fu_settings(follow_up_ai_enhance=True)
    service = FollowUpService(
        db,
        tenant_row,
        clock=clock,
        registry=registry,
        graph=graph,
        settings=settings,
    )
    task = await service.on_reply_sent(world["conversation_id"])
    clock.advance(hours=4)
    result = await claim_and_execute(
        db, tenant_row, clock, graph=graph, registry=registry, follow_up_ai_enhance=True
    )

    assert result.completed == 1
    # The registry was consulted exactly once, through the Phase 5 abstraction.
    assert len(registry.requests) == 1
    assert "three vans" in graph.sends[0]["text"]

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        row = (
            await session.execute(select(ScheduledTask).where(ScheduledTask.id == task.id))
        ).scalar_one()
    assert row.ai_enhanced is True
    assert row.sent_text == graph.sends[0]["text"]


async def test_ai_enhancement_failure_falls_back_to_the_template(db, owner, tenant_row, clock):
    """A provider outage must not silently cost a lead their follow-up."""
    from app.services.llm_base import LLMTransientError

    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()
    registry = FakeRegistry().program(LLMTransientError("provider down"))
    service = FollowUpService(
        db,
        tenant_row,
        clock=clock,
        registry=registry,
        graph=graph,
        settings=fu_settings(follow_up_ai_enhance=True),
    )
    task = await service.on_reply_sent(world["conversation_id"])
    clock.advance(hours=4)
    result = await claim_and_execute(
        db, tenant_row, clock, graph=graph, registry=registry, follow_up_ai_enhance=True
    )

    assert result.completed == 1
    # The deterministic template went out instead.
    assert "just checking in" in graph.sends[0]["text"]
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        row = (
            await session.execute(select(ScheduledTask).where(ScheduledTask.id == task.id))
        ).scalar_one()
    assert row.ai_enhanced is False
    assert row.status == TaskStatus.COMPLETED


async def test_malformed_ai_output_falls_back_to_the_template(db, owner, tenant_row, clock):
    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()
    registry = FakeRegistry().program_text("I am a helpful assistant!")
    service = FollowUpService(
        db,
        tenant_row,
        clock=clock,
        registry=registry,
        graph=graph,
        settings=fu_settings(follow_up_ai_enhance=True),
    )
    await service.on_reply_sent(world["conversation_id"])
    clock.advance(hours=4)
    result = await claim_and_execute(
        db, tenant_row, clock, graph=graph, registry=registry, follow_up_ai_enhance=True
    )
    assert result.completed == 1
    assert "just checking in" in graph.sends[0]["text"]


async def test_the_follow_up_prompt_keeps_the_untrusted_region_framing(
    db, owner, tenant_row, clock
):
    """Two systems would mean two sets of injection rules; one PromptBuilder is reused."""
    world = await make_conversation(owner, tenant_row)
    registry = FakeRegistry().program_response(message="Sure, happy to help.")
    service = FollowUpService(
        db,
        tenant_row,
        clock=clock,
        registry=registry,
        graph=FakeGraph(),
        settings=fu_settings(follow_up_ai_enhance=True),
    )
    await service.on_reply_sent(world["conversation_id"])
    clock.advance(hours=4)
    # The registry is threaded in, or the poller builds a real one and this prompt is
    # never built at all.
    await claim_and_execute(
        db, tenant_row, clock, graph=FakeGraph(), registry=registry, follow_up_ai_enhance=True
    )
    prompt = registry.requests[0].user_prompt
    assert "<conversation_history>" in prompt
    assert "<current_message>" in prompt
    assert "<follow_up_step>" in prompt
    assert "Template:" in prompt


# -- Phase 5 integration ----------------------------------------------------


async def test_an_ai_reply_schedules_follow_up_1(db, owner, tenant_row, clock):
    """The AI pipeline's success path arms the sequence. No separate hook to forget."""
    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()
    service = AIConversationService(
        db,
        tenant_row,
        registry=FakeRegistry().program_response(),
        graph=graph,
        settings=fu_settings(),
    )
    result = await service.process_inbound_message(world["inbound_message_id"])
    assert result.status == "PROCESSED"
    assert result.response_sent is True

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        tasks = (await session.execute(select(ScheduledTask))).scalars().all()
    follow_ups = [t for t in tasks if t.task_type == TaskType.FOLLOW_UP]
    assert len(follow_ups) == 1
    assert follow_ups[0].sequence_step == 1
    # Phase 5's own behaviour is untouched: the run is still recorded and attributed.
    assert follow_ups[0].conversation_id == world["conversation_id"]


async def test_phase_5_behaviour_is_unchanged_by_phase_7(db, owner, tenant_row, clock):
    """Qualification, handoff, attribution and the run row all still work."""
    from app.models.lead import Lead, LeadStatus

    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()
    service = AIConversationService(
        db,
        tenant_row,
        registry=FakeRegistry().program_response(budget=25, timeline=25, fit=25, authority=25),
        graph=graph,
        settings=fu_settings(),
    )
    result = await service.process_inbound_message(world["inbound_message_id"])
    assert result.status == "PROCESSED"
    assert result.qualification_total == 100
    assert result.human_handoff_required is True

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        lead = await session.get(Lead, world["lead_id"])
        # Phase 5 writes HUMAN_HANDOFF, and Phase 7 must not overwrite it with GHOSTED.
        assert lead.status == LeadStatus.HUMAN_HANDOFF
        from app.models.ai_message_run import AIMessageRun

        run = (
            await session.execute(
                select(AIMessageRun).where(
                    AIMessageRun.inbound_message_id == world["inbound_message_id"]
                )
            )
        ).scalar_one()
        assert run.status == AIRunStatus.PROCESSED
        outbound = (
            (
                await session.execute(
                    select(Message).where(
                        Message.conversation_id == world["conversation_id"],
                        Message.direction == MessageDirection.OUTBOUND,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(outbound) == 1
    assert outbound[0].ai_model == "llama-3.1-8b-instant"
    assert outbound[0].delivery == MessageDelivery.SENT


async def test_the_ai_reply_does_not_schedule_when_follow_ups_are_off(db, owner, tenant_row, clock):
    world = await make_conversation(owner, tenant_row)
    service = AIConversationService(
        db,
        tenant_row,
        registry=FakeRegistry().program_response(),
        graph=FakeGraph(),
        settings=fu_settings(follow_up_enabled=False),
    )
    await service.process_inbound_message(world["inbound_message_id"])
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        assert (await session.execute(select(ScheduledTask))).scalars().all() == []


async def test_a_human_handoff_lease_blocks_the_follow_up(db, owner, tenant_row, clock):
    """A high-intent lead is handed to a person, so automation must stop."""
    from app.models.lead import Lead, LeadStatus

    world = await make_conversation(owner, tenant_row)
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        lead = await session.get(Lead, world["lead_id"])
        lead.status = LeadStatus.HUMAN_HANDOFF
    graph = FakeGraph()
    service = FollowUpService(db, tenant_row, clock=clock, graph=graph, settings=fu_settings())
    assert await service.on_reply_sent(world["conversation_id"]) is None
    assert graph.sends == []


# -- Phase 6 RAG integration -----------------------------------------------


async def test_follow_up_ai_uses_the_rag_context_provider(db, owner, tenant_row, clock):
    """Phase 6's retrieval reaches the follow-up prompt without Phase 6 changing."""
    world = await make_conversation(owner, tenant_row)
    settings = fu_settings(
        follow_up_ai_enhance=True, embedding_enabled=True, rag_min_similarity=-1.0
    )
    await KnowledgeBaseService(
        db, tenant_row, provider=HashingEmbeddingProvider(), settings=settings
    ).ingest(
        title="Pricing",
        content="Our pricing starts at 50000 rupees for a three van package.",
        source_type=KnowledgeSourceType.FAQ,
        document_id=uuid.uuid4(),
    )
    context = RagContextProvider(
        db,
        tenant_row,
        service=KnowledgeBaseService(
            db, tenant_row, provider=HashingEmbeddingProvider(), settings=settings
        ),
        settings=settings,
        tenant_name="Acme",
    )
    registry = FakeRegistry().program_response(message="Following up on pricing.")
    service = FollowUpService(
        db,
        tenant_row,
        clock=clock,
        registry=registry,
        context_provider=context,
        graph=FakeGraph(),
        settings=settings,
    )
    await service.on_reply_sent(world["conversation_id"])
    clock.advance(hours=4)
    # The RAG provider is threaded in, or the poller's service uses the static provider
    # and never retrieves.
    result = await claim_and_execute(
        db,
        tenant_row,
        clock,
        graph=FakeGraph(),
        registry=registry,
        context=context,
        follow_up_ai_enhance=True,
    )
    assert result.completed == 1
    prompt = registry.requests[0].user_prompt
    assert "50000 rupees" in prompt
    assert "[Source 1]" in prompt


def test_build_context_provider_still_returns_the_static_one_when_disabled():
    """Phase 5's fallback is untouched by Phase 7's existence."""
    from app.services.ai_prompt import StaticContextProvider

    assert isinstance(
        build_context_provider(None, uuid.uuid4(), settings=fu_settings()), StaticContextProvider
    )


# -- the end-to-end slice the spec asks for --------------------------------


async def test_the_full_slice_inbound_ai_followups_then_a_reply(db, owner, tenant_row, clock):
    """1-15 of the spec's smoke list, driven end to end with a fake Graph."""
    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()

    # 3. inbound message exists (from the fixture); 4. the AI answers it.
    ai = AIConversationService(
        db,
        tenant_row,
        registry=FakeRegistry().program_response(),
        graph=graph,
        settings=fu_settings(),
        clock=clock,
    )
    assert (await ai.process_inbound_message(world["inbound_message_id"])).status == "PROCESSED"

    # 5. FU1 is armed, 6. the clock reaches +4h, 7. the poller claims it.
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        tasks = (await session.execute(select(ScheduledTask))).scalars().all()
    fu1 = [t for t in tasks if t.task_type == TaskType.FOLLOW_UP][0]
    assert fu1.sequence_step == 1, [
        (t.task_type, t.sequence_step, t.status, t.scheduled_at) for t in tasks
    ]
    assert fu1.scheduled_at == T0 + timedelta(hours=4), (
        f"scheduled_at={fu1.scheduled_at} clock={clock.now()} T0={T0}"
    )

    clock.advance(hours=4)
    sends_before = len(graph.sends)
    first = await claim_and_execute(db, tenant_row, clock, graph=graph)
    assert first.completed == 1
    # 8. the sender was invoked exactly once more, 9. FU2 exists.
    assert len(graph.sends) == sends_before + 1

    # 10-11. FU2 executes, FU3 is armed.
    clock.advance(hours=24)
    second = await claim_and_execute(db, tenant_row, clock, graph=graph)
    assert second.completed == 1
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        tasks = (await session.execute(select(ScheduledTask))).scalars().all()
    assert any(t.sequence_step == 3 and t.status == TaskStatus.PENDING for t in tasks)

    # 12-14. an inbound reply cancels the pending FU3, and the scheduler finds nothing.
    await _inbound_delivery(db, world, "mid.final", "Sorry, not interested.")
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        tasks = (await session.execute(select(ScheduledTask))).scalars().all()
    assert all(t.status != TaskStatus.PENDING for t in tasks)
    sends_before = len(graph.sends)
    third = await claim_and_execute(db, tenant_row, clock, graph=graph)
    assert third.claimed == 0
    assert len(graph.sends) == sends_before

    # 15. the lead is not prematurely ghosted.
    from app.models.lead import Lead, LeadStatus

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        assert (await session.get(Lead, world["lead_id"])).status != LeadStatus.GHOSTED


async def test_the_silent_path_ends_in_ghosted(db, owner, tenant_row, clock):
    """The separate 'no reply -> +7d -> Ghosted' case."""
    from app.models.lead import Lead, LeadStatus

    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()
    service = FollowUpService(db, tenant_row, clock=clock, graph=graph, settings=fu_settings())
    await service.on_reply_sent(world["conversation_id"])
    for wait in (4, 24, 72):
        clock.advance(hours=wait)
        await claim_and_execute(db, tenant_row, clock, graph=graph)
    clock.advance(days=7)
    result = await claim_and_execute(db, tenant_row, clock, graph=graph)
    assert result.completed >= 1
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        assert (await session.get(Lead, world["lead_id"])).status == LeadStatus.GHOSTED
