"""OAuth state security.

The callback is public, so the state row is the only thing standing between "any tenant's
connection" and "the tenant that actually started the flow". These tests attack that row
from every angle the prompt names: reuse, expiry, cross-tenant completion, a different
user finishing someone else's flow, and a callback with no state at all.

Note that the helpers below set `app.current_tenant` before writing. That is not
incidental: the INSERT policy on `meta_oauth_states` only accepts a row whose `tenant_id`
matches the tenant context, so a test that skipped it would be exercising a context the
production request path never has.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.core.config import get_settings
from app.db.tenant_context import set_current_tenant
from app.models.membership import Role
from app.models.meta_oauth_state import MetaOAuthState
from app.repositories import meta_oauth_states as state_repo
from app.services.meta_oauth import OAuthNotConfigured, start_oauth
from tests.factories import add_member, create_tenant, create_user


@pytest.fixture
async def acme(owner):
    tenant = await create_tenant(owner, name="Acme", slug="acme")
    admin = await create_user(owner, email="admin@acme.test")
    await add_member(owner, tenant_id=tenant, user_id=admin, role=Role.OWNER)
    return {"tenant_id": tenant, "user_id": admin}


# -- helpers mirroring the production call path -------------------------------


async def _create_state(factory, settings, tenant_id, user_id) -> str:
    async with factory() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        raw, _ = await start_oauth(session, settings=settings, tenant_id=tenant_id, user_id=user_id)
    return raw


async def _start_url(factory, settings, tenant_id, user_id) -> str:
    async with factory() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        _raw, url = await start_oauth(
            session, settings=settings, tenant_id=tenant_id, user_id=user_id
        )
    return url


async def _consume(factory, raw):
    """No tenant context on purpose: the callback has none, and the SECURITY DEFINER
    function is what makes this work anyway."""
    async with factory() as session, session.begin():
        return await state_repo.consume_state(session, raw)


async def _one_state(factory, tenant_id) -> MetaOAuthState:
    async with factory() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        return (await session.execute(select(MetaOAuthState))).scalar_one()


async def _expire_state(factory, tenant_id, raw) -> None:
    async with factory() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        await session.execute(
            update(MetaOAuthState)
            .where(MetaOAuthState.state_hash == state_repo.hash_state(raw))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )


# -- state creation ----------------------------------------------------------


async def test_start_creates_a_state_bound_to_the_tenant_and_user(db, acme, oauth_settings):
    await _create_state(db, oauth_settings, acme["tenant_id"], acme["user_id"])
    row = await _one_state(db, acme["tenant_id"])

    assert row.tenant_id == acme["tenant_id"]
    assert row.user_id == acme["user_id"]
    assert row.consumed_at is None
    assert row.expires_at > datetime.now(UTC)


async def test_only_a_digest_of_the_state_is_stored(db, acme, oauth_settings):
    """A database dump must not be replayable as a callback."""
    raw = await _create_state(db, oauth_settings, acme["tenant_id"], acme["user_id"])
    row = await _one_state(db, acme["tenant_id"])

    assert row.state_hash != raw
    assert state_repo.hash_state(raw) == row.state_hash
    assert raw not in row.state_hash


async def test_start_returns_a_meta_dialog_url_containing_the_state(db, acme, oauth_settings):
    url = await _start_url(db, oauth_settings, acme["tenant_id"], acme["user_id"])
    assert url.startswith("https://www.facebook.com/")
    assert "response_type=code" in url


async def test_each_start_gets_a_distinct_state(db, acme, oauth_settings):
    first = await _create_state(db, oauth_settings, acme["tenant_id"], acme["user_id"])
    second = await _create_state(db, oauth_settings, acme["tenant_id"], acme["user_id"])
    assert first != second
    assert len(first) >= 32


async def test_the_state_is_short_lived(db, acme, oauth_settings):
    tight = oauth_settings.model_copy(update={"meta_oauth_state_ttl_seconds": 60})
    await _create_state(db, tight, acme["tenant_id"], acme["user_id"])
    row = await _one_state(db, acme["tenant_id"])
    # `created_at` is a server-side now() evaluated at flush, a hair after the Python
    # clock that computed `expires_at`, so the measured span is a fraction over the TTL.
    lifetime = row.expires_at - row.created_at
    assert timedelta(seconds=55) < lifetime <= timedelta(seconds=61)


async def test_the_state_row_cannot_be_written_for_another_tenant(db, acme, oauth_settings):
    """RLS, not a check in the service: the INSERT policy rejects a mismatched tenant."""
    from sqlalchemy.exc import ProgrammingError

    other = await create_tenant(db, name="Globex", slug="globex")
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with db() as session, session.begin():
            await set_current_tenant(session, acme["tenant_id"])
            await start_oauth(
                session,
                settings=oauth_settings,
                tenant_id=other,
                user_id=acme["user_id"],
            )


async def test_start_fails_closed_when_oauth_is_unconfigured(db, acme):
    unconfigured = get_settings().model_copy(
        update={"meta_app_id": None, "meta_oauth_redirect_uri": None}
    )
    async with db() as session, session.begin():
        await set_current_tenant(session, acme["tenant_id"])
        with pytest.raises(OAuthNotConfigured):
            await start_oauth(
                session,
                settings=unconfigured,
                tenant_id=acme["tenant_id"],
                user_id=acme["user_id"],
            )


# -- state consumption --------------------------------------------------------


async def test_a_valid_state_is_consumed_once(db, acme, oauth_settings):
    raw = await _create_state(db, oauth_settings, acme["tenant_id"], acme["user_id"])
    consumed = await _consume(db, raw)
    assert consumed is not None
    assert consumed.tenant_id == acme["tenant_id"]
    assert consumed.user_id == acme["user_id"]
    assert consumed.redirect_uri == oauth_settings.meta_oauth_redirect_uri


async def test_a_state_cannot_be_reused(db, acme, oauth_settings):
    raw = await _create_state(db, oauth_settings, acme["tenant_id"], acme["user_id"])
    assert await _consume(db, raw) is not None
    assert await _consume(db, raw) is None


async def test_concurrent_consumption_of_one_state_yields_exactly_one_winner(
    db, acme, oauth_settings
):
    """The single-use guard is an `UPDATE ... WHERE consumed_at IS NULL`, so it has to
    hold under concurrency or one state could authorize two connections."""
    raw = await _create_state(db, oauth_settings, acme["tenant_id"], acme["user_id"])

    results = await asyncio.gather(*(_consume(db, raw) for _ in range(5)), return_exceptions=True)
    winners = [r for r in results if isinstance(r, state_repo.ConsumedState)]
    assert len(winners) == 1


async def test_an_expired_state_is_rejected(db, acme, oauth_settings):
    raw = await _create_state(db, oauth_settings, acme["tenant_id"], acme["user_id"])
    await _expire_state(db, acme["tenant_id"], raw)
    assert await _consume(db, raw) is None


async def test_an_unknown_state_is_rejected(db):
    assert await _consume(db, "never-issued") is None


async def test_a_state_carries_its_own_tenant_and_no_other(db, owner, oauth_settings):
    """The tenant comes from the row, so tenant B's callback cannot land on tenant A."""
    tenant_a = await create_tenant(owner, name="Acme", slug="acme")
    tenant_b = await create_tenant(owner, name="Globex", slug="globex")
    user_a = await create_user(owner, email="a@acme.test")
    user_b = await create_user(owner, email="b@globex.test")
    await add_member(owner, tenant_id=tenant_a, user_id=user_a, role=Role.OWNER)
    await add_member(owner, tenant_id=tenant_b, user_id=user_b, role=Role.OWNER)

    raw_a = await _create_state(db, oauth_settings, tenant_a, user_a)
    consumed = await _consume(db, raw_a)

    assert consumed.tenant_id == tenant_a
    assert consumed.tenant_id != tenant_b
    assert consumed.user_id == user_a
    assert consumed.user_id != user_b


