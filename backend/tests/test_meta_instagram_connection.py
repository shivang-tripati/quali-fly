"""Choosing an Instagram Business Account, end to end through the API.

The bug Phase 10-O.2 exists to close: `instagram_business_id` existed, nothing wrote it,
and the only way to set it was a hand-run request carrying a number the browser supplied -
on the very column that `object: "instagram"` webhooks are routed by.

So the tests here are mostly about refusals. Each one is a way the old path could be
abused or could quietly store the wrong thing: an invented id, another Page's account,
another tenant's connection, a member trying to do it, a token Meta has revoked. The one
happy path exists to prove the refusals are not just a blanket refusal.

Discovery is exercised through the real `MetaGraphClient` against a mocked transport
wherever the point is the HTTP contract, and through `FakeGraph` where the point is
persistence. The database is never mocked - RLS and the partial unique index are the
mechanisms under test and a mock would make them meaningless.
"""

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.crypto import encrypt
from app.db.tenant_context import set_current_tenant
from app.models.membership import Role
from app.models.meta_connection import MetaConnection, MetaConnectionStatus
from app.models.meta_oauth_state import MetaOAuthState
from app.services.meta_graph import InstagramBusinessAccount, MetaAuthError, MetaTransientError
from tests.factories import (
    add_member,
    create_meta_connection,
    create_tenant,
    create_user,
    issue_session,
)
from tests.fakes import FakeGraph
from tests.meta_fixtures import IG_A, PAGE_A, PAGE_B, VERIFY_TOKEN

CONNECTS = "/api/v1/meta/connections"
SESSION_COOKIE = "qf_session"
IG_B = "4000000000000004"
IG_FOREIGN = "5000000000000005"

PAGE_TOKEN = "page-token-for-lead-retrieval"


@pytest.fixture
async def world(owner):
    tenant_a = await create_tenant(owner, name="Acme", slug="acme")
    tenant_b = await create_tenant(owner, name="Globex", slug="globex")
    admin = await create_user(owner, email="admin@dev.local", name="Admin")
    member = await create_user(owner, email="member@dev.local", name="Member")
    b_admin = await create_user(owner, email="b-admin@dev.local", name="B Admin")
    await add_member(owner, tenant_id=tenant_a, user_id=admin, role=Role.OWNER)
    await add_member(owner, tenant_id=tenant_a, user_id=member, role=Role.MEMBER)
    await add_member(owner, tenant_id=tenant_b, user_id=b_admin, role=Role.OWNER)

    conn_a = await create_meta_connection(
        owner, tenant_id=tenant_a, page_id=PAGE_A, verify_token=VERIFY_TOKEN
    )
    conn_b = await create_meta_connection(owner, tenant_id=tenant_b, page_id=PAGE_B)

    # OAuth always stores an encrypted token; discovery needs one. `create_meta_connection`
    # deliberately does not, so it is set the way OAuth would. Both tenants' connections get
    # one, because a refusal caused by a missing credential would mask the refusal under
    # test.
    key = get_settings().meta_token_encryption_key
    async with owner() as session, session.begin():
        await set_current_tenant(session, tenant_a)
        connection = await session.get(MetaConnection, conn_a)
        connection.access_token_encrypted = encrypt(PAGE_TOKEN, key=key)
        connection.display_name = "Acme Auto Sales"
        await set_current_tenant(session, tenant_b)
        other = await session.get(MetaConnection, conn_b)
        other.access_token_encrypted = encrypt("globex-page-token", key=key)
        other.display_name = "Globex Bikes"

    return {
        "a": tenant_a,
        "b": tenant_b,
        "admin": admin,
        "member": member,
        "b_admin": b_admin,
        "conn_a": conn_a,
        "conn_b": conn_b,
    }


async def _as(api, owner, user_id, tenant_id):
    api.cookies.set(SESSION_COOKIE, await issue_session(owner, user_id=user_id))
    return {"X-Tenant-ID": str(tenant_id)}


@pytest.fixture
def ig_graph(monkeypatch):
    """Point `meta_instagram` at a `FakeGraph`, returning it.

    Substituted at the constructor, not inside the service, so `discover_accounts` still
    decrypts the stored credential and `select_account` still runs its real comparison -
    only the HTTP call is replaced.
    """
    from app.services import meta_instagram

    graph = FakeGraph()
    monkeypatch.setattr(meta_instagram, "MetaGraphClient", lambda **_kwargs: graph)
    return graph


