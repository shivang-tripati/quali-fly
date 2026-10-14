"""Meta webhook ingestion: verification, raw-body signatures, routing and idempotency.

These are integration tests against a real PostgreSQL, because the properties that
matter (RLS scoping, the unique dedupe constraint, the trusted resolver function) are
database behaviours, not Python ones.
"""

import asyncio
import hashlib
import json

import pytest
from sqlalchemy import select

from app.models.meta_connection import MetaConnection, MetaConnectionStatus
from app.models.meta_webhook_event import MetaWebhookEvent, MetaWebhookEventStatus
from tests.factories import (
    add_member,
    create_meta_connection,
    create_tenant,
    create_user,
    issue_session,
)
from tests.meta_fixtures import (
    IG_A,
    META_APP_SECRET,
    OTHER_VERIFY_TOKEN,
    PAGE_A,
    PAGE_B,
    VERIFY_TOKEN,
    body,
    instagram_payload,
    leadgen_payload,
    messaging_payload,
    signature_for,
    signed_headers,
)

POST = "/api/v1/webhooks/meta"


@pytest.fixture
async def world(owner):
    """Tenant A owns page A; tenant B owns page B and an Instagram business id."""
    tenant_a = await create_tenant(owner, name="Acme", slug="acme")
    tenant_b = await create_tenant(owner, name="Globex", slug="globex")
    await create_meta_connection(
        owner,
        tenant_id=tenant_a,
        page_id=PAGE_A,
        verify_token=VERIFY_TOKEN,
    )
    await create_meta_connection(
        owner,
        tenant_id=tenant_b,
        page_id=PAGE_B,
        instagram_business_id=IG_A,
        verify_token=OTHER_VERIFY_TOKEN,
    )
    return {"a": tenant_a, "b": tenant_b}


async def _post(api, raw: bytes, headers: dict[str, str]):
    return await api.post(POST, content=raw, headers=headers)


# Stored state is read with the owner factory: RLS hides everything from the
# unprivileged app role without a tenant context, and these helpers verify what was
# *written*. The read path through the app role is covered in test_meta_rls.py.
async def _events_for_connection(owner, connection_id):
    async with owner() as session, session.begin():
        result = await session.execute(
            select(MetaWebhookEvent).where(MetaWebhookEvent.meta_connection_id == connection_id)
        )
        return list(result.scalars().all())


async def _get_event_by_key(owner, connection_id, key):
    async with owner() as session, session.begin():
        result = await session.execute(
            select(MetaWebhookEvent).where(
                MetaWebhookEvent.meta_connection_id == connection_id,
                MetaWebhookEvent.deduplication_key == key,
            )
        )
        return result.scalar_one_or_none()


async def _connection_id_for(owner, page_id: str):
    async with owner() as session, session.begin():
        result = await session.execute(
            select(MetaConnection).where(MetaConnection.page_id == page_id)
        )
        return result.scalar_one().id


# ------------------------------------------------------------------ happy path


async def test_valid_signed_event_is_persisted_and_acked(api, db, world, owner):
    raw = body(leadgen_payload(PAGE_A))

    response = await _post(api, raw, signed_headers(raw))

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["duplicate"] is False

    connection_id = await _connection_id_for(owner, PAGE_A)
    events = await _events_for_connection(owner, connection_id)
    assert len(events) == 1
    event = events[0]
    assert event.tenant_id == world["a"]
    assert event.status is MetaWebhookEventStatus.RECEIVED
    assert event.object_type == "page"
    assert event.event_type == "leadgen"
    assert event.source_identifier == PAGE_A
    assert event.payload == leadgen_payload(PAGE_A)


async def test_messaging_event_keeps_the_meta_event_id(api, db, world, owner):
    raw = body(messaging_payload(PAGE_A))

    response = await _post(api, raw, signed_headers(raw))

    assert response.status_code == 200
    connection_id = await _connection_id_for(owner, PAGE_A)
    event = (await _events_for_connection(owner, connection_id))[0]
    assert event.meta_event_id == "mid.$abc123"
    assert event.event_type == "messaging"


async def test_headers_stored_are_allowlisted_and_exclude_the_signature(api, db, world, owner):
    raw = body(leadgen_payload(PAGE_A))

    await _post(api, raw, signed_headers(raw))

    connection_id = await _connection_id_for(owner, PAGE_A)
    event = (await _events_for_connection(owner, connection_id))[0]
    assert "user-agent" in event.headers
    assert "x-hub-signature-256" not in event.headers
    assert META_APP_SECRET not in json.dumps(event.headers)


