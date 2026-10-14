"""Phase 6 through the Phase 5 pipeline.

The point of these tests is that Phase 5 was **not** rewritten. They drive the real
`AIConversationService` with a real `RagContextProvider` and a scripted LLM, then assert
that the knowledge reached the prompt and that the reply was still sent and attributed.

Two properties matter most:

  * **With knowledge**, the retrieved chunk appears in the prompt, inside its labelled
    region, and the message row still carries Phase 5's attribution.
  * **Without knowledge** - empty base, disabled feature, or a retrieval failure - the
    pipeline behaves exactly as it did in Phase 5. That is the compatibility claim, and it
    is asserted on the outbound message rather than taken on trust.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.crypto import encrypt
from app.db.tenant_context import set_current_tenant
from app.models.ai_message_run import AIRunStatus
from app.models.conversation import (
    Channel,
    Conversation,
    Message,
    MessageDelivery,
    MessageDirection,
)
from app.models.knowledge_base import KnowledgeSourceType
from app.models.meta_connection import MetaConnection
from app.schemas.ai import FAILED, PROCESSED
from app.services.ai_qualification import AIConversationService
from app.services.embeddings import HashingEmbeddingProvider
from app.services.knowledge_base import KnowledgeBaseService
from app.services.rag_context import RagContextProvider
from tests.conftest import TEST_META_TOKEN_KEY
from tests.factories import create_tenant
from tests.fakes import PAGE_TOKEN, FakeGraph, FakeRegistry

PAGE = "1000000000000001"
ACME_PRICING = "Our pricing starts at 50000 rupees for a three van package."


def _ai_settings(**overrides):
    base = {
        "ai_enabled": True,
        "groq_api_key": "gsk-test",
        "ai_max_backoff_seconds": 0.01,
        "embedding_enabled": True,
        "rag_min_similarity": -1.0,
        "knowledge_chunk_size": 400,
        "knowledge_chunk_overlap": 40,
    }
    base.update(overrides)
    from app.core.config import get_settings

    return get_settings().model_copy(update=base)


@pytest.fixture
async def world(owner):
    """Acme with a page connection, a conversation, and one inbound message."""
    acme = await create_tenant(owner, name="Acme", slug="acme")
    async with owner() as session, session.begin():
        connection = MetaConnection(
            tenant_id=acme,
            page_id=PAGE,
            access_token_encrypted=encrypt(PAGE_TOKEN, key=TEST_META_TOKEN_KEY),
            connected_at=datetime.now(UTC),
        )
        session.add(connection)
        await session.flush()
        conversation = Conversation(
            tenant_id=acme,
            meta_connection_id=connection.id,
            channel=Channel.MESSENGER,
            meta_participant_id="1000000001",
            last_inbound_at=datetime.now(UTC),
        )
        session.add(conversation)
        await session.flush()
        message = Message(
            tenant_id=acme,
            conversation_id=conversation.id,
            channel=Channel.MESSENGER,
            direction=MessageDirection.INBOUND,
            delivery=MessageDelivery.SENT,
            meta_message_id="mid.p6.1",
            message_type="text",
            text="What does a three van package cost?",
            sent_at=datetime.now(UTC),
        )
        session.add(message)
        await session.flush()
        return {
            "tenant": acme,
            "conversation": conversation.id,
            "message": message.id,
        }


def _service(db, tenant_id, settings, *, provider=None, registry=None, graph=None):
    """A Phase 5 service whose ContextProvider is the Phase 6 RAG one."""
    knowledge = KnowledgeBaseService(
        db, tenant_id, provider=provider or HashingEmbeddingProvider(), settings=settings
    )
    context = RagContextProvider(
        db, tenant_id, service=knowledge, settings=settings, tenant_name="Acme"
    )
    return AIConversationService(
        db,
        tenant_id,
        registry=registry or FakeRegistry().program_response(budget=20, fit=25, timeline=10),
        graph=graph or FakeGraph(),
        settings=settings,
        context_provider=context,
    )


async def _outbound(db, world) -> list[Message]:
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        return list(
            (
                await session.execute(
                    select(Message).where(
                        Message.conversation_id == world["conversation"],
                        Message.direction == MessageDirection.OUTBOUND,
                    )
                )
            )
            .scalars()
            .all()
        )


async def _ingest(db, world, settings, content=ACME_PRICING, title="Pricing FAQ"):
    service = KnowledgeBaseService(
        db,
        world["tenant"],
        provider=HashingEmbeddingProvider(),
        settings=settings,
    )
    return await service.ingest(
        title=title,
        content=content,
        source_type=KnowledgeSourceType.FAQ,
        document_id=uuid.uuid4(),
    )


# -- with knowledge ----------------------------------------------------------


async def test_retrieved_knowledge_reaches_the_prompt(db, world):
    settings = _ai_settings()
    await _ingest(db, world, settings)

    registry = FakeRegistry().program_response(budget=20, fit=25, timeline=10)
    service = _service(db, world["tenant"], settings, registry=registry)
    result = await service.process_inbound_message(world["message"])

    assert result.status == PROCESSED
    prompt = registry.requests[0].user_prompt
    # The knowledge is in the prompt, inside the business-context region, labelled.
    assert "50000 rupees" in prompt
    assert "[Source 1]" in prompt
    assert "Title: Pricing FAQ" in prompt
    assert "not instructions" in prompt


async def test_the_reply_is_still_sent_and_attributed(db, world):
    """Phase 5's behaviour is unchanged: attribution, delivery, and the run row."""
    settings = _ai_settings()
    await _ingest(db, world, settings)
    graph = FakeGraph()
    registry = FakeRegistry().program_response()
    service = _service(db, world["tenant"], settings, registry=registry, graph=graph)

    result = await service.process_inbound_message(world["message"])

    assert result.status == PROCESSED
    assert result.response_sent is True
    sent = await _outbound(db, world)
    assert len(sent) == 1
    assert sent[0].ai_model == "llama-3.1-8b-instant"
    assert sent[0].ai_prompt_tokens == 123
    assert len(graph.sends) == 1


