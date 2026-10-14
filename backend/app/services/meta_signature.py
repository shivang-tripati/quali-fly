"""Meta webhook signature validation.

The signature covers the *exact bytes Meta sent*, so validation must run on the raw
request body. Parsing the JSON and re-serialising it would change the byte
representation (key order, whitespace, unicode escaping) and break verification - so
the caller hands us `request.body()` and nothing else.
"""

import hashlib
import hmac
import logging

log = logging.getLogger("app.meta")

_ALGORITHM_PREFIX = "sha256="
_HEX_DIGEST_LENGTH = 64


class SignatureError(Exception):
    """Raised for any reason a signature cannot be trusted.

    The reason is deliberately not propagated to the caller: distinguishing
    "unknown page" from "bad secret" would tell an attacker which guesses were close.
    Callers translate this into a single `META_WEBHOOK_SIGNATURE_INVALID` response.
    """


def parse_signature_header(value: str | None) -> bytes:
    """Return the expected digest, or raise. Rejects every unsupported form."""
    if not value:
        raise SignatureError("missing signature header")
    if not value.startswith(_ALGORITHM_PREFIX):
        # `sha1=` is the legacy form. Refusing it is deliberate, not an oversight.
        raise SignatureError("unsupported signature algorithm")
    digest_hex = value[len(_ALGORITHM_PREFIX) :]
    if len(digest_hex) != _HEX_DIGEST_LENGTH:
        raise SignatureError("malformed signature length")
    try:
        return bytes.fromhex(digest_hex)
    except ValueError:
        raise SignatureError("malformed signature encoding") from None


def compute_signature(app_secret: str, raw_body: bytes) -> bytes:
    return hmac.new(app_secret.encode(), raw_body, hashlib.sha256).digest()


def verify_signature(app_secret: str | None, raw_body: bytes, signature_header: str | None) -> None:
    """Validate `X-Hub-Signature-256` over the raw body, or raise `SignatureError`."""
    if not app_secret:
        # Fail closed without telling the caller the app is unconfigured.
        log.error("meta_app_secret is not configured; rejecting webhook")
        raise SignatureError("app secret is not configured")

    provided = parse_signature_header(signature_header)
    expected = compute_signature(app_secret, raw_body)
    if not hmac.compare_digest(expected, provided):
        raise SignatureError("signature mismatch")


def hash_verify_token(token: str) -> str:
    """Stored form of a Meta verification token.

    The token is a shared secret chosen by the tenant, so only its digest is kept.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token_matches(candidate: str | None, stored_hash: str | None) -> bool:
    """Constant-time comparison of a supplied verification token against the digest."""
    if not candidate or not stored_hash:
        return False
    return hmac.compare_digest(hash_verify_token(candidate), stored_hash)
