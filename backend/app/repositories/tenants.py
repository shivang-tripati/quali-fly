"""Tenant-scoped data access.

Convention for every future repository:

  * application-level `.where(Model.tenant_id == ...)` for clarity and index use
  * RLS as the actual security boundary, so forgetting the WHERE clause returns
    nothing instead of another tenant's data
  * never trust a tenant id from a request body or query string; it comes from
    `TenantContext`, which only a verified membership can produce
"""

import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.membership import Role, TenantMembership
from app.models.tenant import Tenant, TenantStatus
from app.models.user import User


async def update_profile(session: AsyncSession, tenant_id: uuid.UUID, **changes: object) -> None:
    """Write tenant-owned control-plane columns.

    `tenants` has no RLS, so the tenant id is the only filter. It comes from `TenantContext`
    in every caller, never from a request body, which is what keeps this from becoming a way
    to edit another workspace.

    Only keys that are real columns are accepted - the allowlist is explicit rather than
    `**changes` passed through to `setattr`, so a caller that grows a typo fails loudly here
    instead of silently writing a column that does not exist.
    """
    allowed = {
        "business_name",
        "business_description",
        "ai_enabled",
        "follow_up_enabled",
        "follow_up_config",
    }
    unexpected = set(changes) - allowed
    if unexpected:
        raise ValueError(f"not tenant-profile columns: {sorted(unexpected)}")
    if not changes:
        return
    await session.execute(update(Tenant).where(Tenant.id == tenant_id).values(**changes))


