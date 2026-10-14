"""Meta connections: the trusted, pre-tenant source lookup and the tenant-scoped CRUD.

`resolve_active_connection` is the only operation in the codebase that reads
`meta_connections` without a tenant context. It calls a single `SECURITY DEFINER`
function (migration 0003) that maps a Meta external identifier to a connection and
tenant. It returns identifiers only - it cannot be used to list connections, read
payloads or enumerate tenants.

Everything else requires `TenantContext`, so normal RLS applies.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meta_connection import MetaConnection, MetaConnectionStatus, MetaProvider

log = logging.getLogger("app.meta")

RESOLVER_SQL = text(
    """
    SELECT connection_id, tenant_id, provider
    FROM resolve_meta_connection(
        :page_id, :instagram_business_id, :whatsapp_business_account_id
    )
    """
)


@dataclass(frozen=True, slots=True)
class ResolvedMetaConnection:
    """Just enough to open the tenant context, and nothing more."""

    connection_id: uuid.UUID
    tenant_id: uuid.UUID
    provider: MetaProvider


async def resolve_active_connection(
    session: AsyncSession,
    *,
    page_id: str | None = None,
    instagram_business_id: str | None = None,
    whatsapp_business_account_id: str | None = None,
) -> ResolvedMetaConnection | None:
    """Map the external identifiers Meta delivered to an ACTIVE connection + tenant.

    Must run inside a transaction. Returns `None` for an unknown, inactive or
    unconfigured source; the caller must not distinguish those cases.
    """
    if not any((page_id, instagram_business_id, whatsapp_business_account_id)):
        return None

    row = (
        await session.execute(
            RESOLVER_SQL,
            {
                "page_id": page_id,
                "instagram_business_id": instagram_business_id,
                "whatsapp_business_account_id": whatsapp_business_account_id,
            },
        )
    ).one_or_none()
    if row is None:
        return None
    return ResolvedMetaConnection(
        connection_id=row.connection_id,
        tenant_id=row.tenant_id,
        provider=MetaProvider(row.provider),
    )


# --- tenant-scoped operations (require app.current_tenant) --------------------


async def list_connections(session: AsyncSession) -> list[MetaConnection]:
    result = await session.execute(select(MetaConnection).order_by(MetaConnection.created_at))
    return list(result.scalars().all())


async def get_connection(session: AsyncSession, connection_id: uuid.UUID) -> MetaConnection | None:
    """Scoped by RLS, so another tenant's connection is simply not found."""
    result = await session.execute(select(MetaConnection).where(MetaConnection.id == connection_id))
    return result.scalar_one_or_none()


async def create_connection(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    values: dict[str, Any],
) -> MetaConnection:
    connection = MetaConnection(tenant_id=tenant_id, **values)
    session.add(connection)
    await session.flush()
    return connection


async def update_connection(
    session: AsyncSession, connection: MetaConnection, values: dict[str, Any]
) -> MetaConnection:
    for field, value in values.items():
        if value is not None or field in _NULLABLE_SETTABLE:
            setattr(connection, field, value)
    await session.flush()
    # `updated_at` is `onupdate=func.now()`, so the UPDATE expires it. Reading it later
    # from a synchronous function such as `redact()` would trigger a lazy load outside
    # greenlet context, which async SQLAlchemy refuses. Refresh once, here, while the
    # load is legal.
    await session.refresh(connection)
    return connection


# Fields where an explicit null is meaningful (clearing a Meta identifier or a
# credential). `access_token_encrypted` is here so disconnecting can actually clear the
# token instead of the setter silently keeping the old one.
_NULLABLE_SETTABLE = frozenset(
    {
        "display_name",
        "page_id",
        "instagram_business_id",
        "whatsapp_business_account_id",
        # Phase 10-O.3: re-binding an account has to be able to clear the number chosen
        # under the previous one, so an explicit null here is a real instruction rather
        # than a PATCH that forgot a field.
        "whatsapp_phone_number_id",
        "whatsapp_display_number",
        "app_id",
        "webhook_verify_token_hash",
        "access_token_encrypted",
        "token_expires_at",
        "connected_at",
    }
)


async def find_active_by_page_id(
    session: AsyncSession, page_id: str, *, tenant_id: uuid.UUID | None = None
) -> MetaConnection | None:
    """The ACTIVE connection already bound to this Page, if any.

    Used when re-authorizing, so a repeat OAuth completes onto the existing row instead
    of colliding with the partial unique index on active `page_id`. `tenant_id` narrows
    the lookup for callers that must not adopt another tenant's connection.
    """
    statement = select(MetaConnection).where(
        MetaConnection.page_id == page_id,
        MetaConnection.status == MetaConnectionStatus.ACTIVE,
    )
    if tenant_id is not None:
        statement = statement.where(MetaConnection.tenant_id == tenant_id)
    result = await session.execute(statement)
    return result.scalars().first()


async def mark_webhook_received(
    session: AsyncSession, connection_id: uuid.UUID, received_at
) -> None:
    """Operational breadcrumb. No-op when unchanged, to avoid pointless writes."""
    await session.execute(
        update(MetaConnection)
        .where(MetaConnection.id == connection_id)
        .values(last_webhook_at=received_at)
    )


def redact(connection: MetaConnection) -> dict[str, Any]:
    """Safe representation for API responses. Secrets become a boolean, never a value."""
    return {
        "id": str(connection.id),
        "provider": connection.provider.value,
        "status": connection.status.value,
        "display_name": connection.display_name,
        "page_id": connection.page_id,
        "instagram_business_id": connection.instagram_business_id,
        "whatsapp_business_account_id": connection.whatsapp_business_account_id,
        # Phase 10-O.3: the send address and the readable number. Neither is a secret, and
        # both are reported so Settings can show what will be used - an opaque id would make
        # a tenant unable to tell whether the right number is connected.
        "whatsapp_phone_number_id": connection.whatsapp_phone_number_id,
        "whatsapp_display_number": connection.whatsapp_display_number,
        "app_id": connection.app_id,
        "webhook_verify_token_configured": connection.webhook_verify_token_hash is not None,
        "access_token_configured": connection.access_token_encrypted is not None,
        "scopes": list(connection.scopes or []),
        "token_expires_at": connection.token_expires_at,
        "connected_at": connection.connected_at,
        "last_webhook_at": connection.last_webhook_at,
        "created_at": connection.created_at,
        "updated_at": connection.updated_at,
    }


__all__ = [
    "MetaConnectionStatus",
    "ResolvedMetaConnection",
    "create_connection",
    "find_active_by_page_id",
    "get_connection",
    "list_connections",
    "mark_webhook_received",
    "redact",
    "resolve_active_connection",
    "update_connection",
]
