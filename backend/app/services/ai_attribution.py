"""The value object that carries provider usage from the AI layer to the send path.

It lives in its own module because two layers need it and neither should import the
other: the AI service knows about providers, and `conversations.py` must not. A four-field
frozen dataclass is enough, and it keeps the dependency arrow pointing one way.
"""

from app.services.conversations import AIAttribution

__all__ = ["AIAttribution"]
