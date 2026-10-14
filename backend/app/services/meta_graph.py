"""The only place the application talks to the Meta Graph API.

Kept deliberately small and isolated so that the rest of the codebase never sees a URL,
a token, or a Graph error shape. Three operations, all of them documented and versioned:

    exchange_authorization_code   GET /oauth/access_token
    list_authorized_pages         GET /{user-id}/accounts
    get_lead                      GET /{leadgen-id}

Endpoints, scopes and payload shapes were verified against Meta's developer
documentation on 2026-09-28 (Graph API v26.0). The version is configuration, not a
literal scattered through this file, so upgrading is a one-line deployment change.

Two invariants, because this is the only code holding a Meta credential:

  * a token is passed as a query parameter (how the Graph API accepts it) and is
    never placed in a log record, an exception message, or a return value that an
    endpoint might echo
  * failures are classified, not string-matched downstream. `MetaAuthError` means our
    credential is wrong and retrying will not help; `MetaTransientError` means the call
    is worth repeating. The lead processor needs that distinction to decide whether an
    event stays retryable.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from app.services.channels import WHATSAPP_PRODUCT

log = logging.getLogger("app.meta.graph")

# A Graph lead is a form submission; anything vastly larger is a mistake or an attack.
_MAX_RESPONSE_BYTES = 1024 * 1024
_GRAPH_HOST = "https://graph.facebook.com"
_DIALOG_HOST = "https://www.facebook.com"

# Error codes Meta documents as retryable. 1/2/4/17/32/341 are rate-limit / temporary
# failures; 190 is an invalid or revoked token and is never worth retrying.
_TRANSIENT_ERROR_CODES = frozenset({1, 2, 4, 17, 32, 341, 613})
_AUTH_ERROR_CODES = frozenset({190, 102, 200, 463, 467})
# Send API only: the standard messaging window is closed. Meta's own table documents
# 1545041 for this, and 10 with subcode 2534022 in the "sent outside allowed window"
# family.
_MESSAGE_WINDOW_CODES = frozenset({1545041})
# Send API only: the person blocked the page, deactivated, or is otherwise unreachable.
_RECIPIENT_UNAVAILABLE_CODES = frozenset({551})


class MetaApiError(Exception):
    """Base for every failure talking to Meta. Carries no token material."""

    #: Whether repeating the identical request could plausibly succeed.
    retryable = False

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class MetaAuthError(MetaApiError):
    """The app secret or access token was rejected. Not retryable."""


class MetaTransientError(MetaApiError):
    """Rate limited or temporarily unavailable, and the request was *not* accepted.

    "Retryable" is the honest reading for the two situations that produce it: a 429/5xx
    from Meta, and a Meta-documented transient error code. In both cases Meta returned a
    response saying it did not take the request, so repeating it is safe.

    That is deliberately narrower than "the call did not succeed". A *timeout* is a different
    failure with the opposite meaning and its own type, `MetaUnknownOutcomeError`, because
    a request that timed out may well have been accepted. Conflating the two is how a
    retry turns into a duplicate message to a real customer.
    """

    retryable = True


class MetaUnknownOutcomeError(MetaApiError):
    """The request was sent and the response was never seen. Delivery is unknown.

    A timeout, a reset connection, or a response that never arrived. The message may have
    been accepted, in which case resending duplicates it; it may not have been, in which
    case not resending loses a follow-up. There is no way to find out from the client side,
    so callers must treat this as terminal and reconcile from the durable record rather than
    guess. `retryable` is False for that reason and not because the transport is broken.
    """

    retryable = False


class MetaInvalidResponseError(MetaApiError):
    """A 2xx response that does not contain what the documented shape promises."""


class MetaMessageWindowClosed(MetaApiError):
    """Meta refused the send because the 24-hour standard messaging window has closed.

    Its own type because this is not a bug and not a transient fault: it needs a product
    decision (a message tag, or a one-time notification) that no automatic retry can make
    for us. Phase 7 owns follow-up messaging and is where that decision belongs.
    """


@dataclass(frozen=True, slots=True)
class ExchangeResult:
    """A user access token from the authorization-code exchange.

    The plaintext token is held in memory for the length of the OAuth request and is
    never logged. `expires_in` is often absent for long-lived tokens, hence None.
    """

    access_token: str = field(repr=False)
    token_type: str | None
    expires_in: int | None

    @property
    def expires_at_epoch(self) -> int | None:
        import time

        return int(time.time()) + self.expires_in if self.expires_in else None


@dataclass(frozen=True, slots=True)
class AuthorizedPage:
    """A Page the authorizing user granted us access to, with its page access token.

    Page tokens are what lead retrieval needs: they are scoped to the Page the webhook
    is routed by, so a leadgen_id from Page A cannot be read with Page B's token.
    """

    page_id: str
    name: str | None
    access_token: str = field(repr=False)
    tasks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InstagramBusinessAccount:
    """An Instagram Business Account linked to a Page, as Meta reports it.

    `business_id` is the `instagram_business_account` edge id, which is what Meta puts in
    `entry[].id` on an `object: "instagram"` webhook - so it is the value the existing
    webhook routing already matches on.

    `username` and `name` are for the person choosing between accounts in Settings. Either
    can be absent (a business account without a username, or a field Meta declines), which
    is why both are optional rather than defaulted to "".
    """

    business_id: str
    username: str | None = None
    name: str | None = None


class WhatsAppBusinessAccount:
    """A WhatsApp Business Account, as Meta reports it for one id.

    Only what a tenant needs to recognise their own account: the id, and the name Meta
    shows for it.
    """

    def __init__(self, *, waba_id: str, name: str | None = None) -> None:
        self.waba_id = waba_id
        self.name = name

    def __repr__(self) -> str:
        return f"WhatsAppBusinessAccount(waba_id={self.waba_id!r}, name={self.name!r})"


class WhatsAppPhoneNumber:
    """A phone number registered on a WhatsApp Business Account.

    `is_verified` is reported rather than used to filter: an unverified number is a real
    number the tenant owns, and hiding it would make Settings disagree with Meta's own
    dashboard. Phase 10-O.3 does not gate sends on it.
    """

    def __init__(
        self,
        *,
        phone_number_id: str,
        display_number: str | None = None,
        verified_name: str | None = None,
        is_verified: bool = False,
    ) -> None:
        self.phone_number_id = phone_number_id
        self.display_number = display_number
        self.verified_name = verified_name
        self.is_verified = is_verified

    def __repr__(self) -> str:
        return f"WhatsAppPhoneNumber(id={self.phone_number_id!r})"


class MetaGraphClient:
    """Small async client for the three calls Phase 3 needs."""

    def __init__(
        self,
        *,
        app_id: str | None,
        app_secret: str | None,
        graph_version: str,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._version = graph_version
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            headers={"User-Agent": "quali-fly/0.1"},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "MetaGraphClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- URLs -------------------------------------------------------------

    def authorization_url(self, *, redirect_uri: str, state: str, scopes: str) -> str:
        """The Meta login dialog URL to redirect the browser to.

        `response_type=code` so the browser hands back a one-time code rather than an
        access token in the URL fragment - the exchange then happens server to server,
        where the app secret is available and the token never reaches the browser.
        """
        query = urlencode(
            {
                "client_id": self._app_id or "",
                "redirect_uri": redirect_uri,
                "state": state,
                "response_type": "code",
                "scope": scopes,
            }
        )
        return f"{_DIALOG_HOST}/{self._version}/dialog/oauth?{query}"

    def _graph_url(self, path: str) -> str:
        return f"{_GRAPH_HOST}/{self._version}/{path.lstrip('/')}"

    # -- operations -------------------------------------------------------

    async def exchange_authorization_code(self, *, code: str, redirect_uri: str) -> ExchangeResult:
        """Trade the one-time code for a user access token (server-to-server)."""
        if not (self._app_id and self._app_secret):
            raise MetaAuthError("Meta OAuth is not configured on this deployment.")
        payload = await self._get(
            self._graph_url("oauth/access_token"),
            {
                "client_id": self._app_id,
                # Never logged: this URL is only ever used as an httpx request target.
                "client_secret": self._app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise MetaInvalidResponseError("token exchange returned no access_token")
        expires_in = payload.get("expires_in")
        token_type = payload.get("token_type")
        return ExchangeResult(
            access_token=token,
            token_type=token_type if isinstance(token_type, str) else None,
            expires_in=expires_in if isinstance(expires_in, int) else None,
        )

    async def list_authorized_pages(self, user_access_token: str) -> list[AuthorizedPage]:
        """Pages the user granted, each with a page access token.

        A user with no pages, or a user who declined, yields an empty list rather than
        an error: the caller turns that into "nothing to connect", which is the honest
        outcome.
        """
        payload = await self._get(
            self._graph_url("me/accounts"),
            {"fields": "id,name,access_token,tasks", "access_token": user_access_token},
        )
        entries = payload.get("data")
        if not isinstance(entries, list):
            raise MetaInvalidResponseError("accounts response has no data list")

        pages: list[AuthorizedPage] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            page_id = entry.get("id")
            token = entry.get("access_token")
            if page_id is None or not isinstance(token, str) or not token:
                # A page we cannot get a token for is not usable; skip rather than
                # store a connection that can never call the API.
                continue
            tasks = entry.get("tasks")
            pages.append(
                AuthorizedPage(
                    page_id=str(page_id),
                    name=entry.get("name") if isinstance(entry.get("name"), str) else None,
                    access_token=token,
                    tasks=tuple(str(t) for t in tasks) if isinstance(tasks, list) else (),
                )
            )
        return pages

    async def list_instagram_business_accounts(
        self, *, page_id: str, page_access_token: str
    ) -> list[InstagramBusinessAccount]:
        """Instagram Business Accounts linked to this Page.

        **Why the Page is the entry point.** Meta models an Instagram Business Account as
        an edge *owned by* a Page (`/{page-id}/instagram_business_account`), not as a
        standalone object a Page token can discover on its own. So the Page access token is
        both the credential and the address of the thing being asked for - which is exactly
        why an Instagram connection cannot exist without one.

        **Why this is a re-query and not a stored list.** This is the server's authority on
        which Instagram account is genuinely attached to the Page the tenant holds a token
        for. Anything a browser claims about its own Instagram account is unverifiable from
        our side; only this call is.

        A Page with no Instagram Business Account returns
        `{"instagram_business_account": null}` - an empty list, not an error, because "this
        Page has no Instagram account" is a normal state the caller renders as an
        explanation rather than a failure.
        """
        payload = await self._get(
            self._graph_url(page_id),
            {
                "fields": "instagram_business_account{id,username,name}",
                "access_token": page_access_token,
            },
        )
        edge = payload.get("instagram_business_account")
        if edge is None:
            return []
        if not isinstance(edge, dict):
            raise MetaInvalidResponseError("instagram_business_account edge is not an object")
        business_id = edge.get("id")
        if business_id is None:
            # An edge with no id cannot be selected or routed to, so there is nothing
            # usable here. Skipping keeps a malformed entry from becoming a stored id.
            return []
        return [
            InstagramBusinessAccount(
                business_id=str(business_id),
                username=edge.get("username") if isinstance(edge.get("username"), str) else None,
                name=edge.get("name") if isinstance(edge.get("name"), str) else None,
            )
        ]

    async def get_lead(self, *, access_token: str, leadgen_id: str) -> dict[str, Any]:
        """Fetch one lead's answers by `leadgen_id`.

        Requires the `leads_retrieval` permission on the token's Page. The fields are
        requested explicitly rather than relying on a default, because Graph defaults
        are not a contract.
        """
        payload = await self._get(
            self._graph_url(str(leadgen_id)),
            {
                "fields": "id,created_time,ad_id,form_id,field_data",
                "access_token": access_token,
            },
        )
        if not isinstance(payload, dict):
            raise MetaInvalidResponseError("lead response is not an object")
        return payload

    async def send_message(
        self,
        *,
        access_token: str,
        page_id: str,
        recipient_id: str,
        text: str,
        messaging_type: str = "RESPONSE",
        tag: str | None = None,
    ) -> str:
        """Send a text message to a person, returning Meta's message id.

        One endpoint serves both Messenger and Instagram; the caller passes the page id
        and the recipient id, which is a PSID for Messenger and an IGSID for Instagram.

        `messaging_type=RESPONSE` is the documented type for a reply, and Meta refuses it
        outside the 24-hour standard messaging window (error 1545041). Sending outside the
        window requires `messaging_type=MESSAGE_TAG` *together with* a `tag` naming an
        approved use case - Phase 7's follow-up engine passes `HUMAN_AGENT`, which Meta
        documents as usable within 7 days of the person's last message and as available on
        both Messenger and Instagram.
        """
        if not page_id:
            # Instagram-only connections have no page id. Without it there is no
            # endpoint to call, and a confusing Graph error is worse than a clear one.
            raise MetaInvalidResponseError(
                "this Meta connection has no page id, so messages cannot be sent to it"
            )
        body = {
            "recipient": {"id": recipient_id},
            "messaging_type": messaging_type,
            "message": {"text": text},
            "access_token": access_token,
        }
        if tag is not None:
            # Only sent when asked for. A `tag` on a `RESPONSE` send is an invalid
            # parameter from Meta, so this is never a harmless default.
            body["tag"] = tag
        payload = await self._post(
            self._graph_url(f"{page_id}/messages"),
            body,
            # The one call in this client that can change the world. It is what makes a
            # transport failure here an *unknown outcome* rather than a retryable transient.
            side_effecting=True,
        )
        # Documented success shape: {"recipient_id": ..., "message_id": "m_..."}.
        message_id = payload.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise MetaInvalidResponseError("send response contained no message_id")
        return message_id

    async def get_whatsapp_business_account(
        self, *, waba_id: str, access_token: str
    ) -> WhatsAppBusinessAccount:
        """Read this WhatsApp Business Account, if this token can see it.

        The step that makes a WABA id trustworthy. The browser may propose one, but an id
        is only accepted once Meta has answered for that exact id with this connection's
        token - otherwise a tenant could bind an account they do not own, and inbound
        webhooks for it would be routed into this workspace.

        Raises `MetaApiError` for an id Meta rejects or does not know, and `MetaAuthError`
        when the token cannot read the one it does know; the caller turns both into a
        refusal without echoing what the account is called.
        """
        payload = await self._get(
            self._graph_url(waba_id),
            {"fields": "id,name", "access_token": access_token},
        )
        account_id = payload.get("id")
        if not isinstance(account_id, str) or not account_id:
            raise MetaInvalidResponseError("whatsapp business account response contained no id")
        name = payload.get("name")
        return WhatsAppBusinessAccount(
            waba_id=account_id,
            name=name if isinstance(name, str) and name else None,
        )

    async def list_whatsapp_phone_numbers(
        self, *, waba_id: str, access_token: str
    ) -> list[WhatsAppPhoneNumber]:
        """Phone numbers registered on this WhatsApp Business Account.

        The server's authority on which numbers the tenant actually owns. Mirrors
        `list_instagram_business_accounts` in purpose: the browser proposes an id, this
        decides whether it is real.

        Requires `whatsapp_business_messaging` on the token. `verified_name` is what Meta
        shows for the business, which is what a tenant recognises in a list - an
        unverified number is still returned rather than hidden, because refusing to show it
        would leave the tenant with a number they can see in Meta and no way to tell us
        about it. `is_verified` is reported so Settings can say which is which.
        """
        payload = await self._get(
            self._graph_url(f"{waba_id}/phone_numbers"),
            {
                "fields": "id,display_phone_number,verified_name,quality_rating,is_verified",
                "access_token": access_token,
            },
        )
        entries = payload.get("data")
        if not isinstance(entries, list):
            raise MetaInvalidResponseError("phone_numbers response has no data list")

        numbers: list[WhatsAppPhoneNumber] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            phone_number_id = entry.get("id")
            if phone_number_id is None:
                # An id we could neither match nor address a send to is not selectable.
                continue
            numbers.append(
                WhatsAppPhoneNumber(
                    phone_number_id=str(phone_number_id),
                    display_number=(
                        entry.get("display_phone_number")
                        if isinstance(entry.get("display_phone_number"), str)
                        else None
                    ),
                    verified_name=(
                        entry.get("verified_name")
                        if isinstance(entry.get("verified_name"), str)
                        else None
                    ),
                    is_verified=bool(entry.get("is_verified")),
                )
            )
        return numbers

    async def send_whatsapp_message(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        recipient_id: str,
        text: str,
    ) -> str:
        """Send a WhatsApp text message, returning Meta's `wamid`.

        `POST /{phone_number_id}/messages` with the Cloud API body
        (`messaging_product`/`to`/`type`/`text.body`) - a different endpoint *and* a
        different body shape from the page messaging send, which is the whole reason
        WhatsApp earns its own adapter.

        `side_effecting=True` for the same reason `send_message` sets it: a timeout here
        may still have delivered a message to a real person, so the caller must record an
        unknown outcome and stop rather than retry.

        **The 24-hour window is not handled here.** The Cloud API requires an approved
        message template outside the customer service window, and template management is
        out of scope for this phase. The caller checks the window and refuses first, so this
        method is only ever reached inside it; sending a free-form text outside the window
        would burn the attempt and produce a permanent Meta rejection.
        """
        if not phone_number_id:
            raise MetaInvalidResponseError(
                "this Meta connection has no WhatsApp phone number, so messages cannot "
                "be sent to it"
            )
        payload = await self._post(
            self._graph_url(f"{phone_number_id}/messages"),
            {
                "messaging_product": WHATSAPP_PRODUCT,
                "to": recipient_id,
                "type": "text",
                "text": {"body": text},
                "access_token": access_token,
            },
            side_effecting=True,
        )
        # Documented success shape:
        #   {"messaging_product": "whatsapp", "messages": [{"id": "wamid..."}]}
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise MetaInvalidResponseError("whatsapp send response contained no messages")
        first = messages[0]
        message_id = first.get("id") if isinstance(first, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise MetaInvalidResponseError("whatsapp send response contained no message id")
        return message_id

    # -- transport --------------------------------------------------------

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._send("GET", url, params=params)

    async def _post(
        self, url: str, form: dict[str, Any], *, side_effecting: bool = False
    ) -> dict[str, Any]:
        """POST with a form body.

        The Send API documents `recipient` and `message` as JSON *object literals* in a
        form body, so those are JSON-encoded. Scalars - `access_token`,
        `messaging_type` - are sent verbatim, because quoting a token is not what the
        documented request looks like and Meta need not guess that we meant it.

        `side_effecting` marks the one call that can change the world. It is what decides how
        a transport failure is classified: a timeout on a read left nothing behind and is
        simply retried, while a timeout on a send may have delivered a message to a real
        person and must be reported as an unknown outcome instead.
        """
        return await self._send(
            "POST",
            url,
            form={
                key: json.dumps(value) if isinstance(value, (dict, list)) else str(value)
                for key, value in form.items()
            },
            side_effecting=side_effecting,
        )

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
        side_effecting: bool = False,
    ) -> dict[str, Any]:
        """One bounded request. Turns Graph's error envelope into a typed exception.

        The request URL and body contain the token, so neither is ever passed to a log
        record or an exception message - only the method, the path and the Graph error
        code are.
        """
        try:
            response = await self._client.request(method, url, params=params, data=form)
        except httpx.TimeoutException as exc:
            # The request may have reached Meta and been accepted; we simply never saw the
            # answer. Not a transient failure - an unknown one, and not retryable without
            # risking a duplicate message to a real person.
            if side_effecting:
                raise MetaUnknownOutcomeError("Meta request timed out") from exc
            # A read left nothing behind, so repeating it cannot duplicate anything.
            raise MetaTransientError("Meta request timed out") from exc
        except httpx.HTTPError as exc:
            # The type name is safe to include and carries no credential material. Same
            # split as the timeout: a failed send is ambiguous, a failed read is not.
            if side_effecting:
                raise MetaUnknownOutcomeError(
                    f"Meta request failed: {type(exc).__name__}"
                ) from None
            raise MetaTransientError(f"Meta request failed: {type(exc).__name__}") from None

        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise MetaInvalidResponseError("Meta response exceeded the size limit")

        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}

        if response.status_code >= 400 or "error" in body:
            self._raise_for_error(response.status_code, body)
        return body

    def _raise_for_error(self, status: int, body: dict[str, Any]) -> None:
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = error.get("code") if isinstance(error.get("code"), int) else None
        subcode = error.get("error_subcode")
        error_type = error.get("type")
        log.warning(
            "meta_api_error",
            extra={
                "http_status": status,
                "meta_error_code": code,
                "meta_error_subcode": subcode,
                "meta_error_type": error_type if isinstance(error_type, str) else None,
            },
        )

        if code in _TRANSIENT_ERROR_CODES or status in (429, 500, 502, 503, 504):
            raise MetaTransientError(f"Meta temporarily unavailable (code {code})", code=code)
        if code in _AUTH_ERROR_CODES:
            raise MetaAuthError(f"Meta rejected our credential (code {code})", code=code)
        if code in _MESSAGE_WINDOW_CODES:
            raise MetaMessageWindowClosed(
                "Meta refused the message: the 24-hour messaging window has closed",
                code=code,
            )
        if code in _RECIPIENT_UNAVAILABLE_CODES:
            # Blocked, deactivated, or otherwise unreachable. Retrying cannot help.
            raise MetaAuthError(f"recipient is not available (code {code})", code=code)
        if status >= 400:
            # 400 with an unrecognized code is usually a bad request: a revoked
            # permission, an unknown leadgen_id, a lead that is not ready yet. Not
            # retryable, and not a transport problem.
            raise MetaApiError(f"Meta rejected the request (code {code})", code=code)
        raise MetaInvalidResponseError(f"Meta returned an error (code {code})", code=code)


__all__ = [
    "AuthorizedPage",
    "ExchangeResult",
    "InstagramBusinessAccount",
    "MetaApiError",
    "MetaAuthError",
    "MetaGraphClient",
    "MetaInvalidResponseError",
    "MetaMessageWindowClosed",
    "MetaTransientError",
    "MetaUnknownOutcomeError",
    "WhatsAppPhoneNumber",
]
