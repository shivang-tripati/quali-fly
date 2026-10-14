"""Tenant-scoped, read-only visibility into follow-up tasks.

Deliberately read-only, and deliberately small. Phase 7 does not need a management UI, and
a write endpoint here would create a second way to mutate task state outside the
scheduler - which is precisely the kind of second path that makes a state machine
ambiguous.

What exists is what an operator or a test needs to answer "what is this conversation's
follow-up state, and why did that one not send?". That is a genuine requirement: without
it, a follow-up that silently does not go out is indistinguishable from one that was never
scheduled.

No `tenant_id` parameter anywhere. The tenant comes from the authenticated
`TenantContext` and the database enforces the rest.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUserSession, require_minimum_role
from app.core.auth import TenantContext
from app.core.errors import ApiError, ErrorCode, error_responses
from app.models.membership import Role
from app.models.scheduled_task import TaskStatus
from app.repositories import scheduled_tasks as task_repo
from app.schemas.follow_up import (
    ScheduledTaskListResponse,
    ScheduledTaskResponse,
    ScheduledTaskSummary,
)

router = APIRouter(prefix="/scheduled-tasks", tags=["follow-ups"])

ReadCtx = Annotated[TenantContext, Depends(require_minimum_role(Role.MEMBER))]


@router.get(
    "",
    response_model=ScheduledTaskListResponse,
    summary="List the tenant's scheduled tasks, newest first.",
    responses=error_responses(401, 403),
)
async def list_scheduled_tasks(
    session: CurrentUserSession,
    context: ReadCtx,
    status: TaskStatus | None = None,
    limit: int = 50,
) -> ScheduledTaskListResponse:
    bounded = max(1, min(limit, 200))
    rows = await task_repo.list_tasks(
        session, tenant_id=context.tenant_id, status=status, limit=bounded
    )
    items = [ScheduledTaskResponse(**_values(task)) for task in rows]
    return ScheduledTaskListResponse(
        items=items,
        count=len(items),
        summary=ScheduledTaskSummary(
            **await task_repo.count_by_status(session, tenant_id=context.tenant_id)
        ),
    )


@router.get(
    "/{task_id}",
    response_model=ScheduledTaskResponse,
    summary="Fetch one task. Scoped by RLS, so another tenant's is simply absent.",
    responses=error_responses(401, 403, 404),
)
async def get_scheduled_task(
    task_id: uuid.UUID,
    session: CurrentUserSession,
    context: ReadCtx,
) -> ScheduledTaskResponse:
    task = await task_repo.get(session, tenant_id=context.tenant_id, task_id=task_id)
    if task is None:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Scheduled task not found.")
    return ScheduledTaskResponse(**_values(task))


@router.get(
    "/conversation/{conversation_id}",
    response_model=ScheduledTaskListResponse,
    summary="A conversation's follow-up sequence, in step order.",
    responses=error_responses(401, 403, 404),
)
async def list_conversation_tasks(
    conversation_id: uuid.UUID,
    session: CurrentUserSession,
    context: ReadCtx,
) -> ScheduledTaskListResponse:
    rows = await task_repo.get_by_conversation(
        session, tenant_id=context.tenant_id, conversation_id=conversation_id
    )
    if not rows:
        # 404 rather than an empty list, so "no follow-ups" and "not your conversation" are
        # not reported identically by a caller that already knows the conversation exists.
        raise ApiError(404, ErrorCode.NOT_FOUND, "No scheduled tasks for that conversation.")
    items = [ScheduledTaskResponse(**_values(task)) for task in rows]
    return ScheduledTaskListResponse(items=items, count=len(items))


def _values(task) -> dict:
    """Plain values for the response schema.

    The vector and the tenant id are not fields on this model and never will be, but the
    response is assembled explicitly rather than with `from_attributes` so that adding a
    column to the table can never accidentally start publishing it.
    """
    return {
        "id": task.id,
        "conversation_id": task.conversation_id,
        "lead_id": task.lead_id,
        "task_type": task.task_type,
        "sequence_step": task.sequence_step,
        "status": task.status,
        "scheduled_at": task.scheduled_at,
        "attempt_count": task.attempt_count,
        "max_attempts": task.max_attempts,
        "last_error": task.last_error,
        "ai_enhanced": task.ai_enhanced,
        "sent_text": task.sent_text,
        "completed_at": task.completed_at,
        "cancelled_at": task.cancelled_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


__all__ = ["router"]
