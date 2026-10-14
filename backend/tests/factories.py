"""Factories for identity fixtures, shared by the tenancy and API tests.

They return plain UUIDs rather than ORM objects so nothing is ever used after its
session closed (which would trigger lazy loads on a detached instance).
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.core.security import hash_password, hash_session_token, new_session_token
from app.models.membership import Role, TenantMembership
from app.models.meta_connection import MetaConnection, MetaConnectionStatus
from app.models.session import Session
from app.models.tenant import Tenant, TenantStatus
from app.models.user import User
from app.services.meta_signature import hash_verify_token

TEST_PASSWORD = "correct-horse-battery-staple"

SessionFactory = Callable[[], object]


async def create_tenant(
    factory: SessionFactory,
    *,
    name: str,
    slug: str,
    status: TenantStatus = TenantStatus.ACTIVE,
) -> uuid.UUID:
    async with factory() as session, session.begin():
        tenant = Tenant(name=name, slug=slug, status=status)
        session.add(tenant)
        await session.flush()  # column defaults (the UUID pk) apply at flush
        return tenant.id


async def create_user(
    factory: SessionFactory,
    *,
    email: str,
    name: str = "Test User",
    password: str = TEST_PASSWORD,
    is_active: bool = True,
) -> uuid.UUID:
    async with factory() as session, session.begin():
        user = User(email=email, name=name, password_hash=hash_password(password))
        user.is_active = is_active
        session.add(user)
        await session.flush()
        return user.id


async def add_member(
    factory: SessionFactory, *, tenant_id: uuid.UUID, user_id: uuid.UUID, role: Role
) -> None:
    async with factory() as session, session.begin():
        session.add(TenantMembership(tenant_id=tenant_id, user_id=user_id, role=role))
        await session.flush()


async def issue_session(
    factory: SessionFactory, *, user_id: uuid.UUID, ttl_seconds: int = 3600
) -> str:
    """Create a session row and return the raw cookie token."""
    token = new_session_token()
    async with factory() as session, session.begin():
        session.add(
            Session(
                user_id=user_id,
                token_hash=hash_session_token(token),
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            )
        )
    return token


async def create_meta_connection(
    factory: SessionFactory,
    *,
    tenant_id: uuid.UUID,
    page_id: str | None = None,
    instagram_business_id: str | None = None,
    whatsapp_business_account_id: str | None = None,
    verify_token: str | None = None,
    status: MetaConnectionStatus = MetaConnectionStatus.ACTIVE,
) -> uuid.UUID:
    """A configured Meta connection, created as the owner role (RLS blocks the app role)."""
    connection = MetaConnection(
        tenant_id=tenant_id,
        page_id=page_id,
        instagram_business_id=instagram_business_id,
        whatsapp_business_account_id=whatsapp_business_account_id,
        status=status,
    )
    if verify_token is not None:
        connection.webhook_verify_token_hash = hash_verify_token(verify_token)
    async with factory() as session, session.begin():
        session.add(connection)
        await session.flush()
        return connection.id