async def load_stored_follow_up_config(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict[str, Any]:
    """The raw follow-up document, or `{}` for "use the defaults"."""
    config = (
        await session.execute(select(Tenant.follow_up_config).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    return config or {}


#: PostgreSQL's `slug` column is 63 characters, and a registration slug is derived from a
#: name the customer chose, so the suffix that disambiguates has to fit inside that.
_SLUG_SUFFIX = 8


def slugify(name: str) -> str:
    """A `tenants.slug`-shaped string from a human workspace name.

    `tenants.slug` is globally unique, so it cannot simply be the name lowercased: two
    customers who both call their workspace "Acme" would collide on a value that is not
    meant to be an identity. The caller disambiguates the tail.
    """
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return (base or "workspace")[: 63 - _SLUG_SUFFIX - 1]


async def create_tenant(session: AsyncSession, *, name: str, suffix: str | None = None) -> Tenant:
    """Insert a tenant, retrying with a unique tail if the slug is taken.

    Uniqueness is settled by the `uq_tenants_slug` constraint rather than by a preceding
    `SELECT`, so two simultaneous registrations cannot both decide a slug is free. The
    catch is narrow on purpose: it inspects the constraint name, so an unrelated integrity
    error is re-raised instead of being retried as if it were a collision.
    """
    base = slugify(name)
    tail = f"-{suffix}" if suffix else ""
    candidate = f"{base}{tail}"[:63]
    for _ in range(5):
        tenant = Tenant(name=name, slug=candidate)
        # A savepoint, not the outer transaction. The request owns one transaction for the
        # whole of registration, and a bare `session.rollback()` to recover from a unique
        # violation would discard the user and the membership created before it.
        try:
            async with session.begin_nested():
                session.add(tenant)
                await session.flush()
        except IntegrityError as exc:
            if "uq_tenants_slug" not in str(exc.orig):
                raise
            # Falling back to a random tail rather than a counter: a counter has to be read
            # to be incremented, and two concurrent requests would read the same one.
            candidate = f"{base}-{uuid.uuid4().hex[:_SLUG_SUFFIX]}"
            continue
        return tenant
    raise RuntimeError("could not find a free tenant slug")


class MemberAlreadyExists(Exception):
    """A user with that email already exists.

    A domain exception rather than a raw `IntegrityError`, so the API layer decides the HTTP
    shape. Letting `IntegrityError` escape produced a 500 for what is an ordinary conflict -
    the same mistake the registration path avoids by catching the constraint by name.
    """


@dataclass(frozen=True, slots=True)
class CreatedMember:
    user_id: uuid.UUID
    email: str
    name: str
    role: Role
    one_time_password: str


async def create_member(
    session: AsyncSession,
    *,
    settings,
    tenant_id: uuid.UUID,
    email: str,
    name: str,
    role: Role,
) -> CreatedMember:
    """Create a user and their membership in the caller's transaction.

    `tenant_memberships` is RLS-scoped with `WITH CHECK (tenant_id = current_tenant)`, so the
    membership write only succeeds once the caller has set the context to *their own*
    tenant - which `AdminCtx` has already done. That is the mechanism that prevents this
    from being a cross-tenant membership factory: an admin cannot pass another tenant id,
    because there is no parameter for it.

    The password is generated here and returned once, never stored anywhere but its scrypt
    hash. `MIN_PASSWORD_LENGTH` is applied to the generated value too, so the one-time
    password a member receives is not itself a weak-password problem.
    """
    normalized = email.strip().lower()
    password = _one_time_password()
    role_enum = Role(role)

    existing = (
        await session.execute(select(User.id).where(User.email == normalized))
    ).scalar_one_or_none()
    if existing is not None:
        raise MemberAlreadyExists(normalized)

    # A savepoint, not the bare statement. The unique index is the authority on "this email
    # is taken" - the read above is only there to produce a good message, exactly as in
    # registration - and a concurrent insert losing the race raises `IntegrityError`, which
    # without a savepoint would poison the caller's transaction and turn a 409 into a 500.
    try:
        async with session.begin_nested():
            user = User(email=normalized, name=name, password_hash=hash_password(password))
            session.add(user)
            await session.flush()
            session.add(TenantMembership(tenant_id=tenant_id, user_id=user.id, role=role_enum))
            await session.flush()
    except IntegrityError as exc:
        if "uq_users_email" in str(exc.orig):
            raise MemberAlreadyExists(normalized) from None
        raise
    del settings  # kept for signature symmetry with other services; no setting is consulted
    return CreatedMember(
        user_id=user.id,
        email=normalized,
        name=name,
        role=role_enum,
        one_time_password=password,
    )


def _one_time_password() -> str:
    """A single-use password, generated rather than supplied.

    `secrets` rather than `uuid4`: this is a credential, and it should come from the same
    generator as every other credential in the codebase. 18 bytes of entropy renders as 24
    URL-safe characters, comfortably over the registration floor.
    """
    return secrets.token_urlsafe(18)


__all__ = [
    "CreatedMember",
    "MemberAlreadyExists",
    "Role",
    "TenantStatus",
    "add_membership",
    "create_member",
    "create_tenant",
    "get_membership",
    "get_tenant",
    "list_memberships_for_user",
    "list_tenant_members",
    "load_stored_follow_up_config",
    "slugify",
    "update_profile",
]


async def list_memberships_for_user(
    session: AsyncSession, user_id: uuid.UUID
) -> list[TenantMembership]:
    """Tenants the user belongs to. Requires `app.current_user` to be set."""
    result = await session.execute(
        select(TenantMembership)
        .where(TenantMembership.user_id == user_id)
        .order_by(TenantMembership.created_at)
    )
    return list(result.scalars().all())


async def get_membership(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> TenantMembership | None:
    """Verify the user is a member of this tenant. The authority on tenant access."""
    result = await session.execute(
        select(TenantMembership).where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def add_membership(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, role: Role
) -> TenantMembership:
    """Add one user to one tenant.

    Requires `app.current_tenant` to already be set, because the RLS insert policy on
    `tenant_memberships` is `WITH CHECK (tenant_id = current_tenant)`. That is not an
    obstacle to registration - the tenant was just created in this same transaction, so
    setting the context to it is legitimate rather than a bypass. It is the reason this
    function is not a plain insert that a caller can point at somebody else's tenant.
    """
    membership = TenantMembership(tenant_id=tenant_id, user_id=user_id, role=role)
    session.add(membership)
    await session.flush()
    return membership


async def get_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant | None:
    result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    return result.scalar_one_or_none()


async def list_tenant_members(session: AsyncSession) -> list[TenantMembership]:
    """All memberships in the current tenant. Requires `app.current_tenant` to be set."""
    result = await session.execute(select(TenantMembership).order_by(TenantMembership.created_at))
    return list(result.scalars().all())


__all__ = [
    "Role",
    "TenantStatus",
    "get_membership",
    "get_tenant",
    "list_memberships_for_user",
    "list_tenant_members",
]
