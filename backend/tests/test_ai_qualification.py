"""Phase 5 integration tests: the pipeline against a real PostgreSQL database.

The database is never mocked here. Everything worth proving in this phase - the unique
constraint that makes the run idempotent, the composite tenant foreign keys, RLS
isolation, and what actually lands in `messages` and `leads` - is database behaviour, and
a mock would make the assertions meaningless. Only the two genuinely external boundaries
are substituted: the LLM providers (`FakeRegistry`) and Meta Graph (`FakeGraph`).
"""

import asyncio
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.crypto import encrypt
from app.db.tenant_context import set_current_tenant
from app.models.ai_message_run import AIMessageRun, AIRunStatus
from app.models.conversation import (
    Channel,
    Conversation,
    Message,
    MessageDelivery,
    MessageDirection,
)
from app.models.lead import Lead, LeadSource, LeadStatus
from app.models.meta_connection import MetaConnection
from app.models.meta_webhook_event import MetaWebhookEvent, MetaWebhookEventStatus
from app.repositories import handoffs as handoff_repo
from app.schemas.ai import FAILED, PROCESSED, SKIPPED
from app.services.ai_qualification import AIConversationService
from app.services.llm_base import LLMResponseError, LLMTransientError
from tests.conftest import TEST_META_TOKEN_KEY
from tests.factories import create_tenant
from tests.fakes import PAGE_TOKEN, FakeGraph, FakeRegistry

PAGE = "1000000000000001"


