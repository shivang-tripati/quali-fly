"""Meta webhook verification handshake.

The critical invariant: one tenant's verification token must never validate another
tenant's connection, so there is no global token and no global shortcut.
"""

import pytest
from sqlalchemy import select

from app.db.tenant_context import set_current_tenant
from app.models.meta_connection import MetaConnection
from app.models.meta_webhook_event import MetaWebhookEvent
from tests.factories import create_meta_connection, create_tenant
from tests.meta_fixtures import OTHER_VERIFY_TOKEN, PAGE_A, PAGE_B, VERIFY_TOKEN

GET = "/api/v1/webhooks/meta"


@pytest.fixture
async def connections(owner):
    tenant_a = await create_tenant(owner, name="Acme", slug="acme")
    tenant_b = await create_tenant(owner, name="Globex", slug="globex")
    id_a = await create_meta_connection(
        owner, tenant_id=tenant_a, page_id=PAGE_A, verify_token=VERIFY_TOKEN
    )
    id_b = await create_meta_connection(
        owner, tenant_id=tenant_b, page_id=PAGE_B, verify_token=OTHER_VERIFY_TOKEN
    )
    return {"a": id_a, "b": id_b, "tenant_a": tenant_a, "tenant_b": tenant_b}


def _params(mode="subscribe", token=VERIFY_TOKEN, challenge="CHALLENGE-123"):
    return {
        "hub.mode": mode,
        "hub.verify_token": token,
        "hub.challenge": challenge,
    }


async def _verify(api, connection_id: str, **params):
    return await api.get(f"{GET}/{connection_id}", params=_params(**params))


async def test_valid_subscribe_returns_the_challenge(api, db, connections):
    response = await _verify(api, str(connections["a"]))

    assert response.status_code == 200
    assert response.text == "CHALLENGE-123"


async def test_tenants_verify_with_their_own_tokens(api, db, connections):
    a = await _verify(api, str(connections["a"]), token=VERIFY_TOKEN)
    b = await _verify(api, str(connections["b"]), token=OTHER_VERIFY_TOKEN)

    assert a.status_code == 200
    assert b.status_code == 200


async def test_tenant_a_token_does_not_validate_tenant_b(api, db, connections):
    response = await _verify(api, str(connections["b"]), token=VERIFY_TOKEN)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "META_WEBHOOK_SIGNATURE_INVALID"


async def test_tenant_b_token_does_not_validate_tenant_a(api, db, connections):
    response = await _verify(api, str(connections["a"]), token=OTHER_VERIFY_TOKEN)

    assert response.status_code == 403


async def test_invalid_token_is_rejected(api, db, connections):
    response = await _verify(api, str(connections["a"]), token="not-the-token")

    assert response.status_code == 403


async def test_missing_token_is_rejected(api, db, connections):
    response = await api.get(
        f"{GET}/{connections['a']}",
        params={"hub.mode": "subscribe", "hub.challenge": "C"},
    )

    assert response.status_code == 403


@pytest.mark.parametrize("mode", ["unsubscribe", "", "SUBSCRIBE", "subscribe "])
async def test_invalid_mode_is_rejected(api, db, connections, mode):
    response = await api.get(
        f"{GET}/{connections['a']}",
        params={
            "hub.mode": mode,
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "C",
        },
    )

    assert response.status_code == 403


async def test_missing_challenge_is_rejected(api, db, connections):
    response = await api.get(
        f"{GET}/{connections['a']}",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN},
    )

    assert response.status_code == 403


async def test_unknown_connection_is_rejected(api, db, connections):
    import uuid as _uuid

    response = await _verify(api, str(_uuid.uuid4()))

    assert response.status_code == 403


async def test_malformed_connection_id_is_rejected(api, db, connections):
    response = await _verify(api, "not-a-uuid")

    assert response.status_code == 403


async def test_connection_without_a_configured_token_cannot_verify(api, db, owner):
    tenant = await create_tenant(owner, name="NoToken", slug="notoken")
    connection_id = await create_meta_connection(owner, tenant_id=tenant, page_id="555")

    response = await _verify(api, str(connection_id), token="anything")

    assert response.status_code == 403


async def test_verification_token_is_never_stored_in_plaintext(api, db, connections):
    async with db() as session, session.begin():
        await set_current_tenant(session, connections["tenant_a"])
        connection = await session.get(MetaConnection, connections["a"])
        assert VERIFY_TOKEN not in connection.webhook_verify_token_hash
        assert connection.webhook_verify_token_hash != VERIFY_TOKEN


async def test_verification_does_not_write_events(api, db, connections):
    await _verify(api, str(connections["a"]))

    async with db() as session, session.begin():
        await set_current_tenant(session, connections["tenant_a"])
        events = (await session.execute(select(MetaWebhookEvent))).scalars().all()

    assert events == []
