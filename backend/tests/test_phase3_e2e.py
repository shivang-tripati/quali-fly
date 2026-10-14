"""The whole vertical slice, through the real HTTP surface.

    Meta webhook -> signature check -> durable event -> ACK
      -> worker claims -> Graph API (faked) -> Lead
      -> tenant reads it back over the API

Everything is the real implementation except the outbound Meta HTTP call, which cannot
be exercised without a live Meta app. The database is real, so the deduplication, RLS and
`SKIP LOCKED` behaviour being relied on is genuine.
"""

import uuid
from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.core.crypto import encrypt
from app.models.membership import Role
from app.models.meta_connection import MetaConnection
from app.repositories import meta_webhook_events as event_repo
from app.services.meta_processor import MetaWebhookEventProcessor
from tests.conftest import TEST_META_TOKEN_KEY
from tests.factories import add_member, create_tenant, create_user, issue_session
from tests.fakes import PAGE_TOKEN, transient_error
from tests.meta_fixtures import (
    LEADGEN_A,
    PAGE_A,
    body,
    lead_graph_payload,
    leadgen_payload,
    signature_for,
    signed_headers,
)

API = "/api/v1"
WEBHOOK = f"{API}/webhooks/meta"


@pytest.fixture
async def tenant(owner):
    """One tenant with an admin session and an OAuth-authorized Meta connection."""
    tenant_id = await create_tenant(owner, name="Acme", slug="acme")
    admin = await create_user(owner, email="admin@acme.test", name="Ada Admin")
    await add_member(owner, tenant_id=tenant_id, user_id=admin, role=Role.OWNER)
    token = await issue_session(owner, user_id=admin)

    async with owner() as session, session.begin():
        connection = MetaConnection(
            tenant_id=tenant_id,
            page_id=PAGE_A,
            app_id="1234567890",
            access_token_encrypted=encrypt(PAGE_TOKEN, key=TEST_META_TOKEN_KEY),
            scopes=["leads_retrieval", "ads_management"],
            connected_at=datetime.now(UTC),
        )
        session.add(connection)
        await session.flush()
        connection_id = connection.id

    return {
        "tenant_id": tenant_id,
        "user_id": admin,
        "token": token,
        "connection_id": connection_id,
        "headers": {"X-Tenant-ID": str(tenant_id), "Cookie": f"qf_session={token}"},
    }


async def _deliver(api, payload) -> dict:
    """POST a correctly signed webhook, exactly as Meta would."""
    raw = body(payload)
    response = await api.post(WEBHOOK, content=raw, headers=signed_headers(raw))
    assert response.status_code == 200, response.text
    return response.json()


async def _drain(db, tenant_id, fake_graph, limit: int = 5) -> int:
    """Run a worker until there is nothing left to claim."""
    processed = 0
    for _ in range(limit):
        async with db() as session, session.begin():
            processor = MetaWebhookEventProcessor(
                session, tenant_id, graph=fake_graph, settings=get_settings()
            )
            event = await processor.claim_next()
        if event is None:
            break
        async with db() as session, session.begin():
            processor = MetaWebhookEventProcessor(
                session, tenant_id, graph=fake_graph, settings=get_settings()
            )
            await processor.handle(event)
        processed += 1
    return processed


async def test_a_meta_lead_becomes_a_tenant_lead_through_the_real_api(api, db, tenant, fake_graph):
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload(LEADGEN_A))

    # 1. Meta delivers the webhook. The ACK must not wait on the Graph API.
    ack = await _deliver(api, leadgen_payload(PAGE_A, LEADGEN_A))
    assert ack["status"] == "ok"
    assert ack["duplicate"] is False
    assert fake_graph.lead_calls == [], "the ACK must not call Meta"

    # 2. A worker drains the queue.
    assert await _drain(db, tenant["tenant_id"], fake_graph) == 1
    assert fake_graph.leadgen_ids_seen == [LEADGEN_A]
    assert fake_graph.tokens_seen_by_lead_retrieval == {PAGE_TOKEN}

    # 3. The tenant reads the lead back over the API.
    listed = await api.get(f"{API}/leads", headers=tenant["headers"])
    assert listed.status_code == 200
    body = listed.json()
    assert body["count"] == 1
    lead = body["items"][0]
    assert lead["meta_leadgen_id"] == LEADGEN_A
    assert lead["name"] == "Ada Lovelace"
    assert lead["email"] == "ada@ex.test"
    assert lead["status"] == "NEW"
    assert lead["source"] == "META_LEAD_ADS"
    assert lead["field_data"]["car_make"] == "Honda"

    # 4. And it is the same row the event produced, not a second one.
    async with db() as session, session.begin():
        from app.db.tenant_context import set_current_tenant

        await set_current_tenant(session, tenant["tenant_id"])
        event = await event_repo.get(session, uuid.UUID(ack["event_id"]))
    assert event.status.value == "PROCESSED"
    assert event.error_message is None