# ------------------------------------------------------------------ signatures


async def test_missing_signature_is_rejected(api, db, world, owner):
    raw = body(leadgen_payload(PAGE_A))

    response = await _post(api, raw, {"Content-Type": "application/json"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "META_WEBHOOK_SIGNATURE_INVALID"


async def test_incorrect_signature_is_rejected(api, db, world, owner):
    raw = body(leadgen_payload(PAGE_A))
    wrong = "sha256=" + "0" * 64

    response = await _post(api, raw, {**signed_headers(raw), "X-Hub-Signature-256": wrong})

    assert response.status_code == 403


async def test_signature_over_modified_body_is_rejected(api, db, world, owner):
    """The body Meta actually sent is what is signed, not a re-serialised copy."""
    signed = body(leadgen_payload(PAGE_A))
    tampered = signed + b" "

    response = await _post(api, tampered, {**signed_headers(signed)})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "META_WEBHOOK_SIGNATURE_INVALID"


@pytest.mark.parametrize(
    "header",
    [
        "sha1=" + "0" * 40,
        "sha256=" + "0" * 63,
        "sha256=nothex",
        "md5=abc",
        "",
    ],
)
async def test_malformed_signature_headers_are_rejected(api, db, world, header):
    raw = body(leadgen_payload(PAGE_A))

    response = await _post(api, raw, {**signed_headers(raw), "X-Hub-Signature-256": header})

    assert response.status_code == 403


async def test_signature_of_a_different_secret_is_rejected(api, db, world, owner):
    raw = body(leadgen_payload(PAGE_A))
    foreign = "sha256=" + signature_for(raw, "someone-elses-secret").removeprefix("sha256=")

    response = await _post(api, raw, {**signed_headers(raw), "X-Hub-Signature-256": foreign})

    assert response.status_code == 403


# ------------------------------------------------------------------- routing


async def test_page_id_routes_to_the_owning_tenant(api, db, world, owner):
    raw_b = body(leadgen_payload(PAGE_B))

    response = await _post(api, raw_b, signed_headers(raw_b))

    assert response.status_code == 200
    connection_b = await _connection_id_for(owner, PAGE_B)
    event = (await _events_for_connection(owner, connection_b))[0]
    assert event.tenant_id == world["b"]


async def test_instagram_business_id_routes_to_the_owning_tenant(api, db, world, owner):
    raw = body(instagram_payload(IG_A))

    response = await _post(api, raw, signed_headers(raw))

    assert response.status_code == 200
    connection_b = await _connection_id_for(owner, PAGE_B)
    event = (await _events_for_connection(owner, connection_b))[0]
    assert event.tenant_id == world["b"]
    assert event.source_identifier == IG_A


async def test_unknown_page_id_is_rejected(api, db, world, owner):
    raw = body(leadgen_payload("9999999999999999"))

    response = await _post(api, raw, signed_headers(raw))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "META_CONNECTION_NOT_FOUND"


async def test_inactive_connection_does_not_route(api, db, world, owner):
    await create_meta_connection(
        owner,
        tenant_id=world["a"],
        page_id="4444444444444444",
        status=MetaConnectionStatus.INACTIVE,
    )
    raw = body(leadgen_payload("4444444444444444"))

    response = await _post(api, raw, signed_headers(raw))

    assert response.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"object": "page"},
        {"object": "page", "entry": []},
        {"object": "page", "entry": [{}]},
        {"object": "page", "entry": "not-a-list"},
    ],
)
async def test_payloads_without_a_routable_identifier_are_rejected(api, db, world, payload):
    raw = body(payload)

    response = await _post(api, raw, signed_headers(raw))

    assert response.status_code in (400, 404)


async def test_non_json_body_is_rejected(api, db, world, owner):
    raw = b"this is not json"

    response = await _post(api, raw, signed_headers(raw))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "META_WEBHOOK_PAYLOAD_INVALID"


# ---------------------------------------------------------------- idempotency


async def test_duplicate_delivery_does_not_create_a_second_row(api, db, world, owner):
    raw = body(leadgen_payload(PAGE_A))

    first = await _post(api, raw, signed_headers(raw))
    second = await _post(api, raw, signed_headers(raw))

    assert first.status_code == second.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert first.json()["event_id"] == second.json()["event_id"]

    connection_id = await _connection_id_for(owner, PAGE_A)
    assert len(await _events_for_connection(owner, connection_id)) == 1


