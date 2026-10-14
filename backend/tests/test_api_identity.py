"""Authentication, tenant selection and role authorization over the real API.

All of these need a live PostgreSQL instance (see `TEST_DATABASE_URL`).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.security import hash_password, hash_session_token, new_session_token, verify_password
from app.models.membership import ROLE_RANK, Role
from app.models.session import Session
from app.models.tenant import TenantStatus
from tests.factories import TEST_PASSWORD, add_member, create_tenant, create_user, issue_session

SESSION_COOKIE = "qf_session"
TENANT_HEADER = "X-Tenant-ID"


@pytest.fixture
async def world(owner):
    """alice: OWNER of acme + MEMBER of globex.  bob: MEMBER of acme."""
    acme = await create_tenant(owner, name="Acme", slug="acme")
    globex = await create_tenant(owner, name="Globex", slug="globex")
    alice = await create_user(owner, email="alice@dev.local", name="Alice")
    bob = await create_user(owner, email="bob@dev.local", name="Bob")
    await add_member(owner, tenant_id=acme, user_id=alice, role=Role.OWNER)
    await add_member(owner, tenant_id=globex, user_id=alice, role=Role.MEMBER)
    await add_member(owner, tenant_id=acme, user_id=bob, role=Role.MEMBER)
    return {"acme": acme, "globex": globex, "alice": alice, "bob": bob}


async def login(api, email: str, password: str = TEST_PASSWORD):
    return await api.post("/api/v1/auth/login", json={"email": email, "password": password})


async def signed_in(api, owner, world) -> str:
    token = await issue_session(owner, user_id=world["alice"])
    api.cookies.set(SESSION_COOKIE, token)
    return token


# ---------------------------------------------------------------- authentication


async def test_login_returns_the_user_without_the_password_hash(api, world):
    response = await login(api, "alice@dev.local")

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == "alice@dev.local"
    assert body["user"]["is_active"] is True
    assert "password_hash" not in response.text
    assert SESSION_COOKIE in response.cookies


async def test_login_sets_an_httponly_cookie(api, world):
    response = await login(api, "alice@dev.local")

    header = response.headers.get("set-cookie", "")
    assert "HttpOnly" in header
    assert "SameSite=lax" in header.lower().replace("samesite=lax", "SameSite=lax")


async def test_login_is_case_insensitive_on_email(api, world):
    response = await login(api, "ALICE@DEV.LOCAL")

    assert response.status_code == 200


async def test_login_with_a_wrong_password_is_rejected(api, world):
    response = await login(api, "alice@dev.local", "nope")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


async def test_login_for_an_unknown_user_is_rejected(api, world):
    response = await login(api, "nobody@dev.local")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


async def test_inactive_user_cannot_authenticate(api, world, owner):
    await create_user(owner, email="dormant@dev.local", name="Dormant", is_active=False)

    response = await login(api, "dormant@dev.local")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "USER_INACTIVE"


async def test_me_requires_authentication(api, world):
    response = await api.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


async def test_me_rejects_an_unknown_session_token(api, world):
    api.cookies.set(SESSION_COOKIE, "not-a-real-token")

    response = await api.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


async def test_me_rejects_an_expired_session(api, world, owner):
    api.cookies.set(
        SESSION_COOKIE, await issue_session(owner, user_id=world["alice"], ttl_seconds=-60)
    )

    response = await api.get("/api/v1/auth/me")

    assert response.status_code == 401


async def test_me_returns_the_authenticated_user(api, world, owner):
    await signed_in(api, owner, world)

    response = await api.get("/api/v1/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(world["alice"])
    assert body["email"] == "alice@dev.local"
    assert "password_hash" not in response.text


async def test_logout_revokes_the_session(api, world, owner):
    await signed_in(api, owner, world)
    assert (await api.get("/api/v1/auth/me")).status_code == 200

    logout = await api.post("/api/v1/auth/logout")
    after = await api.get("/api/v1/auth/me")

    assert logout.status_code == 204
    assert after.status_code == 401


async def test_session_tokens_are_stored_hashed(db, owner, world):
    raw = new_session_token()
    async with db() as session, session.begin():
        session.add(
            Session(
                user_id=world["alice"],
                token_hash=hash_session_token(raw),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )

    async with db() as session, session.begin():
        stored = (await session.execute(select(Session.token_hash))).scalars().all()

    assert raw not in stored
    assert hash_session_token(raw) in stored


# ------------------------------------------------------------- tenant selection


async def test_tenants_lists_only_the_users_memberships(api, world, owner):
    await signed_in(api, owner, world)

    response = await api.get("/api/v1/tenants")

    assert response.status_code == 200
    assert {(t["slug"], t["role"]) for t in response.json()} == {
        ("acme", "OWNER"),
        ("globex", "MEMBER"),
    }


async def test_tenants_requires_authentication(api, world):
    assert (await api.get("/api/v1/tenants")).status_code == 401


async def test_a_user_cannot_select_a_tenant_they_do_not_belong_to(api, world, owner):
    """The classic cross-tenant attempt: the header alone must not grant access."""
    stranger = await create_tenant(owner, name="Initech", slug="initech")
    api.cookies.set(SESSION_COOKIE, await issue_session(owner, user_id=world["bob"]))

    response = await api.get("/api/v1/tenant/context", headers={TENANT_HEADER: str(stranger)})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TENANT_ACCESS_DENIED"


async def test_valid_tenant_selection_succeeds(api, world, owner):
    await signed_in(api, owner, world)

    response = await api.get(
        "/api/v1/tenant/context", headers={TENANT_HEADER: str(world["globex"])}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant"]["id"] == str(world["globex"])
    assert body["role"] == "MEMBER"
    assert "user_id" not in body  # no internal security detail is exposed


async def test_malformed_tenant_header_is_rejected(api, world, owner):
    await signed_in(api, owner, world)

    response = await api.get("/api/v1/tenant/context", headers={TENANT_HEADER: "not-a-uuid"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "TENANT_NOT_FOUND"


async def test_ambiguous_tenant_selection_is_refused(api, world, owner):
    """alice is in two tenants, so a request without the header is ambiguous."""
    await signed_in(api, owner, world)

    response = await api.get("/api/v1/tenant/context")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "TENANT_REQUIRED"


async def test_single_membership_needs_no_header(api, world, owner):
    api.cookies.set(SESSION_COOKIE, await issue_session(owner, user_id=world["bob"]))

    response = await api.get("/api/v1/tenant/context")

    assert response.status_code == 200
    assert response.json()["tenant"]["id"] == str(world["acme"])


async def test_user_with_no_memberships_is_denied(api, owner):
    loner = await create_user(owner, email="loner@dev.local", name="Loner")
    api.cookies.set(SESSION_COOKIE, await issue_session(owner, user_id=loner))

    response = await api.get("/api/v1/tenant/context")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TENANT_ACCESS_DENIED"


async def test_tenant_context_requires_authentication(api, world):
    assert (await api.get("/api/v1/tenant/context")).status_code == 401


async def test_suspended_tenant_is_denied(api, owner):
    suspended = await create_tenant(
        owner, name="Dormant Co", slug="dormant", status=TenantStatus.SUSPENDED
    )
    user = await create_user(owner, email="dormant-user@dev.local", name="Dormant User")
    await add_member(owner, tenant_id=suspended, user_id=user, role=Role.OWNER)
    api.cookies.set(SESSION_COOKIE, await issue_session(owner, user_id=user))

    response = await api.get("/api/v1/tenant/context", headers={TENANT_HEADER: str(suspended)})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TENANT_INACTIVE"


async def test_tenant_can_only_come_from_the_header(api, world, owner):
    """No endpoint takes a tenant id in a body or query, so it can only be the header."""
    await signed_in(api, owner, world)
    schema = (await api.get("/api/v1/openapi.json")).json()

    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                assert "tenant" not in parameter["name"].lower(), (
                    f"{method.upper()} {path} accepts tenant parameter {parameter['name']}"
                )

    # The context route reads the header and nothing else.
    context_get = schema["paths"]["/api/v1/tenant/context"]["get"]
    assert "requestBody" not in context_get
    assert all("tenant" not in p["name"].lower() for p in context_get.get("parameters", []))


# ----------------------------------------------------------------------- roles


async def test_owner_may_list_tenant_members(api, world, owner):
    await signed_in(api, owner, world)

    response = await api.get("/api/v1/tenant/members", headers={TENANT_HEADER: str(world["acme"])})

    assert response.status_code == 200
    assert {m["email"] for m in response.json()} == {"alice@dev.local", "bob@dev.local"}


async def test_member_may_not_list_tenant_members(api, world, owner):
    api.cookies.set(SESSION_COOKIE, await issue_session(owner, user_id=world["bob"]))

    response = await api.get("/api/v1/tenant/members", headers={TENANT_HEADER: str(world["acme"])})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ROLE_REQUIRED"


async def test_membership_listing_is_scoped_to_the_selected_tenant(api, world, owner):
    """acme members only: alice must not see a globex-only user through acme."""
    outsider = await create_user(owner, email="carol@dev.local", name="Carol")
    globex = await create_tenant(owner, name="Globex Two", slug="globex-two")
    await add_member(owner, tenant_id=globex, user_id=outsider, role=Role.OWNER)
    await signed_in(api, owner, world)

    response = await api.get("/api/v1/tenant/members", headers={TENANT_HEADER: str(world["acme"])})

    assert {m["email"] for m in response.json()} == {"alice@dev.local", "bob@dev.local"}
    assert "carol@dev.local" not in response.text


async def test_unauthenticated_tenant_members_is_401(api, world):
    response = await api.get("/api/v1/tenant/members", headers={TENANT_HEADER: str(world["acme"])})

    assert response.status_code == 401


# ------------------------------------------------------- unit-level (no database)


def test_password_hash_is_salted_and_never_plaintext():
    first = hash_password("s3cret")
    second = hash_password("s3cret")

    assert first != second  # unique salt
    assert "s3cret" not in first
    assert first.startswith("scrypt$")
    assert verify_password("s3cret", first)
    assert not verify_password("wrong", first)


def test_verify_password_rejects_malformed_hashes():
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "")
    assert not verify_password("x", "bcrypt$1$2$3$4$5")


def test_role_ranking_is_strict():
    assert ROLE_RANK[Role.OWNER] > ROLE_RANK[Role.ADMIN] > ROLE_RANK[Role.MEMBER]


def test_require_role_needs_a_role():
    from app.api.deps import require_role

    with pytest.raises(ValueError):
        require_role()


def test_tenant_context_role_helpers():
    from app.core.auth import TenantContext

    owner = TenantContext(
        tenant_id=uuid.uuid4(),
        tenant_name="Acme",
        tenant_slug="acme",
        user_id=uuid.uuid4(),
        role=Role.OWNER,
    )
    member = TenantContext(
        tenant_id=owner.tenant_id,
        tenant_name=owner.tenant_name,
        tenant_slug=owner.tenant_slug,
        user_id=owner.user_id,
        role=Role.MEMBER,
    )

    assert owner.has_at_least(Role.OWNER)
    assert owner.has_at_least(Role.ADMIN)
    assert member.has_at_least(Role.MEMBER)
    assert not member.has_at_least(Role.ADMIN)
