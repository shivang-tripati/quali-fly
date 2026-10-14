"""A fake Meta Graph API.

The database is never mocked in this suite - what the tests prove about RLS, unique
constraints and `FOR UPDATE SKIP LOCKED` is database behaviour, and a mock would make it
meaningless. The Meta HTTP boundary is the one thing genuinely outside our control, so
that is what gets substituted here.

`FakeGraph` records the arguments it was called with, which is how a test can assert
that the outbound call carried the *decrypted* page token rather than the ciphertext, and
that no request ever received the raw user token.
"""

import json

from app.services.meta_graph import (
    AuthorizedPage,
    ExchangeResult,
    InstagramBusinessAccount,
    MetaApiError,
    MetaAuthError,
    MetaTransientError,
    WhatsAppBusinessAccount,
    WhatsAppPhoneNumber,
)

USER_TOKEN = "user-token-from-oauth-exchange"
PAGE_TOKEN = "page-token-for-lead-retrieval"


class FakeGraph:
    """Implements the three methods `MetaGraphClient` exposes."""

    def __init__(self) -> None:
        # Recorded calls, in order.
        self.exchange_calls: list[dict] = []
        self.accounts_calls: list[str] = []
        self.lead_calls: list[dict] = []
        self.authorization_url_calls: list[dict] = []
        self.instagram_calls: list[dict] = []
        self.aclose_calls = 0

        # Canned answers, overridable per test.
        self.authorization_url_base = "https://www.facebook.com/v26.0/dialog/oauth?client_id=fake"
        self.exchange_result = ExchangeResult(
            access_token=USER_TOKEN, token_type="bearer", expires_in=5_184_000
        )
        self.exchange_error: Exception | None = None
        self.pages: list[AuthorizedPage] = [
            AuthorizedPage(
                page_id="1000000000000001",
                name="Acme Auto Sales",
                access_token=PAGE_TOKEN,
                tasks=("ADVERTISE", "MANAGE"),
            )
        ]
        self.pages_error: Exception | None = None
        # leadgen_id -> response dict, or an Exception instance to raise.
        self.leads: dict[str, object] = {}
        # Outbound sends (Phase 4/5). `send_error` makes the next send raise, which is how
        # a test reproduces a Meta-side rejection - the case where the message row is
        # persisted and the run is marked FAILED even though the provider worked.
        self.sends: list[dict] = []
        self.send_error: Exception | None = None
        self.next_message_id = "m_ag-sent-1"
        self._send_count = 0

        # Instagram Business Accounts per Page id. Keyed by Page because that is the
        # address Meta exposes the edge at: a Page with no entry has no Instagram account,
        # which is the common case and needs no programming to be represented.
        self.instagram_accounts: dict[str, list[InstagramBusinessAccount]] = {}
        self.instagram_error: Exception | None = None
        # Phase 10-O.3: the same shape, for the WhatsApp Business Account's phone numbers.
        self.phone_numbers: dict[str, list[WhatsAppPhoneNumber]] = {}
        self.business_accounts: dict[str, WhatsAppBusinessAccount] = {}
        self.account_calls: list[dict] = []
        self.account_error: Exception | None = None
        self.phone_number_calls: list[dict] = []
        self.phone_numbers_error: Exception | None = None
        self.whatsapp_sends: list[dict] = []
        self.whatsapp_send_error: Exception | None = None
        self._whatsapp_send_count = 0

    def program_send_error(self, error: Exception) -> None:
        self.send_error = error

    async def send_message(self, **kwargs) -> str:
        self.sends.append(kwargs)
        if self.send_error is not None:
            raise self.send_error
        self._send_count += 1
        # A distinct id per send, as a real server returns. Returning a constant would
        # collide on `uq_messages_dedupe` (conversation_id, direction, meta_message_id) the
        # moment a test sends twice in one conversation - which is exactly what Phase 7's
        # follow-up sequence does. The suffix keeps the recognizable `m_ag-sent-` prefix
        # for tests that assert on its shape.
        if self._send_count == 1:
            return self.next_message_id
        return f"{self.next_message_id}-{self._send_count}"

    def program_lead(self, leadgen_id: str, payload: dict) -> None:
        self.leads[leadgen_id] = payload

    def program_lead_error(self, leadgen_id: str, error: Exception) -> None:
        self.leads[leadgen_id] = error

    def authorization_url(self, *, redirect_uri: str, state: str, scopes: str) -> str:
        """Must echo back exactly the one `state` it was given.

        A second `state` parameter would make a test that parses the URL pick the wrong
        one, which reads as a spurious "invalid state" rather than as a fake's bug.
        """
        self.authorization_url_calls.append(
            {"redirect_uri": redirect_uri, "state": state, "scopes": scopes}
        )
        return f"{self.authorization_url_base}&state={state}"

    async def exchange_authorization_code(self, *, code: str, redirect_uri: str) -> ExchangeResult:
        self.exchange_calls.append({"code": code, "redirect_uri": redirect_uri})
        if self.exchange_error is not None:
            raise self.exchange_error
        return self.exchange_result

    async def list_authorized_pages(self, user_access_token: str) -> list[AuthorizedPage]:
        self.accounts_calls.append(user_access_token)
        if self.pages_error is not None:
            raise self.pages_error
        return list(self.pages)

    async def list_instagram_business_accounts(
        self, *, page_id: str, page_access_token: str
    ) -> list[InstagramBusinessAccount]:
        """The Page's Instagram edge. `instagram_calls` records what we asked with."""
        self.instagram_calls.append({"page_id": page_id, "page_access_token": page_access_token})
        if self.instagram_error is not None:
            raise self.instagram_error
        return list(self.instagram_accounts.get(page_id, []))

    async def get_whatsapp_business_account(
        self, *, waba_id: str, access_token: str
    ) -> WhatsAppBusinessAccount:
        """Meta answering for one account id.

        Only ids present in `business_accounts` are confirmed. That is the whole point of
        the call: an id nobody programmed here stands for an account this token cannot see,
        which is exactly what a tenant binding someone else's account would look like.
        """
        self.account_calls.append({"waba_id": waba_id, "access_token": access_token})
        if self.account_error is not None:
            raise self.account_error
        account = self.business_accounts.get(waba_id)
        if account is None:
            raise MetaApiError("Meta rejected the request (code 803)", code=803)
        return account

    async def list_whatsapp_phone_numbers(
        self, *, waba_id: str, access_token: str
    ) -> list[WhatsAppPhoneNumber]:
        """The account's registered numbers. `phone_number_calls` records what we asked with."""
        self.phone_number_calls.append({"waba_id": waba_id, "access_token": access_token})
        if self.phone_numbers_error is not None:
            raise self.phone_numbers_error
        return list(self.phone_numbers.get(waba_id, []))

    async def send_whatsapp_message(
        self, *, access_token: str, phone_number_id: str, recipient_id: str, text: str
    ) -> str:
        """Phase 10-O.3's transport. Records the send so a test can assert the address used."""
        self.whatsapp_sends.append(
            {
                "access_token": access_token,
                "phone_number_id": phone_number_id,
                "recipient_id": recipient_id,
                "text": text,
            }
        )
        if self.whatsapp_send_error is not None:
            raise self.whatsapp_send_error
        self._whatsapp_send_count += 1
        return f"wamid.sent-{self._whatsapp_send_count}"

    async def get_lead(self, *, access_token: str, leadgen_id: str) -> dict:
        self.lead_calls.append({"access_token": access_token, "leadgen_id": leadgen_id})
        outcome = self.leads.get(leadgen_id)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise MetaApiError("no test lead was programmed", code=100)
        return dict(outcome)

    async def aclose(self) -> None:
        self.aclose_calls += 1

    async def __aenter__(self) -> "FakeGraph":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- assertions helpers ----------------------------------------------

    @property
    def leadgen_ids_seen(self) -> list[str]:
        return [call["leadgen_id"] for call in self.lead_calls]

    @property
    def tokens_seen_by_lead_retrieval(self) -> set[str]:
        return {call["access_token"] for call in self.lead_calls}


