"""Outbound sends against a mocked transport.

The transport is mocked rather than the client, so the URL, the form encoding and the
documented `recipient` / `messaging_type` / `message` object literals are all exercised
for real. Those three are sent as *JSON object literals in a form body* - the same way
the curl examples in Meta's docs do it - and expanding them into nested form fields
would not be the documented request.
"""

from urllib.parse import parse_qs

import httpx
import pytest

from app.services.meta_graph import (
    MetaApiError,
    MetaAuthError,
    MetaGraphClient,
    MetaInvalidResponseError,
    MetaMessageWindowClosed,
    MetaTransientError,
    MetaUnknownOutcomeError,
)

PAGE_ID = "1000000000000001"
PSID = "10083363554430212"
PAGE_TOKEN = "page-token-value"
CLIENT_VERSION = "v26.0"


def client_with(handler, *, version: str = CLIENT_VERSION) -> MetaGraphClient:
    return MetaGraphClient(
        app_id="123",
        app_secret="secret",
        graph_version=version,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def ok_send(message_id: str = "m_ag-sent-1"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"recipient_id": PSID, "message_id": message_id})

    return handler


def graph_error(code: int, status: int = 400):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": "no"}})

    return handler


async def test_a_send_posts_the_documented_form_to_the_page_messages_edge():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"recipient_id": PSID, "message_id": "m_1"})

    client = client_with(handler)
    result = await client.send_message(
        access_token=PAGE_TOKEN, page_id=PAGE_ID, recipient_id=PSID, text="hello"
    )

    assert result == "m_1"
    request = seen[0]
    assert request.method == "POST"
    # Instagram uses this same endpoint; only the recipient id differs.
    url = str(request.url)
    assert url == f"https://graph.facebook.com/{CLIENT_VERSION}/{PAGE_ID}/messages"
    # The token travels in the body, not the query string, so it cannot end up in an
    # intermediary's access log the way the lead-retrieval token would.
    assert "access_token" not in url

    form = parse_qs(request.content.decode())
    assert form["access_token"] == [PAGE_TOKEN]
    assert form["recipient"] == [f'{{"id": "{PSID}"}}']
    assert form["messaging_type"] == ["RESPONSE"]
    assert form["message"] == ['{"text": "hello"}']


async def test_the_configured_version_is_used():
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"message_id": "m_1"})

    client = client_with(handler, version="v25.0")
    await client.send_message(
        access_token=PAGE_TOKEN, page_id=PAGE_ID, recipient_id=PSID, text="hi"
    )
    assert "/v25.0/" in seen[0]


async def test_a_send_without_a_message_id_is_an_invalid_response():
    client = client_with(lambda r: httpx.Response(200, json={"recipient_id": PSID}))
    with pytest.raises(MetaInvalidResponseError, match="no message_id"):
        await client.send_message(
            access_token=PAGE_TOKEN, page_id=PAGE_ID, recipient_id=PSID, text="hi"
        )


async def test_sending_without_a_page_id_fails_locally():
    """An Instagram-only connection has no page id, and therefore no endpoint to call."""
    client = client_with(ok_send())
    with pytest.raises(MetaInvalidResponseError, match="no page id"):
        await client.send_message(access_token=PAGE_TOKEN, page_id="", recipient_id=PSID, text="hi")


# -- documented Send API failures ---------------------------------------------


async def test_a_closed_messaging_window_is_its_own_error_type():
    """Not transient, not auth: it needs a product decision no retry can make."""
    client = client_with(graph_error(1545041))
    with pytest.raises(MetaMessageWindowClosed):
        await client.send_message(
            access_token=PAGE_TOKEN, page_id=PAGE_ID, recipient_id=PSID, text="hi"
        )


async def test_an_unreachable_recipient_is_permanent():
    """Blocked or deactivated: 551. Retrying is pointless."""
    client = client_with(graph_error(551))
    with pytest.raises(MetaAuthError) as caught:
        await client.send_message(
            access_token=PAGE_TOKEN, page_id=PAGE_ID, recipient_id=PSID, text="hi"
        )
    assert "not available" in str(caught.value)


async def test_a_revoked_token_is_an_auth_error():
    client = client_with(graph_error(190))
    with pytest.raises(MetaAuthError):
        await client.send_message(
            access_token=PAGE_TOKEN, page_id=PAGE_ID, recipient_id=PSID, text="hi"
        )


async def test_a_rate_limit_is_transient():
    client = client_with(graph_error(613))
    with pytest.raises(MetaTransientError) as caught:
        await client.send_message(
            access_token=PAGE_TOKEN, page_id=PAGE_ID, recipient_id=PSID, text="hi"
        )
    assert caught.value.retryable is True


async def test_a_bad_parameter_is_permanent():
    client = client_with(graph_error(100))
    with pytest.raises(MetaApiError) as caught:
        await client.send_message(
            access_token=PAGE_TOKEN, page_id=PAGE_ID, recipient_id=PSID, text="hi"
        )
    assert caught.value.retryable is False


async def test_a_server_error_is_transient():
    client = client_with(lambda r: httpx.Response(503, json={}))
    with pytest.raises(MetaTransientError):
        await client.send_message(
            access_token=PAGE_TOKEN, page_id=PAGE_ID, recipient_id=PSID, text="hi"
        )


# -- secret hygiene -----------------------------------------------------------


async def test_a_failed_send_never_leaks_the_token(caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="app")
    client = client_with(graph_error(190))
    with pytest.raises(MetaAuthError) as caught:
        await client.send_message(
            access_token="SUPER-SECRET-PAGE-TOKEN", page_id=PAGE_ID, recipient_id=PSID, text="hi"
        )

    assert "SUPER-SECRET-PAGE-TOKEN" not in str(caught.value)
    assert "SUPER-SECRET-PAGE-TOKEN" not in caplog.text


async def test_a_timeout_during_a_send_is_an_unknown_outcome_not_a_transient():
    """A send that timed out is not a failure - it is an absence of information.

    Meta's Send API takes no idempotency key, so a request that was in flight when the
    connection dropped may already have delivered a message to a real person. Classifying
    that as a retryable transient failure is how a retry becomes a duplicate send, so it
    gets its own type with `retryable = False`.

    The same transport error on a *read* is still a plain transient failure: nothing was
    delivered, so repeating it is free. See the read/send split test in
    `test_meta_graph_client.py`.
    """

    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    client = client_with(handler)
    with pytest.raises(MetaUnknownOutcomeError, match="timed out"):
        await client.send_message(
            access_token=PAGE_TOKEN, page_id=PAGE_ID, recipient_id=PSID, text="hi"
        )


async def test_sends_do_not_follow_redirects():
    """A redirect would replay the form, token included, to another host."""
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.test/steal"})

    client = client_with(handler)
    with pytest.raises((MetaApiError, MetaTransientError)):
        await client.send_message(
            access_token=PAGE_TOKEN, page_id=PAGE_ID, recipient_id=PSID, text="hi"
        )
    assert all(str(r.url).startswith("https://graph.facebook.com/") for r in seen)
