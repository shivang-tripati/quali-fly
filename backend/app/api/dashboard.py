"""The dashboard endpoint.

One read-only GET. It creates nothing, mutates nothing, and takes no tenant parameter - the
tenant comes from the authenticated `TenantContext` and the database enforces the rest.

Read-only is a deliberate constraint rather than a limitation. A dashboard that could write
would be a second, unaudited path into four different state machines (leads, handoffs,
tasks, knowledge), and the first thing to go wrong would be a dashboard button that silently
means something different from the one in the queue.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUserSession, require_minimum_role
from app.core.auth import TenantContext
from app.core.errors import error_responses
from app.models.membership import Role
from app.schemas.dashboard import DashboardResponse
from app.services.dashboard import DashboardService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

ReadCtx = Annotated[TenantContext, Depends(require_minimum_role(Role.MEMBER))]


async def dashboard_service(session: CurrentUserSession, context: ReadCtx) -> DashboardService:
    """The seam tests override to pin the clock.

    A dependency rather than a constructor argument at the call site, so a test can inject a
    `FixedClock` without the router knowing the clock exists. "Is this follow-up overdue?"
    is a function of time, and a test that has to wait four real hours to observe it is a
    test nobody runs.
    """
    return DashboardService(session, context.tenant_id, role=context.role, user_id=context.user_id)


ServiceDep = Annotated[DashboardService, Depends(dashboard_service)]


@router.get(
    "",
    response_model=DashboardResponse,
    summary="What needs attention, in one tenant-scoped read.",
    responses=error_responses(401, 403),
)
async def get_dashboard(service: ServiceDep) -> DashboardResponse:
    """The first screen after sign-in.

    `integration` comes back only for ADMIN and OWNER. A member does not own the workspace's
    Meta connection, so showing them its state is noise - and a member who *can* fix a
    disconnected page is a role this product does not have.
    """
    return await service.build()


__all__ = ["router"]
