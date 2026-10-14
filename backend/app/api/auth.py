"""Authentication endpoints.

FastAPI issues and verifies its own opaque session cookie. There is no shared
secret and no JWT: a request is authenticated by an indexed lookup of the cookie's
SHA-256 hash in `sessions`.
"""

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import (
    SESSION_COOKIE_NAME,
    CurrentPrincipal,
    DbSession,
    SettingsDep,
    _unauthenticated,
    revoke_session,
)
from app.core.errors import ApiError, ErrorCode, error_responses
from app.core.security import hash_password, hash_session_token, new_session_token, verify_password
from app.db.tenant_context import set_current_tenant
from app.models.membership import Role
from app.models.session import Session
from app.models.types import normalize_email
from app.models.user import User
from app.repositories import tenants as tenant_repo
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    RegisterResponse,
    SessionResponse,
    UserResponse,
)
from app.schemas.tenant import TenantBrief
from app.schemas.tenant_settings import PasswordChangeRequest

log = logging.getLogger("app.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/login",
    response_model=SessionResponse,
    status_code=status.HTTP_200_OK,
    summary="Sign in and receive an HttpOnly session cookie.",
    responses=error_responses(401, 403),
)
async def login(
    payload: LoginRequest,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
) -> SessionResponse:
    user = (
        await session.execute(select(User).where(User.email == normalize_email(payload.email)))
    ).scalar_one_or_none()

    # Always run a verification so a missing user and a wrong password cost the same.
    stored = user.password_hash if user else hash_password("no-such-user")
    if not verify_password(payload.password, stored) or user is None:
        raise _unauthenticated("Email or password is incorrect.")
    if not user.is_active:
        raise ApiError(403, ErrorCode.USER_INACTIVE, "This user account is not active.")

    token = new_session_token()
    session.add(
        Session(
            user_id=user.id,
            token_hash=hash_session_token(token),
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds),
        )
    )
    user.last_login_at = datetime.now(UTC)

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )
    return SessionResponse(user=UserResponse.model_validate(user))


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account, its first workspace, an OWNER membership and a session.",
    responses=error_responses(409, 422),
)
async def register(
    payload: RegisterRequest,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
) -> RegisterResponse:
    """Self-serve signup: the missing new-customer entry path.

    One transaction, opened by `get_db`, covers all four writes, so a failure at any step
    leaves no user without a workspace and no workspace without an owner. The session is
    created inside it rather than after it, which is what makes the response mean "you are
    signed in": the commit happens when the request completes, before the response is sent.

    **The email check is not the safety net.** `SELECT ... WHERE email = ?` is a
    convenience that produces a good error message; the authority is `uq_users_email`, so
    two simultaneous registrations for the same address cannot both succeed. The insert
    happens first and its unique violation is what actually decides the race, which is why
    the catch below inspects the constraint rather than trusting the earlier read.

    **Nothing identifying is taken from the request.** `tenant_id`, `user_id` and `role` are
    not fields on `RegisterRequest` at all, and it is `extra="forbid"`, so sending one is a
    422 rather than a field that quietly does nothing. The role is the `Role.OWNER` literal
    below, and the workspace is the one created in this transaction.
    """
    email = normalize_email(payload.email)

    if await session.scalar(select(User.id).where(User.email == email)) is not None:
        raise _email_taken()

    # Hashed before the insert so the cost is paid whether or not the insert is the one that
    # loses the race. scrypt is deliberately slow; doing it twice per request would halve
    # the throughput a registration endpoint can absorb.
    password_hash = hash_password(payload.password)

    try:
        async with session.begin_nested():
            user = User(email=email, name=payload.name, password_hash=password_hash)
            session.add(user)
            await session.flush()
    except IntegrityError as exc:
        # A concurrent registration for the same address won the race. The transaction is
        # left intact by the savepoint, so this is a clean rejection rather than a 500 from
        # a poisoned connection.
        if "uq_users_email" not in str(exc.orig):
            raise
        raise _email_taken() from None

    tenant = await tenant_repo.create_tenant(session, name=payload.workspace_name)

    # The membership insert policy is `WITH CHECK (tenant_id = current_tenant)`, so the
    # context has to be set before the write. It is set to the tenant just created in this
    # transaction, which is the one place doing so is not a privilege escalation.
    await set_current_tenant(session, tenant.id)
    await tenant_repo.add_membership(session, tenant_id=tenant.id, user_id=user.id, role=Role.OWNER)

    token = new_session_token()
    session.add(
        Session(
            user_id=user.id,
            token_hash=hash_session_token(token),
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds),
        )
    )
    user.last_login_at = datetime.now(UTC)

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )
    log.info(
        "auth.registered",
        extra={"user_id": str(user.id), "tenant_id": str(tenant.id)},
    )
    return RegisterResponse(
        user=UserResponse.model_validate(user),
        workspace=TenantBrief(id=tenant.id, name=tenant.name, slug=tenant.slug),
        role=Role.OWNER,
    )


