"""One channel abstraction, three thin implementations.

The PRD is explicit that no phase may become several separate conversation engines, and
after reading Meta's current documentation the honest reason is stronger than "good
architecture": **Messenger and Instagram already share the send endpoint.** Both post to
`/{page_id}/messages` with a `recipient.id`; only the recipient's id type and the required
permission differ. Verified 2026-09-28:

    Messenger    POST /{page_id}/messages   recipient = PSID     pages_messaging
    Instagram    POST /{page_id}/messages   recipient = IGSID    instagram_manage_messages

**WhatsApp is the first surface that does not**, and Phase 10-O.3 is the phase the module
docstring above used to say would have to wait for proof:

    WhatsApp     POST /{phone_number_id}/messages   recipient = wa_id
                 body: messaging_product=whatsapp, to, type=text, text.body
                 permission: whatsapp_business_messaging

So this file earns its second implementation, and only the transport part of it. The
difference is carried by one extra field on `SendRequest` - `messaging_product` - rather
than by a second engine: the conversation, message, AI, follow-up and handoff paths are
identical and stay shared. That is the smallest honest expression of "different endpoint,
same conversation".

What an adapter does here:
  * declare which payload `object` values it claims
  * name the permission the tenant must have granted
  * build the send request, including which product the send is for

Everything else - find-or-create the thread, deduplicate, persist, retry, classify
failures - lives in the engine and is shared.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from app.models.conversation import Channel

# Meta's documented messaging object types. A Page webhook carries "page"; an Instagram
# business account carries "instagram"; a WhatsApp Cloud API webhook carries the account.
MESSENGER_OBJECT = "page"
INSTAGRAM_OBJECT = "instagram"
WHATSAPP_OBJECT = "whatsapp_business_account"

#: The only non-page messaging product. `None` on a `SendRequest` means "page messaging",
#: which is what Messenger and Instagram use and what every pre-Phase-10-O.3 send did.
WHATSAPP_PRODUCT = "whatsapp"


class UnknownRecipient(Exception):
    """The conversation has no participant id to send to.

    Not a Meta error: it means the conversation was never given one, which should be
    impossible for a thread created from an inbound message.
    """


@dataclass(frozen=True, slots=True)
class SendRequest:
    """A send, ready to hand to the Graph client."""

    recipient_id: str
    text: str
    # `RESPONSE` is the documented messaging_type for a reply inside the 24-hour window.
    messaging_type: str = "RESPONSE"
    # Message tag, sent only alongside `messaging_type=MESSAGE_TAG`. `None` for every
    # Phase 4 send; Phase 7's follow-ups set `HUMAN_AGENT` when a send falls outside the
    # 24-hour window but inside Meta's 7-day tagged-message allowance.
    tag: str | None = None
    # Phase 10-O.3. `None` is page messaging (`POST /{page_id}/messages`), which is every
    # Messenger and Instagram send. `WHATSAPP_PRODUCT` selects the Cloud API send
    # (`POST /{phone_number_id}/messages`), whose body shape Meta defines separately and
    # which has no `messaging_type`/`tag` concept at all - outside its 24-hour customer
    # service window WhatsApp requires an approved *template*, which is out of scope for
    # this phase, so that case is refused by the sender rather than silently attempted.
    messaging_product: str | None = None


class ChannelAdapter(Protocol):
    """What the engine needs from a surface. Deliberately three members."""

    channel: Channel
    payload_objects: tuple[str, ...]
    required_permission: str

    def build_send(
        self,
        *,
        participant_id: str,
        text: str,
        messaging_type: str | None = None,
        tag: str | None = None,
    ) -> SendRequest: ...


class MessengerAdapter:
    channel = Channel.MESSENGER
    payload_objects = (MESSENGER_OBJECT,)
    required_permission = "pages_messaging"

    def build_send(
        self,
        *,
        participant_id: str,
        text: str,
        messaging_type: str | None = None,
        tag: str | None = None,
    ) -> SendRequest:
        if not participant_id:
            raise UnknownRecipient("conversation has no Messenger participant id")
        return SendRequest(
            recipient_id=participant_id,
            text=text,
            messaging_type=messaging_type or "RESPONSE",
            tag=tag,
        )


class InstagramAdapter:
    channel = Channel.INSTAGRAM
    payload_objects = (INSTAGRAM_OBJECT,)
    required_permission = "instagram_manage_messages"

    def build_send(
        self,
        *,
        participant_id: str,
        text: str,
        messaging_type: str | None = None,
        tag: str | None = None,
    ) -> SendRequest:
        # The recipient is an Instagram-scoped id, but the request is otherwise
        # identical to Messenger's - that is the point of this file.
        if not participant_id:
            raise UnknownRecipient("conversation has no Instagram participant id")
        return SendRequest(
            recipient_id=participant_id,
            text=text,
            messaging_type=messaging_type or "RESPONSE",
            tag=tag,
        )


class WhatsAppAdapter:
    """Phase 10-O.3. The third surface, and the first with its own endpoint.

    `messaging_type` and `tag` are accepted and deliberately ignored. They are Page
    Messaging concepts; the Cloud API has no equivalent, and inventing one would mean
    sending a field Meta documents as invalid. The 24-hour window that governs them is
    enforced by the sender, which has the conversation's `last_inbound_at`.
    """

    channel = Channel.WHATSAPP
    payload_objects = (WHATSAPP_OBJECT,)
    required_permission = "whatsapp_business_messaging"

    def build_send(
        self,
        *,
        participant_id: str,
        text: str,
        messaging_type: str | None = None,
        tag: str | None = None,
    ) -> SendRequest:
        # The recipient is a WhatsApp-scoped id (`wa_id`, a phone number in E.164). It is
        # the conversation key, exactly as a PSID or IGSID is for the other two surfaces.
        if not participant_id:
            raise UnknownRecipient("conversation has no WhatsApp participant id")
        return SendRequest(
            recipient_id=participant_id,
            text=text,
            messaging_type="text",
            tag=None,
            messaging_product=WHATSAPP_PRODUCT,
        )


_ADAPTERS: dict[Channel, ChannelAdapter] = {
    Channel.MESSENGER: MessengerAdapter(),
    Channel.INSTAGRAM: InstagramAdapter(),
    Channel.WHATSAPP: WhatsAppAdapter(),
}

# Reverse index, so routing an event to a surface does not scan the registry.
_BY_PAYLOAD_OBJECT: dict[str, ChannelAdapter] = {
    obj: adapter for adapter in _ADAPTERS.values() for obj in adapter.payload_objects
}


def adapter_for(channel: Channel) -> ChannelAdapter:
    return _ADAPTERS[channel]


def channel_for_payload(object_type: str | None) -> ChannelAdapter | None:
    """Which surface sent this event, if it is one we handle.

    `None` means the event is not a messaging event for a known surface - a leadgen
    change, an Instagram comment, a permission update - and the caller must leave it
    alone rather than guess.
    """
    if object_type is None:
        return None
    return _BY_PAYLOAD_OBJECT.get(str(object_type))


def describe() -> list[dict[str, Any]]:
    """Introspection for the API/UI and for tests that assert the registry is complete."""
    return [
        {
            "channel": adapter.channel.value,
            "payload_objects": list(adapter.payload_objects),
            "required_permission": adapter.required_permission,
        }
        for adapter in _ADAPTERS.values()
    ]


__all__ = [
    "ChannelAdapter",
    "InstagramAdapter",
    "MessengerAdapter",
    "SendRequest",
    "UnknownRecipient",
    "WHATSAPP_OBJECT",
    "WHATSAPP_PRODUCT",
    "WhatsAppAdapter",
    "adapter_for",
    "channel_for_payload",
    "describe",
]