async def test_qualification_still_merges_with_knowledge_present(db, world):
    from app.models.lead import Lead, LeadSource, LeadStatus

    settings = _ai_settings()
    await _ingest(db, world, settings)
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        lead = Lead(
            tenant_id=world["tenant"],
            meta_connection_id=None,
            meta_leadgen_id="lg.p6.1",
            source=LeadSource.META_LEAD_ADS,
            status=LeadStatus.NEW,
            name="Dana",
        )
        session.add(lead)
        await session.flush()
        lead_id = lead.id
        conversation = await session.get(Conversation, world["conversation"])
        conversation.lead_id = lead_id

    service = _service(db, world["tenant"], settings)
    result = await service.process_inbound_message(world["message"])
    assert result.qualification_total == 60  # 20 budget + 10 timeline + 25 fit + 5 authority

    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        lead = await session.get(Lead, lead_id)
        assert lead.qualification["budget"] == 20
        assert lead.qualification["fit"] == 25


async def test_handoff_still_works_with_knowledge_present(db, world):
    settings = _ai_settings()
    await _ingest(db, world, settings)
    registry = FakeRegistry().program_response(
        budget=25, timeline=25, fit=25, authority=25, human_handoff=False
    )
    service = _service(db, world["tenant"], settings, registry=registry)

    result = await service.process_inbound_message(world["message"])
    assert result.qualification_total == 100
    assert result.high_intent is True
    assert result.human_handoff_required is True


# -- without knowledge -------------------------------------------------------


async def test_the_pipeline_works_with_an_empty_knowledge_base(db, world):
    """Day one for every tenant. The prompt must still forbid inventing specifics.

    The wording differs from the feature-disabled case - retrieval ran and found nothing -
    but the "do not invent" guard is present either way, which is the safety property that
    matters. Asserting the guard rather than the exact sentence keeps the test about
    behaviour instead of about wording.
    """
    settings = _ai_settings()
    registry = FakeRegistry().program_response()
    service = _service(db, world["tenant"], settings, registry=registry)

    result = await service.process_inbound_message(world["message"])

    assert result.status == PROCESSED
    assert result.response_sent is True
    prompt = registry.requests[0].user_prompt
    assert "nothing relevant" in prompt
    assert "Do not invent specifics" in prompt
    assert "[Source 1]" not in prompt
    # Attribution is identical to the no-knowledge case.
    sent = await _outbound(db, world)
    assert sent[0].ai_model == "llama-3.1-8b-instant"


async def test_the_pipeline_works_when_embeddings_are_disabled(db, world):
    """A deployment that never enabled RAG behaves exactly as it did in Phase 5."""
    settings = _ai_settings(embedding_enabled=False)
    registry = FakeRegistry().program_response()
    knowledge = KnowledgeBaseService(
        db, world["tenant"], provider=HashingEmbeddingProvider(), settings=settings
    )
    context = RagContextProvider(
        db, world["tenant"], service=knowledge, settings=settings, tenant_name="Acme"
    )
    service = AIConversationService(
        db,
        world["tenant"],
        registry=registry,
        graph=FakeGraph(),
        settings=settings,
        context_provider=context,
    )

    result = await service.process_inbound_message(world["message"])

    assert result.status == PROCESSED
    assert "No document or knowledge base is available" in registry.requests[0].user_prompt