def _with_ig(graph: FakeGraph, page_id: str, *accounts: InstagramBusinessAccount) -> FakeGraph:
    graph.instagram_accounts[page_id] = list(accounts)
    return graph


def _ig(business_id: str, username: str = "acmecars", name: str = "Acme Cars"):
    return InstagramBusinessAccount(business_id=business_id, username=username, name=name)


# ------------------------------------------------------------- discovery


async def test_the_accounts_meta_reports_are_offered_to_the_tenant(api, owner, world, ig_graph):
    _with_ig(ig_graph, PAGE_A, _ig(IG_A))
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(f"{CONNECTS}/{world['conn_a']}/instagram-accounts", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["page_id"] == PAGE_A
    assert body["page_name"] == "Acme Auto Sales"
    assert body["connected_id"] is None
    assert body["accounts"] == [
        {
            "instagram_business_id": IG_A,
            "username": "acmecars",
            "name": "Acme Cars",
            "label": "Acme Cars",
        }
    ]
    # The Graph call carried the decrypted token, and the response carried no secret.
    assert ig_graph.instagram_calls == [{"page_id": PAGE_A, "page_access_token": PAGE_TOKEN}]
    assert PAGE_TOKEN not in response.text


async def test_a_page_with_no_instagram_account_is_a_200_with_nothing_offered(
    api, owner, world, ig_graph
):
    """Meta saying "none" is the answer to the question, not a failure to render."""
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(f"{CONNECTS}/{world['conn_a']}/instagram-accounts", headers=headers)

    assert response.status_code == 200
    assert response.json()["accounts"] == []


async def test_an_account_without_a_username_still_has_a_usable_label(api, owner, world, ig_graph):
    """Two nameless accounts must not render as two identical options."""
    _with_ig(
        ig_graph,
        PAGE_A,
        InstagramBusinessAccount(business_id=IG_A),
        InstagramBusinessAccount(business_id=IG_B),
    )
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(f"{CONNECTS}/{world['conn_a']}/instagram-accounts", headers=headers)

    labels = [account["label"] for account in response.json()["accounts"]]
    assert labels == [f"Instagram {IG_A}", f"Instagram {IG_B}"]
    assert len(set(labels)) == 2


async def test_a_revoked_meta_authorization_is_reported_as_such(api, owner, world, ig_graph):
    """A 401-shaped Meta answer must not read as "this Page has no account"."""
    ig_graph.instagram_error = MetaAuthError("Invalid OAuth token")
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(f"{CONNECTS}/{world['conn_a']}/instagram-accounts", headers=headers)

    assert response.status_code == 502
    assert "Reconnect" in response.json()["error"]["message"]


async def test_meta_being_down_is_a_503_not_an_empty_account_list(api, owner, world, ig_graph):
    ig_graph.instagram_error = MetaTransientError("Meta request timed out")
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(f"{CONNECTS}/{world['conn_a']}/instagram-accounts", headers=headers)

    assert response.status_code == 503


async def test_a_connection_with_no_page_says_which_thing_is_missing(api, owner, world, ig_graph):
    """Reachable through a hand-made connection, and the message has to be actionable."""
    conn = await create_meta_connection(owner, tenant_id=world["a"])
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(f"{CONNECTS}/{conn}/instagram-accounts", headers=headers)

    assert response.status_code == 422
    assert "Facebook Page" in response.json()["error"]["message"]
    assert ig_graph.instagram_calls == []


async def test_a_disconnected_connection_refuses_before_calling_meta(api, owner, world, ig_graph):
    conn = await create_meta_connection(
        owner, tenant_id=world["a"], page_id=PAGE_B, status=MetaConnectionStatus.INACTIVE
    )
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(f"{CONNECTS}/{conn}/instagram-accounts", headers=headers)

    assert response.status_code == 422
    assert "not active" in response.json()["error"]["message"]


# ------------------------------------------------------------- selection


async def test_a_selected_account_is_persisted(api, owner, world, ig_graph):
    """The one happy path, end to end: select, store, and the row reports it back."""
    _with_ig(ig_graph, PAGE_A, _ig(IG_A))
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/instagram",
        json={"instagram_business_id": IG_A},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["instagram_business_id"] == IG_A
    # The Page relationship is preserved, which is what the outbound adapter needs.
    assert body["page_id"] == PAGE_A
    assert body["access_token_configured"] is True
    assert body["webhook_verify_token_configured"] is True
    assert PAGE_TOKEN not in response.text

    async with owner() as session, session.begin():
        await set_current_tenant(session, world["a"])
        connection = await session.get(MetaConnection, world["conn_a"])
        assert connection.instagram_business_id == IG_A
        # Still the same row: reconnecting a selection must not fork the connection.
        assert connection.page_id == PAGE_A


async def test_switching_accounts_updates_the_same_connection(api, owner, world, ig_graph):
    _with_ig(ig_graph, PAGE_A, _ig(IG_A), _ig(IG_B, "acmebikes", "Acme Bikes"))
    headers = await _as(api, owner, world["admin"], world["a"])

    first = await api.post(
        f"{CONNECTS}/{world['conn_a']}/instagram",
        json={"instagram_business_id": IG_A},
        headers=headers,
    )
    assert first.status_code == 200

    second = await api.post(
        f"{CONNECTS}/{world['conn_a']}/instagram",
        json={"instagram_business_id": IG_B},
        headers=headers,
    )

    assert second.status_code == 200
    assert second.json()["instagram_business_id"] == IG_B

    async with owner() as session, session.begin():
        await set_current_tenant(session, world["a"])
        # The owner session bypasses RLS, so this has to be scoped: tenant B has a
        # connection of its own and its absence here is the point.
        connections = (
            (
                await session.execute(
                    select(MetaConnection).where(MetaConnection.tenant_id == world["a"])
                )
            )
            .scalars()
            .all()
        )
        assert len(connections) == 1


async def test_an_invented_instagram_id_is_refused(api, owner, world, ig_graph):
    """The whole point: the browser's number is a proposal, never the stored value."""
    _with_ig(ig_graph, PAGE_A, _ig(IG_A))
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/instagram",
        json={"instagram_business_id": IG_FOREIGN},
        headers=headers,
    )

    assert response.status_code == 422
    async with owner() as session, session.begin():
        await set_current_tenant(session, world["a"])
        assert (await session.get(MetaConnection, world["conn_a"])).instagram_business_id is None


