"""Lead list filtering, search and pagination.

Small, but these are the queries behind the leads screen, and each one has a specific way of
being wrong that only shows up with real rows: a `total` that ignores the filters makes
every page look like the last one, a search that only covers the name misses the person
who knows a lead by their email, and a `%` in the search term must not turn into a wildcard.
"""

import uuid
from datetime import UTC, datetime

from app.models.lead import LeadStatus
from tests.factories import create_tenant, create_user, issue_session
from tests.follow_up_fixtures import make_conversation

LEADS = "/api/v1/leads"


async def _tenant(owner, *, name="Acme"):
    tenant = await create_tenant(owner, name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    user = await create_user(owner, email=f"u-{uuid.uuid4().hex[:8]}@{name.lower()}.test", name="U")
    from app.models.membership import Role
    from tests.factories import add_member

    await add_member(owner, tenant_id=tenant, user_id=user, role=Role.OWNER)
    headers = {
        "X-Tenant-ID": str(tenant),
        "Cookie": f"qf_session={await issue_session(owner, user_id=user)}",
    }
    return tenant, headers


async def _add_leads(
    owner, tenant, count, *, status=LeadStatus.NEW, name_prefix="Lead", phone_start=555
):
    """Leads with distinct names and phones.

    `phone_start` exists because two groups sharing a phone range would make a phone search
    match both, and the test would end up asserting that a bug is a feature.
    """
    import uuid as _uuid

    from app.db.tenant_context import set_current_tenant
    from app.models.lead import Lead, LeadSource

    ids = []
    async with owner() as session, session.begin():
        await set_current_tenant(session, tenant)
        for i in range(count):
            lead = Lead(
                tenant_id=tenant,
                meta_leadgen_id=f"lg.{_uuid.uuid4().hex[:12]}",
                source=LeadSource.META_LEAD_ADS,
                status=status,
                name=f"{name_prefix} {i}",
                email=f"{name_prefix.lower()}{i}@example.test",
                phone=f"+1{phone_start}{i:07d}",
            )
            session.add(lead)
            await session.flush()
            ids.append(lead.id)
    return ids


async def test_a_lead_carries_its_qualification(db, api, owner):
    """The lead screen renders bars; it must not have to invent the numbers."""
    from app.db.tenant_context import set_current_tenant
    from app.models.lead import Lead

    tenant, headers = await _tenant(owner)
    lead_id = (await _add_leads(owner, tenant, 1))[0]
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant)
        lead = await session.get(Lead, lead_id)
        lead.qualification = {
            "budget": 21,
            "timeline": 18,
            "fit": 23,
            "authority": 19,
            "total": 81,
            "high_intent": True,
        }

    response = await api.get(f"{LEADS}/{lead_id}", headers=headers)

    assert response.status_code == 200
    assert response.json()["qualification"]["total"] == 81
    assert response.json()["qualification"]["high_intent"] is True


async def test_an_unscored_lead_reports_none_not_zeroes(db, api, owner):
    """`None` and "four zeroes" look identical on a bar chart and mean opposite things."""
    tenant, headers = await _tenant(owner)
    lead_id = (await _add_leads(owner, tenant, 1))[0]

    response = await api.get(f"{LEADS}/{lead_id}", headers=headers)

    assert response.json()["qualification"] is None


async def test_listing_reports_a_total_alongside_the_page(db, api, owner):
    tenant, headers = await _tenant(owner)
    await _add_leads(owner, tenant, 7)

    response = await api.get(f"{LEADS}?limit=3", headers=headers)

    body = response.json()
    assert body["count"] == 3
    assert body["total"] == 7, "without a total every page looks like the last one"


async def test_a_status_filter_narrows_both_the_page_and_the_total(db, api, owner):
    tenant, headers = await _tenant(owner)
    await _add_leads(owner, tenant, 3, status=LeadStatus.NEW, name_prefix="New")
    await _add_leads(owner, tenant, 2, status=LeadStatus.QUALIFIED, name_prefix="Hot")

    response = await api.get(f"{LEADS}?status=QUALIFIED", headers=headers)

    body = response.json()
    assert body["total"] == 2
    assert body["count"] == 2
    assert all(item["status"] == "QUALIFIED" for item in body["items"])


async def test_pagination_walks_the_whole_set_without_repeats(db, api, owner):
    tenant, headers = await _tenant(owner)
    await _add_leads(owner, tenant, 5)

    first = (await api.get(f"{LEADS}?limit=2&offset=0", headers=headers)).json()
    second = (await api.get(f"{LEADS}?limit=2&offset=2", headers=headers)).json()
    third = (await api.get(f"{LEADS}?limit=2&offset=4", headers=headers)).json()

    ids = [i["id"] for page in (first, second, third) for i in page["items"]]
    assert len(ids) == 5
    assert len(set(ids)) == 5, "pages must not overlap"
    assert first["total"] == second["total"] == third["total"] == 5