def _email_taken() -> ApiError:
    """409 for an address that already has an account.

    This does disclose that an address is registered, which sign-in deliberately does not.
    The difference is that sign-in has no way to proceed without guessing, whereas here the
    only honest answer to "why did my signup fail" is that the address is taken - a generic
    error would send the user round the loop trying variations of a password that was never
    the problem. The disclosure is confined to the address the caller just typed, so it
    confirms nothing they did not already hold.
    """
    return ApiError(
        409,
        ErrorCode.EMAIL_TAKEN,
        "An account with this email already exists. Sign in instead.",
    )


@router.post(
    "/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change your own password. Every other session is revoked.",
    responses=error_responses(401, 422),
)
async def change_password(
    payload: PasswordChangeRequest,
    session: DbSession,
    settings: SettingsDep,
    principal: CurrentPrincipal,
    response: Response,
) -> None:
    """Let a signed-in user replace their own password.

    This exists because of the team-onboarding design rather than as a general account
    feature: an admin creates a member with a generated one-time password (this codebase has
    no email delivery to send an invitation with), so the member's first act must be to
    replace a credential that the admin and the HTTP response have both seen.

    **Every other session is revoked.** Not a nicety: the old password was transmitted in
    plaintext to an admin's browser and appears in that admin's response body, so treating it
    as still valid elsewhere would mean a credential that has demonstrably been exposed keeps
    working. `revoke_all_other_sessions` is one conditional delete, so the cost is one
    statement and the guarantee is that the exposed password is only ever good for this one
    request.

    The current session survives, so the user is not logged out of the browser they just used.
    """
    user = await session.get(User, principal.user_id)
    if user is None:  # pragma: no cover - the session join above already proved it exists
        raise _unauthenticated()
    # Verified rather than compared, for the same reason as login: the work must be done even
    # when it fails, so a wrong current password costs the same as a right one.
    stored = user.password_hash if user else hash_password("no-such-user")
    if not verify_password(payload.current_password, stored):
        raise _unauthenticated("Your current password is incorrect.")

    user.password_hash = hash_password(payload.new_password)
    await session.execute(
        delete(Session).where(
            Session.user_id == user.id,
            Session.id != principal.session_id,
        )
    )
    log.info("auth.password_changed", extra={"user_id": str(user.id)})
    del response, settings


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current session.",
)
async def logout(response: Response, session: DbSession, principal: CurrentPrincipal) -> None:
    await revoke_session(session, principal)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


@router.get(
    "/me",
    response_model=UserResponse,
    summary="The authenticated user. Never includes the password hash.",
    responses=error_responses(401, 403),
)
async def me(principal: CurrentPrincipal) -> UserResponse:
    return UserResponse(
        id=principal.user_id,
        email=principal.email,
        name=principal.name,
        is_active=True,
    )
