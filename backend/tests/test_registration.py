"""Registration: the new-customer entry path.

Needs a live PostgreSQL (`TEST_DATABASE_URL`). Nothing here is mocked except where a test
explicitly injects a failure, because the properties under test - unique-violation handling,
RLS on the membership insert, transaction atomicity - are all properties of the real
database and are exactly what a mock would fake away.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from app.api.deps import TENANT_HEADER
from app.core.errors import ErrorCode
from app.core.security import hash_session_token
from app.models.membership import Role, TenantMembership
from app.models.session import Session
from app.models.tenant import Tenant
from app.models.user import User
from tests.factories import TEST_PASSWORD, add_member, create_tenant, create_user, issue_session

REGISTER = "/api/v1/auth/register"
SESSION_COOKIE = "qf_session"
EMAIL = "newcomer@example.test"


def payload(**overrides) -> dict:
    body = {
        "name": "New Comer",
        "email": EMAIL,
        "password": "a-long-enough-password",
        "workspace_name": "New Comer Auto",
    }
    body.update(overrides)
    return body


async def register(api, **overrides):
    return await api.post(REGISTER, json=payload(**overrides))


async def counts(owner) -> dict[str, int]:
    """Row counts per table, read as the owner so RLS is not in the way."""

    async with owner() as session, session.begin():
        return {
            "users": await session.scalar(select(func.count()).select_from(User)),
            "tenants": await session.scalar(select(func.count()).select_from(Tenant)),
            "memberships": await session.scalar(select(func.count()).select_from(TenantMembership)),
            "sessions": await session.scalar(select(func.count()).select_from(Session)),
        }


async def read_user(owner, email: str) -> User | None:
    async with owner() as session, session.begin():
        return (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()


async def read_memberships(owner, user_id: uuid.UUID) -> list[TenantMembership]:
    async with owner() as session, session.begin():
        result = await session.execute(
            select(TenantMembership).where(TenantMembership.user_id == user_id)
        )
        return list(result.scalars().all())


# ----------------------------------------------------------------- happy path


async def test_registration_creates_user_tenant_owner_membership_and_session(api, owner):
    response = await register(api)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user"]["email"] == EMAIL
    assert body["user"]["name"] == "New Comer"
    assert "password_hash" not in response.text
    assert body["role"] == "OWNER"
    assert body["workspace"]["name"] == "New Comer Auto"
    assert body["workspace"]["slug"]


async def test_registration_is_atomic_and_creates_exactly_one_of_each(api, owner):
    before = await counts(owner)
    await register(api)
    after = await counts(owner)

    assert after["users"] == before["users"] + 1
    assert after["tenants"] == before["tenants"] + 1
    assert after["memberships"] == before["memberships"] + 1
    assert after["sessions"] == before["sessions"] + 1


async def test_the_new_user_is_the_owner_of_exactly_the_new_workspace(api, owner):
    body = (await register(api)).json()

    memberships = await read_memberships(owner, uuid.UUID(body["user"]["id"]))

    assert len(memberships) == 1
    assert memberships[0].role is Role.OWNER
    assert str(memberships[0].tenant_id) == body["workspace"]["id"]


async def test_the_password_is_hashed_and_never_stored_in_the_clear(api, owner):
    await register(api)
    user = await read_user(owner, EMAIL)

    assert user is not None
    assert user.password_hash != payload()["password"]
    assert user.password_hash.startswith("scrypt$")
    assert payload()["password"] not in user.password_hash


async def test_registration_sets_the_same_httponly_cookie_as_login(api, owner):
    response = await register(api)
    header = response.headers.get("set-cookie", "")

    assert SESSION_COOKIE in response.cookies
    assert "HttpOnly" in header
    # SameSite, and Secure in production, are the existing login behaviour - a new cookie
    # attribute set here but not there would be a session policy that differs by entry path.
    assert "samesite=lax" in header.lower()


async def test_only_the_hash_of_the_session_token_is_stored(api, owner):
    response = await register(api)
    raw = response.cookies[SESSION_COOKIE]

    async with owner() as session, session.begin():
        stored = (await session.execute(select(Session.token_hash))).scalars().all()

    assert raw not in stored
    assert hash_session_token(raw) in stored


async def test_the_raw_session_token_is_not_in_the_response_body(api, owner):
    response = await register(api)
    raw = response.cookies[SESSION_COOKIE]

    assert raw not in response.text
    assert "token" not in response.json()


# ------------------------------------------- session is durable before the response


async def test_auth_me_works_on_the_very_next_request_after_registering(api, owner):
    """The Phase 10-A property, applied to registration.

    A commit that landed after the response would be invisible here, because nothing in
    this test waits, polls or retries - the second request is a separate transaction on a
    separate connection, issued immediately after the first one returned.
    """
    await register(api)

    response = await api.get("/api/v1/auth/me")

    assert response.status_code == 200, response.text
    assert response.json()["email"] == EMAIL


async def test_the_new_session_works_from_an_independent_connection(api, owner, db):
    """Stronger than the read above: a second pool, not the one that wrote the session.

    The response can only be correct if the row is committed, and the row can only be
    committed if the request transaction finished, which is what the next request's
    visibility proves independently of the connection that wrote it.
    """
    await register(api)

    async with db() as session, session.begin():
        count = await session.scalar(select(func.count()).select_from(Session))

    assert count == 1


async def test_tenant_context_resolves_with_no_header_after_registering(api, owner):
    """One membership means the tenant is unambiguous, so no `X-Tenant-ID` is needed."""
    await register(api)

    response = await api.get("/api/v1/tenant/context")

    assert response.status_code == 200, response.text
    assert response.json()["role"] == "OWNER"


async def test_the_tenants_list_shows_exactly_the_new_workspace(api, owner):
    body = (await register(api)).json()

    response = await api.get("/api/v1/tenants")

    assert response.status_code == 200
    assert [(t["id"], t["role"]) for t in response.json()] == [(body["workspace"]["id"], "OWNER")]


async def test_the_new_owner_can_reach_the_dashboard_immediately(api, owner):
    await register(api)

    response = await api.get("/api/v1/dashboard")

    assert response.status_code == 200, response.text


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": "not-an-email"},
        {"email": ""},
        {"email": "a@b"},
        {"password": "short"},
        {"password": ""},
        {"name": ""},
        {"workspace_name": ""},
    ],
)
async def test_invalid_input_is_rejected_before_anything_is_written(api, owner, overrides):
    before = await counts(owner)

    response = await register(api, **overrides)

    assert response.status_code == 422, response.text
    assert await counts(owner) == before


async def test_a_weak_password_is_refused_with_the_length_rule(api, owner):
    response = await register(api, password="a" * 7)

    assert response.status_code == 422
    detail = response.json()["error"]["details"]
    assert any("password" in " ".join(str(part) for part in item["loc"]) for item in detail)


async def test_an_eight_character_password_is_accepted(api, owner):
    assert (await register(api, password="a" * 8)).status_code == 201


async def test_registration_normalises_the_email_like_login_does(api, owner):
    """Case is folded; surrounding whitespace is refused, exactly as at sign-in.

    `EMAIL_PATTERN` is anchored and disallows whitespace, so `" alice@dev.local "` is a 422
    on `/auth/login` too. Registration deliberately does not quietly widen that to
    "strip it and carry on" - accepting input the other entry point rejects would mean two
    different notions of what an email is, and the unique index is on the normalised form.
    """
    response = await register(api, email="MiXeD@Example.TEST")

    assert response.status_code == 201
    assert response.json()["user"]["email"] == "mixed@example.test"
    assert await read_user(owner, "mixed@example.test") is not None

    # The same input, on the same field, is refused by sign-in as well.
    padded = await api.post(
        "/api/v1/auth/login", json={"email": "  mixed@example.test  ", "password": "x"}
    )
    assert padded.status_code == 422


async def test_a_workspace_name_may_contain_anything_a_person_would_type(api, owner):
    # The slug is derived, so the name itself is not constrained to slug characters.
    response = await register(api, workspace_name="Ada's Auto & Truck Co. (Pty) Ltd")

    assert response.status_code == 201
    body = response.json()
    assert body["workspace"]["name"] == "Ada's Auto & Truck Co. (Pty) Ltd"
    assert body["workspace"]["slug"] == "ada-s-auto-truck-co-pty-ltd"


# ------------------------------------------------------------------ duplicates


async def test_a_duplicate_email_is_rejected(api, owner):
    await register(api)

    response = await register(api, workspace_name="Second Workspace")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EMAIL_TAKEN"
    # The rejection must not have created a second workspace for the address that already
    # had one - a customer who retries after a confusing error would otherwise accumulate
    # empty workspaces they never see.
    assert (await counts(owner))["tenants"] == 1


async def test_a_duplicate_email_is_rejected_case_insensitively(api, owner):
    await register(api, email="taken@example.test")

    response = await register(api, email="TAKEN@EXAMPLE.TEST")

    assert response.status_code == 409
    assert (await counts(owner))["tenants"] == 1


async def test_a_duplicate_leaves_no_orphan_workspace_behind(api, owner):
    """The failure the brief calls out: duplicate email must not create a tenant."""
    await register(api)
    before = await counts(owner)

    await register(api)

    assert await counts(owner) == before


async def test_ten_concurrent_registrations_for_one_email_produce_exactly_one_account(api, owner):
    """Real PostgreSQL, real concurrent transactions, no mock of any kind.

    Every request proposes the same address, so exactly one must win. The pre-check
    `SELECT` cannot be what settles this - all ten read "not taken" before any of them
    inserts - so a passing test here is evidence that `uq_users_email` is doing the work.
    """
    results = await asyncio.gather(
        *(register(api, email="race@example.test", workspace_name=f"Race {i}") for i in range(10))
    )

    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 1, [r.status_code for r in results]
    assert codes.count(409) == 9, [r.status_code for r in results]

    after = await counts(owner)
    assert after["users"] == 1
    # One winner, one tenant, one membership. The nine losers rolled their whole
    # transaction back, so none of them left a workspace behind.
    assert after["tenants"] == 1
    assert after["memberships"] == 1
    assert after["sessions"] == 1


async def test_concurrent_registrations_for_different_emails_all_succeed(api, owner):
    results = await asyncio.gather(
        *(
            register(api, email=f"many{i}@example.test", workspace_name=f"Many {i}")
            for i in range(5)
        )
    )

    assert [r.status_code for r in results] == [201] * 5
    after = await counts(owner)
    assert (after["users"], after["tenants"], after["memberships"]) == (5, 5, 5)


async def test_concurrent_registrations_with_the_same_workspace_name_both_succeed(api, owner):
    """The same workspace *name* is fine; only the derived slug has to differ.

    `uq_tenants_slug` is global, so two customers who both call their workspace "Acme"
    would collide on a value that is not meant to be an identity.
    """
    results = await asyncio.gather(
        *(register(api, email=f"acme{i}@example.test", workspace_name="Acme") for i in range(5))
    )

    assert [r.status_code for r in results] == [201] * 5
    slugs = {r.json()["workspace"]["slug"] for r in results}
    assert len(slugs) == 5, slugs
    assert (await counts(owner))["tenants"] == 5


# ------------------------------------------------------------------ atomicity


async def test_a_tenant_failure_leaves_no_orphan_user(api, owner, monkeypatch):
    """User created, then the tenant write fails: the user must not survive."""
    from app.api import auth as auth_module

    async def boom(*args, **kwargs):
        raise RuntimeError("tenant creation failed")

    monkeypatch.setattr(auth_module.tenant_repo, "create_tenant", boom)
    before = await counts(owner)

    response = await register(api)

    assert response.status_code == 500
    assert await counts(owner) == before, "a user survived a failed workspace creation"
    assert await read_user(owner, EMAIL) is None


async def test_a_membership_failure_leaves_no_usable_partial_account(api, owner, monkeypatch):
    from app.api import auth as auth_module

    async def boom(*args, **kwargs):
        raise RuntimeError("membership creation failed")

    monkeypatch.setattr(auth_module.tenant_repo, "add_membership", boom)
    before = await counts(owner)

    response = await register(api)

    assert response.status_code == 500
    assert await counts(owner) == before
    assert await read_user(owner, EMAIL) is None


async def test_a_session_failure_rolls_the_whole_registration_back(api, owner, monkeypatch):
    from app.api import auth as auth_module

    def boom(*args, **kwargs):
        raise RuntimeError("session creation failed")

    monkeypatch.setattr(auth_module, "new_session_token", boom)
    before = await counts(owner)

    response = await register(api)

    assert response.status_code == 500
    # This is the case the brief is most explicit about: the session is created inside the
    # account transaction, so a failure there takes the user and the workspace with it.
    assert await counts(owner) == before
    assert await read_user(owner, EMAIL) is None


async def test_a_failed_registration_does_not_set_a_session_cookie(api, owner, monkeypatch):
    from app.api import auth as auth_module

    def boom(*args, **kwargs):
        raise RuntimeError("session creation failed")

    monkeypatch.setattr(auth_module, "new_session_token", boom)

    response = await register(api)

    assert response.status_code == 500
    assert SESSION_COOKIE not in response.cookies


async def test_the_email_is_reusable_after_a_failed_registration(api, owner, monkeypatch):
    """A rolled-back registration must not squat on the address.

    If the failed attempt had committed a user, the customer's real second attempt would
    be rejected with EMAIL_TAKEN for an account they never received.
    """
    from app.api import auth as auth_module

    def boom(*args, **kwargs):
        raise RuntimeError("session creation failed")

    monkeypatch.setattr(auth_module, "new_session_token", boom)
    assert (await register(api)).status_code == 500
    monkeypatch.undo()

    retry = await register(api)

    assert retry.status_code == 201, retry.text
    assert await read_user(owner, EMAIL) is not None


# ------------------------------------------------- authorization / tenant safety


async def test_the_request_cannot_choose_a_privileged_role(api, owner):
    """No role field exists, and extra fields are refused rather than ignored."""
    for role in ("OWNER", "ADMIN", "SUPERADMIN", "SUPER_OWNER", "ROOT"):
        response = await api.post(REGISTER, json=payload(**{"role": role}))
        assert response.status_code == 422, f"{role} was accepted: {response.text}"

    assert await counts(owner) == {"users": 0, "tenants": 0, "memberships": 0, "sessions": 0}


async def test_the_request_cannot_choose_a_tenant_or_a_user_id(api, owner):
    for field, value in (
        ("tenant_id", str(uuid.uuid4())),
        ("user_id", str(uuid.uuid4())),
        ("membership_id", str(uuid.uuid4())),
        ("permissions", ["*"]),
    ):
        response = await api.post(REGISTER, json=payload(**{field: value}))
        assert response.status_code == 422, f"{field} was accepted: {response.text}"

    assert (await counts(owner))["tenants"] == 0


async def test_the_created_membership_is_always_owner(api, owner):
    """Server-derived, not merely defaulted.

    `role` is rejected outright above, so the only way to observe the role is to read back
    what the server chose on its own.
    """
    response = await register(api, email="derived@example.test")

    memberships = await read_memberships(owner, uuid.UUID(response.json()["user"]["id"]))

    assert [m.role for m in memberships] == [Role.OWNER]
    assert response.json()["role"] == "OWNER"


async def test_a_new_owner_cannot_reach_someone_elses_tenant(api, owner):
    """The classic cross-tenant attempt, immediately after signup."""
    await register(api)
    stranger = await create_tenant(owner, name="Initech", slug=f"initech-{uuid.uuid4().hex[:8]}")
    other = await create_user(owner, email="other@example.test", name="Other")
    await add_member(owner, tenant_id=stranger, user_id=other, role=Role.OWNER)

    response = await api.get("/api/v1/tenant/context", headers={TENANT_HEADER: str(stranger)})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TENANT_ACCESS_DENIED"


async def test_a_new_owner_sees_no_other_workspaces_members(api, owner):
    stranger = await create_tenant(owner, name="Initech", slug=f"initech-{uuid.uuid4().hex[:8]}")
    other = await create_user(owner, email="other@example.test", name="Other")
    await add_member(owner, tenant_id=stranger, user_id=other, role=Role.OWNER)

    await register(api)
    mine = (await register(api, email="second@example.test")).json()["workspace"]["id"]

    response = await api.get("/api/v1/tenant/members", headers={TENANT_HEADER: mine})

    assert response.status_code == 200
    assert {m["email"] for m in response.json()} == {"second@example.test"}
    assert "other@example.test" not in response.text
    assert str(stranger) != mine


async def test_registration_does_not_disturb_an_existing_tenant(api, owner):
    """A signup in the same database must not touch anyone else's rows.

    The incumbent's session is issued *after* the registration, because `register` sets its
    own cookie and a cookie jar holds exactly one - issuing first would mean the
    verification request ran as the brand-new user, which would pass for the wrong reason.
    """
    existing = await create_tenant(owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    incumbent = await create_user(owner, email="incumbent@example.test", name="Incumbent")
    await add_member(owner, tenant_id=existing, user_id=incumbent, role=Role.OWNER)

    assert (await register(api)).status_code == 201

    api.cookies.set(SESSION_COOKIE, await issue_session(owner, user_id=incumbent))
    response = await api.get("/api/v1/tenant/context", headers={TENANT_HEADER: str(existing)})
    assert response.status_code == 200, response.text
    assert response.json()["tenant"]["id"] == str(existing)
    assert response.json()["role"] == "OWNER"

    # And the incumbent still has exactly the one membership they started with.
    assert [m.role for m in await read_memberships(owner, incumbent)] == [Role.OWNER]


async def test_a_new_user_cannot_read_another_tenants_data_by_id(api, owner):
    """RLS, exercised rather than assumed: no WHERE clause on tenant_id anywhere."""
    stranger = await create_tenant(owner, name="Initech", slug=f"initech-{uuid.uuid4().hex[:8]}")
    other = await create_user(owner, email="other@example.test", name="Other")
    await add_member(owner, tenant_id=stranger, user_id=other, role=Role.OWNER)

    await register(api)

    for path in ("/api/v1/leads", "/api/v1/conversations", "/api/v1/handoffs"):
        response = await api.get(path, headers={TENANT_HEADER: str(stranger)})
        assert response.status_code == 403, f"{path} answered {response.status_code}"


# ---------------------------------------------------- existing users unaffected


async def test_an_existing_user_can_still_sign_in_after_this_feature_exists(api, owner):
    tenant = await create_tenant(owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    user = await create_user(owner, email="incumbent@example.test", name="Incumbent")
    await add_member(owner, tenant_id=tenant, user_id=user, role=Role.OWNER)

    response = await api.post(
        "/api/v1/auth/login",
        json={"email": "incumbent@example.test", "password": TEST_PASSWORD},
    )

    assert response.status_code == 200, response.text
    me = await api.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "incumbent@example.test"
    ctx = await api.get("/api/v1/tenant/context", headers={TENANT_HEADER: str(tenant)})
    assert ctx.status_code == 200
    assert ctx.json()["tenant"]["id"] == str(tenant)


async def test_an_existing_weak_password_can_still_sign_in(api, owner):
    """The registration floor must not become a sign-in floor.

    Registration is the only place a password is *chosen*, so that is the only place the
    minimum length applies. Applying it at the door would lock out any account created
    before the rule existed.
    """
    tenant = await create_tenant(owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    user = await create_user(owner, email="old@example.test", name="Old", password="short")
    await add_member(owner, tenant_id=tenant, user_id=user, role=Role.MEMBER)

    response = await api.post(
        "/api/v1/auth/login", json={"email": "old@example.test", "password": "short"}
    )

    assert response.status_code == 200, response.text


async def test_registration_does_not_change_the_login_route(api, owner):
    """Guards the new route from shadowing the old one.

    A user with no membership still gets USER_INACTIVE-free, tenant-aware behaviour: the
    login route answers, and the absence of a workspace surfaces as 403 on the
    tenant-scoped read rather than as a 404 on login.
    """
    await create_user(owner, email="incumbent@example.test", name="Incumbent")

    login = await api.post(
        "/api/v1/auth/login",
        json={"email": "incumbent@example.test", "password": TEST_PASSWORD},
    )
    me = await api.get("/api/v1/auth/me")
    context = await api.get("/api/v1/tenant/context")

    assert login.status_code == 200
    assert me.status_code == 200
    assert context.status_code == 403
    assert context.json()["error"]["code"] == "TENANT_ACCESS_DENIED"


# ------------------------------------------------------------------ error shape


async def test_errors_use_the_standard_envelope_and_leak_nothing(api, owner):
    response = await register(api, email="nope")

    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) <= {"code", "message", "details", "request_id"}
    assert "password_hash" not in response.text
    assert "scrypt" not in response.text


async def test_a_duplicate_email_response_leaks_no_account_detail(api, owner):
    await register(api)

    response = await register(api)

    assert response.status_code == 409
    text = response.text.lower()
    for leak in ("password", "scrypt", "select ", "users where", "traceback", "tenant_id"):
        assert leak not in text


async def test_a_server_failure_does_not_return_internal_detail(api, owner, monkeypatch):
    from app.api import auth as auth_module

    async def boom(*args, **kwargs):
        raise RuntimeError("SECRET_CONNECTION_STRING=do-not-leak")

    monkeypatch.setattr(auth_module.tenant_repo, "create_tenant", boom)

    response = await register(api)

    assert response.status_code == 500
    assert "SECRET_CONNECTION_STRING" not in response.text
    # The envelope's code, not a re-raised class name. `INTERNAL_ERROR` as a literal would
    # have been a guess that passed for the wrong reason if the constant ever changed.
    assert response.json()["error"]["code"] == ErrorCode.INTERNAL_ERROR