async def test_search_matches_name_email_and_phone(db, api, owner):
    tenant, headers = await _tenant(owner)
    await _add_leads(owner, tenant, 3, name_prefix="Priya", phone_start=555)
    await _add_leads(owner, tenant, 3, name_prefix="Rahul", phone_start=777)

    by_name = await api.get(f"{LEADS}?q=priya", headers=headers)
    by_email = await api.get(f"{LEADS}?q=rahul1@example.test", headers=headers)
    # `+` is encoded because a bare `+` in a query string decodes to a space.
    by_phone = await api.get(f"{LEADS}?q=%2B15550000001", headers=headers)

    assert by_name.json()["total"] == 3, "a person knows a lead by name"
    assert by_email.json()["total"] == 1, "or by the email they typed on the form"
    assert by_phone.json()["total"] == 1, "or by phone"


async def test_search_combines_with_the_status_filter(db, api, owner):
    tenant, headers = await _tenant(owner)
    await _add_leads(owner, tenant, 2, status=LeadStatus.QUALIFIED, name_prefix="Priya")
    await _add_leads(owner, tenant, 3, status=LeadStatus.NEW, name_prefix="Priya")

    response = await api.get(f"{LEADS}?q=Priya&status=QUALIFIED", headers=headers)

    assert response.json()["total"] == 2


async def test_a_wildcard_in_the_search_is_matched_literally(db, api, owner):
    """A user-supplied `%` must not become a LIKE wildcard.

    Without the escaping, searching for `%` returns every lead in the tenant, which is both
    useless and a slow query on a large table.
    """
    tenant, headers = await _tenant(owner)
    await _add_leads(owner, tenant, 3, name_prefix="Normal")

    response = await api.get(f"{LEADS}?q=%25", headers=headers)

    assert response.json()["total"] == 0


async def test_an_empty_search_is_ignored_rather_than_matching_nothing(db, api, owner):
    tenant, headers = await _tenant(owner)
    await _add_leads(owner, tenant, 2)

    response = await api.get(f"{LEADS}?q=%20%20", headers=headers)

    assert response.json()["total"] == 2, "a blank search box must not hide everything"


async def test_search_and_filters_are_tenant_scoped(db, api, owner):
    acme, acme_headers = await _tenant(owner, name="Acme")
    globex, globex_headers = await _tenant(owner, name="Globex")
    await _add_leads(owner, acme, 2, name_prefix="AcmePerson")
    await _add_leads(owner, globex, 3, name_prefix="AcmePerson")

    acme_body = (await api.get(f"{LEADS}?q=AcmePerson", headers=acme_headers)).json()
    globex_body = (await api.get(f"{LEADS}?q=AcmePerson", headers=globex_headers)).json()

    assert acme_body["total"] == 2
    assert globex_body["total"] == 3
    assert not ({i["id"] for i in acme_body["items"]} & {i["id"] for i in globex_body["items"]})


async def test_conversations_can_be_filtered_by_lead(db, api, owner):
    """So a lead's detail page opens the right thread in one request."""
    from app.db.tenant_context import set_current_tenant
    from app.models.conversation import Channel
    from app.services.conversations import ConversationEngine
    from app.services.meta_messaging import InboundMessage
    from tests.fakes import FakeGraph
    from tests.handoff_fixtures import ho_settings

    tenant, headers = await _tenant(owner)
    with_lead = await make_conversation(owner, tenant)
    without_lead = await make_conversation(owner, tenant, with_lead=False)
    T = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)

    for world in (with_lead, without_lead):
        async with db() as session, session.begin():
            await set_current_tenant(session, tenant)
            engine = ConversationEngine(session, tenant, graph=FakeGraph(), settings=ho_settings())
            await engine.ingest_inbound(
                meta_connection_id=world["connection_id"],
                channel=Channel.MESSENGER,
                messages=[
                    InboundMessage(
                        participant_id=world["participant_id"],
                        meta_message_id=f"mid.{world['participant_id']}",
                        source_id=None,
                        message_type="text",
                        text="hello",
                        sent_at=T,
                    )
                ],
            )

    response = await api.get(
        f"/api/v1/conversations?lead_id={with_lead['lead_id']}", headers=headers
    )

    assert response.status_code == 200
    ids = [c["id"] for c in response.json()["items"]]
    assert len(ids) == 1
    assert without_lead["conversation_id"] not in ids