async def test_an_account_belonging_to_another_page_is_refused(api, owner, world, ig_graph):
    """Same id, different Page: only the accounts of *this* Page are eligible."""
    _with_ig(ig_graph, PAGE_A, _ig(IG_A))
    # IG_B is Meta's answer for the other tenant's Page, so tenant A cannot reach it.
    _with_ig(ig_graph, PAGE_B, _ig(IG_B, "globexbikes", "Globex Bikes"))
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/instagram",
        json={"instagram_business_id": IG_B},
        headers=headers,
    )

    assert response.status_code == 422
    # And the call went to this tenant's Page, not the one that owns IG_B.
    assert [call["page_id"] for call in ig_graph.instagram_calls] == [PAGE_A]


async def test_a_page_with_no_instagram_account_cannot_have_one_attached(
    api, owner, world, ig_graph
):
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/instagram",
        json={"instagram_business_id": IG_A},
        headers=headers,
    )

    assert response.status_code == 422
    assert "no Instagram Business Account" in response.json()["error"]["message"]


async def test_a_revoked_token_cannot_be_used_to_select(api, owner, world, ig_graph):
    ig_graph.instagram_error = MetaAuthError("Invalid OAuth token")
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/instagram",
        json={"instagram_business_id": IG_A},
        headers=headers,
    )

    assert response.status_code == 502


@pytest.mark.parametrize("field", ["instagram_business_id: ''", "instagram_business_id: 'abc'"])
async def test_a_nonsensical_id_is_refused_by_validation(api, owner, world, ig_graph, field):
    key, _, raw = field.partition(": ")
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/instagram",
        json={key: raw.strip("'")},
        headers=headers,
    )

    assert response.status_code == 422
    assert ig_graph.instagram_calls == []