async def test_replaying_the_identical_webhook_creates_no_second_lead(api, db, tenant, fake_graph):
    """The exact byte-identical delivery Meta sends when it retries."""
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload(LEADGEN_A))
    payload = leadgen_payload(PAGE_A, LEADGEN_A)

    first = await _deliver(api, payload)
    await _drain(db, tenant["tenant_id"], fake_graph)

    second = await _deliver(api, payload)
    assert second["duplicate"] is True
    assert second["event_id"] == first["event_id"]

    # The duplicate is not even re-queued, so no second Graph call and no second lead.
    assert await _drain(db, tenant["tenant_id"], fake_graph) == 0
    assert fake_graph.leadgen_ids_seen == [LEADGEN_A]

    listed = await api.get(f"{API}/leads", headers=tenant["headers"])
    assert listed.json()["count"] == 1


async def test_two_deliveries_of_the_same_lead_still_produce_one_lead(api, db, tenant, fake_graph):
    """Same lead, different deliveries - the case a per-delivery dedupe key cannot catch."""
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload(LEADGEN_A))

    first = leadgen_payload(PAGE_A, LEADGEN_A)
    await _deliver(api, first)
    await _drain(db, tenant["tenant_id"], fake_graph)

    second = leadgen_payload(PAGE_A, LEADGEN_A)
    second["entry"][0]["time"] = 1730000555  # a genuinely separate delivery
    await _deliver(api, second)
    await _drain(db, tenant["tenant_id"], fake_graph)

    assert fake_graph.leadgen_ids_seen == [LEADGEN_A, LEADGEN_A], "Meta was asked twice"
    listed = await api.get(f"{API}/leads", headers=tenant["headers"])
    assert listed.json()["count"] == 1, "the unique constraint collapsed the duplicates"


async def test_a_graph_outage_leaves_the_lead_recoverable(api, db, tenant, fake_graph):
    """No corrupt lead, no consumed event, and a retry that works."""
    fake_graph.program_lead_error(LEADGEN_A, transient_error())

    ack = await _deliver(api, leadgen_payload(PAGE_A, LEADGEN_A))
    await _drain(db, tenant["tenant_id"], fake_graph)

    listed = await api.get(f"{API}/leads", headers=tenant["headers"])
    assert listed.json()["count"] == 0

    async with db() as session, session.begin():
        from app.db.tenant_context import set_current_tenant

        await set_current_tenant(session, tenant["tenant_id"])
        event = await event_repo.get(session, uuid.UUID(ack["event_id"]))
        assert event.status.value == "FAILED"
        assert event.error_message.startswith("meta_api_unavailable:")
        assert await event_repo.requeue_failed(session, event.id, max_attempts=5) is True

    # Meta recovers.
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload(LEADGEN_A))
    await _drain(db, tenant["tenant_id"], fake_graph)

    listed = await api.get(f"{API}/leads", headers=tenant["headers"])
    assert listed.json()["count"] == 1


async def test_the_webhook_ack_never_touches_the_graph_api(api, db, tenant, fake_graph):
    """A slow Meta must not be able to make Meta's own delivery time out.

    Proven deterministically rather than by timing: after the ACK, no Graph call has
    happened and the event is still waiting as RECEIVED. The work is the worker's.
    """
    fake_graph.program_lead(LEADGEN_A, lead_graph_payload(LEADGEN_A))
    ack = await _deliver(api, leadgen_payload(PAGE_A, LEADGEN_A))

    assert fake_graph.lead_calls == []

    async with db() as session, session.begin():
        from app.db.tenant_context import set_current_tenant

        await set_current_tenant(session, tenant["tenant_id"])
        event = await event_repo.get(session, uuid.UUID(ack["event_id"]))
    assert event.status.value == "RECEIVED"
    assert event.processed_at is None


async def test_an_unsigned_delivery_never_creates_a_lead(api, db, tenant, fake_graph):
    raw = body(leadgen_payload(PAGE_A, LEADGEN_A))
    response = await api.post(
        WEBHOOK,
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "META_WEBHOOK_SIGNATURE_INVALID"

    assert await _drain(db, tenant["tenant_id"], fake_graph) == 0
    listed = await api.get(f"{API}/leads", headers=tenant["headers"])
    assert listed.json()["count"] == 0


async def test_a_tampered_body_after_signing_is_rejected(api, db, tenant, fake_graph):
    """Phase 2's raw-body guarantee must still hold now that leads exist."""
    raw = body(leadgen_payload(PAGE_A, LEADGEN_A))
    signature = signature_for(raw)
    tampered = raw[:-1] + b" "  # one extra byte, same length class

    response = await api.post(
        WEBHOOK,
        content=tampered,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )
    assert response.status_code == 403

    assert await _drain(db, tenant["tenant_id"], fake_graph) == 0
    listed = await api.get(f"{API}/leads", headers=tenant["headers"])
    assert listed.json()["count"] == 0