def _ai_settings(**overrides):
    base = {
        "ai_enabled": True,
        "groq_api_key": "gsk-test",
        "ai_max_backoff_seconds": 0.01,
        "ai_history_limit": 10,
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


@pytest.fixture
async def world(owner):
    """Two tenants, each with a page connection, a conversation, and one inbound message.

    Second tenant exists for the isolation test. It is created here rather than in a
    separate fixture so both live in the same database state and a cross-tenant read
    genuinely has a row to find.
    """
    acme = await create_tenant(owner, name="Acme", slug="acme")
    globex = await create_tenant(owner, name="Globex", slug="globex")

    async with owner() as session, session.begin():
        acme_connection = MetaConnection(
            tenant_id=acme,
            page_id=PAGE,
            access_token_encrypted=encrypt(PAGE_TOKEN, key=TEST_META_TOKEN_KEY),
            connected_at=datetime.now(UTC),
        )
        globex_connection = MetaConnection(
            tenant_id=globex,
            page_id="2000000000000002",
            access_token_encrypted=encrypt(PAGE_TOKEN, key=TEST_META_TOKEN_KEY),
            connected_at=datetime.now(UTC),
        )
        session.add_all([acme_connection, globex_connection])
        await session.flush()

        acme_conversation = Conversation(
            tenant_id=acme,
            meta_connection_id=acme_connection.id,
            channel=Channel.MESSENGER,
            meta_participant_id="1000000001",
            last_inbound_at=datetime.now(UTC),
        )
        globex_conversation = Conversation(
            tenant_id=globex,
            meta_connection_id=globex_connection.id,
            channel=Channel.MESSENGER,
            meta_participant_id="2000000001",
            last_inbound_at=datetime.now(UTC),
        )
        session.add_all([acme_conversation, globex_conversation])
        await session.flush()

        acme_message = Message(
            tenant_id=acme,
            conversation_id=acme_conversation.id,
            channel=Channel.MESSENGER,
            direction=MessageDirection.INBOUND,
            delivery=MessageDelivery.SENT,
            meta_message_id="mid.acme.1",
            message_type="text",
            text="Hi, I need pricing for three vans.",
            sent_at=datetime.now(UTC),
        )
        globex_message = Message(
            tenant_id=globex,
            conversation_id=globex_conversation.id,
            channel=Channel.MESSENGER,
            direction=MessageDirection.INBOUND,
            delivery=MessageDelivery.SENT,
            meta_message_id="mid.globex.1",
            message_type="text",
            text="Do you have stock?",
            sent_at=datetime.now(UTC),
        )
        session.add_all([acme_message, globex_message])
        await session.flush()

        acme_lead = Lead(
            tenant_id=acme,
            meta_connection_id=acme_connection.id,
            meta_leadgen_id="lg.acme.1",
            source=LeadSource.META_LEAD_ADS,
            status=LeadStatus.NEW,
            name="Dana Buyer",
            email="dana@example.test",
        )
        session.add(acme_lead)
        await session.flush()
        acme_conversation.lead_id = acme_lead.id

        return {
            "acme": acme,
            "globex": globex,
            "acme_connection": acme_connection.id,
            "acme_conversation": acme_conversation.id,
            "acme_message": acme_message.id,
            "acme_lead": acme_lead.id,
            "globex_conversation": globex_conversation.id,
            "globex_message": globex_message.id,
        }


def _service(db, tenant_id, registry, graph=None, **overrides):
    return AIConversationService(
        db,
        tenant_id,
        registry=registry,
        graph=graph or FakeGraph(),
        settings=_ai_settings(**overrides),
    )


async def _outbound(db, tenant_id, conversation_id) -> list[Message]:
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        return list(
            (
                await session.execute(
                    select(Message).where(
                        Message.conversation_id == conversation_id,
                        Message.direction == MessageDirection.OUTBOUND,
                    )
                )
            )
            .scalars()
            .all()
        )


async def _ai_outbound(db, tenant_id, conversation_id) -> list[Message]:
    """Outbound messages the *AI* produced, i.e. the ones carrying model attribution.

    Phase 8 added a second, non-AI message to the failure path: the deterministic handoff
    acknowledgement, which exists precisely because the AI did *not* work. A test that
    asserted "no outbound" would now be asserting the absence of the honest
    "someone is coming" line, so the Phase 5 invariant is restated against AI-attributed
    messages only - which is what it was always actually about.
    """
    return [m for m in await _outbound(db, tenant_id, conversation_id) if m.ai_model is not None]


async def _run_row(db, tenant_id, message_id) -> AIMessageRun | None:
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        return await session.scalar(
            select(AIMessageRun).where(AIMessageRun.inbound_message_id == message_id)
        )


# -- the happy path ----------------------------------------------------------


async def test_an_inbound_message_becomes_a_qualified_attributed_reply(db, world):
    registry = FakeRegistry().program_response(budget=20, timeline=15, fit=25, authority=20)
    service = _service(db, world["acme"], registry)

    result = await service.process_inbound_message(world["acme_message"])

    assert result.status == PROCESSED
    assert result.qualification_total == 80
    assert result.response_sent is True
    assert result.provider == "groq"
    assert result.model == "llama-3.1-8b-instant"
    # Real usage from the provider, not zeros.
    assert (result.prompt_tokens, result.completion_tokens) == (123, 45)
    assert result.latency_ms == 321


async def test_the_reply_is_attributed_on_the_message_row(db, world):
    registry = FakeRegistry().program_response()
    service = _service(db, world["acme"], registry)

    result = await service.process_inbound_message(world["acme_message"])

    sent = await _outbound(db, world["acme"], world["acme_conversation"])
    assert len(sent) == 1
    message = sent[0]
    assert message.delivery == MessageDelivery.SENT
    # This is the attribution requirement: the outbound row records which model spoke and
    # what it cost, so a conversation can be audited per reply.
    assert message.ai_model == "llama-3.1-8b-instant"
    assert message.ai_prompt_tokens == 123
    assert message.ai_completion_tokens == 45
    assert message.ai_latency_ms == 321
    assert result.outbound_message_id == str(message.id)


async def test_the_run_row_records_the_whole_outcome(db, world):
    registry = FakeRegistry().program_response(budget=20, timeline=20, fit=20, authority=20)
    await _service(db, world["acme"], registry).process_inbound_message(world["acme_message"])

    run = await _run_row(db, world["acme"], world["acme_message"])
    assert run is not None
    assert run.status == AIRunStatus.PROCESSED
    assert run.provider == "groq"
    assert run.qualification_total == 80
    assert run.high_intent is True
    assert run.human_handoff_required is True
    # A score of 80 with no stated reason still gets an explainable one.
    assert "above threshold" in run.handoff_reason
    assert run.outbound_message_id is not None
    assert run.completed_at is not None
    assert run.error_category is None
    # The validated result is retained, so a run is auditable without re-billing a provider.
    assert run.ai_response["budget"] == 20


async def test_the_qualification_is_persisted_onto_the_lead(db, world):
    registry = FakeRegistry().program_response(budget=15, timeline=5, fit=10, authority=25)
    await _service(db, world["acme"], registry).process_inbound_message(world["acme_message"])

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        lead = await session.get(Lead, world["acme_lead"])
        assert lead.qualification == {
            "budget": 15,
            "timeline": 5,
            "fit": 10,
            "authority": 25,
            "total": 55,
            "high_intent": False,
        }


# -- lead lifecycle: NEW -> REPLIED -------------------------------------------


async def _lead_status(db, world) -> LeadStatus:
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        lead = await session.get(Lead, world["acme_lead"])
        return lead.status


async def test_answering_a_new_lead_moves_it_to_replied(db, world):
    """The PRD's NEW -> REPLIED edge.

    Nothing before this phase ever wrote `REPLIED`, so without this the status machine
    was missing an edge it claims to have, and every lead would sit at NEW forever
    however many times it was answered.
    """
    assert await _lead_status(db, world) is LeadStatus.NEW

    registry = FakeRegistry().program_response(budget=2, timeline=2, fit=2, authority=1)
    await _service(db, world["acme"], registry).process_inbound_message(world["acme_message"])

    assert await _lead_status(db, world) is LeadStatus.REPLIED


async def test_a_lead_needing_a_human_becomes_handoff_not_replied(db, world):
    """Handoff must win, or the lead would be automated around in a later phase."""
    registry = FakeRegistry().program_response(
        budget=20, timeline=20, fit=20, authority=20, human_handoff=True, handoff_reason="call"
    )
    await _service(db, world["acme"], registry).process_inbound_message(world["acme_message"])

    assert await _lead_status(db, world) is LeadStatus.HUMAN_HANDOFF


async def test_answering_again_does_not_reopen_a_closed_lead(db, world):
    """Monotonic, like the scores: a late answer must not resurrect a dead deal."""
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        lead = await session.get(Lead, world["acme_lead"])
        lead.status = LeadStatus.CLOSED

    registry = FakeRegistry().program_response(budget=2, timeline=2, fit=2, authority=1)
    await _service(db, world["acme"], registry).process_inbound_message(world["acme_message"])

    assert await _lead_status(db, world) is LeadStatus.CLOSED


async def test_answering_again_does_not_reopen_a_ghosted_lead(db, world):
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        lead = await session.get(Lead, world["acme_lead"])
        lead.status = LeadStatus.GHOSTED

    registry = FakeRegistry().program_response(budget=2, timeline=2, fit=2, authority=1)
    await _service(db, world["acme"], registry).process_inbound_message(world["acme_message"])

    assert await _lead_status(db, world) is LeadStatus.GHOSTED


# -- qualification rules -----------------------------------------------------


async def test_a_low_total_is_not_high_intent(db, world):
    registry = FakeRegistry().program_response(budget=0, timeline=0, fit=5, authority=0)
    result = await _service(db, world["acme"], registry).process_inbound_message(
        world["acme_message"]
    )
    assert result.high_intent is False
    assert result.human_handoff_required is False


async def test_a_total_exactly_at_the_threshold_is_not_high_intent(db, world):
    """The PRD says *above* 75, so 75 is not high intent. Boundary tested deliberately."""
    registry = FakeRegistry().program_response(budget=20, timeline=20, fit=20, authority=15)
    result = await _service(db, world["acme"], registry).process_inbound_message(
        world["acme_message"]
    )
    assert result.qualification_total == 75
    assert result.high_intent is False


async def test_a_total_just_over_the_threshold_is_high_intent(db, world):
    registry = FakeRegistry().program_response(budget=20, timeline=20, fit=20, authority=16)
    result = await _service(db, world["acme"], registry).process_inbound_message(
        world["acme_message"]
    )
    assert result.qualification_total == 76
    assert result.high_intent is True
    assert result.human_handoff_required is True


async def test_the_model_can_request_handoff_without_a_high_score(db, world):
    """A lead asking for a human is a handoff at any score. Scores are not the only signal."""
    registry = FakeRegistry().program_response(
        budget=0, timeline=0, fit=0, authority=0, human_handoff=True, handoff_reason="wants a call"
    )
    result = await _service(db, world["acme"], registry).process_inbound_message(
        world["acme_message"]
    )
    assert result.high_intent is False
    assert result.human_handoff_required is True
    assert result.handoff_reason == "wants a call"


async def test_a_later_weaker_run_cannot_lower_a_recorded_score(db, world):
    """Monotonic by design: an ambiguous later message does not un-learn a lead's budget."""
    strong = FakeRegistry().program_response(budget=25, timeline=25, fit=25, authority=25)
    await _service(db, world["acme"], strong).process_inbound_message(world["acme_message"])

    weak = FakeRegistry().program_response(budget=0, timeline=0, fit=0, authority=0)
    second = await _service(db, world["acme"], weak).process_inbound_message(world["acme_message"])
    # The reply is suppressed the second time (already claimed), but the merge rule is
    # asserted directly against the stored lead so it is tested on its own terms.
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        lead = await session.get(Lead, world["acme_lead"])
        assert lead.qualification["budget"] == 25
        assert lead.qualification["total"] == 100
        assert lead.qualification["high_intent"] is True
    assert second.status == SKIPPED


async def test_a_merge_preserves_unrelated_keys_already_in_the_jsonb(db, world):
    """The column is shared with other phases; qualification must not wipe it."""
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        lead = await session.get(Lead, world["acme_lead"])
        lead.qualification = {"source_phase_4_flag": True}

    registry = FakeRegistry().program_response(budget=5, timeline=5, fit=5, authority=5)
    await _service(db, world["acme"], registry).process_inbound_message(world["acme_message"])

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        lead = await session.get(Lead, world["acme_lead"])
        assert lead.qualification["source_phase_4_flag"] is True
        assert lead.qualification["budget"] == 5


async def test_a_conversation_with_no_lead_is_still_answered(db, world):
    """Someone messages a page without ever submitting an ad form. That is not an error."""
    registry = FakeRegistry().program_response(budget=5, timeline=5, fit=5, authority=5)
    result = await _service(db, world["globex"], registry).process_inbound_message(
        world["globex_message"]
    )
    assert result.status == PROCESSED
    assert result.response_sent is True
    assert result.lead_id is None
    # No Lead row was created to hold the scores - the pipeline does not invent one.
    async with db() as session, session.begin():
        await set_current_tenant(session, world["globex"])
        leads = (await session.execute(select(Lead))).scalars().all()
        assert leads == []


# -- idempotency -------------------------------------------------------------


async def test_processing_the_same_message_twice_calls_the_provider_once(db, world):
    registry = FakeRegistry().program_response()
    service = _service(db, world["acme"], registry)

    first = await service.process_inbound_message(world["acme_message"])
    second = await service.process_inbound_message(world["acme_message"])

    assert first.status == PROCESSED
    assert second.status == SKIPPED
    assert second.reason == "already_claimed"
    # One provider call and one reply, not two. A lead must not be answered twice because
    # an event was redelivered.
    assert len(registry.requests) == 1
    assert len(await _outbound(db, world["acme"], world["acme_conversation"])) == 1


async def test_concurrent_workers_produce_exactly_one_run(db, world):
    """Two workers race on the same message. The unique index is the arbiter.

    `FakeRegistry` is not concurrency-safe by design, so the assertion is on the database:
    exactly one run row exists, and the loser is told it lost.
    """
    from app.repositories import ai_runs

    async def _claim(tenant_id):
        async with db() as session, session.begin():
            await set_current_tenant(session, tenant_id)
            try:
                run = await ai_runs.claim(
                    session,
                    tenant_id=tenant_id,
                    conversation_id=world["acme_conversation"],
                    inbound_message_id=world["acme_message"],
                    lead_id=world["acme_lead"],
                )
                return run.id
            except ai_runs.RunAlreadyClaimed:
                return None

    winners = await asyncio.gather(_claim(world["acme"]), _claim(world["acme"]))
    assert sum(1 for w in winners if w is not None) == 1

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        runs = (await session.execute(select(AIMessageRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].lead_id == world["acme_lead"]


# -- failure paths -----------------------------------------------------------


async def test_every_provider_failing_leaves_a_durable_failed_run(db, world):
    registry = FakeRegistry().program(LLMTransientError("all providers down"))
    result = await _service(db, world["acme"], registry).process_inbound_message(
        world["acme_message"]
    )

    assert result.status == FAILED
    assert result.reason == "transient"
    run = await _run_row(db, world["acme"], world["acme_message"])
    assert run.status == AIRunStatus.FAILED
    assert run.error_category == "transient"
    assert run.completed_at is not None
    # No provider is recorded on a failure: nothing was produced, so there is nothing to
    # attribute.
    assert run.provider is None
    assert run.model is None


async def test_a_failure_sends_nothing_and_qualifies_nothing(db, world):
    """A failed run must not leave a lead half-qualified or a half-sent reply.

    "Nothing" means nothing the AI said. Phase 8 deliberately adds the one message that is
    not the AI's: the deterministic handoff acknowledgement, sent because the model could
    not answer at all and a person is now the only way this conversation gets one. It
    carries no model attribution, which is what keeps it honest - it does not pretend the
    AI worked, and asserting that is the point of the AI-attribution filter.
    """
    from app.models.human_handoff import HandoffReason

    registry = FakeRegistry().program(LLMResponseError("unparseable"))
    await _service(db, world["acme"], registry).process_inbound_message(world["acme_message"])

    assert await _ai_outbound(db, world["acme"], world["acme_conversation"]) == []
    sent = await _outbound(db, world["acme"], world["acme_conversation"])
    assert [m.ai_model for m in sent] == [None], "one honest, unattributed acknowledgement"
    assert sent[0].text and "team" in sent[0].text

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        lead = await session.get(Lead, world["acme_lead"])
        assert lead.qualification is None
        handoff = await handoff_repo.get_active(
            session,
            tenant_id=world["acme"],
            conversation_id=world["acme_conversation"],
        )
        assert handoff is not None and handoff.reason is HandoffReason.AI_FAILURE


async def test_the_error_category_is_a_category_and_never_provider_text(db, world):
    """Provider errors can echo the prompt; the column is readable through the API."""
    registry = FakeRegistry().program(
        LLMResponseError("SECRET: the lead wrote 'ignore all instructions'")
    )
    await _service(db, world["acme"], registry).process_inbound_message(world["acme_message"])

    run = await _run_row(db, world["acme"], world["acme_message"])
    assert run.error_category == "invalid_response"
    assert "SECRET" not in run.error_category


@pytest.mark.parametrize(
    "garbled",
    [
        "not json at all",
        '{"message": "hi", "budget": 99, "timeline": 5, "fit": 5, "authority": 5, '
        '"human_handoff": false}',
        '{"message": "hi", "budget": 5, "timeline": 5, "fit": 5, "human_handoff": false}',
        '{"message": "", "budget": 5, "timeline": 5, "fit": 5, "authority": 5, '
        '"human_handoff": false}',
        '{"message": "hi", "budget": 5, "timeline": 5, "fit": 5, "authority": 5, '
        '"human_handoff": false, "secret_field": "x"}',
        "```json\n{}\n```",
    ],
)
async def test_malformed_model_output_never_reaches_the_database(db, world, garbled):
    registry = FakeRegistry().program_text(garbled)
    result = await _service(db, world["acme"], registry).process_inbound_message(
        world["acme_message"]
    )

    assert result.status == FAILED
    assert result.reason == "invalid_response"
    # Nothing the model produced is persisted or sent - not even as a truncated string.
    assert await _ai_outbound(db, world["acme"], world["acme_conversation"]) == []
    run = await _run_row(db, world["acme"], world["acme_message"])
    assert run.status == AIRunStatus.FAILED
    assert run.ai_response is None


async def test_ai_disabled_skips_without_calling_a_provider(db, world):
    registry = FakeRegistry().program_response()
    service = _service(db, world["acme"], registry, ai_enabled=False)

    result = await service.process_inbound_message(world["acme_message"])

    assert result.status == SKIPPED
    assert result.reason == "ai_disabled"
    assert registry.requests == []
    # No run row is written, so a run is not an artefact of a feature being off.
    assert await _run_row(db, world["acme"], world["acme_message"]) is None


async def test_an_attachment_only_message_is_skipped_durably(db, world):
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        message = await session.get(Message, world["acme_message"])
        message.text = None
        message.attachments = [{"url": "https://example.test/photo.jpg"}]

    registry = FakeRegistry().program_response()
    result = await _service(db, world["acme"], registry).process_inbound_message(
        world["acme_message"]
    )

    assert result.status == SKIPPED
    assert result.reason == "no_text"
    assert registry.requests == []
    run = await _run_row(db, world["acme"], world["acme_message"])
    assert run.status == AIRunStatus.SKIPPED


async def test_an_unknown_message_id_is_skipped_not_raised(db, world):
    registry = FakeRegistry().program_response()
    result = await _service(db, world["acme"], registry).process_inbound_message(uuid.uuid4())
    assert result.status == SKIPPED
    assert result.reason == "inbound_message_not_found"


# -- delivery reporting ------------------------------------------------------


async def test_a_meta_rejection_is_recorded_but_the_run_still_reports_what_happened(db, world):
    """The provider worked and the answer is stored; only the delivery failed.

    Phase 4 established that a send can be persisted and still fail at Meta. Reporting
    this as an AI failure would discard a good answer and hide the real problem, which is
    a Meta-side rejection.
    """
    from app.services.meta_graph import MetaMessageWindowClosed

    graph = FakeGraph()
    graph.program_send_error(MetaMessageWindowClosed("window closed"))
    registry = FakeRegistry().program_response()

    result = await _service(db, world["acme"], registry, graph=graph).process_inbound_message(
        world["acme_message"]
    )

    assert result.status == PROCESSED
    assert result.response_sent is False
    sent = await _outbound(db, world["acme"], world["acme_conversation"])
    assert sent[0].delivery == MessageDelivery.FAILED
    # Attribution survives the failed delivery, so the cost of the attempt is still known.
    assert sent[0].ai_model == "llama-3.1-8b-instant"
    # And the qualification still applied, because the lead was answered conceptually.
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        lead = await session.get(Lead, world["acme_lead"])
        assert lead.qualification is not None


# -- tenancy -----------------------------------------------------------------


async def test_runs_are_invisible_to_another_tenant(db, world):
    registry = FakeRegistry().program_response()
    await _service(db, world["acme"], registry).process_inbound_message(world["acme_message"])

    # Globex's RLS context must not see Acme's run, even by primary key.
    async with db() as session, session.begin():
        await set_current_tenant(session, world["globex"])
        rows = (await session.execute(select(AIMessageRun))).scalars().all()
        assert rows == []


async def test_another_tenant_cannot_claim_someone_elses_message(db, world):
    """A cross-tenant claim is reported as the security event it is.

    The two failure modes are indistinguishable to the unique index, and the naive reading
    of "conflict" would tell the second tenant the message was simply already processed -
    quietly, and for a message it does not own.
    """
    from app.repositories import ai_runs

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        await ai_runs.claim(
            session,
            tenant_id=world["acme"],
            conversation_id=world["acme_conversation"],
            inbound_message_id=world["acme_message"],
            lead_id=world["acme_lead"],
        )

    async with db() as session:
        await set_current_tenant(session, world["globex"])
        with pytest.raises(ai_runs.CrossTenantClaim) as caught:
            async with session.begin_nested():
                await ai_runs.claim(
                    session,
                    tenant_id=world["globex"],
                    conversation_id=world["acme_conversation"],
                    inbound_message_id=world["acme_message"],
                    lead_id=None,
                )
    assert caught.value.message_id == world["acme_message"]


async def test_a_run_claimed_by_the_same_tenant_is_a_plain_duplicate(db, world):
    """The ordinary case, kept distinct from the cross-tenant one above."""
    from app.repositories import ai_runs

    async def _claim():
        # A fresh transaction per attempt: `set_current_tenant` opens the transaction, so
        # reusing one session for both claims would leave the second in a begun state.
        # Status is read inside the block for the same reason - the row is detached once
        # the session closes.
        async with db() as session, session.begin():
            await set_current_tenant(session, world["acme"])
            run = await ai_runs.claim(
                session,
                tenant_id=world["acme"],
                conversation_id=world["acme_conversation"],
                inbound_message_id=world["acme_message"],
                lead_id=world["acme_lead"],
            )
            return run.status, run.tenant_id

    status, tenant_id = await _claim()
    assert status == AIRunStatus.IN_PROGRESS
    assert tenant_id == world["acme"]

    with pytest.raises(ai_runs.RunAlreadyClaimed) as caught:
        await _claim()
    # The winner row is carried on the exception, so a caller can report what already
    # happened without a second query.
    assert caught.value.existing.tenant_id == world["acme"]


# -- the post-commit stage reached from the webhook processor ----------------


def _messaging_payload(mid: str, text: str) -> dict:
    """A real Meta page messaging event.

    `messaging` sits directly on the `entry`, not under `changes` - the same shape
    `tests/test_phase4_e2e.py::messenger_text` uses, and the only shape the extractor
    accepts. The timestamp is epoch *milliseconds*, and it matters: a stale stamp puts the
    thread outside Meta's 24-hour window and changes what the send path may do.
    """
    stamp = int(datetime.now(UTC).timestamp() * 1000)
    return {
        "object": "page",
        "entry": [
            {
                "id": PAGE,
                "time": int(stamp // 1000),
                "messaging": [
                    {
                        "sender": {"id": "1000000001"},
                        "recipient": {"id": PAGE},
                        "timestamp": stamp,
                        "message": {"mid": mid, "text": text},
                    }
                ],
            }
        ],
    }


async def _enqueue_event(db, world, mid: str, text: str) -> None:
    """A received webhook event, as the Phase 2 inbox would have left it."""
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        session.add(
            MetaWebhookEvent(
                tenant_id=world["acme"],
                meta_connection_id=world["acme_connection"],
                deduplication_key=f"e2e-{mid}",
                object_type="page",
                event_type="MESSAGES",
                payload=_messaging_payload(mid, text),
                status=MetaWebhookEventStatus.RECEIVED,
                received_at=datetime.now(UTC),
            )
        )


async def test_a_webhook_event_becomes_an_attributed_ai_reply(db, world):
    """The vertical slice, driven the way a worker drives it.

    `handle` commits, and only then does `run_ai_stage` run. The final assertions are the
    point of the test: the reply exists, carries real provider attribution, and the webhook
    event is still PROCESSED - the AI stage is additive and never un-ingests a message.
    """
    from app.services.meta_processor import MetaWebhookEventProcessor

    await _enqueue_event(db, world, "mid.e2e.1", "How much for three vans?")
    graph = FakeGraph()
    registry = FakeRegistry().program_response(budget=20, timeline=20, fit=20, authority=20)
    settings = _ai_settings()

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        processor = MetaWebhookEventProcessor(
            session, world["acme"], graph=graph, settings=settings, session_factory=db
        )
        event = await processor.claim_next()
    assert event is not None

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        processor = MetaWebhookEventProcessor(
            session, world["acme"], graph=graph, settings=settings, session_factory=db
        )
        with _patched_registry(registry):
            await processor.handle(event)

    # The transaction above has committed, which is the whole point: only now can the AI
    # service's own sessions see the inbound message. Called earlier it would report every
    # message as `inbound_message_not_found`.
    with _patched_registry(registry):
        results = await processor.run_ai_stage()

    assert len(results) == 1, results
    assert results[0].status == PROCESSED, (results[0].status, results[0].reason)
    assert results[0].response_sent is True
    assert results[0].qualification_total == 80

    sent = await _outbound(db, world["acme"], world["acme_conversation"])
    assert len(sent) == 1
    assert sent[0].text == (
        "Thanks for reaching out! Could you tell me a bit more about what you need?"
    )
    assert sent[0].ai_model == "llama-3.1-8b-instant"
    assert (sent[0].ai_prompt_tokens, sent[0].ai_completion_tokens) == (123, 45)
    # The Graph client received the reply, decrypted token and all.
    assert len(graph.sends) == 1

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        statuses = [
            row.status.value
            for row in ((await session.execute(select(MetaWebhookEvent))).scalars().all())
        ]
        assert statuses == ["PROCESSED"]


async def test_a_failed_ai_stage_leaves_the_webhook_event_processed(db, world):
    """A provider outage must not re-ingest a message or mark a healthy event broken.

    The message is safely stored and the run row carries the failure, so the right
    recovery is a retry of the *run*, not a redelivery of the *event*.
    """
    from app.services.meta_processor import MetaWebhookEventProcessor

    await _enqueue_event(db, world, "mid.e2e.2", "Are you still available?")
    settings = _ai_settings()

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        processor = MetaWebhookEventProcessor(
            session,
            world["acme"],
            graph=FakeGraph(),
            settings=settings,
            session_factory=db,
        )
        event = await processor.claim_next()
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        processor = MetaWebhookEventProcessor(
            session,
            world["acme"],
            graph=FakeGraph(),
            settings=settings,
            session_factory=db,
        )
        await processor.handle(event)

    with _patched_registry(FakeRegistry().program(LLMTransientError("provider down"))):
        results = await processor.run_ai_stage()

    assert [r.status for r in results] == [FAILED]
    # The AI said nothing. The only outbound is the unattributed handoff acknowledgement,
    # which is the honest response to "the model could not answer": a person is coming.
    assert await _ai_outbound(db, world["acme"], world["acme_conversation"]) == []
    assert len(await _outbound(db, world["acme"], world["acme_conversation"])) == 1
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        assert [
            row.status.value
            for row in (await session.execute(select(MetaWebhookEvent))).scalars().all()
        ] == ["PROCESSED"]


async def test_the_ai_stage_is_a_no_op_without_a_session_factory(db, world):
    """Phase 2/4 callers pass a single session and must pay nothing for Phase 5."""
    from app.services.meta_processor import MetaWebhookEventProcessor

    processor = MetaWebhookEventProcessor(db, world["acme"], graph=FakeGraph())
    assert await processor.run_ai_stage() == []


async def test_the_ai_stage_is_a_no_op_when_ai_is_disabled(db, world):
    from app.services.meta_processor import MetaWebhookEventProcessor

    processor = MetaWebhookEventProcessor(
        db,
        world["acme"],
        graph=FakeGraph(),
        settings=_ai_settings(ai_enabled=False),
        session_factory=db,
    )
    processor._last_inbound_ids = [world["acme_message"]]
    assert await processor.run_ai_stage() == []


@contextmanager
def _patched_registry(registry):
    """Swap the registry `run_ai_stage` builds for a scripted one.

    The stage deliberately constructs its own `AIConversationService`, which is right for
    production (no provider state is threaded through the processor) but means a test has
    no injection point. Patching the factory is narrower than patching the service.
    """
    with patch("app.services.ai_qualification.build_registry", return_value=registry):
        yield


# -- prompt contents ---------------------------------------------------------
