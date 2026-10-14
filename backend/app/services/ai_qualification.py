"""The AI qualification pipeline for one inbound message.

The shape of the work, in order, and why the order matters:

    1. claim a run            short transaction, commits
    2. read context/history   short transaction, commits
    3. call the provider      NO transaction open
    4. validate the output    no DB access; a malformed answer dies here
    5. persist + send         short transaction for each
    6. mark the run           short transaction

Step 3 sitting alone in the middle is the load-bearing decision. An LLM call takes
seconds; a Meta send takes hundreds of milliseconds. Holding a transaction - or an open
connection, which is the same thing under a pool - across either of them would pin a
connection per in-flight request, hold row locks on the message while a third party
decides what to say to a lead, and turn any provider slowness into a pool exhaustion
incident. So every database interaction is its own short transaction, and the two network
calls happen with nothing open. The cost is that the run row can be left IN_PROGRESS if
the process dies mid-call, which is a far better failure than a stuck connection pool: the
row is visible, countable, and reclaimable.

Everything about the model is treated as untrusted (see `app/schemas/ai.py`), the outbound
send goes through the existing `ConversationEngine` (never the Graph client directly), and
the provider is reached only through the registry (never a vendor name here).
"""

import json
import logging
import uuid
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.db.tenant_context import set_current_tenant
from app.models.ai_message_run import AIMessageRun
from app.models.conversation import Conversation, Message, MessageDirection
from app.models.human_handoff import HandoffReason
from app.models.lead import Lead, LeadStatus
from app.repositories import ai_runs
from app.schemas.ai import (
    FAILED,
    PROCESSED,
    SKIPPED,
    AIProcessingResult,
    AIResponse,
    QualificationUpdate,
)
from app.services.ai_prompt import (
    ContextProvider,
    ConversationTurn,
    PromptBuilder,
    StaticContextProvider,
)
from app.services.conversations import AIAttribution, ConversationEngine, is_delivered
from app.services.llm_base import LLMError
from app.services.llm_registry import LLMRegistry, build_registry

log = logging.getLogger("app.ai.service")


# Refuse to attribute a total outside 0-100 without noticing. Guards a bad merge or a
# hand-edited row, and costs nothing.
_MAX_TOTAL = 100

#: Recorded on the AI run when a human already owns the conversation. A stable string, not
#: prose, because it is read by operators filtering on run status and asserted in tests.
REASON_HANDOFF_ACTIVE = "handoff_active"

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from app.services.handoffs import HumanHandoffService


