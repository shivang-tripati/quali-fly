"""The only place that writes the tenant context into PostgreSQL.

`SET LOCAL` scopes the value to the current transaction, so PostgreSQL resets it on
commit or rollback. A plain `SET` would survive until the pooled connection is
returned, which is how tenant context leaks between requests.

Every future tenant-owned table must add `tenant_id` and a policy like:

    tenant_id = current_setting('app.current_tenant', true)::uuid
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

CURRENT_TENANT = "app.current_tenant"
CURRENT_USER = "app.current_user"

_SET_LOCAL = "SELECT set_config(:setting, :value, true)"


async def _set_local(target: AsyncSession | AsyncConnection, setting: str, value: str) -> None:
    await target.execute(text(_SET_LOCAL), {"setting": setting, "value": value})


async def set_current_user(target: AsyncSession | AsyncConnection, user_id: uuid.UUID) -> None:
    """Lets a user read their own rows in user-scoped tables."""
    await _set_local(target, CURRENT_USER, str(user_id))


async def set_current_tenant(target: AsyncSession | AsyncConnection, tenant_id: uuid.UUID) -> None:
    """Enables every tenant-scoped RLS policy for the rest of the transaction."""
    await _set_local(target, CURRENT_TENANT, str(tenant_id))