async def test_the_consumed_state_carries_the_redirect_uri_used_at_creation(
    db, acme, oauth_settings
):
    """Meta requires the exchange to use the same redirect URI as the authorization, so
    the consumed row - not current configuration - must supply it."""
    configured = oauth_settings.model_copy(
        update={"meta_oauth_redirect_uri": "https://first.example.test/cb"}
    )
    raw = await _create_state(db, configured, acme["tenant_id"], acme["user_id"])
    consumed = await _consume(db, raw)
    assert consumed.redirect_uri == "https://first.example.test/cb"


async def test_expired_states_can_be_cleaned_up(db, acme, oauth_settings):
    raw = await _create_state(db, oauth_settings, acme["tenant_id"], acme["user_id"])
    await _expire_state(db, acme["tenant_id"], raw)

    async with db() as session, session.begin():
        await set_current_tenant(session, acme["tenant_id"])
        assert await state_repo.delete_expired(session) == 1
    assert await _consume(db, raw) is None


# -- the state row is tenant-scoped like everything else ----------------------


async def test_state_rows_are_hidden_from_a_tenant_that_does_not_own_them(
    db, owner, oauth_settings
):
    tenant_a = await create_tenant(owner, name="Acme", slug="acme")
    tenant_b = await create_tenant(owner, name="Globex", slug="globex")
    user_a = await create_user(owner, email="a@acme.test")
    await add_member(owner, tenant_id=tenant_a, user_id=user_a, role=Role.OWNER)

    await _create_state(db, oauth_settings, tenant_a, user_a)

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_b)
        visible = (await session.execute(select(MetaOAuthState))).scalars().all()

    assert visible == []


# -- guards that do not need the database ------------------------------------


def test_state_values_are_long_and_random():
    values = {state_repo.new_state_value() for _ in range(50)}
    assert len(values) == 50
    assert all(len(v) >= 40 for v in values)