class FakeRegistry:
    """A stand-in for `LLMRegistry` (Phase 5).

    Substituted at the same boundary as `FakeGraph`: the LLM HTTP calls are genuinely
    outside our control, and the thing worth testing is what the pipeline does with the
    answers. The registry itself is a real class with its own unit tests, so replacing it
    here is not hiding logic under test.

    `queue` is consumed in order. An `Exception` instance is raised, an `LLMResult` is
    returned, and an empty queue means the test did not expect another call - which raises
    rather than returning something plausible, so an unexpected extra provider call fails
    loudly instead of passing quietly.
    """

    def __init__(self) -> None:
        self.queue: list[object] = []
        self.requests: list = []
        self.aclose_calls = 0

    def program(self, *outcomes: object) -> "FakeRegistry":
        self.queue.extend(outcomes)
        return self

    def program_response(self, **overrides) -> "FakeRegistry":
        """Queue a well-formed structured response, overriding any field.

        Serialised to text, because that is what a provider actually returns: the
        pipeline parses `LLMResult.content` as JSON, and a fake handing back a dict would
        quietly skip the most important step in the code under test.
        """
        payload = {
            "message": "Thanks for reaching out! Could you tell me a bit more about what you need?",
            "budget": 10,
            "timeline": 10,
            "fit": 15,
            "authority": 5,
            "human_handoff": False,
        }
        payload.update(overrides)
        return self.program_text(json.dumps(payload))

    def program_text(self, text: str) -> "FakeRegistry":
        return self.program(_result(text))

    async def generate_structured(self, request):
        self.requests.append(request)
        if not self.queue:
            raise AssertionError(
                "FakeRegistry was called more times than the test programmed "
                f"(calls so far: {len(self.requests)})"
            )
        outcome = self.queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        self.aclose_calls += 1


