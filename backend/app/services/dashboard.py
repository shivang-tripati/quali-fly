"""The dashboard read model.

One place decides what "needs attention" means. Not the router, not the repository, and
certainly not the frontend - if the same rule were written in two of those, they would
eventually disagree and the dashboard would either cry wolf or miss something.

The rule, in one sentence: **an attention item is work a human could do right now that
something else is not doing.** Everything else is a statistic.

That definition is what keeps the list short. A lead that is simply `NEW` is not on it -
nobody is waiting on a specific person to do something about one unworked lead today. A
handoff with a person assigned to nobody *is*, because the escalation explicitly asked for a
human and there is not one. A knowledge base with zero documents is on it, because the AI is
quietly answering from nothing and the fix is two minutes of work.

Ordering is severity first, then age. Severity answers "how bad", age answers "how long",
and a queue that sorts only by one of them buries the item that is both.
"""

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, SystemClock
from app.models.human_handoff import HandoffReason, HandoffStatus
from app.models.lead import LeadStatus
from app.models.membership import Role
from app.repositories import dashboard as dash_repo
from app.schemas.dashboard import (
    AttentionItem,
    DashboardResponse,
    IntegrationHealth,
    LeadActivity,
    RecentConversationItem,
    SummaryCounts,
)

log = logging.getLogger("app.dashboard")

#: A failure older than this has been seen, ignored, or is not worth interrupting for. Two
#: days is long enough that a transient provider blip clears itself, and short enough that a
#: genuine integration break does not disappear.
FAILURE_WINDOW = timedelta(days=2)

#: Bounds on the payload. The dashboard is one screen; a person cannot read 200 attention
#: items, and shipping them would cost more than it is worth on every single load.
MAX_ATTENTION = 25
MAX_RECENT_CONVERSATIONS = 8

#: Human words for a handoff reason. The enum is a database value; this is what a person
#: reading the queue should see.
_REASON_PHRASES: dict[HandoffReason, str] = {
    HandoffReason.EXPLICIT_HUMAN_REQUEST: "asked to speak to someone",
    HandoffReason.HIGH_INTENT: "looks ready to buy",
    HandoffReason.AI_FAILURE: "could not be answered automatically",
    HandoffReason.OTHER: "was escalated",
}

#: Human words for a processing failure. Categories only, never provider text - a provider
#: error can echo the lead's own message and this renders in a list.
_FAILURE_PHRASES = {
    "ai_processing": "AI could not process a reply",
    "message_not_delivered": "A message was not delivered",
}


def _seconds_since(moment: datetime | None, *, now: datetime) -> int:
    if moment is None:
        return 0
    delta = now - moment
    return max(0, int(delta.total_seconds()))


def _lead_title(name: str | None) -> str:
    return name.strip() if name and name.strip() else "A new lead"


