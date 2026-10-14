"""OAuth and connection management at the HTTP boundary.

Authorization is Phase 1's, reused unchanged: `require_minimum_role(Role.ADMIN)` for
mutations. What is new here is the callback, which must be reachable *without* a session
while still refusing to attach a connection to the wrong tenant.

These tests drive the real endpoints through an ASGI client, so routing, dependency
injection, authorization and the error envelope are all exercised. Only the Meta HTTP
boundary is faked.
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.crypto import decrypt
from app.models.membership import Role
from app.models.meta_connection import MetaConnection, MetaConnectionStatus
from app.models.meta_oauth_state import MetaOAuthState
from tests.conftest import TEST_META_TOKEN_KEY
from tests.factories import (
    add_member,
    create_meta_connection,
    create_tenant,
    create_user,
    issue_session,
)
from tests.fakes import PAGE_TOKEN, USER_TOKEN, auth_error, transient_error
from tests.meta_fixtures import PAGE_A

API = "/api/v1"


@pytest.fixture
async def tenants(owner):
    acme = await create_tenant(owner, name="Acme", slug="acme")
    globex = await create_tenant(owner, name="Globex", slug="globex")

    admin = await create_user(owner, email="admin@acme.test", name="Ada Admin")
    await add_member(owner, tenant_id=acme, user_id=admin, role=Role.OWNER)
    admin_token = await issue_session(owner, user_id=admin)

    member = await create_user(owner, email="member@acme.test", name="Mel Member")
    await add_member(owner, tenant_id=acme, user_id=member, role=Role.MEMBER)
    member_token = await issue_session(owner, user_id=member)

    outsider = await create_user(owner, email="out@globex.test", name="Otto Outsider")
    await add_member(owner, tenant_id=globex, user_id=outsider, role=Role.OWNER)
    outsider_token = await issue_session(owner, user_id=outsider)

    return {
        "acme": str(acme),
        "globex": str(globex),
        "admin": str(admin),
        "admin_token": admin_token,
        "member_token": member_token,
        "outsider_token": outsider_token,
    }


def _as_admin(tenants, tenant=None, token=None):
    return {
        "X-Tenant-ID": tenant or tenants["acme"],
        "Cookie": f"qf_session={token or tenants['admin_token']}",
    }


async def _start(client, tenants, *, token=None, tenant=None):
    """Begin OAuth and return `(response, url, state)`.

    The state is recovered from the redirect URL, exactly as the browser would hand it
    back to the callback.
    """
    from urllib.parse import parse_qs, urlparse

    response = await client.get(
        f"{API}/meta/oauth/start",
        headers={
            "X-Tenant-ID": tenant or tenants["acme"],
            "Cookie": f"qf_session={token or tenants['admin_token']}",
        },
    )
    if response.status_code != 200:
        return response, None, None
    url = response.json()["authorize_url"]
    return response, url, parse_qs(urlparse(url).query)["state"][0]


def _patch_graph(monkeypatch, *, pages=None, exchange_error=None):
    """Point the OAuth service at a fake Graph client."""
    from app.services import meta_oauth
    from tests.fakes import FakeGraph

    fake = FakeGraph()
    if pages is not None:
        fake.pages = pages
    if exchange_error is not None:
        fake.exchange_error = exchange_error
    monkeypatch.setattr(meta_oauth, "build_graph_client", lambda _settings: fake)
    return fake


def _all_connections():
    from sqlalchemy import select

    return select(MetaConnection)


# -- authorization ------------------------------------------------------------


async def test_an_unauthenticated_user_cannot_start_oauth(oauth_api, tenants):
    response = await oauth_api.get(
        f"{API}/meta/oauth/start", headers={"X-Tenant-ID": tenants["acme"]}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


async def test_a_member_cannot_start_oauth(oauth_api, tenants):
    """Read access is not connect access: a MEMBER must not obtain a Meta credential."""
    response, _url, _state = await _start(oauth_api, tenants, token=tenants["member_token"])
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ROLE_REQUIRED"


async def test_a_user_of_another_tenant_cannot_start_oauth_for_it(oauth_api, tenants):
    response, _url, _state = await _start(
        oauth_api, tenants, token=tenants["outsider_token"], tenant=tenants["acme"]
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TENANT_ACCESS_DENIED"


async def test_an_admin_can_start_oauth(oauth_api, tenants):
    response, url, state = await _start(oauth_api, tenants)
    assert response.status_code == 200
    assert url.startswith("https://www.facebook.com/")
    assert len(state) >= 40
    assert response.json()["expires_in"] > 0


async def test_the_state_url_carries_no_tenant_id_or_credential(oauth_api, tenants):
    """The tenant is recovered from the database, so the URL must not carry it."""
    _response, url, _state = await _start(oauth_api, tenants)
    assert tenants["acme"] not in url
    assert tenants["admin"] not in url
    assert "access_token" not in url


async def test_oauth_start_fails_closed_when_unconfigured(app, db, tenants):
    """A deployment with no Meta app must say so, not pretend to be connectable.

    Built explicitly rather than relying on the ambient environment, because a developer
    with META_APP_ID in their .env must still get this coverage.
    """
    import httpx

    from app.core.config import get_settings

    unconfigured = get_settings().model_copy(
        update={"meta_app_id": None, "meta_oauth_redirect_uri": None, "meta_app_secret": None}
    )
    app.dependency_overrides[get_settings] = lambda: unconfigured
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response, _url, _state = await _start(client, tenants)
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "META_OAUTH_NOT_CONFIGURED"


async def test_the_callback_fails_closed_when_unconfigured(app, db, tenants, monkeypatch):
    """Same for the callback, which is the endpoint an attacker can actually reach."""
    import httpx

    from app.core.config import get_settings

    unconfigured = get_settings().model_copy(
        update={"meta_app_id": None, "meta_oauth_redirect_uri": None, "meta_app_secret": None}
    )
    app.dependency_overrides[get_settings] = lambda: unconfigured
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(
                f"{API}/meta/oauth/callback", params={"code": "c", "state": "anything"}
            )
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "META_OAUTH_NOT_CONFIGURED"


# -- the callback -------------------------------------------------------------


async def test_the_callback_works_without_any_session(oauth_api, tenants, monkeypatch):
    """Meta redirects the browser; the session may be gone. State is what authorizes it."""
    _response, _url, state = await _start(oauth_api, tenants)
    _patch_graph(monkeypatch)

    response = await oauth_api.get(
        f"{API}/meta/oauth/callback", params={"code": "the-code", "state": state}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["connection"]["page_id"] == PAGE_A
    assert body["connection"]["access_token_configured"] is True


@pytest.mark.parametrize("params", [{"code": "c"}, {"state": "s"}, {}])
async def test_a_callback_without_both_code_and_state_is_rejected(oauth_api, params):
    response = await oauth_api.get(f"{API}/meta/oauth/callback", params=params)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "META_OAUTH_STATE_INVALID"


async def test_a_callback_with_an_invalid_state_is_rejected(oauth_api, monkeypatch):
    _patch_graph(monkeypatch)
    response = await oauth_api.get(
        f"{API}/meta/oauth/callback", params={"code": "c", "state": "never-issued"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "META_OAUTH_STATE_INVALID"


async def test_a_state_cannot_be_replayed(oauth_api, tenants, monkeypatch):
    _response, _url, state = await _start(oauth_api, tenants)
    _patch_graph(monkeypatch)

    first = await oauth_api.get(f"{API}/meta/oauth/callback", params={"code": "c", "state": state})
    assert first.status_code == 200

    replay = await oauth_api.get(f"{API}/meta/oauth/callback", params={"code": "c", "state": state})
    assert replay.status_code == 400
    assert replay.json()["error"]["code"] == "META_OAUTH_STATE_INVALID"


async def test_a_different_user_cannot_complete_someone_elses_flow(oauth_api, tenants, monkeypatch):
    """A shared state is a credential, so the session's user must match the recorded one."""
    _response, _url, state = await _start(oauth_api, tenants)
    _patch_graph(monkeypatch)

    response = await oauth_api.get(
        f"{API}/meta/oauth/callback",
        params={"code": "c", "state": state},
        headers={"Cookie": f"qf_session={tenants['outsider_token']}"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "META_OAUTH_STATE_INVALID"


async def test_the_connection_lands_on_the_tenant_that_started_the_flow(
    oauth_api, tenants, monkeypatch, owner
):
    """Even with a header naming another tenant, the state decides."""
    _response, _url, state = await _start(oauth_api, tenants, tenant=tenants["acme"])
    _patch_graph(monkeypatch)

    response = await oauth_api.get(
        f"{API}/meta/oauth/callback",
        params={"code": "c", "state": state},
        headers={"X-Tenant-ID": tenants["globex"]},
    )
    assert response.status_code == 200

    async with owner() as session, session.begin():
        connection = (await session.execute(_all_connections())).scalar_one()

    assert str(connection.tenant_id) == tenants["acme"]
    assert str(connection.tenant_id) != tenants["globex"]


async def test_a_rejected_exchange_is_a_502_without_leaking_meta_detail(
    oauth_api, tenants, monkeypatch
):
    _response, _url, state = await _start(oauth_api, tenants)
    _patch_graph(monkeypatch, exchange_error=auth_error(190))

    response = await oauth_api.get(
        f"{API}/meta/oauth/callback", params={"code": "c", "state": state}
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "META_TOKEN_EXCHANGE_FAILED"
    # Meta's own error code and wording must not reach the client.
    assert "190" not in response.text
    assert "Invalid" not in response.text


async def test_a_temporarily_unavailable_meta_is_a_503(oauth_api, tenants, monkeypatch):
    _response, _url, state = await _start(oauth_api, tenants)
    _patch_graph(monkeypatch, exchange_error=transient_error())

    response = await oauth_api.get(
        f"{API}/meta/oauth/callback", params={"code": "c", "state": state}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "META_API_UNAVAILABLE"


async def test_no_usable_page_is_reported_clearly(oauth_api, tenants, monkeypatch):
    _response, _url, state = await _start(oauth_api, tenants)
    _patch_graph(monkeypatch, pages=[])

    response = await oauth_api.get(
        f"{API}/meta/oauth/callback", params={"code": "c", "state": state}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "META_OAUTH_NO_PAGE"


async def test_a_state_is_burned_even_when_the_callback_fails(
    oauth_api, tenants, monkeypatch, owner
):
    """Regression: the request transaction rolls back on a 502, and that must not
    resurrect the state.

    Found by the live smoke test - replaying a state whose first callback had failed with
    `META_TOKEN_EXCHANGE_FAILED` was accepted a second time. A burned state that comes
    back is a replayed authorization.
    """
    _response, _url, state = await _start(oauth_api, tenants)
    _patch_graph(monkeypatch, exchange_error=auth_error(190))

    first = await oauth_api.get(f"{API}/meta/oauth/callback", params={"code": "c", "state": state})
    assert first.status_code == 502

    # The state row survived the rollback, and it is marked consumed.
    async with owner() as session, session.begin():
        rows = (await session.execute(select(MetaOAuthState))).scalars().all()
    assert len(rows) == 1
    assert rows[0].consumed_at is not None, "a failed callback must still burn the state"

    # And it really is unusable a second time.
    _patch_graph(monkeypatch)
    replay = await oauth_api.get(f"{API}/meta/oauth/callback", params={"code": "c", "state": state})
    assert replay.status_code == 400
    assert replay.json()["error"]["code"] == "META_OAUTH_STATE_INVALID"


async def test_a_state_is_burned_when_no_usable_page_is_returned(
    oauth_api, tenants, monkeypatch, owner
):
    _response, _url, state = await _start(oauth_api, tenants)
    _patch_graph(monkeypatch, pages=[])

    first = await oauth_api.get(f"{API}/meta/oauth/callback", params={"code": "c", "state": state})
    assert first.status_code == 400
    assert first.json()["error"]["code"] == "META_OAUTH_NO_PAGE"

    async with owner() as session, session.begin():
        rows = (await session.execute(select(MetaOAuthState))).scalars().all()
    assert rows[0].consumed_at is not None


# -- token handling -----------------------------------------------------------


async def test_the_token_is_encrypted_at_rest_and_never_returned(
    oauth_api, tenants, monkeypatch, owner
):
    _response, _url, state = await _start(oauth_api, tenants)
    _patch_graph(monkeypatch)

    response = await oauth_api.get(
        f"{API}/meta/oauth/callback", params={"code": "c", "state": state}
    )
    assert response.status_code == 200

    # Nothing token-shaped anywhere in the response.
    assert PAGE_TOKEN not in response.text
    assert USER_TOKEN not in response.text
    assert "v1." not in response.text

    async with owner() as session, session.begin():
        connection = (await session.execute(_all_connections())).scalar_one()
        stored = connection.access_token_encrypted

    assert PAGE_TOKEN not in stored
    assert decrypt(stored, key=TEST_META_TOKEN_KEY) == PAGE_TOKEN


async def test_the_connection_response_has_no_credential_fields(oauth_api, tenants, monkeypatch):
    _response, _url, state = await _start(oauth_api, tenants)
    _patch_graph(monkeypatch)
    await oauth_api.get(f"{API}/meta/oauth/callback", params={"code": "c", "state": state})

    listed = await oauth_api.get(f"{API}/meta/connections", headers=_as_admin(tenants))
    assert listed.status_code == 200
    body = listed.json()
    assert len(body) == 1
    assert set(body[0]) == {
        "id",
        "provider",
        "status",
        "display_name",
        "page_id",
        "instagram_business_id",
        "whatsapp_business_account_id",
        # Phase 10-O.3. The send address and its readable form; identifiers, not secrets.
        "whatsapp_phone_number_id",
        "whatsapp_display_number",
        "app_id",
        "webhook_verify_token_configured",
        "access_token_configured",
        "scopes",
        "token_expires_at",
        "connected_at",
        "last_webhook_at",
        "created_at",
        "updated_at",
    }
    assert body[0]["access_token_configured"] is True


async def test_reauthorizing_reuses_the_connection_and_refreshes_the_token(
    oauth_api, tenants, monkeypatch, owner
):
    from tests.fakes import AuthorizedPage

    _response, _url, state = await _start(oauth_api, tenants)
    _patch_graph(monkeypatch)
    first = await oauth_api.get(f"{API}/meta/oauth/callback", params={"code": "c", "state": state})
    assert first.status_code == 200, first.text
    first_id = first.json()["connection"]["id"]

    restarted, _url, state = await _start(oauth_api, tenants)
    assert restarted.status_code == 200, restarted.text
    _patch_graph(
        monkeypatch,
        pages=[AuthorizedPage(page_id=PAGE_A, name="Renamed", access_token="fresh-token")],
    )
    second = await oauth_api.get(f"{API}/meta/oauth/callback", params={"code": "c", "state": state})
    assert second.status_code == 200, second.text
    body = second.json()["connection"]
    assert body["id"] == first_id, "re-authorizing must not orphan the connection"
    assert body["display_name"] == "Renamed"

    async with owner() as session, session.begin():
        rows = (await session.execute(_all_connections())).scalars().all()
    assert len(rows) == 1
    assert decrypt(rows[0].access_token_encrypted, key=TEST_META_TOKEN_KEY) == "fresh-token"


# -- disconnect ---------------------------------------------------------------


async def test_a_member_cannot_disconnect(oauth_api, tenants, owner):
    connection_id = await create_meta_connection(
        owner, tenant_id=uuid.UUID(tenants["acme"]), page_id=PAGE_A
    )
    response = await oauth_api.delete(
        f"{API}/meta/connections/{connection_id}",
        headers={
            "X-Tenant-ID": tenants["acme"],
            "Cookie": f"qf_session={tenants['member_token']}",
        },
    )
    assert response.status_code == 403


async def test_disconnect_deactivates_and_clears_the_credential(
    oauth_api, tenants, monkeypatch, owner
):
    _response, _url, state = await _start(oauth_api, tenants)
    _patch_graph(monkeypatch)
    created = await oauth_api.get(
        f"{API}/meta/oauth/callback", params={"code": "c", "state": state}
    )
    connection_id = created.json()["connection"]["id"]

    response = await oauth_api.delete(
        f"{API}/meta/connections/{connection_id}", headers=_as_admin(tenants)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "INACTIVE"
    assert body["access_token_configured"] is False

    async with owner() as session, session.begin():
        connection = await session.get(MetaConnection, uuid.UUID(connection_id))
    assert connection.status is MetaConnectionStatus.INACTIVE
    assert connection.access_token_encrypted is None


async def test_disconnect_keeps_the_row(oauth_api, tenants, owner):
    """Not a delete: the history of where a lead came from must survive."""
    connection_id = await create_meta_connection(
        owner, tenant_id=uuid.UUID(tenants["acme"]), page_id=PAGE_A
    )
    response = await oauth_api.delete(
        f"{API}/meta/connections/{connection_id}", headers=_as_admin(tenants)
    )
    assert response.status_code == 200

    async with owner() as session, session.begin():
        assert await session.get(MetaConnection, connection_id) is not None


async def test_one_tenant_cannot_disconnect_another_tenants_connection(oauth_api, tenants, owner):
    connection_id = await create_meta_connection(
        owner, tenant_id=uuid.UUID(tenants["acme"]), page_id=PAGE_A
    )
    response = await oauth_api.delete(
        f"{API}/meta/connections/{connection_id}",
        headers=_as_admin(tenants, tenant=tenants["globex"], token=tenants["outsider_token"]),
    )
    # RLS hid the row, so 404 rather than 403: indistinguishable from a bad id.
    assert response.status_code == 404

    async with owner() as session, session.begin():
        connection = await session.get(MetaConnection, connection_id)
    assert connection.status is MetaConnectionStatus.ACTIVE


async def test_a_disconnected_connection_stops_routing_webhooks(
    oauth_api, tenants, monkeypatch, owner
):
    """The end of the promise: after disconnect, new deliveries stop resolving."""
    from app.services.meta_signature import hash_verify_token
    from tests.meta_fixtures import VERIFY_TOKEN, body, leadgen_payload, signature_for

    _response, _url, state = await _start(oauth_api, tenants)
    _patch_graph(monkeypatch)
    created = await oauth_api.get(
        f"{API}/meta/oauth/callback", params={"code": "c", "state": state}
    )
    connection_id = uuid.UUID(created.json()["connection"]["id"])

    async with owner() as session, session.begin():
        connection = await session.get(MetaConnection, connection_id)
        connection.webhook_verify_token_hash = hash_verify_token(VERIFY_TOKEN)
        await session.flush()

    raw = body(leadgen_payload())
    signature = signature_for(raw)

    await oauth_api.delete(f"{API}/meta/connections/{connection_id}", headers=_as_admin(tenants))

    delivery = await oauth_api.post(
        f"{API}/webhooks/meta",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
        },
    )
    assert delivery.status_code == 404
    assert delivery.json()["error"]["code"] == "META_CONNECTION_NOT_FOUND"


# -- tenant isolation on reads ------------------------------------------------


async def test_connections_are_listed_per_tenant(oauth_api, tenants, owner):
    await create_meta_connection(owner, tenant_id=uuid.UUID(tenants["acme"]), page_id=PAGE_A)
    await create_meta_connection(
        owner, tenant_id=uuid.UUID(tenants["globex"]), page_id="2000000000000002"
    )

    acme_list = await oauth_api.get(f"{API}/meta/connections", headers=_as_admin(tenants))
    globex_list = await oauth_api.get(
        f"{API}/meta/connections",
        headers=_as_admin(tenants, tenant=tenants["globex"], token=tenants["outsider_token"]),
    )

    assert [c["page_id"] for c in acme_list.json()] == [PAGE_A]
    assert [c["page_id"] for c in globex_list.json()] == ["2000000000000002"]


async def test_another_tenants_connection_is_not_readable_by_id(oauth_api, tenants, owner):
    connection_id = await create_meta_connection(
        owner, tenant_id=uuid.UUID(tenants["acme"]), page_id=PAGE_A
    )
    response = await oauth_api.get(
        f"{API}/meta/connections/{connection_id}",
        headers=_as_admin(tenants, tenant=tenants["globex"], token=tenants["outsider_token"]),
    )
    assert response.status_code == 404


async def test_changing_the_tenant_header_cannot_reach_another_tenants_leads(
    oauth_api, tenants, owner
):
    """The header selects among *your own* memberships; it grants nothing."""
    from app.models.lead import Lead

    lead = Lead(
        tenant_id=uuid.UUID(tenants["acme"]),
        meta_leadgen_id="9000000000000009",
        name="Ada",
    )
    async with owner() as session, session.begin():
        session.add(lead)
        await session.flush()
        lead_id = str(lead.id)

    acme_view = await oauth_api.get(f"{API}/leads", headers=_as_admin(tenants))
    assert acme_view.status_code == 200
    assert [item["meta_leadgen_id"] for item in acme_view.json()["items"]] == ["9000000000000009"]

    globex_headers = _as_admin(tenants, tenant=tenants["globex"], token=tenants["outsider_token"])
    assert (await oauth_api.get(f"{API}/leads", headers=globex_headers)).json()["items"] == []
    assert (
        await oauth_api.get(f"{API}/leads/{lead_id}", headers=globex_headers)
    ).status_code == 404


async def test_an_unauthenticated_user_cannot_list_leads(api, tenants):
    response = await api.get(f"{API}/leads", headers={"X-Tenant-ID": tenants["acme"]})
    assert response.status_code == 401
