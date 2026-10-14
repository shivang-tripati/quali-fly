"""Request-scoped identity types.

These are immutable values passed between dependencies. The current user and
tenant are never module-level mutable state.
"""

import uuid
from dataclasses import dataclass

from app.models.membership import Role


@dataclass(frozen=True, slots=True)
class Principal:
    """Who the caller is. Established by session verification only."""

    user_id: uuid.UUID
    email: str
    name: str
    session_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Which tenant the caller is operating in, and with what role.

    Only constructable by a dependency that has already verified an active
    membership, so an instance of this type is itself proof of authorization.
    """

    tenant_id: uuid.UUID
    tenant_name: str
    tenant_slug: str
    user_id: uuid.UUID
    role: Role

    def has_at_least(self, *roles: Role) -> bool:
        from app.models.membership import ROLE_RANK

        return any(ROLE_RANK[self.role] >= ROLE_RANK[role] for role in roles)