def _result(text: str):
    from app.schemas.ai import LLMResult

    return LLMResult(
        content=text,
        provider="groq",
        model="llama-3.1-8b-instant",
        prompt_tokens=123,
        completion_tokens=45,
        latency_ms=321,
    )


class FakeEmbeddingProvider:
    """A scripted `EmbeddingProvider` for tests.

    Substituted at the same boundary as `FakeRegistry` and `FakeGraph`. Crucially it
    declares **the same dimension** as the real provider, because the point of the Phase 6
    contract is that the two agree - a fake with a convenient 4-dimensional output would
    make every dimension assertion in the suite meaningless.

    Two failure modes are available because they are the ones that matter operationally: a
    provider that returns the wrong width, and one that raises.
    """

    name = "fake"
    model = "fake-384"

    def __init__(self, dimension: int = 384) -> None:
        self.dimension = dimension
        self.batches: list[list[str]] = []
        self.error: Exception | None = None
        #: Set to force a specific width, to prove a wrong dimension is rejected.
        self.returned_dimension: int | None = None

    def program_error(self, error: Exception) -> "FakeEmbeddingProvider":
        self.error = error
        return self

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.error is not None:
            raise self.error
        self.batches.append(list(texts))
        width = self.returned_dimension or self.dimension
        return [self._vector(text, width) for text in texts]

    @staticmethod
    def _vector(text: str, width: int) -> list[float]:
        # Position-weighted by where characters fall, so two different texts produce
        # different vectors. Deterministic, so ordering assertions are stable.
        vector = [0.0] * width
        for index, char in enumerate(text):
            vector[(ord(char) + index) % width] += 1.0
        return vector

    @property
    def call_count(self) -> int:
        return len(self.batches)

    @property
    def texts_embedded(self) -> list[str]:
        return [text for batch in self.batches for text in batch]


def transient_error(code: int = 4) -> MetaTransientError:
    return MetaTransientError(f"rate limited (code {code})", code=code)


def auth_error(code: int = 190) -> MetaAuthError:
    return MetaAuthError(f"token rejected (code {code})", code=code)


__all__ = [
    "PAGE_TOKEN",
    "USER_TOKEN",
    "FakeEmbeddingProvider",
    "FakeGraph",
    "FakeRegistry",
    "auth_error",
    "transient_error",
]
