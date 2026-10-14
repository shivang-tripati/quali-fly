"""The Meta Graph client, against a mocked HTTP transport.

The transport is mocked rather than the client, so URL construction, the version prefix,
parameter names and error classification are all exercised for real. The behaviours worth
pinning down are: every request is versioned from configuration, tokens travel in the
query string but never into an exception or a log record, and failures are classified
into "worth retrying" and "not".
"""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.services.meta_graph import (
    MetaApiError,
    MetaAuthError,
    MetaGraphClient,
    MetaInvalidResponseError,
    MetaTransientError,
    MetaUnknownOutcomeError,
)

APP_ID = "1234567890"
APP_SECRET = "app-secret-value"
REDIRECT = "https://app.example.test/api/v1/meta/oauth/callback"
USER_TOKEN = "user-token"
PAGE_TOKEN = "page-token"
VERSION = "v26.0"


def client_with(handler, *, version: str = VERSION) -> MetaGraphClient:
    """A client whose only outbound traffic is `handler`."""
    transport = httpx.MockTransport(handler)
    return MetaGraphClient(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        graph_version=version,
        client=httpx.AsyncClient(transport=transport),
    )


def json_handler(payload, *, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


# -- authorization URL --------------------------------------------------------


def test_the_authorization_url_is_versioned_and_requests_a_code():
    client = client_with(json_handler({}))
    url = client.authorization_url(
        redirect_uri=REDIRECT, state="state-abc", scopes="leads_retrieval,ads_management"
    )

    assert url.startswith("https://www.facebook.com/v26.0/dialog/oauth?")
    assert "client_id=1234567890" in url
    assert "response_type=code" in url
    # Code flow only: an access token in a URL fragment would reach the browser.
    assert "response_type=token" not in url
    assert "state=state-abc" in url
    assert "leads_retrieval" in url


def test_the_authorization_url_uses_the_configured_version():
    client = client_with(json_handler({}), version="v25.0")
    url = client.authorization_url(redirect_uri=REDIRECT, state="s", scopes="x")
    assert "/v25.0/dialog/oauth" in url


def test_redirect_uri_and_state_are_url_encoded():
    client = client_with(json_handler({}))
    url = client.authorization_url(
        redirect_uri="https://app.test/cb?x=1&y=2", state="a b&c", scopes="x"
    )
    # A raw `&` in either value would let an attacker append their own parameters.
    assert "redirect_uri=https%3A%2F%2Fapp.test%2Fcb%3Fx%3D1%26y%3D2" in url
    assert "state=a+b%26c" in url


# -- token exchange -----------------------------------------------------------


async def test_exchange_returns_the_token_and_expiry():
    captured: list[httpx.Request] = []

    def handler(request):
        captured.append(request)
        return httpx.Response(
            200, json={"access_token": USER_TOKEN, "token_type": "bearer", "expires_in": 5184000}
        )

    client = client_with(handler)
    result = await client.exchange_authorization_code(code="the-code", redirect_uri=REDIRECT)

    assert result.access_token == USER_TOKEN
    assert result.expires_in == 5_184_000
    assert result.expires_at_epoch is not None

    url = str(captured[0].url)
    assert url.startswith("https://graph.facebook.com/v26.0/oauth/access_token?")
    assert "code=the-code" in url
    assert f"client_id={APP_ID}" in url
    assert "client_secret=app-secret-value" in url


async def test_exchange_without_a_token_is_an_invalid_response():
    client = client_with(json_handler({"token_type": "bearer"}))
    with pytest.raises(MetaInvalidResponseError, match="no access_token"):
        await client.exchange_authorization_code(code="c", redirect_uri=REDIRECT)


async def test_exchange_fails_closed_when_oauth_is_not_configured():
    client = MetaGraphClient(
        app_id=None, app_secret=None, graph_version="v26.0", client=httpx.AsyncClient()
    )
    with pytest.raises(MetaAuthError, match="not configured"):
        await client.exchange_authorization_code(code="c", redirect_uri=REDIRECT)
    await client.aclose()


# -- authorized pages ---------------------------------------------------------


async def test_pages_are_returned_with_their_page_token():
    client = client_with(
        json_handler(
            {
                "data": [
                    {
                        "id": "111",
                        "name": "Acme",
                        "access_token": PAGE_TOKEN,
                        "tasks": ["ADVERTISE"],
                    }
                ]
            }
        )
    )
    pages = await client.list_authorized_pages(USER_TOKEN)

    assert len(pages) == 1
    assert pages[0].page_id == "111"
    assert pages[0].name == "Acme"
    assert pages[0].access_token == PAGE_TOKEN
    assert pages[0].tasks == ("ADVERTISE",)


async def test_a_page_without_a_token_is_skipped_rather_than_stored():
    """A connection we cannot call the API with is worse than no connection."""
    client = client_with(
        json_handler(
            {"data": [{"id": "111", "name": "NoToken"}, {"id": "222", "access_token": "t"}]}
        )
    )
    pages = await client.list_authorized_pages(USER_TOKEN)
    assert [p.page_id for p in pages] == ["222"]


async def test_no_pages_is_an_empty_list_not_an_error():
    client = client_with(json_handler({"data": []}))
    assert await client.list_authorized_pages(USER_TOKEN) == []


# -- Instagram Business Accounts ----------------------------------------------


async def test_the_instagram_account_is_read_from_the_pages_own_edge():
    """The Page is the address, not just the credential.

    Meta models the account as an edge owned by a Page, so the request must be
    `/{page-id}?fields=instagram_business_account{...}` with the *page* token. Asking
    `me/accounts` for it, or using a user token here, is how this goes wrong in a way that
    only shows up against the real Graph.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "instagram_business_account": {
                    "id": "9001",
                    "username": "acmecars",
                    "name": "Acme Cars",
                }
            },
        )

    client = client_with(handler)
    accounts = await client.list_instagram_business_accounts(
        page_id="111", page_access_token=PAGE_TOKEN
    )
    await client.aclose()

    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == f"/{VERSION}/{111}", request.url.path
    # `request.url.query` is bytes, and str() on that produces a b'...' literal, so go via
    # urlparse the way the other URL assertions in this file do.
    query = parse_qs(urlparse(str(request.url)).query)
    assert query.get("access_token") == [PAGE_TOKEN], str(request.url)
    assert query.get("fields") == ["instagram_business_account{id,username,name}"], str(request.url)
    assert len(accounts) == 1
    assert accounts[0].business_id == "9001"
    assert accounts[0].username == "acmecars"
    assert accounts[0].name == "Acme Cars"


async def test_a_page_with_no_instagram_account_is_an_empty_list_not_an_error():
    """`instagram_business_account: null` is the answer for most Pages, not a failure."""
    client = client_with(json_handler({"id": "111", "instagram_business_account": None}))

    assert (
        await client.list_instagram_business_accounts(page_id="111", page_access_token=PAGE_TOKEN)
        == []
    )
    await client.aclose()


async def test_an_instagram_edge_with_no_id_is_not_offered():
    """An account we could neither route to nor match on is not selectable."""
    client = client_with(json_handler({"instagram_business_account": {"username": "ghost"}}))

    assert (
        await client.list_instagram_business_accounts(page_id="111", page_access_token=PAGE_TOKEN)
        == []
    )
    await client.aclose()


async def test_an_instagram_edge_of_the_wrong_shape_is_an_invalid_response():
    client = client_with(json_handler({"instagram_business_account": ["9001"]}))

    with pytest.raises(MetaInvalidResponseError):
        await client.list_instagram_business_accounts(page_id="111", page_access_token=PAGE_TOKEN)
    await client.aclose()


async def test_a_revoked_token_on_discovery_is_an_auth_error():
    """Discovery spends the tenant's credential, so an expired one must be distinguishable."""
    client = client_with(json_handler({"error": {"message": "Invalid OAuth token", "code": 190}}))

    with pytest.raises(MetaAuthError):
        await client.list_instagram_business_accounts(page_id="111", page_access_token=PAGE_TOKEN)
    await client.aclose()


async def test_a_missing_instagram_scope_reports_as_a_permanent_auth_error():
    """No `instagram_basic` on the token is a permission answer, not a transient one."""
    client = client_with(
        json_handler(
            {
                "error": {
                    "message": "(#200) Requires permission",
                    "code": 200,
                    "error_subcode": 33,
                }
            }
        )
    )

    with pytest.raises(MetaAuthError):
        await client.list_instagram_business_accounts(page_id="111", page_access_token=PAGE_TOKEN)
    await client.aclose()


# -- lead retrieval -----------------------------------------------------------


async def test_get_lead_requests_the_documented_fields():
    captured: list[httpx.Request] = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"id": "999", "field_data": []})

    client = client_with(handler)
    await client.get_lead(access_token=PAGE_TOKEN, leadgen_id="999")

    url = str(captured[0].url)
    assert url.startswith("https://graph.facebook.com/v26.0/999?")
    query = parse_qs(urlparse(url).query)
    # Requested explicitly rather than relying on a default that is not a contract.
    assert set(query["fields"][0].split(",")) == {
        "created_time",
        "ad_id",
        "form_id",
        "field_data",
        "id",
    }
    assert query["access_token"] == [PAGE_TOKEN]


