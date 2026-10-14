"""Handoff response shapes.

Assembled field by field rather than with `from_attributes`, the same reasoning
`api/follow_ups.py` gives: a new column added to the table must never start being
published by accident. That matters more here than for a task row, because this data is
what a human reads to decide whether to contact a real person.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.human_handoff import HandoffReason, HandoffStatus
from app.models.internal_notification import NotificationStatus, NotificationType


class HandoffResponse(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    lead_id: uuid.UUID | None
    status: HandoffStatus
    reason: HandoffReason
    detail: str | None
    priority: int
    assigned_user_id: uuid.UUID | None
    assigned_at: datetime | None
    acknowledged_at: datetime | None
    acknowledgement_failure: str | None
    acknowledgement_source: str | None
    resolved_at: datetime | None
    resolution_note: str | None
    lead_name: str | None
    latest_inbound_text: str | None
    latest_inbound_at: datetime | None
    qualification_total: int | None
    created_at: datetime
    updated_at: datetime


class HandoffSummary(BaseModel):
    """Queue header counts. Built with `extra="allow"` for the same reason the follow-up
    summary is: the status vocabulary and the counts are both derived from the enum, and
    hard-coding the keys would mean editing this file every time a status is added."""

    model_config = {"extra": "allow"}


class HandoffListResponse(BaseModel):
    items: list[HandoffResponse]
    count: int
    summary: HandoffSummary | None = None


class HandoffAssignRequest(BaseModel):
    """Assign to a specific user.

    No `tenant_id` field, and no way to add one by accident: the tenant comes from the
    authenticated context, and the service verifies the target user is a member of *that*
    tenant before the id is written.
    """

    user_id: uuid.UUID


class HandoffResolveRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class NotificationResponse(BaseModel):
    id: uuid.UUID
    handoff_id: uuid.UUID
    conversation_id: uuid.UUID
    lead_id: uuid.UUID | None
    type: NotificationType
    title: str
    message: str
    payload: dict[str, Any]
    status: NotificationStatus
    read_at: datetime | None
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    count: int


def handoff_values(handoff) -> dict:
    """Explicit projection. See the module docstring."""
    return {
        "id": handoff.id,
        "conversation_id": handoff.conversation_id,
        "lead_id": handoff.lead_id,
        "status": handoff.status,
        "reason": handoff.reason,
        "detail": handoff.detail,
        "priority": handoff.priority,
        "assigned_user_id": handoff.assigned_user_id,
        "assigned_at": handoff.assigned_at,
        "acknowledged_at": handoff.acknowledged_at,
        "acknowledgement_failure": handoff.acknowledgement_failure,
        "acknowledgement_source": handoff.acknowledgement_source,
        "resolved_at": handoff.resolved_at,
        "resolution_note": handoff.resolution_note,
        "lead_name": handoff.lead_name,
        "latest_inbound_text": handoff.latest_inbound_text,
        "latest_inbound_at": handoff.latest_inbound_at,
        "qualification_total": handoff.qualification_total,
        "created_at": handoff.created_at,
        "updated_at": handoff.updated_at,
    }


def notification_values(notification) -> dict:
    return {
        "id": notification.id,
        "handoff_id": notification.handoff_id,
        "conversation_id": notification.conversation_id,
        "lead_id": notification.lead_id,
        "type": notification.type,
        "title": notification.title,
        "message": notification.message,
        "payload": notification.payload,
        "status": notification.status,
        "read_at": notification.read_at,
        "created_at": notification.created_at,
    }