class DashboardService:
    """Builds one tenant's dashboard. Read-only; owns no state."""

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        *,
        role: Role,
        user_id: uuid.UUID,
        clock: Clock | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._role = role
        self._user_id = user_id
        # Injected for the same reason Phase 7 injects one: "is this follow-up overdue?" is a
        # function of the clock, and a test that has to wait four real hours to observe it is
        # a test nobody runs. The real clock in production.
        self._clock = clock or SystemClock()

    async def build(self) -> DashboardResponse:
        now = self._clock.now()

        # Independent reads, issued together. Sequential awaits here would be a waterfall on
        # the screen the user opens most, and there is no dependency between them.
        leads = await dash_repo.lead_counts(self._session, tenant_id=self._tenant_id, now=now)
        handoffs = await dash_repo.handoff_counts(
            self._session, tenant_id=self._tenant_id, user_id=self._user_id
        )
        follow_ups = await dash_repo.follow_up_counts(
            self._session, tenant_id=self._tenant_id, now=now
        )
        knowledge = await dash_repo.knowledge_counts(self._session, tenant_id=self._tenant_id)
        connections = await dash_repo.meta_connection_health(
            self._session, tenant_id=self._tenant_id
        )

        open_handoffs = await dash_repo.active_handoffs(
            self._session, tenant_id=self._tenant_id, limit=MAX_ATTENTION
        )
        due_follow_ups = await dash_repo.pending_follow_ups(
            self._session, tenant_id=self._tenant_id, now=now, limit=MAX_ATTENTION
        )
        failures = await dash_repo.recent_failures(
            self._session,
            tenant_id=self._tenant_id,
            since=now - FAILURE_WINDOW,
            limit=MAX_ATTENTION,
        )
        conversations = await dash_repo.recent_conversations(
            self._session, tenant_id=self._tenant_id, limit=MAX_RECENT_CONVERSATIONS
        )

        attention = self._attention(
            open_handoffs, due_follow_ups, failures, knowledge, connections, now=now
        )

        response = DashboardResponse(
            summary=SummaryCounts(
                new_leads=leads.by_status.get(LeadStatus.NEW, 0),
                new_leads_today=leads.new_today,
                qualified_leads=leads.by_status.get(LeadStatus.QUALIFIED, 0),
                handoffs_open=handoffs.open_count,
                handoffs_unassigned=handoffs.unassigned_count,
                handoffs_mine=handoffs.mine_count,
                follow_ups_due=follow_ups.overdue + follow_ups.due_today,
                follow_ups_overdue=follow_ups.overdue,
                follow_ups_upcoming=follow_ups.upcoming,
                knowledge_documents=knowledge["documents"],
            ),
            attention=attention[:MAX_ATTENTION],
            lead_activity=LeadActivity(
                new=leads.by_status.get(LeadStatus.NEW, 0),
                qualified=leads.by_status.get(LeadStatus.QUALIFIED, 0),
                replying=leads.by_status.get(LeadStatus.REPLIED, 0),
                handoff=leads.by_status.get(LeadStatus.HUMAN_HANDOFF, 0),
                closed=leads.by_status.get(LeadStatus.CLOSED, 0),
            ),
            recent_conversations=[self._conversation(item, now=now) for item in conversations],
            integration=(self._integration(connections, knowledge) if self._is_admin() else None),
            generated_at=now,
        )
        log.info(
            "dashboard.built",
            extra={
                "tenant_id": str(self._tenant_id),
                "attention_items": len(response.attention),
                "role": self._role.value,
            },
        )
        return response

    # -- attention --------------------------------------------------------

    def _attention(
        self,
        handoffs: list,
        follow_ups: list,
        failures: list,
        knowledge: dict[str, int],
        connections: dict,
        *,
        now: datetime,
    ) -> list[AttentionItem]:
        items: list[AttentionItem] = []

        for entry in handoffs:
            handoff = entry.handoff
            phrase = _REASON_PHRASES[handoff.reason]
            unclaimed = handoff.assigned_user_id is None
            items.append(
                AttentionItem(
                    kind="handoff",
                    # Unclaimed is critical: the escalation explicitly asked for a person and
                    # no person has taken it. Claimed and in progress is informational - the
                    # work is already owned, so interrupting about it is noise.
                    severity="critical" if unclaimed else "info",
                    title=_lead_title(entry.lead_name),
                    detail=f"{_lead_title(entry.lead_name)} {phrase}."
                    if entry.lead_name
                    else f"A conversation {phrase}.",
                    waiting_seconds=_seconds_since(handoff.created_at, now=now),
                    conversation_id=handoff.conversation_id,
                    handoff_id=handoff.id,
                    lead_id=handoff.lead_id,
                    # The status is a fact about the escalation, not a guess: a handoff nobody
                    # has taken is unclaimed, and saying "in progress" for one that is merely
                    # assigned to someone tells the operator work has started when it has not.
                    label=(
                        "UNCLAIMED"
                        if unclaimed
                        else (
                            "IN PROGRESS"
                            if handoff.status == HandoffStatus.IN_PROGRESS
                            else "CLAIMED"
                        )
                    ),
                )
            )

        for entry in follow_ups:
            task = entry.task
            overdue = task.scheduled_at < now
            title = _lead_title(entry.lead_name)
            items.append(
                AttentionItem(
                    kind="follow_up_overdue" if overdue else "follow_up_due",
                    severity="warning" if overdue else "info",
                    title=title,
                    detail=(
                        f"Follow-up for {title} was not sent."
                        if overdue
                        else f"Follow-up for {title} is scheduled."
                    ),
                    waiting_seconds=_seconds_since(task.scheduled_at, now=now),
                    conversation_id=task.conversation_id,
                    handoff_id=None,
                    lead_id=task.lead_id,
                    label="OVERDUE" if overdue else "DUE SOON",
                )
            )

        for failure in failures:
            phrase = _FAILURE_PHRASES.get(failure.kind, "Something needs attention")
            subject = _lead_title(failure.subject) if failure.subject else None
            items.append(
                AttentionItem(
                    kind=("ai_failure" if failure.kind == "ai_processing" else "message_failed"),
                    severity="warning",
                    title=subject or "A conversation",
                    detail=(f"{phrase} for {subject}." if subject else f"{phrase}."),
                    waiting_seconds=_seconds_since(failure.occurred_at, now=now),
                    conversation_id=failure.conversation_id,
                    handoff_id=None,
                    lead_id=None,
                    label=(failure.detail or "FAILED").upper(),
                )
            )

        # A Meta connection that is not ACTIVE means nothing is being ingested at all, which
        # silently starves every other number on this page. It outranks everything except
        # an unclaimed handoff.
        for connection in connections["connections"]:
            if connection["status"] != "ACTIVE" or not connection["configured"]:
                items.append(
                    AttentionItem(
                        kind="connection",
                        severity="critical",
                        title="Meta connection needs attention",
                        detail=(
                            f"{connection['name']} needs to be reconnected. No new leads or "
                            "messages are being received."
                        ),
                        waiting_seconds=0,
                        label="RECONNECT",
                    )
                )
                break

        # Zero documents means the AI is answering from nothing. Worth surfacing exactly
        # once, and only because it is a two-minute fix with a visible payoff.
        if self._is_admin() and knowledge["documents"] == 0:
            items.append(
                AttentionItem(
                    kind="knowledge",
                    severity="info",
                    title="Your AI has no knowledge yet",
                    detail=(
                        "Add your pricing, policies or FAQs so replies answer from your "
                        "business instead of guessing."
                    ),
                    waiting_seconds=0,
                    label="ADD KNOWLEDGE",
                )
            )

        order = {"critical": 0, "warning": 1, "info": 2}
        items.sort(key=lambda i: (order[i.severity], -i.waiting_seconds))
        return items

    # -- projections ------------------------------------------------------

    def _conversation(self, item, *, now: datetime) -> RecentConversationItem:
        conversation = item.conversation
        return RecentConversationItem(
            id=conversation.id,
            lead_id=conversation.lead_id,
            lead_name=item.lead_name,
            lead_status=item.lead_status.value if item.lead_status else None,
            channel=conversation.channel.value,
            last_message_text=item.last_message_text,
            last_message_direction=item.last_message_direction,
            last_message_delivery=item.last_message_delivery,
            last_message_at=item.last_message_sent_at or conversation.last_message_at,
            has_active_handoff=item.has_active_handoff,
            within_messaging_window=self._within_window(conversation, now=now),
        )

    @staticmethod
    def _within_window(conversation, *, now: datetime) -> bool:
        """Meta accepts a `RESPONSE` send for 24 hours after the last inbound message.

        Mirrors `ConversationEngine.is_within_messaging_window`, and deliberately
        informational: the send is still attempted and Meta is still the authority. The
        inbox uses this to disable its composer honestly rather than to refuse.
        """
        from app.services.conversations import MESSAGING_WINDOW

        if conversation.last_inbound_at is None:
            return False
        return now < conversation.last_inbound_at + MESSAGING_WINDOW

    def _integration(self, connections: dict, knowledge: dict[str, int]) -> IntegrationHealth:
        last_webhook = max(
            (c["last_webhook_at"] for c in connections["connections"] if c["last_webhook_at"]),
            default=None,
        )
        return IntegrationHealth(
            meta_connected=connections["active"] > 0,
            meta_needs_attention=connections["needs_attention"] > 0,
            meta_connection_count=connections["total"],
            last_webhook_at=last_webhook,
            knowledge_documents=knowledge["documents"],
            knowledge_chunks=knowledge["chunks"],
        )

    def _is_admin(self) -> bool:
        return self._role in (Role.ADMIN, Role.OWNER)


__all__ = ["DashboardService"]
