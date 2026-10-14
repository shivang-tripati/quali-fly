"""Row Level Security for the Meta tables, and the one narrow trusted lookup.

The properties under test are the same ones Phase 1 established: PostgreSQL, not the
application, is what keeps tenants apart, and the application role must not be able to
switch that off.
"""

import pytest
from sqlalchemy import func, select, text

from app.db.tenant_context import set_current_tenant
from app.models.meta_connection import MetaConnection
from app.models.meta_webhook_event import MetaWebhookEvent
from tests.factories import create_meta_connection, create_tenant
from tests.meta_fixtures import PAGE_A, PAGE_B


@pytest.fixture
async def world(owner, db):
    tenant_a = await create_tenant(owner, name="Acme", slug="acme")
    tenant_b = await create_tenant(owner, name="Globex", slug="globex")
    conn_a = await create_meta_connection(owner, tenant_id=tenant_a, page_id=PAGE_A)
    conn_b = await create_meta_connection(owner, tenant_id=tenant_b, page_id=PAGE_B)
    return {"a": tenant_a, "b": tenant_b, "conn_a": conn_a, "conn_b": conn_b}


async def _seed_event(db, tenant_id, connection_id, key):
    from datetime import UTC, datetime

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        session.add(
            MetaWebhookEvent(
                tenant_id=tenant_id,
                meta_connection_id=connection_id,
                deduplication_key=key,
                payload={"object": "page"},
                headers={},
                received_at=datetime.now(UTC),
            )
        )


async def test_the_application_role_still_cannot_bypass_rls(db):
    async with db() as session, session.begin():
        is_superuser, bypassrls = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
        owns = (
            await session.execute(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                    "WHERE c.relname IN ('meta_connections','meta_webhook_events') "
                    "AND r.rolname = current_user"
                )
            )
        ).scalar()

    assert is_superuser is False
    assert bypassrls is False
    assert owns == 0


async def test_rls_is_enabled_and_forced_on_both_tables(owner):
    async with owner() as session, session.begin():
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname IN ('meta_connections','meta_webhook_events') "
                    "ORDER BY relname"
                )
            )
        ).all()

    assert [r[0] for r in rows] == ["meta_connections", "meta_webhook_events"]
    assert all(r[1] and r[2] for r in rows), "RLS must be enabled AND forced"


async def test_no_context_sees_no_connections_or_events(db, world):
    async with db() as session, session.begin():
        connections = (await session.execute(select(MetaConnection))).scalars().all()
        events = (await session.execute(select(MetaWebhookEvent))).scalars().all()

    assert connections == []
    assert events == []


async def test_tenant_context_scopes_connections_without_a_where_clause(db, world):
    """No `.where(tenant_id == ...)` anywhere: RLS is doing the work."""
    async with db() as session, session.begin():
        await set_current_tenant(session, world["a"])
        seen = (await session.execute(select(MetaConnection.id))).scalars().all()

    assert set(seen) == {world["conn_a"]}


async def test_tenant_context_scopes_events_without_a_where_clause(db, world):
    await _seed_event(db, world["a"], world["conn_a"], "k-a")
    await _seed_event(db, world["b"], world["conn_b"], "k-b")

    async with db() as session, session.begin():
        await set_current_tenant(session, world["a"])
        seen = (await session.execute(select(MetaWebhookEvent))).scalars().all()

    assert {e.tenant_id for e in seen} == {world["a"]}
    assert {e.deduplication_key for e in seen} == {"k-a"}


async def test_the_other_tenant_is_equally_isolated(db, world):
    await _seed_event(db, world["a"], world["conn_a"], "k-a")
    await _seed_event(db, world["b"], world["conn_b"], "k-b")

    async with db() as session, session.begin():
        await set_current_tenant(session, world["b"])
        seen = (await session.execute(select(MetaWebhookEvent))).scalars().all()

    assert {e.deduplication_key for e in seen} == {"k-b"}