async def test_a_second_tenant_holding_the_same_account_gets_a_conflict(
    api, owner, world, ig_graph
):
    """The uniqueness rule, reached through the supported path rather than a raw insert."""
    _with_ig(ig_graph, PAGE_A, _ig(IG_A))
    _with_ig(ig_graph, PAGE_B, _ig(IG_A))
    headers_a = await _as(api, owner, world["admin"], world["a"])
    assert (
        await api.post(
            f"{CONNECTS}/{world['conn_a']}/instagram",
            json={"instagram_business_id": IG_A},
            headers=headers_a,
        )
    ).status_code == 200

    headers_b = await _as(api, owner, world["b_admin"], world["b"])
    conflict = await api.post(
        f"{CONNECTS}/{world['conn_b']}/instagram",
        json={"instagram_business_id": IG_A},
        headers=headers_b,
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "META_IDENTIFIER_CONFLICT"


# ------------------------------------------------------------- authorization


async def test_another_tenant_cannot_select_on_this_tenants_connection(api, owner, world, ig_graph):
    """404 before any Graph call: the connection is not even visible to them."""
    _with_ig(ig_graph, PAGE_A, _ig(IG_A))
    headers = await _as(api, owner, world["b_admin"], world["b"])

    response = await api.post(
        f"{CONNECTS}/{world['conn_a']}/instagram",
        json={"instagram_business_id": IG_A},
        headers=headers,
    )

    assert response.status_code == 404
    assert ig_graph.instagram_calls == []


async def test_another_tenant_cannot_list_this_tenants_instagram_accounts(
    api, owner, world, ig_graph
):
    _with_ig(ig_graph, PAGE_A, _ig(IG_A))
    headers = await _as(api, owner, world["b_admin"], world["b"])

    response = await api.get(f"{CONNECTS}/{world['conn_a']}/instagram-accounts", headers=headers)

    assert response.status_code == 404


async def test_a_member_cannot_list_or_select(api, owner, world, ig_graph):
    """A MEMBER may read the connection but must not spend its credential or change it."""
    _with_ig(ig_graph, PAGE_A, _ig(IG_A))
    headers = await _as(api, owner, world["member"], world["a"])

    listed = await api.get(f"{CONNECTS}/{world['conn_a']}/instagram-accounts", headers=headers)
    selected = await api.post(
        f"{CONNECTS}/{world['conn_a']}/instagram",
        json={"instagram_business_id": IG_A},
        headers=headers,
    )

    assert listed.status_code == 403
    assert selected.status_code == 403
    assert ig_graph.instagram_calls == []


async def test_an_unauthenticated_caller_reaches_neither(api, world, ig_graph):
    listed = await api.get(f"{CONNECTS}/{world['conn_a']}/instagram-accounts")
    selected = await api.post(
        f"{CONNECTS}/{world['conn_a']}/instagram", json={"instagram_business_id": IG_A}
    )

    assert listed.status_code == 401
    assert selected.status_code == 401


# ------------------------------- the injection hole this phase closes


async def test_the_generic_connection_api_can_no_longer_write_an_instagram_id(
    api, owner, world, ig_graph
):
    """POST/PATCH are the developer-only path Phase 10-O.0 found, and they are now closed.

    `instagram_business_id` is an inbound-routing input - `object: "instagram"` webhooks are
    resolved by it - so accepting one from a request body was never a display setting. The
    field is simply not in the write allowlist, so it is refused at validation.
    """
    headers = await _as(api, owner, world["admin"], world["a"])

    created = await api.post(
        CONNECTS, json={"page_id": PAGE_B, "instagram_business_id": IG_FOREIGN}, headers=headers
    )
    patched = await api.patch(
        f"{CONNECTS}/{world['conn_a']}",
        json={"instagram_business_id": IG_FOREIGN},
        headers=headers,
    )

    assert created.status_code == 422
    assert patched.status_code == 422
    async with owner() as session, session.begin():
        await set_current_tenant(session, world["a"])
        assert (await session.get(MetaConnection, world["conn_a"])).instagram_business_id is None


# ------------------------------------------- existing behaviour is preserved


async def test_an_existing_messenger_connection_is_untouched_by_discovery(
    api, owner, world, ig_graph
):
    """A Page-only connection keeps working exactly as before; discovery only reads."""
    headers = await _as(api, owner, world["admin"], world["a"])
    before = await api.get(f"{CONNECTS}/{world['conn_a']}", headers=headers)

    await api.get(f"{CONNECTS}/{world['conn_a']}/instagram-accounts", headers=headers)

    after = await api.get(f"{CONNECTS}/{world['conn_a']}", headers=headers)
    assert after.json() == before.json()
    assert after.json()["instagram_business_id"] is None


async def test_reauthorizing_meta_keeps_the_selected_instagram_account(
    api, owner, world, ig_graph, monkeypatch
):
    """Reconnect refreshes the credential; it must not silently unbind the account.

    The tenant chose that account, so a token refresh has no reason to forget it - and
    unbinding it would silently stop Instagram routing for a working connection.
    """
    from app.services import meta_oauth
    from app.services.meta_graph import AuthorizedPage

    _with_ig(ig_graph, PAGE_A, _ig(IG_A))
    headers = await _as(api, owner, world["admin"], world["a"])
    await api.post(
        f"{CONNECTS}/{world['conn_a']}/instagram",
        json={"instagram_business_id": IG_A},
        headers=headers,
    )

    # Re-authorize against the same Page with a new page token, which is what OAuth does.
    ig_graph.pages = [
        AuthorizedPage(
            page_id=PAGE_A, name="Acme Auto Sales", access_token="a-fresh-page-token", tasks=()
        )
    ]
    settings = get_settings().model_copy(update={"meta_app_id": "1234567890"})

    async with owner() as session, session.begin():
        await set_current_tenant(session, world["a"])
        connection = await meta_oauth._store_authorized_connection(
            session,
            settings=settings,
            tenant_id=world["a"],
            page=ig_graph.pages[0],
            extra_pages=[],
            expires_at=None,
        )
        assert connection.instagram_business_id == IG_A
        assert connection.page_id == PAGE_A
        assert connection.id == world["conn_a"], "re-authorizing forked the connection"


async def test_discovery_does_not_disturb_the_oauth_guarantees(api, owner, world, ig_graph):
    """The Instagram work adds no second OAuth path, so it must not touch the state rules.

    Phase 10-O.2's whole design is that there is still exactly one OAuth implementation. If
    selecting an account ever started minting state or accepting a token, these are the
    assertions that would catch it.
    """
    _with_ig(ig_graph, PAGE_A, _ig(IG_A))
    headers = await _as(api, owner, world["admin"], world["a"])

    started = await api.get("/api/v1/meta/oauth/start", headers=headers)
    assert started.status_code == 200
    authorize_url = started.json()["authorize_url"]
    # `instagram_basic` is what reading a Page's Instagram edge requires, so it has to be on
    # the consent screen or discovery can only ever fail.
    assert "instagram_basic" in authorize_url
    # And no credential of ours is in the URL we hand the browser.
    assert "client_secret" not in authorize_url

    selected = await api.post(
        f"{CONNECTS}/{world['conn_a']}/instagram",
        json={"instagram_business_id": IG_A},
        headers=headers,
    )
    assert selected.status_code == 200
    # Selecting an account neither mints nor consumes OAuth state.
    async with owner() as session, session.begin():
        await set_current_tenant(session, world["a"])
        states = (await session.execute(select(MetaOAuthState))).scalars().all()
        assert len(states) == 1, "the selection disturbed the OAuth state row"


async def test_an_inbound_instagram_webhook_resolves_the_connection_chosen_in_settings(
    api, db, owner, world, ig_graph
):
    """Acceptance: the routing that depends on this column now has a way to be filled.

    Proved with the real resolution path rather than by reading the column, because a
    correct column is not what Phase 10-O.0 found missing.
    """
    _with_ig(ig_graph, PAGE_A, _ig(IG_A))
    headers = await _as(api, owner, world["admin"], world["a"])
    await api.post(
        f"{CONNECTS}/{world['conn_a']}/instagram",
        json={"instagram_business_id": IG_A},
        headers=headers,
    )

    async with db() as session, session.begin():
        from app.repositories import meta_connections as connection_repo

        resolved = await connection_repo.resolve_active_connection(
            session, instagram_business_id=IG_A
        )

    assert resolved is not None
    assert resolved.tenant_id == world["a"]
    assert resolved.connection_id == world["conn_a"]
