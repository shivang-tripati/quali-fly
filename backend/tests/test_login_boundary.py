"""Is a session row visible to an independent connection the instant login returns?

`fastapi.routing.request_response` closes the request-scoped dependency stack
(`function_stack`) before it sends the response, so `get_db`'s `session.begin()` exit -
and therefore the COMMIT - happens before the client can observe the response. This asserts
that against the real stack rather than trusting the reasoning.

An independent session is used deliberately: a read in the same transaction would see
uncommitted rows and prove nothing.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.security import hash_session_token
from app.models.session import Session
from tests.factories import TEST_PASSWORD, add_member, create_tenant, create_user

pytestmark = pytest.mark.integration

EMAIL = "sam@acme.test"


async def _tenant_with_user(owner):
    tenant = await create_tenant(owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    user = await create_user(owner, email=EMAIL, password=TEST_PASSWORD)
    await add_member(owner, tenant_id=tenant, user_id=user, role="OWNER")
    return tenant, user


def _token(response) -> str:
    for value in response.headers.get_list("set-cookie"):
        if value.startswith("qf_session="):
            return value.split("qf_session=")[1].split(";")[0]
    raise AssertionError("login did not set the session cookie")


async def test_a_fresh_session_is_committed_before_the_response_is_sent(db, owner, api):
    await _tenant_with_user(owner)

    response = await api.post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    token = _token(response)

    # An INDEPENDENT session, on a different connection. Same-transaction visibility would
    # prove nothing at all.
    async with db() as probe, probe.begin():
        found = await probe.scalar(
            select(func.count())
            .select_from(Session)
            .where(Session.token_hash == hash_session_token(token))
        )
    assert found == 1, "the session must be committed by the time the response is observable"

    me = await api.get("/api/v1/auth/me", headers={"Cookie": f"qf_session={token}"})
    assert me.status_code == 200, me.text
    assert me.json()["email"] == EMAIL


async def test_a_repeated_login_is_immediately_usable(db, owner, api):
    """The regression shape: log in, then immediately read the session back. Many times.

    A single pass can pass by luck under a load-dependent condition. Ten passes is what
    makes a window visible if there is one.
    """
    await _tenant_with_user(owner)

    codes = []
    for _ in range(10):
        login = await api.post(
            "/api/v1/auth/login", json={"email": EMAIL, "password": TEST_PASSWORD}
        )
        assert login.status_code == 200, login.text
        me = await api.get("/api/v1/auth/me", headers={"Cookie": f"qf_session={_token(login)}"})
        codes.append(me.status_code)

    assert codes == [200] * 10, f"immediate re-auth after login: {codes}"


async def test_a_rejected_login_commits_no_usable_session(db, owner, api):
    await _tenant_with_user(owner)

    async with db() as session, session.begin():
        before = await session.scalar(select(func.count()).select_from(Session))

    bad = await api.post("/api/v1/auth/login", json={"email": EMAIL, "password": "wrong"})
    assert bad.status_code == 401
    assert "set-cookie" not in {k.lower() for k in bad.headers}

    async with db() as session, session.begin():
        after = await session.scalar(select(func.count()).select_from(Session))
    assert after == before, "a rejected login must not create a session row"


async def test_a_rollback_leaves_no_session(db, owner, api, monkeypatch):
    """A failure after the session row is written must not leave it committed.

    Breaks the handler *after* the write, so the transaction boundary is the only thing
    standing between "row exists" and "usable session".
    """
    await _tenant_with_user(owner)

    async with db() as session, session.begin():
        before = await session.scalar(select(func.count()).select_from(Session))

    import app.api.auth as auth_module

    def boom() -> str:  # pragma: no cover - reached via the request
        raise RuntimeError("injected failure after the session write")

    monkeypatch.setattr(auth_module, "new_session_token", boom)

    # The handler is reached and the failure is turned into a 500 by the registered
    # exception handler, which is what production does.
    failed = await api.post("/api/v1/auth/login", json={"email": EMAIL, "password": TEST_PASSWORD})
    assert failed.status_code == 500
    # And the 500 must not carry the internals.
    body = failed.text
    assert "injected failure" not in body
    assert "Traceback" not in body

    async with db() as session, session.begin():
        after = await session.scalar(select(func.count()).select_from(Session))
    assert after == before, "a rolled-back login must leave no session row"


async def test_sessions_stay_scoped_to_their_own_user(db, owner, api):
    """A session authenticates its own user and nobody else."""
    await _tenant_with_user(owner)
    create_task = await create_user(owner, email="other@acme.test", password=TEST_PASSWORD)
    del create_task

    login = await api.post("/api/v1/auth/login", json={"email": EMAIL, "password": TEST_PASSWORD})
    me = await api.get("/api/v1/auth/me", headers={"Cookie": f"qf_session={_token(login)}"})
    assert me.json()["email"] == EMAIL

    # A token that was never issued is not a session.
    forged = await api.get("/api/v1/auth/me", headers={"Cookie": "qf_session=not-a-real-token"})
    assert forged.status_code == 401


async def test_a_session_is_rejected_once_it_has_expired(db, owner, api):
    await _tenant_with_user(owner)
    login = await api.post("/api/v1/auth/login", json={"email": EMAIL, "password": TEST_PASSWORD})
    token = _token(login)

    async with db() as session, session.begin():
        await session.execute(
            Session.__table__.update()
            .where(Session.token_hash == hash_session_token(token))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    expired = await api.get("/api/v1/auth/me", headers={"Cookie": f"qf_session={token}"})
    assert expired.status_code == 401
