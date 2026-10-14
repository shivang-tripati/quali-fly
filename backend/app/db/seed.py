"""Development seed data. Run with: python -m app.db.seed

Creates two tenants and three users so tenant isolation can be exercised by hand:

    alice  alice@dev.local   OWNER  of "Acme"     + MEMBER of "Globex"
    bob    bob@dev.local     MEMBER of "Acme"
    carol  carol@dev.local   OWNER  of "Globex"

All credentials are fake and the script refuses to run when APP_ENV=production.

It connects with MIGRATION_DATABASE_URL, like Alembic: `tenant_memberships` is RLS
protected, and the application role may only write to it inside a tenant context.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import close_database
from app.models.membership import Role, TenantMembership
from app.models.tenant import Tenant
from app.models.types import normalize_email
from app.models.user import User

DEV_PASSWORD = "dev-password-change-me"


async def seed() -> None:
    settings = get_settings()
    if settings.is_production:
        raise RuntimeError("Refusing to seed development data when APP_ENV=production.")

    engine = create_async_engine(
        settings.migration_database_url or settings.database_url, pool_pre_ping=True
    )
    try:
        await _seed(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


async def _seed(factory) -> None:
    async with factory() as session, session.begin():
        tenants: dict[str, Tenant] = {}
        for name, slug in (("Acme", "acme"), ("Globex", "globex")):
            existing = (
                await session.execute(select(Tenant).where(Tenant.slug == slug))
            ).scalar_one_or_none()
            if existing is None:
                existing = Tenant(name=name, slug=slug)
                session.add(existing)
                await session.flush()
            tenants[slug] = existing

        plan = [
            (
                "alice@dev.local",
                "Alice",
                [(tenants["acme"], Role.OWNER), (tenants["globex"], Role.MEMBER)],
            ),
            ("bob@dev.local", "Bob", [(tenants["acme"], Role.MEMBER)]),
            ("carol@dev.local", "Carol", [(tenants["globex"], Role.OWNER)]),
        ]

        for email, name, memberships in plan:
            user = (
                await session.execute(select(User).where(User.email == normalize_email(email)))
            ).scalar_one_or_none()
            if user is None:
                user = User(email=email, name=name, password_hash=hash_password(DEV_PASSWORD))
                session.add(user)
                await session.flush()
            else:
                # These are fake dev accounts: make sure the documented password and
                # active flag actually apply, whatever was in the row before.
                user.name = name
                user.password_hash = hash_password(DEV_PASSWORD)
                user.is_active = True
            for tenant, role in memberships:
                existing = (
                    await session.execute(
                        select(TenantMembership).where(
                            TenantMembership.tenant_id == tenant.id,
                            TenantMembership.user_id == user.id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    session.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role=role))
                    await session.flush()

    print("Seeded tenants 'acme' and 'globex'.")
    print("  alice@dev.local  OWNER of acme, MEMBER of globex")
    print("  bob@dev.local    MEMBER of acme")
    print("  carol@dev.local  OWNER of globex")
    print(f"  password for all: {DEV_PASSWORD}")


async def _main() -> None:
    try:
        await seed()
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(_main())


__all__ = ["DEV_PASSWORD", "seed"]
