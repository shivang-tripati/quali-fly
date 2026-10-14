"""The user <-> tenant link. This is the first RLS-protected table.

A user may belong to several tenants, so a user is never equal to a tenant.
"""

import enum
import uuid

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.tenant import Tenant
from app.models.user import User


class Role(enum.StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


# Higher wins. Kept here so role comparison lives in exactly one place.
ROLE_RANK: dict[Role, int] = {Role.MEMBER: 1, Role.ADMIN: 2, Role.OWNER: 3}


class TenantMembership(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_tenant_memberships_tenant_user"),
        Index("ix_tenant_memberships_user_id", "user_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[Role] = mapped_column(
        SAEnum(Role, name="membership_role", native_enum=True),
        nullable=False,
        default=Role.MEMBER,
    )

    tenant: Mapped[Tenant] = relationship(lazy="joined")
    user: Mapped[User] = relationship(lazy="joined")
