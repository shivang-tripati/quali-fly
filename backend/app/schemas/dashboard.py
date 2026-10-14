"""Dashboard response shapes.

The vocabulary is the product's, not the database's. Nothing in these schemas names a
table, a task type, a lease, a run status or a provider - an operator reading this payload
should never be able to tell that `follow_ups_due` came from a `scheduled_tasks` row.

Each `AttentionItem` is designed to be rendered directly: severity, a title, a sentence of
context, how long it has been waiting, the entities involved, and where to go next. The UI
adds a label; it does not have to invent meaning from an enum value.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "warning", "info"]


class AttentionItem(BaseModel):
    """One piece of work waiting for a human.

    `kind` is a closed vocabulary the UI can switch on for an icon and a link. It is a
    product concept - "a person asked for a person" - not a table name.
    """

    kind: Literal[
        "handoff",
        "follow_up_overdue",
        "follow_up_due",
        "ai_failure",
        "message_failed",
        "connection",
        "knowledge",
    ]
    severity: Severity
    #: Who or what this is about. Never an internal id on its own - a lead with no name
    #: still needs something a human can recognise.
    title: str
    #: One sentence of context, already human-readable.
    detail: str
    #: Seconds since the item became actionable. A number, not a string: the client renders
    #: a relative time and a server-rendered string would be wrong for a client in another
    #: timezone and would go stale in a cached payload.
    waiting_seconds: int
    conversation_id: uuid.UUID | None = None
    handoff_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    #: `OVERDUE` / `DUE SOON` / `REQUESTED HUMAN` - short, uppercase, display-ready.
    label: str | None = None


class SummaryCounts(BaseModel):
    #: Leads sitting in the NEW state - work not yet done, not arrivals today. Paired with
    #: `new_leads_today` because "12 new leads" and "12 leads that arrived today" are
    #: different numbers, and showing one while describing it as the other is a lie.
    new_leads: int
    new_leads_today: int
    qualified_leads: int
    handoffs_open: int
    handoffs_unassigned: int
    handoffs_mine: int
    follow_ups_due: int
    follow_ups_overdue: int
    follow_ups_upcoming: int
    knowledge_documents: int


class LeadActivity(BaseModel):
    """The lifecycle bar.

    Only the statuses that mean something operationally. A GHOSTED count is in the summary
    vocabulary but not here, because "3 ghosted" is a number with no action attached and the
    dashboard is a list of things to do.
    """

    new: int
    qualified: int
    replying: int
    handoff: int
    closed: int


class RecentConversationItem(BaseModel):
    id: uuid.UUID
    lead_id: uuid.UUID | None
    lead_name: str | None
    lead_status: str | None
    channel: str
    last_message_text: str | None
    last_message_direction: Literal["INBOUND", "OUTBOUND"] | None
    last_message_delivery: str | None
    last_message_at: datetime | None
    has_active_handoff: bool
    #: Whether Meta will still accept a `RESPONSE` send. The inbox needs this to disable
    #: its composer honestly instead of letting someone click send and be refused.
    within_messaging_window: bool


class IntegrationHealth(BaseModel):
    """Workspace-level state, shown to admins.

    Never a token, a token hash, a scope list, or a page access token. Just whether each
    moving part is doing its job.
    """

    meta_connected: bool
    meta_needs_attention: bool
    meta_connection_count: int
    last_webhook_at: datetime | None
    knowledge_documents: int
    knowledge_chunks: int


class DashboardResponse(BaseModel):
    summary: SummaryCounts
    #: Ordered by severity, then by how long each item has waited. The UI renders this list
    #: as-is rather than re-sorting it.
    attention: list[AttentionItem] = Field(default_factory=list)
    lead_activity: LeadActivity
    recent_conversations: list[RecentConversationItem] = Field(default_factory=list)
    #: Omitted entirely for a MEMBER. A member does not own the workspace connection, so
    #: showing it to them is noise at best and a nudge to find someone who does at worst.
    integration: IntegrationHealth | None = None
    generated_at: datetime


__all__ = [
    "AttentionItem",
    "DashboardResponse",
    "IntegrationHealth",
    "LeadActivity",
    "RecentConversationItem",
    "Severity",
    "SummaryCounts",
]