class AIConversationService:
    """Turns an inbound message into a qualified, attributed reply."""

    def __init__(
        self,
        session_factory,
        tenant_id: uuid.UUID,
        *,
        registry: LLMRegistry | None = None,
        context_provider: ContextProvider | None = None,
        settings: Settings | None = None,
        graph=None,
        clock=None,
        tenant_ai_enabled: bool = True,
    ) -> None:
        # A *factory*, not a session. The pipeline opens and closes several short
        # transactions per message; a single long-lived session would defeat that.
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self._settings = settings or get_settings()
        self._registry = registry
        self._context = context_provider or StaticContextProvider()
        self._graph = graph
        # Phase 7 addition. The follow-up sequence this pipeline arms is scheduled against
        # a clock, and without injection its timing can only be exercised by waiting four
        # real hours. Defaulting to `SystemClock` keeps every pre-Phase-7 caller
        # byte-identical; nothing about the AI's own behaviour reads it.
        self._clock = clock
        # Phase 10-O.1. The tenant's own switch, consulted alongside - never instead of -
        # `settings.ai_enabled`. Both must be true for the AI to run, because they answer
        # different questions: the deployment flag asks whether AI *can* run here, and this
        # asks whether *this workspace* wants it to. Defaulting to True keeps every
        # pre-existing caller (and test) behaving exactly as before.
        self._tenant_ai_enabled = tenant_ai_enabled
        self._prompt = PromptBuilder(
            history_limit=self._settings.ai_history_limit,
            max_prompt_chars=self._settings.ai_max_prompt_chars,
        )

    @property
    def enabled(self) -> bool:
        """Both halves. See `self._tenant_ai_enabled` for why it is not one or the other.

        `SKIPPED` rather than an error when a tenant has switched it off: a workspace that
        chose not to automate itself is working as configured, not broken.
        """
        return self._settings.ai_enabled and self._tenant_ai_enabled

    # -- entry point ------------------------------------------------------

    async def process_inbound_message(self, inbound_message_id: uuid.UUID) -> AIProcessingResult:
        """Process one inbound message. Idempotent per message.

        Returns a result describing what happened rather than raising for expected
        outcomes, so a caller processing a batch can keep going and an operator can see
        the decision. Genuine bugs still propagate.
        """
        if not self.enabled:
            return AIProcessingResult(status=SKIPPED, reason="ai_disabled")

        # -- 1. claim ------------------------------------------------------
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            inbound = await self._load_inbound(session, inbound_message_id)
            if inbound is None:
                # Loud on purpose. The overwhelmingly common cause is calling this stage
                # while the caller's transaction is still open - the AI service uses its
                # own sessions and so cannot see rows that have not committed yet. A
                # silent skip there looks identical to "the AI feature is broken", which
                # is exactly the wrong thing for an operator to be told at 3am.
                log.warning(
                    "ai.run.message_not_found",
                    extra={
                        "inbound_message_id": str(inbound_message_id),
                        "hint": "the inbound message may not be committed yet",
                    },
                )
                return AIProcessingResult(status=SKIPPED, reason="inbound_message_not_found")
            if not (inbound.text or "").strip():
                # An attachment-only message has nothing to qualify. Claimed and skipped
                # so a later retry does not re-examine it either.
                run = await ai_runs.claim(
                    session,
                    tenant_id=self._tenant_id,
                    conversation_id=inbound.conversation_id,
                    inbound_message_id=inbound.id,
                    lead_id=None,
                )
                await ai_runs.mark_skipped(session, run, reason="no_text")
                return AIProcessingResult(status=SKIPPED, reason="no_text")
            # Recorded on the run up front rather than discovered later, so a run that
            # failed mid-flight still says which lead it was about.
            inbound_lead_id = await self._lead_id_for(session, inbound.conversation_id)
            try:
                run = await ai_runs.claim(
                    session,
                    tenant_id=self._tenant_id,
                    conversation_id=inbound.conversation_id,
                    inbound_message_id=inbound.id,
                    lead_id=inbound_lead_id,
                )
            except ai_runs.CrossTenantClaim:
                # A message id belonging to another tenant reached us. That is a
                # containment failure, not a delivery quirk, so it is re-raised instead of
                # being folded into a "skipped" result.
                log.error(
                    "ai.claim.cross_tenant",
                    extra={"inbound_message_id": str(inbound.id)},
                )
                raise
            except ai_runs.RunAlreadyClaimed as already:
                log.info(
                    "ai.run.already_claimed",
                    extra={
                        "conversation_id": str(inbound.conversation_id),
                        "inbound_message_id": str(inbound.id),
                        "existing_status": already.existing.status.value,
                    },
                )
                return AIProcessingResult(
                    status=SKIPPED,
                    reason="already_claimed",
                    lead_id=str(already.existing.lead_id) if already.existing.lead_id else None,
                )
            run_id = run.id
            conversation_id = inbound.conversation_id

            # -- 1b. a human already has this conversation -------------------
            # Checked inside the claim transaction so the decision is made against
            # committed state, and before the provider call so a conversation in human
            # hands costs no tokens and cannot produce an autonomous answer.
            #
            # The inbound message is already stored - Phase 4 committed it - and it stays
            # visible in the thread. What is suppressed is the *reply*, not the evidence.
            if await self._handoff_is_active(conversation_id):
                await ai_runs.mark_skipped(session, run, reason=REASON_HANDOFF_ACTIVE)
                log.info(
                    "handoff_ai_suppressed",
                    extra={
                        "tenant_id": str(self._tenant_id),
                        "conversation_id": str(conversation_id),
                        "inbound_message_id": str(inbound.id),
                        "stage": "before_provider",
                    },
                )
                return AIProcessingResult(
                    status=SKIPPED,
                    reason=REASON_HANDOFF_ACTIVE,
                    lead_id=str(inbound_lead_id) if inbound_lead_id else None,
                )

        # -- 2. context and history ---------------------------------------
        # Read in its own transaction, after the claim has committed. If this fails the
        # run is durably IN_PROGRESS, which is the honest state: the claim is real, the
        # work is not done.
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            conversation = await self._load_conversation(session, conversation_id)
            lead = await self._load_lead(session, conversation)
            history = await self._load_history(
                session, conversation_id, exclude_message_id=inbound_message_id
            )
            # The current message is passed as the retrieval query. Phase 5's static
            # provider ignores it, so this is behaviour-preserving when RAG is off.
            business_context = await self._context.get_context(
                self._tenant_id, query=inbound.text or ""
            )

        request = self._prompt.build(
            context=business_context,
            lead_summary=_summarize_lead(lead),
            history=history,
            current_text=inbound.text or "",
        )

        # -- 3. provider call (no transaction open) ------------------------
        registry = self._registry or build_registry(self._settings)
        try:
            result = await registry.generate_structured(request)
        except LLMError as exc:
            return await self._record_failure(run_id, exc.category)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("ai.provider.unexpected_error")
            return await self._record_failure(run_id, "unexpected", detail=type(exc).__name__)

        # -- 4. validate ---------------------------------------------------
        try:
            parsed = AIResponse.model_validate_json(result.content)
        except (ValidationError, ValueError) as exc:
            # Malformed output is a real failure, not something to repair by guessing.
            # Escalation already happened inside the registry for unparseable answers;
            # if every model was unusable we stop here and say so.
            log.warning(
                "ai.response.invalid",
                extra={
                    "provider": result.provider,
                    "model": result.model,
                    "error": _first_line(exc),
                },
            )
            return await self._record_failure(run_id, "invalid_response")
        except json.JSONDecodeError:  # pragma: no cover - model_validate_json covers it
            return await self._record_failure(run_id, "invalid_response")

        # -- 4b. re-check: a handoff may have landed during the call ----------
        # The LLM call takes seconds and a human escalation can land inside that window -
        # a person claiming the queue, or a *second* inbound message that escalated while
        # this one was still thinking. Without this check the slow run would answer
        # autonomously over the top of a conversation that now belongs to a person.
        #
        # This is the same shape as Phase 7's second eligibility check, for the same
        # reason: the first check cannot close a window that opened during an external
        # call.
        if await self._handoff_is_active(conversation_id):
            return await self._record_suppressed_after_handoff(run_id, conversation_id)

        update = parsed.qualification
        total = update.total
        high_intent = total > self._settings.ai_handoff_score_threshold
        handoff = parsed.human_handoff or high_intent
        handoff_reason = parsed.handoff_reason or (
            f"qualification score {total} above threshold "
            f"{self._settings.ai_handoff_score_threshold}"
            if high_intent
            else None
        )

        # -- 5. qualification, then send ------------------------------------
        merged_total = await self._apply_qualification(
            conversation_id, update, total, high_intent, handoff
        )
        sent, outbound_id = await self._send(conversation_id, parsed.message, result)

        # Phase 8. A handoff *replaces* the follow-up sequence rather than sitting beside
        # it: a conversation that needs a person must not also be scheduled for automated
        # messages four hours later. And it is the only branch that creates one - a
        # conversation with no escalation gets no handoff, so this cannot become a second
        # path into the handoff table.
        if handoff:
            await self._escalate(
                conversation_id,
                reason=HandoffReason.HIGH_INTENT,
                detail=handoff_reason,
                source_message_id=inbound_message_id,
                qualification_total=total,
                # The AI's own reply *is* the acknowledgement when it landed. Sending the
                # deterministic line as well would put two "we'll be in touch" messages in
                # one thread, which reads as a stuck bot.
                already_delivered=sent,
            )
        else:
            await self._maybe_start_follow_up_sequence(conversation_id, sent)

        # -- 6. mark the run -------------------------------------------------
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            run = await session.get(AIMessageRun, run_id)
            if run is not None:
                await ai_runs.mark_processed(
                    session,
                    run,
                    provider=result.provider,
                    model=result.model,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    latency_ms=result.latency_ms,
                    qualification_total=total,
                    high_intent=high_intent,
                    human_handoff_required=handoff,
                    handoff_reason=handoff_reason,
                    ai_response=parsed.model_dump(mode="json"),
                    outbound_message_id=outbound_id,
                )

        log.info(
            "ai.run.processed",
            extra={
                "conversation_id": str(conversation_id),
                "provider": result.provider,
                "model": result.model,
                "qualification_total": total,
                "merged_total": merged_total,
                "high_intent": high_intent,
                "human_handoff_required": handoff,
                "response_sent": sent,
            },
        )
        return AIProcessingResult(
            status=PROCESSED,
            provider=result.provider,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=result.latency_ms,
            qualification_total=merged_total,
            high_intent=high_intent,
            human_handoff_required=handoff,
            handoff_reason=handoff_reason,
            response_sent=sent,
            outbound_message_id=str(outbound_id) if outbound_id else None,
            lead_id=str(conversation.lead_id) if conversation.lead_id else None,
        )

    # -- steps -------------------------------------------------------------

    def _handoff_service(self) -> "HumanHandoffService":
        """The Phase 8 service, built on this pipeline's own session factory and clock.

        Constructed per call rather than cached: it holds no state, and building it here
        means the clock and settings a test injected into the AI service are the same ones
        the handoff sees. Two services with two different clocks would be a confusing test
        failure to debug.
        """
        from app.services.handoffs import HumanHandoffService

        return HumanHandoffService(
            self._session_factory,
            self._tenant_id,
            settings=self._settings,
            clock=self._clock,
        )

    async def _handoff_is_active(self, conversation_id: uuid.UUID) -> bool:
        return await self._handoff_service().has_active(conversation_id)

    async def _escalate(
        self,
        conversation_id: uuid.UUID,
        *,
        reason: HandoffReason,
        detail: str | None,
        source_message_id: uuid.UUID | None,
        qualification_total: int | None = None,
        already_delivered: bool = False,
    ) -> None:
        """Hand the conversation to a human, and acknowledge the lead if nobody has.

        Never raises. By the time this runs the AI's own reply has already been delivered
        or has already failed, and a real lead has heard from us either way; failing the
        whole message because the internal queue could not be written would throw that away.
        So a handoff failure is logged and the reply stands.

        The one thing that must not be lost is the escalation itself, so this logs at
        `error` rather than `exception` noise - an operator needs to know the queue is
        missing a row that the lead's conversation implies exists.
        """
        service = self._handoff_service()
        try:
            creation = await service.create_or_get_active_handoff(
                conversation_id,
                reason=reason,
                detail=detail,
                source_message_id=source_message_id,
                qualification_total=qualification_total,
            )
            if creation is None or not creation.created:
                # Disabled, unknown conversation, or already escalating. Nothing to do:
                # another thread already owns the acknowledgement and the notification.
                return
            await service.acknowledge(
                creation.handoff.id,
                conversation=await self._load_conversation_model(conversation_id),
                graph=self._graph,
                already_delivered=already_delivered,
            )
        except Exception:  # noqa: BLE001 - the lead's reply is already persisted
            log.exception(
                "handoff.escalation_failed",
                extra={
                    "tenant_id": str(self._tenant_id),
                    "conversation_id": str(conversation_id),
                    "reason": reason.value,
                },
            )

    async def _load_conversation_model(self, conversation_id: uuid.UUID) -> Conversation | None:
        async with self._session_factory() as session:
            await set_current_tenant(session, self._tenant_id)
            return await self._load_conversation(session, conversation_id)

    async def _record_suppressed_after_handoff(
        self, run_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> AIProcessingResult:
        """A run that finished too late: record it, send nothing.

        Marked SKIPPED rather than FAILED. Nothing went wrong - the run did its work, and the
        work turned out to be unwanted. Calling it a failure would put provider errors and
        "a person got there first" in the same bucket, and the retry that follows a FAILED
        run would do the same suppressed thing again.
        """
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            run = await session.get(AIMessageRun, run_id)
            if run is not None:
                await ai_runs.mark_skipped(session, run, reason=REASON_HANDOFF_ACTIVE)
        log.info(
            "handoff_ai_suppressed",
            extra={
                "tenant_id": str(self._tenant_id),
                "conversation_id": str(conversation_id),
                "run_id": str(run_id),
                "stage": "after_provider",
            },
        )
        return AIProcessingResult(status=SKIPPED, reason=REASON_HANDOFF_ACTIVE)

    async def _maybe_start_follow_up_sequence(self, conversation_id: uuid.UUID, sent: bool) -> None:
        """Start Phase 7's sequence when the AI's first reply actually went out.

        Two things are deliberate here.

        It runs only on a real delivery. A Meta rejection is not "the lead has heard from
        us", and scheduling a follow-up off a failed send would produce a sequence
        starting from a message nobody received.

        It runs only for the AI's own reply, never for a follow-up's. `FollowUpService`
        sends through this same engine, so without this guard every follow-up would schedule
        another follow-up and the sequence would never terminate.
        """
        if not sent or not self._settings.follow_up_enabled:
            return
        from app.services.follow_ups import FollowUpService

        try:
            await FollowUpService(
                self._session_factory,
                self._tenant_id,
                settings=self._settings,
                clock=self._clock,
            ).on_reply_sent(conversation_id)
        except Exception:  # noqa: BLE001 - the reply already went; do not lose it
            # The AI's response is persisted and sent at this point. Failing to arm a
            # follow-up is a problem for a later poll, not a reason to fail the message
            # that was just delivered to a real lead.
            log.exception(
                "ai.follow_up.schedule_failed", extra={"conversation_id": str(conversation_id)}
            )

    async def _record_failure(
        self, run_id: uuid.UUID, category: str, *, detail: str | None = None
    ) -> AIProcessingResult:
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            run = await session.get(AIMessageRun, run_id)
            if run is not None:
                await ai_runs.mark_failed(session, run, error_category=category)
                conversation_id = run.conversation_id
                inbound_message_id = run.inbound_message_id
            else:  # pragma: no cover - the run was claimed moments ago
                conversation_id = inbound_message_id = None
        log.warning(
            "ai.run.failed",
            extra={"run_id": str(run_id), "error_category": category, "detail": detail},
        )
        # No reply is sent on failure. A holding message generated locally would be
        # pretending the AI worked, and the PRD reserves that copy for the window-aware
        # path; guessing at it here would send the wrong thing to a real lead.
        #
        # So the lead gets nothing, which means a real person is now the only way this
        # conversation gets answered - hence the escalation. `AI_FAILURE` rather than
        # reusing the qualification reason, because "we could not run the model" and "the
        # lead is a hot prospect" are different facts about different work.
        #
        # Every failed message therefore produces one escalation. That is correct rather
        # than a flood: each one is a distinct conversation where someone is waiting for a
        # reply nobody sent, and one escalation per conversation is exactly the queue an
        # operator wants. `HANDOFF_ENABLED` is the lever for suppressing the lot.
        if conversation_id is not None:
            await self._escalate(
                conversation_id,
                reason=HandoffReason.AI_FAILURE,
                detail=f"ai {category}" + (f": {detail}" if detail else ""),
                source_message_id=inbound_message_id,
                already_delivered=False,
            )
        return AIProcessingResult(status=FAILED, reason=category)

    async def _apply_qualification(
        self,
        conversation_id: uuid.UUID,
        update: QualificationUpdate,
        total: int,
        high_intent: bool,
        handoff: bool,
    ) -> int:
        """Merge the model's scores into the Lead, keeping the better score per dimension.

        `max` rather than overwrite, and the reasoning is worth stating: a model can
        under-score a dimension it had less context about, and overwriting would let a
        later, worse-informed run *lower* a lead's recorded qualification. Leads are
        monotonic in this product - once we know they have budget, an ambiguous later
        message does not un-know that. Unrelated keys already in the JSONB are preserved,
        because this column is shared with other phases.
        """
        if not 0 <= total <= _MAX_TOTAL:  # pragma: no cover - schema forbids it
            log.warning("ai.qualification.out_of_range_total", extra={"total": total})
            return total
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            conversation = await self._load_conversation(session, conversation_id)
            lead = await self._load_lead(session, conversation)
            if lead is None:
                # Normal: someone messaged the page without submitting an ad form.
                return total
            existing = dict(lead.qualification or {})
            # Start from the existing document, not from an empty one. The column is
            # shared with other phases, and rewriting it with only the four dimensions
            # would silently delete whatever else is stored there.
            merged = dict(existing)
            merged.update(
                {
                    name: max(int(existing.get(name, 0)), getattr(update, name))
                    for name in update.model_dump()
                }
            )
            merged["total"] = min(sum(merged[name] for name in update.model_dump()), _MAX_TOTAL)
            merged["high_intent"] = bool(existing.get("high_intent", False)) or high_intent
            if handoff:
                # Phase 5's only lead-lifecycle write. `HUMAN_HANDOFF` already exists on
                # the model, so this needs no new state machine - and Phase 8 owns what
                # happens next. It is not a new status: a lead already in CLOSED stays
                # CLOSED, because a late AI observation must not reopen a dead deal.
                if lead.status not in (LeadStatus.CLOSED, LeadStatus.GHOSTED):
                    lead.status = LeadStatus.HUMAN_HANDOFF
            elif lead.status is LeadStatus.NEW:
                # The PRD's NEW -> REPLIED edge, on the same reasoning as the handoff
                # write: the lead has now actually been answered, and nothing before this
                # phase ever moved the status off NEW.
                #
                # Only from NEW, and only when not handing off, for two reasons. A late
                # AI observation must not pull a CLOSED, GHOSTED or HUMAN_HANDOFF lead
                # backwards - the same monotonicity rule that governs the scores above.
                # And a lead that needs a human is HUMAN_HANDOFF, not REPLIED, so the
                # `if` branch has to win rather than be overwritten by this one.
                lead.status = LeadStatus.REPLIED
            lead.qualification = merged
            await session.flush()
            return int(merged["total"])

    async def _send(
        self, conversation_id: uuid.UUID, text: str, result
    ) -> tuple[bool, uuid.UUID | None]:
        """Send the reply through the Phase 4 engine, attributing it to the provider.

        Returns `(sent, outbound_message_id)`. `sent` reflects real delivery, not the
        HTTP status: Phase 4 established that a send can be persisted and still fail at
        Meta, and reporting that as a success would be a lie the operator cannot detect.
        """
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            conversation = await self._load_conversation(session, conversation_id)
            engine = ConversationEngine(
                session, self._tenant_id, graph=self._graph, settings=self._settings
            )
            message = await engine.send_outbound(
                conversation,
                text=text,
                ai_attribution=AIAttribution(
                    model=result.model,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    latency_ms=result.latency_ms,
                ),
            )
        return is_delivered(message), message.id

    # -- reads -------------------------------------------------------------

    async def _load_inbound(self, session, message_id: uuid.UUID) -> Message | None:
        return await session.scalar(
            select(Message).where(
                Message.id == message_id,
                Message.tenant_id == self._tenant_id,
                Message.direction == MessageDirection.INBOUND,
            )
        )

    async def _lead_id_for(self, session, conversation_id: uuid.UUID) -> uuid.UUID | None:
        """The lead this thread belongs to, or `None`.

        A conversation may have no lead - someone can message a page without ever
        submitting an ad form - so `None` is a normal answer and never an error.
        """
        lead_id = await session.scalar(
            select(Conversation.lead_id).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == self._tenant_id,
            )
        )
        return lead_id

    async def _load_conversation(self, session, conversation_id: uuid.UUID) -> Conversation | None:
        return await session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == self._tenant_id,
            )
        )

    async def _load_lead(self, session, conversation: Conversation | None) -> Lead | None:
        if conversation is None or conversation.lead_id is None:
            return None
        return await session.scalar(
            select(Lead).where(Lead.id == conversation.lead_id, Lead.tenant_id == self._tenant_id)
        )

    async def _load_history(
        self, session, conversation_id: uuid.UUID, exclude_message_id: uuid.UUID
    ) -> list[ConversationTurn]:
        """Newest N turns, re-chronological, excluding the message being answered.

        The in-memory cap is the same one the prompt applies, so the query does not drag
        rows it will immediately discard. Excluding the current message matters: it is
        already in `<current_message>`, and repeating it in the history block invites the
        model to answer the wrong turn.
        """
        rows = (
            (
                await session.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.tenant_id == self._tenant_id,
                        Message.text.is_not(None),
                        Message.id != exclude_message_id,
                    )
                    .order_by(Message.sent_at.desc(), Message.id.desc())
                    .limit(self._settings.ai_history_limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            ConversationTurn(direction=r.direction.value, text=r.text or "") for r in reversed(rows)
        ]


def _summarize_lead(lead: Lead | None) -> str | None:
    if lead is None:
        return None
    parts = []
    if lead.name:
        parts.append(f"Name: {lead.name}")
    if lead.email:
        parts.append(f"Email: {lead.email}")
    if lead.source:
        parts.append(f"Came from: {lead.source.value}")
    return "\n".join(parts) if parts else None


def _first_line(exc: Exception) -> str:
    """First line of a validation error only - never the model's raw content."""
    return str(exc).splitlines()[0][:300]