async def test_an_event_cannot_be_inserted_into_another_tenant(db, world):
    from datetime import UTC, datetime

    from sqlalchemy.exc import ProgrammingError

    # The INSERT policy's WITH CHECK compares tenant_id against app.current_tenant, so
    # the database itself refuses the write.
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with db() as session, session.begin():
            await set_current_tenant(session, world["a"])
            session.add(
                MetaWebhookEvent(
                    tenant_id=world["b"],
                    meta_connection_id=world["conn_b"],
                    deduplication_key="k-cross",
                    payload={},
                    headers={},
                    received_at=datetime.now(UTC),
                )
            )


async def test_counts_are_tenant_scoped(db, world):
    await _seed_event(db, world["a"], world["conn_a"], "k-a")
    await _seed_event(db, world["b"], world["conn_b"], "k-b")

    async with db() as session, session.begin():
        await set_current_tenant(session, world["a"])
        count_a = (
            await session.execute(select(func.count()).select_from(MetaWebhookEvent))
        ).scalar()
    async with db() as session, session.begin():
        await set_current_tenant(session, world["b"])
        count_b = (
            await session.execute(select(func.count()).select_from(MetaWebhookEvent))
        ).scalar()

    assert (count_a, count_b) == (1, 1)


# --------------------------------------------------- the one trusted lookup


async def test_the_resolver_maps_an_external_id_to_its_tenant(db, world):
    from app.repositories import meta_connections as connection_repo

    async with db() as session, session.begin():
        resolved = await connection_repo.resolve_active_connection(session, page_id=PAGE_A)

    assert resolved is not None
    assert resolved.tenant_id == world["a"]
    assert resolved.connection_id == world["conn_a"]


async def test_the_resolver_returns_nothing_for_an_unknown_source(db, world):
    from app.repositories import meta_connections as connection_repo

    async with db() as session, session.begin():
        assert await connection_repo.resolve_active_connection(session, page_id="000000") is None
        assert await connection_repo.resolve_active_connection(session) is None


async def test_both_trusted_functions_still_depend_on_a_privileged_owner(db):
    """`SECURITY DEFINER` is only safe while the owner outranks the app role.

    The function body is pinned to a `search_path` and inherits the *owner's* rights. If
    a future migration does `OWNER TO quali_fly_app`, the function stops bypassing RLS
    and `resolve_meta_connection` returns nothing for every webhook — silently, with no
    error. The shipped setup works because the schema owner is the `POSTGRES_USER`
    superuser, and superusers bypass RLS unconditionally. This asserts that precondition
    rather than trusting it.
    """
    async with db() as session, session.begin():
        rows = (
            await session.execute(
                text(
                    "SELECT p.proname, (r.rolsuper OR r.rolbypassrls), "
                    "p.provolatile::text, coalesce(p.proconfig::text, '') "
                    "FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "JOIN pg_roles r ON r.oid = p.proowner "
                    "WHERE n.nspname = 'public' AND p.proname = ANY("
                    "  ARRAY['resolve_meta_connection', 'verify_meta_webhook_token',"
                    "        'consume_meta_oauth_state']"
                    ") ORDER BY p.proname"
                )
            )
        ).all()

    assert [r[0] for r in rows] == [
        "consume_meta_oauth_state",
        "resolve_meta_connection",
        "verify_meta_webhook_token",
    ]
    for name, owner_outranks, volatility, proconfig in rows:
        assert owner_outranks, f"{name}: owner cannot see rows without BYPASSRLS"
        # The two resolvers only read, so STABLE is truthful and lets the planner cache
        # them within a statement. `consume_meta_oauth_state` performs an UPDATE, so it
        # must be VOLATILE - claiming STABLE for a mutating function would be a lie the
        # planner could act on.
        expected_volatility = "v" if name == "consume_meta_oauth_state" else "s"
        assert volatility == expected_volatility, f"{name}: wrong volatility"
        # The pin lives in pg_proc.proconfig (a function attribute), not in the body.
        assert "search_path" in proconfig, f"{name}: must pin search_path"
        assert "pg_temp" not in proconfig, f"{name}: pg_temp in search_path is hijackable"


