"""Reusable authentication, tenancy and authorization dependencies.

Resolution order for any tenant-scoped endpoint:

    get_db                      one session, one transaction
      -> get_current_principal    session cookie -> sessions/users lookup
      -> get_user_transaction     SET LOCAL app.current_user
      -> get_tenant_context       verify membership, SET LOCAL app.current_tenant
      -> require_role(...)        centralized role check

Nothing here trusts a tenant id, user id or role from the request body or query
string. `X-Tenant-ID` is only a *preference*: it is checked against a real
membership before a `TenantContext` can be created.
"""

import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, TenantContext
from app.core.config import Settings, get_settings
from app.core.errors import ApiError, ErrorCode
from app.core.security import hash_session_token
from app.db.session import get_db
from app.db.tenant_context import set_current_tenant, set_current_user
from app.models.membership import ROLE_RANK, Role
from app.models.session import Session
from app.models.tenant import TenantStatus
from app.models.user import User
from app.repositories import tenants as tenant_repo

SESSION_COOKIE_NAME = "qf_session"
TENANT_HEADER = "X-Tenant-ID"

DbSession = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _unauthenticated(message: str = "Authentication required.") -> ApiError:
    # No `WWW-Authenticate` challenge: this is a cookie, not a bearer token, so a
    # challenge would only mislead clients.
    return ApiError(401, ErrorCode.AUTH_REQUIRED, message)


async def get_current_principal(request: Request, session: DbSession) -> Principal:
    """Verify the session cookie. Runs before any tenant context exists."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise _unauthenticated()

    result = await session.execute(
        select(Session, User)
        .join(User, User.id == Session.user_id)
        .where(Session.token_hash == hash_session_token(token))
    )
    row = result.one_or_none()
    if row is None:
        raise _unauthenticated("Session is invalid or has expired.")

    session_model, user = row
    if session_model.expires_at <= datetime.now(UTC):
        raise _unauthenticated("Session is invalid or has expired.")
    if not user.is_active:
        raise ApiError(403, ErrorCode.USER_INACTIVE, "This user account is not active.")

    # Read by RequestContextMiddleware so the access log carries the identity.
    request.state.user_id = str(user.id)
    return Principal(
        user_id=user.id,
        email=user.email,
        name=user.name,
        session_id=session_model.id,
    )


async def get_user_transaction(
    session: DbSession, principal: Annotated[Principal, Depends(get_current_principal)]
) -> AsyncSession:
    """Opens the user scope: a user may read their own rows."""
    await set_current_user(session, principal.user_id)
    return session


def _requested_tenant_id(request: Request) -> uuid.UUID | None:
    raw = request.headers.get(TENANT_HEADER)
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ApiError(
            400, ErrorCode.TENANT_NOT_FOUND, "The requested tenant id is not a valid UUID."
        ) from None


async def get_tenant_context(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_user_transaction)],
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> TenantContext:
    """Verify membership, then enable the tenant scope for the rest of the transaction."""
    memberships = await tenant_repo.list_memberships_for_user(session, principal.user_id)
    if not memberships:
        raise ApiError(
            403, ErrorCode.TENANT_ACCESS_DENIED, "This user does not belong to any tenant."
        )

    requested = _requested_tenant_id(request)
    if requested is None:
        if len(memberships) > 1:
            raise ApiError(
                400,
                ErrorCode.TENANT_REQUIRED,
                f"This user belongs to {len(memberships)} tenants. "
                f"Send the {TENANT_HEADER} header to choose one.",
            )
        membership = memberships[0]
    else:
        membership = next((m for m in memberships if m.tenant_id == requested), None)
        if membership is None:
            # The header never grants access on its own.
            raise ApiError(
                403,
                ErrorCode.TENANT_ACCESS_DENIED,
                "This user has no active membership in the requested tenant.",
            )

    tenant = membership.tenant
    if tenant.status is not TenantStatus.ACTIVE:
        raise ApiError(403, ErrorCode.TENANT_INACTIVE, "This tenant is not active.")

    await set_current_tenant(session, tenant.id)
    request.state.tenant_id = str(tenant.id)
    return TenantContext(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        tenant_slug=tenant.slug,
        user_id=principal.user_id,
        role=membership.role,
    )


async def revoke_session(session: AsyncSession, principal: Principal) -> None:
    await session.execute(delete(Session).where(Session.id == principal.session_id))


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
CurrentUserSession = Annotated[AsyncSession, Depends(get_user_transaction)]
TenantCtx = Annotated[TenantContext, Depends(get_tenant_context)]


def require_role(
    *roles: Role,
) -> Callable[[TenantCtx], Coroutine[None, None, TenantContext]]:
    """Centralized exact-match role check."""
    if not roles:
        raise ValueError("require_role needs at least one role")
    allowed = set(roles)

    async def dependency(context: TenantCtx) -> TenantContext:
        if context.role not in allowed:
            raise ApiError(
                403,
                ErrorCode.ROLE_REQUIRED,
                f"This action requires one of: {', '.join(sorted(r.value for r in allowed))}.",
            )
        return context

    return dependency


def require_minimum_role(
    minimum: Role,
) -> Callable[[TenantCtx], Coroutine[None, None, TenantContext]]:
    """Role check by rank, so a higher role implicitly satisfies a lower bar."""

    async def dependency(context: TenantCtx) -> TenantContext:
        if ROLE_RANK[context.role] < ROLE_RANK[minimum]:
            raise ApiError(
                403,
                ErrorCode.ROLE_REQUIRED,
                f"This action requires the {minimum.value} role or higher.",
            )
        return context

    return dependency
