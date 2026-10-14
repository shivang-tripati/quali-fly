"""Authenticated encryption for secrets at rest.

Meta access tokens are bearer credentials: anyone holding one can read the tenant's
leads. They are therefore stored encrypted and decrypted only at the single boundary
where an outbound Graph call needs them.

The standard library has no AEAD cipher, so this uses AES-256-GCM from `cryptography`.
GCM rather than plain AES because it authenticates as well as encrypts: a tampered
ciphertext fails to decrypt instead of yielding garbage that we would then send to
Meta as a token.

    storage format:  v1.<base64url(nonce)>.<base64url(ciphertext||tag)>

The version prefix leaves room to rotate the algorithm later without guessing which
rows are which. The nonce is fresh per encryption, so encrypting the same token twice
produces different ciphertext - which is required for GCM, not a defect.
"""

import base64
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = "v1"
_NONCE_BYTES = 12  # GCM's recommended nonce size
_KEY_BYTES = 32  # AES-256

# Bound into every encryption so a blob cannot be moved between purposes: a token
# ciphertext decrypted as, say, a webhook verification token simply fails.
_AAD = b"quali-fly:meta-access-token:v1"


class DecryptionError(Exception):
    """The ciphertext is malformed, truncated, or was tampered with.

    Deliberately carries no detail about the input: the caller is a log line, and a
    decryption failure must never echo key material.
    """


def generate_key() -> str:
    """A fresh base64url 32-byte key. For generating META_TOKEN_ENCRYPTION_KEY once."""
    return base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).decode().rstrip("=")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _load_key(encoded: str | None) -> bytes:
    """Decode and length-check the configured key. A wrong-size key is a config bug."""
    if not encoded:
        raise DecryptionError("no encryption key is configured")
    try:
        key = _b64d(encoded)
    except (ValueError, TypeError):
        raise DecryptionError("encryption key is not valid base64url") from None
    if len(key) != _KEY_BYTES:
        raise DecryptionError(f"encryption key must decode to {_KEY_BYTES} bytes")
    return key


def encrypt(plaintext: str, *, key: str | None) -> str:
    """Encrypt a secret for storage. `key` comes from configuration, never from code."""
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(_load_key(key)).encrypt(nonce, plaintext.encode(), _AAD)
    return f"{_VERSION}.{_b64e(nonce)}.{_b64e(sealed)}"


def decrypt(envelope: str, *, key: str | None) -> str:
    """Decrypt a stored secret. Raises `DecryptionError` on any failure.

    InvalidTag is what a modified ciphertext produces, and it is also what a wrong key
    produces. The two are indistinguishable by design, so neither is reported.
    """
    aesgcm = AESGCM(_load_key(key))
    try:
        version, nonce_b64, sealed_b64 = envelope.split(".")
        if version != _VERSION:
            raise DecryptionError("unsupported ciphertext version")
        return aesgcm.decrypt(_b64d(nonce_b64), _b64d(sealed_b64), _AAD).decode()
    except InvalidTag:
        raise DecryptionError("ciphertext failed authentication") from None
    except (ValueError, AttributeError):
        raise DecryptionError("ciphertext is malformed") from None


__all__ = ["DecryptionError", "decrypt", "encrypt", "generate_key"]