# -- error classification -----------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (190, MetaAuthError),
        (102, MetaAuthError),
        (463, MetaAuthError),
        (4, MetaTransientError),
        (17, MetaTransientError),
        (1, MetaTransientError),
    ],
)
async def test_graph_error_codes_are_classified(code, expected):
    client = client_with(
        json_handler({"error": {"code": code, "type": "OAuthException"}}, status=400)
    )
    with pytest.raises(expected):
        await client.get_lead(access_token="t", leadgen_id="1")


async def test_an_unknown_error_code_is_permanent_not_retryable():
    client = client_with(json_handler({"error": {"code": 100}}, status=400))
    with pytest.raises(MetaApiError) as caught:
        await client.get_lead(access_token="t", leadgen_id="1")
    assert not caught.value.retryable
    assert not isinstance(caught.value, (MetaAuthError, MetaTransientError))


async def test_a_server_error_is_retryable_even_without_a_graph_code():
    client = client_with(json_handler({}, status=503))
    with pytest.raises(MetaTransientError):
        await client.get_lead(access_token="t", leadgen_id="1")


async def test_a_timeout_is_retryable():
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    client = client_with(handler)
    with pytest.raises(MetaTransientError, match="timed out"):
        await client.get_lead(access_token="t", leadgen_id="1")


