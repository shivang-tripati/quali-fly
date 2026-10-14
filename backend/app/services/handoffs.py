"""The handoff workflow: one place that turns an escalation into work for a person.

Everything that needs to know "is a human handling this conversation, and what do we owe
them" comes through here. That includes three call sites that are easy to mistake for
unrelated code:

  * Phase 5, when the AI decides to escalate.
  * Phase 7, before every follow-up send and before the ghost transition.
  * Phase 5 again, to refuse to answer autonomously while a handoff is open.

One service, one decision. A second rule in a second module would eventually disagree with
this one, and the disagreement would be "an automated message was sent to someone who had
asked for a person".

The transaction shape
---------------------
    transaction 1:  create the handoff, mark the lead, cancel pending follow-ups,
                    create the notification                    -> COMMIT
    (nothing open)  send the acknowledgement through ConversationEngine
    transaction 2:  record delivered / failed, notify if it failed  -> COMMIT

The handoff is durable and visible to a human *before* anything is sent to the lead. That
ordering is the point: if the process dies during the send, the worst outcome is a lead who
was not told and an agent who can see that, rather than a lead who believes someone is
coming and no queue row explaining why not.

Idempotency
-----------
`create_or_get_active_handoff` is safe to call from a retried webhook, a retried AI run and
a restarted worker. The database decides who created the handoff (see
`repositories.handoffs.create_active`), and only that thread acknowledges or notifies. A
duplicate call therefore costs one `INSERT ... ON CONFLICT DO NOTHING` and one read, and
changes nothing a lead or a human can observe.

Two paths produce a handoff, and both go through this method:

    Phase 5 handoff flag  ->  reason = EXPLICIT_HUMAN_REQUEST or HIGH_INTENT
    Phase 5 provider/API failure  ->  reason = AI_FAILURE
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.db.tenant_context import set_current_tenant
from app.models.conversation import Conversation, Message, MessageDirection
from app.models.human_handoff import (
    REASON_PRIORITY,
    HandoffReason,
    HandoffStatus,
    HumanHandoff,
)
from app.models.internal_notification import NotificationType
from app.models.lead import Lead, LeadStatus
from app.repositories import handoffs as handoff_repo
from app.repositories import notifications as notification_repo
from app.repositories import scheduled_tasks as task_repo
from app.repositories import tenants as tenant_repo

log = logging.getLogger("app.handoff.service")

#: Lead states a handoff must never overwrite. `CLOSED` and `GHOSTED` are terminal business
#: outcomes; arriving late with a good qualification score does not reopen them. `HUMAN_HANDOFF`
#: is deliberately absent - re-asserting it is a no-op, and allowing it means a duplicate
#: escalation does not have to special-case an already-correct status.
_TERMINAL_LEAD_STATUSES = (LeadStatus.CLOSED, LeadStatus.GHOSTED)

#: Acknowledgement provenance, stored on the handoff so an operator can tell how the lead
#: was told.
ACK_SOURCE_AI_REPLY = "AI_REPLY"
ACK_SOURCE_TEMPLATE = "TEMPLATE"


class HandoffNotFound(Exception):
    """No handoff with that id in this tenant. Includes "exists in another tenant"."""


class HandoffAlreadyAssigned(Exception):
    """Someone else already owns it. Carries who, so the loser can be told."""

    def __init__(self, handoff: HumanHandoff) -> None:
        self.handoff = handoff
        self.assigned_user_id = handoff.assigned_user_id
        super().__init__("handoff is already assigned")


class HandoffInvalidState(Exception):
    """The transition is not legal from the current status."""

    def __init__(self, handoff: HumanHandoff, action: str) -> None:
        self.handoff = handoff
        self.status = handoff.status
        super().__init__(f"cannot {action} a handoff in state {handoff.status.value}")


class HandoffAssignmentNotAllowed(Exception):
    """The target user does not belong to this tenant."""


class HandoffResolutionNotAllowed(Exception):
    """The handoff is already terminal."""


@dataclass
class HandoffCreation:
    """What `create_or_get_active_handoff` did.

    `created` is the whole contract: it is the only thing that authorises acknowledging the
    lead and notifying a human. A caller that ignores it will re-send an acknowledgement on
    every duplicate event, which is the specific failure this whole design exists to avoid.
    """

    handoff: HumanHandoff
    created: bool
    follow_ups_cancelled: int


class HumanHandoffService:
    """One tenant's handoff workflow."""

    def __init__(
        self,
        session_factory,
        tenant_id: uuid.UUID,
        *,
        settings: Settings | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self._settings = settings or get_settings()
        # Injected so acknowledgement and assignment timestamps are assertable without
        # sleeping, matching Phase 7's treatment of the same problem.
        self._clock = clock or SystemClock()

    @property
    def enabled(self) -> bool:
        return self._settings.handoff_enabled

    def now(self) -> datetime:
        return self._clock.now()

    # -- creation ----------------------------------------------------------

    async def create_or_get_active_handoff(
        self,
        conversation_id: uuid.UUID,
        *,
        reason: HandoffReason,
        detail: str | None = None,
        source_message_id: uuid.UUID | None = None,
        ai_run_id: uuid.UUID | None = None,
        qualification_total: int | None = None,
    ) -> HandoffCreation | None:
        """Escalate a conversation, or return the escalation already in flight.

        Returns `None` when handoffs are disabled or the conversation is not in this
        tenant, so a caller in the AI pipeline can treat "no handoff" as "carry on exactly
        as Phase 7 did" without branching on a configuration flag.
        """
        if not self.enabled:
            return None

        now = self.now()
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            conversation = await self._load_conversation(session, conversation_id)
            if conversation is None:
                # Not a validation error to raise: a message id from another tenant, or a
                # conversation deleted between the AI run and here. Either way there is
                # nothing to escalate, and the caller already handles the null case.
                log.warning(
                    "handoff.conversation_not_found",
                    extra={
                        "tenant_id": str(self._tenant_id),
                        "conversation_id": str(conversation_id),
                    },
                )
                return None

            lead = await self._load_lead(session, conversation)
            preview, preview_at = await self._latest_inbound(session, conversation)

            handoff, created = await handoff_repo.create_active(
                session,
                tenant_id=self._tenant_id,
                conversation_id=conversation.id,
                lead_id=conversation.lead_id,
                reason=reason,
                priority=REASON_PRIORITY[reason],
                detail=detail,
                context={"qualification_total": qualification_total},
                now=now,
            )
            if not created:
                log.info(
                    "handoff_duplicate",
                    extra={
                        "tenant_id": str(self._tenant_id),
                        "handoff_id": str(handoff.id),
                        "conversation_id": str(conversation.id),
                        "lead_id": str(conversation.lead_id) if conversation.lead_id else None,
                        "reason": reason.value,
                        "existing_status": handoff.status.value,
                    },
                )
                return HandoffCreation(handoff=handoff, created=False, follow_ups_cancelled=0)

            await self._mark_lead_handed_off(session, lead)
            # Same transaction as the insert, for the same reason Phase 7 cancels follow-ups
            # inside the inbound transaction: a follow-up that is still PENDING when the
            # handoff commits must not survive it. The claimed-task case is handled by
            # `FollowUpService`'s eligibility re-check, because a PROCESSING row cannot be
            # cancelled from here without stranding a worker mid-send.
            cancelled = await task_repo.cancel_pending_for_conversation(
                session,
                tenant_id=self._tenant_id,
                conversation_id=conversation.id,
                now=now,
            )

            handoff.lead_name = lead.name if lead is not None else None
            handoff.latest_inbound_text = preview
            handoff.latest_inbound_at = preview_at
            handoff.qualification_total = qualification_total
            handoff.source_message_id = source_message_id
            handoff.ai_run_id = ai_run_id
            await session.flush()

            await notification_repo.create(
                session,
                tenant_id=self._tenant_id,
                handoff_id=handoff.id,
                conversation_id=conversation.id,
                lead_id=conversation.lead_id,
                type=NotificationType.HANDOFF_CREATED,
                title=_notification_title(reason, lead),
                message=_notification_message(reason, lead, handoff, preview),
                payload=self._notification_payload(handoff, conversation, lead, preview),
                now=now,
            )

        log.info(
            "handoff_created",
            extra={
                "tenant_id": str(self._tenant_id),
                "handoff_id": str(handoff.id),
                "conversation_id": str(conversation_id),
                "lead_id": str(handoff.lead_id) if handoff.lead_id else None,
                "reason": reason.value,
                "follow_ups_cancelled": cancelled,
            },
        )
        if cancelled:
            log.info(
                "handoff_followup_suppressed",
                extra={
                    "tenant_id": str(self._tenant_id),
                    "handoff_id": str(handoff.id),
                    "conversation_id": str(conversation_id),
                    "cancelled_count": cancelled,
                },
            )
        return HandoffCreation(handoff=handoff, created=True, follow_ups_cancelled=cancelled)

    # -- acknowledgement ---------------------------------------------------

    async def acknowledge(
        self,
        handoff_id: uuid.UUID,
        *,
        conversation: Conversation,
        graph=None,
        already_delivered: bool = False,
    ) -> bool:
        """Tell the lead a person is coming. Returns whether it was delivered.

        `already_delivered=True` when the AI's own message reached the lead. The PRD wants
        an acknowledgement on escalation, and the AI's reply *is* one - it is the message
        the lead is reading at the moment they are handed over. Sending the deterministic
        line on top of it would put two "we'll be in touch" messages in one thread, which
        reads as a stuck bot.

        `graph` is threaded rather than constructed so the service stays constructible in
        tests with a fake transport, exactly as `ConversationEngine` is.
        """
        if already_delivered:
            now = self.now()
            async with self._session_factory() as session, session.begin():
                await set_current_tenant(session, self._tenant_id)
                await handoff_repo.record_acknowledgement(
                    session,
                    tenant_id=self._tenant_id,
                    handoff_id=handoff_id,
                    now=now,
                    source=ACK_SOURCE_AI_REPLY,
                )
            log.info(
                "handoff_ack_sent",
                extra={
                    "tenant_id": str(self._tenant_id),
                    "handoff_id": str(handoff_id),
                    "source": ACK_SOURCE_AI_REPLY,
                },
            )
            return True

        from app.services.conversations import ConversationEngine, is_delivered

        now = self.now()
        reason: str | None = None
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            engine = ConversationEngine(
                session, self._tenant_id, graph=graph, settings=self._settings
            )
            message = await engine.send_outbound(
                conversation,
                text=self._settings.handoff_acknowledgement_text,
            )
            if is_delivered(message):
                await handoff_repo.record_acknowledgement(
                    session,
                    tenant_id=self._tenant_id,
                    handoff_id=handoff_id,
                    now=now,
                    source=ACK_SOURCE_TEMPLATE,
                )
            else:
                # A category from the existing message vocabulary, never a Meta payload.
                reason = message.failure_reason or "unknown"

        if reason is None:
            log.info(
                "handoff_ack_sent",
                extra={
                    "tenant_id": str(self._tenant_id),
                    "handoff_id": str(handoff_id),
                    "source": ACK_SOURCE_TEMPLATE,
                },
            )
            return True

        # The handoff stays open and assigned to nobody. A lead who was not told is a lead
        # an agent should probably phone, so the failure is recorded *and* raised as a
        # notification rather than swallowed.
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            await handoff_repo.record_acknowledgement_failure(
                session,
                tenant_id=self._tenant_id,
                handoff_id=handoff_id,
                reason=reason,
                now=self.now(),
            )
            await notification_repo.create(
                session,
                tenant_id=self._tenant_id,
                handoff_id=handoff_id,
                conversation_id=conversation.id,
                lead_id=conversation.lead_id,
                type=NotificationType.HANDOFF_ACK_FAILED,
                title="Handoff acknowledgement not delivered",
                message=(
                    "A conversation was escalated to a human, but the acknowledgement to the "
                    f"lead failed ({reason}). The lead may not know anyone is coming."
                ),
                payload={"failure_reason": reason},
                now=self.now(),
            )
        log.warning(
            "handoff_ack_failed",
            extra={
                "tenant_id": str(self._tenant_id),
                "handoff_id": str(handoff_id),
                "reason": reason,
            },
        )
        return False

    # -- human workflow ----------------------------------------------------

    async def claim(self, handoff_id: uuid.UUID, user_id: uuid.UUID) -> HumanHandoff:
        """Take an unassigned handoff. Concurrency-safe; the loser is told who won."""
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            claimed, current = await handoff_repo.claim(
                session,
                tenant_id=self._tenant_id,
                handoff_id=handoff_id,
                user_id=user_id,
                now=self.now(),
            )
            if claimed is not None:
                log.info(
                    "handoff_assigned",
                    extra={
                        "tenant_id": str(self._tenant_id),
                        "handoff_id": str(handoff_id),
                        "assigned_user_id": str(user_id),
                        "via": "claim",
                    },
                )
                return claimed
            if current is None:
                raise HandoffNotFound(str(handoff_id))
            if current.assigned_user_id is not None:
                raise HandoffAlreadyAssigned(current)
            # Unassigned but not OPEN: it was resolved or cancelled between the read and
            # this claim. A stale button, not a lost race.
            raise HandoffInvalidState(current, "claim")

    async def assign(
        self, handoff_id: uuid.UUID, user_id: uuid.UUID, *, actor_tenant_id: uuid.UUID | None = None
    ) -> HumanHandoff:
        """Put a handoff on a specific agent, reassigning if necessary.

        The membership check is the only thing standing between a valid `users.id` from
        another tenant and a handoff owned by someone who cannot even see it. RLS cannot
        do this job - the assignee is not the caller - so it is checked explicitly, and the
        user id never reaches the database unverified.
        """
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            existing = await handoff_repo.get(
                session, tenant_id=self._tenant_id, handoff_id=handoff_id
            )
            if existing is None:
                raise HandoffNotFound(str(handoff_id))
            if existing.status not in (
                HandoffStatus.OPEN,
                HandoffStatus.ASSIGNED,
                HandoffStatus.IN_PROGRESS,
            ):
                raise HandoffInvalidState(existing, "assign")
            if (
                await tenant_repo.get_membership(
                    session, tenant_id=self._tenant_id, user_id=user_id
                )
                is None
            ):
                raise HandoffAssignmentNotAllowed(str(user_id))
            assigned, current = await handoff_repo.assign(
                session,
                tenant_id=self._tenant_id,
                handoff_id=handoff_id,
                user_id=user_id,
                now=self.now(),
            )
        if assigned is not None:
            log.info(
                "handoff_assigned",
                extra={
                    "tenant_id": str(self._tenant_id),
                    "handoff_id": str(handoff_id),
                    "assigned_user_id": str(user_id),
                    "via": "assign",
                },
            )
            return assigned
        raise HandoffInvalidState(current, "assign")  # type: ignore[arg-type]

    async def start(self, handoff_id: uuid.UUID) -> HumanHandoff:
        """OPEN|ASSIGNED -> IN_PROGRESS."""
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            started, current = await handoff_repo.start(
                session, tenant_id=self._tenant_id, handoff_id=handoff_id, now=self.now()
            )
        if started is not None:
            return started
        if current is None:
            raise HandoffNotFound(str(handoff_id))
        raise HandoffInvalidState(current, "start")

    async def resolve(self, handoff_id: uuid.UUID, *, note: str | None = None) -> HumanHandoff:
        """Close a handoff out.

        Deliberately does *not* restart the follow-up sequence, re-enable the AI, or move the
        lead anywhere. A resolved escalation is a historical record of a conversation a
        person handled, and the follow-up sequence for it ended when the handoff cancelled
        it. Re-arming automation afterwards would be a product decision about who owns the
        relationship after a human has spoken to someone, and this phase has no basis to
        make it.
        """
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            resolved, current = await handoff_repo.resolve(
                session,
                tenant_id=self._tenant_id,
                handoff_id=handoff_id,
                note=note,
                now=self.now(),
            )
        if resolved is not None:
            log.info(
                "handoff_resolved",
                extra={
                    "tenant_id": str(self._tenant_id),
                    "handoff_id": str(handoff_id),
                },
            )
            return resolved
        if current is None:
            raise HandoffNotFound(str(handoff_id))
        raise HandoffResolutionNotAllowed(str(handoff_id))

    # -- reads used by other phases ---------------------------------------

    async def has_active(self, conversation_id: uuid.UUID) -> bool:
        """Is a human handling this conversation right now?

        The single question Phase 5 and Phase 7 both ask. Exists as a method so those two
        call sites cannot end up with subtly different definitions of "active".
        """
        if not self.enabled:
            return False
        async with self._session_factory() as session:
            await set_current_tenant(session, self._tenant_id)
            return await handoff_repo.has_active(
                session, tenant_id=self._tenant_id, conversation_id=conversation_id
            )

    # -- internals ---------------------------------------------------------

    async def _load_conversation(
        self, session: AsyncSession, conversation_id: uuid.UUID
    ) -> Conversation | None:
        return await session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == self._tenant_id,
            )
        )

    async def _load_lead(self, session: AsyncSession, conversation: Conversation) -> Lead | None:
        if conversation.lead_id is None:
            return None
        return await session.scalar(
            select(Lead).where(Lead.id == conversation.lead_id, Lead.tenant_id == self._tenant_id)
        )

    async def _latest_inbound(
        self, session: AsyncSession, conversation: Conversation
    ) -> tuple[str | None, datetime | None]:
        """The lead's most recent words, truncated for the queue preview."""
        row = (
            await session.execute(
                select(Message.text, Message.sent_at)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.tenant_id == self._tenant_id,
                    Message.direction == MessageDirection.INBOUND,
                    Message.text.is_not(None),
                )
                .order_by(Message.sent_at.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None, None
        text, sent_at = row
        limit = self._settings.handoff_preview_chars
        truncated = text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
        return truncated, sent_at

    async def _mark_lead_handed_off(self, session: AsyncSession, lead: Lead | None) -> None:
        if lead is None:
            # Someone messaging a page without submitting an ad form. The handoff still
            # exists and is still real; there is simply no lead row to move.
            return
        if lead.status in _TERMINAL_LEAD_STATUSES:
            return
        lead.status = LeadStatus.HUMAN_HANDOFF
        await session.flush()

    def _notification_payload(
        self,
        handoff: HumanHandoff,
        conversation: Conversation,
        lead: Lead | None,
        preview: str | None,
    ) -> dict:
        """Exactly what an agent needs to triage, and nothing that would be a liability.

        No access token, no encrypted credential, no provider key, no embedding, no message
        history. The one piece of customer text here is the truncated preview, because an
        agent cannot decide whether to take a lead they cannot see.
        """
        return {
            "lead_id": str(handoff.lead_id) if handoff.lead_id else None,
            "lead_name": handoff.lead_name,
            "lead_identifier": lead.meta_leadgen_id if lead is not None else None,
            "conversation_id": str(conversation.id),
            "channel": conversation.channel.value,
            "reason": handoff.reason.value,
            "priority": handoff.priority,
            "qualification_total": handoff.qualification_total,
            "latest_inbound_text": preview,
            "created_at": handoff.created_at.isoformat() if handoff.created_at else None,
            "assigned_user_id": (
                str(handoff.assigned_user_id) if handoff.assigned_user_id else None
            ),
        }


def _notification_title(reason: HandoffReason, lead: Lead | None) -> str:
    who = lead.name if lead is not None and lead.name else "A new lead"
    if reason is HandoffReason.EXPLICIT_HUMAN_REQUEST:
        return f"{who} asked to speak to someone"
    if reason is HandoffReason.HIGH_INTENT:
        return f"{who} looks like a high-intent lead"
    if reason is HandoffReason.AI_FAILURE:
        return f"AI could not continue with {who}"
    return f"{who} was escalated"


def _notification_message(
    reason: HandoffReason,
    lead: Lead | None,
    handoff: HumanHandoff,
    preview: str | None,
) -> str:
    headline = {
        HandoffReason.EXPLICIT_HUMAN_REQUEST: "asked to talk to a person",
        HandoffReason.HIGH_INTENT: "scored high intent and needs a human",
        HandoffReason.AI_FAILURE: "needs a human because the AI could not continue",
        HandoffReason.OTHER: "was escalated",
    }[reason]
    if handoff.qualification_total is not None:
        headline += f" (score {handoff.qualification_total})"
    if preview:
        headline += f': "{preview}"'
    return headline[:500]


__all__ = [
    "ACK_SOURCE_AI_REPLY",
    "ACK_SOURCE_TEMPLATE",
    "HandoffAlreadyAssigned",
    "HandoffAssignmentNotAllowed",
    "HandoffCreation",
    "HandoffInvalidState",
    "HandoffNotFound",
    "HandoffResolutionNotAllowed",
    "HumanHandoffService",
]
