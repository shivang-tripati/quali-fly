"""Tenant-scoped Lead reads.

Exists so the vertical slice can be verified end to end - that a Meta leadgen webhook
actually produced a tenant-scoped Lead - and so the frontend can show the result of
connecting. It is a list and a count, not the Phase 9 dashboard.

Every read is scoped by RLS. There is no `tenant_id` parameter anywhere in this module:
the tenant comes from the `TenantContext`, and the database enforces the rest.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUserSession, require_minimum_role
from app.core.auth import TenantContext
from app.core.errors import ApiError, ErrorCode, error_responses
from app.models.lead import LeadStatus
from app.models.membership import Role
from app.repositories import leads as lead_repo
from app.schemas.lead import LeadListResponse, LeadResponse

router = APIRouter(prefix="/leads", tags=["leads"])

ReadCtx = Annotated[TenantContext, Depends(require_minimum_role(Role.MEMBER))]


@router.get(
    "",
    response_model=LeadListResponse,
    summary="List the current tenant's leads, newest first.",
    responses=error_responses(401, 403),
)
async def list_leads(
    session: CurrentUserSession,
    _context: ReadCtx,
    limit: int = 50,
    offset: int = 0,
    status: LeadStatus | None = None,
    q: str | None = None,
) -> LeadListResponse:
    """A working list, not a dump.

    `status` and `q` exist so the leads screen can answer "the hot ones" and "Priya"
    without scrolling. `q` is bounded so a client cannot ask the database to match an
    unbounded pattern, and both filters are reflected in `total` so pagination is honest.
    """
    # Bounded so a tenant cannot ask for the whole table in one request.
    bounded = max(1, min(limit, 200))
    skip = max(0, offset)
    term = (q or "").strip()[:120] or None
    rows = await lead_repo.list_leads(
        session, limit=bounded, status=status, search=term, offset=skip
    )
    return LeadListResponse(
        items=[LeadResponse(**lead_repo.lead_values(lead)) for lead in rows],
        count=len(rows),
        total=await lead_repo.count_leads_filtered(session, status=status, search=term),
    )


@router.get(
    "/{lead_id}",
    response_model=LeadResponse,
    summary="Fetch one lead. Scoped by RLS, so another tenant's lead is simply absent.",
    responses=error_responses(401, 403, 404),
)
async def get_lead(
    lead_id: uuid.UUID,
    session: CurrentUserSession,
    _context: ReadCtx,
) -> LeadResponse:
    lead = await lead_repo.get(session, lead_id)
    if lead is None:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Lead not found.")
    return LeadResponse(**lead_repo.lead_values(lead))