async def test_a_transport_error_is_retryable_and_names_no_url():
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    client = client_with(handler)
    with pytest.raises(MetaTransientError, match="ConnectError"):
        await client.get_lead(access_token=PAGE_TOKEN, leadgen_id="1")


async def test_a_non_json_error_response_still_raises_rather_than_raising_on_parse():
    client = client_with(lambda request: httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(MetaTransientError):
        await client.get_lead(access_token="t", leadgen_id="1")


async def test_a_timeout_on_a_read_is_retryable_but_on_a_send_it_is_ambiguous():
    """The distinction that stops a retry becoming a duplicate message.

    Both raise on a timeout, but they mean opposite things and must not share a type. A read
    that timed out changed nothing, so repeating it is free. A send that timed out may have
    delivered a message to a real person, so repeating it may not be.

    Meta's Send API takes no idempotency key, so this is the whole of what can be guaranteed
    locally: the caller's job is to know which of the two it is looking at.
    """

    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    client = client_with(handler)

    with pytest.raises(MetaTransientError):
        await client.get_lead(access_token="t", leadgen_id="1")

    with pytest.raises(MetaUnknownOutcomeError):
        await client.send_message(
            access_token=PAGE_TOKEN,
            page_id="1000000000000001",
            recipient_id="10",
            text="hello",
        )


# -- secret hygiene -----------------------------------------------------------


async def test_no_token_appears_in_our_logs_or_exception_messages(caplog):
    """The failure path is where tokens leak, so this pins it down.

    Scoped to *our* loggers on purpose. httpx itself writes the full request URL - token
    included - at DEBUG; that is handled by clamping third-party loggers in
    `configure_logging`, which `test_third_party_http_loggers_are_clamped` covers.
    """
    import logging

    caplog.set_level(logging.DEBUG, logger="app")
    client = client_with(json_handler({"error": {"code": 190, "message": "Invalid token"}}))
    with pytest.raises(MetaAuthError) as caught:
        await client.get_lead(access_token="SUPER-SECRET-PAGE-TOKEN", leadgen_id="1")

    assert "SUPER-SECRET-PAGE-TOKEN" not in str(caught.value)
    assert "SUPER-SECRET-PAGE-TOKEN" not in caplog.text
    assert APP_SECRET not in caplog.text


def test_third_party_http_loggers_are_clamped():
    """A `LOG_LEVEL=DEBUG` must not be enough to start logging Meta tokens.

    The Graph API takes the access token as a query parameter and httpx logs request
    URLs verbatim at DEBUG, so without this clamp a debugging session would write every
    tenant's token into the log pipeline.
    """
    import logging

    from app.core.config import Settings
    from app.core.logging import configure_logging

    configure_logging(Settings(app_env="test", log_level="DEBUG", database_url="postgresql://x"))
    try:
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        logging.getLogger().handlers = []
        logging.getLogger("httpx").setLevel(logging.NOTSET)
        logging.getLogger("httpcore").setLevel(logging.NOTSET)


async def test_the_client_does_not_follow_redirects():
    """A redirect would replay our query string, token and all, to another host."""
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.test/steal"})

    client = client_with(handler)
    try:
        await client.get_lead(access_token="t", leadgen_id="1")
    except MetaApiError:
        pass
    assert all(str(r.url).startswith("https://graph.facebook.com/") for r in seen)