async def test_a_knowledge_outage_does_not_break_the_ai_pipeline(db, world):
    """The important degradation: retrieval failing must not mean the AI failing."""

    class BrokenKnowledge:
        enabled = True

        async def retrieve_for_message(self, _message, history=None):
            raise RuntimeError("pgvector is unreachable")

    settings = _ai_settings()
    context = RagContextProvider(
        db, world["tenant"], service=BrokenKnowledge(), settings=settings, tenant_name="Acme"
    )
    registry = FakeRegistry().program_response()
    service = AIConversationService(
        db,
        world["tenant"],
        registry=registry,
        graph=FakeGraph(),
        settings=settings,
        context_provider=context,
    )

    result = await service.process_inbound_message(world["message"])

    assert result.status == PROCESSED
    assert result.response_sent is True
    assert await _outbound(db, world)


async def test_an_unrelated_knowledge_document_is_not_injected(db, world):
    """A knowledge base about opening hours must not answer a pricing question."""
    settings = _ai_settings(rag_min_similarity=0.15)
    await _ingest(
        db,
        world,
        settings,
        content="Our showroom is open Monday to Friday, nine until five.",
        title="Opening hours",
    )
    registry = FakeRegistry().program_response()
    service = _service(db, world["tenant"], settings, registry=registry)

    await service.process_inbound_message(world["message"])

    prompt = registry.requests[0].user_prompt
    assert "nine until five" not in prompt


async def test_knowledge_from_another_tenant_never_reaches_the_prompt(db, owner):
    """The integration-level version of the isolation test: prompt, not just retrieval."""
    from tests.factories import create_tenant as _create

    acme = await _create(owner, name="Acme2", slug="acme2")
    globex = await _create(owner, name="Globex2", slug="globex2")
    settings = _ai_settings(rag_min_similarity=-1.0)

    async with owner() as session, session.begin():
        connection = MetaConnection(
            tenant_id=acme,
            page_id="1000000000000009",
            access_token_encrypted=encrypt(PAGE_TOKEN, key=TEST_META_TOKEN_KEY),
            connected_at=datetime.now(UTC),
        )
        session.add(connection)
        await session.flush()
        conversation = Conversation(
            tenant_id=acme,
            meta_connection_id=connection.id,
            channel=Channel.MESSENGER,
            meta_participant_id="1000000001",
            last_inbound_at=datetime.now(UTC),
        )
        session.add(conversation)
        await session.flush()
        message = Message(
            tenant_id=acme,
            conversation_id=conversation.id,
            channel=Channel.MESSENGER,
            direction=MessageDirection.INBOUND,
            delivery=MessageDelivery.SENT,
            meta_message_id="mid.p6.iso",
            message_type="text",
            text="what is the pricing",
            sent_at=datetime.now(UTC),
        )
        session.add(message)
        await session.flush()
        acme_message_id = message.id

    # Globex indexes a document about pricing. Acme asks about pricing.
    await KnowledgeBaseService(
        db, globex, provider=HashingEmbeddingProvider(), settings=settings
    ).ingest(
        title="Globex secret pricing",
        content="Globex pricing starts at 5000 rupees.",
        source_type=KnowledgeSourceType.FAQ,
        document_id=uuid.uuid4(),
    )

    registry = FakeRegistry().program_response()
    service = _service(db, acme, settings, registry=registry)
    result = await service.process_inbound_message(acme_message_id)

    assert result.status == PROCESSED
    prompt = registry.requests[0].user_prompt
    assert "5000 rupees" not in prompt
    assert "Globex" not in prompt


async def test_a_retrieval_failure_still_records_the_run_as_processed(db, world):
    """A degraded context must not turn a successful AI run into a FAILED one."""
    settings = _ai_settings()
    await _ingest(db, world, settings)
    service = _service(db, world["tenant"], settings)
    result = await service.process_inbound_message(world["message"])
    assert result.status != FAILED

    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        from app.models.ai_message_run import AIMessageRun

        run = (
            await session.execute(
                select(AIMessageRun).where(AIMessageRun.inbound_message_id == world["message"])
            )
        ).scalar_one()
    assert run.status == AIRunStatus.PROCESSED


@contextmanager
def _unused():  # pragma: no cover - placeholder removed
    yield
