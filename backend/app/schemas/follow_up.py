"""Scheduled task response schemas.

`sent_text` is included and `locked_by` is not. The first answers "what did this lead
actually receive?", which is the question an operator has when a follow-up looks wrong. The
second is an internal worker identity and has no business being published.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.scheduled_task import TaskStatus, TaskType


class ScheduledTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    conversation_id: uuid.UUID
    lead_id: uuid.UUID | None
    task_type: TaskType
    sequence_step: int
    status: TaskStatus
    scheduled_at: datetime
    attempt_count: int
    max_attempts: int
    # A short category, never a provider or Meta payload.
    last_error: str | None = None
    #: Whether the body came from the AI rather than the tenant's template verbatim.
    ai_enhanced: bool = False
    sent_text: str | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ScheduledTaskSummary(BaseModel):
    """Counts by status, so a tenant can see at a glance what is outstanding."""

    model_config = ConfigDict(extra="allow")

    PENDING: int = 0
    PROCESSING: int = 0
    COMPLETED: int = 0
    FAILED: int = 0
    CANCELLED: int = 0


class ScheduledTaskListResponse(BaseModel):
    items: list[ScheduledTaskResponse] = Field(default_factory=list)
    count: int
    #: Absent on the per-conversation route, which is already scoped to one thread.
    summary: ScheduledTaskSummary | None = None


__all__ = [
    "ScheduledTaskListResponse",
    "ScheduledTaskResponse",
    "ScheduledTaskSummary",
]
