"""The human handoff inbox: list, view, claim, assign, start, resolve.

Read and write the queue; nothing here decides *whether* a handoff exists. That belongs to
`HumanHandoffService`, which only the AI pipeline and the follow-up poller call. Keeping
creation out of this router is deliberate: an endpoint that can manufacture an escalation
would be a second, unaudited way for work to enter the queue.

Role split
----------
`MEMBER` may list, view, claim, start and resolve. `ADMIN` and above may additionally
assign a handoff to a specific teammate.

`claim` exists separately from `assign` because they are different permissions with
different bodies: claiming takes *you* as the argument, so it needs no request body and
cannot be used to dump work onto someone else. Assignment puts a name in the request, which
is why it is the one that is admin-only. Both refuse to move a resolved handoff, and
`claim` refuses to take one that is already owned - the second agent gets a 409 naming the
current owner rather than a silent success.

Every mutation is safe to retry. A double-clicked claim is idempotent, because the second
one loses the race in the database and is told it lost.

Reads use the request's own session; the write endpoints build a `HumanHandoffService` on
the application's session factory instead. That is deliberate and is the same shape the AI
pipeline and the follow-up poller use - a service method opens its own short transaction,
so it works identically from a request and from a worker with no request at all. Routing
writes through the request's long-lived transaction would make the two callers differ in
transaction boundaries, which is exactly the kind of difference that makes a race
reproducible in one caller and impossible in the other.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.deps import CurrentUserSession, require_minimum_role
from app.core.auth import TenantContext
from app.core.clock import SystemClock
from app.core.errors import ApiError, ErrorCode, error_responses
from app.db.session import get_session_factory
from app.models.human_handoff import HandoffReason, HandoffStatus
from app.models.internal_notification import NotificationStatus
from app.models.membership import Role
from app.repositories import handoffs as handoff_repo
from app.repositories import notifications as notification_repo
from app.schemas.handoff import (
    HandoffAssignRequest,
    HandoffListResponse,
    HandoffResolveRequest,
    HandoffResponse,
    HandoffSummary,
    NotificationListResponse,
    NotificationResponse,
    handoff_values,
    notification_values,
)
from app.services import handoffs as handoff_service

router = APIRouter(prefix="/handoffs", tags=["handoffs"])

MemberCtx = Annotated[TenantContext, Depends(require_minimum_role(Role.MEMBER))]
AdminCtx = Annotated[TenantContext, Depends(require_minimum_role(Role.ADMIN))]


# -- reads -------------------------------------------------------------------


@router.get(
    "",
    response_model=HandoffListResponse,
    summary="The tenant's handoff queue, highest priority first.",
    responses=error_responses(401, 403),
)
async def list_handoffs(
    session: CurrentUserSession,
    context: MemberCtx,
    status_filter: HandoffStatus | None = None,
    assigned_user_id: uuid.UUID | None = None,
    reason: HandoffReason | None = None,
    conversation_id: uuid.UUID | None = None,
    lead_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = 50,
) -> HandoffListResponse:
    """The queue an agent works from.

    `status` is spelled `status_filter` in the handler and exposed as `status` in the
    query string, because `status` shadows the module this file imports for HTTP codes.
    Same pattern as `api/follow_ups.py`.
    """
    rows = await handoff_repo.list_handoffs(
        session,
        tenant_id=context.tenant_id,
        status=status_filter,
        assigned_user_id=assigned_user_id,
        reason=reason,
        conversation_id=conversation_id,
        lead_id=lead_id,
        search=search,
        limit=max(1, min(limit, 200)),
    )
    items = [HandoffResponse(**handoff_values(row)) for row in rows]
    return HandoffListResponse(
        items=items,
        count=len(items),
        summary=HandoffSummary(
            **await handoff_repo.count_by_status(session, tenant_id=context.tenant_id)
        ),
    )


@router.get(
    "/{handoff_id}",
    response_model=HandoffResponse,
    summary="Fetch one handoff. Scoped by RLS, so another tenant's is simply absent.",
    responses=error_responses(401, 403, 404),
)
async def get_handoff(
    handoff_id: uuid.UUID,
    session: CurrentUserSession,
    context: MemberCtx,
) -> HandoffResponse:
    handoff = await handoff_repo.get(session, tenant_id=context.tenant_id, handoff_id=handoff_id)
    if handoff is None:
        raise ApiError(404, ErrorCode.HANDOFF_NOT_FOUND, "Handoff not found.")
    return HandoffResponse(**handoff_values(handoff))


# -- human workflow ----------------------------------------------------------


@router.post(
    "/{handoff_id}/claim",
    response_model=HandoffResponse,
    summary="Take an unassigned handoff. First claim wins.",
    responses=error_responses(401, 403, 404, 409),
)
async def claim_handoff(
    handoff_id: uuid.UUID,
    context: MemberCtx,
) -> HandoffResponse:
    """Claim for yourself. Takes no body, so it cannot be used to claim for someone else.

    Concurrent: the winner is decided by a conditional `UPDATE` in the database, not by a
    check. Two agents clicking at the same moment produce one assignee and one 409 - never
    two owners.
    """
    service = handoff_service.HumanHandoffService(get_session_factory(), context.tenant_id)
    try:
        claimed = await service.claim(handoff_id, context.user_id)
    except handoff_service.HandoffNotFound:
        raise ApiError(404, ErrorCode.HANDOFF_NOT_FOUND, "Handoff not found.") from None
    except handoff_service.HandoffAlreadyAssigned:
        raise ApiError(
            409,
            ErrorCode.HANDOFF_ALREADY_ASSIGNED,
            "This handoff was claimed by another user a moment ago.",
        ) from None
    except handoff_service.HandoffInvalidState as invalid:
        raise ApiError(
            409,
            ErrorCode.HANDOFF_INVALID_STATE,
            f"Cannot claim a handoff that is {invalid.status.value}.",
        ) from None
    return HandoffResponse(**handoff_values(claimed))


@router.post(
    "/{handoff_id}/assign",
    response_model=HandoffResponse,
    summary="Put a handoff on a specific teammate. Admin only.",
    responses=error_responses(401, 403, 404, 409),
)
async def assign_handoff(
    handoff_id: uuid.UUID,
    payload: HandoffAssignRequest,
    context: AdminCtx,
) -> HandoffResponse:
    """Assign or reassign.

    Reassignment is allowed while the handoff is active - an agent going off shift is a
    normal reason - and refused once it is resolved, because a resolved escalation is
    history and rewriting its owner would falsify it.

    The target must be a member of *this* tenant. `users` is global, so a valid user id
    from another tenant exists, and neither RLS nor the request can catch it; the service
    checks membership explicitly.
    """
    service = handoff_service.HumanHandoffService(get_session_factory(), context.tenant_id)
    try:
        assigned = await service.assign(handoff_id, payload.user_id)
    except handoff_service.HandoffNotFound:
        raise ApiError(404, ErrorCode.HANDOFF_NOT_FOUND, "Handoff not found.") from None
    except handoff_service.HandoffAssignmentNotAllowed:
        raise ApiError(
            403,
            ErrorCode.HANDOFF_ASSIGNMENT_NOT_ALLOWED,
            "That user is not a member of this tenant.",
        ) from None
    except handoff_service.HandoffInvalidState as invalid:
        raise ApiError(
            409,
            ErrorCode.HANDOFF_INVALID_STATE,
            f"Cannot assign a handoff that is {invalid.status.value}.",
        ) from None
    return HandoffResponse(**handoff_values(assigned))


@router.post(
    "/{handoff_id}/start",
    response_model=HandoffResponse,
    summary="Mark the handoff as in progress.",
    responses=error_responses(401, 403, 404, 409),
)
async def start_handoff(
    handoff_id: uuid.UUID,
    context: MemberCtx,
) -> HandoffResponse:
    """OPEN|ASSIGNED -> IN_PROGRESS. The agent has picked the work up."""
    service = handoff_service.HumanHandoffService(get_session_factory(), context.tenant_id)
    try:
        started = await service.start(handoff_id)
    except handoff_service.HandoffNotFound:
        raise ApiError(404, ErrorCode.HANDOFF_NOT_FOUND, "Handoff not found.") from None
    except handoff_service.HandoffInvalidState as invalid:
        raise ApiError(
            409,
            ErrorCode.HANDOFF_INVALID_STATE,
            f"Cannot start a handoff that is {invalid.status.value}.",
        ) from None
    return HandoffResponse(**handoff_values(started))


@router.post(
    "/{handoff_id}/resolve",
    response_model=HandoffResponse,
    summary="Close the handoff out.",
    responses=error_responses(401, 403, 404, 409),
)
async def resolve_handoff(
    handoff_id: uuid.UUID,
    payload: HandoffResolveRequest,
    context: MemberCtx,
) -> HandoffResponse:
    """Any non-terminal state -> RESOLVED.

    Does not restart follow-ups, re-enable the AI, or move the lead. The conversation
    belonged to a person and the escalation is now history; who owns the relationship
    afterwards is a product decision this phase has no basis to make.

    Idempotent in the useful sense: a resolved handoff is not *re*-resolved, it 409s, so a
    retried request cannot silently rewrite the original resolution time.
    """
    service = handoff_service.HumanHandoffService(get_session_factory(), context.tenant_id)
    try:
        resolved = await service.resolve(handoff_id, note=payload.note)
    except handoff_service.HandoffNotFound:
        raise ApiError(404, ErrorCode.HANDOFF_NOT_FOUND, "Handoff not found.") from None
    except handoff_service.HandoffResolutionNotAllowed:
        raise ApiError(
            409,
            ErrorCode.HANDOFF_RESOLUTION_NOT_ALLOWED,
            "This handoff is already closed.",
        ) from None
    return HandoffResponse(**handoff_values(resolved))


# -- notifications -----------------------------------------------------------


@router.get(
    "/notifications/inbox",
    response_model=NotificationListResponse,
    summary="The internal notification inbox, newest first.",
    responses=error_responses(401, 403),
)
async def list_notifications(
    session: CurrentUserSession,
    context: MemberCtx,
    status_filter: NotificationStatus | None = None,
    handoff_id: uuid.UUID | None = None,
    limit: int = 50,
) -> NotificationListResponse:
    rows = await notification_repo.list_notifications(
        session,
        tenant_id=context.tenant_id,
        status=status_filter,
        handoff_id=handoff_id,
        limit=max(1, min(limit, 200)),
    )
    items = [NotificationResponse(**notification_values(row)) for row in rows]
    return NotificationListResponse(items=items, count=len(items))


@router.post(
    "/notifications/{notification_id}/read",
    status_code=204,
    summary="Mark one notification read.",
    responses=error_responses(401, 403),
)
async def mark_notification_read(
    notification_id: uuid.UUID,
    session: CurrentUserSession,
    context: MemberCtx,
) -> Response:
    """204 once the notification is read, whether this call or a previous one did it.

    No 404 and no 409 on purpose. A notification that is already read, or one belonging to
    another tenant, is not an error the person clicking a bell can act on - the outcome they
    asked for is true either way, and a 409 would make an ordinary double-click look like a
    failure. Returning 204 unconditionally is what lets the UI not distinguish "I just read
    it" from "someone else already had".

    This is the only notification *write* in the product and it exists because the bell has
    an unread badge: a badge whose count can never go down is a badge people learn to ignore.
    It is a single-column update on an internal record, so it needs no service and no new
    state machine - the request session already carries the tenant context and commits.
    """
    await notification_repo.mark_read(
        session,
        tenant_id=context.tenant_id,
        notification_id=notification_id,
        now=SystemClock().now(),
    )
    return Response(status_code=204)


__all__ = ["router"]