async def test_deduplication_key_is_deterministic_and_addressable(api, db, world, owner):
    raw = body(leadgen_payload(PAGE_A))
    await _post(api, raw, signed_headers(raw))

    connection_id = await _connection_id_for(owner, PAGE_A)
    event = (await _events_for_connection(owner, connection_id))[0]

    # Stable: derived from the payload's own identifiers, not from a clock or a random value.
    canonical = json.dumps(leadgen_payload(PAGE_A), sort_keys=True, separators=(",", ":"))
    assert (
        event.deduplication_key == "meta:sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    )
    found = await _get_event_by_key(owner, connection_id, event.deduplication_key)
    assert found is not None and found.id == event.id


async def test_messaging_dedup_key_prefers_the_meta_message_id(api, db, world, owner):
    raw = body(messaging_payload(PAGE_A, mid="mid.$dedup"))

    await _post(api, raw, signed_headers(raw))

    connection_id = await _connection_id_for(owner, PAGE_A)
    event = (await _events_for_connection(owner, connection_id))[0]
    assert event.deduplication_key == "meta:mid:mid.$dedup"


async def test_concurrent_duplicate_deliveries_create_one_row(api, db, world, owner):
    """The unique index, not a read-then-write, is what makes this safe."""
    raw = body(leadgen_payload(PAGE_A))
    headers = signed_headers(raw)

    responses = await asyncio.gather(
        *[_post(api, raw, headers) for _ in range(5)], return_exceptions=True
    )

    codes = [r.status_code for r in responses if not isinstance(r, BaseException)]
    assert codes and set(codes) == {200}
    connection_id = await _connection_id_for(owner, PAGE_A)
    assert len(await _events_for_connection(owner, connection_id)) == 1


async def test_distinct_events_create_distinct_rows(api, db, world, owner):
    first = body(messaging_payload(PAGE_A, mid="mid.$one"))
    second = body(messaging_payload(PAGE_A, mid="mid.$two"))

    await _post(api, first, signed_headers(first))
    await _post(api, second, signed_headers(second))

    connection_id = await _connection_id_for(owner, PAGE_A)
    events = await _events_for_connection(owner, connection_id)
    assert len(events) == 2
    assert {e.meta_event_id for e in events} == {"mid.$one", "mid.$two"}


# ------------------------------------------------- authentication is Meta's only


async def test_webhook_ignores_a_session_cookie(api, db, world, owner):
    """A valid SaaS session is not a substitute for a Meta signature."""
    user = await create_user(owner, email="attacker@dev.local", name="Attacker")
    tenant = await create_tenant(owner, name="Attacker Co", slug="attacker")
    await add_member(owner, tenant_id=tenant, user_id=user, role="OWNER")

    api.cookies.set("qf_session", await issue_session(owner, user_id=user))

    raw = body(leadgen_payload(PAGE_A))
    response = await _post(api, raw, {"Content-Type": "application/json"})

    assert response.status_code == 403


async def test_x_tenant_id_cannot_impersonate_a_tenant(api, db, world, owner):
    """Pointing the webhook at a victim tenant grants nothing."""
    raw = body(leadgen_payload(PAGE_A))
    headers = {**signed_headers(raw), "X-Tenant-ID": str(world["b"])}

    response = await _post(api, raw, headers)

    assert response.status_code == 200
    connection_a = await _connection_id_for(owner, PAGE_A)
    event = (await _events_for_connection(owner, connection_a))[0]
    # Still tenant A: the header is ignored, the Meta source decided.
    assert event.tenant_id == world["a"]


# -------------------------------------------------------------- no side effects


async def test_leadgen_event_creates_no_domain_objects(api, db, world, owner):
    """A leadgen payload is stored verbatim and nothing else is derived from it."""
    payload = leadgen_payload(PAGE_A)
    raw = body(payload)
    await _post(api, raw, signed_headers(raw))

    # Phase 2 has no lead/conversation/message tables at all, so the inbox is the only
    # place this payload could have landed.
    connection_id = await _connection_id_for(owner, PAGE_A)
    stored = await _events_for_connection(owner, connection_id)
    assert len(stored) == 1
    assert stored[0].payload == payload
    assert stored[0].status is MetaWebhookEventStatus.RECEIVED


async def test_messaging_event_creates_no_domain_objects(api, db, world, owner):
    payload = messaging_payload(PAGE_A)
    raw = body(payload)
    await _post(api, raw, signed_headers(raw))

    connection_id = await _connection_id_for(owner, PAGE_A)
    stored = await _events_for_connection(owner, connection_id)
    assert len(stored) == 1
    # The message body is preserved for Phase 3+; no Message or Conversation exists.
    assert stored[0].payload["entry"][0]["messaging"][0]["message"]["text"] == "hello"
