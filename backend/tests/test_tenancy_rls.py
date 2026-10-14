"""The tests that matter most in Phase 1: PostgreSQL RLS must hold even when the
application forgets to filter by tenant."""

import pytest
from sqlalchemy import func, select, text

from app.db.tenant_context import set_current_tenant, set_current_user
from app.models.membership import Role, TenantMembership
from app.repositories import tenants as tenant_repo
from tests.factories import add_member, create_tenant, create_user


async def _two_tenants_with_members(owner):
    """tenant_a: alice(OWNER), bob(MEMBER).  tenant_b: carol(OWNER), alice(MEMBER)."""
    tenant_a = await create_tenant(owner, name="Acme", slug="acme")
    tenant_b = await create_tenant(owner, name="Globex", slug="globex")
    alice = await create_user(owner, email="alice@dev.local", name="Alice")
    bob = await create_user(owner, email="bob@dev.local", name="Bob")
    carol = await create_user(owner, email="carol@dev.local", name="Carol")

    await add_member(owner, tenant_id=tenant_a, user_id=alice, role=Role.OWNER)
    await add_member(owner, tenant_id=tenant_a, user_id=bob, role=Role.MEMBER)
    await add_member(owner, tenant_id=tenant_b, user_id=carol, role=Role.OWNER)
    await add_member(owner, tenant_id=tenant_b, user_id=alice, role=Role.MEMBER)
    return tenant_a, tenant_b, alice, bob, carol


async def test_rls_denies_everything_without_a_tenant_context(db, owner):
    tenant_a, _, alice, _, _ = await _two_tenants_with_members(owner)

    async with db() as session, session.begin():
        rows = (await session.execute(select(TenantMembership))).scalars().all()

    assert rows == [], "no rows may be visible before a tenant/user context is set"
    assert tenant_a and alice


async def test_the_application_role_cannot_bypass_rls(db, owner):
    """Guards the whole suite: a superuser or table owner would void every RLS test."""
    async with db() as session, session.begin():
        is_superuser, bypassrls = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
        owns_table = (
            await session.execute(
                text(
                    "SELECT (SELECT rolname FROM pg_roles WHERE oid = c.relowner) <> current_user "
                    "FROM pg_class c WHERE c.relname = 'tenant_memberships'"
                )
            )
        ).scalar()

    assert is_superuser is False, "the app role must not be a superuser"
    assert bypassrls is False, "the app role must not have BYPASSRLS"
    assert owns_table is True, "the app role must not own the tenant-scoped table"


async def test_tenant_context_scopes_memberships_to_that_tenant(db, owner):
    tenant_a, tenant_b, _, _, _ = await _two_tenants_with_members(owner)

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_a)
        visible = (await session.execute(select(TenantMembership.tenant_id))).scalars().all()

    assert set(visible) == {tenant_a}
    assert tenant_b not in visible


async def test_other_tenant_context_sees_only_its_own_rows(db, owner):
    _, tenant_b, _, _, _ = await _two_tenants_with_members(owner)

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_b)
        visible = (await session.execute(select(TenantMembership.tenant_id))).scalars().all()

    assert set(visible) == {tenant_b}


async def test_rls_blocks_a_query_with_no_where_clause(db, owner):
    """The dangerous case: a repository that forgets the tenant filter.

    `list_tenant_members` deliberately has no `.where(tenant_id == ...)`. RLS, not
    the application, is what keeps it inside the current tenant.
    """
    tenant_a, tenant_b, _, _, _ = await _two_tenants_with_members(owner)

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_a)
        leaked = await tenant_repo.list_tenant_members(session)

    assert {m.tenant_id for m in leaked} == {tenant_a}
    assert all(m.tenant_id != tenant_b for m in leaked)


async def test_user_context_lets_a_user_see_only_their_own_memberships(db, owner):
    tenant_a, tenant_b, alice, _, _ = await _two_tenants_with_members(owner)

    async with db() as session, session.begin():
        await set_current_user(session, alice)
        visible = (await session.execute(select(TenantMembership.tenant_id))).scalars().all()

    assert set(visible) == {tenant_a, tenant_b}


async def test_user_context_cannot_reach_another_users_memberships(db, owner):
    _, _, _, bob, _ = await _two_tenants_with_members(owner)

    async with db() as session, session.begin():
        await set_current_user(session, bob)
        # bob is only in acme; the globex membership must stay invisible.
        visible = (await session.execute(select(TenantMembership.user_id))).scalars().all()

    assert set(visible) == {bob}


async def test_tenant_context_does_not_leak_across_transactions(db, owner):
    """Transaction 2 must not inherit tenant A, even on a recycled connection."""
    tenant_a, tenant_b, _, _, _ = await _two_tenants_with_members(owner)

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_a)
        first = (await session.execute(select(func.count()).select_from(TenantMembership))).scalar()

    async with db() as session, session.begin():
        second_without_context = (
            await session.execute(select(func.count()).select_from(TenantMembership))
        ).scalar()

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_b)
        third = (await session.execute(select(func.count()).select_from(TenantMembership))).scalar()

    assert first == 2  # alice + bob
    assert second_without_context == 0  # context was reset at commit
    assert third == 2  # carol + alice


async def test_tenant_context_does_not_leak_after_rollback(db, owner):
    tenant_a, _, _, _, _ = await _two_tenants_with_members(owner)

    with pytest.raises(RuntimeError):
        async with db() as session, session.begin():
            await set_current_tenant(session, tenant_a)
            raise RuntimeError("business failure")

    async with db() as session, session.begin():
        leaked = (await session.execute(select(TenantMembership))).scalars().all()

    assert leaked == []


async def test_concurrent_sessions_keep_separate_contexts(db, owner):
    """Two overlapping transactions in separate sessions must not see each other."""
    tenant_a, tenant_b, _, _, _ = await _two_tenants_with_members(owner)

    async with db() as outer, outer.begin():
        await set_current_tenant(outer, tenant_a)
        async with db() as inner, inner.begin():
            await set_current_tenant(inner, tenant_b)
            inner_rows = (await inner.execute(select(TenantMembership.tenant_id))).scalars().all()
        outer_rows = (await outer.execute(select(TenantMembership.tenant_id))).scalars().all()

    assert set(inner_rows) == {tenant_b}
    assert set(outer_rows) == {tenant_a}


async def test_membership_uniqueness_is_enforced_by_the_database(db, owner):
    from sqlalchemy.exc import IntegrityError

    tenant_a, _, alice, _, _ = await _two_tenants_with_members(owner)

    with pytest.raises(IntegrityError):
        await add_member(owner, tenant_id=tenant_a, user_id=alice, role=Role.MEMBER)


async def test_tenant_slug_is_unique(db, owner):
    from sqlalchemy.exc import IntegrityError

    await create_tenant(owner, name="Acme", slug="acme")

    with pytest.raises(IntegrityError):
        await create_tenant(owner, name="Acme Two", slug="acme")


async def test_email_is_stored_lowercased(db, owner):
    from app.models.user import User

    await create_user(owner, email="MiXeD@Dev.Local", name="Mixed")

    async with db() as session, session.begin():
        stored = (await session.execute(select(User.email))).scalars().all()

    assert stored == ["mixed@dev.local"]


async def test_a_membership_lookup_cannot_cross_tenants(db, owner):
    """A user with no membership in the requested tenant must find nothing."""
    _, tenant_b, _, bob, carol = await _two_tenants_with_members(owner)
    assert carol != bob

    async with db() as session, session.begin():
        await set_current_user(session, bob)
        found = await tenant_repo.get_membership(session, tenant_b, bob)

    assert found is None