async def test_every_tenant_scoped_table_has_forced_rls(owner):
    """Guard the pattern, not just this phase's tables.

    RLS is written by hand per migration, so a future table that carries `tenant_id`
    without a policy is silent rather than safe. Deriving the list from the schema means
    Phase 3's tables are covered by this assertion for free.
    """
    async with owner() as session, session.begin():
        rows = (
            await session.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                    "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY c.relname"
                )
            )
        ).all()

    # An explicit list, not a count, so a new tenant-scoped table cannot be added without
    # someone deciding here whether it is protected. The guard works in one direction: a
    # table missing from the database but present here fails, and a table present in the
    # database but missing here also fails - which is how Phase 5's `ai_message_runs`
    # was caught.
    assert [r[0] for r in rows] == [
        "ai_message_runs",
        "conversations",
        "human_handoffs",
        "internal_notifications",
        "knowledge_base",
        "leads",
        "messages",
        "meta_connections",
        "meta_oauth_states",
        "meta_webhook_events",
        "scheduled_tasks",
        "tenant_memberships",
    ]
    unprotected = [r[0] for r in rows if not (r[1] and r[2])]
    assert unprotected == [], f"tenant-scoped tables missing forced RLS: {unprotected}"


async def test_the_resolver_exposes_no_payload_or_secret(db, world):
    """It answers "which tenant owns this id" and nothing more."""
    from dataclasses import fields

    from app.repositories import meta_connections as connection_repo

    async with db() as session, session.begin():
        resolved = await connection_repo.resolve_active_connection(session, page_id=PAGE_A)

    assert resolved is not None
    assert {field.name for field in fields(resolved)} == {
        "connection_id",
        "tenant_id",
        "provider",
    }


async def test_the_resolver_is_granted_to_the_application_role_only(db):
    async with db() as session, session.begin():
        public_has_execute = (
            await session.execute(
                text(
                    "SELECT has_function_privilege("
                    "'public', 'public.resolve_meta_connection(text,text,text)', 'EXECUTE')"
                )
            )
        ).scalar()
        app_has_execute = (
            await session.execute(
                text(
                    "SELECT has_function_privilege("
                    "'quali_fly_app', 'public.resolve_meta_connection(text,text,text)', 'EXECUTE')"
                )
            )
        ).scalar()

    assert public_has_execute is False, "EXECUTE must be revoked from PUBLIC"
    assert app_has_execute is True


async def test_the_oauth_state_consume_function_is_granted_to_the_app_role_only(db):
    """Migration 0004's `consume_meta_oauth_state` is the same kind of trusted escape.

    If PUBLIC could call it, anyone could burn a tenant's pending OAuth state - and,
    worse, read back the tenant and user it belongs to.
    """
    async with db() as session, session.begin():
        public_has_execute = (
            await session.execute(
                text(
                    "SELECT has_function_privilege("
                    "'public', 'public.consume_meta_oauth_state(text)', 'EXECUTE')"
                )
            )
        ).scalar()
        app_has_execute = (
            await session.execute(
                text(
                    "SELECT has_function_privilege("
                    "'quali_fly_app', 'public.consume_meta_oauth_state(text)', 'EXECUTE')"
                )
            )
        ).scalar()

    assert public_has_execute is False, "EXECUTE must be revoked from PUBLIC"
    assert app_has_execute is True


async def test_the_verifier_never_returns_the_stored_token_digest(db, world, owner):
    from sqlalchemy import text as _text

    from app.services.meta_signature import hash_verify_token
    from tests.meta_fixtures import VERIFY_TOKEN

    # Configure the token on the connection the fixture already created.
    async with owner() as session, session.begin():
        connection = await session.get(MetaConnection, world["conn_a"])
        connection.webhook_verify_token_hash = hash_verify_token(VERIFY_TOKEN)
        await session.flush()

    async with db() as session, session.begin():
        # The connection id is passed in directly: reading it through a subquery would
        # be blocked by RLS, since the app role has no tenant context here.
        matched = (
            await session.execute(
                _text("SELECT tenant_id FROM verify_meta_webhook_token(:id, :h)"),
                {"id": world["conn_a"], "h": hash_verify_token(VERIFY_TOKEN)},
            )
        ).scalar_one_or_none()
        mismatched = (
            await session.execute(
                _text("SELECT tenant_id FROM verify_meta_webhook_token(:id, :h)"),
                {"id": world["conn_a"], "h": hash_verify_token("wrong")},
            )
        ).scalar_one_or_none()

    assert matched == world["a"]
    assert mismatched is None
